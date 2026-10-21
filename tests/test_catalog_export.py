"""Unit 2i runtime tests for the producer-side captured-snapshot adapter.

These tests exercise ONLY the public runtime entry point of the new producer
module -- ``catalog_export.build_snapshot`` -- plus the accepted
``watchdog_catalog.catalog`` primitives it composes. The adapter is imported
LAZILY inside each test so that a missing module or symbol is a focused RED
(one failing assertion per group) rather than an unrelated collection error.

Six coherent groups, edges expressed as row loops rather than one test per
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
* G5 -- active-registry public capture (unit 4a): the pure
  ``capture_registry`` projection of the already-resolved provider/wiring maps
  into four-field records in insertion/display order, its root-``TypeError``
  versus selected-record-``CatalogError`` boundary, ignored-extra
  non-traversal, and one end-to-end capture -> REAL ``build_snapshot`` ->
  JSON -> accepted-validator round trip with synthetic secret sentinels.
  HTML rendering, CLI wiring and publication remain OUT of this group's
  boundary.
* G6 -- validated deterministic JSON serialization (unit 4b): the pure
  ``serialize_snapshot`` export-text boundary over the accepted validator and
  whole-value canonical encoder -- exact independent-reference text for every
  existing valid fixture, one trailing LF, ASCII/ensure_ascii evidence,
  object-insertion-order independence with preserved array order, a resealed
  non-default ``provider_order``, capture -> build -> serialize sentinel
  exclusion with hostile retained text, typed pricing distinctions and the
  delegated rejection codes. File publication, HTML escaping and the CLI stay
  OUT of this group's boundary.

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


# =============== G5: active-registry public capture (unit 4a) ===============

def test_active_registry_public_capture_and_secret_sentinels():
    from catalog_export import build_snapshot, capture_registry
    from watchdog_catalog import catalog

    class _Poison:
        """An IGNORED extra that explodes on whole-object operations."""

        def __str__(self):
            raise AssertionError("ignored extra was stringified")

        def __iter__(self):
            raise AssertionError("ignored extra was iterated")

        def __deepcopy__(self, memo):
            raise AssertionError("ignored extra was deep-copied")

    fields = {"key", "name", "chat_completions_url", "api_type"}
    token, auth, base, path, probe = (
        "SENTINEL-4A-TOKEN", "SENTINEL-4A-AUTH", "SENTINEL-4A-BASE",
        "SENTINEL-4A-PATH", "SENTINEL-4A-PROBE")
    pending, unknown = "SENTINEL-4A-PENDING", "SENTINEL-4A-UPSTREAM"
    sentinels = (token, auth, base, path, probe, pending, unknown,
                 "SENTINEL-4A-STEM", "SENTINEL-4A-ROSTER")

    zeta_url = "https://zeta.example/v1/chat/completions"
    alpha_url = "https://alpha.example/v1/chat/completions"
    padded_url = "https://padded.example/v1/chat/completions"
    zero_url = "https://zero-id.example/v1/chat/completions"
    off_url = "https://off.example/v1/chat/completions"
    good_wire = {"chat_completions_url": "https://k.example/v1/chat/completions",
                 "api_type": "openai_compatible"}

    # (a) Exact public projection and INSERTION/display order. Provider order
    # is deliberately neither alphabetical nor the wiring-map order; wiring
    # carries an extra key BEFORE the first provider key, and an `enabled=False`
    # EXTRA inside the already-active map must not become a second filter.
    wiring = {
        "wiring-only": {"chat_completions_url": "https://wiring-only.example/v1/chat/completions",
                        "api_type": "openai_compatible"},
        " pad ": {"chat_completions_url": padded_url,
                  "api_type": "openai_compatible"},
        "zeta": {"chat_completions_url": zeta_url,
                 "api_type": "openai_compatible", "upstream_unknown": unknown,
                 "pending_alert": pending},
        "off-extra": {"chat_completions_url": off_url,
                      "api_type": "openai_compatible"},
        "zero-id": {"chat_completions_url": zero_url,
                    "api_type": "openai_compatible"},
        "Alpha": {"chat_completions_url": alpha_url,
                  "api_type": "openai_compatible"},
    }
    providers = {
        "zeta": {"name": "Zeta ✓", "_token": token, "auth": {"token": auth},
                 "base_url": base, "private_path": path, "probe": probe,
                 "pending_alert": pending, "upstream_unknown": unknown,
                 "_stem": "SENTINEL-4A-STEM", "enabled": True},
        "Alpha": {"name": "  Alpha  ", "_token": token, "enabled": True},
        " pad ": {"name": "", "enabled": True},          # empty -> literal key
        "zero-id": {"name": 17, "enabled": True},        # non-string -> literal key
        "off-extra": {"name": "Off", "enabled": False},  # active: extra wins never
    }
    expected = [
        {"key": "zeta", "name": "Zeta ✓", "chat_completions_url": zeta_url,
         "api_type": "openai_compatible"},
        {"key": "Alpha", "name": "  Alpha  ", "chat_completions_url": alpha_url,
         "api_type": "openai_compatible"},
        {"key": " pad ", "name": " pad ", "chat_completions_url": padded_url,
         "api_type": "openai_compatible"},
        {"key": "zero-id", "name": "zero-id", "chat_completions_url": zero_url,
         "api_type": "openai_compatible"},
        {"key": "off-extra", "name": "Off", "chat_completions_url": off_url,
         "api_type": "openai_compatible"},
    ]
    captured = capture_registry(providers, wiring)
    assert captured == expected
    assert [record["key"] for record in captured] == [
        "zeta", "Alpha", " pad ", "zero-id", "off-extra"]
    for record in captured:
        assert set(record) == fields
        assert not any(secret in json.dumps(record, ensure_ascii=False)
                       for secret in sentinels)
    assert "wiring-only" not in [record["key"] for record in captured]

    # Fresh list of fresh dicts on every call; inputs unchanged.
    second = capture_registry(providers, wiring)
    assert second == captured and second is not captured
    assert all(left is not right for left, right in zip(second, captured))
    second[0]["name"] = "MUTATED"
    second.append({"key": "MUTATED"})
    assert captured[0]["name"] == "Zeta ✓" and len(captured) == 5
    assert providers["zeta"]["name"] == "Zeta ✓"

    # A legitimately EMPTY active registry is valid even with a populated
    # wiring map; bad roots never degrade to that empty registry.
    assert capture_registry({}, wiring) == []
    assert capture_registry({}, {}) is not capture_registry({}, {})

    # (b) Root type gates are programming errors with fixed, non-reflective
    # messages, checked BEFORE any content -- so a poisonous leaf cannot leak.
    for bad in ([], (), "", None, 0, 3.5, {"k": 1}.keys(), ("k",)):
        with pytest.raises(TypeError) as excinfo:
            capture_registry(bad, {})
        assert str(excinfo.value) == (
            "capture_registry() requires a resolved providers mapping")
    for bad in ([], (), "", None, 0, 3.5, {"k": 1}.values(), ("k",)):
        with pytest.raises(TypeError) as excinfo:
            capture_registry({}, bad)
        assert str(excinfo.value) == (
            "capture_registry() requires a resolved gateway wiring mapping")
    with pytest.raises(TypeError):
        capture_registry({"k": _Poison()}, None)

    # Selected-record faults are the frozen structural failure, never a skip.
    bad_cases = [
        ({"k": "not-a-dict"}, {"k": dict(good_wire)}),        # config not a dict
        ({"k": {"name": "K"}}, {}),                           # wiring missing
        ({"k": {"name": "K"}}, {"k": "not-a-dict"}),          # wiring not a dict
        ({"k": {"name": "K"}}, {"k": {}}),                    # url missing
        ({"k": {"name": "K"}},
         {"k": {"chat_completions_url": "", "api_type": "openai_compatible"}}),
        ({"k": {"name": "K"}},
         {"k": {"chat_completions_url": 3, "api_type": "openai_compatible"}}),
        ({"k": {"name": "K"}},
         {"k": {"chat_completions_url": good_wire["chat_completions_url"],
                "api_type": ""}}),
        ({"k": {"name": "K"}},
         {"k": {"chat_completions_url": good_wire["chat_completions_url"],
                "api_type": None}}),
        ({3: {"name": "Three"}}, {3: dict(good_wire)}),       # bad key type
        ({"": {"name": "Empty"}}, {"": dict(good_wire)}),     # empty key
        ({None: {"name": "None"}}, {None: dict(good_wire)}),
    ]
    for bad_providers, bad_wiring in bad_cases:
        with pytest.raises(catalog.CatalogError) as excinfo:
            capture_registry(bad_providers, bad_wiring)
        assert excinfo.value.code == INVALID_SNAPSHOT
        assert excinfo.value.message == INVALID_SNAPSHOT_MESSAGE

    hostile = "<script>alert('x')</script>"
    with pytest.raises(catalog.CatalogError) as excinfo:
        capture_registry({hostile: "not-a-dict"}, {hostile: dict(good_wire)})
    assert hostile not in str(excinfo.value)

    # A malformed selected leaf anywhere fails the WHOLE capture: no partial
    # list is returned and no healthy sibling is salvaged.
    with pytest.raises(catalog.CatalogError):
        capture_registry({"ok": {"name": "OK"}, "bad": "not-a-dict"},
                         {"ok": dict(good_wire), "bad": dict(good_wire)})

    # URL SYNTAX stays with the accepted final validator: capture keeps the
    # nonempty string verbatim, and the REAL builder then rejects it.
    bad_url = "ftp://k.example/v1/chat/completions"
    captured_bad = capture_registry(
        {"k": {"name": "K"}},
        {"k": {"chat_completions_url": bad_url, "api_type": "openai_compatible"}})
    assert captured_bad[0]["chat_completions_url"] == bad_url
    with pytest.raises(catalog.CatalogError):
        build_snapshot(captured_bad, {"providers": {}}, cadence_s=1)

    # (c) Ignored extras are never traversed: poison in every private slot and
    # in an unrelated wiring-only entry neither raises nor leaks. `enabled` is
    # an ignored EXTRA, not a second provider filter.
    poison = _Poison()
    poison_capture = capture_registry(
        {"p": {"name": "P", "_token": poison, "auth": poison,
               "base_url": poison, "private_path": poison, "probe": poison,
               "pending_alert": poison, "upstream_unknown": poison,
               "enabled": False}},
        {"p": {"chat_completions_url": good_wire["chat_completions_url"],
               "api_type": "openai_compatible", "_token": poison,
               "auth": poison, "upstream_unknown": poison},
         "wiring-only": {"chat_completions_url": poison, "api_type": poison}})
    assert poison_capture == [{
        "key": "p", "name": "P",
        "chat_completions_url": good_wire["chat_completions_url"],
        "api_type": "openai_compatible"}]

    # (d) Integration: ONE capture -> the REAL builder -> JSON -> the accepted
    # validator. Public values round-trip; synthetic private sentinels never
    # reach the serialized snapshot.
    roster = {
        "tick_epoch": 0,
        "providers": {
            "zeta": ["m2", "m1", "m2"],
            "Alpha": ["shared"],
            " pad ": [],
            "zero-id": [],
            "off-extra": ["shared"],
            "roster-only": ["m"],
        },
        "provider_models": {
            "zeta": {
                "m1": {"name": "Model ✓ one", "description": "  spaced  ",
                       "context_length": 4096,
                       "architecture": {"input_modalities": ["TEXT"],
                                        "output_modalities": ["text"]},
                       "supported_parameters": ["Temperature"],
                       "pricing": {"prompt": "0", "completion": 0},
                       "unknown_field": unknown},
                "m2": {"name": "M2"},
            },
            "Alpha": {"shared": {"name": "Alpha shared", "context_length": 8}},
            "off-extra": {"shared": {"name": "Off shared", "context_length": 16}},
            "roster-only": {"m": {"name": unknown}},
        },
        "stale_providers": ["Alpha"],
        "pending_alerts": [{"_token": pending}],
        "transients": {"private": auth},
        "ratelimits": {"headers": base},
        "warnings": [unknown],
    }
    providers_before = copy.deepcopy(providers)
    wiring_before = copy.deepcopy(wiring)
    roster_before = copy.deepcopy(roster)

    captured = capture_registry(providers, wiring)
    assert all(set(record) == fields for record in captured)
    snapshot = build_snapshot(captured, roster, cadence_s=3600)

    text = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    for secret in sentinels:
        assert secret not in text, secret
    assert "roster-only" not in snapshot["providers"]
    assert "wiring-only" not in snapshot["providers"]
    assert snapshot["provider_order"] == [
        "zeta", "Alpha", " pad ", "zero-id", "off-extra"]

    # Public values survive verbatim, not over-redacted.
    zeta = snapshot["providers"]["zeta"]
    assert zeta["display_name"] == "Zeta ✓"
    assert zeta["chat_completions_url"] == zeta_url
    assert zeta["api_type"] == "openai_compatible"
    assert zeta["model_ids"] == ["m1", "m2"]       # unsorted/duplicate normalised
    model = zeta["models"]["m1"]
    assert model["name"] == "Model ✓ one"
    assert model["description"] == "  spaced  "
    assert model["context_length"] == 4096
    assert model["architecture"]["input_modalities"] == ["TEXT"]
    assert model["supported_parameters"] == ["Temperature"]
    assert model["pricing"] == {"prompt": "0", "completion": 0}
    assert set(model["pricing"]) == PUBLIC_PRICING_FIELDS
    assert snapshot["providers"][" pad "]["display_name"] == " pad "
    assert snapshot["providers"]["zero-id"]["model_ids"] == []
    assert snapshot["providers"]["off-extra"]["display_name"] == "Off"
    assert snapshot["providers"]["Alpha"]["stale"] is True

    # Per-provider shared-ID metadata is NEVER flattened across providers.
    alpha_shared = snapshot["providers"]["Alpha"]["models"]["shared"]
    off_shared = snapshot["providers"]["off-extra"]["models"]["shared"]
    assert alpha_shared["name"] == "Alpha shared"
    assert alpha_shared["context_length"] == 8
    assert off_shared["name"] == "Off shared"
    assert off_shared["context_length"] == 16
    assert alpha_shared is not off_shared

    # JSON round trip then the REAL accepted validator on the loaded object.
    loaded = json.loads(text)
    assert loaded == snapshot
    assert catalog.validate_snapshot(loaded) is loaded
    assert loaded["providers"]["zeta"]["models"]["m1"]["name"] == "Model ✓ one"

    # Inputs are unchanged by capture + build; captured records never alias
    # the snapshot or a later capture.
    assert _same_inputs(providers, providers_before)
    assert _same_inputs(wiring, wiring_before)
    assert _same_inputs(roster, roster_before)
    captured[0]["name"] = "LATE"
    assert snapshot["providers"]["zeta"]["display_name"] == "Zeta ✓"
    again = capture_registry(providers, wiring)
    assert again[0] is not captured[0]
    assert again[0]["name"] == "Zeta ✓"


# ========= G6: validated deterministic JSON serialization (unit 4b) =========

# Every existing VALID public snapshot fixture, by explicit fixed name -- all
# nine, not a glob over the rule tables.
SERIALIZE_FIXTURES = (
    "basic.json",
    "lookup.json",
    "provider-extension.json",
    "ordering.json",
    "status-short-cadence.json",
    "capability-unknown.json",
    "metadata-edge-cases.json",
    "search-text.json",
    "status-unknown-time.json",
)


def _reference_export_text(snapshot):
    """The export text restated from stdlib ``json`` ONLY (independent oracle).

    Sorted object keys, compact separators, ASCII escapes, finite values only,
    the ENTIRE value including the root ``snapshot_id``, plus exactly one LF.
    """
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False) + "\n"


def _reverse_dict_order(value):
    """Reverse OBJECT member insertion order ONLY; array order is untouched."""
    if isinstance(value, dict):
        return {key: _reverse_dict_order(value[key])
                for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_dict_order(item) for item in value]
    return value


def test_serialize_snapshot_validated_deterministic_export():
    from catalog_export import (build_snapshot, capture_registry,
                                serialize_snapshot)
    from watchdog_catalog import catalog

    messages = {
        INVALID_SNAPSHOT: INVALID_SNAPSHOT_MESSAGE,
        "unsupported_schema_version":
            "Unsupported catalogue schema version.",
    }

    # (a) Every existing valid fixture: exact independent-reference text that
    # INCLUDES the root snapshot_id, one trailing LF and no other framing, an
    # ASCII str, a real accepted-validator round trip, and no input mutation.
    assert len(SERIALIZE_FIXTURES) == 9
    assert len(set(SERIALIZE_FIXTURES)) == 9
    for name in SERIALIZE_FIXTURES:
        snapshot = _load_fixture(name)
        assert snapshot["snapshot_id"] == _independent_snapshot_id(snapshot), name
        before = copy.deepcopy(snapshot)
        text = serialize_snapshot(snapshot)
        assert type(text) is str, name
        assert text == _reference_export_text(snapshot), name
        assert text.endswith("\n") and not text.endswith("\n\n"), name
        assert text.count("\n") == 1, name
        assert not text.startswith("\ufeff"), name
        assert text.isascii(), name
        loaded = catalog.load_snapshot(text)
        assert loaded == snapshot, name
        assert loaded is not snapshot, name
        assert loaded["snapshot_id"] == snapshot["snapshot_id"], name
        assert snapshot == before, name
        assert snapshot["snapshot_id"] == before["snapshot_id"], name

    # (b) Determinism: repeated calls are byte-identical, and reversing OBJECT
    # insertion order at root, provider, model and nested architecture/pricing
    # maps cannot change the exported bytes or the id. Arrays keep their order.
    for name in ("lookup.json", "metadata-edge-cases.json", "search-text.json"):
        snapshot = _load_fixture(name)
        assert len(snapshot["provider_order"]) >= 2, name
        first = serialize_snapshot(snapshot)
        assert serialize_snapshot(snapshot) == first, name
        reordered = _reverse_dict_order(snapshot)
        assert list(reordered) != list(snapshot), name
        assert serialize_snapshot(reordered) == first, name
        assert reordered["snapshot_id"] == snapshot["snapshot_id"], name
        # The array order survives literally -- the serializer sorts objects
        # only, never arrays.
        assert catalog.load_snapshot(first)["provider_order"] == \
            snapshot["provider_order"], name

    # A deliberately reordered provider_order, independently resealed, is a
    # genuine valid snapshot with its OWN digest and text: never silently
    # sorted back into the default provider order by the serializer.
    base = _load_fixture("ordering.json")
    registry, roster = _reverse_inputs(base)
    built = build_snapshot(registry, roster, cadence_s=base["cadence_s"])
    assert built == base
    reordered_snapshot = copy.deepcopy(built)
    reversed_order = list(reversed(built["provider_order"]))
    assert reversed_order != built["provider_order"]
    reordered_snapshot["provider_order"] = reversed_order
    reordered_snapshot["snapshot_id"] = _independent_snapshot_id(
        reordered_snapshot)
    assert reordered_snapshot["snapshot_id"] != base["snapshot_id"]
    assert catalog.validate_snapshot(reordered_snapshot) is reordered_snapshot
    reordered_text = serialize_snapshot(reordered_snapshot)
    assert reordered_text == _reference_export_text(reordered_snapshot)
    assert reordered_text != serialize_snapshot(base)
    assert catalog.load_snapshot(reordered_text)["provider_order"] == \
        reversed_order
    assert built == base

    # (c) ONE capture -> the REAL builder -> serialization: projected public
    # data retained, synthetic private sentinels absent, and hostile retained
    # text surviving JSON decoding under canonical ASCII escaping.
    token, auth, base_url, local_path = (
        "SENTINEL-4B-TOKEN", "SENTINEL-4B-AUTH", "SENTINEL-4B-BASE",
        "SENTINEL-4B-PATH")
    pending, upstream = "SENTINEL-4B-PENDING", "SENTINEL-4B-UPSTREAM"
    hostile = ("Ünïcode 😀 \"quoted\" \\backslash\\ </SCRIPT> "
               "\u2028 \u2029 text-looking-markup <b>x</b>")
    providers = {"p": {"name": "Provider P", "_token": token,
                       "auth": {"token": auth}, "base_url": base_url,
                       "private_path": local_path,
                       "pending_alert": pending,
                       "upstream_unknown": upstream}}
    wiring = {"p": {
        "chat_completions_url": "https://p.example/v1/chat/completions",
        "api_type": "openai_compatible"}}
    roster = {
        "tick_epoch": 1759200000,
        "providers": {"p": ["known", "unknown"]},
        "provider_models": {"p": {
            "known": {"name": "Known", "description": hostile,
                      "context_length": 0,
                      "architecture": {"input_modalities": [],
                                       "output_modalities": []},
                      "supported_parameters": []},
            "unknown": {"name": "Unknown", "description": None,
                        "context_length": None,
                        "architecture": {"input_modalities": None,
                                         "output_modalities": None},
                        "supported_parameters": None},
        }},
        "pending_alerts": [{"_token": pending}],
    }
    snapshot = build_snapshot(capture_registry(providers, wiring), roster,
                              cadence_s=3600)
    text = serialize_snapshot(snapshot)
    for secret in (token, auth, base_url, local_path, pending, upstream):
        assert secret not in text, secret
    assert text == _reference_export_text(snapshot)
    assert text.isascii()
    # Quotes/backslashes and non-ASCII follow canonical ASCII escaping; the
    # literal markup and line separators stay escaped, and raw JSON text is
    # NOT claimed to be safe to paste inside HTML (a later 4d boundary).
    assert "\\\"quoted\\\"" in text
    assert "\\\\backslash\\\\" in text
    assert "\\ud83d\\ude00" in text and "😀" not in text
    assert "\\u2028" in text and "\\u2029" in text
    assert "\u2028" not in text and "\u2029" not in text
    loaded = catalog.load_snapshot(text)
    model = loaded["providers"]["p"]["models"]["known"]
    assert model["description"] == hostile
    assert loaded["providers"]["p"]["display_name"] == "Provider P"
    assert loaded["providers"]["p"]["chat_completions_url"] == \
        wiring["p"]["chat_completions_url"]
    assert loaded["snapshot_id"] == snapshot["snapshot_id"]
    # A known empty [] stays distinguishable from unknown null.
    assert model["architecture"] == {"input_modalities": [],
                                     "output_modalities": []}
    assert model["supported_parameters"] == []
    unknown = loaded["providers"]["p"]["models"]["unknown"]
    assert unknown["architecture"] == {"input_modalities": None,
                                       "output_modalities": None}
    assert unknown["supported_parameters"] is None

    # (d) Typed pricing controls: integer 1 vs float 1.0 vs a numeric string
    # are distinct ids AND distinct export text, each independently valid.
    def pricing_snapshot(prompt, completion):
        registry = [_record("p")]
        entry = {"tick_epoch": 0, "providers": {"p": ["m"]},
                 "provider_models": {"p": {"m": {
                     "name": "M", "context_length": 8,
                     "architecture": {"input_modalities": ["text"],
                                      "output_modalities": ["text"]},
                     "supported_parameters": ["temperature"],
                     "pricing": {"prompt": prompt,
                                 "completion": completion}}}}}
        return build_snapshot(registry, entry, cadence_s=1)

    int_snapshot = pricing_snapshot(1, 0)
    float_snapshot = pricing_snapshot(1.0, 0.5)
    string_snapshot = pricing_snapshot("1.0", "0")
    for snapshot in (int_snapshot, float_snapshot, string_snapshot):
        assert snapshot["snapshot_id"] == _independent_snapshot_id(snapshot)
        assert catalog.validate_snapshot(snapshot) is snapshot
        assert serialize_snapshot(snapshot) == \
            _reference_export_text(snapshot)
    ids = {int_snapshot["snapshot_id"], float_snapshot["snapshot_id"],
           string_snapshot["snapshot_id"]}
    assert len(ids) == 3
    int_text = serialize_snapshot(int_snapshot)
    float_text = serialize_snapshot(float_snapshot)
    string_text = serialize_snapshot(string_snapshot)
    assert '"prompt":1},"supported_parameters"' in int_text
    assert '"prompt":1.0},"supported_parameters"' in float_text
    assert '"prompt":"1.0"},"supported_parameters"' in string_text
    int_prompt = catalog.load_snapshot(int_text)[
        "providers"]["p"]["models"]["m"]["pricing"]["prompt"]
    float_prompt = catalog.load_snapshot(float_text)[
        "providers"]["p"]["models"]["m"]["pricing"]["prompt"]
    string_prompt = catalog.load_snapshot(string_text)[
        "providers"]["p"]["models"]["m"]["pricing"]["prompt"]
    assert type(int_prompt) is int and int_prompt == 1
    assert type(float_prompt) is float and float_prompt == 1.0
    assert type(string_prompt) is str and string_prompt == "1.0"

    # (e) Rejection controls delegate to the accepted validator: exact frozen
    # code/message, no input mutation, no repaired output. The shaped native
    # case would raise an incidental ``TypeError`` from the encoder if
    # validation were skipped, so validator-before-encoder is observable.
    good = _load_fixture("basic.json")
    first_key = good["provider_order"][0]

    missing = copy.deepcopy(good)
    del missing["warnings"]
    extra = copy.deepcopy(good)
    extra["unexpected"] = "x"
    version_type = copy.deepcopy(good)
    version_type["schema_version"] = "1"
    unsupported = copy.deepcopy(good)
    unsupported["schema_version"] = 2
    bad_type = copy.deepcopy(good)
    bad_type["providers"][first_key]["stale"] = "yes"
    bad_hash_format = copy.deepcopy(good)
    bad_hash_format["snapshot_id"] = "A" * 64
    stale_hash = copy.deepcopy(good)
    stale_hash["providers"][first_key]["display_name"] = "Renamed"
    shaped_native = copy.deepcopy(good)
    shaped_native["tick_epoch"] = object()

    rejects = [
        (missing, INVALID_SNAPSHOT),
        (extra, INVALID_SNAPSHOT),
        (version_type, INVALID_SNAPSHOT),
        (unsupported, "unsupported_schema_version"),
        (bad_type, INVALID_SNAPSHOT),
        (bad_hash_format, INVALID_SNAPSHOT),
        (stale_hash, INVALID_SNAPSHOT),
        (shaped_native, INVALID_SNAPSHOT),
    ]
    for case, code in rejects:
        before = copy.deepcopy(case)
        with pytest.raises(catalog.CatalogError) as excinfo:
            serialize_snapshot(case)
        assert excinfo.value.code == code
        assert excinfo.value.message == messages[code]
        assert _same_inputs(case, before)

    # Non-object roots keep the validator's invalid_snapshot behaviour.
    for root in ([], (), "", 0, 3.5, True, None,
                 [good], ("providers",)):
        with pytest.raises(catalog.CatalogError) as excinfo:
            serialize_snapshot(root)
        assert excinfo.value.code == INVALID_SNAPSHOT, root
        assert excinfo.value.message == INVALID_SNAPSHOT_MESSAGE, root

    # Nothing was repaired: the hostile-renamed snapshot still carries the
    # changed value, and the untouched fixture still exports normally.
    assert stale_hash["providers"][first_key]["display_name"] == "Renamed"
    assert serialize_snapshot(good) == _reference_export_text(good)
