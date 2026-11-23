"""Persisted zero-credit probe verdicts. Stdlib only.

Atomic writes and tolerant loads reuse state.py's primitives: temp file +
os.replace, and a corrupt file degrades to {} (never fatal).
DEFER is never stored as a verdict — only 'free'/'paid' strings are valid.
"""

from state import _atomic_write_json, _is_finite_number, _load_json_or_default


# ---------- public API ----------

def load_probe_state(path):
    """Parsed probe state dict, or {} if missing/corrupt/not-a-dict."""
    data = _load_json_or_default(path, {})
    return data if isinstance(data, dict) else {}


def save_probe_state(path, state):
    """Persist the full probe-state dict atomically.

    Used once per tick to persist verdicts AND prunes together. The
    in-memory state dict is the single source of truth during the tick;
    this function writes it atomically at the end.
    """
    if not isinstance(state, dict):
        state = {}
    _atomic_write_json(path, state)


def get_verdict(state, provider, model_id):
    """Return ('free'|'paid'|None, epoch_or_None). None = never probed."""
    if not isinstance(state, dict):
        return None, None

    provider_data = state.get(provider)
    if not isinstance(provider_data, dict):
        return None, None

    entry = provider_data.get(model_id)
    if not isinstance(entry, dict):
        return None, None

    verdict = entry.get("verdict")
    epoch = entry.get("epoch")

    if verdict not in ("free", "paid"):
        return None, None

    # Validate epoch at the read boundary (shared state._is_finite_number):
    # non-numeric/bool/non-finite junk from a hand-edited file reads as
    # absent so it never reaches callers.
    if _is_finite_number(epoch):
        epoch = int(epoch)
    else:
        epoch = None

    return verdict, epoch


def drop_missing(state, provider, catalog_ids):
    """Prune entries for models no longer in the catalog (gateway removed them).
    
    Returns pruned state. Never fatal.
    """
    if not isinstance(state, dict):
        return {}

    if provider not in state:
        return state

    provider_data = state[provider]
    if not isinstance(provider_data, dict):
        return state

    # Prune models not in catalog
    catalog_set = set(catalog_ids)
    pruned_provider = {
        model_id: entry
        for model_id, entry in provider_data.items()
        if model_id in catalog_set
    }

    new_state = dict(state)
    new_state[provider] = pruned_provider

    return new_state