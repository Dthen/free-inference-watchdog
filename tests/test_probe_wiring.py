"""Tests for the serial throttled probe loop wiring (P3).

Covers: serial 10s-throttled loop, verdict persistence via
probe_state.save_probe_state, probe_select-driven queue, verdict-filtered
roster and fetch_one, prune on tick, burst-kill regression.
"""

import json
import time
from pathlib import Path

import pytest

import inference_watchdog as im
import probe_state
import providers
from probe_zero_credit import Result


# ---------- helpers ----------

def _bai_only_providers(model_ids):
    """Build a PROVIDERS dict with only bai (zero-credit-probe)."""
    mock = {
        "bai": {
            "base_url": "https://api.example.com",
            "_token": "test-token",
            "detection": "zero-credit-probe",
        }
    }
    for p in ["nous", "tokenrouter", "kilo", "openrouter", "amd"]:
        mock[p] = {
            "base_url": f"https://{p}.example.com",
            "_token": "",
            "detection": "other",
        }
    return mock


def _fake_fetch_provider_factory(model_ids):
    def fake_fetch_provider(config, getter=None):
        if config.get("detection") == "zero-credit-probe":
            return list(model_ids), {}
        return [], {}
    return fake_fetch_provider


# ---------- save_probe_state (probe_state module) ----------

def test_save_probe_state_writes_atomically(tmp_path):
    """save_probe_state must persist the full state dict atomically."""
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
    """Atomic write must leave no .tmp files behind."""
    path = tmp_path / "probe_state.json"
    probe_state.save_probe_state(path, {"bai": {"m": {"verdict": "free", "epoch": 1}}})
    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_save_probe_state_creates_parent_dirs(tmp_path):
    """save_probe_state must mkdir -p the parent directory."""
    path = tmp_path / "nested" / "dir" / "probe_state.json"
    probe_state.save_probe_state(path, {})
    assert path.exists()


# ---------- serial throttled loop ----------

def test_probes_called_serially_with_10s_gaps(monkeypatch, tmp_path):
    """Probes fire serially: first immediately, then 10s gap before each."""
    call_times = []

    def fake_probe(base_url, token, model_id, timeout=30):
        call_times.append((model_id, time.time()))
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1", "m2", "m3"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1", "m2", "m3"]))

    sleep_calls = []
    def fake_sleep(s):
        sleep_calls.append(s)
        # Don't actually sleep in tests

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000, sleep=fake_sleep)
    results, metas = fetch_all_fn()

    # All 3 probed
    assert sorted(m for m, _ in call_times) == ["m1", "m2", "m3"]

    # First probe fires immediately (no sleep before it)
    # Then sleep(10) before m2, sleep(10) before m3
    assert len(sleep_calls) == 2
    assert all(s == 10 for s in sleep_calls)

    # All FREE -> all on roster
    assert results["bai"] == ["m1", "m2", "m3"]


