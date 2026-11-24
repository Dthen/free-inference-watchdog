"""Tests for probe_state.py — persisted zero-credit probe verdicts."""

import pytest

import probe_state


def test_save_load_roundtrip_and_lookup(tmp_path):
    # One real save/load cycle covers persistence, reload, lookup and
    # provider isolation; the tick's in-memory dict is the written payload.
    path = tmp_path / "probe_state.json"
    state = {
        "bai": {
            "glm-5.3-flash": {"verdict": "free", "epoch": 1789259000},
            "gpt-5.6-sol": {"verdict": "paid", "epoch": 1789259100},
        },
        "nous": {
            "model-b": {"verdict": "free", "epoch": 1789259200},
        },
    }

    probe_state.save_probe_state(path, state)

    assert probe_state.load_probe_state(path) == state
    assert probe_state.get_verdict(state, "bai", "glm-5.3-flash") == \
        ("free", 1789259000)
    assert probe_state.get_verdict(state, "bai", "gpt-5.6-sol") == \
        ("paid", 1789259100)
    assert probe_state.get_verdict(state, "nous", "model-b") == \
        ("free", 1789259200)
    # An id under one provider never answers for another.
    assert probe_state.get_verdict(state, "bai", "model-b") == (None, None)
    assert probe_state.get_verdict(state, "nous", "glm-5.3-flash") == \
        (None, None)


def test_missing_corrupt_and_non_dict_files_degrade_to_empty(tmp_path):
    # Absent file, corrupt JSON and JSON that is not an object all read {}.
    assert probe_state.load_probe_state(tmp_path / "absent.json") == {}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json!!", encoding="utf-8")
    assert probe_state.load_probe_state(corrupt) == {}

    non_dict = tmp_path / "non_dict.json"
    non_dict.write_text("[1, 2, 3]", encoding="utf-8")
    assert probe_state.load_probe_state(non_dict) == {}


def test_get_verdict_never_probed_returns_none():
    state = {"bai": {"model-a": {"verdict": "free", "epoch": 1000}}}

    assert probe_state.get_verdict(state, "bai", "never-probed") == (None, None)
    assert probe_state.get_verdict(state, "unknown", "model-a") == (None, None)


def test_get_verdict_malformed_state_degrades():
    # Junk shapes from a hand-edited file read as "never probed".
    assert probe_state.get_verdict("junk", "bai", "m") == (None, None)
    assert probe_state.get_verdict({"bai": "junk"}, "bai", "m") == (None, None)
    assert probe_state.get_verdict({"bai": {"m": [1, 2]}}, "bai", "m") == \
        (None, None)
    assert probe_state.get_verdict(
        {"bai": {"m": {"verdict": "defer", "epoch": 1000}}}, "bai", "m"
    ) == (None, None)


def test_drop_missing_prunes_vanished_models():
    state = {
        "bai": {
            "model-a": {"verdict": "free", "epoch": 1000},
            "model-b": {"verdict": "paid", "epoch": 1000},
            "model-c": {"verdict": "free", "epoch": 1000},
        }
    }

    pruned = probe_state.drop_missing(state, "bai", ["model-a", "model-b"])

    assert pruned["bai"] == {
        "model-a": {"verdict": "free", "epoch": 1000},
        "model-b": {"verdict": "paid", "epoch": 1000},
    }
    # Pruning returns a new dict; the loaded state is never mutated.
    assert "model-c" in state["bai"]


@pytest.mark.parametrize("junk_epoch", ["abc", None, True, float("nan")])
def test_get_verdict_junk_epoch_degrades(junk_epoch):
    # Mirror state.py load_alive: epoch is validated at the read boundary —
    # non-numeric/non-finite (junk from a hand-edited file) must never reach
    # callers; it degrades to None while the verdict still reads.
    state = {"bai": {"m": {"verdict": "free", "epoch": junk_epoch}}}

    assert probe_state.get_verdict(state, "bai", "m") == ("free", None)


def test_save_probe_state_non_dict_input_writes_empty(tmp_path):
    # Non-dict state degrades to {} (never fatal)
    path = tmp_path / "probe_state.json"
    probe_state.save_probe_state(path, "junk")
    loaded = probe_state.load_probe_state(path)
    assert loaded == {}