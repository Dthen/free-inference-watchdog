"""Tests for diffing.confirm_diffs — the ~3-minute in-run re-check."""

import diffing


def _fetch_map(resps):
    """resps: {provider: (ids_list_or_None, meta)}; counts calls per provider.
    fetch_one returns the (ids, meta) tuple contract."""
    calls = {}

    def fetch(name):
        calls[name] = calls.get(name, 0) + 1
        return resps[name]

    return fetch, calls


def _ids(resps):
    """Adapter: bare-ids map -> (ids, {}) tuples for _fetch_map."""
    return {k: (v, {}) for k, v in resps.items()}


def test_no_candidates_means_no_sleep_no_refetch():
    slept = []
    fetch, calls = _fetch_map({})
    result = diffing.confirm_diffs(
        candidates={}, prev_providers={}, fetch_one=fetch,
        sleep=slept.append, delay=180)
    assert result == {"confirmed": {}, "transients": {},
                      "unconfirmed": {}, "corrected": {}}
    assert slept == []
    assert calls == {}


def test_persistent_removal_confirmed():
    fetch, calls = _fetch_map(_ids({"nous": ["kept"]}))   # model really gone
    slept = []
    result = diffing.confirm_diffs(
        candidates={"nous": {"added": [], "removed": ["gone-model"]}},
        prev_providers={"nous": ["gone-model", "kept"]},
        fetch_one=fetch, sleep=slept.append, delay=180)
    assert result["confirmed"] == {"nous": {"added": [], "removed": ["gone-model"]}}
    assert result["transients"] == {}
    assert result["unconfirmed"] == {}
    assert slept == [180]
    assert calls == {"nous": 1}


def test_transient_vanishes_not_alerted():
    fetch, _ = _fetch_map(_ids({"nous": ["back", "kept"]}))  # model is still there
    result = diffing.confirm_diffs(
        candidates={"nous": {"added": [], "removed": ["back"]}},
        prev_providers={"nous": ["back", "kept"]},
        fetch_one=fetch, sleep=lambda s: None, delay=180)
    assert result["confirmed"] == {}
    assert result["transients"] == {"nous": {"added": [], "removed": ["back"]}}
    assert result["unconfirmed"] == {}


def test_recheck_failure_marks_unconfirmed():
    def failing_fetch(name):
        raise diffing.FetchError("502")

    result = diffing.confirm_diffs(
        candidates={"openrouter": {"added": ["x"], "removed": []}},
        prev_providers={"openrouter": []},
        fetch_one=failing_fetch, sleep=lambda s: None, delay=180)
    assert result["confirmed"] == {}
    assert result["unconfirmed"] == {"openrouter": {"added": ["x"], "removed": []}}


def test_mixed_providers_single_nap_each_resolves():
    resps = {"nous": ["a2"], "zen": ["z1", "z2", "z9"]}  # zen: z9 still there = transient
    fetch, calls = _fetch_map(_ids(resps))
    naps = []
    candidates = {
        "nous": {"added": ["a2"], "removed": ["a1"]},   # persists
        "zen": {"added": [], "removed": ["z9"]},        # transient (z9 not gone)
    }
    result = diffing.confirm_diffs(
        candidates=candidates,
        prev_providers={"nous": ["a1"], "zen": ["z1", "z2", "z9"]},
        fetch_one=fetch, sleep=naps.append, delay=180)
    assert result["confirmed"] == {"nous": {"added": ["a2"], "removed": ["a1"]}}
    assert result["transients"] == {"zen": {"added": [], "removed": ["z9"]}}
    assert naps == [180]          # exactly ONE nap total, not per provider
    assert sorted(calls) == ["nous", "zen"]


# ---------- R2-2: corrected id-map (recheck outcomes persist) ----------

def test_corrected_transient_equals_prev_ids():
    # Flap recovered on recheck -> corrected map must equal PREV ids so the
    # persisted roster never holds the pre-recheck snapshot nor the flap.
    fetch, _ = _fetch_map(_ids({"zen": ["z1", "z2"]}))
    result = diffing.confirm_diffs(
        candidates={"zen": {"added": [], "removed": ["z2"]}},
        prev_providers={"zen": ["z1", "z2"]},
        fetch_one=fetch, sleep=lambda s: None, delay=0)
    assert result["corrected"] == {"zen": ["z1", "z2"]}
    assert result["transients"] == {"zen": {"added": [], "removed": ["z2"]}}


