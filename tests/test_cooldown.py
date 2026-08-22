"""Tests for cooldown.filter_cooldown — same flap never re-spams within TTL."""

from datetime import datetime, timezone

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