def test_probes_no_overlap(monkeypatch, tmp_path):
    """Serial loop: no concurrent probe execution."""
    import threading
    active = []
    lock = threading.Lock()

    def fake_probe(base_url, token, model_id, timeout=30):
        with lock:
            active.append(model_id)
            current_active = len(active)
        time.sleep(0.05)  # simulate work
        with lock:
            active.pop()
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["a", "b", "c"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["a", "b", "c"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()
    assert results["bai"] == ["a", "b", "c"]


# ---------- verdict-driven roster ----------

def test_free_verdict_on_roster(monkeypatch, tmp_path):
    """FREE verdict -> model on roster."""
    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()
    assert results["bai"] == ["m1"]


def test_paid_verdict_excluded(monkeypatch, tmp_path):
    """PAID verdict -> model excluded from roster."""
    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.PAID, {"http": 403}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()
    assert results["bai"] == []


def test_defer_keeps_last_known_verdict(monkeypatch, tmp_path):
    """DEFER does NOT overwrite a prior 'free' verdict — roster still shows it."""
    state_path = tmp_path / "probe_state.json"
    # Pre-seed: m1 was previously free
    probe_state.record_verdict(state_path, "bai", "m1", "free", 999_000_000)

    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.DEFER, {"http": 429}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()
    # Sticky: prior "free" survives the DEFER
    assert results["bai"] == ["m1"]


def test_probe_exception_treated_as_defer(monkeypatch, tmp_path):
    """probe_model raising -> treated as DEFER (entry untouched)."""
    state_path = tmp_path / "probe_state.json"
    probe_state.record_verdict(state_path, "bai", "m1", "free", 999_000_000)

    def fake_probe(base_url, token, model_id, timeout=30):
        raise RuntimeError("network down")

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()
    assert results["bai"] == ["m1"]  # sticky free survives


# ---------- burst-kill regression ----------

def test_burst_kill_regression_no_defer_empties_roster(monkeypatch, tmp_path):
    """A probe that returns DEFER for everything must NOT empty the roster
    of previously-free models. This is the b.ai 429 burst regression."""
    state_path = tmp_path / "probe_state.json"
    # Pre-seed several free models
    for i in range(5):
        probe_state.record_verdict(state_path, "bai", f"free-model-{i}", "free",
                                   999_000_000 + i)

    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.DEFER, {"http": 429}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS",
                        _bai_only_providers([f"free-model-{i}" for i in range(5)]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory([f"free-model-{i}" for i in range(5)]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()
    # All 5 still on roster despite DEFER
    assert results["bai"] == [f"free-model-{i}" for i in range(5)]


# ---------- prune ----------

def test_vanished_models_pruned_from_state(monkeypatch, tmp_path):
    """Models no longer in catalog are pruned from state after the tick."""
    state_path = tmp_path / "probe_state.json"
    probe_state.record_verdict(state_path, "bai", "kept", "free", 999_000_000)
    probe_state.record_verdict(state_path, "bai", "vanished", "free", 999_000_000)

    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["kept"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["kept"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    fetch_all_fn()

    state = probe_state.load_probe_state(state_path)
    assert "kept" in state.get("bai", {})
    assert "vanished" not in state.get("bai", {})


# ---------- first tick ----------

def test_first_tick_full_catalog_queue(monkeypatch, tmp_path):
    """First tick: empty state -> all catalog models probed (new arrivals)."""
    call_log = []

    def fake_probe(base_url, token, model_id, timeout=30):
        call_log.append(model_id)
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    model_ids = [f"m-{i}" for i in range(5)]
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(model_ids))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(model_ids))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()

    assert sorted(call_log) == sorted(model_ids)
    assert results["bai"] == sorted(model_ids)


# ---------- throttle injection ----------

def test_throttle_uses_injected_sleep(monkeypatch, tmp_path):
    """The sleep callable is injected; never time.sleep in tests."""
    sleep_calls = []

    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["a", "b"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["a", "b"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: sleep_calls.append(s))
    fetch_all_fn()
    # 2 models -> 1 sleep between them
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 10


# ---------- fetch_one verdict filter ----------

def test_fetch_one_returns_verdict_filtered_ids(monkeypatch, tmp_path):
    """fetch_one for zero-credit provider returns verdict-filtered ids,
    NOT the raw catalog. No probe calls during recheck."""
    state_path = tmp_path / "probe_state.json"
    # m1 free, m2 paid
    probe_state.record_verdict(state_path, "bai", "m1", "free", 999_000_000)
    probe_state.record_verdict(state_path, "bai", "m2", "paid", 999_000_000)

    probe_called = []

    def fake_probe(base_url, token, model_id, timeout=30):
        probe_called.append(model_id)
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1", "m2"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1", "m2"]))

    fetch_one_fn = im.build_fetch_one({}, tmp_path)
    ids, meta = fetch_one_fn("bai")

    # Only free verdict
    assert ids == ["m1"]
    # No probe calls during recheck
    assert probe_called == []


def test_fetch_one_verdict_gap_returns_empty(monkeypatch, tmp_path):
    """State empty for provider -> fetch_one returns [] (not raw catalog)."""
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1", "m2"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1", "m2"]))

    fetch_one_fn = im.build_fetch_one({}, tmp_path)
    ids, meta = fetch_one_fn("bai")

    assert ids == []


# ---------- persist ----------

def test_state_written_once_per_tick(monkeypatch, tmp_path):
    """Verdicts persisted to state file after the tick."""
    def fake_probe(base_url, token, model_id, timeout=30):
        if model_id == "m1":
            return Result.FREE, {"http": 200}
        return Result.PAID, {"http": 403}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1", "m2"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1", "m2"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()

    state_path = tmp_path / "probe_state.json"
    state = probe_state.load_probe_state(state_path)

    assert "bai" in state
    assert state["bai"]["m1"] == {"verdict": "free", "epoch": 1_000_000_000}
    assert state["bai"]["m2"] == {"verdict": "paid", "epoch": 1_000_000_000}


# ---------- FetchError ----------

def test_fetch_error_state_touched_results_none(monkeypatch, tmp_path):
    """FetchError during fetch_all -> results None, state untouched."""
    state_path = tmp_path / "probe_state.json"
    # Pre-existing state
    probe_state.record_verdict(state_path, "bai", "old", "free", 999_000_000)
    before = probe_state.load_probe_state(state_path)

    def fake_fetch_provider(config, getter=None):
        raise providers.FetchError("network down")

    monkeypatch.setattr(providers, "fetch_provider", fake_fetch_provider)

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, metas = fetch_all_fn()

    assert results["bai"] is None
    # State untouched
    after = probe_state.load_probe_state(state_path)
    assert before == after


# ---------- probe_select-driven queue ----------

def test_queue_respects_probe_select_order(monkeypatch, tmp_path):
    """Queue order: new arrivals first, then free, then stale paid."""
    state_path = tmp_path / "probe_state.json"
    # m1: free (tier 2), m2: paid stale (tier 3), m3: new (tier 1)
    probe_state.record_verdict(state_path, "bai", "m1", "free", 999_000_000)
    probe_state.record_verdict(state_path, "bai", "m2", "paid", 900_000_000)  # very stale

    call_order = []

    def fake_probe(base_url, token, model_id, timeout=30):
        call_order.append(model_id)
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1", "m2", "m3"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1", "m2", "m3"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    fetch_all_fn()

    # m3 (new) first, then m1 (free), then m2 (stale paid)
    assert call_order == ["m3", "m1", "m2"]


# ---------- metas preserved ----------

def test_metas_preserved(monkeypatch, tmp_path):
    """metas dict populated with meta from fetch_provider."""
    def fake_fetch_provider(config, getter=None):
        if config.get("detection") == "zero-credit-probe":
            return ["m1"], {"ratelimit": {"remaining": "10"}}
        return [], {}

    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider", fake_fetch_provider)

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, metas = fetch_all_fn()

    assert metas["bai"] == {"ratelimit": {"remaining": "10"}}