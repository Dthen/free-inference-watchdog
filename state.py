"""Atomic state persistence for the Free Inference Monitor. Stdlib only.

Crash-safe write ORDER (plan mandate): roster.json FIRST, then pending
alerts, THEN cooldowns — a crash between stages can lose a cooldown but
never silently swallow an alert.
"""

import json
import os
import time


# ---------- generic atomic write ----------

def _atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)  # atomic on local ext4


def _load_json_or_default(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default
    return data


# ---------- roster ----------

def load_roster(path):
    """Parsed roster dict, or None if missing/corrupt/not-an-object."""
    data = _load_json_or_default(path, None)
    return data if isinstance(data, dict) else None


def save_roster_atomic(path, data):
    _atomic_write_json(path, data)


# ---------- cooldowns ----------

def load_cooldowns(path):
    data = _load_json_or_default(path, {})
    return data if isinstance(data, dict) else {}


def save_cooldowns(path, cooldowns, ttl_s=43200):
    """Prune entries older than TTL before writing (map never grows forever)."""
    now = time.time()
    pruned = {k: v for k, v in cooldowns.items()
              if isinstance(v, (int, float)) and now - v < ttl_s}
    _atomic_write_json(path, pruned)


# ---------- pending alerts queue ----------

def load_pending(path):
    data = _load_json_or_default(path, [])
    return data if isinstance(data, list) else []


def append_pending(path, payload, now=None):
    items = load_pending(path)
    items.append({
        "payload": payload,
        "attempts": 0,
        "first_queued_epoch": now if now is not None else time.time(),
    })
    _atomic_write_json(path, items)


def save_pending(path, items):
    _atomic_write_json(path, items)


# ---------- alive (two clocks + dropped counter) ----------

def load_alive(path):
    data = _load_json_or_default(path, {})
    return data if isinstance(data, dict) else {}


def save_alive(path, last_tick_epoch, last_output_epoch, dropped_alerts_total=0):
    _atomic_write_json(path, {
        "last_tick_epoch": last_tick_epoch,
        "last_output_epoch": last_output_epoch,
        "dropped_alerts_total": dropped_alerts_total,
    })


# ---------- PID lockfile ----------

LOCK_STALE_S = 30 * 60  # 30 min — worst tick is ~5 min, so 30 is generous


def acquire_lock(lock_path, now=None):
    """True + hold the lock, or False if a LIVE lock exists (never wait).

    Stale locks (>30 min mtime) are broken — crash recovery.
    """
    if lock_path.exists():
        age = (now if now is not None else time.time()) - lock_path.stat().st_mtime
        if age < LOCK_STALE_S:
            return False
        lock_path.unlink()  # stale — break it
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock(lock_path):
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
