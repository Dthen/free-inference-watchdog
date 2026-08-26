"""Tests for state.py — atomic persistence that can't corrupt state."""

import json
import os
import threading
import time

import pytest

import state


# ---------- roster ----------

def test_save_roster_atomic_leaves_no_tmp(tmp_path):
    p = tmp_path / "roster.json"
    state.save_roster_atomic(p, {"providers": {"nous": ["a"]}})
    assert p.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(p.read_text())["providers"]["nous"] == ["a"]


def test_load_roster_missing_is_none(tmp_path):
    assert state.load_roster(tmp_path / "nope.json") is None


def test_load_roster_corrupt_is_none(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text("{not json!!", encoding="utf-8")
    assert state.load_roster(p) is None


def test_load_roster_non_dict_is_none(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    assert state.load_roster(p) is None


# ---------- cooldowns ----------

def test_cooldowns_roundtrip_and_prune(tmp_path):
    p = tmp_path / "cooldowns.json"
    now = time.time()
    data = {"nous|a|added": now - 100, "openrouter|b|removed": now - 999999}
    state.save_cooldowns(p, data, ttl_s=43200)
    loaded = state.load_cooldowns(p)
    assert "nous|a|added" in loaded          # fresh entry survives
    assert "openrouter|b|removed" not in loaded  # stale entry pruned on save


def test_cooldowns_missing_is_empty(tmp_path):
    assert state.load_cooldowns(tmp_path / "nope.json") == {}


def test_cooldowns_prune_uses_injected_now(tmp_path):
    # Tick-clock pruning: a stamp from the (fake) tick epoch survives/fails
    # relative to the INJECTED now, not the real wall clock.
    p = tmp_path / "cooldowns.json"
    fake_now = 1_000_000_000
    data = {"nous|a|added": fake_now - 100, "nous|b|added": fake_now - 999_999}
    state.save_cooldowns(p, data, ttl_s=43200, now=fake_now)
    loaded = state.load_cooldowns(p)
    assert loaded == {"nous|a|added": fake_now - 100}


# ---------- pending alerts queue ----------

def test_pending_roundtrip(tmp_path):
    p = tmp_path / "pending.json"
    state.append_pending(p, {"text": "hello"})
    state.append_pending(p, {"text": "world"})
    items = state.load_pending(p)
    assert [i["payload"]["text"] for i in items] == ["hello", "world"]
    assert all(i["attempts"] == 0 for i in items)


def test_pending_save_replaces(tmp_path):
    p = tmp_path / "pending.json"
    state.append_pending(p, {"text": "hello"})
    items = state.load_pending(p)
    items[0]["attempts"] = 3
    state.save_pending(p, items)
    assert state.load_pending(p)[0]["attempts"] == 3


def test_pending_missing_is_empty(tmp_path):
    assert state.load_pending(tmp_path / "nope.json") == []


# ---------- alive ----------

def test_alive_roundtrip(tmp_path):
    p = tmp_path / "alive.json"
    state.save_alive(p, last_tick_epoch=1000, last_output_epoch=900,
                     dropped_alerts_total=2)
    a = state.load_alive(p)
    assert a == {"last_tick_epoch": 1000, "last_output_epoch": 900,
                 "dropped_alerts_total": 2}


def test_alive_missing_is_empty_dict(tmp_path):
    assert state.load_alive(tmp_path / "nope.json") == {}


# ---------- S5-2: numeric fields validated/coerced at the boundary ----------

def test_alive_numeric_fields_coerced_or_dropped(tmp_path):
    """S5-2: last_tick_epoch / last_output_epoch / dropped_alerts_total feed
    raw arithmetic in alive.py and inference_watchdog.py — a hand-edited
    string or null raised TypeError => FATAL exit 2 on every normal tick.
    At load: int/float coerce via int(), anything else (str, None, missing)
    drops the field (treated absent; callers already handle absent fields)."""
    p = tmp_path / "alive.json"
    p.write_text(json.dumps({
        "last_tick_epoch": "999",
        "dropped_alerts_total": None,
    }), encoding="utf-8")
    assert state.load_alive(p) == {}

    # float coerces (json may carry 1000.0); str/null/bool-free junk drops
    p.write_text(json.dumps({
        "last_tick_epoch": 1755.0,
        "last_output_epoch": "nope",
        "dropped_alerts_total": 7,
    }), encoding="utf-8")
    assert state.load_alive(p) == {
        "last_tick_epoch": 1755,
        "dropped_alerts_total": 7,
    }


def test_alive_wellformed_file_unchanged(tmp_path):
    state.save_alive(tmp_path / "a.json", last_tick_epoch=1000,
                     last_output_epoch=900, dropped_alerts_total=2)
    assert state.load_alive(tmp_path / "a.json") == {
        "last_tick_epoch": 1000, "last_output_epoch": 900,
        "dropped_alerts_total": 2}
    # a hand-written but well-formed file passes through untouched
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"last_tick_epoch": 5, "last_output_epoch": 4,
                             "dropped_alerts_total": 0}), encoding="utf-8")
    assert state.load_alive(p) == {
        "last_tick_epoch": 5, "last_output_epoch": 4,
        "dropped_alerts_total": 0}


