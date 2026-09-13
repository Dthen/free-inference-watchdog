"""Tests for probe_select.py — pure probe-queue selection.

Order contract (operator-corrected, do NOT reorder):
  1. new arrivals (no entry / junk verdict) — sorted by id
  2. free verdicts (every tick) — sorted by id
  3. stale paid (epoch None or now - epoch >= stale_hours*3600) — oldest first:
     None epochs first, then ascending epoch, then id tiebreak
Excluded: paid probed within the stale window (negative ages count as recent).
"""

import probe_select
import pytest

NOW = 1_789_259_000
DAY = 24 * 3600


def entry(verdict, epoch=NOW):
    return {"verdict": verdict, "epoch": epoch}


# ---------- tier ordering ----------

def test_new_arrivals_before_free_before_stale_paid():
    catalog = ["paid-old", "free-a", "new-a"]
    state = {
        "free-a": entry("free"),
        "paid-old": entry("paid", NOW - 2 * DAY),
    }
    q = probe_select.select_queue(catalog, state, NOW)
    assert q == ["new-a", "free-a", "paid-old"]


def test_tier1_and_tier2_sorted_by_id():
    catalog = ["z-new", "a-new", "z-free", "a-free"]
    state = {"z-free": entry("free"), "a-free": entry("free")}
    q = probe_select.select_queue(catalog, state, NOW)
    assert q == ["a-new", "z-new", "a-free", "z-free"]


def test_multiple_free_all_included_every_tick():
    catalog = ["f1", "f2", "f3"]
    state = {"f1": entry("free"), "f2": entry("free"), "f3": entry("free")}
    q = probe_select.select_queue(catalog, state, NOW)
    assert q == ["f1", "f2", "f3"]


# ---------- stale window ----------

def test_paid_within_stale_hours_excluded():
    state = {"p": entry("paid", NOW - DAY + 1)}
    q = probe_select.select_queue(["p"], state, NOW)
    assert q == []


def test_paid_older_than_stale_hours_included():
    state = {"p": entry("paid", NOW - DAY - 1)}
    q = probe_select.select_queue(["p"], state, NOW)
    assert q == ["p"]


def test_paid_exactly_at_stale_boundary_included():
    # boundary is >= : now - epoch == stale_hours*3600 is stale
    state = {"p": entry("paid", NOW - DAY)}
    q = probe_select.select_queue(["p"], state, NOW)
    assert q == ["p"]


def test_custom_stale_hours():
    state = {"p": entry("paid", NOW - 6 * 3600)}
    assert probe_select.select_queue(["p"], state, NOW, stale_hours=12) == []
    assert probe_select.select_queue(["p"], state, NOW, stale_hours=6) == ["p"]


def test_paid_epoch_none_is_stale():
    state = {"p": entry("paid", None)}
    q = probe_select.select_queue(["p"], state, NOW)
    assert q == ["p"]


# ---------- stale paid ordering ----------

def test_stale_paid_sorted_oldest_first_none_epochs_first():
    catalog = ["p-mid", "p-old", "p-none2", "p-none1", "p-new"]
    state = {
        "p-mid": entry("paid", NOW - 2 * DAY),
        "p-old": entry("paid", NOW - 5 * DAY),
        "p-none2": entry("paid", None),
        "p-none1": entry("paid", None),
        "p-new": entry("paid", NOW - DAY),
    }
    q = probe_select.select_queue(catalog, state, NOW)
    assert q == ["p-none1", "p-none2", "p-old", "p-mid", "p-new"]


def test_stale_paid_epoch_tiebreak_by_id():
    catalog = ["b", "a"]
    state = {"a": entry("paid", NOW - 2 * DAY), "b": entry("paid", NOW - 2 * DAY)}
    q = probe_select.select_queue(catalog, state, NOW)
    assert q == ["a", "b"]


