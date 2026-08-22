"""Tests for diffing.py — set-diff engine, sticky carry-forward, bootstrap."""

import diffing


# ---------- compute_events ----------

def test_first_run_no_events():
    events, first = diffing.compute_events(None, {"nous": ["a"]})
    assert first is True
    assert events == {}


def test_no_change_no_events():
    events, first = diffing.compute_events(
        {"providers": {"nous": ["a", "b"]}}, {"nous": ["b", "a"]})
    assert first is False
    assert events == {}


def test_added_removed_mixed():
    prev = {"providers": {"nous": ["a", "b", "c"]}}
    events, _ = diffing.compute_events(prev, {"nous": ["b", "c", "d", "e"]})
    assert events == {"nous": {"added": ["d", "e"], "removed": ["a"]}}


def test_new_provider_in_fetched_counts_as_all_added():
    prev = {"providers": {"nous": ["a"]}}
    events, _ = diffing.compute_events(prev, {"nous": ["a"], "kilo": ["x"]})
    assert events["kilo"] == {"added": ["x"], "removed": []}


def test_provider_gone_from_fetched_is_not_a_removal():
    # A provider missing from `fetched` means the tick never ran it
    # (registry change) — sticky handled upstream; never a removal event.
    prev = {"providers": {"nous": ["a"], "zen": ["z1"]}}
    events, _ = diffing.compute_events(prev, {"nous": ["a"]})
    assert events == {}


# ---------- apply_sticky ----------

def test_failed_fetch_carries_forward_prev_ids():
    new_map, stale = diffing.apply_sticky(
        {"nous": ["a", "b"], "zen": ["z"]}, {"nous": None, "zen": ["z2"]})
    assert new_map == {"nous": ["a", "b"], "zen": ["z2"]}
    assert stale == ["nous"]


def test_failed_fetch_with_no_prev_starts_empty():
    new_map, stale = diffing.apply_sticky(None, {"nous": None})
    assert new_map == {"nous": []}
    assert stale == ["nous"]


def test_successful_fetch_replaces():
    new_map, stale = diffing.apply_sticky(
        {"nous": ["old"]}, {"nous": ["new1", "new2"]})
    assert new_map == {"nous": ["new1", "new2"]}
    assert stale == []


def test_sticky_failure_emits_zero_events():
    prev = {"providers": {"nous": ["a", "b"]}}
    new_map, _ = diffing.apply_sticky(prev["providers"], {"nous": None})
    events, _ = diffing.compute_events(prev, new_map)
    assert events == {}


# ---------- registry filtering (no zombies) ----------

def test_load_filtered_roster_drops_evicted_providers(tmp_path):
    import json
    p = tmp_path / "roster.json"
    p.write_text(json.dumps(
        {"providers": {"nous": ["a"], "zen": ["zombie"]},
         "stale_providers": ["zen"]}), encoding="utf-8")
    roster = diffing.load_filtered_roster(p, {"nous", "kilo", "cline"})
    assert roster["providers"] == {"nous": ["a"]}


def test_load_filtered_roster_missing_returns_none(tmp_path):
    assert diffing.load_filtered_roster(tmp_path / "nope.json", {"nous"}) is None


def test_compute_events_ignores_zombie_provider_in_prev():
    prev = {"providers": {"nous": ["a"], "zen": ["zombie"]}}
    events, _ = diffing.compute_events(prev, {"nous": ["a"]}, registry={"nous"})
    assert events == {}
