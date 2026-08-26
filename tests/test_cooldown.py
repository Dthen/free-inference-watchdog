"""Tests for cooldown.filter_cooldown — same flap never re-spams within TTL."""

from datetime import datetime, timezone

import pytest

import cooldown


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc).timestamp()


def _events(*triples):
    """triples: (provider, kind, model) -> events dict."""
    out = {}
    for provider, kind, model in triples:
        out.setdefault(provider, {"added": [], "removed": []})[kind].append(model)
    return out


def test_first_alert_passes_and_records():
    events = _events(("nous", "added", "m1"))
    survivors, cd = cooldown.filter_cooldown(events, {}, NOW)
    assert survivors == events
    assert cd == {"nous|m1|added": NOW}


def test_repeat_within_ttl_suppressed():
    cd = {"nous|m1|added": NOW - 3600}  # 1h ago, TTL 12h
    survivors, out = cooldown.filter_cooldown(_events(("nous", "added", "m1")), cd, NOW)
    assert out["nous|m1|added"] == NOW - 3600  # original timestamp preserved


def test_fully_suppressed_provider_omitted_from_survivors():
    """F8d: the dead 'provider not in survivors' clause used to insert
    empty-section providers; callers must see them OMITTED instead."""
    cd = {"nous|m1|added": NOW - 3600}
    survivors, _ = cooldown.filter_cooldown(
        _events(("nous", "added", "m1")), cd, NOW)
    assert survivors == {}


def test_different_model_still_passes():
    cd = {"nous|m1|added": NOW - 60}
    survivors, out = cooldown.filter_cooldown(
        _events(("nous", "added", "m2")), cd, NOW)
    assert survivors["nous"]["added"] == ["m2"]
    assert "nous|m2|added" in out


def test_expired_entry_allows_again():
    cd = {"nous|m1|added": NOW - 43200 - 10}  # just past TTL
    survivors, out = cooldown.filter_cooldown(
        _events(("nous", "added", "m1")), cd, NOW)
    assert survivors["nous"]["added"] == ["m1"]
    assert out["nous|m1|added"] == NOW


def test_mixed_events_partially_suppressed():
    cd = {"kilo|gone|removed": NOW - 100}
    events = _events(("kilo", "removed", "gone"), ("kilo", "added", "new"))
    survivors, out = cooldown.filter_cooldown(events, cd, NOW)
    assert survivors["kilo"] == {"added": ["new"], "removed": []}
    assert "kilo|new|added" in out
    assert out["kilo|gone|removed"] == NOW - 100


def test_empty_sections_dropped_from_output():
    survivors, _ = cooldown.filter_cooldown(
        _events(("nous", "added", "m1")), {}, NOW)
    assert survivors["nous"]["removed"] == []


# ---------- fix-round-2 S2: stamp acceptance guard ----------

@pytest.mark.parametrize("bad_stamp", [
    float("inf"), float("-inf"), float("nan"),
])
def test_nonfinite_stamps_alert_and_restamp(bad_stamp):
    """S2: inf/-inf stamps suppress FOREVER — `now - inf` is negative, so
    `< ttl` holds; nan slips through only because every nan comparison is
    False. All three are junk: the stamp counts as INVALID — the alert
    fires and the map is restamped with `now`, healing the poison on the
    next tick instead of persisting it byte-identical forever."""
    events = _events(("nous", "added", "m1"))
    survivors, out = cooldown.filter_cooldown(events,
                                              {"nous|m1|added": bad_stamp},
                                              NOW)
    assert survivors == events                      # alert fires
    assert out["nous|m1|added"] == NOW              # restamped now


def test_future_stamp_is_invalid_alert_fires():
    """S2 mirror of the save-side rule: 0 <= now - v < ttl. A future-dated
    stamp (clock skew / hand edit) gives a negative age — invalid, so the
    alert fires NOW instead of being suppressed arbitrarily long."""
    events = _events(("nous", "added", "m1"))
    survivors, out = cooldown.filter_cooldown(
        events, {"nous|m1|added": NOW + 9999}, NOW)
    assert survivors == events
    assert out["nous|m1|added"] == NOW


def test_bool_true_stamp_is_invalid_alert_fires():
    """S2: bool subclasses int — True would coerce to 1 and (on a
    seconds-since-epoch clock) read as ancient-but-finite, suppressing until
    1970+TTL. Same rule as state.save_cooldowns: bool is never a stamp."""
    events = _events(("nous", "added", "m1"))
    survivors, out = cooldown.filter_cooldown(events,
                                              {"nous|m1|added": True}, NOW)
    assert survivors == events
    assert out["nous|m1|added"] == NOW


def test_fresh_past_stamp_still_suppressed():
    """S2 guard-rail: a legitimate fresh stamp keeps its original timestamp —
    the acceptance gate only widens what counts as invalid."""
    cd = {"nous|m1|added": NOW - 3600}
    survivors, out = cooldown.filter_cooldown(_events(("nous", "added", "m1")),
                                              cd, NOW)
    assert survivors == {}
    assert out["nous|m1|added"] == NOW - 3600

