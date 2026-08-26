"""Alert dedup: same (provider, model, event) stays silent within the TTL."""

import math

COOLDOWN_TTL_S = 12 * 3600  # decision #8: 12h cooldown, kept


def filter_cooldown(events, cooldowns, now, ttl_s=COOLDOWN_TTL_S):
    """Drop alert triples seen inside the TTL window.

    Returns (survivors, new_cooldowns): survivors keeps the events shape but
    only unsuppressed models; new_cooldowns = old map + `now` stamped on every
    triple actually being alerted (suppressed ones keep their original stamp).

    A stored stamp suppresses ONLY if it is a finite number (bool excluded —
    bool subclasses int) AND its age satisfies last <= now, i.e.
    0 <= now - last < ttl_s (fix-round-2 S2, mirror of state.save_cooldowns).
    inf/-inf suppressed forever (`now - inf` is negative) and future-dated
    stamps suppressed arbitrarily long; nan slipped through on a comparison
    technicality. Invalid stamps count as ABSENT: the alert fires and the
    map is restamped with `now`, healing the poison read-side instead of
    persisting it byte-identical forever.
    """
    out_cd = dict(cooldowns)
    survivors = {}

    for provider, sections in sorted(events.items()):
        kept = {"added": [], "removed": []}
        for kind in ("added", "removed"):
            for model in sections.get(kind, []):
                key = f"{provider}|{model}|{kind}"
                last = out_cd.get(key)
                if isinstance(last, (int, float)) \
                        and not isinstance(last, bool) \
                        and math.isfinite(last) and last <= now \
                        and now - last < ttl_s:
                    continue  # suppressed — keep original timestamp
                out_cd[key] = now
                kept[kind].append(model)
        if any(kept.values()):
            survivors[provider] = kept
    return survivors, out_cd
