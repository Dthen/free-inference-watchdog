"""Atomic state persistence for the Free Inference Watchdog. Stdlib only.

Crash-safe write ORDER (plan mandate): roster.json FIRST, then pending
alerts, THEN cooldowns — a crash between stages can lose a cooldown but
never silently swallow an alert.
"""

import json
import math
import os
import threading
import time


# ---------- generic atomic write ----------

def _atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per process + thread: a shared '<name>.tmp' let two concurrent
    # writers race — one writer's os.replace moved the other's temp file away,
    # so the loser died on FileNotFoundError (spurious FATAL exit-2 upstream).
    # threading.get_ident() is deliberate belt-and-braces: ticks are
    # single-threaded today, but the temp name stays collision-free if that
    # ever changes.
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)  # atomic rename on local ext4; finality unchanged


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


def save_cooldowns(path, cooldowns, ttl_s=43200, now=None):
    """Prune entries older than TTL before writing (map never grows forever).

    Sanitation happens here at persist (cooldown.filter_cooldown is left as
    is — every persisted map passes through this save): an entry survives
    ONLY if it is a finite number (bool excluded — bool subclasses int) AND
    its age satisfies 0 <= now - v < ttl_s. Infinity/nan and future-dated
    stamps (negative age) are junk that would otherwise suppress alerts
    forever or arbitrarily long; age == ttl_s drops (matches cooldown.py's
    strict <).

    `now` is injectable so ticks (and tests) prune against the tick clock,
    never against a mismatched wall clock.
    """
    now = time.time() if now is None else now
    pruned = {k: v for k, v in cooldowns.items()
              if isinstance(v, (int, float))
              and not isinstance(v, bool)
              and math.isfinite(v)
              and 0 <= now - v < ttl_s}
    _atomic_write_json(path, pruned)


# ---------- pending alerts queue ----------

def load_pending(path):
    data = _load_json_or_default(path, [])
    return data if isinstance(data, list) else []


def append_pending(path, payload, now=None, attempts=0):
    items = load_pending(path)
    items.append({
        "payload": payload,
        "attempts": attempts,
        "first_queued_epoch": now if now is not None else time.time(),
    })
    _atomic_write_json(path, items)


def save_pending(path, items):
    _atomic_write_json(path, items)


# ---------- alive (two clocks + dropped counter) ----------

def load_alive(path):
    """Loaded alive dict, numeric fields validated at the boundary (S5-2).

    last_tick_epoch / last_output_epoch / dropped_alerts_total feed raw
    arithmetic downstream — int/float coerce via int(), anything else
    (str, None, missing) DROPS the field so it reads as absent (callers
    already default absent fields). Non-finite floats are junk too (S1):
    json.load happily parses NaN/Infinity literals, but int(nan) raises
    ValueError and int(inf) OverflowError — a FATAL exit-2 on every tick,
    self-perpetuating because save only runs after this read. They drop
    like any other junk value. Other keys pass through untouched."""
    data = _load_json_or_default(path, {})
    if not isinstance(data, dict):
        return {}
    clean = dict(data)
    for field in ("last_tick_epoch", "last_output_epoch",
                  "dropped_alerts_total"):
        if field in clean and not isinstance(clean[field], bool) \
                and isinstance(clean[field], (int, float)) \
                and math.isfinite(clean[field]):
            clean[field] = int(clean[field])
        else:
            clean.pop(field, None)
    return clean


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

    Acquisition is exclusively os.open(O_CREAT|O_EXCL): create-or-fail is
    atomic, so two processes racing the same stale break can never both
    win. The old exists()->stat()->unlink()->write_text() interleave let a
    second process slip in between "stale file unlinked" and "new file
    written" — both returned True and stamped their pid over each other.
    Now the loser's create fails against the winner's FRESH file and it
    stands down. Exactly one stale-break retry: if that create also hits
    FileExistsError, someone fresher won — live-lock fast-fail unchanged,
    release_lock unchanged.
    """
    now_v = time.time() if now is None else now

    def _create_exclusive():
        # The ONLY acquisition primitive: returns an open fd, or None if
        # the path already exists (atomically — never truncates a peer's
        # lockfile).
        try:
            return os.open(str(lock_path),
                           os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return None

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _create_exclusive()
    if fd is None:
        try:
            age = now_v - lock_path.stat().st_mtime
        except FileNotFoundError:
            age = None  # vanished mid-race (rival broke it): just retry below
        if age is not None and age < LOCK_STALE_S:
            return False  # live lock — fast fail; bytes/mtime untouched
        if age is not None:
            try:
                os.unlink(str(lock_path))  # stale — break it
            except FileNotFoundError:
                pass  # a rival broke it first
        fd = _create_exclusive()  # ONE retry; losing it = fresher acquirer won
        if fd is None:
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return True


def release_lock(lock_path):
    """Best-effort removal. Swallows every OSError, not just FileNotFoundError
    (F6-1): a failed stale-break leaves the lockfile behind, so unlink can
    raise PermissionError in run_tick's finally block — re-raising there would
    discard the FATAL exit-2 return and crash as exit 1 again."""
    try:
        lock_path.unlink()
    except OSError:
        pass
