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
            "a-first": ["shared/model", "z2", "z10", "z02", "z\u0669",
                        " padded ", "non-free", long_id, "unicode/😀", 3,
                        None, ""],
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
        "z2", "z02", "z10", "z\u0669"]
    assert res["providers"]["m-mid"] == []
    assert res["counts"] == {"z-last": 3, "a-first": 9, "m-mid": 0}
    assert res["total_ids"] == 11
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
    assert kept["counts"] == {"z-last": 3, "a-first": 9, "m-mid": 0}
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

    # -- bad metadata container: an OUTER list OR a provider-level list both
    # degrade to omission + a truthful warning, never fabricated metadata.
    for bad_provider_models in ([], {"z-last": []}):
        roster_path.write_text(json.dumps({
            "tick_epoch": 0,
            "providers": {"z-last": ["shared/model"]},
            "provider_models": bad_provider_models,
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


# ---------- get_model local delegation (unit 2j-U2) ----------

def test_get_model_delegates_to_pure_query(monkeypatch, tmp_path):
    """U2: the LOCAL ``get_model`` captures the registry ONCE, validates the
    literal argument against the frozen ASCII blank set, reads the roster ONCE,
    adapts that same observation to the validated public snapshot, delegates
    selection/order/counts to the accepted pure query, and restores original
    raw metadata behind per-endpoint ``gateway`` keys.

    Fully synthetic module globals plus a synthetic roster written to a real
    tmp state dir and read back through the REAL ``state.load_roster``. The
    REAL ``watchdog_catalog.query.get_model`` is wrapped directly, so a missing
    ACTUAL delegation fails here as a real RED, never as an AttributeError from
    test setup. Independent literal expectations (membership, order, counts,
    identities) are asserted alongside the spy, and identity is compared
    against the ACTUAL loaded JSON object, never a pre-serialization literal.
    """
    import copy

    import watchdog_catalog.query as real_query

    real_query_get = real_query.get_model
    real_load = mcp_server.state.load_roster
    real_capture = mcp_server._capture_registry

    # -- synthetic registry: nonalphabetic display order, two tracked providers
    # sharing a raw id with CONFLICTING raw dicts, plus a zero-ID provider, all
    # with distinct wiring.
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
            "z-last": ["shared/model", "z-extra-free", "dup", "dup", "",
                       3, None, "empty-meta", long_id],
            "a-first": ["shared/model", "plain", " padded ", "non-free",
                        "z2", "z02", "z10", "z\u0669",
                        "g/model", "g/model-free", "g/model:free",
                        "g/model_free", "trk:free", "   "],
            "m-mid": [],
            "unknown-bad": "not-a-list",
        },
        "provider_models": {
            "z-last": {"shared/model": shared_z, "z-extra-free": "BAD-CONTAINER",
                       "empty-meta": {}, "dup": {"name": "DupZ"}},
            "a-first": {"shared/model": shared_a,
                        "trk:free": {"name": "Trk", "unknown_field": "TRK"},
                        "   ": {"name": "Blanks"}},
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
    calls = []
    backstop: dict = {"args": None}
    drop: dict = {"gateway": None}

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
        before = copy.deepcopy(snapshot)
        core_args = backstop["args"] if backstop["args"] is not None else arguments
        out = real_query_get(snapshot, core_args)
        if drop["gateway"] is not None and out.get("ok"):
            kept = [ep for ep in out["endpoints"]
                    if ep["provider"] != drop["gateway"]]
            if len(kept) != len(out["endpoints"]):
                out = dict(out)
                out["endpoints"] = kept
                out["counts"] = dict(out["counts"])
                out["counts"]["endpoints"] = len(kept)
                out["counts"]["distinct_raw_ids"] = len(
                    {ep["model_id"] for ep in kept})
                out["counts"]["marker_group_labels"] = len(
                    {real_query.strip_free_marker(ep["model_id"])
                     for ep in kept})
        calls.append({"snapshot": snapshot, "before": before, "result": out,
                      "arguments": arguments, "core_args": core_args})
        return out

    monkeypatch.setattr(mcp_server, "_capture_registry", _spy_capture)
    monkeypatch.setattr(mcp_server.state, "load_roster", _spy_load)
    monkeypatch.setattr(real_query, "get_model", _spy_query)

    def _reset():
        captures.clear()
        loads.clear()
        calls.clear()
        load_guard["count"] = 0

    # -- literal exact membership across providers, one capture/load/core each -
    _reset()
    res = mcp_server.get_model("shared/model", root=tmp_path)
    assert res["ok"] is True
    assert res["tool"] == "get_model"
    assert res["state_root"] == str(tmp_path.resolve())
    assert len(captures) == 1 and len(loads) == 1 and len(calls) == 1
    assert calls[0]["arguments"] == {"model_id": "shared/model"}
    assert calls[0]["core_args"] == {"model_id": "shared/model"}
    snap = calls[0]["snapshot"]
    assert snap["provider_order"] == ["z-last", "a-first", "m-mid"]
    # local sanitization: empty/non-string/unknown-provider IDs dropped,
    # duplicates collapsed, code-point order retained into the adapter.
    assert snap["providers"]["z-last"]["model_ids"] == [
        long_id, "dup", "empty-meta", "shared/model", "z-extra-free"]
    assert "   " in snap["providers"]["a-first"]["model_ids"]
    assert "unknown-bad" not in snap["providers"]
    assert set(res) == {
        "tool", "state_root", "ok", "query", "snapshot_id", "tick_epoch",
        "exact_matches", "exact_gateways", "provider_staleness", "endpoints",
        "caveats", "counts", "warnings"}
    assert res["query"] == "shared/model"
    # zero-ID providers stay in the exact and staleness maps.
    assert res["exact_matches"] == {
        "z-last": True, "a-first": True, "m-mid": False}
    assert res["exact_gateways"] == ["z-last", "a-first"]
    assert res["provider_staleness"] == {
        "z-last": True, "a-first": False, "m-mid": False}
    assert res["tick_epoch"] == 100
    assert res["snapshot_id"] == snap["snapshot_id"]
    assert res["snapshot_id"] == calls[0]["result"]["snapshot_id"]
    assert res["counts"] == {"exact_matches": 2, "endpoints": 2,
                             "distinct_raw_ids": 1, "marker_group_labels": 1}
    assert res["caveats"] == [mcp_server.ABSENCE_CAVEAT]
    assert res["caveats"] == [real_query._ABSENCE_CAVEAT]
    assert real_query._ABSENCE_CAVEAT == (
        "Cross-gateway ABSENCE is unreliable: gateways rename inconsistently, "
        "so a missing exact id does NOT prove the model is unavailable there.")
    assert res["warnings"] == snap["warnings"] == [
        "metadata_missing", "metadata_invalid"]
    assert res["warnings"] is not snap["warnings"]

    # -- endpoints: display order, local gateway naming, core stale kept, and
    # ORIGINAL raw metadata identity restored from the SAME loaded roster -----
    loaded = loads[0]
    raw_z = loaded["provider_models"]["z-last"]["shared/model"]
    raw_a = loaded["provider_models"]["a-first"]["shared/model"]
    assert loaded == control                       # source unchanged by value
    assert [ep["gateway"] for ep in res["endpoints"]] == ["z-last", "a-first"]
    assert [ep["model_id"] for ep in res["endpoints"]] == [
        "shared/model", "shared/model"]
    for ep in res["endpoints"]:
        assert list(ep) == ["gateway", "model_id", "chat_completions_url",
                            "api_type", "stale", "metadata"]
        assert "provider" not in ep
    assert [ep["stale"] for ep in res["endpoints"]] == [True, False]
    assert res["endpoints"][0]["chat_completions_url"] == (
        "https://z.example/v1/chat/completions")
    assert res["endpoints"][1]["chat_completions_url"] == (
        "https://a.example/v1/chat/completions")
    assert res["endpoints"][0]["metadata"] is raw_z
    assert res["endpoints"][1]["metadata"] is raw_a
    # conflicting shared raw id stays per-provider, never merged or flattened.
    assert raw_z is not raw_a
    assert raw_z["context_length"] != raw_a["context_length"]
    assert raw_z["unknown_field"] == "Z-UNKNOWN"
    assert raw_z["params"] == {"a": 1}
    # raw local metadata stays distinct from the projected public model object.
    proj_z = snap["providers"]["z-last"]["models"]["shared/model"]
    assert "unknown_field" not in proj_z and "params" not in proj_z
    assert res["endpoints"][0]["metadata"] is not proj_z
    assert "unknown_field" not in calls[0]["result"]["endpoints"][0]["metadata"]
    # neither the validated snapshot nor the returned core result was mutated.
    assert snap == calls[0]["before"]
    assert [ep["provider"] for ep in calls[0]["result"]["endpoints"]] == [
        "z-last", "a-first"]
    assert "gateway" not in calls[0]["result"]["endpoints"][0]

    # -- literal vs marker-only lookup, and raw-query/stripped equivalence -----
    _reset()
    marker_only = mcp_server.get_model("trk", root=tmp_path)
    assert marker_only["ok"] is True
    assert marker_only["exact_matches"] == {
        "z-last": False, "a-first": False, "m-mid": False}
    assert marker_only["exact_gateways"] == []
    assert [ep["gateway"] for ep in marker_only["endpoints"]] == ["a-first"]
    assert marker_only["endpoints"][0]["model_id"] == "trk:free"
    assert marker_only["counts"] == {
        "exact_matches": 0, "endpoints": 1, "distinct_raw_ids": 1,
        "marker_group_labels": 1}
    trk_meta = loads[0]["provider_models"]["a-first"]["trk:free"]
    assert marker_only["endpoints"][0]["metadata"] is trk_meta
    assert marker_only["endpoints"][0]["metadata"]["unknown_field"] == "TRK"

    _reset()
    raw_variant = mcp_server.get_model("trk:free", root=tmp_path)
    assert raw_variant["exact_matches"] == {
        "z-last": False, "a-first": True, "m-mid": False}
    assert raw_variant["exact_gateways"] == ["a-first"]
    assert raw_variant["endpoints"] == marker_only["endpoints"]  # same wiring
    assert raw_variant["counts"]["exact_matches"] == 1

    # -- marker-group order is the accepted natural raw-ID order --------------
    _reset()
    grouped = mcp_server.get_model("g/model", root=tmp_path)
    assert grouped["ok"] is True
    assert grouped["exact_gateways"] == ["a-first"]
    assert [ep["model_id"] for ep in grouped["endpoints"]] == [
        "g/model", "g/model-free", "g/model:free", "g/model_free"]
    assert all(ep["gateway"] == "a-first" for ep in grouped["endpoints"])
    assert grouped["counts"] == {
        "exact_matches": 1, "endpoints": 4, "distinct_raw_ids": 4,
        "marker_group_labels": 1}
    # duplicates collapse to ONE pair (the roster listed "dup" twice).
    _reset()
    dup = mcp_server.get_model("dup", root=tmp_path)
    assert dup["exact_gateways"] == ["z-last"]
    assert [ep["model_id"] for ep in dup["endpoints"]] == ["dup"]
    assert dup["counts"]["endpoints"] == 1

    # -- non-free and padded literal ids: exact stays literal, no trimming ----
    _reset()
    nonfree = mcp_server.get_model("non-free", root=tmp_path)
    assert nonfree["exact_gateways"] == ["a-first"]
    # the query "non-free" strips to "non", and the tracked id "non-free"
    # strips to "non" too, so it is its own marker-group endpoint -- exact
    # membership and the marker lookup stay SEPARATE signals that agree here.
    assert [ep["model_id"] for ep in nonfree["endpoints"]] == ["non-free"]
    assert nonfree["counts"] == {
        "exact_matches": 1, "endpoints": 1, "distinct_raw_ids": 1,
        "marker_group_labels": 1}
    _reset()
    non = mcp_server.get_model("non", root=tmp_path)
    assert non["exact_gateways"] == []
    assert [ep["model_id"] for ep in non["endpoints"]] == ["non-free"]

    _reset()
    padded = mcp_server.get_model(" padded ", root=tmp_path)
    assert padded["exact_matches"]["a-first"] is True
    assert [ep["model_id"] for ep in padded["endpoints"]] == [" padded "]
    assert padded["endpoints"][0]["metadata"] == {}
    _reset()
    unpadded = mcp_server.get_model("padded", root=tmp_path)
    assert unpadded["exact_gateways"] == []
    assert unpadded["endpoints"] == []

    # -- Unicode-digit and leading-zero literal ids stay literal --------------
    _reset()
    nonascii_digit = mcp_server.get_model("z\u0669", root=tmp_path)
    assert nonascii_digit["exact_matches"]["a-first"] is True
    assert [ep["model_id"] for ep in nonascii_digit["endpoints"]] == ["z\u0669"]
    assert nonascii_digit["counts"]["endpoints"] == 1
    _reset()
    leading_zero = mcp_server.get_model("z02", root=tmp_path)
    assert leading_zero["exact_matches"]["a-first"] is True
    assert [ep["model_id"] for ep in leading_zero["endpoints"]] == ["z02"]
    _reset()
    plain_two = mcp_server.get_model("z2", root=tmp_path)
    assert plain_two["exact_matches"]["a-first"] is True
    assert [ep["model_id"] for ep in plain_two["endpoints"]] == ["z2"]

    # -- no-match success keeps the honest empty shape ------------------------
    _reset()
    nothing = mcp_server.get_model("nonexistent/model", root=tmp_path)
    assert nothing["ok"] is True
    assert nothing["exact_gateways"] == []
    assert nothing["endpoints"] == []
    assert nothing["counts"] == {
        "exact_matches": 0, "endpoints": 0, "distinct_raw_ids": 0,
        "marker_group_labels": 0}
    assert nothing["caveats"] == [mcp_server.ABSENCE_CAVEAT]
    assert nothing["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert len(captures) == 1 and len(loads) == 1 and len(calls) == 1

    # -- the frozen ASCII blank set: empty/ASCII-blank/non-string arguments
    # fail BEFORE any roster read, and a tracked ASCII-blank-only id does NOT
    # make such a query valid.
    for bad_value in (7, True, None, ["shared/model"], {"model_id": "x"},
                      "", " ", "   ", "\t\n\r\v\f"):
        _reset()
        bad = mcp_server.get_model(bad_value, root=tmp_path)
        assert bad["ok"] is False
        assert bad["error"] == "model_id must be a non-empty string"
        assert set(bad) == {"tool", "state_root", "ok", "error"}
        assert loads == [] and calls == []
    # a tracked literal ASCII-blank id exists in the roster above; the query is
    # still rejected before state I/O because validation runs first.
    assert "   " in control["providers"]["a-first"]

    # -- NBSP (and other non-ASCII whitespace) is a LITERAL valid query -------
    _reset()
    nbsp_miss = mcp_server.get_model("\u00a0", root=tmp_path)
    assert nbsp_miss["ok"] is True
    assert nbsp_miss["query"] == "\u00a0"
    assert nbsp_miss["exact_gateways"] == [] and nbsp_miss["endpoints"] == []
    _reset()
    em_space = mcp_server.get_model("\u2003", root=tmp_path)
    assert em_space["ok"] is True and em_space["endpoints"] == []
    # now TRACK the NBSP id: the same query is a real literal membership.
    tracked = copy.deepcopy(control)
    tracked["providers"]["a-first"].append("\u00a0")
    roster_path.write_text(json.dumps(tracked))
    _reset()
    nbsp_hit = mcp_server.get_model("\u00a0", root=tmp_path)
    assert nbsp_hit["ok"] is True
    assert nbsp_hit["exact_matches"]["a-first"] is True
    assert nbsp_hit["exact_gateways"] == ["a-first"]
    assert [ep["model_id"] for ep in nbsp_hit["endpoints"]] == ["\u00a0"]
    assert nbsp_hit["endpoints"][0]["metadata"] == {}
    roster_path.write_text(json.dumps(roster_doc))

    # -- malformed metadata containers: omitted-or-empty raw metadata, truthful
    # warnings, and empty-dict IDENTITY preserved (never ``raw or {}``).
    _reset()
    empty_meta = mcp_server.get_model("empty-meta", root=tmp_path)
    empty_raw = loads[0]["provider_models"]["z-last"]["empty-meta"]
    assert empty_meta["exact_gateways"] == ["z-last"]
    assert empty_meta["endpoints"][0]["metadata"] is empty_raw
    assert empty_meta["endpoints"][0]["metadata"] == {}
    assert empty_meta["warnings"] == ["metadata_missing", "metadata_invalid"]

    _reset()
    bad_model = mcp_server.get_model("z-extra-free", root=tmp_path)
    assert bad_model["exact_gateways"] == ["z-last"]
    assert bad_model["endpoints"][0]["metadata"] == {}
    assert bad_model["endpoints"][0]["metadata"] is not "BAD-CONTAINER"
    # warnings describe the WHOLE validated snapshot, never just this endpoint.
    assert bad_model["warnings"] == ["metadata_missing", "metadata_invalid"]

    _reset()
    missing_meta = mcp_server.get_model("plain", root=tmp_path)
    assert missing_meta["exact_gateways"] == ["a-first"]
    assert missing_meta["endpoints"][0]["metadata"] == {}
    assert missing_meta["warnings"] == ["metadata_missing", "metadata_invalid"]

    for bad_outer in ([], {"z-last": []}):
        roster_path.write_text(json.dumps({
            "tick_epoch": 0,
            "providers": {"z-last": ["shared/model"]},
            "provider_models": bad_outer,
        }))
        _reset()
        bad_meta = mcp_server.get_model("shared/model", root=tmp_path)
        assert bad_meta["ok"] is True
        assert bad_meta["exact_matches"] == {
            "z-last": True, "a-first": False, "m-mid": False}
        assert [ep["gateway"] for ep in bad_meta["endpoints"]] == ["z-last"]
        assert bad_meta["endpoints"][0]["metadata"] == {}
        assert "metadata_invalid" in bad_meta["warnings"]
    roster_path.write_text(json.dumps({"tick_epoch": 0,
                                       "providers": {"z-last": ["shared/model"]}}))
    _reset()
    absent_meta = mcp_server.get_model("shared/model", root=tmp_path)
    assert absent_meta["ok"] is True
    assert absent_meta["endpoints"][0]["metadata"] == {}
    assert absent_meta["warnings"] == ["metadata_missing"]
    roster_path.write_text(json.dumps(roster_doc))

    # -- wrapper subset: local output FOLLOWS the returned endpoints/counts ---
    _reset()
    drop["gateway"] = "a-first"
    try:
        subset = mcp_server.get_model("shared/model", root=tmp_path)
    finally:
        drop["gateway"] = None
    assert subset["ok"] is True
    assert [ep["gateway"] for ep in subset["endpoints"]] == ["z-last"]
    subset_raw_z = loads[0]["provider_models"]["z-last"]["shared/model"]
    assert subset["endpoints"][0]["metadata"] is subset_raw_z
    assert subset["counts"] == {
        "exact_matches": 2, "endpoints": 1, "distinct_raw_ids": 1,
        "marker_group_labels": 1}
    # the roster still tracks the removed pair, and the core result is coherent.
    assert "shared/model" in loads[0]["providers"]["a-first"]
    assert [ep["provider"] for ep in calls[0]["result"]["endpoints"]] == [
        "z-last"]
    assert calls[0]["result"]["counts"]["endpoints"] == 1

    # -- legitimately empty captured registry is a valid empty success --------
    _reset()
    monkeypatch.setattr(mcp_server, "PROVIDERS", {})
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    roster_path.write_text(json.dumps({"tick_epoch": 0, "providers": {}}))
    empty = mcp_server.get_model("shared/model", root=tmp_path)
    assert empty["ok"] is True
    assert empty["exact_matches"] == {} and empty["exact_gateways"] == []
    assert empty["provider_staleness"] == {}
    assert empty["endpoints"] == []
    assert empty["counts"] == {
        "exact_matches": 0, "endpoints": 0, "distinct_raw_ids": 0,
        "marker_group_labels": 0}
    assert empty["tick_epoch"] == 0 and empty["warnings"] == []
    assert len(captures) == 1 and captures[0] == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    roster_path.write_text(json.dumps(roster_doc))

    # -- exactly one capture stays authoritative when the roots are replaced --
    conflicting = {"bogus": {"name": "Bogus"}}
    conflicting_wiring = {"bogus": {
        "chat_completions_url": "https://bogus.example/v1/chat/completions",
        "api_type": "openai_compatible"}}
    _reset()
    swap["to"] = (conflicting, conflicting_wiring)
    try:
        kept = mcp_server.get_model("shared/model", root=tmp_path)
    finally:
        swap["to"] = None
    assert kept["ok"] is True
    assert len(captures) == 1
    assert [r["key"] for r in captures[0]] == ["z-last", "a-first", "m-mid"]
    assert set(kept["exact_matches"]) == {"z-last", "a-first", "m-mid"}
    assert "bogus" not in kept["exact_matches"]
    assert [ep["gateway"] for ep in kept["endpoints"]] == ["z-last", "a-first"]
    assert mcp_server.PROVIDERS == conflicting      # the swap really happened
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # -- capture-root and deferred structural failures stay fixed local errors -
    monkeypatch.setattr(mcp_server, "PROVIDERS", [])
    _reset()
    root_guard = mcp_server.get_model("", root=tmp_path)
    assert root_guard["ok"] is False
    assert root_guard["error"] == "Invalid catalogue snapshot."
    assert set(root_guard) == {"tool", "state_root", "ok", "error"}
    assert loads == [] and calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    _reset()
    bad_wiring = mcp_server.get_model("shared/model", root=tmp_path)
    assert bad_wiring["ok"] is False
    assert bad_wiring["error"] == "Invalid catalogue snapshot."
    assert calls == []
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    monkeypatch.setattr(mcp_server, "PROVIDERS", {"z-last": "not-a-dict"})
    _reset()
    bad_record = mcp_server.get_model("shared/model", root=tmp_path)
    assert bad_record["ok"] is False
    assert bad_record["error"] == "Invalid catalogue snapshot."
    assert calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    monkeypatch.setattr(mcp_server, "CADENCE_S", 0)
    _reset()
    bad_cadence = mcp_server.get_model("shared/model", root=tmp_path)
    assert bad_cadence["ok"] is False
    assert bad_cadence["error"] == "Invalid catalogue snapshot."
    assert calls == []
    monkeypatch.setattr(mcp_server, "CADENCE_S", 3600)

    # -- missing/corrupt/root/provider-map state failures ---------------------
    _reset()
    missing = mcp_server.get_model("shared/model", root=tmp_path / "nope")
    assert missing["ok"] is False and "roster" in missing["error"].lower()
    assert calls == []
    corrupt_root = tmp_path / "corrupt"
    (corrupt_root / "state").mkdir(parents=True)
    (corrupt_root / "state" / "roster.json").write_text("{not json!!")
    _reset()
    corrupt = mcp_server.get_model("shared/model", root=corrupt_root)
    assert corrupt["ok"] is False and "corrupt" in corrupt["error"].lower()
    assert calls == []
    map_root = tmp_path / "map"
    (map_root / "state").mkdir(parents=True)
    (map_root / "state" / "roster.json").write_text(json.dumps(
        {"tick_epoch": 100, "providers": []}))
    _reset()
    bad_map = mcp_server.get_model("shared/model", root=map_root)
    assert bad_map["ok"] is False
    assert "'providers' is not a map" in bad_map["error"]
    assert calls == []
    # local invalid argument outranks missing state.
    _reset()
    bad_arg_first = mcp_server.get_model("", root=tmp_path / "nope")
    assert bad_arg_first["ok"] is False
    assert bad_arg_first["error"] == "model_id must be a non-empty string"
    assert loads == [] and calls == []
    # missing state outranks a deferred wiring failure.
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    _reset()
    state_first = mcp_server.get_model("shared/model", root=tmp_path / "nope")
    assert state_first["ok"] is False and "roster" in state_first["error"].lower()
    assert calls == []
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # -- the real-core invalid-argument envelope maps to the SAME flat error --
    _reset()
    backstop["args"] = {"model_id": ""}
    try:
        backstop_error = mcp_server.get_model("shared/model", root=tmp_path)
    finally:
        backstop["args"] = None
    assert backstop_error["ok"] is False
    assert backstop_error["error"] == "model_id must be a non-empty string"
    assert set(backstop_error) == {"tool", "state_root", "ok", "error"}
    assert calls[-1]["result"]["ok"] is False
    assert calls[-1]["result"]["error"] == {
        "code": "invalid_arguments", "message": "Invalid tool arguments.",
        "parameter": "model_id"}

    # unexpected PROGRAMMING exceptions are never swallowed at this boundary.
    def _boom(snapshot, arguments):
        raise RuntimeError("programming exception")

    _reset()
    monkeypatch.setattr(real_query, "get_model", _boom)
    with pytest.raises(RuntimeError, match="programming exception"):
        mcp_server.get_model("shared/model", root=tmp_path)
    monkeypatch.setattr(real_query, "get_model", _spy_query)

    # -- freshness, unchanged sources, mutation isolation ---------------------
    _reset()
    fresh1 = mcp_server.get_model("shared/model", root=tmp_path)
    assert len(captures) == 1 and len(loads) == 1 and len(calls) == 1
    snap_a = calls[0]["snapshot"]
    before_a = calls[0]["before"]
    proj_a = calls[0]["result"]
    load1 = loads[0]
    assert load1 == control
    assert snap_a == before_a
    assert fresh1["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert fresh1["warnings"] is not snap_a["warnings"]

    _reset()
    fresh2 = mcp_server.get_model("shared/model", root=tmp_path)
    assert len(loads) == 1
    assert fresh2["warnings"] == fresh1["warnings"]
    assert fresh2["warnings"] is not fresh1["warnings"]
    assert loads[0] == control

    # a local raw mutation touches only the intentionally aliased captured raw
    # dict -- never the projected snapshot, the saved core result, or a fresh
    # independently loaded request.
    fresh1["endpoints"][0]["metadata"]["name"] = "MUTATED"
    assert snap_a["providers"]["z-last"]["models"]["shared/model"]["name"] == "Z Shared"
    assert proj_a["endpoints"][0]["metadata"]["name"] == "Z Shared"
    assert load1["provider_models"]["z-last"]["shared/model"]["name"] == "MUTATED"
    _reset()
    fresh3 = mcp_server.get_model("shared/model", root=tmp_path)
    assert fresh3["endpoints"][0]["metadata"]["name"] == "Z Shared"


# ---------- list_endpoints local delegation (unit 2j-U3) ----------

def test_list_endpoints_delegates_to_pure_query(monkeypatch, tmp_path):
    """U3: the LOCAL ``list_endpoints`` captures the registry ONCE, validates
    the provider argument against the CAPTURED names, reads the roster ONCE,
    adapts that same observation to the validated public snapshot, delegates
    provider selection, raw-ID ordering and every count to the accepted pure
    query, and rebuilds only the gateway ``models`` maps from the ORIGINAL raw
    metadata dictionaries of the same retained roster.

    Fully synthetic module globals plus a synthetic roster written to a real
    tmp state dir and read back through the REAL ``state.load_roster``. The
    REAL ``watchdog_catalog.query.list_endpoints`` is wrapped directly, so a
    missing ACTUAL delegation fails here as a real RED, never as an
    AttributeError from test setup. Independent literal expectations (order,
    identities, the five count definitions, scopes) are asserted alongside the
    spy, and identity is compared against the ACTUAL loaded JSON object, never
    a pre-serialization literal.
    """
    import copy

    import watchdog_catalog.query as real_query

    real_query_endpoints = real_query.list_endpoints
    real_load = mcp_server.state.load_roster
    real_capture = mcp_server._capture_registry

    # -- synthetic registry: nonalphabetic display order, two tracked providers
    # sharing a raw id with CONFLICTING raw dicts, and a zero-ID provider that
    # is ALSO missing from the roster providers map. Distinct wiring per key.
    configs = {
        "z-last": {"name": "Zeta"},
        "a-first": {"name": "Alpha"},
        "m-mid": {"name": "Mu"},
    }
    wiring = {
        "z-last": {"chat_completions_url":
                   "https://z.example/v1/chat/completions",
                   "api_type": "openai_compatible"},
        "a-first": {"chat_completions_url":
                    "https://a.example/v1/chat/completions",
                    "api_type": "openai_compatible"},
        "m-mid": {"chat_completions_url":
                  "https://m.example/v1/chat/completions",
                  "api_type": "anthropic_compatible"},
    }
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    monkeypatch.setattr(mcp_server, "CADENCE_S", 3600)

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
            # duplicates, empty, non-string, padded, non-free and literal
            # non-ASCII/leading-zero ids all in ONE tracked provider.
            "z-last": ["shared/model", "z-extra-free", "dup", "dup", "",
                       3, None, "empty-meta", " padded ", "non-free",
                       "z2", "z02", "z10", "z\u0669", True],
            "a-first": ["shared/model", "plain", "trk:free", "trk-free",
                        "   "],
            # an UNKNOWN malformed provider entry: excluded by the local
            # sanitizer before the accepted builder ever validates it.
            "unknown-bad": "not-a-list",
        },
        "provider_models": {
            "z-last": {"shared/model": shared_z,
                       "z-extra-free": "BAD-CONTAINER",
                       "empty-meta": {},
                       "dup": {"name": "DupZ"}},
            "a-first": {"shared/model": shared_a,
                        "trk:free": {"name": "Trk",
                                     "unknown_field": "TRK"},
                        "trk-free": {"name": "TrkFree",
                                     "nested": {"deep": [1, None,
                                                         {"x": True}]}}},
        },
        "stale_providers": ["z-last"],
        "private": {"deep": "SENTINEL"},
    }
    (tmp_path / "state").mkdir()
    roster_path = tmp_path / "state" / "roster.json"
    roster_path.write_text(json.dumps(roster_doc))
    control = json.loads(roster_path.read_text())

    # -- spies: exactly ONE capture and exactly ONE roster read per request ---
    captures = []
    loads = []
    load_guard: dict = {"count": 0}
    swap: dict = {"to": None}
    calls = []
    backstop: dict = {"args": None}
    drop: dict = {"provider": None, "raw_id": None}

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
        before = copy.deepcopy(snapshot)
        core_args = backstop["args"] if backstop["args"] is not None else arguments
        out = real_query_endpoints(snapshot, core_args)
        # The GENUINE core envelope before any wrapper reduction: used to prove
        # the local adaptation below never mutated what the core returned.
        pre_gateways = copy.deepcopy(out.get("gateways"))
        pre_counts = dict(out.get("counts") or {})
        if drop["provider"] is not None and out.get("ok"):
            entry = out["gateways"].get(drop["provider"])
            if entry is not None and drop["raw_id"] in entry["model_ids"]:
                kept = [rid for rid in entry["model_ids"]
                        if rid != drop["raw_id"]]
                reduced = dict(entry)
                reduced["model_ids"] = kept
                reduced["models"] = {rid: entry["models"][rid] for rid in kept}
                out = dict(out)
                out["gateways"] = dict(out["gateways"])
                out["gateways"][drop["provider"]] = reduced
                out["counts"] = dict(out["counts"])
                out["counts"]["endpoints"] = len(kept)
                out["counts"]["distinct_raw_ids"] = len(set(kept))
                labels = len({real_query.strip_free_marker(rid)
                              for rid in kept})
                out["counts"]["marker_group_labels"] = labels
                out["counts"]["models"] = labels
        calls.append({"snapshot": snapshot, "before": before, "result": out,
                      "pre_gateways": pre_gateways,
                      "pre_counts": pre_counts,
                      "arguments": arguments, "core_args": core_args})
        return out

    monkeypatch.setattr(mcp_server, "_capture_registry", _spy_capture)
    monkeypatch.setattr(mcp_server.state, "load_roster", _spy_load)
    monkeypatch.setattr(real_query, "list_endpoints", _spy_query)

    def _reset():
        captures.clear()
        loads.clear()
        calls.clear()
        load_guard["count"] = 0

    # -- bulk: one capture/load/core call, selected-scope counts, literal order
    _reset()
    res = mcp_server.list_endpoints(root=tmp_path)
    assert res["ok"] is True
    assert res["tool"] == "list_endpoints"
    assert res["state_root"] == str(tmp_path.resolve())
    assert len(captures) == 1 and len(loads) == 1 and len(calls) == 1
    assert calls[0]["arguments"] == {"provider": None}
    assert calls[0]["core_args"] == {"provider": None}
    snap = calls[0]["snapshot"]
    assert snap["provider_order"] == ["z-last", "a-first", "m-mid"]
    # local sanitization: empty/non-string/unknown-provider IDs dropped,
    # duplicates collapsed, code-point order retained into the adapter.
    assert snap["providers"]["z-last"]["model_ids"] == [
        " padded ", "dup", "empty-meta", "non-free", "shared/model",
        "z-extra-free", "z02", "z10", "z2", "z\u0669"]
    assert "   " in snap["providers"]["a-first"]["model_ids"]
    assert snap["providers"]["m-mid"]["model_ids"] == []
    assert "unknown-bad" not in snap["providers"]
    assert "m-mid" not in control["providers"]  # genuinely absent in the roster
    assert set(res) == {
        "tool", "state_root", "ok", "gateways", "counts", "provider",
        "snapshot_id", "tick_epoch", "provider_staleness", "warnings"}
    assert res["provider"] is None
    assert res["tick_epoch"] == 100
    assert res["snapshot_id"] == snap["snapshot_id"]
    assert res["snapshot_id"] == calls[0]["result"]["snapshot_id"]
    # all five count definitions, independently: selected providers (zero-ID
    # included), selected pairs, selected literal strings, selected stripped
    # labels, and the documented legacy alias of the label count.
    assert res["counts"] == {
        "gateways": 3, "endpoints": 15, "distinct_raw_ids": 14,
        "marker_group_labels": 13, "models": 13}
    assert res["provider_staleness"] == {
        "z-last": True, "a-first": False, "m-mid": False}
    assert res["warnings"] == snap["warnings"] == [
        "metadata_missing", "metadata_invalid"]
    assert res["warnings"] is not snap["warnings"]

    # -- gateways: selected keys only, exact four fields, natural literal order
    assert list(res["gateways"]) == ["z-last", "a-first", "m-mid"]
    for entry in res["gateways"].values():
        assert list(entry) == ["chat_completions_url", "api_type",
                               "model_ids", "models"]
    z = res["gateways"]["z-last"]
    a = res["gateways"]["a-first"]
    m = res["gateways"]["m-mid"]
    assert z["model_ids"] == [
        " padded ", "dup", "empty-meta", "non-free", "shared/model",
        "z2", "z02", "z10", "z-extra-free", "z\u0669"]
    assert a["model_ids"] == ["   ", "plain", "shared/model",
                              "trk-free", "trk:free"]
    assert m["model_ids"] == [] and m["models"] == {}
    assert z["api_type"] == a["api_type"] == "openai_compatible"
    assert m["api_type"] == "anthropic_compatible"
    # three DISTINCT wiring URLs, passed through per selected key.
    assert z["chat_completions_url"] == (
        "https://z.example/v1/chat/completions")
    assert a["chat_completions_url"] == (
        "https://a.example/v1/chat/completions")
    assert m["chat_completions_url"] == (
        "https://m.example/v1/chat/completions")
    assert len({z["chat_completions_url"], a["chat_completions_url"],
                m["chat_completions_url"]}) == 3

    # -- ORIGINAL raw metadata identity restored from the SAME loaded roster --
    loaded = loads[0]
    raw_z = loaded["provider_models"]["z-last"]["shared/model"]
    raw_a = loaded["provider_models"]["a-first"]["shared/model"]
    assert loaded == control                     # source unchanged by value
    assert z["models"]["shared/model"] is raw_z
    assert a["models"]["shared/model"] is raw_a
    assert raw_z is not raw_a
    assert raw_z["context_length"] != raw_a["context_length"]
    assert raw_z["unknown_field"] == "Z-UNKNOWN"
    assert raw_z["params"] == {"a": 1}
    assert a["models"]["trk:free"]["unknown_field"] == "TRK"
    # rich nested unknown metadata survives untouched on the label-sharing id.
    nested_raw = loaded["provider_models"]["a-first"]["trk-free"]
    assert a["models"]["trk-free"] is nested_raw
    assert a["models"]["trk-free"]["nested"] == {
        "deep": [1, None, {"x": True}]}
    # empty raw dict IDENTITY is preserved (never `raw or {}`).
    empty_raw = loaded["provider_models"]["z-last"]["empty-meta"]
    assert z["models"]["empty-meta"] is empty_raw
    assert z["models"]["empty-meta"] == {}
    # missing / non-dict individual values are omitted from the models map.
    assert "z-extra-free" not in z["models"]
    assert "   " not in a["models"] and "plain" not in a["models"]
    assert a["models"].get("   ") is None
    # local raw metadata stays distinct from the projected public model object.
    proj_z = snap["providers"]["z-last"]["models"]["shared/model"]
    assert "unknown_field" not in proj_z and "params" not in proj_z
    assert z["models"]["shared/model"] is not proj_z
    # the core result carries the projected public model, not raw metadata.
    core_model = calls[0]["result"]["gateways"]["z-last"]["models"][
        "shared/model"]
    assert "unknown_field" not in core_model
    assert core_model == proj_z
    # neither the validated snapshot nor the returned core object was mutated.
    assert snap == calls[0]["before"]
    assert calls[0]["result"]["counts"] == res["counts"]

    # -- filtered nonempty provider: selected scope only, no registry leak ----
    _reset()
    filtered = mcp_server.list_endpoints("a-first", root=tmp_path)
    assert filtered["ok"] is True
    assert filtered["provider"] == "a-first"
    assert list(filtered["gateways"]) == ["a-first"]
    assert filtered["gateways"]["a-first"]["model_ids"] == a["model_ids"]
    assert filtered["counts"] == {
        "gateways": 1, "endpoints": 5, "distinct_raw_ids": 5,
        "marker_group_labels": 4, "models": 4}
    assert filtered["counts"]["endpoints"] != res["counts"]["endpoints"]
    assert filtered["counts"]["gateways"] != res["counts"]["gateways"]
    assert filtered["provider_staleness"] == {"a-first": False}
    # snapshot-wide warnings stay snapshot-wide on a filtered response.
    assert filtered["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert filtered["gateways"]["a-first"]["models"]["shared/model"] is (
        loads[0]["provider_models"]["a-first"]["shared/model"])
    assert len(captures) == 1 and len(loads) == 1 and len(calls) == 1

    # -- filtered ZERO-ID provider: one gateway, every other count zero ------
    _reset()
    zero = mcp_server.list_endpoints("m-mid", root=tmp_path)
    assert zero["ok"] is True
    assert list(zero["gateways"]) == ["m-mid"]
    assert zero["gateways"]["m-mid"]["model_ids"] == []
    assert zero["gateways"]["m-mid"]["models"] == {}
    assert zero["counts"] == {
        "gateways": 1, "endpoints": 0, "distinct_raw_ids": 0,
        "marker_group_labels": 0, "models": 0}
    assert zero["provider_staleness"] == {"m-mid": False}
    assert zero["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert zero["snapshot_id"] == snap["snapshot_id"]
    assert len(captures) == 1 and len(loads) == 1 and len(calls) == 1

    # -- non-list known-provider value degrades to empty, NEVER a failure -----
    roster_path.write_text(json.dumps({
        "tick_epoch": 7, "providers": {"a-first": "not-a-list"}}))
    _reset()
    degraded = mcp_server.list_endpoints("a-first", root=tmp_path)
    assert degraded["ok"] is True
    assert list(degraded["gateways"]) == ["a-first"]
    assert degraded["gateways"]["a-first"]["model_ids"] == []
    assert degraded["gateways"]["a-first"]["models"] == {}
    assert degraded["counts"] == {
        "gateways": 1, "endpoints": 0, "distinct_raw_ids": 0,
        "marker_group_labels": 0, "models": 0}
    assert degraded["tick_epoch"] == 7
    assert degraded["warnings"] == []   # zero tracked IDs, no metadata rows
    assert len(captures) == 1 and len(loads) == 1 and len(calls) == 1

    # -- inherited strict tick provenance: zero and invalid epochs ------------
    roster_path.write_text(json.dumps({
        "tick_epoch": 0, "providers": {"z-last": ["shared/model"]}}))
    _reset()
    epoch_zero = mcp_server.list_endpoints(root=tmp_path)
    assert epoch_zero["ok"] is True
    assert epoch_zero["tick_epoch"] == 0
    assert epoch_zero["counts"] == {
        "gateways": 3, "endpoints": 1, "distinct_raw_ids": 1,
        "marker_group_labels": 1, "models": 1}
    assert epoch_zero["gateways"]["z-last"]["models"] == {}
    assert epoch_zero["warnings"] == ["metadata_missing"]

    roster_path.write_text(json.dumps({
        "tick_epoch": -3, "providers": {"z-last": ["shared/model"]}}))
    _reset()
    bad_epoch = mcp_server.list_endpoints(root=tmp_path)
    assert bad_epoch["ok"] is True
    assert bad_epoch["tick_epoch"] is None
    assert bad_epoch["counts"]["endpoints"] == 1
    assert bad_epoch["warnings"] == ["tick_epoch_unknown", "metadata_missing"]

    # -- malformed outer/provider metadata containers: truthful warnings -----
    for bad_outer in ([], {"z-last": []}):
        roster_path.write_text(json.dumps({
            "tick_epoch": 0, "providers": {"z-last": ["shared/model"]},
            "provider_models": bad_outer}))
        _reset()
        bad_meta = mcp_server.list_endpoints(root=tmp_path)
        assert bad_meta["ok"] is True
        assert bad_meta["gateways"]["z-last"]["models"] == {}
        assert bad_meta["warnings"] == ["metadata_invalid"]
        assert "metadata_missing" not in bad_meta["warnings"]
    roster_path.write_text(json.dumps({
        "tick_epoch": 0, "providers": {"z-last": ["shared/model"]}}))
    _reset()
    absent_meta = mcp_server.list_endpoints(root=tmp_path)
    assert absent_meta["gateways"]["z-last"]["models"] == {}
    assert absent_meta["warnings"] == ["metadata_missing"]
    roster_path.write_text(json.dumps(roster_doc))

    # -- wrapper subset: local output follows the RETURNED ids/counts ---------
    _reset()
    drop["provider"] = "z-last"
    drop["raw_id"] = "shared/model"
    try:
        subset = mcp_server.list_endpoints("z-last", root=tmp_path)
    finally:
        drop["provider"] = None
        drop["raw_id"] = None
    assert subset["ok"] is True
    assert list(subset["gateways"]) == ["z-last"]
    kept = [rid for rid in z["model_ids"] if rid != "shared/model"]
    assert subset["gateways"]["z-last"]["model_ids"] == kept
    assert "shared/model" not in subset["gateways"]["z-last"]["models"]
    assert subset["counts"] == {
        "gateways": 1, "endpoints": len(kept), "distinct_raw_ids": len(kept),
        "marker_group_labels": len(kept), "models": len(kept)}
    assert subset["counts"]["endpoints"] == 9
    # the roster still tracks the removed pair, and the GENUINE core envelope
    # kept its own projected copy: local adaptation mutated neither.
    assert "shared/model" in loads[0]["providers"]["z-last"]
    assert "shared/model" in calls[0]["pre_gateways"]["z-last"]["models"]
    assert calls[0]["pre_counts"]["endpoints"] == 10
    assert calls[0]["result"]["counts"]["endpoints"] == 9
    assert calls[0]["snapshot"] == calls[0]["before"]
    assert subset["warnings"] == ["metadata_missing", "metadata_invalid"]

    # -- exactly one capture stays authoritative when the roots are replaced --
    conflicting = {"bogus": {"name": "Bogus"}}
    conflicting_wiring = {"bogus": {
        "chat_completions_url": "https://bogus.example/v1/chat/completions",
        "api_type": "openai_compatible"}}
    _reset()
    swap["to"] = (conflicting, conflicting_wiring)
    try:
        kept_request = mcp_server.list_endpoints(root=tmp_path)
    finally:
        swap["to"] = None
    assert kept_request["ok"] is True
    assert len(captures) == 1
    assert [r["key"] for r in captures[0]] == ["z-last", "a-first", "m-mid"]
    assert list(kept_request["gateways"]) == ["z-last", "a-first", "m-mid"]
    assert "bogus" not in kept_request["gateways"]
    assert kept_request["gateways"]["a-first"]["chat_completions_url"] == (
        "https://a.example/v1/chat/completions")
    assert mcp_server.PROVIDERS == conflicting   # the swap really happened
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # an error provider list also comes from the CAPTURE, not the later globals.
    _reset()
    swap["to"] = (conflicting, conflicting_wiring)
    try:
        arg_after_swap = mcp_server.list_endpoints("nope", root=tmp_path)
    finally:
        swap["to"] = None
    assert arg_after_swap["ok"] is False
    assert arg_after_swap["error"] == (
        "unknown provider 'nope'; valid providers: z-last, a-first, m-mid")
    assert arg_after_swap["valid_providers"] == ["z-last", "a-first", "m-mid"]
    assert loads == [] and calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # -- every invalid provider argument fails BEFORE any roster read ---------
    for bad_value in (7, True, [], {}, "", " z-last", "Z-last", "nope"):
        _reset()
        bad = mcp_server.list_endpoints(bad_value, root=tmp_path)
        assert bad["ok"] is False
        assert bad["error"] == (
            f"unknown provider {bad_value!r}; "
            "valid providers: z-last, a-first, m-mid")
        assert bad["valid_providers"] == ["z-last", "a-first", "m-mid"]
        assert set(bad) == {"tool", "state_root", "ok", "error",
                            "valid_providers"}
        assert loads == [] and calls == []

    # -- capture-root and deferred structural failures stay fixed local errors
    monkeypatch.setattr(mcp_server, "PROVIDERS", [])
    _reset()
    root_guard = mcp_server.list_endpoints("nope", root=tmp_path)
    assert root_guard["ok"] is False
    assert root_guard["error"] == "Invalid catalogue snapshot."
    assert set(root_guard) == {"tool", "state_root", "ok", "error"}
    assert loads == [] and calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    _reset()
    bad_wiring = mcp_server.list_endpoints(root=tmp_path)
    assert bad_wiring["ok"] is False
    assert bad_wiring["error"] == "Invalid catalogue snapshot."
    assert len(loads) == 1 and calls == []
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    monkeypatch.setattr(mcp_server, "PROVIDERS", {"z-last": "not-a-dict"})
    _reset()
    bad_record = mcp_server.list_endpoints(root=tmp_path)
    assert bad_record["ok"] is False
    assert bad_record["error"] == "Invalid catalogue snapshot."
    assert len(loads) == 1 and calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    monkeypatch.setattr(mcp_server, "CADENCE_S", 0)
    _reset()
    bad_cadence = mcp_server.list_endpoints(root=tmp_path)
    assert bad_cadence["ok"] is False
    assert bad_cadence["error"] == "Invalid catalogue snapshot."
    assert calls == []
    monkeypatch.setattr(mcp_server, "CADENCE_S", 3600)

    # -- missing/corrupt/root/provider-map state failures ---------------------
    _reset()
    missing = mcp_server.list_endpoints(root=tmp_path / "nope")
    assert missing["ok"] is False and "roster" in missing["error"].lower()
    assert loads == [] and calls == []
    corrupt_root = tmp_path / "corrupt"
    (corrupt_root / "state").mkdir(parents=True)
    (corrupt_root / "state" / "roster.json").write_text("{not json!!")
    _reset()
    corrupt = mcp_server.list_endpoints(root=corrupt_root)
    assert corrupt["ok"] is False and "corrupt" in corrupt["error"].lower()
    assert calls == []
    map_root = tmp_path / "map"
    (map_root / "state").mkdir(parents=True)
    (map_root / "state" / "roster.json").write_text(json.dumps(
        {"tick_epoch": 100, "providers": []}))
    _reset()
    bad_map = mcp_server.list_endpoints(root=map_root)
    assert bad_map["ok"] is False
    assert "'providers' is not a map" in bad_map["error"]
    assert calls == []

    # precedence: bad capture root > bad argument > missing state > deferred.
    _reset()
    monkeypatch.setattr(mcp_server, "PROVIDERS", [])
    root_first = mcp_server.list_endpoints("nope", root=tmp_path / "nope")
    assert root_first["error"] == "Invalid catalogue snapshot."
    assert loads == [] and calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    _reset()
    arg_first = mcp_server.list_endpoints("nope", root=tmp_path / "nope")
    assert arg_first["ok"] is False
    assert arg_first["error"] == (
        "unknown provider 'nope'; valid providers: z-last, a-first, m-mid")
    assert set(arg_first) == {"tool", "state_root", "ok", "error",
                              "valid_providers"}
    assert loads == [] and calls == []

    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    _reset()
    state_first = mcp_server.list_endpoints(root=tmp_path / "nope")
    assert state_first["ok"] is False
    assert "roster" in state_first["error"].lower()
    assert calls == []
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # -- the real-core argument envelopes map to the SAME flat local error ----
    _reset()
    backstop["args"] = {"provider": "not-captured"}
    try:
        unknown_core = mcp_server.list_endpoints(root=tmp_path)
    finally:
        backstop["args"] = None
    assert unknown_core["ok"] is False
    assert unknown_core["error"] == (
        "unknown provider None; valid providers: z-last, a-first, m-mid")
    assert unknown_core["valid_providers"] == ["z-last", "a-first", "m-mid"]
    assert set(unknown_core) == {"tool", "state_root", "ok", "error",
                                 "valid_providers"}
    assert calls[-1]["arguments"] == {"provider": None}
    assert calls[-1]["result"]["error"] == {
        "code": "unknown_provider", "message": "Unknown provider.",
        "parameter": "provider"}

    _reset()
    backstop["args"] = {"provider": 7}
    try:
        invalid_core = mcp_server.list_endpoints(root=tmp_path)
    finally:
        backstop["args"] = None
    assert invalid_core["ok"] is False
    assert invalid_core["error"] == (
        "unknown provider None; valid providers: z-last, a-first, m-mid")
    assert set(invalid_core) == {"tool", "state_root", "ok", "error",
                                 "valid_providers"}
    assert calls[-1]["result"]["error"] == {
        "code": "invalid_arguments", "message": "Invalid tool arguments.",
        "parameter": "provider"}

    # unexpected PROGRAMMING exceptions are never swallowed at this boundary.
    def _boom(snapshot, arguments):
        raise RuntimeError("programming exception")

    _reset()
    monkeypatch.setattr(real_query, "list_endpoints", _boom)
    with pytest.raises(RuntimeError, match="programming exception"):
        mcp_server.list_endpoints(root=tmp_path)
    monkeypatch.setattr(real_query, "list_endpoints", _spy_query)

    # -- legitimately empty captured registry is a valid empty success --------
    _reset()
    monkeypatch.setattr(mcp_server, "PROVIDERS", {})
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    roster_path.write_text(json.dumps({"tick_epoch": 0, "providers": {}}))
    empty = mcp_server.list_endpoints(root=tmp_path)
    assert empty["ok"] is True
    assert empty["gateways"] == {}
    assert empty["provider_staleness"] == {}
    assert empty["counts"] == {
        "gateways": 0, "endpoints": 0, "distinct_raw_ids": 0,
        "marker_group_labels": 0, "models": 0}
    assert empty["provider"] is None
    assert empty["tick_epoch"] == 0 and empty["warnings"] == []
    assert len(captures) == 1 and captures[0] == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    roster_path.write_text(json.dumps(roster_doc))

    # -- freshness, unchanged sources, mutation isolation ---------------------
    _reset()
    fresh1 = mcp_server.list_endpoints("z-last", root=tmp_path)
    assert len(captures) == 1 and len(loads) == 1 and len(calls) == 1
    snap_a = calls[0]["snapshot"]
    before_a = calls[0]["before"]
    core_a = calls[0]["result"]
    load1 = loads[0]
    assert load1 == control
    assert snap_a == before_a
    assert fresh1["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert fresh1["warnings"] is not snap_a["warnings"]

    _reset()
    fresh2 = mcp_server.list_endpoints("z-last", root=tmp_path)
    assert len(loads) == 1 and len(captures) == 1 and len(calls) == 1
    assert fresh2["warnings"] == fresh1["warnings"]
    assert fresh2["warnings"] is not fresh1["warnings"]
    assert fresh2["gateways"]["z-last"]["models"]["shared/model"] is not (
        fresh1["gateways"]["z-last"]["models"]["shared/model"])
    assert loads[0] == control

    # a local raw mutation touches only the intentionally aliased captured raw
    # dict -- never the projected snapshot, the saved core result, or a fresh
    # independently loaded request.
    fresh1["gateways"]["z-last"]["models"]["shared/model"]["name"] = "MUTATED"
    assert snap_a["providers"]["z-last"]["models"]["shared/model"]["name"] == (
        "Z Shared")
    assert core_a["gateways"]["z-last"]["models"]["shared/model"]["name"] == (
        "Z Shared")
    assert load1["provider_models"]["z-last"]["shared/model"]["name"] == (
        "MUTATED")
    _reset()
    fresh3 = mcp_server.list_endpoints("z-last", root=tmp_path)
    assert fresh3["gateways"]["z-last"]["models"]["shared/model"]["name"] == (
        "Z Shared")
    assert len(loads) == 1 and len(calls) == 1


# ---------- watchdog_status local delegation (unit 2j-U4) ----------

def test_watchdog_status_delegates_and_preserves_local_diagnostics(
        monkeypatch, tmp_path):
    """U4: the LOCAL ``watchdog_status`` captures the registry ONCE, validates
    and captures its clock ONCE, reads the roster ONCE, adapts that same
    observation to the validated public snapshot, delegates catalogue health,
    freshness and each count/cardinality/coverage projection to the accepted
    pure query, and keeps separately observed LOCAL operator diagnostics.

    Fully synthetic globals plus a real tmp state dir read through the REAL
    ``state.load_roster``; the REAL ``watchdog_catalog.query.watchdog_status``
    is wrapped directly so a missing ACTUAL delegation is a real RED. The core
    payload keeps its OWN null pending/site placeholders in the recorded call,
    independent literal expectations cover every count/coverage/map, and a
    returned-sentinel phase proves the local output really follows that core
    result instead of recomputing health from the outer clock.
    """
    import copy
    import os

    import watchdog_catalog.query as real_query

    real_status = real_query.watchdog_status
    real_load = mcp_server.state.load_roster
    real_capture = mcp_server._capture_registry
    real_local_snapshot = mcp_server._local_snapshot

    configs = {"z-last": {"name": "Zeta"},
               "a-first": {"name": "Alpha"},
               "m-zero": {"name": "Mu"}}
    wiring = {
        "z-last": {"chat_completions_url":
                   "https://z.example/v1/chat/completions",
                   "api_type": "openai_compatible"},
        "a-first": {"chat_completions_url":
                    "https://a.example/v1/chat/completions",
                    "api_type": "openai_compatible"},
        "m-zero": {"chat_completions_url":
                   "https://m.example/v1/chat/completions",
                   "api_type": "anthropic_compatible"},
    }
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    # Synthetic NON-DEFAULT cadence: a hardcoded 3600 would misgrade freshness.
    monkeypatch.setattr(mcp_server, "CADENCE_S", 1800)

    shared_z = {"name": "Z Shared", "context_length": 8192,
                "unknown_field": "Z-UNKNOWN",
                "architecture": {"input_modalities": ["text"]},
                "supported_parameters": ["temperature"],
                "description": "Z desc", "pricing": {"prompt": "0"}}
    shared_a = {"name": "A Shared", "context_length": 131072,
                "unknown_field": "A-UNKNOWN",
                "architecture": {"input_modalities": ["text", "image"],
                                 "output_modalities": ["text"]},
                "supported_parameters": [], "pricing": {"prompt": "0"}}
    roster_doc = {
        "tick_epoch": 1000,
        "providers": {
            # duplicates, empty, non-string, padded, non-free and literal
            # non-ASCII/leading-zero ids in ONE tracked provider.
            "z-last": ["shared/model", "z-extra-free", "dup", "dup", "",
                       3, None, "empty-meta", " padded ", "non-free",
                       "z2", "z02", "z10", "z\u0669", True],
            "a-first": ["shared/model", "plain", "trk:free", "trk-free",
                        "   "],
            # unknown provider entry, malformed value: excluded by the local
            # sanitizer before the accepted builder ever validates it.
            "unknown-bad": "not-a-list",
        },
        "provider_models": {
            "z-last": {"shared/model": shared_z,
                       "z-extra-free": "BAD-CONTAINER",
                       "empty-meta": {},
                       "dup": {"name": "DupZ"}},
            "a-first": {"shared/model": shared_a,
                        "trk:free": {"name": "Trk",
                                     "unknown_field": "TRK"},
                        "trk-free": {"name": "TrkFree",
                                     "nested": {"deep": [1, None,
                                                         {"x": True}]}}},
        },
        # carried names keep ORIGINAL order/duplicates, inactive names included
        "stale_providers": ["z-last", "ghost", "z-last"],
        "private": {"deep": "SENTINEL"},
    }
    (tmp_path / "state").mkdir()
    roster_path = tmp_path / "state" / "roster.json"
    roster_path.write_text(json.dumps(roster_doc))
    control = json.loads(roster_path.read_text())
    site_path = tmp_path / "site" / "index.html"
    site_path.parent.mkdir()
    site_path.write_text("<html></html>")
    os.utime(site_path, (1000.5, 1000.5))     # fractional mtime on purpose
    pending_path = tmp_path / "state" / "pending_alerts.json"
    pending_path.write_text(json.dumps([{"payload": {"a": 1}},
                                        {"payload": {"b": 2}}]))

    captures, loads, snapshots, calls = [], [], [], []
    guard = {"load_count": 0, "swap": None, "inner_now": None,
             "core_args": None, "sentinel": False}

    def _spy_capture(base):
        out = real_capture(base)
        captures.append(out)
        if guard["swap"] is not None:
            # Replace BOTH module roots AFTER capture: the rest of this request
            # must still use the already-captured records.
            mcp_server.PROVIDERS, mcp_server.GATEWAY_WIRING = guard["swap"]
        return out

    def _spy_load(path):
        out = real_load(path)
        loads.append(out)
        guard["load_count"] += 1
        if guard["load_count"] > 1:
            raise AssertionError("roster must be loaded exactly once per request")
        return out

    def _spy_local_snapshot(roster, registry, base):
        out = real_local_snapshot(roster, registry, base)
        # Same retained observation flows into the snapshot AND the local
        # raw-metadata presence summary.
        assert roster is loads[-1]
        snapshots.append(out)
        return out

    def _spy_status(snapshot, arguments, *, now):
        before = copy.deepcopy(snapshot)
        core_args = (guard["core_args"] if guard["core_args"] is not None
                     else arguments)
        inner = guard["inner_now"]
        out = real_status(snapshot, core_args,
                          now=now if inner is None else inner)
        calls.append({"snapshot": snapshot, "before": before,
                      "arguments": arguments, "core_args": core_args,
                      "now": now, "inner": inner, "result": out})
        if guard["sentinel"]:
            # A VALID returned payload with coherent mutated values: the local
            # wrapper must carry THESE, not recompute health from its own clock.
            out = dict(out)
            out["snapshot_id"] = "SENTINEL-SNAPSHOT-ID"
            out["last_tick_age_s"] = 4242
            out["tick_fresh"] = False
            out["provider_counts"] = {**out["provider_counts"], "a-first": 77}
            out["metadata_coverage"] = {key: 9 for key in out["metadata_coverage"]}
            out["counts"] = {"gateways": 9, "endpoints": 9,
                             "distinct_raw_ids": 9, "marker_group_labels": 9}
            out["warnings"] = ["tick_stale", "metadata_missing"]
            out["pending_alerts"] = None
            out["site_published"] = None
            out["site_publish_age_s"] = None
        return out

    monkeypatch.setattr(mcp_server, "_capture_registry", _spy_capture)
    monkeypatch.setattr(mcp_server, "_local_snapshot", _spy_local_snapshot)
    monkeypatch.setattr(mcp_server.state, "load_roster", _spy_load)
    monkeypatch.setattr(real_query, "watchdog_status", _spy_status)

    def _reset():
        captures.clear()
        loads.clear()
        snapshots.clear()
        calls.clear()
        guard["load_count"] = 0

    # -- health: one capture/load/snapshot/core call, every count independent --
    _reset()
    res = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    assert res["ok"] is True
    assert res["tool"] == "watchdog_status"
    assert res["state_root"] == str(tmp_path.resolve())
    assert len(captures) == 1 and len(loads) == 1
    assert len(snapshots) == 1 and len(calls) == 1
    assert calls[0]["arguments"] == {}
    now_int = calls[0]["now"]
    assert type(now_int) is int and now_int == 1060   # int() exactly once
    assert set(res) == {
        "tool", "state_root", "ok", "snapshot_id", "tick_epoch", "cadence_s",
        "last_tick_age_s", "tick_fresh", "provider_staleness",
        "provider_counts", "metadata_coverage", "counts", "pending_alerts",
        "site_published", "site_publish_age_s", "warnings", "stale_providers",
        "metadata_summary"}
    assert res["tick_epoch"] == 1000
    assert res["cadence_s"] == 1800
    assert res["last_tick_age_s"] == 60 and res["tick_fresh"] is True
    snap = calls[0]["snapshot"]
    assert snap["provider_order"] == ["z-last", "a-first", "m-zero"]
    assert snap["providers"]["z-last"]["model_ids"] == [
        " padded ", "dup", "empty-meta", "non-free", "shared/model",
        "z-extra-free", "z02", "z10", "z2", "z\u0669"]
    assert snap["providers"]["a-first"]["model_ids"] == [
        "   ", "plain", "shared/model", "trk-free", "trk:free"]
    assert snap["providers"]["m-zero"]["model_ids"] == []
    assert "unknown-bad" not in snap["providers"]
    assert "m-zero" not in control["providers"]     # genuinely absent
    assert res["provider_counts"] == {"z-last": 10, "a-first": 5, "m-zero": 0}
    assert res["provider_staleness"] == {
        "z-last": True, "a-first": False, "m-zero": False}
    assert res["counts"] == {"gateways": 3, "endpoints": 15,
                             "distinct_raw_ids": 14, "marker_group_labels": 13}
    # projected-field knowledge: known 0/[]/"" count, null does not.
    assert res["metadata_coverage"] == {
        "endpoints": 15, "context_length_known": 2,
        "input_modalities_known": 2, "output_modalities_known": 1,
        "supported_parameters_known": 2, "description_known": 1}
    assert res["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert res["snapshot_id"] == snap["snapshot_id"]
    # LOCAL raw-dictionary PRESENCE over the core-selected scope: an EMPTY raw
    # dict counts, a non-dict/missing value does not, and the total (6)
    # deliberately differs from projected description coverage (1).
    assert res["metadata_summary"] == {
        "models_with_metadata": 6,
        "provider_counts": {"z-last": 3, "a-first": 3, "m-zero": 0}}
    assert res["stale_providers"] == ["z-last", "ghost", "z-last"]
    # LOCAL operator observations, not core placeholders.
    assert res["pending_alerts"] == 2
    assert res["site_published"] is True
    # RAW validated clock for the filesystem diagnostic: int(1060.75 - 1000.5)
    # is 60, while prematurely normalized arithmetic would report 59.
    assert res["site_publish_age_s"] == 60
    # the recorded CORE envelope keeps its own null operator placeholders.
    assert calls[0]["result"]["pending_alerts"] is None
    assert calls[0]["result"]["site_published"] is None
    assert calls[0]["result"]["site_publish_age_s"] is None
    # one observation, never mutated; the raw dict stays authoritative.
    loaded = loads[0]
    assert loaded == control
    assert snap == calls[0]["before"]
    raw_z = loaded["provider_models"]["z-last"]["shared/model"]
    assert raw_z["unknown_field"] == "Z-UNKNOWN"
    proj_z = snap["providers"]["z-last"]["models"]["shared/model"]
    assert proj_z is not raw_z and "unknown_field" not in proj_z

    # -- local-only changes never move the snapshot hash ---------------------
    site_path.unlink()
    pending_path.write_text(json.dumps([{"payload": {"a": 1}}]))
    _reset()
    local_only = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    assert local_only["ok"] is True
    assert local_only["snapshot_id"] == res["snapshot_id"]
    assert local_only["pending_alerts"] == 1
    assert local_only["site_published"] is False
    assert local_only["site_publish_age_s"] is None
    assert len(loads) == 1 and len(calls) == 1

    # -- present site with a FUTURE mtime clamps to zero ---------------------
    site_path.write_text("<html></html>")
    os.utime(site_path, (2000.0, 2000.0))
    _reset()
    future_site = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    assert future_site["site_published"] is True
    assert future_site["site_publish_age_s"] == 0
    assert future_site["snapshot_id"] == res["snapshot_id"]

    # -- local output follows the RETURNED core result (sentinel phase) ------
    os.utime(site_path, (1000.5, 1000.5))
    _reset()
    guard["sentinel"] = True
    try:
        sent = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    finally:
        guard["sentinel"] = False
    assert sent["snapshot_id"] == "SENTINEL-SNAPSHOT-ID"
    assert sent["last_tick_age_s"] == 4242 and sent["tick_fresh"] is False
    assert sent["tick_epoch"] == 1000
    assert sent["provider_counts"]["a-first"] == 77
    assert sent["provider_counts"]["z-last"] == 10
    assert sent["metadata_coverage"] == {
        "endpoints": 9, "context_length_known": 9, "input_modalities_known": 9,
        "output_modalities_known": 9, "supported_parameters_known": 9,
        "description_known": 9}
    assert sent["counts"] == {"gateways": 9, "endpoints": 9,
                              "distinct_raw_ids": 9, "marker_group_labels": 9}
    assert sent["warnings"] == ["tick_stale", "metadata_missing"]
    # ... while LOCAL diagnostics stay independently observed.
    assert sent["metadata_summary"] == {
        "models_with_metadata": 6,
        "provider_counts": {"z-last": 3, "a-first": 3, "m-zero": 0}}
    assert sent["stale_providers"] == ["z-last", "ghost", "z-last"]
    assert sent["pending_alerts"] == 1
    assert sent["site_publish_age_s"] == 60
    assert sent is not calls[0]["result"] and sent is not calls[0]["snapshot"]
    assert calls[0]["snapshot"] == calls[0]["before"]

    # -- genuine core result with a LATER inner clock plus the outer raw clock
    _reset()
    guard["inner_now"] = 6060
    try:
        inner_res = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    finally:
        guard["inner_now"] = None
    assert calls[0]["now"] == 1060 and calls[0]["inner"] == 6060
    assert inner_res["last_tick_age_s"] == 5060     # the CORE age, not 60
    assert inner_res["tick_fresh"] is False
    assert inner_res["warnings"] == [
        "metadata_missing", "metadata_invalid", "tick_stale"]
    assert inner_res["site_publish_age_s"] == 60    # outer RAW clock
    assert inner_res["snapshot_id"] == res["snapshot_id"]

    # -- the real-core argument envelope maps to a flat local error ----------
    _reset()
    guard["core_args"] = {"provider": "not-an-argument"}
    try:
        envelope = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    finally:
        guard["core_args"] = None
    assert envelope["ok"] is False
    assert envelope["error"] == "Invalid tool arguments."
    assert set(envelope) == {"tool", "state_root", "ok", "error"}
    assert calls[-1]["arguments"] == {}
    assert calls[-1]["result"] == {
        "ok": False,
        "error": {"code": "invalid_arguments",
                  "message": "Invalid tool arguments.", "parameter": None}}
    assert len(loads) == 1 and len(snapshots) == 1

    # -- capture-root and deferred structural failures stay fixed local errors
    monkeypatch.setattr(mcp_server, "PROVIDERS", [])
    _reset()
    providers_root = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    assert providers_root == {"tool": "watchdog_status",
                              "state_root": str(tmp_path.resolve()),
                              "ok": False,
                              "error": "Invalid catalogue snapshot."}
    assert loads == [] and calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    _reset()
    wiring_root = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    assert wiring_root["ok"] is False
    assert wiring_root["error"] == "Invalid catalogue snapshot."
    assert set(wiring_root) == {"tool", "state_root", "ok", "error"}
    assert len(loads) == 1 and calls == []
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    monkeypatch.setattr(mcp_server, "PROVIDERS", {"z-last": "not-a-dict"})
    _reset()
    bad_record = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    assert bad_record["ok"] is False
    assert bad_record["error"] == "Invalid catalogue snapshot."
    assert len(loads) == 1 and calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    monkeypatch.setattr(mcp_server, "CADENCE_S", 0)
    _reset()
    bad_cadence = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    assert bad_cadence["ok"] is False
    assert bad_cadence["error"] == "Invalid catalogue snapshot."
    assert calls == []
    monkeypatch.setattr(mcp_server, "CADENCE_S", 1800)

    # -- missing/corrupt/non-object/providers-map state + legacy defaults ----
    _reset()
    missing = mcp_server.watchdog_status(now=1060.75, root=tmp_path / "nope")
    assert missing["ok"] is False
    assert "roster" in missing["error"].lower()
    assert missing["cadence_s"] == 1800
    assert missing["pending_alerts"] is None
    assert missing["site_published"] is False
    assert loads == [] and calls == []

    corrupt_root = tmp_path / "corrupt"
    (corrupt_root / "state").mkdir(parents=True)
    (corrupt_root / "state" / "roster.json").write_text("{not json!!")
    _reset()
    corrupt = mcp_server.watchdog_status(now=1060.75, root=corrupt_root)
    assert corrupt["ok"] is False and "corrupt" in corrupt["error"].lower()
    assert corrupt["cadence_s"] == 1800
    assert corrupt["pending_alerts"] is None
    assert corrupt["site_published"] is False
    assert calls == []

    nonobj_root = tmp_path / "nonobj"
    (nonobj_root / "state").mkdir(parents=True)
    (nonobj_root / "state" / "roster.json").write_text("[]")
    _reset()
    nonobj = mcp_server.watchdog_status(now=1060.75, root=nonobj_root)
    assert nonobj["ok"] is False
    assert "corrupt or unreadable roster" in nonobj["error"]
    assert nonobj["pending_alerts"] is None and calls == []

    map_root = tmp_path / "map"
    (map_root / "state").mkdir(parents=True)
    (map_root / "state" / "roster.json").write_text(json.dumps(
        {"tick_epoch": 1000, "providers": []}))
    _reset()
    bad_map = mcp_server.watchdog_status(now=1060.75, root=map_root)
    assert bad_map["ok"] is False
    assert "'providers' is not a map" in bad_map["error"]
    assert calls == []

    # precedence: bad root > invalid clock > missing state > deferred content.
    monkeypatch.setattr(mcp_server, "PROVIDERS", [])
    _reset()
    root_first = mcp_server.watchdog_status(now=-1, root=tmp_path / "nope")
    assert root_first["error"] == "Invalid catalogue snapshot."
    assert loads == [] and calls == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)

    _reset()
    clock_first = mcp_server.watchdog_status(now=-0.5, root=tmp_path / "nope")
    assert clock_first["ok"] is False
    assert clock_first["error"] == "now must be a finite non-negative safe number"
    assert set(clock_first) == {"tool", "state_root", "ok", "error"}
    assert loads == [] and calls == []

    _reset()
    clock_over_state = mcp_server.watchdog_status(now=-1, root=corrupt_root)
    assert clock_over_state["error"] == (
        "now must be a finite non-negative safe number")
    assert loads == [] and calls == []

    # -- exactly one capture stays authoritative when the roots are replaced --
    conflicting = {"bogus": {"name": "Bogus"}}
    conflicting_wiring = {"bogus": {
        "chat_completions_url": "https://bogus.example/v1/chat/completions",
        "api_type": "openai_compatible"}}
    _reset()
    guard["swap"] = (conflicting, conflicting_wiring)
    try:
        kept = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    finally:
        guard["swap"] = None
    assert kept["ok"] is True
    assert [rec["key"] for rec in captures[0]] == ["z-last", "a-first", "m-zero"]
    assert list(kept["provider_counts"]) == ["z-last", "a-first", "m-zero"]
    assert list(kept["provider_staleness"]) == ["z-last", "a-first", "m-zero"]
    assert list(kept["metadata_summary"]["provider_counts"]) == [
        "z-last", "a-first", "m-zero"]
    assert "bogus" not in kept["provider_counts"]
    assert mcp_server.PROVIDERS == conflicting        # the swap really happened
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # -- a programmer exception at the real call boundary PROPAGATES ----------
    def _boom(snapshot, arguments, *, now):
        raise RuntimeError("programming exception")

    _reset()
    monkeypatch.setattr(real_query, "watchdog_status", _boom)
    with pytest.raises(RuntimeError, match="programming exception"):
        mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    monkeypatch.setattr(real_query, "watchdog_status", _spy_status)

    # -- legitimately empty captured registry is a valid empty success -------
    empty_root = tmp_path / "emptyreg"
    (empty_root / "state").mkdir(parents=True)
    (empty_root / "state" / "roster.json").write_text(json.dumps(
        {"tick_epoch": 0, "providers": {}}))
    monkeypatch.setattr(mcp_server, "PROVIDERS", {})
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", {})
    _reset()
    empty = mcp_server.watchdog_status(now=0, root=empty_root)
    assert empty["ok"] is True
    assert empty["provider_counts"] == {} and empty["provider_staleness"] == {}
    assert empty["counts"] == {"gateways": 0, "endpoints": 0,
                               "distinct_raw_ids": 0, "marker_group_labels": 0}
    assert empty["metadata_coverage"] == {
        "endpoints": 0, "context_length_known": 0, "input_modalities_known": 0,
        "output_modalities_known": 0, "supported_parameters_known": 0,
        "description_known": 0}
    assert empty["metadata_summary"] == {
        "models_with_metadata": 0, "provider_counts": {}}
    assert empty["tick_epoch"] == 0
    assert empty["last_tick_age_s"] == 0 and empty["tick_fresh"] is True
    assert empty["warnings"] == []
    assert empty["pending_alerts"] == 0
    assert empty["site_published"] is False
    assert len(captures) == 1 and captures[0] == []
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)

    # -- mutation isolation: a returned diagnostic never corrupts the snapshot
    _reset()
    first = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    snap_a = calls[0]["snapshot"]
    first["metadata_summary"]["provider_counts"]["z-last"] = 999
    first["stale_providers"].append("MUTATED")
    # the snapshot stays the deduplicated observation; the roster keeps both.
    assert snap_a["providers"]["z-last"]["model_ids"].count("dup") == 1
    assert loads[-1]["providers"]["z-last"].count("dup") == 2
    assert snap_a["warnings"] == ["metadata_missing", "metadata_invalid"]
    assert snap_a == calls[0]["before"]
    _reset()
    later = mcp_server.watchdog_status(now=1060.75, root=tmp_path)
    assert later["metadata_summary"]["provider_counts"]["z-last"] == 3
    assert later["stale_providers"] == ["z-last", "ghost", "z-last"]
    assert later["snapshot_id"] == first["snapshot_id"]
    assert loads[0] == control


def test_watchdog_status_local_clock_contract(monkeypatch, tmp_path):
    """U4: the legacy ``now=None`` parameter keeps its local meaning with a
    COMPLETE safe boundary: ``None`` reads ``time.time()`` exactly once and
    validates what it got; an explicit native int or finite non-negative float
    inside the safe integer range is used ONCE (0 is literal, never a fallback)
    and normalized with ``int()`` exactly once for the fixed pure core clock;
    negative fractional, negative/bool/non-numeric, NaN/Infinity, unsafe and
    overhead integers fail with one fixed flat string BEFORE state, core, site
    or pending I/O and never echo the value.

    Tick validation stays a SEPARATE contract: the raw roster epoch is strict
    and unknown/future means not fresh, with an inclusive cadence boundary and
    the exact core warning union/order.
    """
    import inspect
    import os

    import watchdog_catalog.query as real_query

    real_status = real_query.watchdog_status
    real_load = mcp_server.state.load_roster
    real_pending = mcp_server.state.load_pending

    configs = {"p-one": {"name": "P One"}}
    wiring = {"p-one": {
        "chat_completions_url": "https://p.example/v1/chat/completions",
        "api_type": "openai_compatible"}}
    monkeypatch.setattr(mcp_server, "PROVIDERS", configs)
    monkeypatch.setattr(mcp_server, "GATEWAY_WIRING", wiring)
    monkeypatch.setattr(mcp_server, "CADENCE_S", 1000)

    full_meta = {"context_length": 1000,
                 "architecture": {"input_modalities": ["text"],
                                  "output_modalities": ["text"]},
                 "supported_parameters": [], "description": "d"}
    (tmp_path / "state").mkdir()
    (tmp_path / "site").mkdir()
    roster_path = tmp_path / "state" / "roster.json"
    site_path = tmp_path / "site" / "index.html"
    pending_path = tmp_path / "state" / "pending_alerts.json"

    def _write_roster(doc):
        roster_path.write_text(json.dumps(doc))

    def _std_roster(tick=None, **extra):
        doc = {"providers": {"p-one": ["m"]},
               "provider_models": {"p-one": {"m": full_meta}}}
        if tick is not _MISSING:
            doc["tick_epoch"] = tick
        doc.update(extra)
        return doc

    _MISSING = object()
    _write_roster({"tick_epoch": 1000, "providers": {"p-one": ["m"]},
                   "provider_models": {"p-one": {"m": full_meta}}})
    site_path.write_text("<html></html>")
    os.utime(site_path, (1000.5, 1000.5))
    pending_path.write_text(json.dumps([{"payload": {"a": 1}}]))

    clock_reads, loads, calls, pending_reads, stats = [], [], [], [], []
    real_stat = os.stat

    def _fake_time():
        clock_reads.append(True)
        return 1060.75

    def _spy_load(path):
        loads.append(real_load(path))
        return loads[-1]

    def _spy_pending(path):
        pending_reads.append(path)
        return real_pending(path)

    def _spy_status(snapshot, arguments, *, now):
        out = real_status(snapshot, arguments, now=now)
        calls.append({"arguments": arguments, "now": now, "result": out})
        return out

    def _spy_stat(path, *args, **kwargs):
        stats.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(mcp_server.time, "time", _fake_time)
    monkeypatch.setattr(mcp_server.state, "load_roster", _spy_load)
    monkeypatch.setattr(mcp_server.state, "load_pending", _spy_pending)
    monkeypatch.setattr(real_query, "watchdog_status", _spy_status)

    def _reset():
        clock_reads.clear()
        loads.clear()
        calls.clear()
        pending_reads.clear()
        stats.clear()

    def _call_without_io(**kwargs):
        """Call with os.stat spied: proves NO state/site/pending probe happened.

        ``Path.resolve()`` never stats, so an empty record is a real assertion
        that the rejection happened before every filesystem observation.
        """
        saved = os.stat
        os.stat = _spy_stat
        try:
            return mcp_server.watchdog_status(**kwargs)
        finally:
            os.stat = saved

    def _assert_core_clock(value):
        now_seen = calls[-1]["now"]
        assert type(now_seen) is int, (value, now_seen)
        assert not isinstance(now_seen, bool)
        assert 0 <= now_seen <= 9007199254740991
        assert calls[-1]["arguments"] == {}

    # -- the local signature is unchanged and exposes the SAME parameter -----
    params = inspect.signature(mcp_server.watchdog_status).parameters
    assert list(params) == ["now", "root"]
    assert params["now"].default is None
    assert params["now"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    # -- None: one clock read, one normalization, raw value kept for site age -
    _reset()
    default = mcp_server.watchdog_status(root=tmp_path)
    assert default["ok"] is True
    assert clock_reads == [True]                 # read EXACTLY once
    assert len(calls) == 1 and len(loads) == 1
    _assert_core_clock(None)
    assert calls[0]["now"] == 1060               # int(1060.75)
    assert default["last_tick_age_s"] == 60
    assert default["site_publish_age_s"] == 60   # raw 1060.75 - 1000.5

    # -- explicit values never fall back; 0 is literal and stays 0 -----------
    rows = [
        # (now, expected core clock, expected last_tick_age_s, site age)
        (0, 0, None, 0),            # tick 1000 is in the future of 0
        (1060, 1060, 60, 59),       # integral explicit clock
        (1060.0, 1060, 60, 59),     # finite float, exact
        (1060.75, 1060, 60, 60),    # fractional: raw clock, NOT normalized
        (0.0, 0, None, 0),
    ]
    for value, core_now, age, site_age in rows:
        _reset()
        row = mcp_server.watchdog_status(now=value, root=tmp_path)
        assert row["ok"] is True, value
        assert clock_reads == [], value          # no fallback clock read
        _assert_core_clock(value)
        assert calls[0]["now"] == core_now, value
        assert row["last_tick_age_s"] == age, value
        if age is None:
            assert row["tick_fresh"] is False, value
            assert row["warnings"] == ["tick_epoch_future"], value
        else:
            assert row["tick_fresh"] is True, value
            assert row["warnings"] == [], value
        assert row["site_publish_age_s"] == site_age, value

    # explicit safe boundaries: the max is accepted, one past it is not.
    for value in (9007199254740991, 9007199254740991.0):
        _reset()
        boundary = mcp_server.watchdog_status(now=value, root=tmp_path)
        assert boundary["ok"] is True
        _assert_core_clock(value)
        assert calls[0]["now"] == 9007199254740991
        assert boundary["last_tick_age_s"] == 9007199254739991
        assert boundary["tick_fresh"] is False
        assert boundary["warnings"] == ["tick_stale"]

    # -- every invalid explicit clock: fixed flat string, ZERO I/O -----------
    rejected = [-0.5, -1, True, False, "1060", float("nan"), float("inf"),
                float("-inf"), 9007199254740992, 9007199254740992.0,
                10**400, [0], {}, object()]
    for bad in rejected:
        _reset()
        bad_res = _call_without_io(now=bad, root=tmp_path)
        assert bad_res["ok"] is False, repr(bad)
        assert bad_res["error"] == (
            "now must be a finite non-negative safe number"), repr(bad)
        assert set(bad_res) == {"tool", "state_root", "ok", "error"}
        assert repr(bad) not in bad_res["error"]
        assert loads == [] and calls == [] and pending_reads == []
        assert clock_reads == [] and stats == [], repr(bad)

    # -- the DEFAULT clock return is validated by the same boundary ----------
    def _bad_clock(value):
        def _fake_bad_clock():
            clock_reads.append(True)
            return value
        return _fake_bad_clock

    for bad_default in (-5.0, float("nan"), float("inf"), True, "nope",
                        9007199254740992, 10**400):
        monkeypatch.setattr(mcp_server.time, "time", _bad_clock(bad_default))
        _reset()
        bad_default_res = _call_without_io(root=tmp_path)
        assert bad_default_res["ok"] is False, repr(bad_default)
        assert bad_default_res["error"] == (
            "now must be a finite non-negative safe number")
        assert set(bad_default_res) == {"tool", "state_root", "ok", "error"}
        assert len(clock_reads) == 1              # captured once, then rejected
        assert loads == [] and calls == [] and pending_reads == []
        assert stats == [], repr(bad_default)
    monkeypatch.setattr(mcp_server.time, "time", _fake_time)

    # -- tick validation is a SEPARATE contract -------------------------------
    tick_rows = [
        # (tick, now, epoch, age, fresh, warnings)
        (1000, 1000, 1000, 0, True, []),
        (None, 1000, None, None, False, ["tick_epoch_unknown"]),
        (_MISSING, 1000, None, None, False, ["tick_epoch_unknown"]),
        (1000.0, 1000, None, None, False, ["tick_epoch_unknown"]),
        (True, 1000, None, None, False, ["tick_epoch_unknown"]),
        (-5, 1000, None, None, False, ["tick_epoch_unknown"]),
        (9007199254740992, 1000, None, None, False, ["tick_epoch_unknown"]),
        (2000, 1000, 2000, None, False, ["tick_epoch_future"]),
        (0, 1000, 0, 1000, True, []),             # exact cadence is FRESH
        (0, 1001, 0, 1001, False, ["tick_stale"]),  # one second past
    ]
    for tick, now_value, epoch, age, fresh, warnings in tick_rows:
        _write_roster(_std_roster(tick))
        _reset()
        row = mcp_server.watchdog_status(now=now_value, root=tmp_path)
        assert row["ok"] is True, (tick, now_value)
        assert row["tick_epoch"] == epoch, (tick, now_value)
        assert row["last_tick_age_s"] == age, (tick, now_value)
        assert row["tick_fresh"] is fresh, (tick, now_value)
        assert isinstance(row["tick_fresh"], bool)
        if age is not None:
            assert type(row["last_tick_age_s"]) is int
        assert row["warnings"] == warnings, (tick, now_value)
        assert len(loads) == 1 and len(calls) == 1

    # snapshot warnings are UNIONED with the derived status code, in the frozen
    # order -- a status warning never replaces or reorders snapshot warnings.
    _write_roster({"tick_epoch": 0, "providers": {"p-one": ["m"]}})
    _reset()
    union = mcp_server.watchdog_status(now=1001, root=tmp_path)
    assert union["warnings"] == ["metadata_missing", "tick_stale"]
    assert union["metadata_coverage"] == {
        "endpoints": 1, "context_length_known": 0, "input_modalities_known": 0,
        "output_modalities_known": 0, "supported_parameters_known": 0,
        "description_known": 0}
    assert union["metadata_summary"] == {
        "models_with_metadata": 0, "provider_counts": {"p-one": 0}}

    _write_roster({"tick_epoch": 1000, "providers": {"p-one": ["m"]},
                   "provider_models": {"p-one": {"m": full_meta}}})

