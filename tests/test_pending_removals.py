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


def test_save_load_roundtrip(tmp_path):
    p = pr.path_in(tmp_path)
    held = {"amd": {"m1": {"gone_since": 100, "last_absent_seen": 150}}}
    pr.save(p, held)
    assert pr.load(p) == held
    # atomic write leaves no tmp debris
    assert not list(tmp_path.glob("*.tmp"))


def test_enqueue_creates_entry_and_merges_second_provider():
    held = {}
    pr.enqueue(held, "amd", ["x"], now=100.9)
    assert held == {"amd": {"x": {"gone_since": 100,
                                  "last_absent_seen": 100}}}
    assert isinstance(held["amd"]["x"]["gone_since"], int)  # now int-coerced
    pr.enqueue(held, "nous", ["y"], now=200)
    assert held["nous"] == {"y": {"gone_since": 200, "last_absent_seen": 200}}
    assert held["amd"] == {"x": {"gone_since": 100, "last_absent_seen": 100}}


def test_enqueue_reenqueue_keeps_original_gone_since():
    """The clock never resets: a re-confirmed absence only advances
    last_absent_seen."""
    held = {}
    pr.enqueue(held, "amd", ["x"], now=100)
    pr.enqueue(held, "amd", ["x"], now=500)
    assert held["amd"]["x"]["gone_since"] == 100
    assert held["amd"]["x"]["last_absent_seen"] == 500


def test_enqueue_empty_ids_does_not_create_slice():
    """enqueue([]) must not persist {"p": {}} dust."""
    held = {}
    pr.enqueue(held, "amd", [], now=100)
    assert held == {}


def test_settle_recovery_consumes_prunes_and_marks_changed():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": ["x", "new"]}, {"amd": 1800}, now=200)
    assert out["recovered"] == [("amd", "x")]  # untracked "new" is not held
    assert out["expired"] == {}
    assert out["released"] == []
    assert out["changed"] == {"amd"}
    assert held == {}  # emptied slice pruned — no {"amd": {}} dust


def test_settle_recovered_sorted_by_provider_then_id():
    held = {"zeta": {"b": {"gone_since": 100, "last_absent_seen": 100}},
            "alpha": {"a": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"zeta": ["b"], "alpha": ["a"]},
                    {"zeta": 1800, "alpha": 1800}, now=200)
    assert out["recovered"] == [("alpha", "a"), ("zeta", "b")]


def test_settle_still_absent_holds_without_extending_clock():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=200)
    assert out["recovered"] == []
    assert out["expired"] == {}
    assert out["released"] == []
    assert out["changed"] == set()  # consumption-only contract, refresh side
    entry = held["amd"]["x"]
    assert entry["last_absent_seen"] == 200
    assert entry["gone_since"] == 100  # immutability pinned


def test_settle_expired_consumes_entry():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=2000)
    assert out["expired"] == {"amd": ["x"]}
    assert out["recovered"] == []
    assert out["released"] == []
    assert out["changed"] == {"amd"}
    assert held == {}  # consumed slice emptied and pruned — no dust


def test_settle_expiry_boundary_exactly_at_hold():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=1900)
    assert out["expired"] == {"amd": ["x"]}  # now - gone_since >= hold


def test_settle_expired_ids_sorted():
    held = {"amd": {"b": {"gone_since": 100, "last_absent_seen": 100},
                    "a": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=2000)
    assert out["expired"] == {"amd": ["a", "b"]}


def test_settle_absent_provider_key_is_neutral():
    """Provider ABSENT from fetches (fetch failure) -> entry untouched, both
    stamps preserved, zero outputs for it — no expiry even though the hold
    elapsed, no last_absent_seen rewrite. The clock does not move on failure.
    (A None VALUE is a caller bug; settle may assume list-or-absent.)"""
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 150}},
            "nous": {"y": {"gone_since": 50, "last_absent_seen": 50}}}
    out = pr.settle(held, {"nous": ["y"]}, {"amd": 1800, "nous": 1800},
                    now=5000)
    assert held["amd"] == {"x": {"gone_since": 100, "last_absent_seen": 150}}
    assert out["recovered"] == [("nous", "y")]
    assert out["expired"] == {}
    assert out["released"] == []
    assert out["changed"] == {"nous"}
