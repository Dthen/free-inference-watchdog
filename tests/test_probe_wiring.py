"""Tests for the serial throttled probe loop wiring (P3).

Covers: serial 10s-throttled loop, verdict persistence via
probe_state.save_probe_state, probe_select-driven queue, verdict-filtered
roster and fetch_one, prune on tick, burst-kill regression, dry-run purity,
junk-safe verdict reads, probe-phase budget cap, no-op save skip.
"""

import time

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
    """Serial loop: no concurrent probe execution — each probe starts only
    after the previous finished (strict start/end alternation, max active 1).
    P3 minor: the old version computed active-count machinery but never
    asserted it."""
    events = []

    def fake_probe(base_url, token, model_id, timeout=30):
        events.append(("start", model_id))
        time.sleep(0.05)  # simulate work
        events.append(("end", model_id))
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["a", "b", "c"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["a", "b", "c"]))

    fetch_all_fn = im.build_fetch_all({}, tmp_path, now=1_000_000_000,
                                       sleep=lambda s: None)
    results, _ = fetch_all_fn()
    assert results["bai"] == ["a", "b", "c"]
    kinds = [e[0] for e in events]
    assert kinds == ["start", "end"] * 3, \
        f"probes overlapped or mis-sequenced: {events}"


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


# ---------- dry-run purity (P3 IMPORTANT 1) ----------


def test_dry_run_does_not_write_probe_state(monkeypatch, tmp_path):
    """--dry-run must leave probe_state.json ABSENT on a fresh state-dir."""
    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1"]))

    fetch_all_fn = im.build_fetch_all(
        {}, tmp_path, now=1_000_000_000, sleep=lambda s: None, dry_run=True)
    fetch_all_fn()
    assert not (tmp_path / "probe_state.json").exists()


def test_dry_run_leaves_pre_existing_state_untouched(monkeypatch, tmp_path):
    """--dry-run must NOT overwrite a pre-existing probe_state.json."""
    state_path = tmp_path / "probe_state.json"
    probe_state.record_verdict(state_path, "bai", "m1", "free", 999_000_000)

    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.PAID, {"http": 403}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1"]))

    fetch_all_fn = im.build_fetch_all(
        {}, tmp_path, now=1_000_000_000, sleep=lambda s: None, dry_run=True)
    fetch_all_fn()

    # Pre-existing state untouched
    loaded = probe_state.load_probe_state(state_path)
    assert loaded == {"bai": {"m1": {"verdict": "free", "epoch": 999_000_000}}}


# ---------- junk-safe verdict reads (P3 IMPORTANT 2) ----------


def test_junk_provider_degrades_to_empty_roster(monkeypatch, tmp_path):
    """A junk provider value (non-dict) must NOT crash the tick — roster
    degrades to [], verdict-recording still works (write path self-heals)."""
    state_path = tmp_path / "probe_state.json"
    # Write a junk provider value directly
    state_path.write_text('{"bai": "junk"}', encoding="utf-8")

    def fake_probe(base_url, token, model_id, timeout=30):
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1"]))

    fetch_all_fn = im.build_fetch_all(
        {}, tmp_path, now=1_000_000_000, sleep=lambda s: None)
    # Must not crash with AttributeError
    results, _ = fetch_all_fn()
    # Degraded roster
    assert results["bai"] == []
    # Verdict still recorded (write path self-heals via get_verdict)
    loaded = probe_state.load_probe_state(state_path)
    assert loaded.get("bai", {}).get("m1", {}).get("verdict") == "free"


def test_junk_model_entry_excluded(monkeypatch, tmp_path):
    """A junk model entry (non-dict) must NOT crash — excluded from roster
    when the probe doesn't record a verdict (e.g. DEFER). P3 IMPORTANT 2:
    get_verdict at the read boundary handles every junk shape."""
    state_path = tmp_path / "probe_state.json"
    # m1 is a list (junk), m2 is a valid free entry
    state_path.write_text(
        '{"bai": {"m1": [1, 2], "m2": {"verdict": "free", "epoch": 999}}}',
        encoding="utf-8")

    def fake_probe(base_url, token, model_id, timeout=30):
        # DEFER — entry untouched, junk stays junk -> excluded via get_verdict
        return Result.DEFER, {"http": 429}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1", "m2"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1", "m2"]))

    fetch_all_fn = im.build_fetch_all(
        {}, tmp_path, now=1_000_000_000, sleep=lambda s: None)
    # Must not crash
    results, _ = fetch_all_fn()
    # m2 free, m1 excluded (junk entry untouched by DEFER)
    assert "m1" not in results["bai"]
    assert "m2" in results["bai"]


# ---------- probe-phase budget cap (P3 IMPORTANT 3) ----------


