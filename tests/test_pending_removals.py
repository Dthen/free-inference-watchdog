"""Tests for pending_removals.py — removal hold queue: settle semantics +
validated state I/O. Failure is signaled by KEY ABSENCE (never None);
load validation mirrors state.load_alive: numeric non-bool fields survive
with int() coercion, string digits and everything else are DROPPED —
no "int-coercible strings" leniency.
"""

import copy
import json

import pending_removals as pr


# ---------- path_in ----------

def test_path_in_joins_state_dir(tmp_path):
    p = pr.path_in(tmp_path)
    assert p == tmp_path / "pending_removals.json"  # full path pinned


# ---------- load / save ----------

def test_load_missing_file_is_empty_dict(tmp_path):
    assert pr.load(tmp_path / "nope.json") == {}


def test_save_load_roundtrip(tmp_path):
    p = pr.path_in(tmp_path)
    held = {"amd": {"m1": {"gone_since": 100, "last_absent_seen": 150}}}
    pr.save(p, held)
    assert pr.load(p) == held
    # atomic write leaves no tmp debris
    assert not list(tmp_path.glob("*.tmp"))


def test_load_junk_top_level_is_empty_dict(tmp_path):
    p = tmp_path / "pending_removals.json"
    for junk in (["nonsense"], "a string", 42, None):
        p.write_text(json.dumps(junk))
        assert pr.load(p) == {}


def test_load_corrupt_json_is_empty_dict(tmp_path):
    p = tmp_path / "pending_removals.json"
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


def test_load_string_stamp_dropped_not_coerced(tmp_path):
    """load_alive semantics: digit-strings are DROPPED, never int()-coerced."""
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": "100"}}}))
    out = pr.load(p)
    assert out == {"amd": {}}  # junk entry dropped (load keeps the empty slice)


def test_load_bool_stamp_dropped(tmp_path):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": True,
                                           "last_absent_seen": 100}}}))
    out = pr.load(p)
    assert out == {"amd": {}}


def test_load_null_stamp_dropped(tmp_path):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": None,
                                           "last_absent_seen": 100}}}))
    out = pr.load(p)
    assert out == {"amd": {}}


def test_load_nonfinite_literals_dropped(tmp_path):
    """json.load happily parses NaN/Infinity — same junk class as load_alive
    (int(nan)/int(inf) would FATAL the resolve loop)."""
    p = tmp_path / "pending_removals.json"
    p.write_text('{"amd": {"m": {"gone_since": Infinity, '
                 '"last_absent_seen": 100}}}')
    assert pr.load(p) == {"amd": {}}
    p.write_text('{"amd": {"m": {"gone_since": NaN, '
                 '"last_absent_seen": 100}}}')
    assert pr.load(p) == {"amd": {}}


def test_load_both_numeric_stamps_survive(tmp_path):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": 100,
                                           "last_absent_seen": 100}}}))
    assert pr.load(p) == {"amd": {"m": {"gone_since": 100,
                                        "last_absent_seen": 100}}}


def test_load_numeric_float_coerced_to_int(tmp_path):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": 100.7,
                                           "last_absent_seen": 150.2}}}))
    out = pr.load(p)
    assert out == {"amd": {"m": {"gone_since": 100,
                                 "last_absent_seen": 150}}}
    assert isinstance(out["amd"]["m"]["gone_since"], int)


def test_load_non_dict_entry_dropped(tmp_path):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"bad": 7,
                                     "good": {"gone_since": 100,
                                              "last_absent_seen": 100}}}))
    assert pr.load(p) == {"amd": {"good": {"gone_since": 100,
                                           "last_absent_seen": 100}}}


def test_load_valid_siblings_survive_alongside_junk(tmp_path):
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


# ---------- load drop note (stderr) ----------

def test_load_drops_note_with_count_and_plural(tmp_path, capsys):
    """Amendment: drops are a visible skip, never silent — but healthy
    runs stay silent (note only when N>0)."""
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