# ---------- fix-round-2 S1: NaN/Infinity JSON literals ----------

def test_alive_nonfinite_json_literals_drop_field(tmp_path):
    """S1: json.load accepts Python's NaN/Infinity/-Infinity literals (the
    stdlib parser is deliberately liberal). They are int/float, so they used
    to pass the boundary gate and explode in int() — ValueError for NaN,
    OverflowError for Infinity — a FATAL exit-2 on EVERY tick. Worse, the
    poison self-perpetuates: save only happens after the read that crashed.
    Non-finite numbers must DROP the field exactly like str/None junk."""
    p = tmp_path / "alive.json"
    # Raw text: these literals have no JSON.dumps round-trip via allow_nan
    # defaults worth relying on — write them verbatim.
    p.write_text(
        '{"last_tick_epoch": NaN, "last_output_epoch": Infinity,'
        ' "dropped_alerts_total": -Infinity, "extra": "kept"}',
        encoding="utf-8")
    assert state.load_alive(p) == {"extra": "kept"}


def test_alive_finite_float_still_coerces_after_nan_guard(tmp_path):
    """S1 guard-rail: the non-finite rejection must not swallow legitimate
    finite float stamps (json may carry 1755.9) — they still coerce."""
    p = tmp_path / "alive.json"
    p.write_text(json.dumps({"last_tick_epoch": 1755.9,
                             "dropped_alerts_total": 3.0}),
                 encoding="utf-8")
    assert state.load_alive(p) == {"last_tick_epoch": 1755,
                                   "dropped_alerts_total": 3}



# ---------- lockfile ----------

def test_lock_acquire_and_release(tmp_path):
    lock = tmp_path / "monitor.lock"
    assert state.acquire_lock(lock) is True
    state.release_lock(lock)
    assert not lock.exists()


def test_lock_contention_fails_fast(tmp_path):
    lock = tmp_path / "monitor.lock"
    assert state.acquire_lock(lock) is True
    assert state.acquire_lock(lock) is False  # live lock: never waits
    state.release_lock(lock)


def test_lock_stale_broken(tmp_path):
    lock = tmp_path / "monitor.lock"
    lock.write_text("999999", encoding="utf-8")
    old = time.time() - 31 * 60
    os.utime(lock, (old, old))
    assert state.acquire_lock(lock) is True  # stale (>30 min) gets broken
    state.release_lock(lock)


# ---------- F1: O_EXCL acquisition — no TOCTOU dual holders ----------

def test_lock_live_contention_leaves_file_byte_and_mtime_identical(tmp_path):
    """F1(a): a live lock must make the second acquire return False WITHOUT
    touching the file — no pid overwrite, no mtime bump (O_EXCL open fails
    before any write)."""
    lock = tmp_path / "monitor.lock"
    assert state.acquire_lock(lock) is True
    bytes_before = lock.read_bytes()
    mtime_before = os.stat(lock).st_mtime_ns
    assert state.acquire_lock(lock) is False
    assert lock.read_bytes() == bytes_before == str(os.getpid()).encode()
    assert os.stat(lock).st_mtime_ns == mtime_before


