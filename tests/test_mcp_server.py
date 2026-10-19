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


def test_tools_surface_raw_metadata_per_gateway(state_dir):
    path = state_dir / "state" / "roster.json"
    roster = json.loads(path.read_text())
    mid = "vendor-z/zero-priced-model"
    model = {"id": mid, "context_length": 128000,
             "architecture": {"input_modalities": ["text", "image"]},
             "pricing": {"prompt": "0"}, "unknown_field": [None, False]}
    other = {**model, "context_length": 32000}
    roster["provider_models"] = {
        "nous": {mid: model, "not-tracked": {"id": "not-tracked"}},
        "kilo": {mid: other}, "unknown-gateway": {mid: model}}
    path.write_text(json.dumps(roster))

    single = mcp_server.list_free_models("nous", root=state_dir)
    assert single["models"] == {mid: model}
    full = mcp_server.list_free_models(root=state_dir)
    assert full["models"]["nous"] == {mid: model}
    assert full["models"]["kilo"] == {mid: other}
    assert set(full["models"]) == set(mcp_server.PROVIDERS)
    lookup = mcp_server.get_model(mid, root=state_dir)
    assert {ep["gateway"]: ep["metadata"] for ep in lookup["endpoints"]} == {
        "nous": model, "kilo": other, "amd": {}}
    endpoints = mcp_server.list_endpoints("nous", root=state_dir)
    assert endpoints["gateways"]["nous"]["models"] == {mid: model}
    summary = mcp_server.watchdog_status(root=state_dir)["metadata_summary"]
    assert summary["models_with_metadata"] == 2  # gateway/model pairs, not union
    assert summary["provider_counts"]["nous"] == 1
    assert summary["provider_counts"]["amd"] == 0


@pytest.mark.parametrize("metadata", [None, [], "junk", {"nous": []},
                                      {"nous": {"vendor-z/zero-priced-model": 7}}])
def test_tools_degrade_on_missing_or_malformed_metadata(state_dir, metadata):
    path = state_dir / "state" / "roster.json"
    roster = json.loads(path.read_text())
    if metadata is not None:
        roster["provider_models"] = metadata
    path.write_text(json.dumps(roster))
    assert mcp_server.list_free_models("nous", root=state_dir)["models"] == {}
    assert all(not models for models in mcp_server.list_free_models(
        root=state_dir)["models"].values())
    assert all(ep["metadata"] == {} for ep in mcp_server.get_model(
        "vendor-z/zero-priced-model", root=state_dir)["endpoints"])
    assert mcp_server.list_endpoints("nous", root=state_dir)["gateways"]["nous"]["models"] == {}
    assert mcp_server.watchdog_status(root=state_dir)["metadata_summary"]["models_with_metadata"] == 0


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
            "tokenrouter": ["vendor-x/preview-free", "vendor-d/model-4-free"],
            "kilo": [
                "vendor-z/zero-priced-model",
                "cohere/north-mini-code:free",
                "kilo-auto/free",
            ],
            "amd": ["vendor-z/zero-priced-model"],
            "openrouter": ["vendor-f/model-5:free"],
        },
        "stale_providers": [],
        "transients": {},
        "unconfirmed": {},
        "ratelimits": {},
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
    # the known gateways, always — unknown junk roster keys are ignored,
    # gateways missing from this tick degrade to empty lists.
    assert set(provs) == set(mcp_server.PROVIDERS)
    assert provs["nous"] == ["vendor-d/model-6:free", "vendor-z/zero-priced-model"]
    assert res["counts"]["nous"] == 2
    assert res["n_gateways"] == len(mcp_server.PROVIDERS) == 6
    assert res["total_ids"] > 0
    assert isinstance(res["tick_epoch"], int)


def test_list_partial_roster_canonical_shape(tmp_path):
    """One gateway-key semantic everywhere: a partial roster ({nous, tokenrouter}
    plus a junk key) STILL yields all canonical gateways — kilo/openrouter/amd/bai
    as empty lists, n_gateways==6 — and 'mysterygw' never leaks
    into the output. nim is absent: providers/nim.json is on disk but
    carries enabled:false, so it is not in the registry."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "roster.json").write_text(json.dumps({
        "tick_epoch": 100,
        "providers": {
            "nous": ["vendor-z/zero-priced-model"],
            "tokenrouter": [],
            "mysterygw": ["junk/id"],
        },
    }))
    res = mcp_server.list_free_models(root=tmp_path)
    assert res["ok"] is True
    assert set(res["providers"]) == set(mcp_server.PROVIDERS)
    assert res["providers"]["nous"] == ["vendor-z/zero-priced-model"]
    for gw in ("tokenrouter", "kilo", "openrouter", "amd", "bai"):
        assert res["providers"][gw] == []
    assert res["counts"] == {"nous": 1, "tokenrouter": 0, "kilo": 0,
                             "openrouter": 0, "amd": 0, "bai": 0}
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
    for gw in ("nous", "tokenrouter", "amd", "openrouter", "bai"):
        assert res["exact_matches"][gw] is False
    # caveat present on EVERY get_model result
    assert any("unreliable" in s.lower() for s in res["caveats"])


def test_get_model_exact_match_everywhere(state_dir):
    res = mcp_server.get_model("vendor-z/zero-priced-model", root=state_dir)
    present = [gw for gw, p in res["exact_matches"].items() if p]
    assert sorted(present) == ["amd", "kilo", "nous"]


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
    # All DISPLAY_ORDER gateways always appear (stable shape); unknown
    # roster keys never appear.
    for gw in ("nous", "tokenrouter", "kilo", "openrouter", "amd", "bai"):
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
        "stale_providers": ["tokenrouter", "kilo"],
    }))
    res = mcp_server.watchdog_status(now=200, root=tmp_path)
    assert res["stale_providers"] == ["tokenrouter", "kilo"]
    for gw in ("nous", "tokenrouter", "kilo", "openrouter", "amd", "bai"):
        assert res["provider_counts"][gw] == 0


def test_status_stale_providers_hostile_string_degrades(tmp_path):
    """Hostile roster: stale_providers as a bare STRING must degrade to []
    (same rule as _clean_ids for id lists) — never char-split into
    ['t', 'o', 'k', 'e', 'n', 'r', 'o', 'u', 't', 'e', 'r']."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "roster.json").write_text(json.dumps({
        "tick_epoch": 100,
        "providers": {"nous": [], "openrouter": []},
        "stale_providers": "tokenrouter",
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
    from config_loader import PROVIDERS, GATEWAY_WIRING
    assert set(GATEWAY_WIRING) == set(PROVIDERS)


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
    assert "auth" not in ep, "Bearer auth field dropped from wiring displays"
    assert "api_type" in ep
    # The MCP server imports the pure query helper directly: it must be the SAME
    # function object as the query module, never a copied wrapper.
    from watchdog_catalog.query import strip_free_marker as core_strip_free_marker
    assert mcp_server.strip_free_marker is core_strip_free_marker


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
    # present on nous, kilo, amd (in PROVIDERS order)
    assert [ep["gateway"] for ep in res["endpoints"]] == ["nous", "kilo", "amd"]
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
    # All gateways present in the gateways map
    assert set(res["gateways"]) == set(mcp_server.PROVIDERS)