# ---------- junk tolerance ----------
@pytest.mark.parametrize("junk_verdict", ["", "defer", 42, None, "FREE", "Paid", {}, []])
def test_junk_verdict_treated_as_new_arrival(junk_verdict):
    catalog = ["m"]
    state = {"m": {"verdict": junk_verdict, "epoch": NOW}}
    q = probe_select.select_queue(catalog, state, NOW)
    assert q == ["m"]


def test_entry_missing_verdict_key_treated_as_new_arrival():
    catalog = ["m"]
    state = {"m": {"epoch": NOW}}
    assert probe_select.select_queue(catalog, state, NOW) == ["m"]


@pytest.mark.parametrize("junk", ["junk", 42, None, [], True])
def test_non_dict_entry_treated_as_new_arrival(junk):
    catalog = ["m"]
    state = {"m": junk}
    assert probe_select.select_queue(catalog, state, NOW) == ["m"]


def test_junk_epoch_on_paid_treated_as_stale():
    # non-numeric epoch cannot prove recency — same read-boundary policy as
    # probe_state.get_verdict: degrade to None => stale.
    catalog = ["m"]
    state = {"m": {"verdict": "paid", "epoch": "yesterday"}}
    assert probe_select.select_queue(catalog, state, NOW) == ["m"]


def test_junk_epoch_on_free_stays_in_tier2():
    # tier 2 keys off verdict only; junk epoch must not crash or re-tier it
    catalog = ["m"]
    state = {"m": {"verdict": "free", "epoch": "garbage"}}
    assert probe_select.select_queue(catalog, state, NOW) == ["m"]


def test_provider_state_not_a_dict_never_crashes():
    assert probe_select.select_queue(["a", "b"], "not-a-dict", NOW) == ["a", "b"]
    assert probe_select.select_queue(["a"], None, NOW) == ["a"]


def test_catalog_not_a_dict_but_iterable():
    assert probe_select.select_queue("ab", {}, NOW) == ["a", "b"]


# ---------- catalog/state intersection ----------

def test_vanished_state_entries_ignored():
    catalog = ["kept"]
    state = {"kept": entry("free"), "gone": entry("paid", NOW - 10 * DAY)}
    q = probe_select.select_queue(catalog, state, NOW)
    assert "gone" not in q
    assert q == ["kept"]


def test_empty_catalog_empty_queue():
    state = {"x": entry("paid", NOW - 10 * DAY), "y": entry("free")}
    assert probe_select.select_queue([], state, NOW) == []


def test_empty_state_everything_new():
    q = probe_select.select_queue(["b", "a"], {}, NOW)
    assert q == ["a", "b"]


# ---------- clock skew ----------

def test_future_epoch_excluded_as_recent():
    state = {"p": entry("paid", NOW + 100)}
    assert probe_select.select_queue(["p"], state, NOW) == []


# ---------- purity / determinism ----------

def test_determinism_same_inputs_same_output():
    catalog = ["p-old", "new", "free-a", "p-none", "free-b", "p-mid"]
    state = {
        "free-a": entry("free"),
        "free-b": entry("free"),
        "p-old": entry("paid", NOW - 3 * DAY),
        "p-mid": entry("paid", NOW - 2 * DAY),
        "p-none": entry("paid", None),
    }
    a = probe_select.select_queue(catalog, state, NOW)
    b = probe_select.select_queue(catalog, state, NOW)
    assert a == b
    assert a == ["new", "free-a", "free-b", "p-none", "p-old", "p-mid"]


def test_inputs_not_mutated():
    catalog = ["a", "b"]
    state = {"a": entry("paid", None), "b": entry("free")}
    catalog_snapshot = list(catalog)
    state_snapshot = {"a": dict(state["a"]), "b": dict(state["b"])}
    probe_select.select_queue(catalog, state, NOW)
    assert catalog == catalog_snapshot
    assert state == state_snapshot


def test_generator_catalog_accepted():
    # catalog_ids is documented as an iterable — a generator must work
    q = probe_select.select_queue((m for m in ["b", "a"]), {}, NOW)
    assert q == ["a", "b"]
