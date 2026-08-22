"""Discord delivery: pretty alerts, poison-pill caps, bounded retry queue.

Delivery topology (plan round 2): every user-visible message goes to BOTH
stdout (cron -> Discord home) AND the webhook (kennel channel). Webhook
failure never blocks the stdout copy.
"""

import json
import time
import urllib.request

MAX_BULLETS_PER_SECTION = 15
HARD_CAP_CHARS = 1900           # Discord limit is 2000; footer must survive
MAX_ATTEMPTS = 5                # pending payload dropped after this many POSTs


def append_pending(queue_path, payload, **kwargs):
    from state import append_pending as _append
    _append(queue_path, payload, **kwargs)


def load_pending(queue_path):
    from state import load_pending as _load
    return _load(queue_path)


def save_pending(queue_path, items):
    from state import save_pending as _save
    _save(queue_path, items)


def format_bullet_list(models, bullet):
    """Capped bullet list: '…+N more' beyond the cap."""
    lines = []
    shown = models[:MAX_BULLETS_PER_SECTION]
    for m in shown:
        lines.append(f"{bullet} `{m}`")
    rest = len(models) - len(shown)
    if rest > 0:
        lines.append(f"…+{rest} more")
    return "\n".join(lines)


def format_transient_counts(transients):
    """Single rendering of the transients field: `name(count), name(count)`.

    Shared by notify.format_alert and alive.format_alive (F-R2 cosmetic:
    one field, one rendering). `count` is the int itself when the value is
    already a number, else added+removed with a floor of 1."""
    bits = []
    for name, val in sorted((transients or {}).items()):
        n = val if isinstance(val, int) else max(
            1, len(val.get("added", [])) + len(val.get("removed", [])))
        bits.append(f"{name}({n})")
    return ", ".join(bits)


def format_alert(events, tick_iso, providers_polled, transients, stale,
                 dropped_total):
    """Human-readable alert body. ALWAYS < HARD_CAP_CHARS (footer survives)."""
    parts = ["**🔔 Free-tier roster change**"]
    for name in sorted(events):
        added = events[name].get("added") or []
        removed = events[name].get("removed") or []
        body = []
        if added:
            body.append(format_bullet_list(added, "➕"))
        if removed:
            body.append(format_bullet_list(removed, "➖"))
        if body:
            parts.append(f"**{name}**\n" + "\n".join(body))
    notes = []
    if transients:
        notes.append("transient flaps ignored: "
                     + format_transient_counts(transients))
    if stale:
        notes.append(f"fetch failed (carried forward): {', '.join(sorted(stale))}")
    if dropped_total:
        notes.append(f"dropped undeliverable alerts total: {dropped_total}")
    tail = f"\n\n— {tick_iso} · {providers_polled} providers polled"
    if notes:
        tail += " · " + " · ".join(notes)
    body_text = "\n\n".join(parts)
    if len(body_text) + len(tail) >= HARD_CAP_CHARS:
        keep = HARD_CAP_CHARS - len(tail) - 20
        body_text = body_text[:keep].rsplit("\n", 1)[0] + "\n…(truncated)"
    return body_text + tail


# ---------- webhook plumbing ----------

def _urlopen(req, timeout=10):  # indirection so tests can monkeypatch
    return urllib.request.urlopen(req, timeout=timeout)


def _split_valid_items(items):
    """F8f: hand-edited queues can hold non-dict rows. Purge them, counting
    each as a dropped alert — never crash the tick on a malformed shape."""
    valid = [it for it in items if isinstance(it, dict)]
    for _ in range(len(items) - len(valid)):
        _bump_drop_counter()
    return valid


def _bump_attempts(item):
    """Normalize a possibly hand-edited attempts field, then increment."""
    attempts = item.get("attempts")
    item["attempts"] = attempts + 1 if isinstance(attempts, int) else 1


def send_webhook(webhook_url, content, queue_path, max_attempts=MAX_ATTEMPTS):
    """POST text to the webhook. On failure: enqueue/attempt-bump, return False.
    A payload is DROPPED after max_attempts failures (queue can't poison)."""
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "free-inference-monitor/1.0"})
    try:
        with _urlopen(req, timeout=10) as resp:
            if 200 <= getattr(resp, "status", 200) < 300:
                return True
            raise OSError(f"webhook HTTP {resp.status}")
    except Exception:
        items = _split_valid_items(load_pending(queue_path))
        save_pending(queue_path, items)          # purge malformed immediately
        existing = next((it for it in items
                         if isinstance(it.get("payload"), dict)
                         and it["payload"].get("content") == content), None)
        if existing is not None:
            _bump_attempts(existing)
            if existing["attempts"] >= max_attempts:
                items.remove(existing)   # drop — surfaced via alive counter
                _bump_drop_counter()
            save_pending(queue_path, items)
        else:
            # the failed POST counts as attempt #1
            append_pending(queue_path, {"content": content}, attempts=1)
        return False


_dropped_total = 0


def _bump_drop_counter():
    global _dropped_total
    _dropped_total += 1


def get_dropped_total():
    return _dropped_total


def drain_pending(webhook_url, queue_path):
    """Retry queued payloads oldest-first. Returns number actually delivered.
    F8f: non-dict / malformed rows are purged and counted dropped, never crash."""
    items = _split_valid_items(load_pending(queue_path))
    remaining, sent = [], 0
    for item in items:
        payload = item.get("payload")
        content = payload.get("content", "") if isinstance(payload, dict) else ""
        data = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "free-inference-monitor/1.0"})
        try:
            with _urlopen(req, timeout=10) as resp:
                ok = 200 <= getattr(resp, "status", 200) < 300
        except Exception:
            ok = False
        if ok:
            sent += 1
        else:
            _bump_attempts(item)
            if item["attempts"] >= MAX_ATTEMPTS:
                _bump_drop_counter()
                continue
            remaining.append(item)
    save_pending(queue_path, remaining)  # queue file shape is ALWAYS a list
    return sent
