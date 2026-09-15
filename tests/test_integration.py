"""Integration tests — the full tick loop against stubbed providers."""

import json
import os
import stat
import time
from pathlib import Path

import inference_watchdog as im


REGISTRY = {"nous", "openrouter", "tokenrouter", "kilo", "amd", "bai"}


def _fetcher(scenarios):
    """scenarios: list of {name: ids|None} consumed one per fetch_all call.
    fetch_one uses the CURRENT scenario."""
    calls = {"n": 0, "one_n": 0}

    def current():
        return scenarios[min(calls["n"], len(scenarios) - 1)]

    def fetch_all():
        snap = dict(current())
        calls["n"] += 1
        # Tick now expects (results_map, meta_map); meta is passive telemetry.
        # Real build_fetch_all semantics: metas keyed ONLY on fetch SUCCESS —
        # failed gateways (None) are ABSENT from metas, not present-with-{}.
        metas = {name: {} for name, ids in snap.items() if ids is not None}
        return snap, metas

    def fetch_one(name):
        calls["one_n"] += 1
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
    alert."""
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


def test_confirmed_removal_alerts(tmp_path, capsys):
    _run(tmp_path, [{"nous": ["a", "b"]}])                       # baseline
    code, _ = _run(tmp_path, [{"nous": ["a"]}],                  # b disappears
                   now=1_000_000_000 + 1 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "🔴 `b`" in out


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
    next invocation acquired and ran concurrent full ticks (duplicate alerts).
    Contract: contended run exits 0 AND leaves the
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


def test_ratelimits_persisted_for_every_succeeded_gateway(tmp_path):
    """R2-6 generalized: every gateway that succeeded carries its passive
    ratelimit headers into roster['ratelimits']; failed gateways are
    absent (not present-with-{})."""
    fetch_all, fetch_one, _ = _fetcher([{"nous": ["a"], "kilo": ["b"],
                                         "openrouter": None}])
    def fetch_all_with_meta():
        results, metas = fetch_all()
        # nous succeeded WITH ratelimit headers; kilo succeeded WITHOUT
        # (empty meta); openrouter failed — the base harness already keeps
        # it absent from metas, mirroring real build_fetch_all semantics.
        metas["nous"] = {"ratelimit": {"x-ratelimit-remaining": "9"}}
        return results, metas
    im.run_tick(
        tmp_path, REGISTRY, fetch_all_with_meta, fetch_one,
        webhook_url=None, sleep=lambda s: None, now=1_000_000_000,
        recheck_delay=0)
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["ratelimits"] == {"nous": {"x-ratelimit-remaining": "9"},
                                    "kilo": {}}
    assert "nous_ratelimit" not in roster
    assert "openrouter" not in roster["ratelimits"]   # the failed gateway


def test_ratelimits_empty_when_no_gateway_carries_headers(tmp_path):
    """Succeeded gateways with no ratelimit headers land as {} entries;
    a fully-failed gateway is absent (metas keyed only on success)."""
    _run(tmp_path, [{"nous": None, "openrouter": ["x"]}])
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["ratelimits"] == {"openrouter": {}}
    assert "nous" not in roster["ratelimits"]


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
    Tick B: same candidate recheck succeeds => one alert."""
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


def test_zero_credit_probe_serial_throttle(monkeypatch, tmp_path, capsys):
    """P3: probes run serially with 5s spacing, not concurrently.
    Verifies burst-kill regression is fixed — no ThreadPoolExecutor."""
    import inference_watchdog as im
    from probe_zero_credit import Result
    import providers

    call_log = []
    sleep_log = []

    def fake_probe(base_url, token, model_id, probe_cfg=None, timeout=30):
        call_log.append(model_id)
        return Result.FREE, {"http": 200}

    def fake_sleep(s):
        sleep_log.append(s)

    monkeypatch.setattr(im, "probe_model", fake_probe)

    model_ids = [f"model-{i}" for i in range(5)]
    mock_providers = {
        "bai": {
            "base_url": "https://api.example.com",
            "_token": "test-token",
            "detection": "zero-credit-probe",
        }
    }
    for p in ["nous", "tokenrouter", "kilo", "openrouter", "amd"]:
        mock_providers[p] = {
            "base_url": f"https://{p}.example.com",
            "_token": "",
            "detection": "other",
        }
    monkeypatch.setattr(im, "PROVIDERS", mock_providers)

    def fake_fetch_provider(config, getter=None):
        if config.get("detection") == "zero-credit-probe":
            return model_ids, {}
        return [], {}

    monkeypatch.setattr(providers, "fetch_provider", fake_fetch_provider)

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=fake_sleep)
    results, metas = fetch_all_fn()

    # All 5 models probed serially
    assert sorted(call_log) == sorted(model_ids)

    # All FREE -> all on roster
    assert results["bai"] == sorted(model_ids)

    # 5 models: 4 sleeps between them (first fires immediately)
    assert len(sleep_log) == 4
    assert all(s == 5 for s in sleep_log)

    # State persisted
    state = im.probe_state.load_probe_state(tmp_path / "probe_state.json")
    assert "bai" in state
    assert len(state["bai"]) == 5


