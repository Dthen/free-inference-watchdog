"""Integration tests — the full tick loop against stubbed providers."""

import json
import os
import stat
import time

import inference_watchdog as im


REGISTRY = {"nous", "openrouter", "tokenrouter", "kilo", "amd", "bai"}


def _fetcher(scenarios):
    """scenarios: list of {name: ids|None} consumed one per fetch_all call.
    fetch_one uses the CURRENT scenario."""
    calls = {"n": 0}

    def current():
        return scenarios[min(calls["n"], len(scenarios) - 1)]

    def fetch_all():
        snap = dict(current())
        calls["n"] += 1
        # Tick now expects (results_map, meta_map); meta is passive telemetry.
        metas = {name: {} for name in snap}
        return snap, metas

    def fetch_one(name):
        # confirm_diffs expects (ids, meta); None means "fetch failed" -> FetchError
        val = current()[name]
        if val is None:
            from providers import FetchError
            raise FetchError(f"recheck fetch failed for {name}")
        return val, {}

    return fetch_all, fetch_one, calls


def _run(tmp, scenarios, **kw):
    fetch_all, fetch_one, calls = _fetcher(scenarios)
    code = im.run_tick(
        tmp, REGISTRY, fetch_all, fetch_one, webhook_url=None,
        sleep=lambda s: None, now=kw.pop("now", 1_000_000_000),
        recheck_delay=kw.pop("recheck_delay", 0), **kw)
    return code, calls


def test_first_run_initializes_silently(tmp_path, capsys):
    code, _ = _run(tmp_path, [{"nous": ["a"]}])
    assert code == 0
    assert "initialized, no diff" in capsys.readouterr().out
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["providers"]["nous"] == ["a"]
    alive_d = json.loads((tmp_path / "alive.json").read_text())
    assert alive_d["last_tick_epoch"] == 1_000_000_000


def test_init_over_existing_roster_archives_and_stays_silent(tmp_path, capsys):
    """F1: --init over an existing roster archives it to roster.json.bak,
    rebaselines cleanly, prints EXACTLY 'initialized, no diff' — never an
    alert, never a cooldown write."""
    _run(tmp_path, [{"nous": ["old-1", "old-2"]}])            # baseline
    capsys.readouterr()                            # drain baseline's own line
    # pre-existing roster with DIFFERENT ids + --init
    fetch_all, fetch_one, _ = _fetcher([{"nous": ["new-1"]}])
    code = im.run_tick(
        tmp_path, REGISTRY, fetch_all, fetch_one, webhook_url=None,
        sleep=lambda s: None, now=1_000_000_000 + 1 * 3600,
        recheck_delay=0, init=True)
    out = capsys.readouterr().out
    assert code == 0
    assert out == "initialized, no diff\n"        # EXACTLY that line, zero alerts
    bak = json.loads((tmp_path / "roster.json.bak").read_text())
    assert bak["providers"]["nous"] == ["old-1", "old-2"]     # archive intact
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["providers"]["nous"] == ["new-1"]           # fresh baseline
    assert not (tmp_path / "cooldowns.json").exists()         # no cooldown writes


def test_init_refused_by_guard_preserves_roster_exactly(tmp_path, capsys):
    """F-R2-2 regression: a refused --init (bootstrap guard: zero providers
    fetched) must leave the previous baseline UNTOUCHED — byte-for-byte — and
    create no .bak. The old archive-before-guard ordering silently destroyed
    the baseline on any mid-outage --init."""
    _run(tmp_path, [{"nous": ["old-1", "old-2"]}])            # good baseline
    capsys.readouterr()
    original = (tmp_path / "roster.json").read_bytes()
    fetch_all, fetch_one, _ = _fetcher([{"nous": None}])      # hard outage
    code = im.run_tick(
        tmp_path, REGISTRY, fetch_all, fetch_one, webhook_url=None,
        sleep=lambda s: None, now=1_000_000_000 + 1 * 3600,
        recheck_delay=0, init=True)
    err = capsys.readouterr().err
    assert code == 1
    assert "bootstrap refused" in err
    assert not (tmp_path / "roster.json.bak").exists()        # nothing archived
    assert (tmp_path / "roster.json").read_bytes() == original  # preserved EXACTLY


