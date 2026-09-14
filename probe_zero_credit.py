"""Zero-credit probe for black-box gateways."""
import json
import urllib.error
import urllib.request


class Result:
    FREE, PAID, DEFER = "free", "paid", "defer"


# b.ai's paid classification signals, kept as the generic fallback for
# configs that omit a "probe" block. A zero-credit gateway with its own
# dialect ships "probe.paid_signals" in its providers/*.json — a custom
# list REPLACES these defaults rather than extending them.
#
# Signal shape: {"status": int, "all_of": [substrs], "any_of": [substrs]}
# — match means the HTTP status equals "status", EVERY "all_of" substring
# appears in the (case-insensitively lowered) body, and either no "any_of"
# key is present or AT LEAST ONE "any_of" substring appears.
DEFAULT_PAID_SIGNALS = (
    {"status": 403, "all_of": ("deposit",)},
    {"status": 400, "all_of": ("insufficient_user_quota",)},
    {"status": 400, "all_of": ("insufficient",),
     "any_of": ("balance", "quota")},
)


def _signal_matches(sig, status, lowered):
    """True when an HTTP status + lowered body match one paid signal."""
    if status != sig.get("status"):
        return False
    if not all(s in lowered for s in sig.get("all_of", ())):
        return False
    any_of = sig.get("any_of")
    return not any_of or any(s in lowered for s in any_of)


def probe_model(base_url, token, model_id, probe_cfg=None, timeout=30):
    """Fire a minimal completion. Returns (Result.*, meta_dict).

    probe_cfg is the provider's optional "probe" block: max_tokens and
    paid_signals override the generic defaults below. The default
    max_tokens is 3 because b.ai rejects values <= 2 with HTTP 400
    ("max_tokens must be greater than 2") — 3 is the smallest payload that
    gateway accepts; a different zero-credit gateway sets its own via
    "probe.max_tokens".
    """
    cfg = probe_cfg or {}
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": cfg.get("max_tokens", 3),
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/').removesuffix('/v1')}/v1/chat/completions",
        data=body,
        headers={
            "User-Agent": "free-inference-watchdog/1.0",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Result.FREE, {"http": resp.status}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        meta = {"http": exc.code, "body": body_text}
        lowered = body_text.lower()
        for sig in cfg.get("paid_signals", DEFAULT_PAID_SIGNALS):
            if _signal_matches(sig, exc.code, lowered):
                # A matching paid signal: the gateway says the key's
                # balance/quota is exhausted for this model class. A 400
                # WITHOUT a matching signal (e.g. a probe-shape bug) stays
                # DEFER so it self-heals rather than misclassifying.
                return Result.PAID, meta
        return Result.DEFER, meta  # 404, 500, 429, other 400, etc
    except Exception as exc:
        return Result.DEFER, {"error": str(exc)}