def test_env_loaded_from_project_local_env_not_hermes(tmp_path, monkeypatch):
    """The watchdog must read its webhook/config from THIS project's .env
    (envfile.parse_envfile default), never from ~/.hermes/.env — the repo is
    agent-agnostic. Guards against a hardcoded Hermes path sneaking back in.

    Regression: a source-level pin proving (a) the hardcoded HERMES_ENV path
    is gone and (b) main() loads via parse_envfile() project-local default.
    """
    src = (Path(__file__).resolve().parent.parent / "inference_watchdog.py").read_text()

    # (a) no hardcoded Hermes env path and no HERMES_ENV constant may exist
    assert "HERMES_ENV" not in src
    assert "~/.hermes/.env" not in src

    # (b) main() reads env via parse_envfile() with NO explicit path arg,
    # so it resolves to envfile's project-local .env default (agent-agnostic).
    assert "env = parse_envfile()" in src

    # (c) behavioral proof: parse_envfile with no arg reads the project-local
    # .env next to the repo (envfile default), not ~/.hermes/.env.
    captured = {}
    def fake_parse_envfile(*args):
        captured["args"] = args
        return {}
    monkeypatch.setattr(im, "parse_envfile", fake_parse_envfile)
    fake_tick = lambda *a, **kw: 0
    monkeypatch.setattr(im, "run_tick", fake_tick)
    im.main([])
    assert captured["args"] == (), f"parse_envfile called with args {captured['args']}"


# ---------------------------------------------------------------------------
# Resolver path: build_resolver / resolve_pending (--resolve guts)
# ---------------------------------------------------------------------------

import pending_removals
from datetime import datetime

RESOLVER_T0 = 1_000_000_000
RESOLVER_REGISTRY = {"amd": {"removal_hold_seconds": 1800}, "nous": {}}


def _write_q(tmp, mapping):
    """Write the hold queue verbatim (junk shapes allowed) at its canonical
    path and return that path for before/after byte comparisons."""
    path = pending_removals.path_in(tmp)
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def _load_q(tmp):
    return json.loads(pending_removals.path_in(tmp).read_text())


def test_r1_empty_queue_zero_fetch_zero_write_silent(tmp_path, capsys):
    """Behavior 1: nothing held -> no fetch, no writes, no output."""
    _write_q(tmp_path, {})
    _fetch_all, fetch_one, calls = _fetcher([{}])
    res = im.build_resolver(tmp_path, fetch_one, RESOLVER_REGISTRY)
    out = res(RESOLVER_T0 + 9000)
    cap = capsys.readouterr()
    assert out == {"fired": False, "changed": False}
    assert calls["n"] == 0 and calls.get("one_n", 0) == 0
    assert _load_q(tmp_path) == {}
    assert cap.out == "" and cap.err == ""
    assert not (tmp_path / "roster.json").exists()


def _resolver(tmp, scenario, now, registry=RESOLVER_REGISTRY, **kw):
    """Run one resolve pass on a _fetcher([scenario]) seam."""
    _fetch_all, fetch_one, calls = _fetcher([scenario])
    res = im.build_resolver(tmp, fetch_one, registry)
    return res(now, **kw), calls