def test_load_healthy_run_is_silent(tmp_path, capsys):
    p = tmp_path / "pending_removals.json"
    p.write_text(json.dumps({"amd": {"m": {"gone_since": 100,
                                           "last_absent_seen": 100}}}))
    pr.load(p)
    assert capsys.readouterr().err == ""


def test_load_missing_file_is_silent(tmp_path, capsys):
    pr.load(tmp_path / "nope.json")
    assert capsys.readouterr().err == ""


# ---------- sorted_ids ----------

def test_sorted_ids_sorted_deduped_str_coerced():
    assert pr.sorted_ids([3, 1, "1", 2]) == ["1", "2", "3"]
    assert pr.sorted_ids([]) == []


# ---------- enqueue ----------

def test_enqueue_creates_entry():
    held = {}
    pr.enqueue(held, "amd", ["x"], now=100)
    assert held == {"amd": {"x": {"gone_since": 100,
                                  "last_absent_seen": 100}}}


def test_enqueue_reenqueue_keeps_original_gone_since():
    held = {}
    pr.enqueue(held, "amd", ["x"], now=100)
    pr.enqueue(held, "amd", ["x"], now=500)
    assert held["amd"]["x"]["gone_since"] == 100
    assert held["amd"]["x"]["last_absent_seen"] == 500


def test_enqueue_second_provider_merges():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    pr.enqueue(held, "nous", ["y"], now=200)
    assert held["nous"] == {"y": {"gone_since": 200,
                                  "last_absent_seen": 200}}
    assert held["amd"] == {"x": {"gone_since": 100, "last_absent_seen": 100}}


def test_enqueue_coerces_now_to_int():
    held = {}
    pr.enqueue(held, "amd", ["x"], now=100.9)
    assert held["amd"]["x"]["gone_since"] == 100
    assert isinstance(held["amd"]["x"]["gone_since"], int)


def test_enqueue_empty_ids_does_not_create_slice():
    """Amendment: enqueue([]) must not persist {"p": {}} dust."""
    held = {}
    pr.enqueue(held, "amd", [], now=100)
    assert held == {}


# ---------- pending_ids ----------

def test_pending_ids_empty_when_absent():
    assert pr.pending_ids({}, "amd") == []
    assert pr.pending_ids({"nous": {}}, "amd") == []


def test_pending_ids_sorted():
    held = {"amd": {"b": {}, "a": {}, "c": {}}}
    assert pr.pending_ids(held, "amd") == ["a", "b", "c"]


# ---------- settle: recovery ----------

def test_settle_recovery():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": ["x", "new"]}, {"amd": 1800}, now=200)
    assert out["recovered"] == [("amd", "x")]
    assert out["expired"] == {}
    assert out["released"] == []
    assert out["changed"] == {"amd"}
    assert held == {}  # amendment: emptied slice is pruned, no {"amd": {}} dust


def test_settle_recovery_sorted_by_provider_then_id():
    held = {"zeta": {"b": {"gone_since": 100, "last_absent_seen": 100}},
            "alpha": {"a": {"gone_since": 100, "last_absent_seen": 100}}}
    fetches = {"zeta": ["b"], "alpha": ["a"]}
    out = pr.settle(held, fetches, {"zeta": 1800, "alpha": 1800}, now=200)
    assert out["recovered"] == [("alpha", "a"), ("zeta", "b")]


# ---------- settle: still-absent holds ----------

def test_settle_still_absent_holds_without_extending_clock():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=200)
    assert out["recovered"] == []
    assert out["expired"] == {}
    assert out["released"] == []
    assert out["changed"] == set()
    entry = held["amd"]["x"]
    assert entry["last_absent_seen"] == 200
    assert entry["gone_since"] == 100  # immutability pinned


# ---------- settle: expiry ----------

def test_settle_expired_consumes_entry():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=2000)
    assert out["expired"] == {"amd": ["x"]}
    assert out["recovered"] == []
    assert out["released"] == []
    assert out["changed"] == {"amd"}
    assert held == {}  # amendment: consumed emptied slice pruned, no dust