def test_list_endpoints_filter_tokenrouter(state_dir):
    """list_endpoints("tokenrouter") returns only tokenrouter's wiring + model_ids."""
    res = mcp_server.list_endpoints(provider="tokenrouter", root=state_dir)
    assert res["ok"] is True
    assert res["provider"] == "tokenrouter"
    assert set(res["gateways"]) == {"tokenrouter"}
    # tokenrouter carries vendor-x/preview-free and vendor-d/model-4-free
    assert sorted(res["gateways"]["tokenrouter"]["model_ids"]) == [
        "vendor-d/model-4-free", "vendor-x/preview-free"]
    # wiring fields present — auth is gone (dropped from every display)
    assert "chat_completions_url" in res["gateways"]["tokenrouter"]
    assert "auth" not in res["gateways"]["tokenrouter"]
    assert "api_type" in res["gateways"]["tokenrouter"]


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
            "tokenrouter": [],
        },
    }))
    res = mcp_server.list_endpoints(root=tmp_path)
    assert res["ok"] is True
    # Hostile value degrades to an empty list (same rule as _clean_ids)
    assert res["gateways"]["nous"]["model_ids"] == []
    assert res["gateways"]["tokenrouter"]["model_ids"] == []


# ---------- tool descriptions ----------

def test_tool_descriptions_derive_gateway_list():
    """The gateway list in tool descriptions is DERIVED from PROVIDERS —
    a dropped/added gateway must not leave stale prose shipped to MCP
    clients."""
    watched = ", ".join(mcp_server.PROVIDERS)
    desc = mcp_server.TOOL_DESCRIPTIONS["list_free_models"]
    assert f"({watched})" in desc
    assert "the seven gateways" not in mcp_server.TOOL_DESCRIPTIONS["get_model"]


# ---------- local snapshot helper pipeline (unit 2j-U1a) ----------