def test_r4_unexpired_absent_refreshes_stamp_and_saves_silently(tmp_path,
                                                                capsys):
    """Behavior 4: still absent, hold unexpired -> last_absent_seen updated,
    gone_since NEVER rewritten, roster untouched, stdout+stderr silent; the
    save decision is the DEEP held != snapshot comparison, not settle's
    changed-membership (a stamp refresh is absent from changed by contract —
    this test would silently pass if persistence ever keyed off it)."""
    roster = tmp_path / "roster.json"
    roster.write_text("PIN", encoding="utf-8")
    _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8000,
                                      "last_absent_seen": RESOLVER_T0 + 8000}}})
    out, calls = _resolver(tmp_path, {"amd": []}, RESOLVER_T0 + 9000)
    cap = capsys.readouterr()
    assert out == {"fired": False, "changed": True}
    assert cap.out == "" and cap.err == ""
    assert calls["one_n"] == 1
    e = _load_q(tmp_path)["amd"]["a"]
    assert e == {"gone_since": RESOLVER_T0 + 8000,
                 "last_absent_seen": RESOLVER_T0 + 9000}
    assert roster.read_bytes() == b"PIN"


def test_r2_orphan_guard_drops_without_registry_fetch_or_roster_touch(
        tmp_path, capsys):
    """Behavior 2: held provider absent from the registry is dropped from the
    queue with the unified stderr note, zero fetch_one calls for it, roster
    untouched; amd (in registry) just waits and its refresh saves."""
    roster = tmp_path / "roster.json"
    roster.write_text("PIN", encoding="utf-8")   # never re-read: bytes pin
    _write_q(tmp_path, {"ghost": {"x": {"gone_since": RESOLVER_T0,
                                        "last_absent_seen": RESOLVER_T0}},
                        "amd": {"a": {"gone_since": RESOLVER_T0 + 8000,
                                      "last_absent_seen": RESOLVER_T0 + 8000}}})
    out, calls = _resolver(tmp_path, {"amd": []}, RESOLVER_T0 + 9000)
    err = capsys.readouterr().err
    assert ("inference-watchdog: dropped pending entries for ghost "
            "(not in registry)") in err
    q = _load_q(tmp_path)
    assert "ghost" not in q                       # dropped from the file...
    assert q["amd"]["a"] == {"gone_since": RESOLVER_T0 + 8000,
                             "last_absent_seen": RESOLVER_T0 + 9000}
    assert calls["one_n"] == 1                    # fetched amd exactly once
    assert out == {"fired": False, "changed": True}
    assert roster.read_bytes() == b"PIN"


def test_r3_recovery_unions_roster_and_preserves_every_other_field(tmp_path,
                                                                   capsys):
    """Behavior 3: recovery consumes the entry and restores the roster
    whole-document read-modify-write — ONLY that provider's list is replaced
    by sorted_ids(set(old) | set(recovered)); tick_epoch/stale_providers/
    transients/unconfirmed/ratelimits and other providers survive byte-for-
    byte (crash re-run re-applies the union idempotently)."""
    roster = tmp_path / "roster.json"
    seeded = {"tick_epoch": 777, "providers": {"amd": [42, "b"], "nous": ["keep"]},
              "stale_providers": ["nous"],
              "transients": {"nous": {"added": [], "removed": ["t"]}},
              "unconfirmed": {"amd": {"added": [], "removed": ["u"]}},
              "ratelimits": {"nous": {"x-ratelimit-remaining": "9"}}}
    roster.write_text(json.dumps(seeded), encoding="utf-8")
    _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8500,
                                      "last_absent_seen": RESOLVER_T0 + 8500},
                                "c": {"gone_since": RESOLVER_T0 + 8900,
                                      "last_absent_seen": RESOLVER_T0 + 8900}}})
    out, calls = _resolver(tmp_path, {"amd": ["a", "c"]}, RESOLVER_T0 + 9000)
    cap = capsys.readouterr()
    assert out == {"fired": False, "changed": True}   # consumed -> changed
    assert cap.out == ""                              # recovery is silent
    assert calls["one_n"] == 1
    r = json.loads(roster.read_text())
    assert r["providers"]["amd"] == ["a", "b", "c"]  # union, sorted
    assert r["tick_epoch"] == 777
    assert r["stale_providers"] == ["nous"]
    assert r["transients"] == seeded["transients"]
    assert r["unconfirmed"] == seeded["unconfirmed"]
    assert r["ratelimits"] == seeded["ratelimits"]
    assert r["providers"]["nous"] == ["keep"]         # untouched provider
    assert _load_q(tmp_path) == {}
    # crash re-run: queue still held (pre-save crash sim) -> union
    # re-applies byte-identically (idempotent)
    _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8500,
                                      "last_absent_seen": RESOLVER_T0 + 8500},
                                "c": {"gone_since": RESOLVER_T0 + 8900,
                                      "last_absent_seen": RESOLVER_T0 + 8900}}})
    out2, calls2 = _resolver(tmp_path, {"amd": ["a", "c"]},
                             RESOLVER_T0 + 9060)
    assert out2 == {"fired": False, "changed": True}
    assert calls2["one_n"] == 1
    assert json.loads(roster.read_text()) == r  # union byte-identical again
    assert _load_q(tmp_path) == {}