def test_probe_phase_budget_caps_probes(monkeypatch, tmp_path):
    """Probe phase is wall-clock bounded — remaining queue items stay
    unprobed when the budget would be exceeded."""
    probe_calls = []

    def fake_probe(base_url, token, model_id, timeout=30):
        probe_calls.append(model_id)
        return Result.FREE, {"http": 200}

    # Inject a clock that advances by 150s per probe to consume the 900s budget
    clock = {"t": 0}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(
        [f"m{i}" for i in range(10)]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(
                            [f"m{i}" for i in range(10)]))

    # Patch time.time in the module to consume budget
    import inference_watchdog
    orig_time = inference_watchdog.time.time
    def advancing_time():
        val = clock["t"]
        clock["t"] += 150  # each probe "takes" 150s of budget
        return val
    inference_watchdog.time.time = advancing_time

    try:
        fetch_all_fn = im.build_fetch_all(
            {}, tmp_path, now=1_000_000_000, sleep=lambda s: None)
        results, _ = fetch_all_fn()
    finally:
        inference_watchdog.time.time = orig_time

    # Budget 900s, 150s per probe. The budget check fires BEFORE each probe
    # (including the first), so when elapsed >= 900 the probe is skipped.
    # probe_phase_start captures t=0, then clock advances to 150.
    # Probe 1: elapsed=150 < 900, fires. ... Probe 5: elapsed=750 < 900,
    # fires. Probe 6: elapsed=900 >= 900, SKIPPED (exactly at the boundary).
    # So 5 probes fire.
    assert len(probe_calls) == 5, (
        f"expected 5 probes (budget), got {len(probe_calls)}: {probe_calls}")
    assert len(results["bai"]) == 5


def test_probe_phase_budget_skipped_providers_logged(monkeypatch, tmp_path,
                                                      capsys):
    """When budget truncates, a one-line stderr note names the provider and
    count skipped."""
    probe_calls = []
    clock = {"t": 0}

    def fake_probe(base_url, token, model_id, timeout=30):
        probe_calls.append(model_id)
        return Result.FREE, {"http": 200}

    import inference_watchdog
    orig_time = inference_watchdog.time.time
    def advancing_time():
        val = clock["t"]
        clock["t"] += 300  # each probe "takes" 300s of budget
        return val
    inference_watchdog.time.time = advancing_time

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(
        [f"m{i}" for i in range(5)]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(
                            [f"m{i}" for i in range(5)]))

    try:
        fetch_all_fn = im.build_fetch_all(
            {}, tmp_path, now=1_000_000_000, sleep=lambda s: None)
        fetch_all_fn()
    finally:
        inference_watchdog.time.time = orig_time

    # 300s per probe, budget 900s: probe 1 at elapsed=300 < 900 fires,
    # probe 2 at elapsed=600 < 900 fires, probe 3 at elapsed=900 >= 900
    # skipped (3 remaining).
    assert len(probe_calls) == 2
    err = capsys.readouterr().err
    assert "bai" in err
    assert "3 remaining" in err


def test_probe_phase_budget_regression_pin():
    """REGRESSION PIN: PROBE_PHASE_BUDGET_S must stay <= 1200s.

    The budget clock spans fetches + probes from the TOP of fetch_all()
    (probe_phase_start is captured BEFORE the fetch loop — catalog fetches
    run INSIDE the budget, so don't add ~30s on top). The check fires
    before each probe, so the last probe can start at budget-0.1s and run
    sleep(10) + 30s timeout ≈ 40s more: worst case budget+40s from
    fetch_all() start to save_probe_state (the PERSIST point).

    The live constraint is the Hermes cron runner's script window
    (cron.script_timeout_seconds = 1800s since 2026-09-13, deployment
    config outside this repo). The full tick after the persist point
    still carries confirm_diffs' unconditional 180s recheck nap +
    re-fetches, so the ceiling must leave room: 1200 + 40 (overshoot) +
    ~180 (recheck) + slack stays inside 1800s.

    History: the original 240s pin existed because the runner SIGKILLed
    the wrapper at 300s (observed 3 consecutive ticks die mid-probe-loop
    on 2026-09-13 — nothing persisted, death spiral). That kill is gone;
    the 240-era pin is superseded by the 1800s runner. 900s is the value
    P3 originally chose (commit e4ec690) and lets the full 47-model b.ai
    pass (~8 min) finish in ONE tick. A bump past ~1200s reintroduces
    mid-loop kill risk regardless of the configured timeout: the persist
    point — save_probe_state — must always precede the runner window.
    """
    assert im.PROBE_PHASE_BUDGET_S <= 1200, (
        f"PROBE_PHASE_BUDGET_S={im.PROBE_PHASE_BUDGET_S} exceeds 1200s — "
        "persist point (budget + ~40s overshoot) plus the 180s recheck nap "
        "must stay inside the 1800s runner window. "
        "See the comment on PROBE_PHASE_BUDGET_S in inference_watchdog.py.")