def test_lock_stale_takeover_writes_own_pid(tmp_path):
    """F1(b): serialized stale-break still succeeds and stamps OUR pid."""
    lock = tmp_path / "monitor.lock"
    lock.write_text("999999", encoding="utf-8")
    old = time.time() - 31 * 60
    os.utime(lock, (old, old))
    assert state.acquire_lock(lock) is True
    assert lock.read_text(encoding="utf-8") == str(os.getpid())
    state.release_lock(lock)


def test_lock_race_stale_break_single_winner(tmp_path, monkeypatch):
    """F1(c): two acquirers race the stale break — exactly one may win.

    Ordering 1: rival completes its WHOLE acquire inside our unlink window,
    so it recreates the file first; we must lose on the retry (its file is
    fresh) instead of stamping over it. The old exists()/unlink/write code
    made BOTH acquirers return True here."""
    lock = tmp_path / "monitor.lock"
    lock.write_text("888888", encoding="utf-8")
    old = time.time() - 31 * 60
    os.utime(lock, (old, old))

    results = {}
    real_unlink = os.unlink
    fired = []

    def rival_full_acquire():
        results["rival"] = state.acquire_lock(lock)

    def hooked(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(lock) and not fired:
            fired.append(True)
            rival_full_acquire()
        return result

    monkeypatch.setattr(os, "unlink", hooked)
    ours = state.acquire_lock(lock)
    monkeypatch.undo()

    assert results.get("rival") is True
    assert ours is False                      # loser stands down
    assert sum(1 for v in results.values() if v) + (1 if ours else 0) == 1
    assert lock.read_text(encoding="utf-8") != "888888"   # stale pid gone
    state.release_lock(lock)


def test_lock_race_rival_dies_after_break_we_recover(tmp_path):
    """F1(c), ordering 2: a rival breaks the stale file then vanishes before
    creating its own — from our side that is exactly the plain stale-break
    path (unlink succeeded, nothing re-created the file). The exclusive
    create must succeed: crash recovery still works when the file vanishes
    between break and create."""
    lock = tmp_path / "monitor.lock"
    lock.write_text("777777", encoding="utf-8")
    old = time.time() - 31 * 60
    os.utime(lock, (old, old))
    assert state.acquire_lock(lock) is True
    assert lock.read_text(encoding="utf-8") == str(os.getpid())
    state.release_lock(lock)


def test_lock_race_rival_raw_creates_first_we_stand_down(tmp_path):
    """F1(c), ordering 3: during our stale-break window a rival raw-writes a
    FRESH lockfile (crashed mid-acquire while holding it). Our retry hits
    FileExistsError on a fresh file -> False. Never overwrite a fresh peer."""
    lock = tmp_path / "monitor.lock"
    lock.write_text("666666", encoding="utf-8")
    old = time.time() - 31 * 60
    os.utime(lock, (old, old))

    real_unlink = os.unlink
    fired = []

    def rival_raw_create():
        lock.write_text("424242", encoding="utf-8")  # fresh rival holder

    def hooked(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(lock) and not fired:
            fired.append(True)
            rival_raw_create()
        return result

    import unittest.mock as mock
    with mock.patch("os.unlink", hooked):
        ours = state.acquire_lock(lock)
    assert ours is False
    assert lock.read_text(encoding="utf-8") == "424242"   # rival untouched


# ---------- F2: unique temp name per writer ----------

def test_atomic_write_concurrent_threads_no_lost_tmp(tmp_path):
    """F2: two writers racing the SAME target must never steal each other's
    temp file (shared '<name>.tmp' caused FileNotFoundError ~27% of raced
    rounds). 2 threads x 50 writes: zero exceptions, target always valid
    JSON from exactly one writer, no *.tmp leftovers."""
    target = tmp_path / "target.json"
    errors = []

    def worker(n):
        try:
            for i in range(50):
                state._atomic_write_json(target, {"worker": n, "i": i})
        except Exception as exc:  # noqa: BLE001 — any failure is the bug
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["worker"] in (0, 1)
    assert isinstance(data["i"], int)
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_failed_dump_leaves_only_own_named_tmp(tmp_path):
    """F2: a mid-write failure may leave the WRITER'S OWN temp behind but
    nothing else — the temp name carries this process's pid so a crashed
    writer's debris can't be mistaken for another writer's."""
    target = tmp_path / "target.json"
    with pytest.raises(TypeError):
        state._atomic_write_json(target, {"bad": {1, 2}})  # set: unserializable
    leftovers = list(tmp_path.glob("*.tmp"))
    for leftover in leftovers:
        assert leftover.name.startswith("target.json.")
        assert str(os.getpid()) in leftover.name
    assert not target.exists()


# ---------- F3: cooldown stamp sanitation at persist ----------

def test_cooldowns_save_drops_nonfinite_future_bool_and_string(tmp_path):
    """F3: Infinity suppressed alerts forever, future-dated stamps suppressed
    them arbitrarily long, NaN survived every prune. Sanitation happens at
    persist: keep ONLY finite numbers (bool excluded) whose age satisfies
    0 <= now - v < ttl_s."""
    p = tmp_path / "cooldowns.json"
    now = 1_000_000_000.0
    ttl = 43_200
    data = {
        "good|fresh": now - 100,
        "good|near_ttl": now - ttl + 5,
        "bad|inf": float("inf"),
        "bad|-inf": float("-inf"),
        "bad|nan": float("nan"),
        "bad|future": now + 500,
        "bad|bool": True,
        "bad|string": "123",
    }
    state.save_cooldowns(p, data, ttl_s=ttl, now=now)
    assert state.load_cooldowns(p) == {
        "good|fresh": now - 100,
        "good|near_ttl": now - ttl + 5,
    }


def test_cooldowns_exact_ttl_boundary_dropped(tmp_path):
    """F3 boundary: age exactly ttl_s drops — matches cooldown.py's strict
    `now - last < ttl_s` suppression check."""
    p = tmp_path / "cooldowns.json"
    now = 5_000_000.0
    state.save_cooldowns(p, {"edge": now - 43_200}, ttl_s=43_200, now=now)
    assert state.load_cooldowns(p) == {}


def test_cooldowns_negative_age_stamp_dropped(tmp_path):
    """F3: a stamp dated AFTER now (clock skew / hand edit) has negative age
    and is treated as stale, not preserved forever."""
    p = tmp_path / "cooldowns.json"
    now = 5_000_000.0
    state.save_cooldowns(p, {"skewed": now + 1}, ttl_s=43_200, now=now)
    assert state.load_cooldowns(p) == {}


# ---------- sweep-2: RecursionError gate on local-disk JSON loads ----------

# Raw hostile literal: nesting so deep the stdlib parser exhausts the
# interpreter's recursion stack. Written verbatim (not via json.dumps).
DEEP_NEST_JSON = "[" * 120000 + "]" * 120000


@pytest.mark.parametrize(
    "loader_name,default",
    [
        ("load_roster", None),
        ("load_cooldowns", {}),
        ("load_pending", []),
        ("load_alive", {}),
    ],
)
def test_deep_nested_json_returns_loader_default(tmp_path, loader_name,
                                                 default):
    """Sweep #2: a deeply-nested state file makes json.load raise
    RecursionError, which was NOT in _load_json_or_default's except tuple
    (OSError, json.JSONDecodeError) — it escaped every loader into
    run_tick's fatal handler and permanently FATAL exit-2 looped the
    monitor. Round 2 fixed exactly this class for NETWORK JSON
    (providers._loads_or_fetcherror); the local-disk seam must degrade the
    same way: each public loader falls back to its DEFAULT, like any other
    unreadable/corrupt file."""
    p = tmp_path / f"{loader_name}.json"
    p.write_text(DEEP_NEST_JSON, encoding="utf-8")
    assert getattr(state, loader_name)(p) == default


def test_deep_but_under_limit_json_still_parses(tmp_path):
    """Guard-rail: the RecursionError gate must not swallow legitimate
    deep-but-parseable JSON — nesting comfortably under the interpreter's
    recursion limit still parses with structure intact."""
    p = tmp_path / "deep.json"
    depth = 300
    p.write_text("[" * depth + '{"k": 1}' + "]" * depth, encoding="utf-8")
    data = state._load_json_or_default(p, None)
    assert data is not None          # parsed, not defaulted
    for _ in range(depth):
        data = data[0]
    assert data == {"k": 1}