def test_r3b_missing_roster_is_visible_skip(tmp_path, capsys):
    """No roster file: recovery is skipped with the unified note and NOTHING
    is written as a roster; the queue still consumes via the normal save."""
    _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8500,
                                      "last_absent_seen": RESOLVER_T0 + 8500}}})
    out, calls = _resolver(tmp_path, {"amd": ["a"]}, RESOLVER_T0 + 9000)
    cap = capsys.readouterr()
    assert "inference-watchdog: roster unusable — hold recoveries not applied" in cap.err
    assert cap.out == ""
    assert not (tmp_path / "roster.json").exists()
    assert _load_q(tmp_path) == {}
    assert out == {"fired": False, "changed": True}


def test_r3c_corrupt_providers_is_visible_skip(tmp_path, capsys):
    """providers not a dict: visible skip, roster bytes UNCHANGED (no
    rewrite — the next tick bootstraps clean instead of added-storming)."""
    roster = tmp_path / "roster.json"
    corrupt = json.dumps({"tick_epoch": 1, "providers": "junk"})
    roster.write_text(corrupt, encoding="utf-8")
    _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8500,
                                      "last_absent_seen": RESOLVER_T0 + 8500}}})
    before = roster.read_bytes()
    out, calls = _resolver(tmp_path, {"amd": ["a"]}, RESOLVER_T0 + 9000)
    cap = capsys.readouterr()
    assert "inference-watchdog: roster unusable — hold recoveries not applied" in cap.err
    assert roster.read_bytes() == before
    assert _load_q(tmp_path) == {}
    assert out == {"fired": False, "changed": True}


def test_r3d_union_only_extras_never_enter_roster(tmp_path, capsys):
    """Behavior 3 (union only): an untracked id visible in the resolver's
    fetch is NOT added to the roster — the next hourly tick still alerts it 🟢."""
    roster = tmp_path / "roster.json"
    seeded = {"tick_epoch": 777, "providers": {"amd": [], "nous": ["keep"]},
              "stale_providers": [], "transients": {}, "unconfirmed": {},
              "ratelimits": {}}
    roster.write_text(json.dumps(seeded), encoding="utf-8")
    _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8500,
                                      "last_absent_seen": RESOLVER_T0 + 8500}}})
    out, _ = _resolver(tmp_path, {"amd": ["a", "surprise"]}, RESOLVER_T0 + 9000)
    assert out == {"fired": False, "changed": True}
    r = json.loads(roster.read_text())
    assert r["providers"]["amd"] == ["a"]             # no "surprise"
    assert _load_q(tmp_path) == {}
    capsys.readouterr()
    # The hourly tick faces the same evidence: the extra is a fresh 🟢.
    code, _ = _run(tmp_path, [{"amd": ["a", "surprise"], "nous": ["keep"]}],
                   now=RESOLVER_T0 + 9600)
    assert code == 0
    out_tick = capsys.readouterr().out
    assert "🟢 `surprise`" in out_tick


def test_r5_expired_alerts_with_two_phase_at_least_once_persist(tmp_path,
                                                                 capsys,
                                                                 monkeypatch):
    """Behavior 5: expiry fires byte-identically AND persists two-phase: the
    pre-emit save still holds the expired entry with ORIGINAL stamps (a crash
    mid-emit re-alerts next pass); the post-emit save consumes it; the roster
    is never touched by an expiry."""
    import notify
    monkeypatch.setattr(notify, "_dropped_total", 0)
    roster = tmp_path / "roster.json"
    roster.write_text("PIN", encoding="utf-8")
    stamps = {"amd": {"x": {"gone_since": RESOLVER_T0 + 7000,
                            "last_absent_seen": RESOLVER_T0 + 7000}}}
    _write_q(tmp_path, stamps)
    saves = []
    real_save = pending_removals.save

    def spy(path, held):
        saves.append(json.loads(json.dumps(held)))
        real_save(path, held)

    monkeypatch.setattr(pending_removals, "save", spy)
    now = RESOLVER_T0 + 9000          # 2000 >= hold 1800 -> EXPIRED
    tick_iso = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    expected = notify.format_alert({"amd": {"added": [], "removed": ["x"]}},
                                   tick_iso=tick_iso,
                                   providers_polled=len(RESOLVER_REGISTRY),
                                   transients={}, stale=[], dropped_total=0)
    out, calls = _resolver(tmp_path, {"amd": []}, now)
    cap = capsys.readouterr()
    assert out == {"fired": True, "changed": True}
    assert calls["one_n"] == 1
    assert cap.out == expected + "\n"             # EXACT equality, no substring
    assert saves[0] == stamps                     # pre-emit: entry STILL queued
    assert saves[-1] == {}                        # post-emit: consumed
    assert roster.read_bytes() == b"PIN"          # expiry never touches roster


