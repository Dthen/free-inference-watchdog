"""Tests for mcp_server.py — the MCP query surface over watchdog state.

Tool FUNCTIONS are called directly (no protocol transport needed): the
transport is the MCP SDK's job and was probe-verified separately. What these
tests own is the data contract: honest presence lookups across gateways, a
clearly-labeled heuristic section for stealth-renamed near-matches, an
absence caveat in every result (gateways rename inconsistently, so
cross-gateway absence is unreliable — Dthen 2026-08-25), structured error
payloads instead of exceptions on missing/corrupt state, and full graceful
degradation on an empty state dir.
"""

import json

import pytest

import mcp_server


# ---------- fixtures ----------

@pytest.fixture()
def state_dir(tmp_path):
    """A realistic fixture state dir: five providers, one id shared by all,
    one stealth-renamed pair (vendor-z/zero-priced-model vs vendor-x/preview-free), and one
    single-gateway exclusive."""
    roster = {
        "tick_epoch": 1_787_721_434,
        "providers": {
            "nous": ["vendor-z/zero-priced-model", "vendor-d/model-6:free"],
            "zen": ["vendor-x/preview-free", "vendor-d/model-4-free"],
            "kilo": [
                "vendor-z/zero-priced-model",
                "cohere/north-mini-code:free",
                "kilo-auto/free",
            ],
            "cline": ["vendor-z/zero-priced-model"],
            "openrouter": ["vendor-f/model-5:free"],
        },
        "stale_providers": [],
        "transients": {},
        "unconfirmed": {},
        "nous_ratelimit": {},
    }
    (tmp_path / "state").mkdir()
    (tmp_path / "site").mkdir()
    (tmp_path / "state" / "roster.json").write_text(json.dumps(roster))
    (tmp_path / "site" / "index.html").write_text("<html></html>")
    (tmp_path / "state" / "alive.json").write_text(json.dumps({
        "last_tick_epoch": 1_787_721_434,
        "last_output_epoch": 1_787_696_916,
        "dropped_alerts_total": 0,
    }))
    (tmp_path / "state" / "pending_alerts.json").write_text(json.dumps([]))
    return tmp_path


