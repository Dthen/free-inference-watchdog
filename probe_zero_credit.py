"""Zero-credit probe for black-box gateways."""
import json
import urllib.error
import urllib.request


class Result:
    FREE, PAID, DEFER = "free", "paid", "defer"


def probe_model(base_url, token, model_id, timeout=30):
    """Fire 1-token completion. Returns (Result.*, meta_dict)."""
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
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
        return Result.DEFER, meta  # 404, 500, 429, etc
    except Exception as exc:
        return Result.DEFER, {"error": str(exc)}