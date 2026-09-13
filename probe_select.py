"""Pure probe-queue selection. Stdlib only, no I/O, no imports of siblings.

The tick cannot fire a completion at every tracked model every tick, so
select_queue() decides WHAT gets probed and in WHAT order. Priority order is
operator-corrected — do NOT reorder:

  1. new arrivals  — catalog ids with no usable verdict yet (classified
                     within one tick). Junk entries (non-dict value, missing
                     or non-'free'/'paid' verdict) count as needing a probe.
  2. free          — verdict == 'free', re-probed EVERY tick: a free model
                     quietly going paid must be caught fast.
  3. stale paid    — verdict == 'paid' last probed >= stale_hours ago
                     (daily pass catches a promo going free). Sorted
                     oldest-first: unknown epochs, then ascending epoch,
                     then id.

Excluded: paid models probed inside the stale window — the recent verdict
stands. A future-dated epoch (clock skew / hand-edit) has negative age,
which is < the stale window, so it is excluded as 'recent'.

provider_state may contain entries for models that vanished from the
catalog; they are ignored here. Pruning is the caller's job via
probe_state.drop_missing — this module stays a pure leaf.

Read-boundary policy for epochs mirrors probe_state.get_verdict: bool /
non-numeric / non-finite junk degrades to None. None epoch on a paid entry
means "no evidence of a recent probe" => stale.
"""

import math

VALID_VERDICTS = ("free", "paid")


def _clean_epoch(epoch):
    """Validate an epoch at the read boundary (mirrors probe_state).

    Bool/non-numeric/non-finite junk from a hand-edited file reads as None
    so it can never masquerade as a recent probe.
    """
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None
    if not math.isfinite(epoch):
        return None
    return int(epoch)


def _classify(entry):
    """Return (verdict, epoch) for one provider_state entry, junk-safe.

    verdict is 'free', 'paid', or None (None => needs a probe: no entry,
    non-dict entry, or verdict not in VALID_VERDICTS).
    """
    if not isinstance(entry, dict):
        return None, None
    verdict = entry.get("verdict")
    if verdict not in VALID_VERDICTS:
        # unhashable junk (dict/list) never equals 'free'/'paid' anyway,
        # but guard so a weird __eq__ cannot crash the tick
        return None, None
    return verdict, _clean_epoch(entry.get("epoch"))


def select_queue(catalog_ids, provider_state, now, stale_hours=24):
    """Return ordered list of model ids to probe this tick. Pure — no I/O.

    catalog_ids: iterable of model ids currently in the gateway catalog.
    provider_state: dict {model_id: {"verdict": str, "epoch": int_or_None,
        ...}} — the per-provider slice of probe_state. Entries for models
        not in catalog_ids are ignored; may itself be junk (non-dict).
    now: tick epoch (int).
    stale_hours: paid models with (now - epoch) >= stale_hours*3600 get
        re-probed.

    Tiers 1 (new arrivals) and 2 (free) are sorted by model id. Tier 3
    (stale paid) is oldest-first: None epochs first (by id), then ascending
    epoch, then id. Never raises on junk data.
    """
    catalog = set(catalog_ids or ())
    state = provider_state if isinstance(provider_state, dict) else {}

    try:
        stale_seconds = float(stale_hours) * 3600
    except (TypeError, ValueError):
        stale_seconds = 24 * 3600

    new_arrivals = []
    free_models = []
    stale_paid = []  # (epoch_or_None, id)

    for model_id in catalog:
        verdict, epoch = _classify(state.get(model_id))
        if verdict is None:
            new_arrivals.append(model_id)
        elif verdict == "free":
            # tier 2 keys off the verdict alone — a free entry with epoch
            # None still re-probes every tick
            free_models.append(model_id)
        else:  # paid
            # epoch None => no evidence of a recent probe => stale.
            # now - epoch negative (future epoch) => recent => excluded.
            if epoch is None or (now - epoch) >= stale_seconds:
                stale_paid.append((epoch, model_id))

    new_arrivals.sort()
    free_models.sort()
    # None epochs first, then ascending epoch, then id
    stale_paid.sort(key=lambda pair: (pair[0] is not None,
                                      pair[0] if pair[0] is not None else 0,
                                      pair[1]))

    return new_arrivals + free_models + [model_id for _, model_id in stale_paid]