@pytest.fixture()
def empty_dir(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    return tmp_path


# ---------- list_free_models ----------

def test_list_full_roster(state_dir):
    res = mcp_server.list_free_models(root=state_dir)
    assert res["ok"] is True
    provs = res["providers"]
    # CANONICAL GATEWAY-KEY SEMANTIC (shared with watchdog_status): exactly
    # the five known gateways, always — unknown junk roster keys are ignored,
    # gateways missing from this tick degrade to empty lists.
    assert set(provs) == set(mcp_server.PROVIDERS)
    assert provs["nous"] == ["vendor-d/model-6:free", "vendor-z/zero-priced-model"]
    assert res["counts"]["nous"] == 2
    assert res["n_gateways"] == len(mcp_server.PROVIDERS) == 6
    assert res["total_ids"] > 0
    assert isinstance(res["tick_epoch"], int)


def test_list_partial_roster_canonical_six_shape(tmp_path):
    """One gateway-key semantic everywhere: a partial roster ({nous, zen}
    plus a junk key) STILL yields all five canonical gateways — kilo/cline/
    openrouter as empty lists, n_gateways==5 — and 'mysterygw' never leaks
    into the output."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "roster.json").write_text(json.dumps({
        "tick_epoch": 100,
        "providers": {
            "nous": ["vendor-z/zero-priced-model"],
            "zen": [],
            "mysterygw": ["junk/id"],
        },
    }))
    res = mcp_server.list_free_models(root=tmp_path)
    assert res["ok"] is True
    assert set(res["providers"]) == set(mcp_server.PROVIDERS)
    assert res["providers"]["nous"] == ["vendor-z/zero-priced-model"]
    for gw in ("zen", "kilo", "cline", "openrouter"):
        assert res["providers"][gw] == []
    assert res["counts"] == {"nous": 1, "zen": 0, "kilo": 0,
                             "cline": 0, "openrouter": 0, "command_code": 0}
    assert res["n_gateways"] == 6
    assert "mysterygw" not in res["providers"]
    assert "mysterygw" not in res["counts"]


def test_list_single_provider_filter(state_dir):
    res = mcp_server.list_free_models(provider="kilo", root=state_dir)
    assert res["ok"] is True
    assert res["provider"] == "kilo"
    assert res["model_ids"] == sorted(res["model_ids"])
    assert "vendor-z/zero-priced-model" in res["model_ids"]


def test_list_unknown_provider_clean_error(state_dir):
    res = mcp_server.list_free_models(provider="anthropic", root=state_dir)
    assert res["ok"] is False
    assert "error" in res
    assert set(mcp_server.PROVIDERS) & set(res["valid_providers"]) == set(
        mcp_server.PROVIDERS)


def test_list_missing_roster_is_error_not_crash(empty_dir):
    res = mcp_server.list_free_models(root=empty_dir)
    assert res["ok"] is False
    assert "roster" in res["error"].lower()


def test_list_corrupt_roster_is_error_not_crash(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "roster.json").write_text("{not json!!")
    res = mcp_server.list_free_models(root=tmp_path)
    assert res["ok"] is False
    assert "corrupt" in res["error"].lower()


# ---------- get_model: exact matches ----------

def test_get_model_exact_match_across_gateways(state_dir):
    res = mcp_server.get_model("cohere/north-mini-code:free", root=state_dir)
    assert res["query"] == "cohere/north-mini-code:free"
    assert res["exact_matches"]["kilo"] is True
    for gw in ("nous", "zen", "cline", "openrouter"):
        assert res["exact_matches"][gw] is False
    # caveat present on EVERY get_model result
    assert any("unreliable" in s.lower() for s in res["caveats"])


def test_get_model_exact_match_everywhere(state_dir):
    res = mcp_server.get_model("vendor-z/zero-priced-model", root=state_dir)
    present = [gw for gw, p in res["exact_matches"].items() if p]
    assert sorted(present) == ["cline", "kilo", "nous"]


def _tokens(s):
    return mcp_server._tokenize(s)


def test_status_fields_present(state_dir):
    now = 1_787_721_434 + 60  # one minute after the tick
    res = mcp_server.watchdog_status(now=now, root=state_dir)
    assert res["ok"] is True
    assert res["cadence_s"] == mcp_server.CADENCE_S == 1 * 3600
    assert res["last_tick_age_s"] == 60
    assert res["tick_fresh"] is True
    assert res["stale_providers"] == []
    # All five DISPLAY_ORDER gateways always appear (stable shape), plus
    # any unknown roster keys.
    for gw in ("nous", "zen", "kilo", "cline", "openrouter", "command_code"):
        assert gw in res["provider_counts"]
    assert res["provider_counts"]["kilo"] == 3
    assert res["provider_counts"]["nous"] == 2
    assert res["pending_alerts"] == 0
    assert res["site_published"] is True
    assert isinstance(res["site_publish_age_s"], int)


def test_status_stale_tick_flagged(state_dir):
    res = mcp_server.watchdog_status(
        now=1_787_721_434 + mcp_server.CADENCE_S * 3, root=state_dir)
    assert res["tick_fresh"] is False


def test_status_reports_stale_providers(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "roster.json").write_text(json.dumps({
        "tick_epoch": 100,
        "providers": {"nous": [], "openrouter": []},
        "stale_providers": ["zen", "kilo"],
    }))
    res = mcp_server.watchdog_status(now=200, root=tmp_path)
    assert res["stale_providers"] == ["zen", "kilo"]
    for gw in ("nous", "zen", "kilo", "cline", "openrouter", "command_code"):
        assert res["provider_counts"][gw] == 0


def test_status_stale_providers_hostile_string_degrades(tmp_path):
    """Hostile roster: stale_providers as a bare STRING must degrade to []
    (same rule as _clean_ids for id lists) — never char-split into
    ['z', 'e', 'n']."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "roster.json").write_text(json.dumps({
        "tick_epoch": 100,
        "providers": {"nous": [], "openrouter": []},
        "stale_providers": "zen",
    }))
    res = mcp_server.watchdog_status(now=200, root=tmp_path)
    assert res["ok"] is True
    assert res["stale_providers"] == []


def test_status_graceful_on_empty_dir(empty_dir):
    res = mcp_server.watchdog_status(root=empty_dir)
    # No exception, ok stays truthful, every optional field degrades.
    assert res["ok"] is False
    assert "error" in res
    for field in ("cadence_s", "pending_alerts", "site_published"):
        assert field in res
    assert res["site_published"] is False
    assert res["pending_alerts"] is None
