"""Discord delivery: pretty alerts, message splitting, bounded retry queue.

Delivery topology (fix-round-2 S4): the webhook in ~/.hermes/.env
($DISCORD_WEBHOOK_INFERENCE_WATCHDOG) is the ONLY alert delivery channel.
stdout stays local — the Hermes cron job runs silent (--deliver local) and
is not a delivery path; stderr carries fatal diagnostics for the operator.
Webhook failure never blocks the tick (alerts queue for retry instead).

CHANGE 4 (fix-round-9): alerts are NEVER capped or truncated — format_alert
lists EVERY added/removed model by name. Discord's 2000-char hard limit is
handled by SPLITTING: assembled text >1900 chars is split at section/bullet
boundaries into sequential messages delivered in order; the retry queue
holds failed chunks individually (per-chunk semantics).
"""

import json
import time
import urllib.request
import uuid

SAFE_CHUNK_CHARS = 1900         # Discord hard limit is 2000, in UTF-16 units
MAX_ATTEMPTS = 5                # pending payload dropped after this many POSTs


def _discord_len(s):
    """Length of `s` in UTF-16 code units — what Discord actually counts.

    Astral characters (the program's emoji alphabet) cost 2 units each;
    lone surrogates cannot encode, so fall back to the Python char count
    (defensive only; format_* never produces them)."""
    try:
        return len(s.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return len(s)


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

    max_chars is measured in UTF-16 code units (what Discord enforces),
    NOT Python characters — astral emoji cost 2 units each (F2).

    Joining the chunks reproduces the input exactly (fuzz-pinned, incl.
    trailing empty lines); a single line longer than max_chars becomes its
    own oversized chunk rather than being dropped. Empty/short input yields
    a single-element list."""
    if _discord_len(text) <= max_chars:
        return [text]
    chunks, cur_lines, cur_len = [], [], 0

    def flush():
        chunks.append("\n".join(cur_lines))

    for line in text.split("\n"):
        extra = _discord_len(line) + 1 if cur_lines else _discord_len(line)
        if cur_len + extra <= max_chars:
            cur_lines.append(line)
            cur_len += extra
            continue
        if cur_lines:
            flush()
        if _discord_len(line) <= max_chars:
            cur_lines, cur_len = [line], _discord_len(line)  # next chunk
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
    already a number, else added+removed with a floor of 1. Hand-edited
    junk never crashes it: non-dict truthy values render as name(1);
    None / empty-dict values are skipped (nothing to count)."""
    bits = []
    for name, val in sorted((transients or {}).items()):
        if isinstance(val, dict):
            if not val:
                continue
            n = max(1, len(val.get("added", [])) + len(val.get("removed", [])))
        elif val is None or val == 0 or val == "":
            continue
        elif isinstance(val, int):
            n = val
        else:
            n = 1
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
    """Normalize a possibly hand-edited attempts field, then increment.

    Strict int gate (F4): True would pass isinstance(x, int) and become 2;
    float 4.7 would floor to 4 — both silently regress retry counting.
    Anything that is not exactly an int restarts at 1."""
    attempts = item.get("attempts")
    item["attempts"] = attempts + 1 if type(attempts) is int else 1


def send_webhook(webhook_url, content, queue_path, max_attempts=MAX_ATTEMPTS):
    """POST text to the webhook, SPLIT into chunks within Discord's limit
    (UTF-16 units — F2).

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
                        max_attempts=MAX_ATTEMPTS, uid=None):
    """Single-chunk POST with the bounded retry-queue semantics.

    Each SEND carries a unique nonce (uid) stamped into the payload (F1):
    identity for queue merging is (content, uid) — identical content from a
    DIFFERENT send is a distinct alert and never merges; only the same send
    retried with the same uid merges. Queued rows predating uids have no
    payload["uid"] and therefore never match — always treated as distinct
    (backward compatible)."""
    if uid is None:
        uid = uuid.uuid4().hex
    data = json.dumps({"content": content, "uid": uid}).encode("utf-8")
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
                         and it["payload"].get("content") == content
                         and it["payload"].get("uid") == uid), None)
        if existing is not None:
            _bump_attempts(existing)
            if existing["attempts"] >= max_attempts:
                items.remove(existing)   # drop — surfaced via alive counter
                _bump_drop_counter()
            save_pending(queue_path, items)
        else:
            # the failed POST counts as attempt #1
            append_pending(queue_path,
                           {"content": content, "uid": uid}, attempts=1)
        return False


_dropped_total = 0


def _bump_drop_counter():
    global _dropped_total
    _dropped_total += 1


def get_dropped_total():
    return _dropped_total


def drain_pending(webhook_url, queue_path):
    """Retry queued payloads oldest-first. Returns number actually delivered.
    F8f: non-dict / malformed rows are purged and counted dropped, never crash.

    Durable against mid-drain aborts (F3): after EVERY POST — success,
    failure-bump, or drop — the surviving queue is saved immediately, so an
    abort can only ever re-deliver the in-flight item (at-least-once),
    never re-deliver ones already flushed."""
    items = _split_valid_items(load_pending(queue_path))
    save_pending(queue_path, items)     # persist purge even if nothing valid
    remaining, sent = [], 0
    for idx, item in enumerate(items):
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
                _bump_drop_counter()             # dropped — alive counter
            else:
                remaining.append(item)
        # durable checkpoint after EVERY item (F3): delivered/dropped leave
        # the file now; undelivered (incl. this item's bump) persist now
        save_pending(queue_path, items[idx + 1:] + remaining)
    return sent
