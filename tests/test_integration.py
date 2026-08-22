"""Integration tests — the full tick loop against stubbed providers."""

import json

import inference_monitor as im


REGISTRY = {"nous", "openrouter", "zen", "kilo", "ollama", "cline"}


def _fetcher(scenarios):
    """scenarios: list of {name: ids|None} consumed one per fetch_all call.
    fetch_one uses the CURRENT scenario."""
    calls = {"n": 0}

    def current():
        return scenarios[min(calls["n"], len(scenarios) - 1)]

    def fetch_all():
        snap = dict(current())
        calls["n"] += 1
        return snap

    def fetch_one(name):
        return current()[name]

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


def test_confirmed_removal_alerts_once_then_cooldowns(tmp_path, capsys):
    _run(tmp_path, [{"nous": ["a", "b"]}])                       # baseline
    code, _ = _run(tmp_path, [{"nous": ["a"]}],                  # b disappears
                   now=1_000_000_000 + 6 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "➖ `b`" in out
    # same flap 1h later: suppressed by cooldown
    _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 7 * 3600)
    out2 = capsys.readouterr().out
    assert "➖ `b`" not in out2


def test_transient_removal_never_alerts(tmp_path, capsys):
    _run(tmp_path, [{"zen": ["z1", "z2"]}])
    # candidate removal, but recheck sees z2 back -> transient, silent
    code, _ = _run(tmp_path, [{"zen": ["z1"]}, {"zen": ["z1", "z2"]}],
                   now=1_000_000_000 + 6 * 3600)
    out = capsys.readouterr().out
    assert code == 0
    assert "➖" not in out
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert roster["providers"]["zen"] == ["z1", "z2"]  # recheck state wins


def test_fetch_failure_sticky_no_alert(tmp_path, capsys):
    _run(tmp_path, [{"nous": ["a", "b"]}])
    code, _ = _run(tmp_path, [{"nous": None}], now=1_000_000_000 + 6 * 3600)
    out = capsys.readouterr().out
    assert code == 1                       # partial failure exit code
    assert "➖" not in out                 # outage never looks like removal
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


def test_dry_run_writes_nothing(tmp_path, capsys):
    code, _ = _run(tmp_path, [{"nous": ["a"]}], dry_run=True)
    assert code == 0
    assert not (tmp_path / "roster.json").exists()
    assert not (tmp_path / "alive.json").exists()


def test_registry_filter_kills_zombies(tmp_path, capsys):
    _run(tmp_path, [{"nous": ["a"], "zen": ["zombie"]}])
    # registry shrinks to nous only: zombie zen must vanish silently
    code, _ = _run(tmp_path, [{"nous": ["a"]}], now=1_000_000_000 + 6 * 3600)
    roster = json.loads((tmp_path / "roster.json").read_text())
    assert "zen" not in roster["providers"]
    assert "➖" not in capsys.readouterr().out
