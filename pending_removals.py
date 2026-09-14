"""Removal hold queue: confirmed removals on opted-in providers wait here,
resolved against later fetches. Stdlib only, pure logic + validated state I/O.

Contract (anti-cooldown doctrine):
- gone_since is stamped once at first confirmed absence and NEVER rewritten;
  a failed fetch is neutral (entry waits; clock does not extend or reset).
- Mutates held entries IN PLACE — shallow copies alias them; to fork a map
  use `copy.deepcopy`, never `dict()`.
- Failure is signaled by ABSENCE of the provider key in fetches, never None.
- Recovery drops the entry (caller restores roster by union). Expiry hands
  the id to the caller to alert through the normal path (consumed from held;
  re-merge via with_expired() for the pre-emit save if at-least-once wanted).
  Un-flagged providers release their entries silently. Entries are always
  CONSUMED on resolution; no stale stamps can silence anything forever
  (the bug class of commit 898724b).
"""
import copy
import math
import sys
from pathlib import Path

import state

PENDING_FILE = "pending_removals.json"

# Stamp fields validated at the boundary, load_alive semantics.
_STAMPS = ("gone_since", "last_absent_seen")


def path_in(state_dir):
    """Canonical location of the hold queue inside a state directory."""
    return Path(state_dir) / PENDING_FILE


def sorted_ids(items):
    return sorted(set(str(i) for i in items))


def _valid_stamp(value):
    """Non-bool int/float, finite — same junk class as state.load_alive:
    json.load happily parses NaN/Infinity, but int(nan) raises ValueError
    and int(inf) OverflowError, which would FATAL the resolve loop."""
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value))


def load(path):
    """Validate hard, load_alive semantics: top-level dict; per-provider
    dict slice; per-entry dict whose gone_since/last_absent_seen are
    non-bool int/float (int()-coerced). Junk slices/entries are DROPPED,
    never crash settle, never poison the resolve loop. Drops are a visible
    skip: one stderr note when N>0 (healthy runs stay silent)."""
    data = state._load_json_or_default(path, {})
    dropped = 0
    if not isinstance(data, dict):
        dropped = 1  # whole payload was junk — one visible skip
        data = {}
    clean = {}
    for provider, slice_ in data.items():
        if not isinstance(slice_, dict):
            dropped += 1
            continue  # junk provider slice — dropped, valid siblings survive
        entries = {}
        for model_id, entry in slice_.items():
            if not isinstance(entry, dict):
                dropped += 1
                continue
            if not all(f in entry and _valid_stamp(entry[f])
                       for f in _STAMPS):
                dropped += 1
                continue  # junk stamp (str/bool/None/NaN/Inf/missing) dropped
            entries[model_id] = {f: int(entry[f]) for f in _STAMPS}
        clean[provider] = entries
    if dropped:
        print(f"pending_removals: dropped {dropped} junk "
              f"{'entry' if dropped == 1 else 'entries'} from {path}",
              file=sys.stderr)
    return clean


def save(path, held):
    """Atomically write the hold queue to `path` (state._atomic_write_json
    semantics: tmp file + os.replace — readers never see a half-written map)."""
    state._atomic_write_json(path, held)


def enqueue(held, provider, ids, now):
    """Stamp first confirmed absence. Mutates held. Mutates held entries
    IN PLACE — shallow copies alias them; to fork a map use `copy.deepcopy`,
    never `dict()`. Re-enqueue keeps the ORIGINAL gone_since (the clock never
    resets); only last_absent_seen advances. Empty `ids` creates no slice."""
    now_i = int(now)
    if not ids:
        return  # amendment: no enqueue([]) dust — {"p": {}} never created
    slice_ = held.setdefault(provider, {})
    for model_id in ids:
        model_id = str(model_id)
        entry = slice_.get(model_id)
        if entry is None:
            slice_[model_id] = {"gone_since": now_i,
                                "last_absent_seen": now_i}
        else:
            entry["last_absent_seen"] = now_i


def pending_ids(held, provider):
    return sorted_ids(held.get(provider, {}))


def settle(held, fetches, holds, now):
    """Mutates held. Mutates held entries IN PLACE — shallow copies alias
    them; to fork a map use `copy.deepcopy`, never `dict()`. Returns
    {"recovered": [(p,id)] sorted, "expired": {p: [ids] sorted}, "released":
    [(p,id)] sorted, "changed": set(p)}.
    - provider absent from fetches -> skip (neutral)
    - holds.get(p) is None/absent -> release all entries
    - id in fetches[p] -> recovered; else if now - gone_since >= hold ->
      expired (consumed); else last_absent_seen = int(now)
    A slice emptied during the pass is pruned (`del held[p]`) — no
    {"p": {}} dust persists."""
    now_i = int(now)
    recovered = []
    expired = {}
    released = []
    changed = set()
    for provider in sorted(held):
        entries = held[provider]
        if provider not in fetches:
            continue  # fetch failure is neutral: entry waits, clock frozen
        fetched = fetches[provider]
        hold = holds.get(provider)
        if hold is None:
            # Un-flagged (or unknown) provider: release silently —
            # neither recovered nor expired, never re-alerted.
            for model_id in sorted(entries):
                released.append((provider, model_id))
            if entries:
                changed.add(provider)
            entries.clear()
            if not entries:
                del held[provider]  # amendment: prune emptied slice
            continue
        for model_id in sorted(entries):
            entry = entries[model_id]
            if model_id in fetched:
                recovered.append((provider, model_id))
                del entries[model_id]
                changed.add(provider)
            elif now_i - entry["gone_since"] >= hold:
                expired.setdefault(provider, []).append(model_id)
                del entries[model_id]
                changed.add(provider)
            else:
                entry["last_absent_seen"] = now_i
        if not entries:
            del held[provider]  # amendment: prune emptied slice
    return {"recovered": sorted(recovered),
            "expired": expired,
            "released": sorted(released),
            "changed": changed}


def with_expired(held, expired, stamps_from):
    """Pure (never mutates any arg): NEW dict = deep-copied held plus expired
    ids re-merged using the stamps from stamps_from (the full stamped
    PRE-settle snapshot map load() returned before settle ran). None is
    tolerated for expired and stamps_from; an expired id with no stamp in
    stamps_from is NOT re-merged (no invented stamps) and prints a stderr
    note — a visible skip, never silent."""
    merged = copy.deepcopy(held)
    for provider, ids in (expired or {}).items():
        for model_id in ids:
            stamp = (stamps_from or {}).get(provider, {}).get(model_id)
            if stamp is None:
                print(f"pending_removals: no pre-settle stamp for "
                      f"{provider}/{model_id} — expired entry not re-queued",
                      file=sys.stderr)
                continue  # amendment: skip + note, never invent a stamp
            merged.setdefault(provider, {})[model_id] = copy.deepcopy(stamp)
    return merged
