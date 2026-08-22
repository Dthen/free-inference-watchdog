"""Tests for notify.py — formatting, poison-pill caps, bounded retry queue."""

import json

import notify


# ---------- formatting ----------

def test_format_basic_diff():
    msg = notify.format_alert(
        {"nous": {"added": ["a/new-model"], "removed": ["old/gone"]}},
        tick_iso="2026-08-22 12:00", providers_polled=6,
        transients={}, stale=[], dropped_total=0)
    assert "➕ `a/new-model`" in msg
    assert "➖ `old/gone`" in msg
    assert "2026-08-22 12:00" in msg
    assert len(msg) < 1900


def test_format_caps_bullets_and_counts_rest():
    many = {f"m/model-{i:02d}" for i in range(30)}
    events = {"openrouter": {"added": sorted(many), "removed": []}}
    msg = notify.format_alert(events, tick_iso="t", providers_polled=1,
                              transients={}, stale=[], dropped_total=0)
    assert "…+15 more" in msg
    assert len(msg) < 1900


def test_format_mentions_transients_stale_dropped():
    msg = notify.format_alert(
        {}, tick_iso="t", providers_polled=5,
        transients={"zen": 2}, stale=["nous"], dropped_total=3)
    assert "zen" in msg and "nous" in msg and "3" in msg


def test_format_under_discord_limit_always():
    huge = {f"p{i}": {"added": [f"x/y-{j}" for j in range(40)],
                      "removed": [f"z/w-{j}" for j in range(40)]}
            for i in range(6)}
    msg = notify.format_alert(huge, tick_iso="t", providers_polled=6,
                              transients={}, stale=["ollama"], dropped_total=99)
    assert len(msg) < 2000


# ---------- webhook post + retry queue ----------

class FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"ok"


def _urlopen_ok(req, timeout=10):
    return FakeResp()


def _urlopen_500(req, timeout=10):
    raise notify.UrllibError500()


class UrllibError500(Exception):
    pass


def test_post_success_no_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_urlopen", _urlopen_ok)
    q = tmp_path / "pending.json"
    assert notify.send_webhook("https://hook", "hello", q) is True
    assert notify.load_pending(q) == []


def test_post_failure_enqueues(tmp_path, monkeypatch):
    def boom(req, timeout=10):
        raise OSError("network down")

    monkeypatch.setattr(notify, "_urlopen", boom)
    q = tmp_path / "pending.json"
    assert notify.send_webhook("https://hook", "hello", q) is False
    items = json.loads(q.read_text())
    assert items[0]["payload"]["content"] == "hello"
    assert items[0]["attempts"] == 1


def test_drain_flushes_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_urlopen", _urlopen_ok)
    q = tmp_path / "pending.json"
    notify.append_pending(q, {"content": "stuck"})
    sent = notify.drain_pending("https://hook", q)
    assert sent == 1
    assert notify.load_pending(q) == []


def test_drain_drops_after_five_attempts(tmp_path, monkeypatch):
    def boom(req, timeout=10):
        raise OSError("still down")

    monkeypatch.setattr(notify, "_urlopen", boom)
    q = tmp_path / "pending.json"
    notify.append_pending(q, {"content": "poison"})
    # simulate 4 prior failed attempts already on the item
    items = notify.load_pending(q)
    items[0]["attempts"] = 4
    notify.save_pending(q, items)

    dropped = notify.drain_pending("https://hook", q)
    assert dropped == 0
    remaining = notify.load_pending(q)
    assert remaining == []          # dropped after 5th attempt — never infinite


# ---------- F8f: hand-edited queues with non-dict rows never crash ----------

def _write_queue(path, items):
    path.write_text(json.dumps(items), encoding="utf-8")


def _boom(req, timeout=10):
    raise OSError("network down")


def test_send_webhook_purges_non_dict_items_as_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_urlopen", _boom)
    qpath = tmp_path / "pending.json"
    _write_queue(qpath, ["junk-string", 42,
                         {"payload": {"content": "valid"}, "attempts": 1}])
    before = notify.get_dropped_total()
    assert notify.send_webhook("https://hook", "hello", qpath) is False
    assert notify.get_dropped_total() == before + 2      # junk counted dropped
    items = notify.load_pending(qpath)
    contents = sorted(it["payload"]["content"] for it in items)
    assert contents == ["hello", "valid"]


def test_drain_tolerates_non_dict_queue_items(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_urlopen", _urlopen_ok)
    qpath = tmp_path / "pending.json"
    _write_queue(qpath, [{"payload": {"content": "good"}}, None, ["nested"]])
    before = notify.get_dropped_total()
    sent = notify.drain_pending("https://hook", qpath)
    assert sent == 1                                     # valid item delivered
    assert notify.get_dropped_total() == before + 2      # junk counted dropped
    assert notify.load_pending(qpath) == []


def test_drain_item_with_non_int_attempts_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_urlopen", _boom)
    qpath = tmp_path / "pending.json"
    _write_queue(qpath, [{"payload": {"content": "x"}, "attempts": "two"}])
    sent = notify.drain_pending("https://hook", qpath)
    assert sent == 0
    items = notify.load_pending(qpath)
    assert items and items[0]["attempts"] == 1           # normalized, retained