def test_local_snapshot_helper_pipeline(monkeypatch):
    """U1a: capture -> local-only sanitize -> accepted snapshot, helpers only.

    Fully synthetic module globals plus a wrapper around the REAL accepted
    ``catalog_export.build_snapshot``. The three helpers are intentionally not
    wired into the tools yet, so they are driven directly; no state, clock,
    pending-queue, filesystem access or recapture may happen while they run.
    """
    import copy
    import types

    import catalog_export as _export_mod
    from watchdog_catalog import catalog as _catalog

    fixed = {"ok": False, "error": "Invalid catalogue snapshot."}
    base = {"tool": "list_free_models", "state_root": "/synthetic"}
    base_before = dict(base)

    sentinel = object()
    z_meta = {"name": "Z Shared", "context_length": 8192,
              "unknown_field": "Z-UNKNOWN"}
    a_meta = {"name": "A Shared", "context_length": 131072,
              "unknown_field": "A-UNKNOWN"}
    long_id = "9" * 64

    # -- Phase A: synthetic capture sources and the capture boundary ----------
    configs = {
        "z-last": {"name": "Zeta", "base_url": "https://z.example/v1",
                   "_stem": "z-last", "_token": sentinel,
                   "auth": {"method": "none"}, "private": sentinel},
        "a-first": {"name": "Alpha", "base_url": "https://a.example/v1",
                    "_stem": "a-first", "_token": sentinel,
                    "auth": {"method": "none"}, "private": sentinel},
        "m-mid": {"name": "Mu", "base_url": "https://m.example/v1",
                  "_stem": "m-mid", "_token": sentinel,
                  "auth": {"method": "none"}, "private": sentinel},
    }
    wiring = {
        "z-last": {"chat_completions_url": "https://z.example/v1/chat/completions",
                   "api_type": "openai_compatible", "auth": sentinel},
        "a-first": {"chat_completions_url": "https://a.example/v1/chat/completions",
                    "api_type": "openai_compatible", "auth": sentinel},
        "m-mid": {"chat_completions_url": "https://m.example/v1/chat/completions",
                  "api_type": "openai_compatible", "auth": sentinel},
        "wiring-only": {"chat_completions_url": "https://w.example/v1/chat/completions",
                        "api_type": "openai_compatible"},
    }
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    monkeypatch.setattr(mcp_server, "CADENCE_S", 3600)

    records = mcp_server._capture_registry(base)
    assert records == [
        {"key": "z-last", "name": "Zeta",
         "chat_completions_url": "https://z.example/v1/chat/completions",
         "api_type": "openai_compatible"},
        {"key": "a-first", "name": "Alpha",
         "chat_completions_url": "https://a.example/v1/chat/completions",
         "api_type": "openai_compatible"},
        {"key": "m-mid", "name": "Mu",
         "chat_completions_url": "https://m.example/v1/chat/completions",
         "api_type": "openai_compatible"},
    ]
    assert [r["key"] for r in records] == ["z-last", "a-first", "m-mid"]
    assert all(set(r) == {"key", "name", "chat_completions_url", "api_type"}
               for r in records)
    # The wiring-only key is not a registry member.
    assert "wiring-only" not in {r["key"] for r in records}
    # Private extras are never traversed/copied; the source is unmutated.
    assert configs["z-last"]["_token"] is sentinel
    assert configs["z-last"]["private"] is sentinel
    # A later source mutation is not reflected in the earlier capture.
    configs["z-last"]["name"] = "LATE"
    assert mcp_server._capture_registry(base)[0]["name"] == "LATE"
    assert records[0]["name"] == "Zeta"
    configs["z-last"]["name"] = "Zeta"  # restore the synthetic control

    # Root guards: a non-dict PROVIDERS/GATEWAY_WIRING root is the fixed local
    # structural error, never a partial or empty registry.
    for bad in ([], None, "str", 3):
        monkeypatch.setattr(mcp_server, "PROVIDERS", bad)
        with pytest.raises(mcp_server._StateError) as exc:
            mcp_server._capture_registry(base)
        assert exc.value.payload == {**base, **fixed}
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    for bad in ([], None, "str"):
        monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", bad)
        with pytest.raises(mcp_server._StateError):
            mcp_server._capture_registry(base)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # Per-key malformed config/wiring does NOT raise during capture: required
    # slots become None and the record is retained for later structural
    # rejection. The bad-config + otherwise-VALID-wiring row is obligatory.
    mal_configs = {
        "good": {"name": "Good"},
        "bad-config-none": None,
        "bad-config-list": [],
        "bad-config-bool": True,
        "empty-name": {"name": ""},
        "nonstr-name": {"name": 7},
        "missing-name": {},
    }
    good_url = "https://ok.example/v1/chat/completions"
    good_wiring = {"chat_completions_url": good_url,
                   "api_type": "openai_compatible"}
    mal_wiring = {
        "good": dict(good_wiring),
        "bad-config-none": dict(good_wiring),   # VALID wiring under a bad config
        "bad-config-list": "not-a-dict",
        "bad-config-bool": {"chat_completions_url": 7, "api_type": sentinel},
        "empty-name": dict(good_wiring),
        "nonstr-name": dict(good_wiring),
        "missing-name": dict(good_wiring),
    }
    monkeypatch.setattr(mcp_server, "PROVIDERS", mal_configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", mal_wiring)
    mal_records = {r["key"]: r for r in mcp_server._capture_registry(base)}
    assert set(mal_records) == set(mal_configs)
    assert mal_records["good"]["chat_completions_url"] == good_url
    # A bad config forces the required slots to None even under valid wiring.
    for key in ("bad-config-none", "bad-config-list", "bad-config-bool"):
        assert mal_records[key]["chat_completions_url"] is None
        assert mal_records[key]["api_type"] is None
    # A valid dict config with an empty/non-string/absent name keeps its GOOD
    # wiring and passes the name through for the accepted key fallback.
    assert mal_records["empty-name"]["name"] == ""
    assert mal_records["nonstr-name"]["name"] == 7
    assert mal_records["missing-name"]["name"] is None
    for key in ("empty-name", "nonstr-name", "missing-name"):
        assert mal_records[key]["chat_completions_url"] == good_url

    # An invalid provider KEY is retained at capture, never filtered away.
    monkeypatch.setattr(mcp_server, "PROVIDERS", {3: {"name": "Three"}})
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {3: dict(good_wiring)})
    key_records = mcp_server._capture_registry(base)
    assert key_records == [{"key": 3, "name": "Three",
                            "chat_completions_url": good_url,
                            "api_type": "openai_compatible"}]

    # -- Phase B: local-only sanitizer ---------------------------------------
    for bad_roster in (None, [], "str", 3, {}, {"tick_epoch": 0},
                       {"providers": []}, {"providers": None},
                       {"providers": "x"}):
        with pytest.raises(_catalog.CatalogError) as exc:
            mcp_server._presanitized_roster(bad_roster, records)
        assert exc.value.code == _catalog.INVALID_SNAPSHOT
        assert exc.value.message == "Invalid catalogue snapshot."

    roster = {
        "tick_epoch": 0,
        "providers": {
            "z-last": ["shared/model", "z-extra"],
            "a-first": ["shared/model", "z2", "z10", "z2", " padded ", "", 3,
                        None, "unicode/😀", "non-free", long_id],
            "m-mid": [],
            "unknown-bad": "not-a-list",
        },
        "provider_models": {
            "z-last": {"shared/model": z_meta, "z-extra": "BAD-CONTAINER"},
            "a-first": {"shared/model": a_meta},
        },
        "stale_providers": ["z-last"],
        "private": {"deep": "SENTINEL"},
    }
    roster_before = copy.deepcopy(roster)
    clean = mcp_server._presanitized_roster(roster, records)
    # Fresh root/providers/ID lists; untouched members alias by reference.
    assert clean is not roster
    assert clean["providers"] is not roster["providers"]
    assert clean["providers"]["z-last"] is not roster["providers"]["z-last"]
    assert clean["providers"]["a-first"] is not roster["providers"]["a-first"]
    assert clean["provider_models"] is roster["provider_models"]
    assert clean["stale_providers"] is roster["stale_providers"]
    assert clean["private"] is roster["private"]
    assert clean["tick_epoch"] == 0
    # Unknown providers excluded; missing known keys become empty.
    assert set(clean["providers"]) == {"z-last", "a-first", "m-mid"}
    assert clean["providers"]["m-mid"] == []
    # Literal, untrimmed, duplicate, input-order retention; junk dropped.
    assert clean["providers"]["a-first"] == [
        "shared/model", "z2", "z10", "z2", " padded ", "unicode/😀",
        "non-free", long_id]
    assert clean["providers"]["z-last"] == ["shared/model", "z-extra"]
    assert roster == roster_before

    # A non-list provider value degrades to [] without touching other members.
    hostile = {"providers": {"z-last": "hostile"}, "provider_models": None}
    clean_hostile = mcp_server._presanitized_roster(hostile, [records[0]])
    assert clean_hostile["providers"] == {"z-last": []}
    assert clean_hostile["provider_models"] is None

    # Legitimate empty roots are a VALID empty registry, not a structural
    # error: capture yields [] and the local pipeline still produces a
    # validated empty snapshot. The captured [] is fed straight through.
    monkeypatch.setattr(mcp_server, "PROVIDERS", {})
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    empty_registry = mcp_server._capture_registry(base)
    assert empty_registry == []
    empty_snap = mcp_server._local_snapshot(
        {"tick_epoch": 0, "providers": {}}, empty_registry, base)
    assert empty_snap["schema_version"] == 1
    assert empty_snap["cadence_s"] == 3600
    assert empty_snap["tick_epoch"] == 0
    assert empty_snap["provider_order"] == []
    assert empty_snap["providers"] == {}
    assert empty_snap["warnings"] == []
    # The accepted boundary accepts that exact object back, unchanged.
    assert _catalog.validate_snapshot(empty_snap) is empty_snap
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # -- Phase C: wrap the REAL accepted builder; forbid other boundaries -----
    real_build = _export_mod.build_snapshot
    calls = []

    def _spy(registry, roster_arg, *, cadence_s):
        out = real_build(registry, roster_arg, cadence_s=cadence_s)
        calls.append((registry, roster_arg, cadence_s, out))
        return out

    monkeypatch.setattr(mcp_server._catalog_export, "build_snapshot", _spy)

    def _boom(*args, **kwargs):
        raise AssertionError("helper touched a forbidden boundary")

    monkeypatch.setattr(mcp_server, "_capture_registry", _boom)
    monkeypatch.setattr(mcp_server, "state",
                        types.SimpleNamespace(load_roster=_boom,
                                              load_pending=_boom))
    monkeypatch.setattr(mcp_server, "time", types.SimpleNamespace(time=_boom))
    monkeypatch.setattr(mcp_server, "Path", _boom)

    # -- Phase D: sanitize -> accepted snapshot integration -------------------
    calls.clear()
    snap = mcp_server._local_snapshot(roster, records, base)
    assert len(calls) == 1
    supplied_registry, supplied_roster, supplied_cadence, built = calls[0]
    assert supplied_registry is records
    assert supplied_cadence == 3600
    assert snap is built                       # the EXACT validated object
    assert supplied_roster is not roster
    assert supplied_roster["providers"] is not roster["providers"]
    assert supplied_roster["provider_models"] is roster["provider_models"]

    assert snap["schema_version"] == 1
    assert snap["cadence_s"] == 3600
    assert snap["tick_epoch"] == 0
    assert snap["provider_order"] == ["z-last", "a-first", "m-mid"]
    assert snap["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert set(snap["providers"]) == {"z-last", "a-first", "m-mid"}
    # Zero-ID active provider retained; no metadata warning manufactured.
    assert snap["providers"]["m-mid"]["model_ids"] == []
    assert snap["providers"]["m-mid"]["models"] == {}
    # Canonical literal code-point sorted unique public ids.
    assert snap["providers"]["a-first"]["model_ids"] == [
        " padded ", long_id, "non-free", "shared/model", "unicode/😀",
        "z10", "z2"]
    assert snap["providers"]["z-last"]["model_ids"] == ["shared/model", "z-extra"]
    # Staleness is literal key membership.
    assert snap["providers"]["z-last"]["stale"] is True
    assert snap["providers"]["a-first"]["stale"] is False
    assert snap["providers"]["m-mid"]["stale"] is False
    # Public models drop unknown fields but keep each pair's own context value.
    z_model = snap["providers"]["z-last"]["models"]["shared/model"]
    a_model = snap["providers"]["a-first"]["models"]["shared/model"]
    assert z_model["context_length"] == 8192
    assert a_model["context_length"] == 131072
    assert "unknown_field" not in z_model and "unknown_field" not in a_model
    assert z_model is not z_meta and a_model is not a_meta

    # Sanitizer root failures map to the fixed local payload through the helper.
    for bad_roster in (None, [], "str", {}, {"providers": []}):
        with pytest.raises(mcp_server._StateError) as exc:
            mcp_server._local_snapshot(bad_roster, records, base)
        assert exc.value.payload == {**base, **fixed}
        assert exc.value.payload is not base

    # Deferred malformed records and invalid keys reject structurally.
    for key in ("bad-config-none", "bad-config-list", "bad-config-bool"):
        with pytest.raises(mcp_server._StateError) as exc:
            mcp_server._local_snapshot({"tick_epoch": 0, "providers": {}},
                                       [mal_records[key]], base)
        assert exc.value.payload == {**base, **fixed}
    with pytest.raises(mcp_server._StateError):
        mcp_server._local_snapshot({"providers": {}}, key_records, base)

    # Invalid URL syntax / non-string wiring / missing api_type all reject.
    for rec in (
        {"key": "u", "name": "U", "chat_completions_url": "ftp://u.example/x",
         "api_type": "openai_compatible"},
        {"key": "u", "name": "U", "chat_completions_url": object(),
         "api_type": "openai_compatible"},
        {"key": "u", "name": "U", "chat_completions_url": "https://u.example/x",
         "api_type": None},
        {"key": "u", "name": "U", "chat_completions_url": "https://u.example/x"},
    ):
        with pytest.raises(mcp_server._StateError) as exc:
            mcp_server._local_snapshot({"providers": {"u": []}}, [rec], base)
        assert exc.value.payload == {**base, **fixed}

    # A bad cadence maps to the same fixed local payload (builder CatalogError).
    for bad_cadence in (0, True, False, 1.0):
        monkeypatch.setattr(mcp_server, "CADENCE_S", bad_cadence)
        with pytest.raises(mcp_server._StateError) as exc:
            mcp_server._local_snapshot(
                {"tick_epoch": 0, "providers": {"m-mid": []}},
                [records[2]], base)
        assert exc.value.payload == {**base, **fixed}
    monkeypatch.setattr(mcp_server, "CADENCE_S", 3600)

    # Tick: 0 is KNOWN; a float or negative tick is unknown (+warning).
    for tick, known in ((0, True), (1.0, False), (-1, False)):
        out = mcp_server._local_snapshot(
            {"tick_epoch": tick, "providers": {"m-mid": []}},
            [records[2]], base)
        assert out["tick_epoch"] == (0 if known else None)
        assert ("tick_epoch_unknown" in out["warnings"]) is (not known)

    # Metadata hierarchy: a bad outer/provider/model container is
    # metadata_invalid ALONE on its pair; a missing container is
    # metadata_missing; a zero-ID provider manufactures no warning.
    reg1 = [{"key": "p", "name": "P",
             "chat_completions_url": "https://p.example/x",
             "api_type": "openai_compatible"}]

    def warnings_of(roster_arg):
        return mcp_server._local_snapshot(roster_arg, reg1, base)["warnings"]

    assert warnings_of({"tick_epoch": 0, "providers": {"p": ["m"]},
                        "provider_models": []}) == ["metadata_invalid"]
    assert warnings_of({"tick_epoch": 0, "providers": {"p": ["m"]},
                        "provider_models": {"p": "bad"}}) == ["metadata_invalid"]
    assert warnings_of({"tick_epoch": 0, "providers": {"p": ["m"]},
                        "provider_models": {"p": {"m": []}}}) == [
        "metadata_invalid"]
    assert warnings_of({"tick_epoch": 0, "providers": {"p": ["m"]}}) == [
        "metadata_missing"]
    assert warnings_of({"tick_epoch": 0, "providers": {"p": []},
                        "provider_models": "bad"}) == []

    # The base mapping is never mutated by any helper path.
    assert base == base_before


# ---------- list_free_models local delegation (unit 2j-U1b) ----------

def test_list_free_models_delegates_to_pure_query(monkeypatch, tmp_path):
    """U1b: the LOCAL ``list_free_models`` reads the roster ONCE, adapts the
    captured registry plus that same in-memory roster to the validated public
    snapshot, delegates selection/order/count to the accepted pure query, and
    restores raw local metadata identity on the way out.

    Fully synthetic module globals plus a synthetic roster written to a real
    tmp state dir and read back through the REAL ``state.load_roster``. The
    REAL ``watchdog_catalog.query.list_free_models`` is wrapped directly (the
    local ``_query`` alias does not exist before the runtime switch), so a
    missing ACTUAL delegation fails here as a real RED, never as an
    AttributeError from test setup. Identity assertions compare against the
    ACTUAL loaded JSON object, never a pre-serialization literal.
    """
    import watchdog_catalog.query as real_query

    real_query_list = real_query.list_free_models
    real_load = mcp_server.state.load_roster
    real_capture = mcp_server._capture_registry

    # -- synthetic registry: nonalphabetic display order, two tracked providers
    # sharing a raw id with CONFLICTING raw dicts, plus a zero-ID provider, all
    # with independent URLs.
    configs = {
        "z-last": {"name": "Zeta"},
        "a-first": {"name": "Alpha"},
        "m-mid": {"name": "Mu"},
    }
    wiring = {
        "z-last": {"chat_completions_url": "https://z.example/v1/chat/completions",
                   "api_type": "openai_compatible"},
        "a-first": {"chat_completions_url": "https://a.example/v1/chat/completions",
                    "api_type": "openai_compatible"},
        "m-mid": {"chat_completions_url": "https://m.example/v1/chat/completions",
                  "api_type": "openai_compatible"},
    }
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    monkeypatch.setattr(mcp_server, "CADENCE_S", 3600)

    long_id = "9" * 64
    shared_z = {"name": "Z Shared", "context_length": 8192,
                "unknown_field": "Z-UNKNOWN",
                "architecture": {"input_modalities": ["text"]},
                "params": {"a": 1}, "pricing": {"prompt": "0"}}
    shared_a = {"name": "A Shared", "context_length": 131072,
                "unknown_field": "A-UNKNOWN",
                "architecture": {"input_modalities": ["text", "image"]},
                "pricing": {"prompt": "0"}}
    roster_doc = {
        "tick_epoch": 100,
        "providers": {
            "z-last": ["shared/model", "z-extra", "z-extra", "", "empty-meta"],
            "a-first": ["shared/model", "z2", "z10", " padded ", "non-free",
                        long_id, "unicode/😀", 3, None, ""],
            "m-mid": [],
            "unknown-bad": "not-a-list",
        },
        "provider_models": {
            "z-last": {"shared/model": shared_z, "z-extra": "BAD-CONTAINER",
                       "empty-meta": {}},
            "a-first": {"shared/model": shared_a},
        },
        "stale_providers": ["z-last"],
        "private": {"deep": "SENTINEL"},
    }
    (tmp_path / "state").mkdir()
    roster_path = tmp_path / "state" / "roster.json"
    roster_path.write_text(json.dumps(roster_doc))
    control = json.loads(roster_path.read_text())

    # -- spies: exactly ONE capture and exactly ONE roster read per request ----
    captures = []
    loads = []
    load_guard: dict = {"count": 0}
    swap: dict = {"to": None}
    query_calls = []
    core_args = []
    projected = []
    drop: dict = {"spec": None}
    backstop: dict = {"args": None}

    def _spy_capture(base):
        out = real_capture(base)
        captures.append(out)
        if swap["to"] is not None:
            # Replace the module roots AFTER capture: the rest of the request
            # must still use the already-captured records.
            mcp_server.PROVIDERS, mcp_server.GATEWAY_WIRING = swap["to"]
        return out

    def _spy_load(path):
        out = real_load(path)
        loads.append(out)
        load_guard["count"] += 1
        if load_guard["count"] > 1:
            raise AssertionError("roster must be loaded exactly once per request")
        return out

    def _spy_query(snapshot, arguments):
        call_args = backstop["args"] if backstop["args"] is not None else arguments
        out = real_query_list(snapshot, call_args)
        query_calls.append((snapshot, arguments))
        core_args.append(call_args)
        projected.append(out)
        if drop["spec"] is not None and out.get("ok") and "provider" in out:
            prov, drop_id = drop["spec"]
            if out["provider"] == prov and drop_id in out["model_ids"]:
                out = dict(out)
                out["model_ids"] = [i for i in out["model_ids"] if i != drop_id]
                out["models"] = {k: v for k, v in out["models"].items()
                                 if k != drop_id}
                out["count"] = len(out["model_ids"])
        return out

    monkeypatch.setattr(mcp_server, "_capture_registry", _spy_capture)
    monkeypatch.setattr(mcp_server.state, "load_roster", _spy_load)
    monkeypatch.setattr(real_query, "list_free_models", _spy_query)

    def _reset():
        captures.clear()
        loads.clear()
        query_calls.clear()
        core_args.clear()
        projected.clear()
        load_guard["count"] = 0

    # -- bulk: local selection/order/count follow the returned core result -----
    _reset()
    res = mcp_server.list_free_models(root=tmp_path)
    assert res["ok"] is True
    assert res["tool"] == "list_free_models"
    assert res["state_root"] == str(tmp_path.resolve())
    assert len(captures) == 1 and len(loads) == 1 and len(query_calls) == 1
    snap, sent_args = query_calls[0]
    assert sent_args == {"provider": None}
    assert core_args[0] == {"provider": None}
    assert snap["provider_order"] == ["z-last", "a-first", "m-mid"]
    assert set(res) == {"tool", "state_root", "ok", "providers", "models",
                        "counts", "total_ids", "n_gateways", "snapshot_id",
                        "tick_epoch", "provider_staleness", "warnings"}
    assert set(res["providers"]) == {"z-last", "a-first", "m-mid"}
    assert list(res["providers"]) == ["z-last", "a-first", "m-mid"]
    assert res["providers"]["z-last"] == ["empty-meta", "shared/model", "z-extra"]
    assert res["providers"]["a-first"] == [
        long_id, " padded ", "non-free", "shared/model", "unicode/😀",
        "z2", "z10"]
    assert res["providers"]["m-mid"] == []
    assert res["counts"] == {"z-last": 3, "a-first": 7, "m-mid": 0}
    assert res["total_ids"] == 9
    assert res["n_gateways"] == 3
    assert res["tick_epoch"] == 100
    assert res["snapshot_id"] == snap["snapshot_id"]
    assert res["provider_staleness"] == {"z-last": True, "a-first": False,
                                         "m-mid": False}
    assert res["warnings"] == snap["warnings"] == [
        "metadata_missing", "metadata_invalid"]
    assert res["warnings"] is not snap["warnings"]

    # -- raw metadata restoration: identity, conflicts, empty dict, omission ---
    loaded = loads[0]
    raw_z = loaded["provider_models"]["z-last"]["shared/model"]
    raw_a = loaded["provider_models"]["a-first"]["shared/model"]
    raw_empty = loaded["provider_models"]["z-last"]["empty-meta"]
    assert loaded == control                       # source unchanged by value
    assert set(res["models"]) == {"z-last", "a-first", "m-mid"}
    assert set(res["models"]["z-last"]) == {"empty-meta", "shared/model"}
    assert res["models"]["z-last"]["shared/model"] is raw_z
    assert res["models"]["z-last"]["empty-meta"] is raw_empty
    assert res["models"]["z-last"]["empty-meta"] == {}
    assert "z-extra" not in res["models"]["z-last"]     # non-dict omitted
    assert res["models"]["a-first"] == {"shared/model": raw_a}
    assert res["models"]["m-mid"] == {}
    # conflicting shared raw id stays per-provider, never merged or flattened.
    assert raw_z is not raw_a and raw_z["context_length"] != raw_a["context_length"]
    # raw local maps stay distinct from the projected query values / snapshot.
    proj_z = snap["providers"]["z-last"]["models"]["shared/model"]
    assert "unknown_field" not in proj_z and "params" not in proj_z
    assert "unknown_field" in raw_z and "params" in raw_z
    assert raw_z is not proj_z
    assert res["models"]["z-last"]["shared/model"] is not proj_z
    # no raw unknown-field leak into the snapshot handed to the real query.
    assert "unknown_field" not in snap["providers"]["a-first"]["models"]["shared/model"]
    # the saved core result containers are never mutated by local restoration.
    assert projected[0]["providers"]["z-last"] == res["providers"]["z-last"]
    assert "unknown_field" not in projected[0]["models"]["z-last"]["shared/model"]

    # -- single provider success, and a zero-ID provider success ---------------
    _reset()
    one = mcp_server.list_free_models(provider="z-last", root=tmp_path)
    assert one["ok"] is True and one["provider"] == "z-last"
    assert set(one) == {"tool", "state_root", "ok", "provider", "model_ids",
                        "models", "count", "snapshot_id", "tick_epoch",
                        "provider_staleness", "warnings"}
    assert one["model_ids"] == ["empty-meta", "shared/model", "z-extra"]
    assert one["count"] == 3
    assert one["tick_epoch"] == 100
    assert one["snapshot_id"] == query_calls[0][0]["snapshot_id"]
    assert one["provider_staleness"] == {"z-last": True}
    assert one["warnings"] == ["metadata_missing", "metadata_invalid"]
    one_raw_z = loads[0]["provider_models"]["z-last"]["shared/model"]
    one_raw_empty = loads[0]["provider_models"]["z-last"]["empty-meta"]
    assert one["models"]["shared/model"] is one_raw_z
    assert one["models"]["empty-meta"] is one_raw_empty
    assert "z-extra" not in one["models"]
    assert len(captures) == 1 and len(loads) == 1 and len(query_calls) == 1
    assert core_args[0] == {"provider": "z-last"}

    _reset()
    zero = mcp_server.list_free_models(provider="m-mid", root=tmp_path)
    assert zero["ok"] is True and zero["provider"] == "m-mid"
    assert zero["model_ids"] == [] and zero["count"] == 0
    assert zero["models"] == {} and zero["provider_staleness"] == {"m-mid": False}

    # -- drop mode: local output FOLLOWS the returned core selection ----------
    _reset()
    drop["spec"] = ("z-last", "shared/model")
    try:
        dropped = mcp_server.list_free_models(provider="z-last", root=tmp_path)
    finally:
        drop["spec"] = None
    assert dropped["ok"] is True
    assert dropped["model_ids"] == ["empty-meta", "z-extra"]
    assert dropped["count"] == 2
    assert dropped["models"] == {"empty-meta": raw_empty}
    assert "shared/model" not in dropped["models"]
    assert dropped["snapshot_id"] == projected[0]["snapshot_id"]
    # the roster still tracks the removed id, and the SAVED core result is intact
    assert "shared/model" in loaded["providers"]["z-last"]
    assert "shared/model" in projected[0]["model_ids"]
    assert projected[0]["count"] == 3

    # -- legitimately empty captured registry is a valid empty success --------
    _reset()
    monkeypatch.setattr(mcp_server, "PROVIDERS", {})
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    roster_path.write_text(json.dumps({"tick_epoch": 0, "providers": {}}))
    empty = mcp_server.list_free_models(root=tmp_path)
    assert empty["ok"] is True
    assert empty["providers"] == {} and empty["models"] == {}
    assert empty["counts"] == {} and empty["total_ids"] == 0
    assert empty["n_gateways"] == 0
    assert empty["tick_epoch"] == 0
    assert empty["warnings"] == []
    assert len(captures) == 1 and captures[0] == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    roster_path.write_text(json.dumps(roster_doc))

    # -- exactly one capture stays authoritative when the roots are replaced ---
    conflicting = {"bogus": {"name": "Bogus"}}
    conflicting_wiring = {"bogus": {
        "chat_completions_url": "https://bogus.example/v1/chat/completions",
        "api_type": "openai_compatible"}}
    _reset()
    swap["to"] = (conflicting, conflicting_wiring)
    kept = mcp_server.list_free_models(root=tmp_path)
    assert kept["ok"] is True
    assert len(captures) == 1
    assert [r["key"] for r in captures[0]] == ["z-last", "a-first", "m-mid"]
    assert set(kept["providers"]) == {"z-last", "a-first", "m-mid"}
    assert list(kept["providers"]) == ["z-last", "a-first", "m-mid"]
    assert kept["counts"] == {"z-last": 3, "a-first": 7, "m-mid": 0}
    assert "bogus" not in kept["providers"]
    assert mcp_server.PROVIDERS == conflicting      # the swap really happened
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    _reset()
    swap["to"] = (conflicting, conflicting_wiring)
    try:
        swapped_bad = mcp_server.list_free_models(provider="nope", root=tmp_path)
    finally:
        swap["to"] = None
    assert swapped_bad["ok"] is False
    assert swapped_bad["valid_providers"] == ["z-last", "a-first", "m-mid"]
    assert "bogus" not in swapped_bad["valid_providers"]
    assert swapped_bad["error"] == (
        "unknown provider 'nope'; valid providers: z-last, a-first, m-mid")
    assert len(captures) == 1 and loads == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # -- legacy provider argument boundary, before any state read -------------
    for bad_provider in (7, True, ["z-last"], {"provider": "z-last"}, "",
                         " padded ", "Z-LAST", "nope"):
        _reset()
        bad_arg = mcp_server.list_free_models(provider=bad_provider, root=tmp_path)
        assert bad_arg["ok"] is False
        assert bad_arg["valid_providers"] == ["z-last", "a-first", "m-mid"]
        assert bad_arg["error"] == (
            f"unknown provider {bad_provider!r}; valid providers: "
            "z-last, a-first, m-mid")
        assert loads == [] and query_calls == []

    # a bad registry ROOT outranks every argument error (capture guard first).
    monkeypatch.setattr(mcp_server, "PROVIDERS", [])
    _reset()
    root_guard = mcp_server.list_free_models(provider=7, root=tmp_path)
    assert root_guard["ok"] is False
    assert root_guard["error"] == "Invalid catalogue snapshot."
    assert set(root_guard) == {"tool", "state_root", "ok", "error"}
    assert loads == [] and query_calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    # -- missing/corrupt state outranks a deferred wiring failure -------------
    monkeypatch.setattr(mcp_server, "PROVIDERS", {"z-last": {"name": "Zeta"}})
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    _reset()
    missing = mcp_server.list_free_models(root=tmp_path / "nope")
    assert missing["ok"] is False and "roster" in missing["error"].lower()
    corrupt_root = tmp_path / "corrupt"
    (corrupt_root / "state").mkdir(parents=True)
    (corrupt_root / "state" / "roster.json").write_text("{not json!!")
    _reset()
    corrupt = mcp_server.list_free_models(root=corrupt_root)
    assert corrupt["ok"] is False and "corrupt" in corrupt["error"].lower()
    # valid state, bad wiring -> structural rejection, never a silent success.
    _reset()
    bad_wiring = mcp_server.list_free_models(root=tmp_path)
    assert bad_wiring["ok"] is False
    assert bad_wiring["error"] == "Invalid catalogue snapshot."
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    monkeypatch.setattr(mcp_server, "CADENCE_S", 0)
    _reset()
    bad_cadence = mcp_server.list_free_models(root=tmp_path)
    assert bad_cadence["ok"] is False
    assert bad_cadence["error"] == "Invalid catalogue snapshot."
    monkeypatch.setattr(mcp_server, "CADENCE_S", 3600)

    # -- both core provider-error codes map to the SAME local provider error ---
    for core_arg, core_code in (({"provider": "not-captured"}, "unknown_provider"),
                                ({"provider": 7}, "invalid_arguments")):
        _reset()
        backstop["args"] = core_arg
        try:
            bs = mcp_server.list_free_models(root=tmp_path)
        finally:
            backstop["args"] = None
        assert bs["ok"] is False
        assert bs["error"] == (
            "unknown provider None; valid providers: z-last, a-first, m-mid")
        assert bs["valid_providers"] == ["z-last", "a-first", "m-mid"]
        for absent in ("count", "counts", "total_ids", "n_gateways"):
            assert absent not in bs
        assert projected[-1]["ok"] is False
        assert projected[-1]["error"]["code"] == core_code

    # -- bad metadata OUTER container: omission + truthful warning, no fiction -
    roster_path.write_text(json.dumps({
        "tick_epoch": 0,
        "providers": {"z-last": ["shared/model"]},
        "provider_models": [],
    }))
    _reset()
    bad_meta = mcp_server.list_free_models(root=tmp_path)
    assert bad_meta["ok"] is True
    assert bad_meta["providers"]["z-last"] == ["shared/model"]
    assert bad_meta["models"] == {"z-last": {}, "a-first": {}, "m-mid": {}}
    assert bad_meta["warnings"] == ["metadata_invalid"]
    roster_path.write_text(json.dumps(roster_doc))

    # -- freshness, unchanged sources, mutation isolation ---------------------
    _reset()
    fresh1 = mcp_server.list_free_models(root=tmp_path)
    assert len(captures) == 1 and len(loads) == 1 and len(query_calls) == 1
    assert fresh1["warnings"] == ["metadata_missing", "metadata_invalid"]
    snap_a, _ = query_calls[0]
    proj_a = projected[0]
    load1 = loads[0]
    assert load1 == control
    assert captures[0] == [
        {"key": "z-last", "name": "Zeta",
         "chat_completions_url": "https://z.example/v1/chat/completions",
         "api_type": "openai_compatible"},
        {"key": "a-first", "name": "Alpha",
         "chat_completions_url": "https://a.example/v1/chat/completions",
         "api_type": "openai_compatible"},
        {"key": "m-mid", "name": "Mu",
         "chat_completions_url": "https://m.example/v1/chat/completions",
         "api_type": "openai_compatible"},
    ]

    _reset()
    fresh2 = mcp_server.list_free_models(root=tmp_path)
    assert len(loads) == 1
    assert fresh2["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert fresh2["warnings"] is not fresh1["warnings"]
    assert loads[0] == control

    # a local raw mutation touches only the intentionally aliased captured raw
    # dict -- never the projected snapshot, the saved core result, or a fresh
    # independently loaded request.
    fresh1["models"]["z-last"]["shared/model"]["name"] = "MUTATED"
    assert snap_a["providers"]["z-last"]["models"]["shared/model"]["name"] == "Z Shared"
    assert proj_a["models"]["z-last"]["shared/model"]["name"] == "Z Shared"
    assert load1["provider_models"]["z-last"]["shared/model"]["name"] == "MUTATED"
    _reset()
    fresh3 = mcp_server.list_free_models(root=tmp_path)
    assert fresh3["models"]["z-last"]["shared/model"]["name"] == "Z Shared"
