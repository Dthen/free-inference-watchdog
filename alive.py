"""Self-watch: two-clock heartbeat so silence means healthy, not dead.

(Critic round-1 blocker fix: the original single-clock design could NEVER
fire at 6h cadence because every tick refreshed the very timestamp the
ping condition tested.)
"""

OUTPUT_STALE_S = 20 * 3600   # 💚 ping when nothing user-visible for 20h+
TICK_SLACK_S = 2 * 3600      # ⚠️ warning when last tick older than cadence+slack


def should_ping(last_output, now):
    """Alive ping fires on OUTPUT age alone — ticks updating timestamps must
    not suppress it (that was the original bug). F8c: the old `last_tick`
    param was dead and is removed."""
    if last_output is None:
        return True
    return now - last_output >= OUTPUT_STALE_S


def missed_ticks(last_tick, cadence_s, now):
    """True when the cron itself looks dead: no tick within cadence + slack."""
    if last_tick is None:
        return False
    return now - last_tick > cadence_s + TICK_SLACK_S


def format_alive(providers_polled, stale, transients, dropped_total):
    import notify  # deferred: single rendering of transients (F-R2 cosmetic)

    bits = [f"💚 monitor alive — {providers_polled} providers polled"]
    if stale:
        bits.append("fetch failed (carried forward): " + ", ".join(sorted(stale)))
    if transients:
        bits.append("transient flaps ignored: "
                    + notify.format_transient_counts(transients))
    if dropped_total:
        bits.append(f"dropped undeliverable alerts total: {dropped_total}")
    return " · ".join(bits)