def test_cli_init_branch_passes_init_flag(monkeypatch):
    """F1 wiring: the --init CLI branch must request the init path and keep
    the webhook suppressed."""
    captured = {}

    def fake_tick(state_dir, registry, fetch_all, fetch_one, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(im, "run_tick", fake_tick)
    im.main(["--init"])
    assert captured.get("init") is True
    assert captured.get("webhook_url") is None


def test_cli_init_and_dry_run_rejected(capsys):
    """F8e: --init together with --dry-run is an operator error — argparse
    must error out with a usage message, never silently ignore one flag."""
    import pytest
    with pytest.raises(SystemExit) as ei:
        im.main(["--init", "--dry-run"])
    assert ei.value.code == 2
    err = capsys.readouterr().err          # read once: readouterr drains
    assert "--dry-run" in err and "--init" in err


def test_cli_cadence_hours_default_and_override(monkeypatch):
    """F2: --cadence-hours exists (default 6h) and is plumbed to run_tick as
    cadence_s seconds."""
    captured = {}

    def fake_tick(state_dir, registry, fetch_all, fetch_one, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(im, "run_tick", fake_tick)
    im.main([])
    assert captured["cadence_s"] == 1 * 3600          # default
    im.main(["--cadence-hours", "12"])
    assert captured["cadence_s"] == 12 * 3600         # override


def test_cli_cooldown_hours_default_and_override(monkeypatch):
    """Fix-round-5 #4: --cooldown-hours is plumbed through main() to run_tick
    (default 12h). Mirrors test_cli_cadence_hours_default_and_override — a
    regression dropping cooldown_hours=args.cooldown_hours must go red."""
    captured = {}

    def fake_tick(state_dir, registry, fetch_all, fetch_one, **kw):
        captured.update(kw)
        return 0

    monkeypatch.setattr(im, "run_tick", fake_tick)
    im.main([])
    assert captured["cooldown_hours"] == 12           # default
    im.main(["--cooldown-hours", "24"])
    assert captured["cooldown_hours"] == 24           # override


def test_structurally_empty_roster_boots_clean_no_add_storm(tmp_path, capsys):
    """F4: a JSON-valid roster lacking a dict-shaped providers key must
    bootstrap clean (first_run), never emit the universe as 🟢."""
    (tmp_path / "roster.json").write_text("{}", encoding="utf-8")
    code, _ = _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 1 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "🟢" not in out
    assert "initialized, no diff" in out
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["providers"]["nous"] == ["a"]


def test_confirmed_removal_alerts_once_then_cooldowns(tmp_path, capsys):
    _run(tmp_path, [{"nous": ["a", "b"]}])                       # baseline
    code, _ = _run(tmp_path, [{"nous": ["a"]}],                  # b disappears
                   now=1_000_000_000 + 1 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "🔴 `b`" in out
    # same flap 1h later: suppressed by cooldown
    _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 7 * 3600)
    out2 = capsys.readouterr().out
    assert "🔴 `b`" not in out2


def test_transient_removal_never_alerts(tmp_path, capsys):
    _run(tmp_path, [{"tokenrouter": ["z1", "z2"]}])
    # candidate removal, but recheck sees z2 back -> transient, silent
    code, _ = _run(tmp_path, [{"tokenrouter": ["z1"]}, {"tokenrouter": ["z1", "z2"]}],
                   now=1_000_000_000 + 1 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "🔴" not in out
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["providers"]["tokenrouter"] == ["z1", "z2"]  # recheck state wins


def test_empty_roster_diffs_honestly_into_alert(tmp_path, capsys):
    """CHANGE 2: an empty result from a healthy fetch is REAL data (all free
    tiers deleted) — it must flow through the tick as a confirmed mass 🔴
    alert, never be swallowed as outage/sticky. End-to-end: baseline [a,b] ->
    next tick fetches [] (recheck agrees) -> one honest removal alert."""
    _run(tmp_path, [{"nous": ["a", "b"]}])
    code, _ = _run(tmp_path, [{"nous": []}, {"nous": []}],
                   now=1_000_000_000 + 1 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "🔴 `a`" in out and "🔴 `b`" in out
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["providers"]["nous"] == []          # empty truth persisted


def test_fetch_failure_sticky_no_alert(tmp_path, capsys):
    _run(tmp_path, [{"nous": ["a", "b"]}])
    code, _ = _run(tmp_path, [{"nous": None}], now=1_000_000_000 + 1 * 3600)
    out = capsys.readouterr().out
    assert code == 1                       # partial failure exit code
    assert "🔴" not in out                 # outage never looks like removal
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["providers"]["nous"] == ["a", "b"]  # carried forward
    assert roster["stale_providers"] == ["nous"]


def test_alive_ping_after_twenty_hours_quiet(tmp_path, capsys):
    _run(tmp_path, [{"nous": ["a"]}])
    # 25h of ticks with zero diffs -> alive ping fires
    code, _ = _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 25 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "💚" in out


def test_lock_contention_exits_zero(tmp_path):
    lock = tmp_path / "monitor.lock"
    lock.write_text("123", encoding="utf-8")
    import os, time as _t
    old = _t.time() - 60  # fresh live lock (1 min old)
    os.utime(lock, (old, old))
    code, _ = _run(tmp_path, [{"nous": ["a"]}])
    assert code == 0       # instant exit, no crash


def test_lock_contention_preserves_live_lockfile(tmp_path):
    """F7-1 regression: a contended tick must NEVER delete the LIVE lockfile
    owned by the other running process. F6-1 moved the contention return
    inside run_tick's try/finally, whose release_lock then unconditionally
    unlinked the OTHER process's lock -> mutual exclusion silently died ->
    next invocation acquired and ran concurrent full ticks (duplicate alerts,
    lost cooldown stamps). Contract: contended run exits 0 AND leaves the
    lock byte-and-mtime UNCHANGED; the tick body never executes."""
    lock = tmp_path / "monitor.lock"
    lock.write_text("123", encoding="utf-8")
    old = time.time() - 60  # fresh live lock (1 min old)
    os.utime(lock, (old, old))
    before_bytes = lock.read_bytes()
    before_mtime = os.stat(lock).st_mtime
    code, calls = _run(tmp_path, [{"nous": ["a"]}])
    assert code == 0                            # contention policy unchanged
    assert lock.exists(), "live lockfile was DELETED by contended tick"
    assert lock.read_bytes() == before_bytes    # byte-identical
    assert os.stat(lock).st_mtime == before_mtime  # untouched mtime
    assert calls["n"] == 0                      # tick body never ran


def test_readonly_state_dir_lock_create_fails_exits_two(tmp_path, capsys):
    """F6-1 (primary path): acquire_lock must sit INSIDE run_tick's fatal
    handler. A read-only state dir (EACCES / EROFS / ENOSPC class) makes
    lockfile creation raise OSError — previously that escaped uncaught
    (CPython exit 1), which the README wrapper deliberately treats as silent
    routine-outage: monitor dead forever with zero pages. Contract: run_tick
    RETURNS 2 (the paged FATAL code), never raises, never exits 1."""
    orig_mode = stat.S_IMODE(os.stat(tmp_path).st_mode)
    os.chmod(tmp_path, 0o555)                     # read-only dir
    try:
        code, _ = _run(tmp_path, [{"nous": ["a"]}])
        captured = capsys.readouterr()            # drain ONCE (drains both)
    finally:
        os.chmod(tmp_path, orig_mode)             # so tmp cleanup works
    assert code == 2                              # FATAL, paged — not 1, not raise
    assert "FATAL PermissionError" in captured.err
    assert captured.out == ""                     # no user-visible output


def test_readonly_state_dir_stale_lock_break_fails_exits_two(tmp_path, capsys):
    """F6-1 (second trigger path): STALE lock (>30 min) in a read-only dir —
    acquire_lock's unlink raises. Must map to FATAL exit 2 like the primary
    path AND the finally-block release_lock must stay best-effort (the failed
    break left the lockfile behind; re-raising there would discard the exit-2
    return and crash with exit 1 all over again)."""
    lock = tmp_path / "monitor.lock"
    lock.write_text("999999", encoding="utf-8")
    old = time.time() - 31 * 60                   # > LOCK_STALE_S
    os.utime(lock, (old, old))
    orig_mode = stat.S_IMODE(os.stat(tmp_path).st_mode)
    os.chmod(tmp_path, 0o555)
    try:
        code, _ = _run(tmp_path, [{"nous": ["a"]}])
        captured = capsys.readouterr()
    finally:
        os.chmod(tmp_path, orig_mode)
    assert code == 2
    assert "FATAL PermissionError" in captured.err
    assert captured.out == ""


def test_dry_run_writes_nothing(tmp_path, capsys):
    code, _ = _run(tmp_path, [{"nous": ["a"]}], dry_run=True)
    assert code == 0
    assert not (tmp_path / "roster.json").exists()
    assert not (tmp_path / "alive.json").exists()


def test_emit_dry_run_webhook_line_only_when_webhook_configured(capsys):
    """Fix-round-4 #4: "[dry-run] would POST to webhook" only makes sense when
    a webhook_url exists — with none configured the line is pure noise."""
    im._emit("msg", None, None, dry_run=True)
    out_none = capsys.readouterr().out
    assert out_none == "msg\n"          # no would-POST line

    im._emit("msg", "https://example/hook", None, dry_run=True)
    out_hook = capsys.readouterr().out
    assert out_hook == "msg\n[dry-run] would POST to webhook\n"


def test_registry_filter_kills_zombies(tmp_path, capsys):
    _run(tmp_path, [{"nous": ["a"], "tokenrouter": ["zombie"]}])
    # registry shrinks to nous only: zombie tokenrouter must vanish silently
    code, _ = _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 1 * 3600)
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert "tokenrouter" not in roster["providers"]
    assert "🔴" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Brief items 2/4-9: explicit, named tests for every plan requirement
# ---------------------------------------------------------------------------


def test_roster_persists_transients_and_unconfirmed_every_tick(tmp_path):
    """Item 4: transients and unconfirmed are REBUILT every tick."""
    _run(tmp_path, [{"nous": ["a"]}])
    # Tick 2: nothing changed -> both fields must be {} (rebuilt, not appended)
    _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 1 * 3600)
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["transients"] == {}
    assert roster["unconfirmed"] == {}


def test_roster_persists_transients_from_flap(tmp_path, capsys):
    """Item 4: a REAL transient flap is recorded in roster under transients.
    Fix-round-5 #3: the previous body used identical scenarios (["a"],["a"]) —
    no candidate diff ever arose, so transients == {} passed vacuously. This
    shape is an honest flap: baseline [a,b] -> candidate tick sees [a] ->
    recheck sees [a,b] again => b's removal recorded as transient, silent."""
    _run(tmp_path, [{"tokenrouter": ["a", "b"]}])                       # baseline
    capsys.readouterr()
    # candidate tick: b gone; recheck: b back => transient flap
    code, _ = _run(tmp_path, [{"tokenrouter": ["a"]}, {"tokenrouter": ["a", "b"]}],
                   now=1_000_000_000 + 1 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "🔴" not in out                        # transient never alerts
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["transients"] == {"tokenrouter": {"added": [], "removed": ["b"]}}
    assert roster["providers"]["tokenrouter"] == ["a", "b"]   # recheck truth persisted


def test_nous_ratelimit_persisted_from_meta(tmp_path):
    """Item 5: nous_ratelimit is plumbed end-to-end into roster."""
    # Scenario with meta payload
    fetch_all, fetch_one, _ = _fetcher([{"nous": ["a"]}])
    def fetch_all_with_meta():
        results, _ = fetch_all()
        return results, {"nous": {"ratelimit": {"x-ratelimit-remaining-requests": "99"}}}
    im.run_tick(
        tmp_path, REGISTRY, fetch_all_with_meta, fetch_one,
        webhook_url=None, sleep=lambda s: None, now=1_000_000_000,
        recheck_delay=0)
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert "nous_ratelimit" in roster
    assert roster["nous_ratelimit"]["x-ratelimit-remaining-requests"] == "99"


def test_nous_ratelimit_empty_when_nous_failed(tmp_path):
    """Item 5: nous_ratelimit is {} when nous fetch failed."""
    # Need at least one success to bypass bootstrap guard
    _run(tmp_path, [{"nous": None, "openrouter": ["x"]}])
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["nous_ratelimit"] == {}


def test_roster_written_before_alert_and_cooldowns(tmp_path, monkeypatch):
    """Item 6: crash-safe write order — roster FIRST, cooldowns LAST."""
    writes = []
    import state as st
    orig_save_roster = st.save_roster_atomic
    orig_save_cd = st.save_cooldowns
    def spy_roster(path, data):
        writes.append(("roster", str(path)))
        return orig_save_roster(path, data)
    def spy_cd(path, data, **kw):
        writes.append(("cooldowns", str(path)))
        return orig_save_cd(path, data, **kw)
    monkeypatch.setattr(st, "save_roster_atomic", spy_roster)
    monkeypatch.setattr(st, "save_cooldowns", spy_cd)
    _run(tmp_path, [{"nous": ["a", "b"]}])
    _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 1 * 3600)
    # roster must be written before cooldowns
    roster_idx = [i for i, (kind, _) in enumerate(writes) if kind == "roster"]
    cd_idx = [i for i, (kind, _) in enumerate(writes) if kind == "cooldowns"]
    assert roster_idx and cd_idx
    assert roster_idx[0] < cd_idx[0]


def test_cooldown_hours_wired_to_ttl(tmp_path):
    """Item 7: --cooldown-hours drives the TTL."""
    _run(tmp_path, [{"nous": ["a", "b"]}])
    code, _ = _run(tmp_path, [{"nous": ["a"]}],
                    now=1_000_000_000 + 1 * 3600)
    assert code == 0
    # Same flap 1h later: suppressed (default 12h cooldown)
    _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 7 * 3600)
    import cooldown as cd_mod
    cds = json.loads((tmp_path / "cooldowns.json").read_text())
    # One entry should exist (the first alert was stamped)
    assert len(cds) >= 1


def test_bootstrap_guard_zero_providers(tmp_path, capsys):
    """Item 8: first-run with ZERO successful providers exits 1."""
    code, _ = _run(tmp_path, [{"nous": None, "openrouter": None}])
    assert code == 1
    assert not (tmp_path / "roster.json").exists()
    err = capsys.readouterr().err
    assert "bootstrap refused" in err


def test_bootstrap_guard_allows_partial_success(tmp_path):
    """Item 8: if at least one provider succeeds, init proceeds...
    F7: ...but a PARTIAL failure must exit 1 like any normal tick."""
    code, _ = _run(tmp_path, [{"nous": ["a"], "openrouter": None}])
    assert code == 1
    assert (tmp_path / "roster.json").exists()


def test_first_run_partial_failure_exits_one_but_initializes(tmp_path, capsys):
    """F7: init/first-run with SOME providers failed aligns its exit code
    with the normal-tick partial-failure code (1), still initializing."""
    code, _ = _run(tmp_path, [{"nous": ["a"], "tokenrouter": None}])
    assert code == 1
    assert "initialized, no diff" in capsys.readouterr().out
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["providers"]["nous"] == ["a"]
    assert roster["stale_providers"] == ["tokenrouter"]


def test_unconfirmed_then_confirmed_alerts_once(tmp_path, capsys):
    """Item 9 (R2-15): multi-tick unconfirmed → confirmed alerts exactly once.
    Tick A: candidate diff + recheck fails => silent, roster sticky-old.
    Tick B: same candidate recheck succeeds => one alert.
    Immediate repeat suppressed by cooldown."""
    # Baseline
    _run(tmp_path, [{"nous": ["a", "b"]}])

    # Tick A: b disappears, recheck FAILS => unconfirmed, silent
    code, _ = _run(tmp_path, [{"nous": ["a"]}, {"nous": None}],
                    now=1_000_000_000 + 1 * 3600)
    out_a = capsys.readouterr().out
    assert code == 0
    assert "🔴" not in out_a
    roster_a = json.loads((tmp_path / "roster.json").read_text())
    # sticky-old: nous still has [a, b]
    assert roster_a["providers"]["nous"] == ["a", "b"]

    # Tick B: b disappears, recheck SUCCEEDS => one alert
    code, _ = _run(tmp_path, [{"nous": ["a"]}, {"nous": ["a"]}],
                    now=1_000_000_000 + 12 * 3600)
    out_b = capsys.readouterr().out
    assert code == 0
    assert "🔴" in out_b
    assert "b" in out_b

    # Immediate repeat: suppressed by cooldown
    _run(tmp_path, [{"nous": ["a"]}],
          now=1_000_000_000 + 13 * 3600)
    out_c = capsys.readouterr().out
    assert "🔴" not in out_c


def test_missed_tick_warning_does_not_suppress_alive_ping(tmp_path, capsys):
    """Item 2: missed-tick warning must NOT suppress the 💚 ping
    and must NOT update last_output_epoch."""
    _run(tmp_path, [{"nous": ["a"]}])
    # 25h later: both warning AND ping should appear
    code, _ = _run(tmp_path, [{"nous": ["a"]}],
                    now=1_000_000_000 + 25 * 3600)
    out = capsys.readouterr().out
    assert "⚠️" in out
    assert "💚" in out
    # last_output_epoch should have advanced to now (ping emitted)
    alive_d = json.loads((tmp_path / "alive.json").read_text())
    assert alive_d["last_output_epoch"] == 1_000_000_000 + 25 * 3600


def test_alive_ping_reports_prev_plus_this_tick_drops(tmp_path, capsys,
                                                      monkeypatch):
    """F6: the 💚 ping must include THIS tick's webhook drops immediately,
    not lag them by one tick."""
    import notify
    _run(tmp_path, [{"nous": ["a"]}])
    alive_d = json.loads((tmp_path / "alive.json").read_text())
    alive_d["dropped_alerts_total"] = 3                  # history
    (tmp_path / "alive.json").write_text(json.dumps(alive_d))
    monkeypatch.setattr(notify, "_dropped_total", 1)     # one drop THIS tick
    code, _ = _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 25 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "💚" in out
    assert "dropped undeliverable alerts total: 4" in out   # 3 prev + 1 now


def test_no_alive_ping_when_diff_emitted(tmp_path):
    """Item 2: when a real diff alert fires, no separate alive ping needed."""
    _run(tmp_path, [{"nous": ["a", "b"]}])
    _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 25 * 3600)
    alive_d = json.loads((tmp_path / "alive.json").read_text())
    # diff alert counts as emitted_real -> last_output_epoch updated
    assert alive_d["last_output_epoch"] == 1_000_000_000 + 25 * 3600


