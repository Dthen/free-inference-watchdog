"""Detection helpers — string-keyed dispatch for free-model detection."""

def detect_free(model, method):
    """Check if a model is free using the named detection method."""
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