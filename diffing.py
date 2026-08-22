"""Diff engine: set-diff, sticky carry-forward, first-run bootstrap. Stdlib only.

Pure logic — no I/O except load_filtered_roster's single file read.
"""

import state

from providers import FetchError  # re-exported for confirm_diffs callers/tests


def _sorted_list(items):
    return sorted(set(str(i) for i in items))


def compute_events(prev_roster, fetched, registry=None):
    """Compare previous roster against this tick's (sticky-applied) results.

    Returns (events, first_run):
      events   = {provider: {"added": [...], "removed": [...]}} — sorted
      first_run= True when there is no previous roster (bootstrap: no alerts)

    Only providers PRESENT in `fetched` can produce events — a provider the
    tick never ran (registry change) is never a mass removal. `registry`
    optionally restricts both sides to known provider names (anti-zombie).
    """
    if prev_roster is None:
        return {}, True

    prev_providers = prev_roster.get("providers") or {}
    allowed = set(registry) if registry else None

    events = {}
    for name, new_ids in fetched.items():
        if allowed is not None and name not in allowed:
            continue
        prev_ids = prev_providers.get(name)
        old_set = set(_sorted_list(prev_ids)) if isinstance(prev_ids, list) else set()
        new_set = set(_sorted_list(new_ids))
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        if added or removed:
            events[name] = {"added": added, "removed": removed}
    return events, False


def apply_sticky(prev_providers_map, fetched_results):
    """Merge fetch outcomes into the new id map.

    A provider whose fetch FAILED (value None) keeps its previous ids
    (last-known-good) — or starts empty on true first sight — and is named
    in the returned stale list. Failed providers therefore emit ZERO diff
    events (blocker #1 fix): outage noise can never look like removals.
    """
    prev = prev_providers_map or {}
    new_map = {}
    stale = []
    for name, result in fetched_results.items():
        if result is None:
            carried = prev.get(name, [])
            new_map[name] = _sorted_list(carried) if isinstance(carried, list) else []
            stale.append(name)
        else:
            new_map[name] = _sorted_list(result)
    return new_map, stale


def load_filtered_roster(roster_path, registry_keys):
    """Load roster.json via state layer, dropping providers evicted from
    PROVIDERS (a zombie entry must never diff forever). None if absent/corrupt
    OR structurally empty (F4: `providers` must be a dict — anything else
    bootstraps clean instead of emitting the universe as added).

    Fix-round-5 #2: coercion is closed on BOTH sides at this boundary — a
    non-list VALUE loads as [], and a list VALUE has its NON-STRING ELEMENTS
    filtered out (e.g. whole API model objects pasted into a hand-edited
    roster), so nothing can reach _sorted_list's str() and ship as a bogus id."""
    roster = state.load_roster(roster_path)
    if roster is None:
        return None
    providers_map = roster.get("providers")
    if not isinstance(providers_map, dict):
        return None
    roster["providers"] = {
        k: ([i for i in v if isinstance(i, str)]
            if isinstance(v, list) else [])
        for k, v in providers_map.items() if k in registry_keys
    }
    return roster


def confirm_diffs(candidates, prev_providers, fetch_one, sleep, delay):
    """In-run confirmation of candidate diffs (decision #1: no multi-tick lag).

    One nap total (delay seconds), then re-fetch ONLY affected providers and
    re-diff against the same prev state:
      confirmed   — diff survived the recheck  -> will be alerted
      transients  — diff vanished              -> recorded, never alerted
      unconfirmed — recheck itself FAILED      -> silent, sticky-old roster
                     entry kept, signal resurfaces next good tick

    R2-2 RECHECK OUTCOMES PERSIST: the result also carries `corrected`, the
    CORRECTED id-map for every rechecked provider built from its REFETCH:
        transient   -> prev ids (flap leaves NO trace in persisted state)
        confirmed   -> refetch ids (the new truth)
        unconfirmed -> provider ABSENT (caller keeps the sticky-old entry)
    The caller must merge `corrected` over its pre-recheck map BEFORE persisting
    the roster — a transient flap must never leave a phantom reversal behind.

    Exactly one recheck per provider — no third looks.
    """
    confirmed, transients, unconfirmed, corrected = {}, {}, {}, {}
    if not candidates:
        return {"confirmed": confirmed, "transients": transients,
                "unconfirmed": unconfirmed, "corrected": corrected}

    sleep(delay)  # one nap covers every affected provider
    for name, event in candidates.items():
        try:
            fresh_ids, _meta = fetch_one(name)
        except FetchError:
            unconfirmed[name] = event
            continue                 # ABSENT from corrected => sticky-old wins
        old_set = set(_sorted_list(prev_providers.get(name) or []))
        fresh_sorted = _sorted_list(fresh_ids)
        new_set = set(fresh_sorted)
        recheck = {
            "added": sorted(new_set - old_set),
            "removed": sorted(old_set - new_set),
        }
        if recheck["added"] or recheck["removed"]:
            confirmed[name] = recheck
            corrected[name] = fresh_sorted       # refetch truth becomes state
        else:
            transients[name] = event
            corrected[name] = _sorted_list(prev_providers.get(name) or [])
    return {"confirmed": confirmed, "transients": transients,
            "unconfirmed": unconfirmed, "corrected": corrected}


def merge_corrected(new_map, confirmation, prev_providers):
    """R2-2 caller-side merge — the persisted roster NEVER holds a pre-recheck
    snapshot. `corrected` (transient -> prev ids, confirmed -> refetch ids)
    overrides the pre-recheck map; UNCONFIRMED providers (absent from
    `corrected` by contract) keep their STICKY-OLD previous-roster entry so a
    real change resurfaces and alerts on the next good tick.

    F-R2-3: an unconfirmed provider with NO previous-roster entry (post-
    eviction re-add, hand-seeded state) has no sticky-old to restore — it is
    merged as sticky-EMPTY ([]) rather than keeping the pre-recheck snapshot,
    so a first-ever addition resurfaces and alerts on the next good tick.
    """
    merged = dict(new_map)
    merged.update(confirmation.get("corrected") or {})
    for name in (confirmation.get("unconfirmed") or {}):
        if name in prev_providers:
            merged[name] = _sorted_list(prev_providers[name])
        else:
            merged[name] = []
    return merged
