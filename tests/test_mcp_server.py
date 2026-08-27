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
import providers


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


# ---------- GATEWAY_WIRING pins ----------

def test_gateway_wiring_keys_match_providers():
    """GATEWAY_WIRING keys must match PROVIDERS keys exactly — the two are
    pinned together so a new gateway added to one is always added to the other."""
    assert set(providers.GATEWAY_WIRING) == set(providers.PROVIDERS)


# ---------- get_model: endpoints (wiring) ----------

def test_get_model_returns_endpoints_for_grouped_name(state_dir):
    """get_model on a stripped name returns one endpoint per (gateway, raw id)
    whose free-marker-stripped name matches — marker-insensitive lookup."""
    res = mcp_server.get_model("vendor-d/model-6", root=state_dir)
    assert res["ok"] is True
    # vendor-d/model-6:free on nous strips to vendor-d/model-6
    assert len(res["endpoints"]) == 1
    ep = res["endpoints"][0]
    assert ep["gateway"] == "nous"
    assert ep["model_id"] == "vendor-d/model-6:free"
    assert "chat_completions_url" in ep
    assert "auth" in ep
    assert "api_type" in ep


def test_get_model_raw_variant_returns_same_endpoints(state_dir):
    """get_model on the raw :free variant returns the SAME endpoints list as
    the stripped name — marker-insensitive lookup."""
    stripped = mcp_server.get_model("vendor-d/model-6", root=state_dir)
    raw = mcp_server.get_model("vendor-d/model-6:free", root=state_dir)
    assert stripped["endpoints"] == raw["endpoints"]


def test_get_model_exact_matches_strictly_unchanged(state_dir):
    """exact_matches stays STRICTLY exact — the honest raw answer, never
    grouped. A stripped query must NOT match a :free raw id here."""
    res = mcp_server.get_model("vendor-d/model-6", root=state_dir)
    # exact_matches is exact: "vendor-d/model-6" is NOT in any roster list
    assert res["exact_matches"]["nous"] is False
    assert res["exact_gateways"] == []
    # but endpoints (wiring) DOES resolve via strip_free_marker
    assert len(res["endpoints"]) == 1


def test_get_model_endpoints_ordered_by_providers_then_natural_key(state_dir):
    """endpoints order: PROVIDERS display order, then raw id by _natural_key."""
    res = mcp_server.get_model("vendor-z/zero-priced-model", root=state_dir)
    # present on nous, kilo, cline (in PROVIDERS order)
    assert [ep["gateway"] for ep in res["endpoints"]] == ["nous", "kilo", "cline"]
    assert [ep["model_id"] for ep in res["endpoints"]] == [
        "vendor-z/zero-priced-model"] * 3


def test_get_model_no_match_returns_empty_endpoints(state_dir):
    """No match at all (even after stripping) => endpoints: []."""
    res = mcp_server.get_model("nonexistent/model", root=state_dir)
    assert res["ok"] is True
    assert res["endpoints"] == []


# ---------- tool: list_endpoints ----------

def test_list_endpoints_totals_match_roster(state_dir):
    """list_endpoints() totals match the roster; counts.models < counts.endpoints
    on the live-shaped fixture (9 raw ids, 7 stripped names)."""
    res = mcp_server.list_endpoints(root=state_dir)
    assert res["ok"] is True
    assert res["tool"] == "list_endpoints"
    # 9 raw ids across the fixture
    assert res["counts"]["endpoints"] == 9
    # 7 distinct stripped names (vendor-x/preview-free -> vendor-x/preview,
    # vendor-d/model-4-free -> vendor-d/model-4, cohere/north-mini-code:free
    # -> cohere/north-mini-code, vendor-f/model-5:free -> vendor-f/model-5)
    assert res["counts"]["models"] == 7
    assert res["counts"]["models"] < res["counts"]["endpoints"]
    assert res["counts"]["gateways"] == 6
    assert res["provider"] is None
    # All six gateways present in the gateways map
    assert set(res["gateways"]) == set(mcp_server.PROVIDERS)


def test_list_endpoints_filter_zen(state_dir):
    """list_endpoints("zen") returns only zen's wiring + model_ids."""
    res = mcp_server.list_endpoints(provider="zen", root=state_dir)
    assert res["ok"] is True
    assert res["provider"] == "zen"
    assert set(res["gateways"]) == {"zen"}
    # zen carries vendor-x/preview-free and vendor-d/model-4-free
    assert sorted(res["gateways"]["zen"]["model_ids"]) == [
        "vendor-d/model-4-free", "vendor-x/preview-free"]
    # wiring fields present
    assert "chat_completions_url" in res["gateways"]["zen"]
    assert "auth" in res["gateways"]["zen"]
    assert "api_type" in res["gateways"]["zen"]


def test_list_endpoints_unknown_provider_error(state_dir):
    """list_endpoints("nope") returns a structured error payload."""
    res = mcp_server.list_endpoints(provider="nope", root=state_dir)
    assert res["ok"] is False
    assert "error" in res
    assert set(mcp_server.PROVIDERS) & set(res["valid_providers"]) == set(
        mcp_server.PROVIDERS)


def test_list_endpoints_hostile_roster_degrades(tmp_path):
    """Hostile roster (provider value is a bare string, not a list) degrades
    cleanly — no exception, hostile ids dropped, ok stays True."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "roster.json").write_text(json.dumps({
        "tick_epoch": 100,
        "providers": {
            "nous": "not-a-list",  # hostile: bare string
            "zen": [],
        },
    }))
    res = mcp_server.list_endpoints(root=tmp_path)
    assert res["ok"] is True
    # Hostile value degrades to an empty list (same rule as _clean_ids)
    assert res["gateways"]["nous"]["model_ids"] == []
    assert res["gateways"]["zen"]["model_ids"] == []