def test_r10_webhook_drains_pending_queue_before_emit(tmp_path, capsys,
                                                      monkeypatch):
    """Behavior 10: with webhook_url set, notify.drain_pending runs BEFORE the
    emit block — the queued alert posts first, the fresh alert second, and the
    drain's writes are real."""
    import notify
    monkeypatch.setattr(notify, "_dropped_total", 0)
    _write_q(tmp_path, {"amd": {"x": {"gone_since": RESOLVER_T0 + 7000,
                                      "last_absent_seen": RESOLVER_T0 + 7000}}})
    alerts = tmp_path / "pending_alerts.json"
    alerts.write_text(json.dumps(
        [{"payload": {"content": "queued-1"}, "attempts": 1,
         "first_queued_epoch": 123}]), encoding="utf-8")
    posted = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10):
        posted.append(json.loads(req.data.decode()))
        return Resp()

    monkeypatch.setattr(notify, "_urlopen", fake_urlopen)
    out, _ = _resolver(tmp_path, {"amd": []}, RESOLVER_T0 + 9000,
                       webhook_url="https://example/hook")
    assert out == {"fired": True, "changed": True}
    assert len(posted) == 2
    assert posted[0]["content"] == "queued-1"     # drained BEFORE the emit
    fresh_msg = capsys.readouterr().out.rstrip("\n")
    assert posted[1]["content"] == fresh_msg      # posted rows are decoded dicts
    assert _load_q(tmp_path) == {}                # post-emit save still runs
    assert json.loads(alerts.read_text()) == []   # drain flushed


def test_r8_fetches_parameter_short_circuits_fetch_one(tmp_path, capsys):
    """Behavior 8: the evidence-injection seam — {p: ids} settles without a
    fetch_one call; None/absent values stay NEUTRAL even when fetches is
    given."""
    _write_q(tmp_path, {"amd": {"x": {"gone_since": RESOLVER_T0 + 8000,
                                      "last_absent_seen": RESOLVER_T0 + 8000}}})
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"tick_epoch": 777,
                                  "providers": {"amd": []}}), encoding="utf-8")
    out, calls = _resolver(tmp_path, {"amd": ["x"]}, RESOLVER_T0 + 9000,
                           fetches={"amd": ["x"]})
    assert out == {"fired": False, "changed": True}
    assert calls["one_n"] == 0                    # short-circuited
    assert json.loads(roster.read_text())["providers"]["amd"] == ["x"]
    assert _load_q(tmp_path) == {}
    # neutral: None value for a held provider -> clock frozen, nothing saved
    _write_q(tmp_path, {"amd": {"y": {"gone_since": RESOLVER_T0 + 8000,
                                      "last_absent_seen": RESOLVER_T0 + 8000}}})
    q = pending_removals.path_in(tmp_path)
    before = q.read_bytes()
    out2, _ = _resolver(tmp_path, {}, RESOLVER_T0 + 9000,
                        fetches={"amd": None})
    assert out2 == {"fired": False, "changed": False}
    assert q.read_bytes() == before
    capsys.readouterr()


def test_r6_fetch_failure_changes_nothing(tmp_path, capsys):
    """Behavior 6: fetch_one raising FetchError -> neutral: zero writes,
    silent, nothing consumed."""
    q = _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0,
                                          "last_absent_seen": RESOLVER_T0}}})
    roster = tmp_path / "roster.json"
    roster.write_text("PIN", encoding="utf-8")
    before = q.read_bytes()
    out, calls = _resolver(tmp_path, {"amd": None}, RESOLVER_T0 + 99_999)
    cap = capsys.readouterr()
    assert out == {"fired": False, "changed": False}
    assert calls["one_n"] == 1                    # attempted, failed
    assert q.read_bytes() == before
    assert _load_q(tmp_path) == {"amd": {"a": {"gone_since": RESOLVER_T0,
                                               "last_absent_seen": RESOLVER_T0}}}
    assert roster.read_bytes() == b"PIN"
    assert cap.out == "" and cap.err == ""


