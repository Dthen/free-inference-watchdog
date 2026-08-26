"""Tests for notify.py — formatting, poison-pill caps, bounded retry queue."""

import json

import pytest

import notify


# ---------- formatting ----------

def test_format_basic_diff():
    msg = notify.format_alert(
        {"nous": {"added": ["a/new-model"], "removed": ["old/gone"]}},
        tick_iso="2026-08-22 12:00", providers_polled=6,
        transients={}, stale=[], dropped_total=0)
    assert "🟢 `a/new-model`" in msg
    assert "🔴 `old/gone`" in msg
    assert "2026-08-22 12:00" in msg
    assert len(msg) < 1900


def test_format_lists_every_model_no_cap():
    """CHANGE 4: NO cap/truncation-by-default — format_alert lists EVERY
    added/removed model by name under its provider heading. '…+N more' logic
    is gone."""
    many = sorted({f"m/model-{i:02d}" for i in range(30)})
    events = {"openrouter": {"added": many, "removed": []}}
    msg = notify.format_alert(events, tick_iso="t", providers_polled=1,
                              transients={}, stale=[], dropped_total=0)
    for m in many:
        assert f"🟢 `{m}`" in msg
    assert "…+" not in msg


def test_format_mentions_transients_stale_dropped():
    msg = notify.format_alert(
        {}, tick_iso="t", providers_polled=5,
        transients={"zen": 2}, stale=["nous"], dropped_total=3)
    assert "zen" in msg and "nous" in msg and "3" in msg


def test_format_huge_event_uncapped_every_id_survives():
    """CHANGE 4: an alert larger than Discord's limit is NOT truncated at
    format time — every id stays in the text; oversized delivery is handled
    downstream by split_message (sequential chunks)."""
    huge = {f"p{i}": {"added": [f"x/y-{j}" for j in range(40)],
                      "removed": [f"z/w-{j}" for j in range(40)]}
            for i in range(6)}
    msg = notify.format_alert(huge, tick_iso="t", providers_polled=6,
                              transients={}, stale=["zen"], dropped_total=99)
    assert len(msg) >= 1900                       # uncapped by design
    for i in range(6):
        for j in range(40):
            assert f"`x/y-{j}`" in msg and f"`z/w-{j}`" in msg


# ---------- CHANGE 4: Discord 2000-char limit handled by SPLITTING ----------

def test_split_short_message_is_single_chunk():
    assert notify.split_message("hello world") == ["hello world"]
    assert notify.split_message("") == [""]


def test_split_oversized_chunks_bounded_all_ids_survive_footer_last():
    """CHANGE 4 core contract: assembled text >1900 chars splits at section/
    bullet boundaries into sequential messages each <=1900 chars; EVERY model
    id survives across chunks (joined coverage); footer lands on the LAST
    chunk only."""
    events = {f"p{i}": {"added": [f"x/y-{i}-{j}" for j in range(50)],
                        "removed": [f"z/w-{i}-{j}" for j in range(50)]}
              for i in range(6)}
    msg = notify.format_alert(events, tick_iso="2026-08-24 12:00",
                              providers_polled=6, transients={"zen": 2},
                              stale=["kilo"], dropped_total=7)
    assert len(msg) > 1900
    chunks = notify.split_message(msg)
    assert len(chunks) > 1
    assert all(len(c) <= 1900 for c in chunks)
    joined = "\n".join(chunks)
    for i in range(6):
        for j in range(50):
            assert f"`x/y-{i}-{j}`" in joined, f"lost added x/y-{i}-{j}"
            assert f"`z/w-{i}-{j}`" in joined, f"lost removed z/w-{i}-{j}"
    for c in chunks[:-1]:
        assert "—" not in c            # footer marker only on final chunk
    assert "2026-08-24 12:00" in chunks[-1]


