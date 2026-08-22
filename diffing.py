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
    PROVIDERS (a zombie entry must never diff forever). None if absent/corrupt."""
    roster = state.load_roster(roster_path)
    if roster is None:
        return None
    providers_map = roster.get("providers")
    if isinstance(providers_map, dict):
        roster["providers"] = {
            k: v for k, v in providers_map.items() if k in registry_keys
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

    Exactly one recheck per provider — no third looks.
    """
    confirmed, transients, unconfirmed = {}, {}, {}
    if not candidates:
        return {"confirmed": confirmed, "transients": transients,
                "unconfirmed": unconfirmed}

    sleep(delay)  # one nap covers every affected provider
    for name, event in candidates.items():
        try:
            fresh_ids = fetch_one(name)
        except FetchError:
            unconfirmed[name] = event
            continue
        old_set = set(_sorted_list(prev_providers.get(name, [])))
        new_set = set(_sorted_list(fresh_ids))
        recheck = {
            "added": sorted(new_set - old_set),
            "removed": sorted(old_set - new_set),
        }
        if recheck["added"] or recheck["removed"]:
            confirmed[name] = recheck
        else:
            transients[name] = event
    return {"confirmed": confirmed, "transients": transients,
            "unconfirmed": unconfirmed}