def test_r7_unflagged_provider_releases_silently_without_roster_rewrite(
        tmp_path, capsys):
    """Behavior 7: registry config lacks removal_hold_seconds -> entries are
    consumed with no alert and the roster is NOT rewritten (released ids
    never enter the union path)."""
    roster = tmp_path / "roster.json"
    seeded = {"tick_epoch": 777, "providers": {"nous": []}, "transients": {},
              "unconfirmed": {}, "ratelimits": {}, "stale_providers": []}
    roster.write_text(json.dumps(seeded), encoding="utf-8")
    _write_q(tmp_path, {"nous": {"x": {"gone_since": RESOLVER_T0 + 8900,
                                       "last_absent_seen": RESOLVER_T0 + 8900}}})
    out, _ = _resolver(tmp_path, {"nous": ["x"]}, RESOLVER_T0 + 9000)
    cap = capsys.readouterr()
    assert out == {"fired": False, "changed": True}
    assert cap.out == "" and cap.err == ""
    assert _load_q(tmp_path) == {}
    assert json.loads(roster.read_text()) == seeded   # no release -> no write


def test_r4b_all_junk_queue_rides_empty_path_forever_and_partial_junk_self_heals(
        tmp_path, capsys):
    """Behavior 4's accepted edges: an entirely-junk queue loads to {}, takes
    the empty-queue rule (no fetch, NO write — load's stderr note re-fires
    every pass, stdout stays silent, no page); a partially-junk file
    self-heals via the normal save once anything else changes."""
    q = _write_q(tmp_path, {"amd": "not-a-dict"})
    out, calls = _resolver(tmp_path, {"amd": ["x"]}, RESOLVER_T0 + 9000)
    cap = capsys.readouterr()
    assert out == {"fired": False, "changed": False}
    assert calls["one_n"] == 0
    assert q.read_bytes() == json.dumps({"amd": "not-a-dict"}).encode()
    assert cap.out == ""
    assert "pending_removals: dropped 1 junk entry" in cap.err
    # second pass: the note re-fires, still no write
    _, _ = _resolver(tmp_path, {"amd": ["x"]}, RESOLVER_T0 + 9060)
    assert "dropped 1 junk entry" in capsys.readouterr().err
    assert q.read_bytes() == json.dumps({"amd": "not-a-dict"}).encode()
    # partially junk: valid amd entry refreshes -> save purges the junk slice
    _write_q(tmp_path, {"ghost": {"y": {"gone_since": RESOLVER_T0,
                                        "last_absent_seen": RESOLVER_T0}},
                        "amd": {"a": {"gone_since": RESOLVER_T0 + 8000,
                                      "last_absent_seen": RESOLVER_T0 + 8000}}})
    out, _ = _resolver(tmp_path, {"amd": []}, RESOLVER_T0 + 9000)
    assert out == {"fired": False, "changed": True}
    assert _load_q(tmp_path) == {
        "amd": {"a": {"gone_since": RESOLVER_T0 + 8000,
                      "last_absent_seen": RESOLVER_T0 + 9000}}}
    capsys.readouterr()


def test_r9_dry_run_prints_would_fire_alert_and_writes_nothing(tmp_path,
                                                               capsys):
    """Behavior 9: dry_run prints the alert the real pass would fire but
    writes NOTHING — pending queue, roster and pending_alerts byte-compare
    before/after; the retry queue is never drained."""
    _write_q(tmp_path, {"amd": {"x": {"gone_since": RESOLVER_T0 + 4000,
                                      "last_absent_seen": RESOLVER_T0 + 4000},
                                "y": {"gone_since": RESOLVER_T0 + 8900,
                                      "last_absent_seen": RESOLVER_T0 + 8900}},
                        "nous": {"n1": {"gone_since": RESOLVER_T0 + 5000,
                                        "last_absent_seen": RESOLVER_T0 + 5000}}})
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"tick_epoch": 777, "providers": {}}),
                      encoding="utf-8")
    alerts = tmp_path / "pending_alerts.json"
    alerts.write_text(json.dumps([{"payload": {"content": "stale"},
                                   "attempts": 1,
                                   "first_queued_epoch": 123}]),
                      encoding="utf-8")
    before = (pending_removals.path_in(tmp_path).read_bytes(),
              roster.read_bytes(), alerts.read_bytes())
    now = RESOLVER_T0 + 9000
    import notify
    expected = notify.format_alert(
        {"amd": {"added": [], "removed": ["x"]}},
        tick_iso=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"),
        providers_polled=len(RESOLVER_REGISTRY),
        transients={}, stale=[], dropped_total=0)
    out, _ = _resolver(tmp_path, {"amd": ["y"], "nous": ["n1"]}, now,
                       dry_run=True)
    cap = capsys.readouterr()
    assert out == {"fired": True, "changed": True}
    assert cap.out == expected + "\n"             # byte-identical to real pass
    assert (pending_removals.path_in(tmp_path).read_bytes(),
            roster.read_bytes(), alerts.read_bytes()) == before


