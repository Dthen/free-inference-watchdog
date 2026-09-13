"""Zero-credit probe for black-box gateways."""
import json
import urllib.error
import urllib.request


class Result:
    FREE, PAID, DEFER = "free", "paid", "defer"


def probe_model(base_url, token, model_id, timeout=30):
    """Fire a minimal 3-token completion. Returns (Result.*, meta_dict).

    b.ai rejects max_tokens <= 2 with HTTP 400 ("max_tokens must be greater
    than 2"), so 3 is the smallest payload the gateway accepts.
    """
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 3,
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
        if exc.code == 403 and "deposit" in body_text.lower():
            return Result.PAID, meta
        lowered = body_text.lower()
        if exc.code == 400 and (
            "insufficient_user_quota" in lowered
            or ("insufficient" in lowered
                and ("balance" in lowered or "quota" in lowered))
        ):
            # b.ai's PAID signal for zero-balance keys: HTTP 400 with an
            # insufficient-balance/quota body. A 400 WITHOUT those markers
            # (e.g. a probe-shape bug) stays DEFER so it self-heals.
            return Result.PAID, meta
        return Result.DEFER, meta  # 404, 500, 429, other 400, etc
    except Exception as exc:
        return Result.DEFER, {"error": str(exc)}