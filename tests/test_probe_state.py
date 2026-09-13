"""Tests for probe_state.py — persisted zero-credit probe verdicts."""

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


def test_record_verdict_survives_corrupt_provider_value(tmp_path):
    # A corrupt provider value (non-dict) must not crash the write path:
    # without the isinstance guard this raises TypeError and FATALs every
    # subsequent tick, breaking the module's "never fatal" contract.
    path = tmp_path / "probe_state.json"
    path.write_text('{"bai": "junk"}', encoding="utf-8")

    probe_state.record_verdict(path, "bai", "model-1", "free", 1000)

    state = probe_state.load_probe_state(path)
    verdict, epoch = probe_state.get_verdict(state, "bai", "model-1")
    assert verdict == "free"
    assert epoch == 1000


@pytest.mark.parametrize("junk_epoch", ['"abc"', "null", "true", "NaN"])
def test_get_verdict_junk_epoch_degrades(tmp_path, junk_epoch):
    # Mirror state.py load_alive: epoch is validated at the read boundary —
    # non-numeric/non-finite (junk from a hand-edited file) must never reach
    # callers; it degrades to None while the verdict still reads.
    path = tmp_path / "probe_state.json"
    path.write_text(
        '{"bai": {"m": {"verdict": "free", "epoch": %s}}}' % junk_epoch,
        encoding="utf-8",
    )

    state = probe_state.load_probe_state(path)
    verdict, epoch = probe_state.get_verdict(state, "bai", "m")
    assert verdict == "free"
    assert epoch is None


def test_save_probe_state_writes_atomically(tmp_path):
    # save_probe_state persists the full state dict atomically
    path = tmp_path / "probe_state.json"
    state = {
        "bai": {
            "glm-5.3-flash": {"verdict": "free", "epoch": 1000},
            "gpt-5.6-sol": {"verdict": "paid", "epoch": 1001},
        }
    }
    probe_state.save_probe_state(path, state)
    loaded = probe_state.load_probe_state(path)
    assert loaded == state


def test_save_probe_state_no_temp_files_left(tmp_path):
    # Atomic write must leave no .tmp files behind
    path = tmp_path / "probe_state.json"
    probe_state.save_probe_state(path,
                                {"bai": {"m": {"verdict": "free", "epoch": 1}}})
    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_save_probe_state_creates_parent_dirs(tmp_path):
    # save_probe_state must mkdir -p the parent directory
    path = tmp_path / "nested" / "dir" / "probe_state.json"
    probe_state.save_probe_state(path, {})
    assert path.exists()


def test_save_probe_state_non_dict_input_writes_empty(tmp_path):
    # Non-dict state degrades to {} (never fatal)
    path = tmp_path / "probe_state.json"
    probe_state.save_probe_state(path, "junk")
    loaded = probe_state.load_probe_state(path)
    assert loaded == {}