"""Tests for probe_state.py — persisted zero-credit probe verdicts."""

import json
import pytest

import probe_state


def test_record_and_get_verdict(tmp_path):
    # record free verdict, read it back
    path = tmp_path / "probe_state.json"
    now = 1789259000

    probe_state.record_verdict(path, "bai", "glm-5.3-flash", "free", now)
    state = probe_state.load_probe_state(path)

    verdict, epoch = probe_state.get_verdict(state, "bai", "glm-5.3-flash")
    assert verdict == "free"
    assert epoch == now

    # paid verdict
    now2 = 1789259100
    probe_state.record_verdict(path, "bai", "gpt-5.6-sol", "paid", now2)
    state = probe_state.load_probe_state(path)

    verdict, epoch = probe_state.get_verdict(state, "bai", "gpt-5.6-sol")
    assert verdict == "paid"
    assert epoch == now2


def test_verdict_survives_reload(tmp_path):
    # record, load fresh, verdict still there
    path = tmp_path / "probe_state.json"
    now = 1789259000

    probe_state.record_verdict(path, "bai", "glm-5.3-flash", "free", now)

    # Load fresh
    state = probe_state.load_probe_state(path)
    verdict, epoch = probe_state.get_verdict(state, "bai", "glm-5.3-flash")
    assert verdict == "free"
    assert epoch == now


def test_defer_never_recorded_as_verdict(tmp_path):
    # attempting to record verdict='defer' raises ValueError
    path = tmp_path / "probe_state.json"
    now = 1789259000

    with pytest.raises(ValueError):
        probe_state.record_verdict(path, "bai", "glm-5.3-flash", "defer", now)


def test_corrupt_file_degrades_to_empty(tmp_path):
    # write garbage, load -> {}
    path = tmp_path / "probe_state.json"
    path.write_text("{not json!!", encoding="utf-8")

    state = probe_state.load_probe_state(path)
    assert state == {}


def test_drop_missing_prunes_vanished_models(tmp_path):
    # record 3 models, drop_missing with 2-model catalog, 3rd pruned
    path = tmp_path / "probe_state.json"
    now = 1789259000

    probe_state.record_verdict(path, "bai", "model-a", "free", now)
    probe_state.record_verdict(path, "bai", "model-b", "paid", now)
    probe_state.record_verdict(path, "bai", "model-c", "free", now)

    state = probe_state.load_probe_state(path)
    pruned = probe_state.drop_missing(state, "bai", ["model-a", "model-b"])

    # model-c should be pruned
    verdict, epoch = probe_state.get_verdict(pruned, "bai", "model-a")
    assert verdict == "free"
    assert epoch == now

    verdict, epoch = probe_state.get_verdict(pruned, "bai", "model-b")
    assert verdict == "paid"
    assert epoch == now

    verdict, epoch = probe_state.get_verdict(pruned, "bai", "model-c")
    assert verdict is None
    assert epoch is None


def test_missing_file_returns_empty(tmp_path):
    # no file -> {}
    path = tmp_path / "nonexistent.json"
    state = probe_state.load_probe_state(path)
    assert state == {}


def test_get_verdict_never_probed_returns_none(tmp_path):
    path = tmp_path / "probe_state.json"
    now = 1789259000
    probe_state.record_verdict(path, "bai", "model-a", "free", now)

    state = probe_state.load_probe_state(path)
    verdict, epoch = probe_state.get_verdict(state, "bai", "never-probed")
    assert verdict is None
    assert epoch is None


def test_defer_epoch_recorded_with_verdict(tmp_path):
    # defer_epoch rides along but doesn't overwrite verdict
    path = tmp_path / "probe_state.json"
    now = 1789259000
    defer_epoch = 1789259100

    probe_state.record_verdict(
        path, "bai", "kimi-k3", "paid", now, defer_epoch=defer_epoch
    )
    state = probe_state.load_probe_state(path)

    verdict, epoch = probe_state.get_verdict(state, "bai", "kimi-k3")
    assert verdict == "paid"
    assert epoch == now

    # defer_epoch should be in the stored state
    assert state["bai"]["kimi-k3"]["defer_epoch"] == defer_epoch


def test_record_verdict_atomic_write(tmp_path):
    # No temp files left behind
    path = tmp_path / "probe_state.json"
    now = 1789259000

    probe_state.record_verdict(path, "bai", "model-a", "free", now)
    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_multiple_providers_isolated(tmp_path):
    # bai and nous states don't interfere
    path = tmp_path / "probe_state.json"
    now = 1789259000

    probe_state.record_verdict(path, "bai", "model-a", "free", now)
    probe_state.record_verdict(path, "nous", "model-b", "paid", now)

    state = probe_state.load_probe_state(path)

    verdict, epoch = probe_state.get_verdict(state, "bai", "model-a")
    assert verdict == "free"
    assert epoch == now

    verdict, epoch = probe_state.get_verdict(state, "nous", "model-b")
    assert verdict == "paid"
    assert epoch == now

    verdict, epoch = probe_state.get_verdict(state, "bai", "model-b")
    assert verdict is None