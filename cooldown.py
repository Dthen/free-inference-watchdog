"""Alert dedup: same (provider, model, event) stays silent within the TTL."""

COOLDOWN_TTL_S = 12 * 3600  # decision #8: 12h cooldown, kept


def filter_cooldown(events, cooldowns, now, ttl_s=COOLDOWN_TTL_S):
    """Drop alert triples seen inside the TTL window.

    Returns (survivors, new_cooldowns): survivors keeps the events shape but
    only unsuppressed models; new_cooldowns = old map + `now` stamped on every
    triple actually being alerted (suppressed ones keep their original stamp).
    """
    out_cd = dict(cooldowns)
    survivors = {}

    for provider, sections in sorted(events.items()):
        kept = {"added": [], "removed": []}
        for kind in ("added", "removed"):
            for model in sections.get(kind, []):
                key = f"{provider}|{model}|{kind}"
                last = out_cd.get(key)
                if isinstance(last, (int, float)) and now - last < ttl_s:
                    continue  # suppressed — keep original timestamp
                out_cd[key] = now
                kept[kind].append(model)
        if any(kept.values()) or provider not in survivors:
            survivors[provider] = kept
    return survivors, out_cd