def test_settle_expiry_boundary_exactly_at_hold():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=1900)
    assert out["expired"] == {"amd": ["x"]}  # now - gone_since >= hold


def test_settle_expired_ids_sorted():
    held = {"amd": {"b": {"gone_since": 100, "last_absent_seen": 100},
                    "a": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=2000)
    assert out["expired"] == {"amd": ["a", "b"]}


# ---------- settle: fetch failure is neutral ----------

def test_settle_absent_provider_key_is_neutral():
    """Provider ABSENT from fetches (fetch failure) -> entry untouched,
    both stamps preserved, zero outputs for it. (Passing None as a value
    is a caller bug; settle may assume list-or-absent.)"""
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 150}},
            "nous": {"y": {"gone_since": 50, "last_absent_seen": 50}}}
    out = pr.settle(held, {"nous": ["y"]}, {"amd": 1800, "nous": 1800},
                    now=5000)
    # amd stayed exactly as it was — no expiry even though hold elapsed,
    # no last_absent_seen rewrite. The clock does not move on failure.
    assert held["amd"] == {"x": {"gone_since": 100, "last_absent_seen": 150}}
    assert out["recovered"] == [("nous", "y")]
    assert out["expired"] == {}
    assert out["released"] == []
    assert out["changed"] == {"nous"}


# ---------- settle: partial three-way ----------

def test_settle_partial_resolution_that_empties_slice_prunes_it():
    """Amendment: partial path (recovered+expired, nothing held back) that
    empties the slice must `del held[provider]` — no {"p": {}} dust."""
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100},
                    "y": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": ["x"]}, {"amd": 1800}, now=2000)
    assert out["recovered"] == [("amd", "x")]
    assert out["expired"] == {"amd": ["y"]}
    assert held == {}


def test_settle_partial_recovery_expiry_and_hold():
    held = {"amd": {
        "x": {"gone_since": 100, "last_absent_seen": 100},
        "y": {"gone_since": 100, "last_absent_seen": 100},
        "z": {"gone_since": 1900, "last_absent_seen": 1900},
    }}
    out = pr.settle(held, {"amd": ["x"]}, {"amd": 1800}, now=2000)
    assert out["recovered"] == [("amd", "x")]
    assert out["expired"] == {"amd": ["y"]}
    assert out["released"] == []
    assert out["changed"] == {"amd"}
    assert held == {"amd": {"z": {"gone_since": 1900,
                                  "last_absent_seen": 2000}}}


# ---------- settle: un-flagged release ----------

def test_settle_unflagged_provider_releases_silently():
    """holds.get(p) is None (un-flagged or unknown provider): every entry
    consumed under 'released' — NOT recovered/expired; changed includes p."""
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100},
                    "y": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {}, now=200)
    assert out["released"] == [("amd", "x"), ("amd", "y")]
    assert out["recovered"] == []
    assert out["expired"] == {}
    assert out["changed"] == {"amd"}
    assert held == {}  # amendment: release-all prunes the slice entirely


def test_settle_holds_none_value_releases():
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {"amd": []}, {"amd": None}, now=200)
    assert out["released"] == [("amd", "x")]
    assert out["expired"] == {}
    assert out["changed"] == {"amd"}
    assert held == {}  # amendment: emptied slice pruned


def test_settle_unflagged_release_even_when_fetch_absent():
    """Release only applies to providers the caller resolved (key present in
    fetches). Absent key stays neutral even when un-flagged."""
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    out = pr.settle(held, {}, {}, now=999999)
    assert out["released"] == []
    assert held == {"amd": {"x": {"gone_since": 100,
                                  "last_absent_seen": 100}}}


# ---------- with_expired ----------

def test_with_expired_remerges_with_original_stamps():
    snapshot = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 150}}}
    held = copy.deepcopy(snapshot)
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=2000)
    assert out["expired"] == {"amd": ["x"]}
    assert held == {}  # amendment: emptied slice pruned
    merged = pr.with_expired(held, out["expired"], snapshot)
    assert merged == {"amd": {"x": {"gone_since": 100,
                                    "last_absent_seen": 150}}}