def test_r9b_neutral_pass_writes_nothing(tmp_path):
    """A pass where every provider fetch-FAILS (settle fully neutral,
    held == snapshot) must NOT rewrite the queue file at all — the
    deep-compare gate must be real in the no-write direction too.
    Byte-equality AND mtime can't catch a redundant identical save (an
    os.replace landing in the same clock tick leaves st_mtime_ns unchanged);
    the inode does — every save replaces the file and changes st_ino."""
    path = _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8000,
                                             "last_absent_seen": RESOLVER_T0 + 8000}}})
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
    out, calls = _resolver(tmp_path, {"amd": None}, RESOLVER_T0 + 9000)
    after = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
    assert out == {"fired": False, "changed": False}
    assert calls["one_n"] == 1                    # fetch attempted, raised
    assert after == before                        # zero rewrite on neutral pass


def test_r9c_ghost_drop_reports_changed_but_writes_nothing(tmp_path, capsys):
    """dry_run must gate even the orphan-drop save: the note is printed,
    changed reports the would-be rewrite, the queue file stays byte-equal,
    and the ghost is never fetched (guard runs before the fetch loop)."""
    path = _write_q(tmp_path, {"ghost": {"x": {"gone_since": RESOLVER_T0,
                                               "last_absent_seen": RESOLVER_T0}}})
    before = path.read_bytes()
    out, calls = _resolver(tmp_path, {}, RESOLVER_T0 + 100, dry_run=True)
    cap = capsys.readouterr()
    assert out == {"fired": False, "changed": True}
    assert path.read_bytes() == before
    assert ("inference-watchdog: dropped pending entries for ghost "
            "(not in registry)") in cap.err
    assert calls["one_n"] == 0


def test_r9d_dry_run_recovery_applies_nothing(tmp_path,
                                              capsys):
    """dry_run must gate the recovery path too: with a queue entry whose id
    is present in the fetch evidence, a dry_run pass reports changed=True,
    prints nothing, and leaves BOTH roster bytes and queue bytes untouched
    (no union applied, no consumption saved)."""
    roster = tmp_path / "roster.json"
    seeded = {"tick_epoch": 777, "providers": {"amd": ["b"]}}
    roster.write_text(json.dumps(seeded), encoding="utf-8")
    path = _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8500,
                                             "last_absent_seen": RESOLVER_T0 + 8500}}})
    before = (path.read_bytes(), roster.read_bytes())
    out, calls = _resolver(tmp_path, {"amd": ["a"]}, RESOLVER_T0 + 9000,
                           dry_run=True)
    cap = capsys.readouterr()
    assert out == {"fired": False, "changed": True}
    assert cap.out == "" and cap.err == ""
    assert (path.read_bytes(), roster.read_bytes()) == before
    assert calls["one_n"] == 1


