#!/usr/bin/env python3
"""Free Inference Watchdog — one tick per invocation. Stdlib only, zero tokens.

Delivery topology (fix-round-2 S4): the webhook in this project's
.env ($DISCORD_WEBHOOK_INFERENCE_WATCHDOG) is the ONLY alert delivery
channel.
The Hermes cron job runs silent (--deliver local): stdout stays local and is
not a delivery path; stderr carries fatal diagnostics for the operator.
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import alive
import diffing
import probe_select
import probe_state
import notify
import providers
import state
from config_loader import PROVIDERS
from envfile import parse_envfile
from probe_zero_credit import probe_model, Result

DEFAULT_CADENCE_S = 1 * 3600

# Serial probe spacing (seconds) — 12 RPM. History: 10s (6 RPM) was a
# conservative guess at b.ai's undocumented rate limit; 4s (operator
# decision 2026-09-13, morning) made the full 47-model re-probe pass fit
# PROBE_PHASE_BUDGET_S in one tick; the operator then chose 5s pacing twice
# ("Idk maybe 5s", "I did say 5") — rate-limit gentleness over one-tick
# completion. Consequence, accepted knowingly: at 5s a full 47-model pass
# costs 44×(5+~1) ≈ 280s realistic and EXCEEDS the 260s budget, so the
# last few models are skipped each pass and resume the next tick as tier-1
# queue items (no verdict yet) via probe_state persistence — the same
# self-healing mechanism every truncation uses. The ONE-TICK-FULL-PASS
# guarantee is superseded by this pacing choice; do NOT go faster (below
# 5s) without the operator's say-so, and if b.ai burst-kills at this rate,
# probes return DEFER and the sticky-roster rule (a DEFER never overwrites
# a prior verdict) contains the damage — watch a tick's DEFER pattern.
PROBE_INTERVAL_S = 5
PROBE_TIMEOUT_S = 30

# Probe-phase budget (seconds). The budget clock starts at the TOP of
# fetch_all() (probe_phase_start), BEFORE the provider fetch loop — the
# serial catalog fetches (15s timeout each, providers.TIMEOUT_S) run INSIDE
# the 260s, so do NOT add ~30s of fetches on top (that double-counts). The
# check is "elapsed >= budget" before each probe, so the last probe can
# start at 259.9s and run sleep(5) + 30s probe timeout ≈ 35s past it:
# worst case ~295s from fetch_all() start to save_probe_state — the
# PERSIST point.
#
# Why 260: the Hermes cron runner SIGKILLs the wrapper at 300s (the window
# was briefly raised to 1800s on 2026-09-13 after the 07:17 tick ran 428s,
# then RESTORED to 300s the same day — commit a8b61e9). History of the
# spacing/budget pair: originally 240s with 10s spacing, where a full
# 47-model pass needed 46×10=460s of sleeps alone and could NEVER fit one
# tick; the budget rose 240→260 when spacing dropped to 4s so the pass
# nearly fit (46×4 + 47 fast probes ≈ 200-230s). The operator has since
# set spacing to 5s (pacing choice, see PROBE_INTERVAL_S), so the pass
# takes ~280s realistic and only NEARLY fits one tick now: the budget cuts
# the last few models each pass and they resume next tick via probe_state
# persistence. And the ~295s worst-case persist point stays under the
# restored 300s kill with margin. That overshoot math is why 260 is the
# ceiling, not a comfort number: budget + sleep(5) + PROBE_TIMEOUT_S must
# stay < 300.
#
# The invariant is "save_probe_state precedes the kill": probe progress
# always persists; a kill during confirm_diffs' unconditional 180s recheck
# nap costs that tick's roster write (roster lags one tick), never
# probe_state — no spiral.
#
# The 30-min lock window (LOCK_STALE_S=1800) is only the OUTER bound and
# is unaffected. When the budget is exhausted, remaining queue items
# stay unprobed but the probed subset still persists (save_probe_state
# runs at the end of build_fetch_all) — the next tick resumes from
# cached verdicts (self-healing, no death spiral). At the operator's 5s
# pacing a tail cut is the NORMAL every-pass outcome (~4 models deferred,
# ~43+ carried), not pathological; only a tick where every probe burns its
# full 30s timeout cuts deep. Either way the spread across hourly ticks is
# the designed fallback, oldest-first priority preserved.
PROBE_PHASE_BUDGET_S = 260


# ---------- provider plumbing ----------

def build_fetch_all(env, state_dir=None, now=None, sleep=time.sleep,
                    dry_run=False):
    """Return fetch_all() -> ({name: ids|None}, {name: meta_dict}).

    Failures become None in the results map (sticky) and are ABSENT from the
    meta map. Meta is passive telemetry only (nous x-ratelimit headers, R2-6).

    Zero-credit-probe providers go through a SERIAL throttled probe loop:
    verdicts persist via probe_state; the queue is probe_select-driven; the
    roster is verdict-filtered (ONLY FREE-verdict models).

    state_dir: Path to the state directory (probe_state.json lives here).
    now: epoch value (int/float), or None for time.time().
    sleep: callable for throttle spacing (inject in tests).
    dry_run: if True, fetch + diff + print, write nothing (no probe_state save).
    """
    state_dir = Path(state_dir) if state_dir else (Path(__file__).resolve().parent / "state")
    probe_state_path = state_dir / "probe_state.json"

    def fetch_all():
        probe_data = probe_state.load_probe_state(probe_state_path)
        now_val = now if now is not None else time.time()
        results, metas = {}, {}
        first_probe = True  # first probe of the tick fires immediately
        probe_phase_start = time.time()
        for name, config in PROVIDERS.items():
            try:
                ids, meta = providers.fetch_provider(config)
                if config.get("detection") == "zero-credit-probe" and ids:
                    ids = list(ids)
                    # Prune vanished models from state.
                    # Guard: empty catalog is anomalous — don't prune on a
                    # suspicious fetch (could drop legit entries on a transient
                    # provider outage). Only prune when we got a real catalog.
                    probe_data = probe_state.drop_missing(probe_data, name, ids)
                    # Detect a junk provider slice (non-dict value at key)
                    # BEFORE the probe loop self-heals it. A junk slice
                    # degrades the roster to [] — we can't trust the baseline.
                    # Note: key-absent (None) is normal first-tick, NOT junk.
                    provider_is_junk = (name in probe_data
                                        and not isinstance(probe_data[name],
                                                           dict))
                    # Select this tick's probe queue
                    queue = probe_select.select_queue(
                        ids, probe_data.get(name, {}), int(now_val))
                    # Serial throttled probe loop
                    for model_id in queue:
                        # Probe-phase budget cap: check elapsed time before
                        # each probe (including the first). If the NEXT probe
                        # would exceed the budget, stop probing — remaining
                        # items stay unprobed (self-healing next tick).
                        elapsed = time.time() - probe_phase_start
                        if elapsed >= PROBE_PHASE_BUDGET_S:
                            remaining = len(queue) - queue.index(model_id)
                            print(
                                f"inference-watchdog: probe-phase budget "
                                f"exhausted — skipping {remaining} remaining "
                                f"model(s) for {name}",
                                file=sys.stderr,
                            )
                            break
                        if not first_probe:
                            sleep(PROBE_INTERVAL_S)
                        first_probe = False
                        try:
                            result, _ = probe_model(
                                config["base_url"],
                                config.get("_token", ""),
                                model_id,
                                timeout=PROBE_TIMEOUT_S,
                            )
                        except Exception:
                            result = None  # treated as DEFER
                        if result == Result.FREE:
                            entry = {"verdict": "free", "epoch": int(now_val)}
                        elif result == Result.PAID:
                            entry = {"verdict": "paid", "epoch": int(now_val)}
                        else:
                            # DEFER or exception -> entry untouched
                            entry = None
                        if entry is not None:
                            if not isinstance(probe_data.get(name), dict):
                                probe_data[name] = {}
                            probe_data[name][model_id] = entry
                    # Roster = only FREE-verdict models
                    # Use get_verdict for junk-safe reads (mirrors 2672037).
                    # If the provider slice was junk at tick start, degrade
                    # the roster to [] — we have no trustworthy baseline.
                    if provider_is_junk:
                        ids = []
                    else:
                        ids = sorted(
                            m for m in ids
                            if probe_state.get_verdict(probe_data, name, m)[0] == "free"
                        )
                results[name] = ids
                metas[name] = meta or {}
            except providers.FetchError:
                results[name] = None
        # Persist verdicts + prunes atomically once per tick — but ONLY if
        # something actually changed (skip redundant writes). Compare the
        # in-memory final state to what was loaded.
        if not dry_run:
            loaded = probe_state.load_probe_state(probe_state_path)
            if probe_data != loaded:
                probe_state.save_probe_state(probe_state_path, probe_data)
        return results, metas

    return fetch_all


def build_fetch_one(env, state_dir=None):
    """Return fetch_one(name) -> (ids, meta_dict). Raises on failure.

    For zero-credit-probe providers, returns the VERDICT-FILTERED list from
    persisted probe state (no re-probing). The recheck must not re-fire probes.
    """
    state_dir = Path(state_dir) if state_dir else (Path(__file__).resolve().parent / "state")
    probe_state_path = state_dir / "probe_state.json"

    def fetch_one(name):
        config = PROVIDERS[name]
        ids, meta = providers.fetch_provider(config)
        if config.get("detection") == "zero-credit-probe":
            # Recheck path: derive from persisted verdicts, do NOT re-probe
            probe_data = probe_state.load_probe_state(probe_state_path)
            # Use get_verdict for junk-safe reads (mirrors 2672037).
            ids = sorted(
                m for m in ids
                if probe_state.get_verdict(probe_data, name, m)[0] == "free"
            )
        return ids, meta or {}

    return fetch_one


# ---------- the tick ----------

def run_tick(state_dir, registry, fetch_all, fetch_one, webhook_url,
             sleep=time.sleep, now=None, recheck_delay=180,
             cadence_s=DEFAULT_CADENCE_S, dry_run=False,
             init=False):
    """Execute one monitor tick. Returns process exit code (0/1/2)."""
    now = now if now is not None else time.time()
    state_dir = Path(state_dir)
    paths = {
        "roster": state_dir / "roster.json",
        "pending": state_dir / "pending_alerts.json",
        "alive": state_dir / "alive.json",
        "lock": state_dir / "monitor.lock",
    }

    # F6-1: acquire_lock runs INSIDE the fatal handler — an OSError creating
    # or breaking the lockfile (EACCES permission drift, EROFS read-only
    # remount, ENOSPC) must map to the FATAL exit-2 path the cron wrapper
    # pages on, never escape as CPython exit 1 (which the wrapper treats as
    # silent routine outage: monitor dead forever, zero pages).
    # F7-1: ownership gate — release ONLY a lock this process acquired.
    # A contended tick (acquire returned False, exit 0 "already running")
    # must never unlink the LIVE lock owned by the other process.
    acquired = False
    try:
        if not state.acquire_lock(paths["lock"]):
            print("inference-watchdog: already running", file=sys.stderr)
            return 0
        acquired = True
        return _tick_locked(paths, registry, fetch_all, fetch_one, webhook_url,
                            sleep, now, recheck_delay,
                            cadence_s, dry_run, init=init)
    except Exception as exc:  # fatal — cron captures stderr
        print(f"inference-watchdog: FATAL {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    finally:
        if acquired:
            state.release_lock(paths["lock"])


def _emit(message, webhook_url, pending_path, dry_run):
    """Delivery topology: stdout ALWAYS, webhook best-effort (never blocks)."""
    print(message)
    if dry_run:
        if webhook_url:
            print("[dry-run] would POST to webhook")
    elif webhook_url:
        notify.send_webhook(webhook_url, message, pending_path)


def _tick_locked(paths, registry, fetch_all, fetch_one, webhook_url, sleep,
                 now, recheck_delay, cadence_s, dry_run,
                 init=False):
    prev_roster = diffing.load_filtered_roster(paths["roster"], set(registry))
    prev_providers = (prev_roster or {}).get("providers") or {}

    results, metas = fetch_all()
    new_map, stale = diffing.apply_sticky(prev_providers, results)
    events, first_run = diffing.compute_events(prev_roster, new_map,
                                               registry=set(registry))
    if init:
        # F-R2-2: --init REBASELINES — it never diffs against the old roster
        # (F1 semantics: exactly "initialized, no diff", zero alerts). The
        # old baseline is archived only later, AFTER the bootstrap guard.
        events, first_run = {}, True

    # Passive x-ratelimit telemetry (R2-6): {} whenever nous did not succeed.
    nous_ratelimit = {}
    if results.get("nous") is not None:
        nous_ratelimit = (metas.get("nous") or {}).get("ratelimit") or {}

    # Per-tick field lifecycle: rebuilt EVERY tick, never appended (Task 4).
    transients, unconfirmed = {}, {}

    prev_alive = state.load_alive(paths["alive"])
    emitted_real = False   # ONLY diff alerts / 💚 ping count (R2-8)

    def persist_roster():
        if dry_run:
            return
        state.save_roster_atomic(paths["roster"], {
            "tick_epoch": int(now),
            "providers": new_map,
            "stale_providers": stale,          # rebuilt every tick
            "transients": transients,          # rebuilt every tick (R2-5)
            "unconfirmed": unconfirmed,        # rebuilt every tick (R2-5)
            "nous_ratelimit": nous_ratelimit,  # passive headers (R2-6)
        })

    if first_run:
        # Bootstrap guard (R2-12): zero providers succeeded -> refuse to write
        # an empty baseline (it would emit the universe as "added" next tick).
        if results and all(v is None for v in results.values()):
            failed = ", ".join(sorted(results))
            print(f"inference-watchdog: bootstrap refused — zero providers "
                  f"fetched successfully (failed: {failed})", file=sys.stderr)
            return 1
        print("initialized, no diff")
        if init and not dry_run and paths["roster"].exists():
            # F-R2-2: the F1 archive now happens HERE — only on an --init run
            # whose bootstrap guard has PASSED. A refused init (guard above)
            # returns before this line, so the old baseline is never moved
            # aside for a rebaseline that never happens. Overwriting a prior
            # .bak is accepted (documented in README).
            os.replace(paths["roster"],
                       paths["roster"].with_name(paths["roster"].name + ".bak"))
        persist_roster()
        if not dry_run:
            state.save_alive(paths["alive"], last_tick_epoch=int(now),
                             last_output_epoch=int(now),
                             dropped_alerts_total=0)
        # F7: a PARTIAL failure on first-run/init exits 1, aligned with the
        # normal-tick partial-failure code (stale non-empty ⇒ 1).
        return 1 if stale else 0

    if events:
        confirmation = diffing.confirm_diffs(
            candidates=events, prev_providers=prev_providers,
            fetch_one=fetch_one, sleep=sleep, delay=recheck_delay)
        confirmed = confirmation["confirmed"]
        transients = confirmation["transients"]
        unconfirmed = confirmation["unconfirmed"]
        # RECHECK OUTCOMES PERSIST (R2-2): the corrected id-map wins over the
        # pre-recheck snapshot — transient flap leaves NO trace, confirmed
        # refetch becomes the persisted truth, unconfirmed keeps sticky-old.
        new_map = diffing.merge_corrected(new_map, confirmation, prev_providers)

        survivors = confirmed
    else:
        survivors = {}

    # Crash-safe write order (R2-9): roster FIRST, then alert enqueue/send
    # (pending_alerts.json inside notify). A crash may delay a retry but
    # never silently swallows an alert.
    persist_roster()

    # Drain the retry queue before handling new alerts (plan mandate).
    if webhook_url and not dry_run:
        notify.drain_pending(webhook_url, paths["pending"])

    if any(s["added"] or s["removed"] for s in survivors.values()):
        tick_iso = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
        msg = notify.format_alert(
            survivors, tick_iso=tick_iso, providers_polled=len(registry),
            transients=transients, stale=stale,
            dropped_total=notify.get_dropped_total())
        _emit(msg, webhook_url, paths["pending"], dry_run)
        emitted_real = True

    # --- alive self-watch (two clocks; critic round-3 R2-8) ---
    # The ⚠️ missed-tick warning is NOT a real emission: it must NOT suppress
    # the 💚 ping and must NOT refresh last_output_epoch.
    if alive.missed_ticks(prev_alive.get("last_tick_epoch"), cadence_s, now):
        _emit("⚠️ inference-watchdog: missed ticks detected "
              f"(last tick {int((now - prev_alive.get('last_tick_epoch', now))//3600)}h ago)",
              webhook_url, paths["pending"], dry_run)

    ping_due = alive.should_ping(prev_alive.get("last_output_epoch"), now)
    if ping_due and not emitted_real:
        # F6: report prev total + THIS tick's drops so a drop is visible on
        # the very next ping instead of lagging a full cadence.
        dropped_now = (prev_alive.get("dropped_alerts_total", 0)
                       + notify.get_dropped_total())
        _emit(alive.format_alive(len(registry), stale, transients,
                                 dropped_now),
              webhook_url, paths["pending"], dry_run)
        emitted_real = True

    if not dry_run:
        state.save_alive(
            paths["alive"],
            last_tick_epoch=int(now),
            last_output_epoch=int(now) if emitted_real
            else prev_alive.get("last_output_epoch", int(now)),
            dropped_alerts_total=prev_alive.get("dropped_alerts_total", 0)
            + notify.get_dropped_total())

    return 1 if stale else 0


# ---------- CLI ----------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Free Inference Watchdog tick")
    parser.add_argument("--init", action="store_true",
                        help="bootstrap roster, no diffing, no alerts")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + diff + print, write nothing, POST nothing")
    parser.add_argument("--recheck-delay", type=int, default=180,
                        help="seconds before confirm re-fetch (0 in tests)")
    parser.add_argument("--cadence-hours", type=int, default=1,
                        help="tick cadence in hours — drives missed-tick "
                             "warning; keep in step with the cron schedule")
    parser.add_argument("--state-dir", default=None,
                        help="default: <this project>/state")
    args = parser.parse_args(argv)
    if args.init and args.dry_run:
        parser.error("--init and --dry-run are mutually exclusive "
                     "(--init writes a fresh baseline; --dry-run writes nothing)")

    state_dir = Path(args.state_dir) if args.state_dir else (
        Path(__file__).resolve().parent / "state")
    env = parse_envfile()
    webhook = env.get("DISCORD_WEBHOOK_INFERENCE_WATCHDOG") or None
    fetch_all = build_fetch_all(env, state_dir=state_dir, now=time.time(),
                                 sleep=time.sleep, dry_run=args.dry_run)
    fetch_one = build_fetch_one(env, state_dir=state_dir)

    if args.init:
        return run_tick(state_dir, PROVIDERS, fetch_all, fetch_one,
                        webhook_url=None, sleep=lambda s: None, now=time.time(),
                        recheck_delay=0, dry_run=False, init=True)
    return run_tick(state_dir, PROVIDERS, fetch_all, fetch_one,
                    webhook_url=webhook, sleep=time.sleep, now=time.time(),
                    recheck_delay=args.recheck_delay,
                    cadence_s=args.cadence_hours * 3600, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())