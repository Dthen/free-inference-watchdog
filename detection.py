"""Detection helpers — string-keyed dispatch for free-model detection.

Legacy methods keep their exact two-argument meanings. The one new key,
``catalog-pricing``, is a bounded optional dispatch into the registered
catalogue adapters: it needs the selected format and is False without one, so
an old caller that never passes a format sees no behaviour change.
"""

import catalog_adapters


def detect_free(model, method, catalog_format=None):
    """Check if a model is free using the named detection method.

    ``catalog_format`` is an optional third argument used ONLY by
    ``catalog-pricing``; every legacy method ignores it.
    """
    if method == "catalog-pricing":
        # No format, the legacy shape and an unregistered name are deliberately
        # not free here (they have their own keys / are rejected by the loader).
        # A REGISTERED family always reaches its adapter: a missing predicate
        # branch raises instead of returning a healthy empty observation.
        if not catalog_adapters.is_registered_format(catalog_format):
            return False
        return catalog_adapters.is_free_row(model, catalog_format)
    if method == "api-pricing":
        pricing = model.get("pricing", {})
        try:
            return float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
        except (TypeError, ValueError):
            return False
    if method == "api-flag":
        return model.get("isFree") is True
    if method == "id-suffix":
        model_id = model.get("id", "")
        if not isinstance(model_id, str):
            return False
        return model_id.endswith(":free") or model_id.endswith("-free") or "free" in model_id.lower()
    if method == "all-free":
        return True
    return False