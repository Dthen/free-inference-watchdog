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


def test_load_filtered_roster_non_dict_providers_returns_none(tmp_path):
    """F4: JSON-valid but structurally-empty rosters must bootstrap clean —
    loading them as an existing baseline emits the universe as 'added'."""
    import json
    for body in ('{}', 'null', '{"tick_epoch": 1}',
                 '{"providers": ["nous"]}', '{"providers": null}'):
        p = tmp_path / "roster.json"
        p.write_text(body, encoding="utf-8")
        assert diffing.load_filtered_roster(p, {"nous"}) is None, body


def test_load_filtered_roster_coerces_non_list_values_to_empty(tmp_path):
    """Fix-round-4 #2: hand-edited rosters are plausible input (F4/F8f class).
    A non-list provider VALUE (e.g. string) must load as [] at this boundary —
    downstream confirm_diffs/merge_corrected call _sorted_list() unguarded, so
    the string 'model-a' would char-split into bogus single-char 🔴 removals."""
    import json
    p = tmp_path / "roster.json"
    p.write_text(json.dumps(
        {"providers": {"nous": "model-a", "kilo": {"oops": 1},
                       "cline": ["real/id"]}}), encoding="utf-8")
    roster = diffing.load_filtered_roster(p, {"nous", "kilo", "cline"})
    assert roster is not None
    assert roster["providers"] == {"nous": [], "kilo": [], "cline": ["real/id"]}


def test_load_filtered_roster_coerces_non_string_list_elements(tmp_path):
    """Fix-round-5 #2 (sweep-4 P5): coercion is closed on BOTH sides — a list
    value whose ELEMENTS are non-strings (whole API model objects pasted in is
    exactly the plausible-mistake class) must not flow to _sorted_list, where
    str() would format them into bogus ids. String elements survive."""
    import json
    p = tmp_path / "roster.json"
    p.write_text(json.dumps(
        {"providers": {"nous": [{"id": "x-model"}],
                       "kilo": [42, None, "ok/id"],
                       "cline": ["real/id"]}}), encoding="utf-8")
    roster = diffing.load_filtered_roster(p, {"nous", "kilo", "cline"})
    assert roster is not None
    assert roster["providers"] == {"nous": [], "kilo": ["ok/id"],
                                   "cline": ["real/id"]}


def test_dict_element_prev_value_no_bogus_repr_alert_downstream(tmp_path):
    """Fix-round-5 P5 end-to-end shape: a hand-edited prev value containing a
    whole model object must ship NO 🔴 repr bullet like "{'id': 'x-model'}"
    and no phantom 🟢/🔴 pair — boundary-coerced [] makes the next tick a
    truthful added-only candidate whose confirm recheck removes nothing."""
    import json
    p = tmp_path / "roster.json"
    p.write_text('{"providers": {"nous": [{"id": "x-model"}]}}', encoding="utf-8")
    roster = diffing.load_filtered_roster(p, {"nous"})
    assert roster is not None
    assert roster["providers"]["nous"] == []
    events, _first_run = diffing.compute_events(roster, {"nous": ["x-model"]})
    assert events["nous"] == {"added": ["x-model"], "removed": []}

    def fetch_one(name):
        return ["x-model"], {}

    conf = diffing.confirm_diffs(events, roster["providers"],
                                 fetch_one=fetch_one,
                                 sleep=lambda s: None, delay=0)
    assert conf["confirmed"]["nous"]["removed"] == []


def test_string_prev_value_no_bogus_removal_alerts_downstream(tmp_path):
    """Fix-round-4 #2 end-to-end shape: a seeded string prev value, once
    boundary-coerced to [], produces an added-only candidate and the confirm
    recheck confirms ZERO removal ids — no char-split 🔴 bullets ship."""
    import json
    p = tmp_path / "roster.json"
    p.write_text('{"providers": {"nous": "model-a"}}', encoding="utf-8")
    roster = diffing.load_filtered_roster(p, {"nous"})
    assert roster is not None
    events, _first_run = diffing.compute_events(roster, {"nous": ["model-a"]})
    assert events["nous"]["removed"] == []

    def fetch_one(name):
        return ["model-a"], {}

    conf = diffing.confirm_diffs(events, roster["providers"],
                                 fetch_one=fetch_one,
                                 sleep=lambda s: None, delay=0)
    assert conf["confirmed"]["nous"]["removed"] == []


def test_compute_events_ignores_zombie_provider_in_prev():
    prev = {"providers": {"nous": ["a"], "zen": ["zombie"]}}
    events, _ = diffing.compute_events(prev, {"nous": ["a"]}, registry={"nous"})
    assert events == {}
