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
  CONSUMED on resolution; no stale stamps can silence anything forever.
"""
from pathlib import Path

PENDING_FILE = "pending_removals.json"

# Stamp fields validated at the boundary, load_alive semantics.
_STAMPS = ("gone_since", "last_absent_seen")


def path_in(state_dir):
    """Canonical location of the hold queue inside a state directory."""
    return Path(state_dir) / PENDING_FILE


def sorted_ids(items):
    """Canonical id list: string-coerced, deduped, sorted (set-safe input)."""
    return sorted(set(str(i) for i in items))


def pending_ids(held, provider):
    """Sorted ids held for one provider ([] when the provider is absent)."""
    return sorted_ids(held.get(provider, {}))
