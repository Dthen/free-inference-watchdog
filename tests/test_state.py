"""Tests for state.py — atomic persistence that can't corrupt state."""

import json
import os
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
    raw arithmetic in alive.py and inference_monitor.py — a hand-edited
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
