"""Tests for diffing.confirm_diffs — the ~3-minute in-run re-check."""

import diffing


def _fetch_map(resps):
    """resps: {provider: ids_list_or_None}; counts calls per provider."""
    calls = {}

    def fetch(name):
        calls[name] = calls.get(name, 0) + 1
        return resps[name]

    return fetch, calls


def test_no_candidates_means_no_sleep_no_refetch():
    slept = []
    fetch, calls = _fetch_map({})
    result = diffing.confirm_diffs(
        candidates={}, prev_providers={}, fetch_one=fetch,
        sleep=slept.append, delay=180)
    assert result == {"confirmed": {}, "transients": {}, "unconfirmed": {}}
    assert slept == []
    assert calls == {}


def test_persistent_removal_confirmed():
    fetch, calls = _fetch_map({"nous": ["kept"]})          # model really gone
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
    fetch, _ = _fetch_map({"nous": ["back", "kept"]})      # model is still there
    result = diffing.confirm_diffs(
        candidates={"nous": {"added": [], "removed": ["back"]}},
        prev_providers={"nous": ["back", "kept"]},
        fetch_one=lambda n: None if False else _fetch_map({"nous": ["back", "kept"]})[0](n),
        sleep=lambda s: None, delay=180)
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
    fetch, calls = _fetch_map(resps)
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
