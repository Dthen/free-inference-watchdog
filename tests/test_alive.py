"""Tests for alive.py — the two-clock self-watch."""

import alive


NOW = 1_000_000_000
SIX_H = 6 * 3600


def test_fresh_output_never_pings():
    assert not alive.should_ping(
        NOW - SIX_H, NOW)                          # output 6h ago (F8c: no last_tick param)
    assert not alive.should_ping(NOW - 100, NOW)   # output 100s ago


def test_ping_after_twenty_hours_of_silence():
    # Ticks kept happening but NOTHING was emitted for 25h.
    assert alive.should_ping(NOW - 25 * 3600, NOW)


def test_original_bug_regression_guard():
    # The dead-code bug: ticks refreshing the tested timestamp suppressed pings.
    for hours in (6, 12, 18):
        assert not alive.should_ping(NOW - hours * 3600, NOW)
    # But once output age crosses the threshold it fires EVEN with fresh tick.
    assert alive.should_ping(NOW - 20 * 3600 - 1, NOW)


def test_first_run_no_output_yet_pings():
    assert alive.should_ping(None, NOW)


def test_missed_ticks_detection():
    cadence = 6 * 3600
    assert not alive.missed_ticks(NOW - cadence, cadence, NOW)      # on time
    assert not alive.missed_ticks(NOW - (cadence + 3600), cadence, NOW)  # in slack
    assert alive.missed_ticks(NOW - (cadence + 3 * 3600), cadence, NOW)  # too old
    assert not alive.missed_ticks(None, cadence, NOW)               # no baseline yet


def test_format_alive_variants():
    plain = alive.format_alive(6, [], {}, 0)
    assert "6 providers" in plain and "💚" in plain
    # F-R2-cosmetic: transients render as counts (name(n)) — same single
    # rendering notify.format_alert uses, not bare names.
    busy = alive.format_alive(
        5, ["nous"],
        {"zen": {"added": [], "removed": ["z2"]}, "kilo": 2}, 2)
    assert "nous" in busy and "2" in busy
    assert "zen(1)" in busy          # dict event -> added+removed count
    assert "kilo(2)" in busy         # pre-counted int passes through