def test_with_expired_empty_expired_is_equal_copy(tmp_path):
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    merged = pr.with_expired(held, {}, {})
    assert merged == held
    assert merged is not held


def test_with_expired_never_mutates_args():
    """Pin the pure contract (T4 map discipline): mutating the returned dict
    must leave held, expired, and snapshot untouched."""
    snapshot = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 150}}}
    held = copy.deepcopy(snapshot)
    expired = {"amd": ["x"]}
    held_before = copy.deepcopy(held)
    expired_before = copy.deepcopy(expired)
    snapshot_before = copy.deepcopy(snapshot)
    merged = pr.with_expired(held, expired, snapshot)
    # smash the result
    merged.clear()
    merged["ghost"] = {"boo": {"gone_since": 0, "last_absent_seen": 0}}
    assert held == held_before
    assert expired == expired_before
    assert snapshot == snapshot_before


def test_with_expired_result_is_deep_copy():
    """Later mutation of held must not leak into an earlier merged dict."""
    held = {"nous": {"y": {"gone_since": 100, "last_absent_seen": 100}}}
    merged = pr.with_expired(held, {}, {})
    held["nous"]["y"]["gone_since"] = 999
    assert merged["nous"]["y"]["gone_since"] == 100


def test_with_expired_accepts_none_for_both_maps():
    """Amendment: one clear rule — None tolerated on expired AND stamps_from
    (previously `stamps_from=None` raised AttributeError)."""
    held = {"amd": {"x": {"gone_since": 100, "last_absent_seen": 100}}}
    assert pr.with_expired(held, None, {"amd": {}}) == held
    assert pr.with_expired(held, {}, None) == held
    assert pr.with_expired(held, None, None) == held
    merged = pr.with_expired(held, {"amd": ["x"]}, None)  # stamps_from None
    assert merged == held  # no stamps -> nothing re-merged


def test_with_expired_missing_stamp_skips_with_stderr_note(capsys):
    """Amendment: an expired id with NO stamp entry in stamps_from is NOT
    re-merged (no invented stamps) and prints a visible skip note."""
    held = {}
    merged = pr.with_expired(held, {"amd": ["x"]}, {"amd": {}})
    assert merged == {}  # x not re-queued
    err = capsys.readouterr().err
    assert (err == "pending_removals: no pre-settle stamp for amd/x — "
                   "expired entry not re-queued\n")


def test_with_expired_missing_provider_slice_also_notes(capsys):
    merged = pr.with_expired({}, {"nous": ["m"]}, {"amd": {}})
    assert merged == {}
    err = capsys.readouterr().err
    assert "no pre-settle stamp for nous/m" in err
    # an id that DOES have a stamp is re-merged silently
    capsys.readouterr()
    merged = pr.with_expired({}, {"amd": ["x"]},
                             {"amd": {"x": {"gone_since": 100,
                                           "last_absent_seen": 100}}})
    assert merged == {"amd": {"x": {"gone_since": 100,
                                    "last_absent_seen": 100}}}
    assert capsys.readouterr().err == ""


def test_with_expired_full_map_discipline_roundtrip(tmp_path):
    """The usage pattern T4 pins: snapshot -> deepcopy -> settle consumes ->
    pre-emit save(with_expired(...)) -> post-emit save(held)."""
    p = pr.path_in(tmp_path)
    snapshot = pr.load(p)  # missing file -> {}
    assert snapshot == {}
    pr.enqueue(snapshot, "amd", ["x"], now=100)
    pr.save(p, snapshot)
    snapshot = pr.load(p)
    held = copy.deepcopy(snapshot)
    out = pr.settle(held, {"amd": []}, {"amd": 1800}, now=2000)
    pr.save(p, pr.with_expired(held, out["expired"], snapshot))
    assert pr.load(p) == {"amd": {"x": {"gone_since": 100,
                                        "last_absent_seen": 100}}}
    pr.save(p, held)
    assert pr.load(p) == {}  # amendment: settle pruned the emptied slice
