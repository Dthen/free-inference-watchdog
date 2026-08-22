#!/usr/bin/env python3
"""Free Inference Monitor — one tick per invocation. Stdlib only, zero tokens.

Hermes cron (no_agent mode): non-empty stdout is delivered verbatim to the
Discord home channel; empty stdout = silent. Every user-visible message also
goes to $DISCORD_WEBHOOK_INFERENCE_MONITOR (kennel channel) when configured.
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import alive
import cooldown
import diffing
import notify
import providers
import state
from envfile import parse_envfile

DEFAULT_CADENCE_S = 6 * 3600
HERMES_ENV = Path("~/.hermes/.env").expanduser()


# ---------- provider plumbing ----------

def build_fetch_all(env):
    """Return fetch_all() -> ({name: ids|None}, {name: meta_dict}).

    Failures become None in the results map (sticky) and are ABSENT from the
    meta map. Meta is passive telemetry only (nous x-ratelimit headers, R2-6).
    """
    keys = {
        "zen": env.get("OPENCODE_ZEN_API_KEY"),
        "kilo": env.get("KILOCODE_API_KEY"),
        "ollama": env.get("OLLAMA_API_KEY"),
    }

    def fetch_all():
        results, metas = {}, {}
        for name, fetcher in providers.PROVIDERS.items():
            try:
                if name == "nous":
                    ids, meta = fetcher(auth=providers._load_nous_auth())
                elif name in keys:
                    ids, meta = fetcher(key=keys[name])
                else:
                    ids, meta = fetcher()
                results[name] = ids
                metas[name] = meta or {}
            except providers.FetchError:
                results[name] = None
        return results, metas

    return fetch_all


def build_fetch_one(env):
    """Return fetch_one(name) -> (ids, meta_dict). Raises on failure."""

    keys = {
        "zen": env.get("OPENCODE_ZEN_API_KEY"),
        "kilo": env.get("KILOCODE_API_KEY"),
        "ollama": env.get("OLLAMA_API_KEY"),
    }

    def fetch_one(name):
        fetcher = providers.PROVIDERS[name]
        if name == "nous":
            ids, meta = fetcher(auth=providers._load_nous_auth())
        elif name in keys:
            ids, meta = fetcher(key=keys[name])
        else:
            ids, meta = fetcher()
        return ids, meta or {}

    return fetch_one


# ---------- the tick ----------

def run_tick(state_dir, registry, fetch_all, fetch_one, webhook_url,
             sleep=time.sleep, now=None, recheck_delay=180,
             cooldown_hours=12, cadence_s=DEFAULT_CADENCE_S, dry_run=False,
             init=False):
    """Execute one monitor tick. Returns process exit code (0/1/2)."""
    now = now if now is not None else time.time()
    state_dir = Path(state_dir)
    paths = {
        "roster": state_dir / "roster.json",
        "cooldowns": state_dir / "cooldowns.json",
        "pending": state_dir / "pending_alerts.json",
        "alive": state_dir / "alive.json",
        "lock": state_dir / "monitor.lock",
    }

    if not state.acquire_lock(paths["lock"]):
        print("inference-monitor: already running", file=sys.stderr)
        return 0
    try:
        if init and paths["roster"].exists():
            # F1: archive FIRST — the rename removes the original, so the
            # first_run path below engages naturally (no diff, no alerts,
            # no cooldown writes). Webhook stays suppressed by the caller.
            os.replace(paths["roster"],
                       paths["roster"].with_name(paths["roster"].name + ".bak"))
        return _tick_locked(paths, registry, fetch_all, fetch_one, webhook_url,
                            sleep, now, recheck_delay, cooldown_hours,
                            cadence_s, dry_run)
    except Exception as exc:  # fatal — cron captures stderr
        print(f"inference-monitor: FATAL {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    finally:
        state.release_lock(paths["lock"])


def _emit(message, webhook_url, pending_path, dry_run):
    """Delivery topology: stdout ALWAYS, webhook best-effort (never blocks)."""
    print(message)
    if dry_run:
        print("[dry-run] would POST to webhook")
    elif webhook_url:
        notify.send_webhook(webhook_url, message, pending_path)


def _tick_locked(paths, registry, fetch_all, fetch_one, webhook_url, sleep,
                 now, recheck_delay, cooldown_hours, cadence_s, dry_run):
    prev_roster = diffing.load_filtered_roster(paths["roster"], set(registry))
    prev_providers = (prev_roster or {}).get("providers") or {}

    results, metas = fetch_all()
    new_map, stale = diffing.apply_sticky(prev_providers, results)
    events, first_run = diffing.compute_events(prev_roster, new_map,
                                               registry=set(registry))

    # Passive x-ratelimit telemetry (R2-6): {} whenever nous did not succeed.
    nous_ratelimit = {}
    if results.get("nous") is not None:
        nous_ratelimit = (metas.get("nous") or {}).get("ratelimit") or {}

    # Per-tick field lifecycle: rebuilt EVERY tick, never appended (Task 4).
    transients, unconfirmed = {}, {}

    # --cooldown-hours must be REAL (R2-10): drives both dedup and pruning.
    ttl_s = int(cooldown_hours * 3600)

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
            print(f"inference-monitor: bootstrap refused — zero providers "
                  f"fetched successfully (failed: {failed})", file=sys.stderr)
            return 1
        print("initialized, no diff")
        persist_roster()
        if not dry_run:
            state.save_alive(paths["alive"], last_tick_epoch=int(now),
                             last_output_epoch=int(now),
                             dropped_alerts_total=0)
        return 0

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

        survivors, new_cd = cooldown.filter_cooldown(
            confirmed, state.load_cooldowns(paths["cooldowns"]), now,
            ttl_s=ttl_s)
    else:
        survivors, new_cd = {}, {}

    # Crash-safe write order (R2-9): roster FIRST, then alert enqueue/send
    # (pending_alerts.json inside notify), THEN cooldowns.json last. A crash
    # may lose a cooldown but never silently swallow an alert.
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
        if not dry_run:
            state.save_cooldowns(paths["cooldowns"], new_cd, ttl_s=ttl_s,
                                 now=now)

    # --- alive self-watch (two clocks; critic round-3 R2-8) ---
    # The ⚠️ missed-tick warning is NOT a real emission: it must NOT suppress
    # the 💚 ping and must NOT refresh last_output_epoch.
    if alive.missed_ticks(prev_alive.get("last_tick_epoch"), cadence_s, now):
        _emit("⚠️ inference-monitor: missed ticks detected "
              f"(last tick {int((now - prev_alive.get('last_tick_epoch', now))//3600)}h ago)",
              webhook_url, paths["pending"], dry_run)

    ping_due = alive.should_ping(prev_alive.get("last_tick_epoch", now),
                                 prev_alive.get("last_output_epoch"), now)
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
    parser = argparse.ArgumentParser(description="Free Inference Monitor tick")
    parser.add_argument("--init", action="store_true",
                        help="bootstrap roster, no diffing, no alerts")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + diff + print, write nothing, POST nothing")
    parser.add_argument("--recheck-delay", type=int, default=180,
                        help="seconds before confirm re-fetch (0 in tests)")
    parser.add_argument("--cadence-hours", type=int, default=6,
                        help="tick cadence in hours — drives missed-tick "
                             "warning; keep in step with the cron schedule")
    parser.add_argument("--cooldown-hours", type=int, default=12)
    parser.add_argument("--state-dir", default=None,
                        help="default: <this project>/state")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else (
        Path(__file__).resolve().parent / "state")
    env = parse_envfile(HERMES_ENV)
    webhook = env.get("DISCORD_WEBHOOK_INFERENCE_MONITOR") or None
    fetch_all = build_fetch_all(env)
    fetch_one = build_fetch_one(env)

    if args.init:
        return run_tick(state_dir, providers.PROVIDERS, fetch_all, fetch_one,
                        webhook_url=None, sleep=lambda s: None, now=time.time(),
                        recheck_delay=0, dry_run=False, init=True)
    return run_tick(state_dir, providers.PROVIDERS, fetch_all, fetch_one,
                    webhook_url=webhook, sleep=time.sleep, now=time.time(),
                    recheck_delay=args.recheck_delay,
                    cooldown_hours=args.cooldown_hours,
                    cadence_s=args.cadence_hours * 3600, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