def test_corrected_confirmed_equals_refetch_truth():
    # Real removal survived -> corrected map is the REFETCH's ids.
    fetch, _ = _fetch_map(_ids({"nous": ["kept"]}))
    result = diffing.confirm_diffs(
        candidates={"nous": {"added": [], "removed": ["gone-model"]}},
        prev_providers={"nous": ["gone-model", "kept"]},
        fetch_one=fetch, sleep=lambda s: None, delay=0)
    assert result["corrected"] == {"nous": ["kept"]}
    assert result["confirmed"] == {"nous": {"added": [], "removed": ["gone-model"]}}


def test_corrected_confirmed_addition_uses_refetch_ids():
    # A real addition also persists refetch truth (both ids present).
    fetch, _ = _fetch_map(_ids({"kilo": ["k1", "k2"]}))
    result = diffing.confirm_diffs(
        candidates={"kilo": {"added": ["k2"], "removed": []}},
        prev_providers={"kilo": ["k1"]},
        fetch_one=fetch, sleep=lambda s: None, delay=0)
    assert result["corrected"] == {"kilo": ["k1", "k2"]}


def test_corrected_unconfirmed_provider_absent():
    # Fetch-failure recheck -> provider ABSENT from corrected map, so the
    # caller keeps its sticky-old entry and the signal resurfaces next tick.
    def failing(name):
        raise diffing.FetchError("timeout")

    result = diffing.confirm_diffs(
        candidates={"zen": {"added": ["z1"], "removed": []}},
        prev_providers={"zen": ["old1"]},
        fetch_one=failing, sleep=lambda s: None, delay=0)
    assert result["unconfirmed"] == {"zen": {"added": ["z1"], "removed": []}}
    assert "zen" not in result["corrected"]


# ---------- R2-2 caller-side merge (merge_corrected) ----------

def test_merge_corrected_transient_and_confirmed():
    new_map = {"zen": ["z1"], "nous": ["a1", "a2"], "kilo": ["k1"]}
    confirmation = {
        "corrected": {"zen": ["z1", "z2"],        # transient -> prev ids
                      "nous": ["a1"]},            # confirmed -> refetch ids
        "unconfirmed": {},
    }
    merged = diffing.merge_corrected(
        new_map, confirmation, prev_providers={"zen": ["z1", "z2"],
                                               "nous": ["a1", "a2"]})
    assert merged["zen"] == ["z1", "z2"]      # flap leaves no trace
    assert merged["nous"] == ["a1"]           # refetch truth persisted
    assert merged["kilo"] == ["k1"]           # untouched provider survives


def test_merge_corrected_unconfirmed_keeps_sticky_old():
    # Unconfirmed: corrected map lacks the provider -> previous-roster entry
    # is restored (NOT this tick's pre-recheck fetch, which showed the flap).
    new_map = {"zen": ["z1"]}                 # pre-recheck snapshot w/ flap
    confirmation = {
        "corrected": {},                       # zen ABSENT (refetch failed)
        "unconfirmed": {"zen": {"added": [], "removed": ["z2"]}},
    }
    merged = diffing.merge_corrected(
        new_map, confirmation, prev_providers={"zen": ["z1", "z2"]})
    assert merged["zen"] == ["z1", "z2"]      # sticky-old wins


def test_merge_corrected_unconfirmed_no_prev_entry_sticky_empty():
    """F-R2-3: provider never seen before (no prev entry — post-eviction
    re-add, hand-seeded state) has NO sticky-old to restore, so the merged
    entry must be sticky-EMPTY ([]), NOT the pre-recheck snapshot. Persisting
    the snapshot would make a first-ever addition invisible forever (next
    tick sees no diff); [] lets it resurface and alert on the next good tick."""
    new_map = {"kilo": ["k9"]}
    confirmation = {"corrected": {}, "unconfirmed": {"kilo": {"added": ["k9"], "removed": []}}}
    merged = diffing.merge_corrected(new_map, confirmation, prev_providers={})
    assert merged["kilo"] == []


def test_merge_corrected_does_not_mutate_input():
    new_map = {"zen": ["z1"]}
    confirmation = {"corrected": {"zen": ["z1", "z2"]}, "unconfirmed": {}}
    merged = diffing.merge_corrected(new_map, confirmation, {"zen": ["z1", "z2"]})
    assert merged is not new_map
    assert new_map == {"zen": ["z1"]}