def test_split_reconstructs_original_at_line_boundaries():
    """Splitting happens ONLY at line (section/bullet) boundaries — no mid-id
    cuts, no data loss: joining the chunks reproduces the input exactly."""
    text = "\n".join(f"🟢 `provider/model-{i:03d}` trailing detail" for i in range(200))
    chunks = notify.split_message(text, max_chars=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "\n".join(chunks) == text


def test_split_single_oversized_line_never_dropped():
    """A single line longer than the limit cannot be split safely at a
    boundary — it becomes its own chunk rather than being dropped."""
    long_line = "x" * 2500
    chunks = notify.split_message(f"header\n{long_line}\ntail", max_chars=1900)
    assert long_line in chunks
    assert "\n".join(chunks) == f"header\n{long_line}\ntail"


def test_split_default_limit_is_safe_for_discord():
    assert notify.SAFE_CHUNK_CHARS == 1900


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


# ---------- CHANGE 4: delivery loop POSTs split chunks IN ORDER ----------

def test_send_webhook_posts_oversized_alert_as_ordered_chunks(tmp_path,
                                                              monkeypatch):
    """CHANGE 4: an alert >1900 chars is POSTed as sequential chunks in
    order, each within Discord's limit, footer on the last chunk only."""
    posted = []
    events = {f"p{i}": {"added": [f"x/y-{i}-{j}" for j in range(50)],
                        "removed": [f"z/w-{i}-{j}" for j in range(50)]}
              for i in range(6)}
    msg = notify.format_alert(events, tick_iso="2026-08-24 12:00",
                              providers_polled=6, transients={}, stale=[],
                              dropped_total=0)

    def capture(req, timeout=10):
        posted.append(json.loads(req.data.decode("utf-8"))["content"])

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()

    monkeypatch.setattr(notify, "_urlopen", capture)
    assert notify.send_webhook("https://hook", msg, tmp_path / "q") is True
    assert len(posted) == len(notify.split_message(msg))
    assert all(len(c) <= 1900 for c in posted)
    assert "\n".join(posted) == msg                      # order preserved


def test_drain_retries_each_chunk_independently(tmp_path, monkeypatch):
    """CHANGE 4: retry-queue semantics are PER-CHUNK — a chunk that fails is
    retried individually while delivered chunks stay delivered; on failure a
    failed chunk is enqueued as its own pending item."""
    calls = {"n": 0}

    def flaky(req, timeout=10):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("first chunk lost")
        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()

    monkeypatch.setattr(notify, "_urlopen", flaky)
    msg = "\n".join(f"🟢 `p/model-{i}`" for i in range(200))
    chunks = notify.split_message(msg)
    assert len(chunks) > 1
    q = tmp_path / "pending.json"
    ok_all = True
    for c in chunks:                                     # delivery loop
        if not notify.send_webhook("https://hook", c, q):
            ok_all = False
    assert ok_all is False
    items = notify.load_pending(q)
    assert len(items) == 1                               # only chunk #1 queued
    assert items[0]["payload"]["content"] == chunks[0]
    # drain: the lost chunk delivers on retry, nothing else re-sent
    before = calls["n"]
    sent = notify.drain_pending("https://hook", q)
    assert sent == 1 and calls["n"] == before + 1
    assert notify.load_pending(q) == []


# ---------- F1: retry-queue identity — per-send nonce, no content dedup ----------

def _ok_resp():
    class R(FakeResp):
        pass
    return R()


def test_f1_identical_chunks_of_one_alert_stay_distinct(tmp_path, monkeypatch):
    """F1(a): two identical-content chunks of one oversized alert that BOTH
    fail POST must enqueue as TWO independent items — content alone is not
    an identity, so nothing is merged away."""
    monkeypatch.setattr(notify, "_urlopen", _boom)
    q = tmp_path / "pending.json"
    same = "y" * 100
    assert notify._send_webhook_chunk("https://hook", same, q) is False
    assert notify._send_webhook_chunk("https://hook", same, q) is False
    items = notify.load_pending(q)
    assert len(items) == 2
    assert all(it["payload"]["content"] == same for it in items)
    assert [it["attempts"] for it in items] == [1, 1]


def test_f1_week_old_item_never_absorbs_fresh_failure(tmp_path, monkeypatch):
    """F1(b): a week-old queued item with identical content does not merge
    with (nor get attempt-bumped by) this week's identical failed ping."""
    monkeypatch.setattr(notify, "_urlopen", _boom)
    q = tmp_path / "pending.json"
    notify.append_pending(q, {"content": "ping"}, now=1_000_000.0, attempts=3)
    before = notify.get_dropped_total()
    assert notify._send_webhook_chunk("https://hook", "ping", q) is False
    items = notify.load_pending(q)
    assert len(items) == 2
    old = next(it for it in items if it["first_queued_epoch"] == 1_000_000.0)
    fresh = next(it for it in items if it["first_queued_epoch"] != 1_000_000.0)
    assert old["attempts"] == 3 and old["payload"]["content"] == "ping"
    assert fresh["attempts"] == 1
    assert notify.get_dropped_total() == before   # old item untouched


def test_f1_same_uid_retries_still_merge(tmp_path, monkeypatch):
    """F1(c): the SAME send retried (same uid) still merges into one item —
    identity is (content, uid), so genuine re-attempts don't multiply rows.
    Reaching max attempts drops exactly once."""
    monkeypatch.setattr(notify, "_urlopen", _boom)
    q = tmp_path / "pending.json"
    uid = "fixed-uid"
    ok = notify._send_webhook_chunk("https://hook", "dup", q, uid=uid,
                                    max_attempts=3)
    ok &= notify._send_webhook_chunk("https://hook", "dup", q, uid=uid,
                                     max_attempts=3)
    ok &= notify._send_webhook_chunk("https://hook", "dup", q, uid=uid,
                                     max_attempts=3)
    assert ok is False
    items = notify.load_pending(q)
    assert len(items) == 0                        # merged item dropped at 3
    assert notify.get_dropped_total() >= 1


def test_f1_recovery_drains_n_messages_for_n_chunks(tmp_path, monkeypatch):
    """F1(d): end-to-end recovery — N distinct queued chunks deliver as N
    separate messages; none were silently merged while queued."""
    monkeypatch.setattr(notify, "_urlopen", _boom)
    q = tmp_path / "pending.json"
    msg = "\n".join(f"🟢 `p/model-{i}`" for i in range(200))
    chunks = notify.split_message(msg)
    assert len(chunks) > 1
    assert notify.send_webhook("https://hook", msg, q) is False
    queued = notify.load_pending(q)
    assert len(queued) == len(chunks)             # one row per chunk
    monkeypatch.setattr(notify, "_urlopen", _urlopen_ok)
    assert notify.drain_pending("https://hook", q) == len(chunks)
    assert notify.load_pending(q) == []


# ---------- F2: chunk budget measured in UTF-16 code units ----------

def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def test_discord_len_matches_utf16_units():
    assert notify._discord_len("") == 0
    assert notify._discord_len("hello") == 5
    assert notify._discord_len("🟢🔴🔔") == 6      # astral chars count DOUBLE
    assert _u16("🟢🔴🔔") == notify._discord_len("🟢🔴🔔")


def test_split_emoji_mass_alert_every_chunk_within_u16_budget():
    """F2 core: realistic mass-alert text whose Python char count fits but
    whose UTF-16 count exceeds Discord's limit must split so EVERY chunk's
    UTF-16 unit count is <= SAFE_CHUNK_CHARS; joined output is lossless."""
    lines = ["🟢 `m`"] * 300
    text = "\n".join(lines)
    assert len(text) <= notify.SAFE_CHUNK_CHARS            # py-chars fit...
    assert _u16(text) > notify.SAFE_CHUNK_CHARS            # ...but units don't
    chunks = notify.split_message(text)
    assert len(chunks) > 1
    assert all(_u16(c) <= notify.SAFE_CHUNK_CHARS for c in chunks)
    assert "\n".join(chunks) == text                        # lossless join


def test_split_pure_emoji_pathological_bounded():
    # multi-line pathological: every line is pure astral emoji (2 units/char)
    text = "\n".join("🟢🔴" * 400 for _ in range(15))     # 15 x 2400 u16
    chunks = notify.split_message(text)
    assert all(_u16(c) <= notify.SAFE_CHUNK_CHARS for c in chunks)
    assert "\n".join(chunks) == text


def test_split_ascii_golden_byte_identical():
    """F2: ASCII input behaves byte-for-byte as before the UTF-16 change."""
    golden_in = ("alpha\nbeta gamma\ndelta\n" + "x" * 40 + "\nepsilon")
    assert notify.split_message(golden_in, max_chars=20) == [
        "alpha\nbeta gamma",
        "delta",
        "x" * 40,                       # oversize line stays its own chunk
        "epsilon",
    ]
    assert notify.split_message("short ascii") == ["short ascii"]
    assert notify.split_message("") == [""]


def test_split_single_astral_line_own_chunk_not_dropped():
    long_line = "🟢" * 1200                               # 1200 chars, 2400 u16
    chunks = notify.split_message(f"h\n{long_line}\nt", max_chars=1900)
    assert long_line in chunks
    assert "\n".join(chunks) == f"h\n{long_line}\nt"


def test_safe_chunk_units_value():
    assert notify.SAFE_CHUNK_CHARS == 1900               # value unchanged


# ---------- F3: drain durability — save after EVERY successful POST ----------

def test_drain_abort_midway_keeps_only_undelivered(tmp_path, monkeypatch):
    """F3: abort after the 2nd of 3 POSTs must leave EXACTLY the undelivered
    item queued — delivered ones are gone from disk (at-least-once)."""
    calls = {"n": 0}

    def flaky(req, timeout=10):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt()
        return FakeResp()

    monkeypatch.setattr(notify, "_urlopen", flaky)
    q = tmp_path / "pending.json"
    for c in ("first", "second", "third"):
        notify.append_pending(q, {"content": c}, attempts=1)
    with pytest.raises(KeyboardInterrupt):
        notify.drain_pending("https://hook", q)
    remaining = notify.load_pending(q)
    # delivered 'first' is gone; undelivered 'second'+'third' stay queued
    assert [it["payload"]["content"] for it in remaining] == ["second", "third"]


def test_drain_full_success_still_empties_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_urlopen", _urlopen_ok)
    q = tmp_path / "pending.json"
    for c in ("a", "b", "c"):
        notify.append_pending(q, {"content": c})
    assert notify.drain_pending("https://hook", q) == 3
    assert notify.load_pending(q) == []


def test_drain_failed_post_bumps_attempts_and_retains(tmp_path, monkeypatch):
    def boom_first_then_fail(req, timeout=10):
        raise OSError("down")

    monkeypatch.setattr(notify, "_urlopen", boom_first_then_fail)
    q = tmp_path / "pending.json"
    notify.append_pending(q, {"content": "keep"}, attempts=2)
    assert notify.drain_pending("https://hook", q) == 0
    items = notify.load_pending(q)
    assert len(items) == 1
    assert items[0]["attempts"] == 3                      # bumped, retained


# ---------- F4: _bump_attempts strict int gate ----------

@pytest.mark.parametrize("raw,expected", [(True, 1), (2.0, 1), (3, 4)])
def test_bump_attempts_type_gate(raw, expected):
    item = {"payload": {"content": "x"}, "attempts": raw}
    notify._bump_attempts(item)
    assert item["attempts"] == expected


# ---------- F5: format_transient_counts tolerates hand-edited shapes ----------

def test_transient_counts_non_dict_truthy_renders_as_one():
    out = notify.format_transient_counts({"weird": "x", "float": 3.5,
                                          "ok": {"added": ["a"],
                                                 "removed": []},
                                          "num": 4})
    bits = sorted(out.split(", "))
    assert bits == ["float(1)", "num(4)", "ok(1)", "weird(1)"]


def test_transient_counts_none_and_empty_map_skipped():
    assert notify.format_transient_counts({"gone": None}) == ""
    assert notify.format_transient_counts({"empty": {}}) == ""
    assert notify.format_transient_counts({}) == ""
    assert notify.format_transient_counts(None) == ""


def test_transient_counts_int_passthrough_alive_consumer():
    """alive.format_alive consumes this — signature/shape unchanged."""
    assert notify.format_transient_counts({"zen": 2}) == "zen(2)"
