"""Tests for pending_removals.py — removal hold queue: settle semantics +
validated state I/O. Failure is signaled by KEY ABSENCE (never None); load
validation mirrors state.load_alive: numeric non-bool fields survive with
int() coercion, string digits and everything else are DROPPED — no
"int-coercible strings" leniency.
"""

import json

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


def test_load_missing_file_is_empty_dict(tmp_path):
    assert pr.load(tmp_path / "nope.json") == {}


def test_load_garbage_inputs_degrade_to_empty(tmp_path):
    """Top-level non-dict payloads and unparseable JSON degrade to {} —
    settle's input is always a dict, resolve/tick can never crash on it."""
    p = tmp_path / "pending_removals.json"
    for junk in (["nonsense"], "a string", 42, None):
        p.write_text(json.dumps(junk))
        assert pr.load(p) == {}
    p.write_text("{not json at all")
    assert pr.load(p) == {}


def test_load_junk_non_dict_slice_dropped(tmp_path):
    """{"amd": "x"} — non-dict provider slice is junk."""
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": "x",
                             "nous": {"m": {"gone_since": 100,
                                            "last_absent_seen": 100}}}))
    out = pr.load(p)
    assert "amd" not in out          # junk slice dropped
    assert "nous" in out             # valid sibling survives


def test_load_non_dict_entry_dropped(tmp_path):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"bad": 7,
                                     "good": {"gone_since": 100,
                                              "last_absent_seen": 100}}}))
    assert pr.load(p) == {"amd": {"good": {"gone_since": 100,
                                           "last_absent_seen": 100}}}