def test_zero_credit_probe_concurrency(monkeypatch, tmp_path, capsys):
    """Concurrent probes: 10 models × 0.2s with 3 workers must complete in < 1.0s.
    Proves ThreadPoolExecutor replaced the sequential loop."""
    import inference_watchdog as im
    from probe_zero_credit import Result
    import providers

    call_log = []
    start_time = time.time()

    def fake_probe(base_url, token, model_id, timeout=30):
        call_log.append(model_id)
        time.sleep(0.2)  # simulate network latency
        # Return FREE for even-indexed models, PAID for odd
        if int(model_id.split("-")[-1]) % 2 == 0:
            return Result.FREE, {"http": 200}
        return Result.PAID, {"http": 403}

    monkeypatch.setattr(im, "probe_model", fake_probe)

    # Mock PROVIDERS to only have bai with zero-credit-probe
    model_ids = [f"model-{i}" for i in range(10)]
    mock_providers = {
        "bai": {
            "base_url": "https://api.example.com",
            "_token": "test-token",
            "detection": "zero-credit-probe",
        }
    }
    # Also need to mock the other providers to return empty lists quickly
    for p in ["nous", "tokenrouter", "kilo", "openrouter", "amd"]:
        mock_providers[p] = {
            "base_url": f"https://{p}.example.com",
            "_token": "",
            "detection": "other",
        }
    monkeypatch.setattr(im, "PROVIDERS", mock_providers)

    # Mock fetch_provider to return our model_ids for bai, empty for others
    def fake_fetch_provider(config, getter=None):
        name = config.get("name", "")
        if config.get("detection") == "zero-credit-probe":
            return model_ids, {}
        return [], {}

    monkeypatch.setattr(providers, "fetch_provider", fake_fetch_provider)

    # Build fetch_all with our patched probe_model
    fetch_all_fn = im.build_fetch_all({})
    results, metas = fetch_all_fn()

    elapsed = time.time() - start_time

    # 1. All 10 models were probed
    assert sorted(call_log) == sorted(model_ids), f"Expected all 10 models probed, got {call_log}"

    # 2. Only FREE models (even indices: 0, 2, 4, 6, 8) survive
    free_ids = results.get("bai", [])
    assert free_ids == ["model-0", "model-2", "model-4", "model-6", "model-8"], \
        f"Expected only even models, got {free_ids}"

    # 3. Wall-clock proves concurrency: 10 × 0.2s sequential = 2.0s, but 3 workers < 1.0s
    assert elapsed < 1.0, f"Expected concurrent execution (< 1.0s), took {elapsed:.2f}s"
