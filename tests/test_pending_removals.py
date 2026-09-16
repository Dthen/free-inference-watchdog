"""Tests for pending_removals.py — removal hold queue: settle semantics +
validated state I/O. Failure is signaled by KEY ABSENCE (never None); load
validation mirrors state.load_alive: numeric non-bool fields survive with
int() coercion, string digits and everything else are DROPPED — no
"int-coercible strings" leniency.
"""

import pending_removals as pr


def test_path_in_joins_state_dir(tmp_path):
    p = pr.path_in(tmp_path)
    assert p == tmp_path / "pending_removals.json"  # full path pinned


def test_sorted_ids_sorted_deduped_str_coerced():
    assert pr.sorted_ids([3, 1, "1", 2]) == ["1", "2", "3"]
    assert pr.sorted_ids([]) == []


def test_pending_ids_empty_when_absent():
    assert pr.pending_ids({}, "amd") == []
    assert pr.pending_ids({"nous": {}}, "amd") == []


def test_pending_ids_sorted():
    held = {"amd": {"b": {}, "a": {}, "c": {}}}
    assert pr.pending_ids(held, "amd") == ["a", "b", "c"]
