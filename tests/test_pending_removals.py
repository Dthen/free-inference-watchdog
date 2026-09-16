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


def test_load_string_stamp_dropped_not_coerced(tmp_path):
    """load_alive semantics: digit-strings are DROPPED, never int()-coerced
    (the machine-written file has no string stamps)."""
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": "100",
                                           "last_absent_seen": 100}}}))
    out = pr.load(p)
    assert out == {"amd": {}}  # entry dropped; load keeps the (empty) slice


def test_load_bool_or_null_stamp_dropped(tmp_path):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": True,
                                           "last_absent_seen": 100}}}))
    assert pr.load(p) == {"amd": {}}
    p.write_text(json.dumps({"amd": {"m": {"gone_since": None,
                                           "last_absent_seen": 100}}}))
    assert pr.load(p) == {"amd": {}}


def test_load_nonfinite_literals_dropped(tmp_path):
    """json.load parses NaN/Infinity — int(nan)/int(inf) would FATAL the
    resolve loop, so the literals are junk like any other."""
    p = tmp_path / "pending_removals.json"
    p.write_text('{"amd": {"m": {"gone_since": Infinity, '
                 '"last_absent_seen": 100}}}')
    assert pr.load(p) == {"amd": {}}
    p.write_text('{"amd": {"m": {"gone_since": NaN, '
                 '"last_absent_seen": 100}}}')
    assert pr.load(p) == {"amd": {}}


def test_load_numeric_stamps_survive_and_floats_coerce_to_int(tmp_path):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": 100,
                                           "last_absent_seen": 100}}}))
    assert pr.load(p) == {"amd": {"m": {"gone_since": 100,
                                        "last_absent_seen": 100}}}
    p.write_text(json.dumps({"amd": {"m": {"gone_since": 100.7,
                                           "last_absent_seen": 150.2}}}))
    out = pr.load(p)
    assert out == {"amd": {"m": {"gone_since": 100, "last_absent_seen": 150}}}
    assert isinstance(out["amd"]["m"]["gone_since"], int)


def test_load_drops_note_with_count_and_plural(tmp_path, capsys):
    """Drops are a visible skip, never silent — but only when N>0."""
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": "100"}}}))
    assert pr.load(p) == {"amd": {}}
    assert (capsys.readouterr().err
            == f"pending_removals: dropped 1 junk entry from {p}\n")
    p.write_text(json.dumps({"amd": {"m": {"gone_since": "100"},
                                      "n": 7}}))
    assert pr.load(p) == {"amd": {}}
    assert (capsys.readouterr().err
            == f"pending_removals: dropped 2 junk entries from {p}\n")


def test_load_junk_top_level_notes_visible_skip_singular(tmp_path, capsys):
    """Whole payload a JSON list -> load returns {} AND a singular
    'dropped 1 junk entry' note — top-level junk counts as ONE dropped
    entry; wording pinned exactly."""
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps(["junk"]))
    assert pr.load(p) == {}
    assert (capsys.readouterr().err
            == f"pending_removals: dropped 1 junk entry from {p}\n")


def test_load_healthy_and_missing_are_silent(tmp_path, capsys):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": 100,
                                           "last_absent_seen": 100}}}))
    pr.load(p)
    assert capsys.readouterr().err == ""
    pr.load(tmp_path / "nope.json")
    assert capsys.readouterr().err == ""


def test_load_valid_siblings_survive_alongside_junk(tmp_path):
    """The mixed-shape matrix in one file: every junk class dropped, the
    one valid entry survives untouched."""
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({
        "amd": "junk-slice",
        "nous": {"bad-str": {"gone_since": "100",
                             "last_absent_seen": 100},
                 "bad-bool": {"gone_since": False,
                              "last_absent_seen": 100},
                 "not-dict": 7,
                 "good": {"gone_since": 100, "last_absent_seen": 100}},
    }))
    out = pr.load(p)
    assert out == {"nous": {"good": {"gone_since": 100,
                                     "last_absent_seen": 100}}}
