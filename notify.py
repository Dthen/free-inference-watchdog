"""Discord delivery: pretty alerts, message splitting, bounded retry queue.

Delivery topology (plan round 2): every user-visible message goes to BOTH
stdout (cron -> Discord home) AND the webhook (kennel channel). Webhook
failure never blocks the stdout copy.

CHANGE 4 (fix-round-9): alerts are NEVER capped or truncated — format_alert
lists EVERY added/removed model by name. Discord's 2000-char hard limit is
handled by SPLITTING: assembled text >1900 chars is split at section/bullet
boundaries into sequential messages delivered in order; the retry queue
holds failed chunks individually (per-chunk semantics).
"""

import json
import time
import urllib.request

SAFE_CHUNK_CHARS = 1900         # Discord hard limit is 2000
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
    """UNCAPPED bullet list (CHANGE 4): every model by name, every time."""
    return "\n".join(f"{bullet} `{m}`" for m in models)


def split_message(text, max_chars=SAFE_CHUNK_CHARS):
    """Split assembled text into chunks each <= max_chars at LINE boundaries
    (sections/bullets are whole lines — an id is never cut mid-name).

    Joining the chunks reproduces the input exactly (fuzz-pinned, incl.
    trailing empty lines); a single line longer than max_chars becomes its
    own oversized chunk rather than being dropped. Empty/short input yields
    a single-element list."""
    if len(text) <= max_chars:
        return [text]
    chunks, cur_lines, cur_len = [], [], 0

    def flush():
        chunks.append("\n".join(cur_lines))

    for line in text.split("\n"):
        extra = len(line) + 1 if cur_lines else len(line)
        if cur_len + extra <= max_chars:
            cur_lines.append(line)
            cur_len += extra
            continue
        if cur_lines:
            flush()
        if len(line) <= max_chars:
            cur_lines, cur_len = [line], len(line)   # starts the next chunk
        else:
            chunks.append(line)                      # oversize line: OWN chunk
            cur_lines, cur_len = [], 0
    if cur_lines or not chunks:
        chunks.append("\n".join(cur_lines))
    return chunks


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
    """Human-readable alert body — UNCAPPED (CHANGE 4).

    Lists EVERY added/removed model by name under its provider heading.
    Text larger than Discord's limit is handled downstream by
    split_message(); nothing is ever truncated or elided here."""
    parts = ["**🔔 Free-tier roster change**"]
    for name in sorted(events):
        added = events[name].get("added") or []
        removed = events[name].get("removed") or []
        body = []
        if added:
            body.append(format_bullet_list(added, "🟢"))
        if removed:
            body.append(format_bullet_list(removed, "🔴"))
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
    return "\n\n".join(parts) + tail


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
    """POST text to the webhook, SPLIT into <=1900-char chunks (CHANGE 4).

    Oversized alerts are delivered as sequential chunks IN ORDER; each chunk
    is retried individually — a failed chunk is enqueued/attempt-bumped on
    its own (per-chunk retry-queue semantics) and DROPPED after max_attempts
    failures (queue can't poison). Returns True only if EVERY chunk posted."""
    all_ok = True
    for chunk in split_message(content):
        if not _send_webhook_chunk(webhook_url, chunk, queue_path,
                                   max_attempts=max_attempts):
            all_ok = False
    return all_ok


def _send_webhook_chunk(webhook_url, content, queue_path,
                        max_attempts=MAX_ATTEMPTS):
    """Single-chunk POST with the bounded retry-queue semantics."""
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "free-inference-watchdog/1.0"})
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
                     "User-Agent": "free-inference-watchdog/1.0"})
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