def test_probe_phase_budget_truncated_tick_persists_subset(monkeypatch,
                                                             tmp_path):
    """Budget-truncated tick PERSISTS the probed subset — the property that
    makes truncation safe instead of a death spiral.

    When the budget cuts the queue mid-way, the verdicts gathered so far
    must still be saved (save_probe_state runs at END of build_fetch_all,
    BEFORE run_tick's confirm_diffs). The next tick then finds the skipped
    models still queued (no verdict => new arrival) and finishes them —
    progress accumulates instead of resetting.
    """
    probe_calls = []
    clock = {"t": 0}

    def fake_probe(base_url, token, model_id, timeout=30):
        probe_calls.append(model_id)
        # Odd models answer PAID so the persisted subset is mixed
        if model_id.endswith(("1", "3")):
            return Result.PAID, {"http": 403}
        return Result.FREE, {"http": 200}

    import inference_watchdog
    orig_time = inference_watchdog.time.time
    def advancing_time():
        val = clock["t"]
        clock["t"] += 250  # each probe "takes" 250s of budget
        return val
    inference_watchdog.time.time = advancing_time

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(
        [f"m{i}" for i in range(5)]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(
                            [f"m{i}" for i in range(5)]))

    try:
        fetch_all_fn = im.build_fetch_all(
            {}, tmp_path, now=1_000_000_000, sleep=lambda s: None)
        results, _ = fetch_all_fn()
    finally:
        inference_watchdog.time.time = orig_time

    # 250s per probe, budget 900s: probes m0 (elapsed=250 < 900), m1
    # (elapsed=500 < 900) and m2 (elapsed=750 < 900) fire; m2's successor
    # m3 (elapsed=1000 >= 900) and everything after it are skipped.
    assert len(probe_calls) == 3, (
        f"expected 3 probes before truncation, got {len(probe_calls)}")

    state_path = tmp_path / "probe_state.json"
    persisted = probe_state.load_probe_state(state_path)

    # The probed subset IS on disk with the tick's epoch — truncation did
    # not discard partial progress.
    assert persisted["bai"]["m0"] == {"verdict": "free",
                                      "epoch": 1_000_000_000}
    assert persisted["bai"]["m1"] == {"verdict": "paid",
                                      "epoch": 1_000_000_000}
    # The unprobed remainder has NO verdict — it will be queued as a new
    # arrival next tick (self-healing).
    for unprobed in ("m3", "m4"):
        assert unprobed not in persisted["bai"], (
            f"{unprobed} must not have a verdict — it was never probed")
    # Roster reflects only FREE verdicts among the probed subset.
    assert results["bai"] == ["m0", "m2"]


# ---------- save skip when unchanged (P3 MINOR 8) ----------


def test_save_skip_when_unchanged(monkeypatch, tmp_path):
    """When the in-memory final state is byte-identical to what was loaded,
    the save must be skipped (no redundant atomic write)."""
    state_path = tmp_path / "probe_state.json"
    # Pre-seed with m1 free at 999_000_000
    probe_state.record_verdict(state_path, "bai", "m1", "free", 999_000_000)

    write_count = {"n": 0}
    orig_save = probe_state.save_probe_state

    def counting_save(path, data):
        write_count["n"] += 1
        return orig_save(path, data)

    monkeypatch.setattr(probe_state, "save_probe_state", counting_save)

    def fake_probe(base_url, token, model_id, timeout=30):
        # Returns FREE with same epoch — verdict entry unchanged
        return Result.FREE, {"http": 200}

    monkeypatch.setattr(im, "probe_model", fake_probe)
    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers(["m1"]))
    monkeypatch.setattr(providers, "fetch_provider",
                        _fake_fetch_provider_factory(["m1"]))

    # now matches the seeded epoch exactly so entry is byte-identical
    fetch_all_fn = im.build_fetch_all(
        {}, tmp_path, now=999_000_000, sleep=lambda s: None)
    fetch_all_fn()

    # When state is unchanged, save_probe_state should not be called
    assert write_count["n"] == 0, (
        f"save called {write_count['n']} times when state unchanged")


# ---------- empty catalog skips drop_missing (P3 MINOR 7) ----------


def test_empty_catalog_skips_drop_missing(monkeypatch, tmp_path):
    """Empty catalog: drop_missing must NOT prune (treat as anomalous fetch).
    P3 minor: explicit assertion of the documented guard."""
    state_path = tmp_path / "probe_state.json"
    probe_state.record_verdict(state_path, "bai", "m1", "free", 999_000_000)

    monkeypatch.setattr(im, "PROVIDERS", _bai_only_providers([]))

    def fake_fetch_provider(config, getter=None):
        if config.get("detection") == "zero-credit-probe":
            return [], {}  # empty catalog (anomalous)
        return [], {}

    monkeypatch.setattr(providers, "fetch_provider", fake_fetch_provider)

    fetch_all_fn = im.build_fetch_all(
        {}, tmp_path, now=1_000_000_000, sleep=lambda s: None)
    results, _ = fetch_all_fn()

    # roster is [] for this provider, state NOT pruned
    loaded = probe_state.load_probe_state(state_path)
    assert "m1" in loaded.get("bai", {}), (
        "empty catalog must NOT trigger drop_missing pruning")