"""Unit 2i runtime tests for the producer-side captured-snapshot adapter.

These tests exercise ONLY the public runtime entry point of the new producer
module -- ``catalog_export.build_snapshot`` -- plus the accepted
``watchdog_catalog.catalog`` primitives it composes. The adapter is imported
LAZILY inside each test so that a missing module or symbol is a focused RED
(one failing assertion per group) rather than an unrelated collection error.

Four coherent groups, edges expressed as row loops rather than one test per
row:

* G1 -- reverse capture: a captured active registry plus one captured roster
  observation reconstructed from FIVE independently-authored public fixtures
  must reproduce each fixture SEMANTICALLY (dict equality plus an identical
  ``snapshot_id`` computed here with stdlib ``json``+``hashlib`` only, never by
  the accepted hasher). One synthetic row then pins non-alphabetic display
  order, literal code-point canonicalisation of unsorted/duplicated input IDs,
  a padded/Unicode/long/non-free ID set, name fallback, a missing active
  provider (zero-ID), an excluded unknown provider, and a shared raw ID whose
  per-provider metadata must NOT be flattened. "Semantic equality" is NOT
  original-file byte equality.
* G2 -- the structural and programming boundary: root ``TypeError``s, every
  structural fault collapsing to the frozen ``invalid_snapshot`` code and
  message, normalisation of unsorted/duplicate input, and no input mutation or
  partial output on failure.
* G3 -- the metadata hierarchy, epoch and staleness matrix, aggregated in the
  frozen warning order.
* G4 -- closed projection (no secret/private field ever serialized), retained
  public text, freshness/isolation of every mutable container, and a REAL
  fresh-process import-purity audit of the producer module.

Every expected value below is an independent literal restatement of the frozen
contract or of an existing fixture; no expected snapshot is generated from the
adapter and no reference engine is imported from ``tests/test_catalog.py``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"

INVALID_SNAPSHOT = "invalid_snapshot"
INVALID_SNAPSHOT_MESSAGE = "Invalid catalogue snapshot."

# The five independently-authored public fixtures the reverse adapter must
# reproduce semantically (dict equality + identical snapshot_id).
REVERSE_FIXTURES = (
    "basic.json",
    "lookup.json",
    "provider-extension.json",
    "ordering.json",
    "status-short-cadence.json",
)

PUBLIC_TOP_FIELDS = frozenset({
    "schema_version", "snapshot_id", "tick_epoch", "cadence_s",
    "provider_order", "providers", "warnings"})
PUBLIC_PROVIDER_FIELDS = frozenset({
    "display_name", "chat_completions_url", "api_type",
    "model_ids", "models", "stale"})
PUBLIC_MODEL_FIELDS = frozenset({
    "id", "name", "description", "context_length", "architecture",
    "supported_parameters", "pricing"})
PUBLIC_ARCHITECTURE_FIELDS = frozenset({"input_modalities", "output_modalities"})
PUBLIC_PRICING_FIELDS = frozenset({"prompt", "completion"})

WARNING_ORDER = ("tick_epoch_unknown", "metadata_missing", "metadata_invalid")


def _export():
    """Lazily import the producer so RED is a focused missing-symbol failure."""
    from catalog_export import build_snapshot
    from watchdog_catalog import catalog
    return build_snapshot, catalog


def _load_fixture(name):
    path = FIXTURE_DIR / name
    assert path.is_file(), f"fixture missing: {name}"
    return json.loads(path.read_text(encoding="utf-8"))


def _independent_snapshot_id(snapshot):
    """The canonical body digest, computed with stdlib ``json``+``hashlib`` ONLY
    (sorted keys, compact separators, ``ensure_ascii``, root id excluded) --
    an oracle independent of the accepted ``catalog.snapshot_hash``."""
    body = {key: value for key, value in snapshot.items() if key != "snapshot_id"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(key, name="RECORD-NAME", url=None, api_type="openai", **extras):
    record = {
        "key": key,
        "name": name,
        "chat_completions_url": url or f"https://{key}.example/v1/chat/completions",
        "api_type": api_type,
    }
    record.update(extras)
    return record


def _reverse_inputs(snapshot):
    """Rebuild a captured (registry, roster) pair from a public fixture.

    Registry records carry ONLY the four resolved members, taken from the
    public provider entry; the raw per-gateway model metadata is the projected
    model object itself (projection is idempotent on an already-projected
    object). Fixture warnings are NEVER copied into the roster -- they must be
    re-derived. Fresh containers only; the fixture object is not reused.
    """
    registry, providers, provider_models = [], {}, {}
    for key in snapshot["provider_order"]:
        entry = snapshot["providers"][key]
        registry.append({
            "key": key,
            "name": entry["display_name"],
            "chat_completions_url": entry["chat_completions_url"],
            "api_type": entry["api_type"],
        })
        providers[key] = list(entry["model_ids"])
        provider_models[key] = {
            raw_id: copy.deepcopy(entry["models"][raw_id])
            for raw_id in entry["model_ids"]
        }
    roster = {
        "tick_epoch": snapshot["tick_epoch"],
        "providers": providers,
        "provider_models": provider_models,
        "stale_providers": [key for key in snapshot["provider_order"]
                            if snapshot["providers"][key]["stale"]],
    }
    return registry, roster


def _same_inputs(left, right):
    """Structural equality for a captured input used by the no-mutation checks.

    An opaque sentinel (a plain ``object()``) is compared by TYPE only, because
    ``copy.deepcopy`` cannot preserve such an object's identity; every real
    container and scalar is compared structurally.
    """
    if type(left) is object or type(right) is object:
        return type(left) is object and type(right) is object
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            _same_inputs(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_inputs(a, b) for a, b in zip(left, right))
    return left == right


# ============================ G1: reverse capture ============================

def test_reverse_capture_reproduces_fixtures_and_canonicalises_ids():
    build_snapshot, catalog = _export()

    # Five distinct, nonempty, existing fixtures -- the inventory itself.
    assert len(REVERSE_FIXTURES) == 5
    assert len(set(REVERSE_FIXTURES)) == 5
    assert all(REVERSE_FIXTURES)

    for name in REVERSE_FIXTURES:
        expected = _load_fixture(name)
        # Independent oracle sanity: the fixture's own id is the canonical body
        # digest, so the reverse comparison below is against a real seal.
        assert expected["snapshot_id"] == _independent_snapshot_id(expected), name
        registry, roster = _reverse_inputs(expected)
        registry_before, roster_before = copy.deepcopy(registry), copy.deepcopy(roster)
        out = build_snapshot(registry, roster, cadence_s=expected["cadence_s"])
        # SEMANTIC dict equality (not original-file byte equality) + same id.
        assert out == expected, name
        assert out["snapshot_id"] == expected["snapshot_id"], name
        assert out is not expected, name
        assert registry == registry_before and roster == roster_before, name

    # One synthetic canonical-ID row.
    long1 = "vendor/long9999999999999999999999999999999999999999999999999999999999999991"
    long2 = "vendor/long9999999999999999999999999999999999999999999999999999999999999992"
    registry = [
        _record("zz-last", name="Zeta Display"),
        _record("aa-first", name=""),          # empty name -> key fallback
        _record("mm-missing", name=None),      # missing name -> key fallback
    ]
    input_ids = ["z10", "z2", "z2", "A", "a", "not-free", " padded ",
                 "vendor/Ａ", "vendor/😀", "vendor/item١", long1, long2,
                 "vendor/shared"]
    roster = {
        "tick_epoch": 1759200000,
        "providers": {
            "aa-first": list(input_ids),
            "zz-last": ["vendor/shared"],
            "excluded-unknown": ["junk/id"],
        },
        "provider_models": {
            "aa-first": {"vendor/shared": {"name": "Alpha Shared",
                                           "context_length": 131072}},
            "zz-last": {"vendor/shared": {"name": "Zeta Shared",
                                          "context_length": 8192}},
            "excluded-unknown": {"junk/id": {"name": "Junk"}},
        },
        "stale_providers": ["zz-last"],
    }
    out = build_snapshot(registry, roster, cadence_s=1)

    assert set(out) == PUBLIC_TOP_FIELDS
    assert out["schema_version"] == 1
    assert out["cadence_s"] == 1
    assert out["tick_epoch"] == 1759200000
    # The ten IDs with no captured metadata are metadata_missing; the shared ID
    # that has metadata contributes none.
    assert out["warnings"] == ["metadata_missing"]
    assert out["snapshot_id"] == _independent_snapshot_id(out)
    # Non-alphabetic display order preserved EXACTLY (record order, not sorted).
    assert out["provider_order"] == ["zz-last", "aa-first", "mm-missing"]
    assert set(out["providers"]) == {"zz-last", "aa-first", "mm-missing"}
    assert "excluded-unknown" not in out["providers"]

    # Name fallback to the tracked key on empty / missing names.
    assert out["providers"]["aa-first"]["display_name"] == "aa-first"
    assert out["providers"]["mm-missing"]["display_name"] == "mm-missing"

    # Literal code-point canonicalisation of the unsorted, duplicated input.
    assert out["providers"]["aa-first"]["model_ids"] == [
        " padded ", "A", "a", "not-free", "vendor/item١", long1, long2,
        "vendor/shared", "vendor/Ａ", "vendor/😀", "z10", "z2"]
    # A missing active roster key -> retained zero-ID provider (not dropped).
    assert out["providers"]["mm-missing"]["model_ids"] == []
    assert out["providers"]["mm-missing"]["models"] == {}

    # Staleness is literal key membership, independent of the tick.
    assert out["providers"]["zz-last"]["stale"] is True
    assert out["providers"]["aa-first"]["stale"] is False
    assert out["providers"]["mm-missing"]["stale"] is False

    # A shared raw ID keeps each provider's OWN metadata (never flattened).
    alpha = out["providers"]["aa-first"]["models"]["vendor/shared"]
    zeta = out["providers"]["zz-last"]["models"]["vendor/shared"]
    assert alpha["context_length"] == 131072 and alpha["name"] == "Alpha Shared"
    assert zeta["context_length"] == 8192 and zeta["name"] == "Zeta Shared"

    # Exact field sets and strict types across the synthetic snapshot.
    for key in out["provider_order"]:
        entry = out["providers"][key]
        assert set(entry) == PUBLIC_PROVIDER_FIELDS
        assert isinstance(entry["display_name"], str) and entry["display_name"]
        assert isinstance(entry["api_type"], str) and entry["api_type"]
        assert isinstance(entry["stale"], bool)
        assert entry["model_ids"] == sorted(set(entry["model_ids"]))
        assert set(entry["models"]) == set(entry["model_ids"])
        for raw_id, model in entry["models"].items():
            assert set(model) == PUBLIC_MODEL_FIELDS
            assert model["id"] == raw_id
            assert set(model["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS


# ==================== G2: structural / programmer boundary ==================

def test_structural_and_programmer_boundaries_are_exact():
    build_snapshot, catalog = _export()

    def expect_type_error(registry, roster, cid):
        with pytest.raises(TypeError) as excinfo:
            build_snapshot(registry, roster, cadence_s=1)
        message = str(excinfo.value)
        assert message.startswith("build_snapshot()"), cid
        return excinfo.value

    def expect_invalid(registry, roster, cadence_s=1, cid=""):
        with pytest.raises(catalog.CatalogError) as excinfo:
            build_snapshot(registry, roster, cadence_s=cadence_s)
        assert excinfo.value.code == INVALID_SNAPSHOT, cid
        assert excinfo.value.message == INVALID_SNAPSHOT_MESSAGE, cid
        return excinfo.value

    empty_roster = {"tick_epoch": 0, "providers": {}}

    # Root argument types are programming errors, checked before content.
    for bad in ({}, "", None, 0, 3.5, {"key": "x"}):
        expect_type_error(bad, empty_roster, f"registry root {bad!r}")
    for bad in ("", [], 0, None, ("providers",)):
        expect_type_error([], bad, f"roster root {bad!r}")

    # A legitimately empty registry (list OR tuple) is a valid empty catalogue.
    for empty in ([], ()):
        out = build_snapshot(empty, empty_roster, cadence_s=1)
        assert out["provider_order"] == [] and out["providers"] == {}
        assert out["warnings"] == []
        assert out["snapshot_id"] == _independent_snapshot_id(out)

    # Cadence: strict positive JSON-safe int; bool/float/str/zero/unsafe reject.
    for cadence in (0, -1, True, False, 1.0, 1.5, "1", None,
                    catalog.MAX_SAFE_INT + 1):
        expect_invalid([], empty_roster, cadence_s=cadence, cid=f"cadence {cadence!r}")
    for cadence in (1, 120, catalog.MAX_SAFE_INT):
        assert build_snapshot([], empty_roster, cadence_s=cadence)["cadence_s"] == cadence

    # Roster provider map: missing / wrong container type is structural.
    for bad_roster in ({}, {"tick_epoch": 0}, {"providers": []},
                       {"providers": "x"}, {"providers": None}):
        expect_invalid([], bad_roster, cid=f"roster {bad_roster!r}")

    # Provider keys and ID lists are validated for EVERY entry -- including an
    # excluded (unknown/disabled) provider with an otherwise empty registry.
    for providers in ({"": []}, {3: []}, {None: []}, {"p": "not-a-list"},
                      {"p": ["ok", 1]}, {"p": [1, 2]}, {"p": [""]},
                      {"p": [None]}, {"p": ("tuple",)}):
        expect_invalid([], {"tick_epoch": 0, "providers": providers},
                       cid=f"providers {providers!r}")

    # Records: non-dict, bad/duplicate keys, bad wiring type, bad URL syntax.
    bad_registries = [
        [42], ["x"], [None], [{}], [{"key": ""}], [{"key": 3}],
        [{"key": "k"}],                                             # missing url
        [{"key": "k", "chat_completions_url": "", "api_type": "openai"}],
        [{"key": "k", "chat_completions_url": object(), "api_type": "openai"}],
        [{"key": "k", "chat_completions_url": 3, "api_type": "openai"}],
        [{"key": "k", "chat_completions_url": "https://k.example/x", "api_type": ""}],
        [{"key": "k", "chat_completions_url": "https://k.example/x",
          "api_type": object()}],
        [{"key": "k", "chat_completions_url": "https://k.example/x"}],
        # A string URL with bad SYNTAX is left to the accepted validator.
        [{"key": "k", "chat_completions_url": "ftp://k.example/x",
          "api_type": "openai"}],
    ]
    for registry in bad_registries:
        expect_invalid(registry, {"providers": {}}, cid=f"registry {registry!r}")

    # Duplicate resolved registry keys reject BEFORE any collapse.
    expect_invalid([_record("k"), _record("k")], {"providers": {"k": []}},
                   cid="duplicate registry key")

    # Unsorted/duplicate input IDs are NORMALISED (success), never a negative.
    out = build_snapshot([_record("p")],
                         {"providers": {"p": ["z2", "z10", "z2", "A", "a"]}},
                         cadence_s=1)
    assert out["providers"]["p"]["model_ids"] == ["A", "a", "z10", "z2"]

    # Inputs are unchanged even on failure; no partial snapshot escapes.
    registry = [_record("p", auth={"token": "SENTINEL"})]
    roster = {"providers": {"p": ["m"], "bad": ["", "x"]}}
    registry_before, roster_before = copy.deepcopy(registry), copy.deepcopy(roster)
    with pytest.raises(catalog.CatalogError):
        build_snapshot(registry, roster, cadence_s=1)
    assert registry == registry_before and roster == roster_before

    # Fixed, non-reflective programming messages: no hostile input echo.
    hostile = "<script>alert(1)</script>"
    with pytest.raises(TypeError) as excinfo:
        build_snapshot(hostile, empty_roster, cadence_s=1)
    assert hostile not in str(excinfo.value)
    assert str(excinfo.value) == (
        "build_snapshot() requires a registry list or tuple of provider records")


# ============== G3: metadata hierarchy, epoch and staleness matrix ===========

def test_metadata_hierarchy_epoch_and_staleness_matrix():
    build_snapshot, catalog = _export()

    reg = [_record("p")]

    def warnings_for(roster, cadence_s=1):
        return build_snapshot(reg, roster, cadence_s=cadence_s)["warnings"]

    def base(**extra):
        roster = {"tick_epoch": 0, "providers": {"p": ["m"]}}
        roster.update(extra)
        return roster

    # (a) Missing vs malformed, exactly per tracked pair, no manufactured code.
    assert warnings_for(base()) == ["metadata_missing"]                     # outer absent
    assert warnings_for(base(provider_models=None)) == ["metadata_missing"]  # outer None
    assert warnings_for(base(provider_models=[])) == ["metadata_invalid"]    # outer bad
    assert warnings_for(base(provider_models="x")) == ["metadata_invalid"]
    assert warnings_for(base(provider_models=True)) == ["metadata_invalid"]
    assert warnings_for(base(provider_models=3)) == ["metadata_invalid"]
    assert warnings_for(base(provider_models={})) == ["metadata_missing"]    # provider absent
    assert warnings_for(base(provider_models={"p": None})) == ["metadata_missing"]
    assert warnings_for(base(provider_models={"p": ["bad"]})) == ["metadata_invalid"]
    assert warnings_for(base(provider_models={"p": "bad"})) == ["metadata_invalid"]
    assert warnings_for(base(provider_models={"p": {}})) == ["metadata_missing"]
    assert warnings_for(base(provider_models={"p": {"m": None}})) == ["metadata_missing"]
    assert warnings_for(base(provider_models={"p": {"m": "bad"}})) == ["metadata_invalid"]
    assert warnings_for(base(provider_models={"p": {"m": []}})) == ["metadata_invalid"]
    # A per-model dict projects normally: {name} only -> capabilities missing.
    assert warnings_for(base(provider_models={"p": {"m": {"name": "N"}}})) == ["metadata_missing"]

    # Zero-ID active provider manufactures NO metadata warnings, whatever the
    # (unused) metadata container says.
    assert warnings_for({"tick_epoch": 0, "providers": {"p": []},
                         "provider_models": {}}) == []
    assert warnings_for({"tick_epoch": 0, "providers": {"p": []},
                         "provider_models": "bad"}) == []

    # Combined codes dedup in the frozen vocabulary order.
    combined = {
        "tick_epoch": None,
        "providers": {"p": ["good", "bad"]},
        "provider_models": {"p": {"good": {"name": "G"}, "bad": "x"}},
    }
    assert warnings_for(combined) == list(WARNING_ORDER)

    # (b) Epoch: strict non-negative safe int retained (0 is KNOWN); everything
    # else becomes unknown + tick_epoch_unknown, and the model pair survives.
    # The metadata fragment is fully known so ONLY the epoch contributes a code.
    known_model = {"name": "N", "description": "d", "context_length": 4096,
                   "architecture": {"input_modalities": ["text"],
                                    "output_modalities": ["text"]},
                   "supported_parameters": ["temperature"],
                   "pricing": {"prompt": "0", "completion": "0"}}
    good_meta = {"providers": {"p": ["m"]},
                 "provider_models": {"p": {"m": dict(known_model)}}}
    for known in (0, 1, 1759200000, catalog.MAX_SAFE_INT):
        roster = dict(good_meta, tick_epoch=known)
        out = build_snapshot(reg, roster, cadence_s=1)
        assert out["tick_epoch"] == known, known
        assert out["warnings"] == [], known
        assert out["providers"]["p"]["models"]["m"]["name"] == "N", known
    for unknown in (None, True, False, 1.0, -1, catalog.MAX_SAFE_INT + 1,
                    float("nan"), float("inf"), "1", [], {}):
        roster = dict(good_meta, tick_epoch=unknown)
        out = build_snapshot(reg, roster, cadence_s=1)
        assert out["tick_epoch"] is None, unknown
        assert out["warnings"] == ["tick_epoch_unknown"], unknown
        assert out["providers"]["p"]["models"]["m"]["name"] == "N", unknown
    # A missing tick_epoch key is unknown too.
    out = build_snapshot(reg, good_meta, cadence_s=1)
    assert out["tick_epoch"] is None and out["warnings"] == ["tick_epoch_unknown"]

    # (c) Staleness: literal key membership, independent of the epoch; a
    # non-list or a bare string is empty membership (all False).
    stale_cases = [
        (None, {"p": False}),
        ("p", {"p": False}),
        (["p"], {"p": True}),
        (["p", 3, None, "other"], {"p": True}),
        (["other"], {"p": False}),
        ([], {"p": False}),
    ]
    for stale_raw, expected in stale_cases:
        for tick in (0, None):
            roster = {"tick_epoch": tick, "providers": {"p": []},
                      "stale_providers": stale_raw}
            out = build_snapshot([_record("p"), _record("other")], roster,
                                 cadence_s=1)
            for key, want in expected.items():
                assert out["providers"][key]["stale"] is want, (stale_raw, key)

    # (d) The tracked map key is authoritative over a mismatched upstream id,
    # and extra raw roster warnings/private containers are never copied.
    roster = {
        "tick_epoch": 0,
        "providers": {"p": ["m"]},
        "provider_models": {"p": {"m": {"id": "DIFFERENT", "name": "Keep",
                                        "context_length": 8}}},
        "warnings": ["metadata_invalid"],
        "transients": {"private": "SENTINEL"},
        "ratelimits": {"headers": "SENTINEL"},
    }
    out = build_snapshot(reg, roster, cadence_s=1)
    model = out["providers"]["p"]["models"]["m"]
    assert model["id"] == "m" and model["name"] == "Keep"
    # The roster's raw warning list is NOT copied: the only code is the one
    # re-derived from the tracked pair's own metadata.
    assert out["warnings"] == ["metadata_missing"]

    # (e) Known-empty vs unknown capabilities survive through the projector.
    roster = {
        "tick_epoch": 0,
        "providers": {"p": ["known", "unknown"]},
        "provider_models": {"p": {
            "known": {"context_length": 0,
                      "architecture": {"input_modalities": [],
                                       "output_modalities": []},
                      "supported_parameters": []},
            "unknown": {"context_length": None,
                        "architecture": {"input_modalities": None,
                                         "output_modalities": None},
                        "supported_parameters": None},
        }},
    }
    out = build_snapshot(reg, roster, cadence_s=1)
    known = out["providers"]["p"]["models"]["known"]
    unknown = out["providers"]["p"]["models"]["unknown"]
    assert known["context_length"] == 0
    assert known["architecture"] == {"input_modalities": [],
                                     "output_modalities": []}
    assert known["supported_parameters"] == []
    assert unknown["context_length"] is None
    assert unknown["architecture"] == {"input_modalities": None,
                                       "output_modalities": None}
    assert unknown["supported_parameters"] is None
    assert out["warnings"] == ["metadata_missing"]


# ========= G4: closed projection, freshness and real import purity ==========

# Application/SDK modules and application data files a producer import must
# never touch. Shared by every fresh-process audit in this group.
FORBIDDEN_IMPORTS = [
    "mcp", "mcp_server", "build_site", "state", "config_loader", "providers",
    "inference_watchdog", "notify", "envfile", "diffing", "detection", "alive",
    "pending_removals", "probe_select", "probe_state", "probe_zero_credit",
    "anthropic", "httpx", "requests",
    "watchdog_catalog.query", "watchdog_catalog.server", "watchdog_catalog.adapter",
]
DATA_PATTERNS = ["/.env", "roster", "tools.json", "catalog.json",
                 "/state/", "/providers/"]

_IMPORT_PROBE = r'''
import importlib, json, os, sys

forbidden = json.loads(os.environ["WC_FORBIDDEN"])
patterns = json.loads(os.environ["WC_DATA_PATTERNS"])
order = json.loads(os.environ["WC_IMPORT_ORDER"])

imports, opens, net = [], [], []

def _audit(name, args):
    if name == "import":
        module = args[0]
        for prefix in forbidden:
            if module == prefix or module.startswith(prefix + "."):
                imports.append(module)
                return
    elif name == "open":
        path = args[0]
        if isinstance(path, str):
            norm = path.replace("\\", "/")
            for pattern in patterns:
                if pattern in norm:
                    opens.append(norm)
                    return
    elif name.startswith("socket."):
        net.append(name)

sys.addaudithook(_audit)
for module_name in order:
    importlib.import_module(module_name)

leaked = [n for n in sys.modules
          if any(n == p or n.startswith(p + ".") for p in forbidden)]
sys.stdout.write(json.dumps({
    "imports": imports, "opens": opens, "net": net, "leaked": leaked,
    "resident": [m for m in order if m in sys.modules],
}))
'''


def _run_import_probe(order, forbidden, tmp_path, tag):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cwd = tmp_path / "neutral-cwd"
    cwd.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "WC_FORBIDDEN": json.dumps(forbidden),
        "WC_DATA_PATTERNS": json.dumps(DATA_PATTERNS),
        "WC_IMPORT_ORDER": json.dumps(order),
    }
    probe = subprocess.run([sys.executable, "-B", "-c", _IMPORT_PROBE],
                           cwd=str(cwd), env=env, capture_output=True,
                           text=True, timeout=120)
    assert probe.returncode == 0, (tag, probe.stderr)
    assert probe.stderr == "", (tag, probe.stderr)
    report = json.loads(probe.stdout)
    assert report["imports"] == [], (tag, report)
    assert report["leaked"] == [], (tag, report)
    assert report["opens"] == [], (tag, report)
    assert report["net"] == [], (tag, report)
    return report


def _assert_producer_import_purity(tmp_path):
    # (a) Importing the producer (either entry order) pulls no operator/SDK
    #     module, opens no application data file and touches no socket.
    for order in (["watchdog_catalog.catalog", "catalog_export"],
                  ["catalog_export"]):
        report = _run_import_probe(order, FORBIDDEN_IMPORTS, tmp_path,
                                   f"producer order {order}")
        assert "catalog_export" in report["resident"], order

    # (b) The pure consumer package/query never pulls the producer.
    consumer_forbidden = [p for p in FORBIDDEN_IMPORTS
                          if p != "watchdog_catalog.query"] + ["catalog_export"]
    report = _run_import_probe(["watchdog_catalog", "watchdog_catalog.query"],
                               consumer_forbidden, tmp_path, "consumer order")
    assert "watchdog_catalog.query" in report["resident"]
    assert "catalog_export" not in report["resident"]

    # (c) A plain producer import from a neutral cwd + clean HOME is silent.
    home = tmp_path / "home"
    cwd = tmp_path / "neutral-cwd"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
           "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    plain = subprocess.run([sys.executable, "-B", "-c", "import catalog_export"],
                           cwd=str(cwd), env=env, capture_output=True,
                           text=True, timeout=120)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == "" and plain.stderr == ""


def test_closed_projection_freshness_and_producer_import_purity(tmp_path):
    build_snapshot, catalog = _export()

    token, auth, base, path, probe, untracked, unknown = (
        "SENTINEL-TOKEN", "SENTINEL-AUTH", "SENTINEL-BASE", "SENTINEL-PATH",
        "SENTINEL-PROBE", "SENTINEL-UNTRACKED", "SENTINEL-UNKNOWNPROV")
    hostile_name = "<script>alert('x')</script> 😀"

    registry = [
        _record("p", name="Provider P", _token=token, auth={"token": auth},
                base_url=base, private_path=path, probe=probe, enabled=True,
                _stem="SENTINEL-STEM", dropped_extra=object()),
    ]
    roster = {
        "tick_epoch": 0,
        "providers": {"p": ["keep"]},
        "provider_models": {
            "p": {
                "keep": {"name": hostile_name, "description": "  spaced  ",
                         "context_length": 4096,
                         "architecture": {"input_modalities": ["TEXT"],
                                          "output_modalities": ["text"]},
                         "supported_parameters": ["Temperature"],
                         "pricing": {"prompt": "0", "completion": 0},
                         "unknown_field": "SENTINEL-ROSTER",
                         "nested": {"deep": object()},
                         "auth": {"token": token}},
                "untracked-extra": {"name": untracked},
            },
            "unknown-provider": {"x": {"name": unknown}},
        },
        "warnings": ["SENTINEL-ROSTER"],
        "transients": {"private": "SENTINEL-ROSTER"},
        "ratelimits": {"headers": "SENTINEL-ROSTER"},
    }
    registry_before = copy.deepcopy(registry)
    roster_before = copy.deepcopy(roster)

    out = build_snapshot(registry, roster, cadence_s=3600)

    serialized = json.dumps(out, sort_keys=True, ensure_ascii=False)
    for secret in (token, auth, base, path, probe, untracked, unknown,
                   "SENTINEL-STEM", "SENTINEL-ROSTER"):
        assert secret not in serialized, secret
    assert "unknown-provider" not in serialized
    assert "untracked-extra" not in serialized

    # Exact field sets; retained public text (no arbitrary over-redaction).
    assert set(out) == PUBLIC_TOP_FIELDS
    assert set(out["providers"]) == {"p"}
    entry = out["providers"]["p"]
    assert set(entry) == PUBLIC_PROVIDER_FIELDS
    assert entry["model_ids"] == ["keep"]
    model = entry["models"]["keep"]
    assert set(model) == PUBLIC_MODEL_FIELDS
    assert set(model["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS
    assert set(model["pricing"]) == PUBLIC_PRICING_FIELDS
    assert model["name"] == hostile_name
    assert model["description"] == "  spaced  "
    assert model["architecture"]["input_modalities"] == ["TEXT"]
    assert model["supported_parameters"] == ["Temperature"]

    # Captured inputs are never mutated, dropped extras included.
    assert _same_inputs(registry, registry_before)
    assert _same_inputs(roster, roster_before)

    # Freshness / no aliasing across independent calls.
    first = build_snapshot(registry, roster, cadence_s=3600)
    second = build_snapshot(registry, roster, cadence_s=3600)
    assert first == second and first is not second
    m1 = first["providers"]["p"]["models"]["keep"]
    m2 = second["providers"]["p"]["models"]["keep"]
    source = roster["provider_models"]["p"]["keep"]
    assert m1 is not m2
    for field in ("architecture", "supported_parameters", "pricing"):
        assert m1[field] is not m2[field] and m1[field] is not source[field]
    assert m1["architecture"]["input_modalities"] is not \
        source["architecture"]["input_modalities"]
    assert first["provider_order"] is not second["provider_order"]
    assert first["providers"]["p"]["model_ids"] is not \
        second["providers"]["p"]["model_ids"]
    assert first["warnings"] is not second["warnings"]

    # Mutating one output (or a captured input) never leaks anywhere.
    first["provider_order"].append("mutated")
    first["providers"]["p"]["model_ids"].append("mutated")
    first["warnings"].append("mutated")
    m1["architecture"]["input_modalities"].append("mutated")
    m1["supported_parameters"].append("mutated")
    m1["pricing"]["prompt"] = "mutated"
    assert _same_inputs(registry, registry_before)
    assert _same_inputs(roster, roster_before)
    assert second == build_snapshot(registry, roster, cadence_s=3600)

    roster["provider_models"]["p"]["keep"]["name"] = "MUTATED"
    roster["providers"]["p"].append("late")
    assert out["providers"]["p"]["models"]["keep"]["name"] == hostile_name
    assert out["providers"]["p"]["model_ids"] == ["keep"]

    # Real fresh-process import-purity audit of the producer module.
    _assert_producer_import_purity(tmp_path)