def test_r9e_dry_run_with_webhook_never_drains(tmp_path, capsys,
                                               monkeypatch):
    """dry_run + webhook_url set: the retry queue must NEVER be drained —
    pending_alerts.json bytes untouched (a drain would flush it to []).
    The emit still prints the alert plus the would-POST marker."""
    import notify
    monkeypatch.setattr(notify, "_dropped_total", 0)
    _write_q(tmp_path, {"amd": {"x": {"gone_since": RESOLVER_T0 + 7000,
                                      "last_absent_seen": RESOLVER_T0 + 7000}}})
    alerts = tmp_path / "pending_alerts.json"
    alerts.write_text(json.dumps([{"payload": {"content": "queued-1"},
                                   "attempts": 1,
                                   "first_queued_epoch": 123}]),
                      encoding="utf-8")
    before = alerts.read_bytes()
    posted = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10):
        posted.append(json.loads(req.data.decode()))
        return Resp()

    monkeypatch.setattr(notify, "_urlopen", fake_urlopen)
    out, _ = _resolver(tmp_path, {"amd": []}, RESOLVER_T0 + 9000,
                       dry_run=True, webhook_url="https://example/hook")
    cap = capsys.readouterr()
    assert out == {"fired": True, "changed": True}
    assert posted == []                           # nothing POSTED at all
    assert alerts.read_bytes() == before          # queue not drained/flushed
    assert "[dry-run] would POST to webhook" in cap.out


def test_resolve_lock_contention_exits_zero(tmp_path, capsys):
    """Mirror of test_lock_contention_exits_zero for the resolve path: a LIVE
    lock (1 min old) makes run_resolve print 'already running', exit 0, and
    never fetch."""
    lock = tmp_path / "monitor.lock"
    lock.write_text("123", encoding="utf-8")
    old = time.time() - 60  # fresh live lock (1 min old)
    os.utime(lock, (old, old))
    # amd held, unexpired -> a real pass WOULD fetch_one (scenario amd: [])
    _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8000,
                                      "last_absent_seen": RESOLVER_T0 + 8000}}})
    _fetch_all, fetch_one, calls = _fetcher([{"amd": []}])
    code = im.run_resolve(tmp_path, RESOLVER_REGISTRY, fetch_one, None,
                          now=RESOLVER_T0 + 9000)
    err = capsys.readouterr().err
    assert code == 0                               # instant exit, no crash
    assert "already running" in err
    assert calls["one_n"] == 0                     # resolver body never ran


def test_resolve_lock_contention_preserves_live_lockfile(tmp_path, capsys):
    """F7-1 mirror for run_resolve: a contended resolve must NEVER delete the
    LIVE lockfile owned by the other running process — lock bytes AND mtime
    byte-identical, zero fetches."""
    lock = tmp_path / "monitor.lock"
    lock.write_text("123", encoding="utf-8")
    old = time.time() - 60  # fresh live lock (1 min old)
    os.utime(lock, (old, old))
    before_bytes = lock.read_bytes()
    before_mtime = os.stat(lock).st_mtime
    _write_q(tmp_path, {"amd": {"a": {"gone_since": RESOLVER_T0 + 8000,
                                      "last_absent_seen": RESOLVER_T0 + 8000}}})
    _fetch_all, fetch_one, calls = _fetcher([{"amd": []}])
    code = im.run_resolve(tmp_path, RESOLVER_REGISTRY, fetch_one, None,
                          now=RESOLVER_T0 + 9000)
    capsys.readouterr()
    assert code == 0                               # contention policy unchanged
    assert lock.exists(), "live lockfile was DELETED by contended resolve"
    assert lock.read_bytes() == before_bytes       # byte-identical
    assert os.stat(lock).st_mtime == before_mtime  # untouched mtime
    assert calls["one_n"] == 0                     # resolver body never ran


def test_cli_resolve_routes_to_run_resolve(monkeypatch, tmp_path):
    """T3B-2: --resolve must route to run_resolve with the state dir,
    registry, fetch_one, webhook and dry-run flag — never to run_tick."""
    captured = {}

    def fake_resolve(state_dir, registry, fetch_one, **kw):
        captured["state_dir"] = state_dir
        captured["registry"] = registry
        captured["fetch_one"] = fetch_one
        captured.update(kw)
        return 0

    monkeypatch.setattr(im, "run_resolve", fake_resolve)
    monkeypatch.setattr(im, "run_tick",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("run_tick must not run")))
    result = im.main(["--resolve", "--state-dir", str(tmp_path)])
    assert result == 0
    assert captured["state_dir"] == tmp_path
    assert captured["dry_run"] is False


def test_cli_resolve_and_init_rejected(capsys):
    """T3B-2: --resolve together with --init is an operator error — --resolve
    never rebaselines, so argparse must reject the pair, never ignore one."""
    import pytest
    with pytest.raises(SystemExit) as ei:
        im.main(["--resolve", "--init"])
    assert ei.value.code == 2
    err = capsys.readouterr().err          # read once: readouterr drains
    assert "--resolve" in err and "--init" in err
