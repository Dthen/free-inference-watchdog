"""Persisted zero-credit probe verdicts. Stdlib only.

Atomic write pattern mirrors state.py: temp file + os.replace.
Corrupt file degrades to {} (never fatal).
DEFER never recorded as a verdict — only 'free'/'paid' strings are valid.
"""

import json
import math
import os
import threading


# ---------- generic atomic write ----------

def _atomic_write_json(path, data):
    """Atomic write using temp file + os.replace (same pattern as state.py)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per process + thread to avoid temp file collision races
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)  # atomic rename on local ext4


def _load_json_or_default(path, default):
    """Load JSON or return default on any error (corrupt, missing, etc.)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, RecursionError):
        return default
    return data


# ---------- public API ----------

def load_probe_state(path):
    """Parsed probe state dict, or {} if missing/corrupt/not-a-dict."""
    data = _load_json_or_default(path, {})
    return data if isinstance(data, dict) else {}


def record_verdict(path, provider, model_id, verdict, now, defer_epoch=None):
    """Record one verdict atomically. verdict is 'free' or 'paid'.
    
    A defer_epoch may ride along when the verdict came from a tick where
    other models deferred, but DEFER itself must NEVER overwrite a verdict
    — pass verdict + defer_epoch from the same probe response.
    
    Raises ValueError if verdict is not 'free' or 'paid'.
    """
    if verdict not in ("free", "paid"):
        raise ValueError(f"verdict must be 'free' or 'paid', got {verdict!r}")

    state = load_probe_state(path)

    if not isinstance(state.get(provider), dict):
        state[provider] = {}

    entry = {"verdict": verdict, "epoch": int(now)}
    if defer_epoch is not None:
        entry["defer_epoch"] = int(defer_epoch)

    state[provider][model_id] = entry
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

    # Validate epoch at the read boundary (mirror state.py load_alive):
    # non-numeric/bool/non-finite junk from a hand-edited file reads as
    # absent so it never reaches callers.
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) \
            or not math.isfinite(epoch):
        epoch = None
    else:
        epoch = int(epoch)

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