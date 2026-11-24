"""Native runtime tests for the pure catalogue core.

The shared fixture corpora -- raw JSON acceptance, typed canonical bytes,
structural validation, metadata projection, marker stripping, natural ordering,
the argument table and the shared query goldens -- are owned by
``tests/test_contract_parity.py``, which executes every row against independent
literal expectations in BOTH implementations (the Python reference and the
dependency-free Node module). This module no longer re-reads or re-asserts those
tables.

What remains here is what the parity lane cannot express: Python host-object
behaviour (non-JSON containers, ``object()`` sentinels, ``TypeError``
programming errors, ``sys`` limit side effects), the exact Python
exception/envelope surface, this module's own import-isolation audit, and
literal characterizations of Python-regex/ordering semantics that are not
table-driven.
"""

import collections
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from watchdog_catalog import catalog as _catalog

REPO = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"

INVALID_JSON = "invalid_json"
INVALID_JSON_MESSAGE = "Invalid catalogue JSON."
INVALID_SNAPSHOT = "invalid_snapshot"
UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
STRUCTURAL_MESSAGES = {
    INVALID_SNAPSHOT: "Invalid catalogue snapshot.",
    UNSUPPORTED_SCHEMA_VERSION: "Unsupported catalogue schema version.",
}


# -- literal depth builders (grouped rows; not one helper per row) --

def _nested_array(n):
    return "[" * n + "0" + "]" * n


def _nested_object(n):
    return '{"a":' * n + "0" + "}" * n


def _nested_mixed(n):
    opens, closes = [], []
    for i in range(n):
        if i % 2 == 0:
            opens.append("[")
            closes.append("]")
        else:
            opens.append('{"a":')
            closes.append("}")
    return "".join(opens) + "0" + "".join(reversed(closes))


def _assert_invalid(text, cid=None):
    catalog = _catalog
    with pytest.raises(catalog.CatalogError) as excinfo:
        catalog.load_json(text)
    err = excinfo.value
    assert err.code == INVALID_JSON, cid or text[:40]
    assert err.message == INVALID_JSON_MESSAGE, cid or text[:40]
    return err


# ---------- raw boundary: native precedence and numeric policy ----------


def test_raw_numeric_safety_and_duplicate_policy_are_notation_independent():
    catalog = _catalog
    load_json = catalog.load_json
    assert catalog.MAX_SAFE_INT == 9007199254740991
    # The safe range is inclusive at both ends.
    assert load_json("9007199254740991") == catalog.MAX_SAFE_INT
    assert load_json("-9007199254740991") == -catalog.MAX_SAFE_INT
    # Unsafe integers reject in EVERY notation, at every depth.
    for text in ("9007199254740992", "-9007199254740992",
                 "9007199254740992.0", "-9007199254740992.0",
                 "1e16", "-1e16", '{"a": [{"b": 9007199254740993}]}'):
        _assert_invalid(text)
    # Non-finite constants and overflowing numbers reject.
    for text in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400"):
        _assert_invalid(text)
    # Numeric-looking strings are untouched, never inspected as numbers.
    assert load_json('"9007199254740992"') == "9007199254740992"
    assert load_json('{"n": "NaN"}') == {"n": "NaN"}
    # A duplicate member name rejects at EVERY object level, including
    # decoded escape-equivalent keys.
    for text in ('{"a":1,"a":2}', '{"a":1,"\\u0061":2}',
                 '{"x":{"b":1,"b":2}}', '[{"c":1,"c":1}]',
                 '{"a":1,"b":{"c":1,"c":2}}'):
        _assert_invalid(text)
    # Different case is distinct; a name repeated in separate objects is fine.
    assert load_json('{"A": 1, "a": 2}') == {"A": 1, "a": 2}
    assert load_json('{"x":{"a":1},"y":{"a":2}}') == \
        {"x": {"a": 1}, "y": {"a": 2}}
    # Ordinary positive control.
    assert load_json('{"a": 0.5, "b": [1, 2.5]}') == {"a": 0.5, "b": [1, 2.5]}


def test_raw_container_depth_bound_is_inclusive_and_huge_depth_rejected():
    catalog = _catalog
    load_json = catalog.load_json
    assert catalog.MAX_JSON_DEPTH == 64
    for build in (_nested_array, _nested_object, _nested_mixed):
        load_json(build(64))  # accepted at the inclusive bound
        _assert_invalid(build(65))
    # Quoted brackets never count; a root scalar is depth 0.
    quoted = '"' + "[" * 70 + '"'
    assert load_json(quoted) == "[" * 70
    assert load_json("42") == 42
    # A 5000-deep document rejects through the preflight without touching the
    # interpreter recursion limit.
    before = sys.getrecursionlimit()
    _assert_invalid("[" * 5000 + "0" + "]" * 5000)
    assert sys.getrecursionlimit() == before


def test_raw_huge_integer_lexeme_rejects_without_changing_int_limits():
    load_json = _catalog.load_json
    huge = "9" * 5000
    int_before = sys.get_int_max_str_digits()
    rec_before = sys.getrecursionlimit()
    for text in (huge, "-" + huge, '{"a": [' + huge + "]}", "1" + "0" * 5000):
        _assert_invalid(text)
    # No sys.set_int_max_str_digits / recursion-limit change was made.
    assert sys.get_int_max_str_digits() == int_before
    assert sys.getrecursionlimit() == rec_before
    # Ordinary integers still load.
    assert load_json("9007199254740991") == 9007199254740991
    assert load_json("-12345") == -12345


# ---------- typed canonical bytes: native encoder properties ----------


def test_canonical_bytes_encoder_flags_and_no_key_exclusion():
    catalog = _catalog
    canonical_bytes = catalog.canonical_bytes
    # The documented public names hang off the submodule exactly as imported.
    assert catalog.load_json is catalog.load_json
    assert catalog.canonical_bytes is canonical_bytes
    # Compact separators and code-point key sorting.
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    # ensure_ascii=True escapes non-ASCII.
    assert canonical_bytes({"s": "caf\u00e9"}) == b'{"s":"caf\\u00e9"}'
    # The generic encoder excludes NO keys, including a root snapshot_id.
    assert b'"snapshot_id"' in canonical_bytes({"snapshot_id": "deadbeef", "x": 1})
    # Native non-finite Python inputs are rejected, never emitted as bare NaN.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_bytes({"x": bad})


# ---------- structural validation: literal table rows and fixtures ----------

# Small TEST-LOCAL change applier + reseal helper. Independent stdlib only
# (json/hashlib); no test-local reference helper is shared and no production
# module is used to build the expected value.


def _decode_doc(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _canonical_text(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _reseal(snap):
    """Reseal a mutated in-memory copy: `snapshot_id` becomes the canonical
    body hash, so hash equality is TRUE before the semantic defect is tested."""
    body = {k: v for k, v in snap.items() if k != "snapshot_id"}
    snap["snapshot_id"] = hashlib.sha256(_canonical_text(body)).hexdigest()
    return snap


def _mutate(base, path, value):
    """A fresh copy of `base` with one literal slot replaced."""
    snap = json.loads(json.dumps(base))
    parent = snap
    for component in path[:-1]:
        parent = parent[component]
    parent[path[-1]] = value
    return snap


def _assert_rejected(snapshot, code, cid):
    catalog = _catalog
    with pytest.raises(catalog.CatalogError) as excinfo:
        catalog.validate_snapshot(snapshot)
    err = excinfo.value
    expect = {"ok": False,
              "error": {"code": code, "message": STRUCTURAL_MESSAGES[code]}}
    assert err.code == code, cid
    assert err.message == STRUCTURAL_MESSAGES[code], cid
    assert str(err) == STRUCTURAL_MESSAGES[code], cid
    assert err.to_payload() == expect, cid
    payload = err.to_payload()
    assert set(payload) == {"ok", "error"}, cid
    assert payload["ok"] is False, cid
    assert set(payload["error"]) == {"code", "message"}, cid
    assert err.code != INVALID_JSON, cid
    return err


def test_structural_error_literals_and_envelopes_are_frozen():
    catalog = _catalog
    assert catalog.INVALID_SNAPSHOT == INVALID_SNAPSHOT
    assert catalog.UNSUPPORTED_SCHEMA_VERSION == UNSUPPORTED_SCHEMA_VERSION
    assert catalog.SCHEMA_VERSION == 2
    for code, message in STRUCTURAL_MESSAGES.items():
        err = catalog.CatalogError(code)
        assert err.code == code
        assert err.message == message
        assert str(err) == message
        payload = err.to_payload()
        assert payload == {"ok": False,
                           "error": {"code": code, "message": message}}
        assert set(payload) == {"ok", "error"}
        # Never an argument-style `parameter`, never a raw value or path.
        assert set(payload["error"]) == {"code", "message"}
        assert payload["ok"] is False
        assert err.to_payload() is not err.to_payload()
    # The DEFAULT code stays the raw one, so the 2a1 surface is unchanged.
    assert catalog.CatalogError().code == INVALID_JSON


def test_raw_faults_precede_structural_validation():
    """The composition order, pinned on ONE literal document: a raw lexical
    fault is reported as invalid_json before the structural layer runs. The raw
    and structural tables themselves are exercised row by row in
    ``tests/test_contract_parity.py``."""
    catalog = _catalog
    with pytest.raises(catalog.CatalogError) as excinfo:
        catalog.load_snapshot('{"schema_version": 2,')
    assert excinfo.value.code == INVALID_JSON
    # A lexically valid non-object root reaches (and fails) the structural
    # layer, so no raw fault can mask it.
    for text in ("null", "[]", "true", "42", "1.5", '"x"', "{}"):
        _assert_rejected(catalog.load_json(text), INVALID_SNAPSHOT, text)


def test_canonical_snapshot_rows_carry_their_declared_body_seal():
    """Seal precondition for the canonical table's snapshot rows: every row
    except the intentional tamper is otherwise VALID, so a rejection there is
    attributable to the strict-token rule and not to a stale fixture seal. The
    rejection envelopes themselves stay parity's two-engine payload oracle."""
    rows = _decode_doc(FIXTURE_DIR / "canonical-cases.json")["snapshot_cases"]
    assert rows
    for row in rows:
        cid = row["id"]
        decoded = json.loads(row["json_text"])
        body = {k: v for k, v in decoded.items() if k != "snapshot_id"}
        seal = hashlib.sha256(_canonical_text(body)).hexdigest()
        if cid == "snap_case_hash_tamper":
            assert decoded["snapshot_id"] != seal, cid
        else:
            assert decoded["snapshot_id"] == seal, cid


def test_structural_precedence_matrix_on_native_values():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")

    # (a) Non-object roots, including tuple/bytes/UserDict, never escape.
    for root in (None, [], "x", 1, 1.5, True, (), b"{}",
                 collections.UserDict(base)):
        _assert_rejected(root, INVALID_SNAPSHOT, f"root {root!r}")

    # (b) Wrong required-field set both ways.
    for mutate in (lambda s: s.pop("cadence_s"),
                   lambda s: s.__setitem__("surprise", 1),
                   lambda s: s.pop("snapshot_id")):
        snap = json.loads(json.dumps(base))
        mutate(snap)
        _assert_rejected(snap, INVALID_SNAPSHOT, "field set")

    # (c) A non-strict-integer version TYPE is invalid_snapshot.
    for value in (True, False, "1", None, 1.0, 1e0):
        _assert_rejected(_mutate(base, ("schema_version",), value),
                         INVALID_SNAPSHOT, f"version type {value!r}")

    # (d) A well-typed integer version != 2 is unsupported_schema_version EVEN
    #     with a malformed body and a wrong hash: the value check precedes
    #     every v2 body check and the hash.
    for value in (3, 1, 0, -1, catalog.MAX_SAFE_INT):
        snap = _mutate(base, ("schema_version",), value)
        snap["providers"] = []
        snap["snapshot_id"] = "0" * 64
        _assert_rejected(snap, UNSUPPORTED_SCHEMA_VERSION, f"version {value}")

    # (e) Version 2: a malformed body, a wrong 64-hex hash and a malformed hash
    #     are each invalid_snapshot.
    _assert_rejected(_mutate(base, ("providers",), []),
                     INVALID_SNAPSHOT, "malformed body")
    _assert_rejected(_mutate(base, ("snapshot_id",), "0" * 64),
                     INVALID_SNAPSHOT, "wrong 64-hex hash")
    _assert_rejected(_mutate(base, ("snapshot_id",), "not-a-hash"),
                     INVALID_SNAPSHOT, "malformed hash")
    _assert_rejected(_mutate(base, ("provider_order",), ["synthetic-a"]),
                     INVALID_SNAPSHOT, "order/map mismatch")

    # (f) Accepting control: the untouched snapshot is returned BY IDENTITY.
    assert catalog.validate_snapshot(base) is base


def test_structural_native_body_guards_reject_without_incidental_errors():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")
    model = ("providers", "synthetic-a", "models", "vendor/shared")
    cases = (
        (("providers",), []),
        (("providers", "synthetic-a"), "oops"),
        ((model[0], model[1], "models"), []),
        (model + ("architecture",), []),
        (("warnings",), {}),
        (("providers",), {1: {}}),
        (("providers", "synthetic-a", "models"), {7: {}}),
        (("tick_epoch",), float("nan")),
        (("tick_epoch",), float("inf")),
        (("tick_epoch",), catalog.MAX_SAFE_INT + 1),
        (("cadence_s",), float("nan")),
        (("cadence_s",), 0),
        (("cadence_s",), catalog.MAX_SAFE_INT + 1),
        (("cadence_s",), 2 ** 200),
        (("cadence_s",), 3600.0),
        (model + ("context_length",), float("nan")),
        (model + ("context_length",), catalog.MAX_SAFE_INT + 1),
        (model + ("context_length",), True),
    )
    for path, value in cases:
        snap = _mutate(base, path, value)
        # Reseal so the hash is consistent and ONLY the body guard can reject:
        # a stale snapshot_id would let a dropped numeric bound hide behind it.
        _reseal(snap)
        _assert_rejected(snap, INVALID_SNAPSHOT, f"{path} = {value!r}")

    # A non-JSON native in an otherwise-shaped slot rejects as invalid_snapshot
    # and is left EXACTLY as supplied -- never repaired, coerced or replaced.
    for field in ("providers", "warnings", "tick_epoch", "cadence_s",
                  "provider_order", "snapshot_id"):
        marker = object()
        snap = json.loads(json.dumps(base))
        snap[field] = marker
        _assert_rejected(snap, INVALID_SNAPSHOT, f"{field} = object()")
        assert snap[field] is marker, field


def test_structural_generic_registry_shapes_and_identifiers_are_accepted():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")

    # (a) Identifier namespaces: a literal `auth` provider key and literal
    #     `__proto__`/`constructor`/`auth` raw tracked ids are LEGAL.
    snap = json.loads(json.dumps(base))
    entry = snap["providers"].pop("synthetic-b")
    for index, raw_id in enumerate(list(entry["model_ids"])):
        replacement = ("__proto__", "constructor")[index]
        model = entry["models"].pop(raw_id)
        model["id"] = replacement
        entry["models"][replacement] = model
    entry["model_ids"] = sorted(entry["models"])
    snap["providers"]["auth"] = entry
    snap["provider_order"] = ["synthetic-a", "auth", "synthetic-c"]
    _reseal(snap)
    assert catalog.validate_snapshot(snap) is snap
    assert snap["providers"]["auth"]["model_ids"] == ["__proto__", "constructor"]

    # (b) A legitimately empty registry.
    snap = json.loads(json.dumps(base))
    snap["provider_order"] = []
    snap["providers"] = {}
    _reseal(snap)
    assert catalog.validate_snapshot(snap) is snap

    # (c) A registry in which every provider tracks at least one id.
    snap = json.loads(json.dumps(base))
    del snap["providers"]["synthetic-c"]
    snap["provider_order"] = ["synthetic-a", "synthetic-b"]
    _reseal(snap)
    assert catalog.validate_snapshot(snap) is snap


def test_structural_wiring_is_syntactic_and_preserved_verbatim():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")
    path = ("providers", "synthetic-a", "chat_completions_url")
    accepted = (
        "https://host.example/v1/chat/completions?x=1",
        "https://host.example/v1/chat/completions?",
        "http://[::1]:8000/v1/chat/completions",
        "http://[::1]/v1/chat/completions",
        "HTTP://EXAMPLE.COM/v1/chat/completions",
        "https://host.example/v1/chat/completions#",
    )
    for url in accepted:
        snap = _mutate(base, path, url)
        _reseal(snap)
        assert catalog.validate_snapshot(snap) is snap, url
        # The ORIGINAL string is preserved verbatim -- never normalized.
        assert snap["providers"]["synthetic-a"]["chat_completions_url"] == url
    rejected = (
        "", "   https://host.example/v1", "https://host.example/v1 ",
        "https://host.example/v1\n", "https://ho\tst.example/v1",
        "https://host.example/v1\x7f", "/v1/chat/completions",
        "ftp://host.example/v1", "https://user@host.example/v1",
        "https://user:pass@host.example/v1", "https://host.example/v1#frag",
        "https://host.example/v1#%20", "http://::1/v1", 5, True, None,
    )
    for url in rejected:
        snap = _mutate(base, path, url)
        _reseal(snap)
        _assert_rejected(snap, INVALID_SNAPSHOT, f"wiring {url!r}")


def test_snapshot_hash_excludes_only_the_root_id_and_rejects_non_mappings():
    """`snapshot_hash` excludes exactly the ROOT ``snapshot_id``: a nested one
    is ordinary content and stays bound into the digest. The digest vectors
    themselves are executed by ``tests/test_contract_parity.py``."""
    catalog = _catalog
    nested = catalog.load_json(
        '{"snapshot_id": "a", "inner": {"snapshot_id": "cafe"}}')
    assert catalog.snapshot_hash(nested) == hashlib.sha256(
        b'{"inner":{"snapshot_id":"cafe"}}').hexdigest()
    assert catalog.snapshot_hash(nested) != catalog.snapshot_hash(
        {"inner": {}})
    # Sealing then setting the root id is not circular.
    body: dict = {"inner": {"snapshot_id": "cafe"}}
    sealed = dict(body)
    sealed["snapshot_id"] = catalog.snapshot_hash(body)
    assert catalog.snapshot_hash(sealed) == catalog.snapshot_hash(body)
    # A non-dict argument is a programming error, never a public code.
    for bad in ([], "x", None, 1, b"{}"):
        with pytest.raises(TypeError):
            catalog.snapshot_hash(bad)


# ---------- optional capability projection + separate normalization ----------
#
# `project_capabilities` (raw upstream fragment -> public capability shape +
# warnings) and `normalize_capabilities` (valid public capability projection ->
# query shape) are exercised row by row against ``metadata-normalization.json``
# in ``tests/test_contract_parity.py``. This module keeps only the native
# semantics and the literal edges either lane can express.

PROJECTED_CAPABILITY_FIELDS = frozenset(
    {"context_length", "architecture", "supported_parameters"})
PROJECTED_ARCHITECTURE_FIELDS = frozenset(
    {"input_modalities", "output_modalities"})


def test_normalize_capabilities_accepts_a_full_model_projection():
    catalog = _catalog
    # A valid full public MODEL (extra identity/display members) is a
    # valid projection input and those extra members are ignored.
    model = {
        "id": "vendor/example",
        "name": "Example",
        "description": "ignored",
        "context_length": 4096,
        "architecture": {"input_modalities": ["TEXT"],
                         "output_modalities": None},
        "supported_parameters": ["Temperature"],
    }
    before = copy.deepcopy(model)
    assert catalog.normalize_capabilities(model) == {
        "context_length": 4096,
        "input_modalities": ["text"],
        "output_modalities": None,
        "supported_parameters": ["temperature"],
    }
    assert model == before


def test_capability_projection_edges_distinguish_unknown_from_malformed():
    catalog = _catalog
    project = catalog.project_capabilities
    # An absent OR explicit-null architecture PARENT is unknown
    # (metadata_missing); a malformed non-null parent is metadata_invalid.
    # Either way BOTH children project to null.
    for raw in (
            {"context_length": 4096, "supported_parameters": ["temperature"]},
            {"context_length": 4096, "architecture": None,
             "supported_parameters": ["temperature"]}):
        projected, warnings = project(raw)
        assert projected["architecture"] == {
            "input_modalities": None, "output_modalities": None}
        assert warnings == ["metadata_missing"]
    projected, warnings = project(
        {"context_length": 4096, "architecture": ["text"],
         "supported_parameters": ["temperature"]})
    assert projected["architecture"] == {
        "input_modalities": None, "output_modalities": None}
    assert warnings == ["metadata_invalid"]
    # A float context (1.0) is malformed, never coerced.
    projected, warnings = project(
        {"context_length": 1.0,
         "architecture": {"input_modalities": [], "output_modalities": ["text"]},
         "supported_parameters": []})
    assert projected["context_length"] is None
    assert projected["architecture"]["input_modalities"] == []
    assert projected["architecture"]["output_modalities"] == ["text"]
    assert projected["supported_parameters"] == []
    assert warnings == ["metadata_invalid"]
    # Unknown (null) and known-empty ([]) stay distinct: a null stays
    # unknown/null, and a known zero context is KNOWN.
    normalized = catalog.normalize_capabilities(
        {"context_length": 0,
         "architecture": {"input_modalities": [],
                          "output_modalities": None},
         "supported_parameters": []})
    assert normalized["context_length"] == 0
    assert normalized["input_modalities"] == []
    assert normalized["output_modalities"] is None
    assert set(normalized) == {
        "context_length", "input_modalities", "output_modalities",
        "supported_parameters"}
    # A malformed array is rejected WHOLE, never partially salvaged.
    projected, _ = project(
        {"context_length": 4096,
         "architecture": {"input_modalities": ["text", 5],
                          "output_modalities": ["text"]},
         "supported_parameters": ["temperature"]})
    assert projected["architecture"]["input_modalities"] is None
    assert projected["architecture"]["output_modalities"] == ["text"]
    # Whitespace tokens are valid and untrimmed; non-ASCII case pairs stay
    # literal (never Unicode-folded) in the public projection.
    projected, warnings = project(
        {"context_length": 4096,
         "architecture": {"input_modalities": ["\u00c9", "\u00e9"],
                          "output_modalities": [" Text "]},
         "supported_parameters": [" TEXT ", " text "]})
    assert warnings == []
    assert projected["architecture"]["input_modalities"] == ["\u00c9", "\u00e9"]
    assert projected["architecture"]["output_modalities"] == [" Text "]
    # Extra upstream members (including a private-looking one) are ignored and
    # never serialized into the projection.
    projected, warnings = project(
        {"context_length": 4096, "api_key": "secret",
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["text"], "extra": 1},
         "supported_parameters": ["temperature"], "note": "ignored"})
    assert set(projected) == PROJECTED_CAPABILITY_FIELDS
    assert set(projected["architecture"]) == PROJECTED_ARCHITECTURE_FIELDS
    assert warnings == []
    # Cross-field warning order and dedup: metadata_missing precedes
    # metadata_invalid and each code appears once.
    _, warnings = project(
        {"context_length": None,
         "architecture": {"input_modalities": "text",
                          "output_modalities": None},
         "supported_parameters": "temperature"})
    assert warnings == ["metadata_missing", "metadata_invalid"]
    # A non-mapping raw is a programming-error TypeError, never a public code.
    for bad in ([], "x", None, 1, b"{}", ("t",)):
        with pytest.raises(TypeError):
            project(bad)


def test_capability_functions_never_alias_or_mutate_their_inputs():
    catalog = _catalog
    shared = ["temperature"]
    raw = {
        "context_length": 4096,
        "architecture": {"input_modalities": shared,
                         "output_modalities": shared},
        "supported_parameters": shared,
    }
    projected, warnings = catalog.project_capabilities(raw)
    # Every retained array is a NEW list, and the two architecture children are
    # not the same object even when upstream shared one.
    assert projected["architecture"]["input_modalities"] is not shared
    assert projected["supported_parameters"] is not shared
    assert projected["architecture"]["input_modalities"] is not \
        projected["architecture"]["output_modalities"]
    projected["architecture"]["input_modalities"].append("vision")
    projected["supported_parameters"].append("seed")
    warnings.append("metadata_invalid")
    assert raw == {
        "context_length": 4096,
        "architecture": {"input_modalities": ["temperature"],
                         "output_modalities": ["temperature"]},
        "supported_parameters": ["temperature"],
    }
    # A later call is unaffected by mutating an earlier result.
    projected2, warnings2 = catalog.project_capabilities(raw)
    assert projected2["architecture"]["input_modalities"] == ["temperature"]
    assert warnings2 == []
    # Normalization returns new lists; no unknown-field list is reported.
    normalized = catalog.normalize_capabilities(projected2)
    normalized["input_modalities"].append("audio")
    again = catalog.normalize_capabilities(projected2)
    assert again["input_modalities"] == ["temperature"]
    assert "unknown_fields" not in again
    assert projected2["architecture"]["input_modalities"] == ["temperature"]
    assert projected2["supported_parameters"] == ["temperature"]


# ---------- import isolation: silent, side-effect-free package root ----------

# Application/operator modules a fresh import of the package must never pull in,
# and the application data files it must never open. Shared by both fresh-process
# audits below so the two import ORDERS are audited against one literal list.
FORBIDDEN_IMPORTS = [
    "mcp", "mcp_server", "build_site", "state", "config_loader",
    "providers", "inference_watchdog", "notify", "envfile", "diffing",
    "detection", "alive", "pending_removals", "probe_select",
    "probe_state", "probe_zero_credit", "anthropic", "httpx", "requests",
    "watchdog_catalog.query", "watchdog_catalog.server",
    "watchdog_catalog.adapter",
]
DATA_PATTERNS = ["/.env", "roster", "tools.json", "catalog.json",
                 "/state/", "/providers/"]
# Package-first and catalog-first, each in its OWN fresh process: the audit must
# not depend on which entry point a consumer reaches the package through.
IMPORT_ORDERS = (
    ["watchdog_catalog", "watchdog_catalog.catalog"],
    ["watchdog_catalog.catalog", "watchdog_catalog"],
)
# The pure query module is a second, EXPLICIT entry point: importing it directly
# is allowed, so for its two orders ONLY "watchdog_catalog.query" is dropped from
# the forbidden set (the requested module itself). Every other prefix, data-file
# and socket rule stays identical, and the original package/catalog orders above
# keep the full list — including the query entry.
QUERY_IMPORT_ORDERS = (
    ["watchdog_catalog", "watchdog_catalog.query"],
    ["watchdog_catalog.query", "watchdog_catalog"],
)

_PROBE = r'''
import importlib, json, os, sys

forbidden_prefixes = json.loads(os.environ["WC_FORBIDDEN"])
data_patterns = json.loads(os.environ["WC_DATA_PATTERNS"])
import_order = json.loads(os.environ.get(
    "WC_IMPORT_ORDER", '["watchdog_catalog", "watchdog_catalog.catalog"]'))

forbidden_imports = []
application_opens = []
network_events = []


def _audit(name, args):
    if name == "import":
        module = args[0]
        for prefix in forbidden_prefixes:
            if module == prefix or module.startswith(prefix + "."):
                forbidden_imports.append(module)
                return
    elif name == "open":
        path = args[0]
        if isinstance(path, str):
            norm = path.replace("\\", "/")
            for pattern in data_patterns:
                if pattern in norm:
                    application_opens.append(norm)
                    return
    elif name.startswith("socket."):
        network_events.append(name)


sys.addaudithook(_audit)

for module_name in import_order:
    importlib.import_module(module_name)

# Root plus every explicitly requested entry point is resident; the query-only
# orders do not require an irrelevant eager catalog import, while the original
# package/catalog orders still assert catalog presence.
assert "watchdog_catalog" in sys.modules
for module_name in import_order:
    assert module_name in sys.modules, module_name

# Belt-and-suspenders: no forbidden module is resident, whether freshly
# imported or already pre-loaded before the audit hook was installed.
leaked_modules = [
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".")
           for prefix in forbidden_prefixes)
]

sys.stdout.write(json.dumps({
    "forbidden_imports": forbidden_imports,
    "leaked_modules": leaked_modules,
    "application_opens": application_opens,
    "network_events": network_events,
}))
'''


def test_package_import_is_silent_and_side_effect_free(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "neutral-cwd"
    cwd.mkdir()
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # (a) A plain import from a neutral cwd and clean HOME is completely silent.
    plain = subprocess.run(
        [sys.executable, "-B", "-c",
         "import watchdog_catalog, watchdog_catalog.catalog"],
        cwd=str(cwd), env=base_env, capture_output=True, text=True, timeout=120)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == "", plain.stdout
    assert plain.stderr == "", plain.stderr

    # (b) An audit-hooked import touches no operator module, application data
    #     file or network facility. Mandatory module source/bytecode reads are
    #     allowed; only application data and operator/SDK imports are trapped.
    env = dict(base_env)
    env["WC_FORBIDDEN"] = json.dumps(FORBIDDEN_IMPORTS)
    env["WC_DATA_PATTERNS"] = json.dumps(DATA_PATTERNS)
    probe = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr
    report = json.loads(probe.stdout)
    assert report["forbidden_imports"] == [], report["forbidden_imports"]
    assert report["leaked_modules"] == [], report["leaked_modules"]
    assert report["application_opens"] == [], report["application_opens"]
    assert report["network_events"] == [], report["network_events"]


def test_package_import_audit_is_import_order_independent(tmp_path):
    """The audit holds for BOTH fresh-process package/catalog entry points —
    importing the submodule directly (catalog-first) and importing the package
    first — and for the separate, EXPLICIT query entry point. A lazy/ordered
    import must never become the way a forbidden module slips in."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "neutral-cwd"
    cwd.mkdir()
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for order in IMPORT_ORDERS:
        env = dict(base_env)
        env["WC_FORBIDDEN"] = json.dumps(FORBIDDEN_IMPORTS)
        env["WC_DATA_PATTERNS"] = json.dumps(DATA_PATTERNS)
        env["WC_IMPORT_ORDER"] = json.dumps(order)
        probe = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
        assert probe.returncode == 0, (order, probe.stderr)
        assert probe.stderr == "", (order, probe.stderr)
        report = json.loads(probe.stdout)
        assert report["forbidden_imports"] == [], (order, report)
        assert report["leaked_modules"] == [], (order, report)
        assert report["application_opens"] == [], (order, report)
        assert report["network_events"] == [], (order, report)

    # The query module is the requested module in its own two orders, so ONLY its
    # own name is dropped from the forbidden set; the original package/catalog
    # orders above still forbid it, and every other rule stays identical.
    assert "watchdog_catalog.query" in FORBIDDEN_IMPORTS
    query_forbidden = [p for p in FORBIDDEN_IMPORTS
                       if p != "watchdog_catalog.query"]
    for order in QUERY_IMPORT_ORDERS:
        env = dict(base_env)
        env["WC_FORBIDDEN"] = json.dumps(query_forbidden)
        env["WC_DATA_PATTERNS"] = json.dumps(DATA_PATTERNS)
        env["WC_IMPORT_ORDER"] = json.dumps(order)
        probe = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
        assert probe.returncode == 0, (order, probe.stderr)
        assert probe.stderr == "", (order, probe.stderr)
        report = json.loads(probe.stdout)
        assert report["forbidden_imports"] == [], (order, report)
        assert report["leaked_modules"] == [], (order, report)
        assert report["application_opens"] == [], (order, report)
        assert report["network_events"] == [], (order, report)

    # A plain explicit query import from a neutral cwd + clean HOME is silent.
    plain = subprocess.run(
        [sys.executable, "-B", "-c", "import watchdog_catalog.query"],
        cwd=str(cwd), env=base_env, capture_output=True, text=True, timeout=120)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == "", plain.stdout
    assert plain.stderr == "", plain.stderr


# ---------- full public model projection ----------
#
# `project_model` composes the accepted capability projection with the two
# identity/display members into ONE complete public model object. Every
# expected value below is an INDEPENDENT literal restatement of the frozen
# contract (the README projected-model field table), never derived from a
# runtime helper.

PUBLIC_MODEL_FIELDS = frozenset(
    {"id", "name", "description", "context_length", "architecture",
     "supported_parameters"})
PUBLIC_ARCHITECTURE_FIELDS = frozenset(
    {"input_modalities", "output_modalities"})

# A complete, all-known upstream fragment used as the positive control.
_KNOWN_FRAGMENT = {
    "name": "Vendor Known",
    "description": "A known model.",
    "context_length": 4096,
    "architecture": {"input_modalities": ["text"],
                     "output_modalities": ["text"]},
    "supported_parameters": ["temperature"],
}


def _known_fragment():
    return json.loads(json.dumps(_KNOWN_FRAGMENT))


def _fragment(**overrides):
    raw = _known_fragment()
    raw.update(overrides)
    return raw


def _fragment_without(*keys):
    raw = _known_fragment()
    for key in keys:
        raw.pop(key, None)
    return raw


def test_project_model_identity_name_description_and_raw_degradation():
    catalog = _catalog
    project = catalog.project_model

    # (a) Tracked-key authority: a mismatched, non-string or absent upstream
    #     `id` is ignored and contributes NO warning.
    for upstream_id in ("upstream/other", 5, None, ["x"]):
        model, warnings = project("vendor/key", _fragment(id=upstream_id))
        assert model["id"] == "vendor/key", upstream_id
        assert warnings == [], upstream_id
    model, warnings = project("vendor/key", _fragment_without("id"))
    assert model["id"] == "vendor/key"
    assert warnings == []

    # (b) Always exactly the six public fields.
    model, _ = project("vendor/key", _known_fragment())
    assert set(model) == PUBLIC_MODEL_FIELDS
    assert set(model["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS

    # (c) `name`: the upstream value iff a NONEMPTY string (verbatim, including
    #     whitespace and non-ASCII); otherwise the tracked key.
    assert project("k", _fragment(name="  spaced  "))[0]["name"] == "  spaced  "
    assert project("k", _fragment(name="\u03a9"))[0]["name"] == "\u03a9"
    for bad in (None, "", True, False, 0, 5, 1.5, [], {}, ["x"]):
        model, warnings = project("tracked/fallback", _fragment(name=bad))
        assert model["name"] == "tracked/fallback", bad
        assert warnings == [], bad
    assert project("tracked/missing", _fragment_without("name"))[0]["name"] == \
        "tracked/missing"

    # (d) `description`: any string (including "") is retained verbatim; every
    #     non-string (or an absent member) degrades to null -- never `""`, and
    #     never a warning.
    assert project("k", _fragment(description=""))[0]["description"] == ""
    assert project("k", _fragment(description="text"))[0]["description"] == "text"
    for bad in (None, True, False, 0, 5, 1.5, [], {}, ["x"]):
        model, warnings = project("k", _fragment(description=bad))
        assert model["description"] is None, bad
        assert warnings == [], bad
    assert project("k", _fragment_without("description"))[0]["description"] \
        is None

    # (e) An invalid tracked id is a PROGRAMMING error (TypeError), never a
    #     public envelope and never a partially-projected model.
    for bad_id in ("", None, 5, 1.5, True, ["x"], ("x",), b"x", {}):
        with pytest.raises(TypeError):
            project(bad_id, _known_fragment())

    # (f) An absent argument, an explicit null and an ACTUAL empty dict are the
    #     same all-unknown model, and the container is metadata_missing.
    all_unknown = {
        "id": "vendor/absent",
        "name": "vendor/absent",
        "description": None,
        "context_length": None,
        "architecture": {"input_modalities": None, "output_modalities": None},
        "supported_parameters": None,
    }
    for raw in (None, {}):
        model, warnings = project("vendor/absent", raw)
        assert model == all_unknown, raw
        assert warnings == ["metadata_missing"], raw
    assert project("vendor/absent") == project("vendor/absent", None)

    # (g) A non-null NON-DICT container is MALFORMED, not missing: the same
    #     all-unknown model, but ONLY metadata_invalid -- never manufactured
    #     missing-field warnings. Classification is `is not None`, NOT
    #     truthiness, so false/0/[]/"" are all non-null and therefore invalid.
    for raw in ([], ["text"], False, 0, 0.0, "", "text", ("x",), b"x"):
        model, warnings = project("vendor/bad-container", raw)
        assert model == {
            "id": "vendor/bad-container",
            "name": "vendor/bad-container",
            "description": None,
            "context_length": None,
            "architecture": {"input_modalities": None,
                             "output_modalities": None},
            "supported_parameters": None,
        }, raw
        assert warnings == ["metadata_invalid"], raw


def test_project_model_isolates_warnings_from_display_members():
    project = _catalog.project_model

    # Warning isolation: with all FOUR capabilities known, a missing,
    # malformed or null name/description NEVER contributes a code.
    complete = {
        "context_length": 4096,
        "architecture": {"input_modalities": ["text"],
                         "output_modalities": ["text"]},
        "supported_parameters": ["temperature"],
    }
    for extra in ({}, {"name": None}, {"name": 5}, {"description": 5},
                  {"description": None},
                  {"name": [], "description": {}}):
        model, warnings = project("k", dict(complete, **extra))
        assert warnings == [], extra
        assert set(model) == PUBLIC_MODEL_FIELDS, extra

    # (b) Mixed missing/invalid capability data warns in the frozen order, each
    #     code exactly once.
    _, warnings = project("k", {
        "context_length": None,
        "architecture": {"input_modalities": "text",
                         "output_modalities": None},
        "supported_parameters": "temperature",
    })
    assert warnings == ["metadata_missing", "metadata_invalid"]


def test_project_model_is_pure_unaliased_and_validates_in_a_strict_snapshot():
    catalog = _catalog
    project = catalog.project_model

    # (a) No aliasing: every retained container/list is a NEW object, and
    #     mutating a returned collection never touches the input or a later call.
    shared = ["temperature"]
    raw = {
        "name": "N",
        "description": "D",
        "context_length": 4096,
        "architecture": {"input_modalities": shared,
                         "output_modalities": shared},
        "supported_parameters": shared,
        "api_key": "secret",          # ignored extras must never be serialized
        "note": "ignored",
    }
    before = copy.deepcopy(raw)
    model, warnings = project("vendor/shared", raw)
    assert raw == before
    assert model["architecture"]["input_modalities"] is not shared
    assert model["supported_parameters"] is not shared
    assert model["architecture"]["input_modalities"] is not \
        model["architecture"]["output_modalities"]
    # Ignored extras (including nested extras) never leak into the projection.
    assert set(model) == PUBLIC_MODEL_FIELDS
    assert set(model["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS
    # Mutate the returned collections: input and a fresh call are unaffected.
    model["architecture"]["input_modalities"].append("vision")
    model["supported_parameters"].append("seed")
    warnings.append("metadata_invalid")
    assert raw == before
    again, again_warnings = project("vendor/shared", raw)
    assert again["architecture"]["input_modalities"] == ["temperature"]
    assert again["supported_parameters"] == ["temperature"]
    assert again_warnings == []
    # Nested extras on architecture are dropped, never interpreted.
    nested = project("k", _fragment(
        architecture={"input_modalities": ["text"],
                      "output_modalities": ["text"], "extra": 1}))[0]
    assert set(nested["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS

    # (b) Every projected model independently satisfies the unchanged public
    #     validator, with the exact literal field set asserted separately.
    known, known_warnings = project("vendor/shared", _known_fragment())
    assert set(known) == PUBLIC_MODEL_FIELDS
    assert known_warnings == []
    assert catalog._is_model("vendor/shared", known)
    degraded, degraded_warnings = project("vendor/shared", None)
    assert set(degraded) == PUBLIC_MODEL_FIELDS
    assert degraded_warnings == ["metadata_missing"]
    assert catalog._is_model("vendor/shared", degraded)

    # (c) Strict-snapshot integration: a synthetic COPY of an existing fixture
    #     with projected models embedded and independently resealed passes the
    #     unchanged public validator and the load_snapshot roundtrip. The
    #     json/hashlib reseal helper is the hash oracle, never a production one.
    snap = _decode_doc(FIXTURE_DIR / "lookup.json")
    snap["providers"]["synthetic-a"]["models"]["vendor/shared"] = known
    snap["providers"]["synthetic-b"]["models"]["vendor/shared"] = degraded
    _reseal(snap)
    body = {k: v for k, v in snap.items() if k != "snapshot_id"}
    assert snap["snapshot_id"] == hashlib.sha256(
        _canonical_text(body)).hexdigest()
    assert catalog.validate_snapshot(snap) is snap
    assert catalog.load_snapshot(json.dumps(snap)) == snap


# ---------- pure free-marker helper: compatibility edges ----------
#
# `watchdog_catalog.query.strip_free_marker` is the pure, stdlib-only marker
# helper extracted from build_site. The 17 `marker_strip_rule` rows are executed
# by `tests/test_contract_parity.py`; what remains here are the literal
# delimiter-only fallbacks and the Python-regex compatibility edges.

# Delimiter-only ids (a "free" segment with its delimiter but nothing else) plus
# the bare non-marker control. Each expected value is a literal restatement of
# the empty-result guard: a strip that would empty the id returns it unchanged,
# and bare "free" carries no delimiter so neither delimiter-bounded regex matches.
MARKER_FALLBACK_CONTROLS = {
    ":free": ":free",
    "-free": "-free",
    "_free": "_free",
    "free:": "free:",
    "free-": "free-",
    "free_": "free_",
    "free": "free",
}

# Controller characterization edges pinning the regex semantics the public
# algorithm must preserve: '$' matches at end OR before a single final newline
# (so one trailing '\n' survives stripping, two do not); stripping is ONE suffix
# pass then ONE prefix pass (never recursive or global); a mid-token 'free', an
# unmatched leading/trailing space and a 'free' inside the namespace are never
# markers.
MARKER_COMPAT_EDGES = {
    "free-free-X-free-free": "free-X-free",
    "vendor/x:free\n": "vendor/x\n",
    "vendor/x:free\n\n": "vendor/x:free\n\n",
    "free:": "free:",
    " free-x ": " free-x ",
    "vendor/freebie": "vendor/freebie",
    "FREE-X:free": "X",
}


def test_strip_free_marker_holds_the_literal_fallback_and_compat_edges():
    """The delimiter-only empty-result guard and the characterized regex edges.

    The frozen marker corpus itself is executed by the parity lane."""
    from watchdog_catalog.query import strip_free_marker

    for raw_id, want in MARKER_FALLBACK_CONTROLS.items():
        assert strip_free_marker(raw_id) == want, (
            f"fallback control mismatch for {raw_id!r}: want {want!r}")
    for raw_id, want in MARKER_COMPAT_EDGES.items():
        assert strip_free_marker(raw_id) == want, (
            f"compat edge mismatch for {raw_id!r}: want {want!r}")


# ---------- pure ordering primitive: natural raw-ID order ----------
#
# `natural_key` is the pure ordering primitive of the frozen README rule
# (README.md, "Total ordering rule" and "Natural comparator"): within one
# provider, raw IDs compare by maximal ASCII digit runs (ORIGINAL digit count,
# then lexical ASCII digits), case-sensitive Unicode code-point text, digit runs
# before text, empty boundary text tokens preserved and a strict prefix first.
#
# The seven `ordering-cases.json` vectors are executed by
# `tests/test_contract_parity.py`; the literal controls below pin the comparator
# edges that table does not reach, against this runtime primitive only.

# Compact literal controls for the frozen comparator edges the seven vectors do
# not reach: the empty id, a strict prefix, several digit runs in one id, the
# never-trimmed surrounding space and the snapshot-versus-query divergence row.
NATURAL_KEY_CONTROLS = (
    (["", "vendor/x"], ["", "vendor/x"]),
    (["vendor/x0", "vendor/x"], ["vendor/x", "vendor/x0"]),
    (["a1b10", "a1b02", "a1b2"], ["a1b2", "a1b02", "a1b10"]),
    (["vendor/model", " vendor/model "], [" vendor/model ", "vendor/model"]),
    (["vendor/item02", "vendor/item1"], ["vendor/item1", "vendor/item02"]),
)


def test_natural_key_matches_every_literal_ordering_control():
    """The natural comparator holds the literal empty/strict-prefix/multi-run/
    no-trim controls, without mutating the inputs and without ever raising a
    heterogeneous comparison TypeError."""
    from watchdog_catalog.query import natural_key

    corpus = set()
    for supplied, expected in NATURAL_KEY_CONTROLS:
        before = copy.deepcopy(supplied)
        assert sorted(supplied, key=natural_key) == expected, supplied
        assert supplied == before, supplied  # the input list is never reordered
        # Reverse-supplied control: the same literal expectation still holds.
        assert sorted(list(reversed(supplied)), key=natural_key) == expected, \
            supplied
        corpus.update(supplied)
        corpus.update(expected)

    # The frozen snapshot order is the OPPOSITE literal code-point order for the
    # divergence row: item02 precedes item1 there and follows it naturally.
    assert sorted(["vendor/item02", "vendor/item1"]) == \
        ["vendor/item02", "vendor/item1"]

    # Cross-control comparability sweep: every key in the corpus compares with
    # every other (no heterogeneous TypeError), equal strings give equal keys and
    # the key order is one consistent total order over the whole corpus.
    assert len(corpus) >= 10, len(corpus)
    ordered = sorted(corpus, key=natural_key)
    assert [natural_key(raw_id) for raw_id in ordered] == \
        sorted(natural_key(raw_id) for raw_id in corpus)
    for left, right in zip(ordered, ordered[1:]):
        assert natural_key(left) <= natural_key(right), (left, right)
    assert natural_key("") == natural_key("")
    assert natural_key("vendor/item1") == natural_key("vendor/item1")


INVALID_ARGUMENTS = "invalid_arguments"
INVALID_ARGUMENTS_MESSAGE = "Invalid tool arguments."
UNKNOWN_PROVIDER = "unknown_provider"
UNKNOWN_PROVIDER_MESSAGE = "Unknown provider."
ARGUMENT_MESSAGES = {
    INVALID_ARGUMENTS: INVALID_ARGUMENTS_MESSAGE,
    UNKNOWN_PROVIDER: UNKNOWN_PROVIDER_MESSAGE,
}


def _argument_envelope(code, parameter):
    return {"ok": False,
            "error": {"code": code, "message": ARGUMENT_MESSAGES[code],
                      "parameter": parameter}}


def _assert_argument_envelope(result, code, parameter, cid):
    assert result == _argument_envelope(code, parameter), cid
    assert set(result) == {"ok", "error"}, cid
    assert result["ok"] is False, cid
    assert set(result["error"]) == {"code", "message", "parameter"}, cid
    assert result["error"]["code"] == code, cid
    assert result["error"]["message"] == ARGUMENT_MESSAGES[code], cid
    assert result["error"]["parameter"] == parameter, cid
    return result


def _validated_snapshot(name):
    """The load_snapshot-validated precondition snapshot for a fixture name."""
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return _catalog.load_snapshot(text)


# ---------- pure get_model: exact provider + literal raw id ----------
#
# `watchdog_catalog.query.get_model(snapshot, arguments)` is the pure core of
# the `get_model` tool. It takes an already-VALIDATED public snapshot plus the
# whole DECODED argument object and returns either the flat success payload or
# a frozen error envelope. The `cases.json` / `argument-cases.json` rows are
# executed by `tests/test_contract_parity.py`; the owners here pin the exact
# pair identity, the details projection, the not_found envelope, the argument
# boundary and output freshness.

# The exact top-level field set of the `get_model` success payload and of its
# single details endpoint.
GET_MODEL_FIELDS = frozenset({
    "ok", "snapshot_id", "tick_epoch", "provider_staleness", "endpoint"})
GET_MODEL_ENDPOINT_FIELDS = frozenset({
    "provider", "model_id", "name", "description", "context_length",
    "input_modalities", "output_modalities", "supported_parameters",
    "chat_completions_url", "api_type"})
NOT_FOUND = {"ok": False,
             "error": {"code": "not_found", "message": "Model not found."}}


def test_get_model_exact_pair_projects_flat_details():
    """One exact (provider, raw id) pair returns that pair's flat details, and
    staleness is scoped to the requested provider only."""
    from watchdog_catalog.query import get_model

    lookup = _validated_snapshot("lookup.json")
    before = copy.deepcopy(lookup)
    result = get_model(lookup, {"provider": "synthetic-a",
                                "model_id": "vendor/model:free"})
    assert set(result) == GET_MODEL_FIELDS
    assert result["ok"] is True
    assert result["snapshot_id"] == lookup["snapshot_id"]
    assert result["tick_epoch"] == lookup["tick_epoch"]
    assert result["provider_staleness"] == {"synthetic-a": False}
    endpoint = result["endpoint"]
    assert set(endpoint) == GET_MODEL_ENDPOINT_FIELDS
    assert endpoint == {
        "provider": "synthetic-a",
        "model_id": "vendor/model:free",
        "name": "Vendor Model",
        "description": (
            "Oracle case 1 alpha endpoint: sufficient context, image input."),
        "context_length": 131072,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "supported_parameters": ["temperature", "max_tokens"],
        "chat_completions_url": (
            "https://synthetic-alpha.example/v1/chat/completions"),
        "api_type": "openai",
    }
    assert lookup == before

    # A raw id tracked by two providers is TWO endpoint identities: each call
    # selects exactly one provider and returns that pair's OWN metadata.
    alpha = get_model(lookup, {"provider": "synthetic-a",
                               "model_id": "vendor/shared"})
    beta = get_model(lookup, {"provider": "synthetic-b",
                              "model_id": "vendor/shared"})
    assert alpha["endpoint"]["context_length"] == 131072
    assert alpha["endpoint"]["input_modalities"] == ["text"]
    assert alpha["endpoint"]["supported_parameters"] == [
        "temperature", "max_tokens"]
    assert beta["endpoint"]["context_length"] == 32768
    assert beta["endpoint"]["input_modalities"] == ["text", "image"]
    assert beta["endpoint"]["supported_parameters"] == [
        "temperature", "top_p"]
    assert alpha["provider_staleness"] == {"synthetic-a": False}
    assert beta["provider_staleness"] == {"synthetic-b": False}
    assert alpha["endpoint"]["model_id"] == beta["endpoint"]["model_id"]


def test_get_model_literal_id_and_not_found_envelope():
    """The id is literal and single-provider: no trimming, ASCII folding,
    marker stripping or cross-provider lookup. An absent exact id in a known
    provider is the fixed, non-reflecting not_found envelope."""
    from watchdog_catalog.query import get_model

    lookup = _validated_snapshot("lookup.json")
    # `vendor/model` is NOT the marker-stripped form of `vendor/model:free`;
    # the :free id exists only on synthetic-a, so a padded/cased/sibling id
    # stays a literal miss.
    for model_id in ("vendor/model", "vendor/model-free", "VENDOR/SHARED",
                     " vendor/shared", "vendor/shared ",
                     "vendor/does-not-exist", "\u00a0", "\u2003"):
        result = get_model(lookup, {"provider": "synthetic-a",
                                    "model_id": model_id})
        assert result == NOT_FOUND, repr(model_id)
        assert set(result["error"]) == {"code", "message"}, repr(model_id)

    # A registered zero-ID provider is a KNOWN provider: every id is absent.
    assert get_model(lookup, {"provider": "synthetic-c",
                              "model_id": "vendor/shared"}) == NOT_FOUND


def test_get_model_argument_boundary_rules():
    """Frozen order: object, unknown key, provider presence/type, provider
    membership, then model_id presence/type/ASCII blankness."""
    from watchdog_catalog.query import get_model

    lookup = _validated_snapshot("lookup.json")

    for args in (None, [], 5, "x", False, 1.0, (), b"{}"):
        _assert_argument_envelope(get_model(lookup, args),
                                  INVALID_ARGUMENTS, None, repr(args))

    # ANY unknown key rejects with parameter null before every other check.
    for args in ({"bogus": 1},
                 {"provider": "synthetic-a", "bogus": 1},
                 {"provider": "synthetic-a", "model_id": "vendor/shared",
                  "limit": 5},
                 {"provider": "synthetic-z", "bogus": 1}):
        _assert_argument_envelope(get_model(lookup, dict(args)),
                                  INVALID_ARGUMENTS, None, repr(args))

    # provider: missing or wrong-typed is invalid_arguments(provider); an
    # absent string -- empty, padded, cased or unknown -- is unknown_provider.
    _assert_argument_envelope(get_model(lookup, {"model_id": "vendor/shared"}),
                              INVALID_ARGUMENTS, "provider", "missing")
    for bad in (None, True, False, 5, 1.5, [], ["synthetic-a"], {}, b"synthetic-a"):
        _assert_argument_envelope(
            get_model(lookup, {"provider": bad,
                               "model_id": "vendor/shared"}),
            INVALID_ARGUMENTS, "provider", repr(bad))
    for bad in ("", " synthetic-a ", "synthetic-a ", " synthetic-a",
                "Synthetic-A", "SYNTHETIC-A", "synthetic-z", "unknown"):
        _assert_argument_envelope(
            get_model(lookup, {"provider": bad, "model_id": "vendor/shared"}),
            UNKNOWN_PROVIDER, "provider", repr(bad))

    # provider membership precedes the model_id check: an unknown provider
    # rejects even when model_id is missing or blank.
    _assert_argument_envelope(get_model(lookup, {"provider": "synthetic-z"}),
                              UNKNOWN_PROVIDER, "provider", "unknown first")
    _assert_argument_envelope(
        get_model(lookup, {"provider": "synthetic-z", "model_id": ""}),
        UNKNOWN_PROVIDER, "provider", "unknown before blank")

    # model_id: missing/wrong-typed is invalid_arguments(model_id).
    _assert_argument_envelope(
        get_model(lookup, {"provider": "synthetic-a"}),
        INVALID_ARGUMENTS, "model_id", "missing")
    for bad in (None, True, False, 5, 1.5, [], ["vendor/shared"], {},
                {"a": 1}, b"vendor/shared"):
        _assert_argument_envelope(
            get_model(lookup, {"provider": "synthetic-a", "model_id": bad}),
            INVALID_ARGUMENTS, "model_id", repr(bad))
    for blank in ("", " ", "\t", "\n", "\r", "\v", "\f", " \t\n\r\v\f",
                  "\t \n", "  "):
        _assert_argument_envelope(
            get_model(lookup, {"provider": "synthetic-a", "model_id": blank}),
            INVALID_ARGUMENTS, "model_id", repr(blank))


def test_get_model_outputs_are_fresh_and_inputs_untouched():
    """Fresh containers on every call, no aliasing of the snapshot and fresh
    non-reflecting errors; an empty registry has no known provider."""
    from watchdog_catalog.query import get_model

    lookup = _validated_snapshot("lookup.json")
    before = copy.deepcopy(lookup)
    args = {"provider": "synthetic-a", "model_id": "vendor/shared"}
    args_before = copy.deepcopy(args)

    first = get_model(lookup, args)
    second = get_model(lookup, args)
    assert first == second
    assert first["endpoint"] is not second["endpoint"]
    assert first["endpoint"]["input_modalities"] is not \
        second["endpoint"]["input_modalities"]
    assert first["provider_staleness"] is not second["provider_staleness"]

    first["endpoint"]["input_modalities"].append("vision")
    first["endpoint"]["supported_parameters"].append("seed")
    first["provider_staleness"]["synthetic-a"] = True
    assert lookup == before
    assert args == args_before
    assert second["endpoint"]["input_modalities"] == ["text"]
    assert second["endpoint"]["supported_parameters"] == [
        "temperature", "max_tokens"]
    assert second["provider_staleness"] == {"synthetic-a": False}
    assert get_model(lookup, args) == second

    first_error = get_model(lookup, {"provider": "synthetic-a",
                                     "model_id": ""})
    first_error["error"]["code"] = "mutated"
    first_error["ok"] = True
    second_error = get_model(lookup, {"provider": "synthetic-a",
                                      "model_id": ""})
    assert second_error == _argument_envelope(INVALID_ARGUMENTS, "model_id")
    assert second_error is not first_error

    empty_registry = _search_snapshot([], {})
    _assert_argument_envelope(
        get_model(empty_registry,
                  {"provider": "synthetic-a", "model_id": "vendor/shared"}),
        UNKNOWN_PROVIDER, "provider", "empty registry")


# ---------- pure watchdog_status: fixed-clock status + coverage ----------
#
# `watchdog_status` is the WHOLE-CATALOGUE status query: its payload reports
# published-catalogue health, never private operator health, so it carries no
# local path/state_root/source/cache timestamp. The fixed test clock is a
# case-level `now` injected as a REQUIRED keyword-only argument into the core --
# it is never a tool argument and never a wall-clock lookup. The nine
# `cases.json` goldens and the status argument rows are executed by
# `tests/test_contract_parity.py`.

# The exact top-level field set of a status payload.
STATUS_FIELDS = frozenset({
    "ok", "snapshot_id", "tick_epoch", "cadence_s", "last_tick_age_s",
    "tick_fresh", "provider_staleness", "provider_counts", "metadata_coverage",
    "counts", "pending_alerts", "site_published", "site_publish_age_s",
    "warnings"})


def test_watchdog_status_valid_in_memory_controls():
    """Valid in-memory controls the parity lane cannot build: they reseal a
    synthetic body with the test-local hash oracle. The nine literal
    `watchdog_status` goldens are executed by `tests/test_contract_parity.py`."""
    from watchdog_catalog.query import watchdog_status

    # Valid controls, built in-memory from existing valid fixtures and resealed
    # with the test-local json/hashlib oracle, then re-validated BEFORE the
    # query. (1) epoch 0 with now 0 is a VALID fresh observation, never
    # "missing", and adds no warning.
    zero_epoch = _decode_doc(FIXTURE_DIR / "lookup.json")
    zero_epoch["tick_epoch"] = 0
    _reseal(zero_epoch)
    assert _catalog.validate_snapshot(zero_epoch) is zero_epoch
    at_zero = watchdog_status(zero_epoch, {}, now=0)
    assert at_zero["last_tick_age_s"] == 0
    assert at_zero["tick_fresh"] is True
    assert at_zero["warnings"] == []

    # (2) A legitimately EMPTY registry is a valid all-provider control with
    # empty provider maps and a fully zeroed counts/coverage pair.
    empty = _decode_doc(FIXTURE_DIR / "lookup.json")
    empty["provider_order"] = []
    empty["providers"] = {}
    _reseal(empty)
    assert _catalog.validate_snapshot(empty) is empty
    whole = watchdog_status(empty, {}, now=1759200000)
    assert set(whole) == STATUS_FIELDS
    assert whole["ok"] is True
    assert whole["provider_staleness"] == {} and whole["provider_counts"] == {}
    assert whole["counts"] == {"gateways": 0, "endpoints": 0,
                               "distinct_raw_ids": 0, "marker_group_labels": 0}
    assert whole["metadata_coverage"] == {
        "endpoints": 0, "context_length_known": 0, "input_modalities_known": 0,
        "output_modalities_known": 0, "supported_parameters_known": 0,
        "description_known": 0}

    # (3) A known-empty description string ("") counts as known, so all five
    # tracked pairs are description-known.
    empty_desc = _decode_doc(FIXTURE_DIR / "lookup.json")
    empty_desc["providers"]["synthetic-a"]["models"][
        "vendor/model-renamed"]["description"] = ""
    _reseal(empty_desc)
    assert _catalog.validate_snapshot(empty_desc) is empty_desc
    described = watchdog_status(empty_desc, {}, now=1759200000)
    assert described["metadata_coverage"]["description_known"] == 5

    # (4) The maximal FOUR-code composition: the snapshot THREE codes plus one
    # derived status code, in the frozen order (a full five is unreachable
    # because tick_epoch_future and tick_stale are mutually exclusive).
    composed = _decode_doc(FIXTURE_DIR / "lookup.json")
    composed["warnings"] = ["tick_epoch_unknown", "metadata_missing",
                            "metadata_invalid"]
    _reseal(composed)
    assert _catalog.validate_snapshot(composed) is composed
    stale = watchdog_status(composed, {}, now=1759203601)
    assert stale["warnings"] == ["tick_epoch_unknown", "metadata_missing",
                                 "metadata_invalid", "tick_stale"]
    future = watchdog_status(composed, {}, now=1759199999)
    assert future["warnings"] == ["tick_epoch_unknown", "metadata_missing",
                                  "metadata_invalid", "tick_epoch_future"]


# ---------- pure watchdog_status: the empty public argument boundary ----------
#
# `watchdog_status` has NO public arguments: only `{}` is accepted, and ANY key
# (including `now`, `provider` and `limit`) is an unknown argument. The frozen
# `argument-cases.json` rows are executed by `tests/test_contract_parity.py`;
# the programmatic rows are compact literal restatements of the README rules.

def test_watchdog_status_argument_boundary_rules():
    """The compact programmatic rows pin the frozen object -> any-unknown-key
    precedence, the never-reflecting fresh envelope and the fact that the fixed
    clock is a keyword-only harness input, never an argument. The frozen
    argument table is executed by `tests/test_contract_parity.py`."""
    from watchdog_catalog.query import watchdog_status

    lookup = _validated_snapshot("lookup.json")

    # A whole argument object that is not a JSON object is invalid_arguments/
    # null -- None, a list, a number, a string, a bool, a tuple and bytes.
    for args in (None, [], 5, "x", False, 1.0, (), b"{}"):
        _assert_argument_envelope(watchdog_status(lookup, args, now=0),
                                  INVALID_ARGUMENTS, None, repr(args))

    # ANY key is unknown, so every key -- including `now`, `provider` and
    # `limit` -- rejects with parameter null; the fixed clock never enters the
    # argument object.
    for key in ("now", "provider", "limit", "hostile", "snapshot_id"):
        _assert_argument_envelope(
            watchdog_status(lookup, {key: 1759200000}, now=0),
            INVALID_ARGUMENTS, None, key)
    _assert_argument_envelope(
        watchdog_status(lookup, {"now": 1759200000, "provider": "synthetic-a"},
                        now=0),
        INVALID_ARGUMENTS, None, "two unknown keys")
    assert watchdog_status(lookup, {}, now=0)["ok"] is True

    # The error envelope is a FRESH object on every call, never reflects a
    # hostile value, and is independent of the clock (validation runs before any
    # clock use).
    first = watchdog_status(lookup, {"now": 1759200000}, now=0)
    first["error"]["code"] = "mutated"
    first["ok"] = True
    second = watchdog_status(lookup, {"now": 1759200000}, now=0)
    assert second == _argument_envelope(INVALID_ARGUMENTS, None)
    assert second is not first
    assert watchdog_status(lookup, {"now": "hostile"}, now=0) == \
        _argument_envelope(INVALID_ARGUMENTS, None)
    assert watchdog_status(lookup, {"now": 1759200000}, now=999999999) == \
        _argument_envelope(INVALID_ARGUMENTS, None)


# ---------- pure watchdog_status: freshness + isolation + clock invariance ----

def test_watchdog_status_outputs_are_fresh_and_isolated_and_clock_invariant():
    """A status payload is never a window onto the cached snapshot: every
    mutable outer map/list is freshly allocated, so a caller mutating a result
    can never corrupt the snapshot or another call. On ONE snapshot the
    clock-independent provider/counts/coverage/provenance fields are IDENTICAL
    across four clocks while only last_tick_age_s/tick_fresh/warnings move."""
    from watchdog_catalog.query import watchdog_status

    lookup = _validated_snapshot("lookup.json")
    before = copy.deepcopy(lookup)

    # (a) Two calls are equal but every mutable outer container is distinct.
    first = watchdog_status(lookup, {}, now=1759203601)
    second = watchdog_status(lookup, {}, now=1759203601)
    assert lookup == before
    assert first == second
    for field in ("provider_staleness", "provider_counts", "metadata_coverage",
                  "counts", "warnings"):
        assert first[field] is not second[field], field

    # (b) Mutating a returned payload deeply leaves the snapshot, an earlier
    #     independent result and a brand-new call all unchanged.
    first["provider_staleness"]["synthetic-a"] = True
    first["provider_counts"]["synthetic-a"] = 99
    first["metadata_coverage"]["endpoints"] = 99
    first["counts"]["gateways"] = 99
    first["warnings"].append("injected")
    assert lookup == before
    assert second["provider_staleness"]["synthetic-a"] is False
    assert second["provider_counts"]["synthetic-a"] == 3
    assert second["counts"]["gateways"] == 3
    assert second["warnings"] == ["tick_stale"]
    third = watchdog_status(lookup, {}, now=1759203601)
    assert third == second

    # (c) Clock invariance: future, exactly-tick, at-cadence and just-over.
    clocks = (1759199999, 1759200000, 1759203600, 1759203601)
    results = [watchdog_status(lookup, {}, now=clock) for clock in clocks]
    for field in ("provider_staleness", "provider_counts", "metadata_coverage",
                  "counts", "snapshot_id", "tick_epoch", "cadence_s"):
        baseline = results[0][field]
        for result in results[1:]:
            assert result[field] == baseline, field
    # Only the clock-derived fields move, exactly as the inclusive boundary and
    # the status/snapshot warning split dictate.
    assert [result["last_tick_age_s"] for result in results] == \
        [None, 0, 3600, 3601]
    assert [result["tick_fresh"] for result in results] == \
        [False, True, True, False]
    assert [result["warnings"] for result in results] == \
        [["tick_epoch_future"], [], [], ["tick_stale"]]

    # (d) A fresh error envelope is returned on every call (mutate then recall).
    first_error = watchdog_status(lookup, {"now": 0}, now=0)
    first_error["error"]["code"] = "mutated"
    first_error["ok"] = True
    second_error = watchdog_status(lookup, {"now": 0}, now=0)
    assert second_error == _argument_envelope(INVALID_ARGUMENTS, None)
    assert second_error is not first_error

# ---------- internal metadata search: text/provider candidate pairs (3a) ------
#
# `watchdog_catalog.query._search_text_provider_pairs(snapshot, *, query=None,
# provider=None)` is the INTERNAL, UNPAGINATED candidate-selection stage that
# later `search_models` units compose. Preconditions are an already-VALIDATED
# snapshot and already-validated query/provider values: public argument
# validation -- including `unknown_provider` -- belongs to a later unit, so this
# stage deliberately has no temporary argument envelope and no public tool.
#
# The `cases.json` search goldens are executed through the public `search_models`
# by `tests/test_contract_parity.py`; every expectation below is a literal,
# hand-derived synthetic boundary control.


# -- synthetic boundary controls (structurally valid, independently pinned) ---

# Provider order is deliberately NON-alphabetic and differs from the provider
# map's insertion order, so the pinned pair sequences prove `provider_order`
# precedence rather than any incidental dict/alphabetic order.
SEARCH_ZETA_ORDER = [
    ("zeta", "m-1"),
    ("zeta", "m-1a"),
    ("zeta", "m-2"),
    ("zeta", "m-9"),
    ("zeta", "m-01"),
    ("zeta", "m-10"),
    ("zeta", "m-" + "0" * 29 + "1"),
    ("zeta", "m-\u00b2"),
    ("zeta", "m-\u0661"),
]
SEARCH_ALPHA_ORDER = [
    ("Alpha", "ascii-case"),
    ("Alpha", "caps-only"),
    ("Alpha", "fusion-left"),
    ("Alpha", "idtoken-model"),
    ("Alpha", "literal-regex"),
    ("Alpha", "name-target"),
    ("Alpha", "shared-id"),
    ("Alpha", "unicode-upper"),
    ("Alpha", "vendor/tool:free"),
]
SEARCH_ALPHA_LOWER_ORDER = [
    ("alpha", "shared-id"),
    ("alpha", "unicode-lower"),
    ("alpha", "vendor/tool"),
]
SEARCH_BULK_PAIRS = [("bulk", "bulk-%02d" % index) for index in range(55)]
SEARCH_ALL_PAIRS = (SEARCH_ZETA_ORDER + SEARCH_ALPHA_ORDER
                    + SEARCH_ALPHA_LOWER_ORDER + SEARCH_BULK_PAIRS)

# Compact (query, provider, expected ordered pairs) boundary rows.
SEARCH_BOUNDARY_ROWS = (
    # Omitted/empty query applies NO text constraint; whitespace is literal.
    (None, None, SEARCH_ALL_PAIRS),
    ("", None, SEARCH_ALL_PAIRS),
    ("   ", None, []),
    ("   ", "zeta", []),
    # Per-pair text only: a shared raw ID never borrows a sibling's name or
    # description, and provider scope ANDs with the text predicate.
    ("borrow", None, [("Alpha", "shared-id")]),
    ("borrow", "Alpha", [("Alpha", "shared-id")]),
    ("borrow", "alpha", []),
    ("other label", None, [("alpha", "shared-id")]),
    ("shared-id", None, [("Alpha", "shared-id"), ("alpha", "shared-id")]),
    # Each individual text field matches on its own: raw ID, name, description.
    ("idtoken", None, [("Alpha", "idtoken-model")]),
    ("nametoken", None, [("Alpha", "name-target")]),
    ("descword", None, [("Alpha", "idtoken-model")]),
    # Substring across a field boundary does not match (name "Alpha" plus
    # description "Beta prose body." is never joined).
    ("Alpha Beta", None, []),
    # ASCII-only fold: ASCII letters fold, non-ASCII case pairs stay literal.
    ("mixed name", None, [("Alpha", "ascii-case")]),
    ("MIXED NAME", None, [("Alpha", "ascii-case")]),
    ("\u00c9CHO", None, [("Alpha", "unicode-upper")]),
    ("\u00e9cho", None, [("alpha", "unicode-lower")]),
    ("\u00e9cho", "Alpha", []),
    # Literal regex-looking and invalid-regex queries; literal parentheses.
    ("[ab].*", None, [("Alpha", "literal-regex")]),
    ("(", None, [("Alpha", "literal-regex")]),
    # Never trimmed, never marker-normalized.
    ("Caps Only", None, [("Alpha", "caps-only")]),
    ("Caps Only ", None, []),
    ("vendor/tool", None, [("Alpha", "vendor/tool:free"),
                           ("alpha", "vendor/tool")]),
    ("vendor/tool", "alpha", [("alpha", "vendor/tool")]),
    ("vendor/tool:free", None, [("Alpha", "vendor/tool:free")]),
    # A null description is NOT a text field: neither Python's `str(None)`
    # spelling ("None") nor the JSON spelling ("null") may match a null.
    ("none", None, []),
    ("null", None, []),
    # Capability arrays, URL, provider key and object key names are
    # never searched.
    ("ImageToken", None, []),
    ("ParamToken", None, []),
    ("urltoken", None, []),
    ("apitoken", None, []),
    ("https", None, []),
    ("zeta", None, []),
    ("context_length", None, []),
    ("architecture", None, []),
    ("supported_parameters", None, []),
    ("display_name", None, []),
    # Exact provider scoping, including differently-cased VALID keys and a
    # registered zero-ID provider.
    (None, "zeta", SEARCH_ZETA_ORDER),
    (None, "Alpha", SEARCH_ALPHA_ORDER),
    (None, "alpha", SEARCH_ALPHA_LOWER_ORDER),
    (None, "empty", []),
    ("bulk", None, SEARCH_BULK_PAIRS),
)


def _search_model(raw_id, name, description=None, *, context_length=None,
                  input_modalities=None, output_modalities=None,
                  supported_parameters=None):
    """One complete projected public model object (the accepted model shape)."""
    return {
        "id": raw_id,
        "name": name,
        "description": description,
        "context_length": context_length,
        "architecture": {"input_modalities": input_modalities,
                         "output_modalities": output_modalities},
        "supported_parameters": supported_parameters,
    }


def _search_provider(models, display_name, url, *, api_type="openai",
                     stale=False):
    """One provider entry with its raw-ID array in the required literal order."""
    ids = sorted(models)
    return {
        "display_name": display_name,
        "chat_completions_url": url,
        "api_type": api_type,
        "model_ids": ids,
        "models": {raw_id: models[raw_id] for raw_id in ids},
        "stale": stale,
    }


def _search_snapshot(provider_order, providers):
    """A resealed synthetic snapshot that the REAL validator accepts."""
    snapshot = {
        "schema_version": 2,
        "snapshot_id": "0" * 64,
        "tick_epoch": 1759200000,
        "cadence_s": 3600,
        "provider_order": list(provider_order),
        "providers": providers,
        "warnings": [],
    }
    _reseal(snapshot)
    assert _catalog.validate_snapshot(snapshot) is snapshot
    return snapshot


def _boundary_snapshot():
    zeta = {}
    for raw_id, name in (
            ("m-1", "Order A"), ("m-01", "Order B"), ("m-1a", "Order C"),
            ("m-2", "Order D"), ("m-9", "Order E"), ("m-10", "Order F"),
            ("m-" + "0" * 29 + "1", "Order G"), ("m-\u00b2", "Order H"),
            ("m-\u0661", "Order I")):
        zeta[raw_id] = _search_model(raw_id, name)
    alpha_upper = {
        "ascii-case": _search_model("ascii-case", "MiXeD Name"),
        "caps-only": _search_model(
            "caps-only", "Caps Only", "plain synthetic prose",
            context_length=7, input_modalities=["ImageToken"],
            output_modalities=["OutToken"],
            supported_parameters=["ParamToken"]),
        "fusion-left": _search_model(
            "fusion-left", "Alpha", "Beta prose body."),
        "idtoken-model": _search_model(
            "idtoken-model", "Alpha Display One", "descword one."),
        "literal-regex": _search_model(
            "literal-regex", "paren (test)", "literal [ab].* marker"),
        "name-target": _search_model(
            "name-target", "NameToken Display", "Alpha prose two."),
        "shared-id": _search_model("shared-id", "Borrow Target"),
        "unicode-upper": _search_model("unicode-upper", "\u00c9CHO"),
        "vendor/tool:free": _search_model(
            "vendor/tool:free", "Tool Free Display"),
    }
    alpha_lower = {
        "shared-id": _search_model(
            "shared-id", "Other Label", "Sibling prose."),
        "unicode-lower": _search_model("unicode-lower", "\u00e9cho"),
        "vendor/tool": _search_model("vendor/tool", "Tool Sibling"),
    }
    bulk = {"bulk-%02d" % index: _search_model(
                "bulk-%02d" % index, "Bulk row %02d" % index,
                "Bulk synthetic row.")
            for index in range(55)}
    # Insertion order here is deliberately unrelated to provider_order.
    providers = {
        "empty": _search_provider(
            {}, "Empty Gateway", "https://empty.example/v1/chat/completions"),
        "bulk": _search_provider(
            bulk, "Bulk Gateway", "https://bulk.example/v1/chat/completions"),
        "alpha": _search_provider(
            alpha_lower, "Lower Alpha",
            "https://alpha.example/v1/chat/completions"),
        "zeta": _search_provider(
            zeta, "Zeta Order", "https://zeta.example/v1/chat/completions"),
        "Alpha": _search_provider(
            alpha_upper, "Upper Alpha",
            "https://urltoken.example/v1/chat/completions",
            api_type="apitoken"),
    }
    return _search_snapshot(
        ["zeta", "Alpha", "alpha", "bulk", "empty"], providers)


def test_search_text_provider_pairs_boundary_controls():
    """Literal ASCII-only text + exact provider scope over a synthetic, fully
    validated registry: pinned endpoint order, field isolation, no inference,
    no trimming, no marker repair, no cross-provider borrowing and no silent
    page cap."""
    from watchdog_catalog.query import _search_text_provider_pairs

    snapshot = _boundary_snapshot()
    # The pin is meaningful: neither provider-map insertion order nor literal
    # raw-ID order coincides with the frozen endpoint order.
    assert snapshot["provider_order"] != list(snapshot["providers"])
    assert snapshot["provider_order"] != sorted(snapshot["provider_order"])
    assert sorted(snapshot["providers"]["zeta"]["model_ids"]) != \
        [pair[1] for pair in SEARCH_ZETA_ORDER]

    before = copy.deepcopy(snapshot)
    for query, provider, expected in SEARCH_BOUNDARY_ROWS:
        result = _search_text_provider_pairs(
            snapshot, query=query, provider=provider)
        assert result == expected, (query, provider)
        assert type(result) is list, (query, provider)
        assert all(type(pair) is tuple and len(pair) == 2
                   for pair in result), (query, provider)
    assert snapshot == before

    # More than the public default page (5) is returned WHOLE: the internal
    # stage neither slices nor truncates.
    bulk = _search_text_provider_pairs(snapshot, query="bulk")
    assert len(bulk) == 55
    assert bulk[0] == ("bulk", "bulk-00")
    assert bulk[-1] == ("bulk", "bulk-54")

    # A legitimate empty registry and a zero-ID single provider both select
    # nothing, including for an omitted/empty query.
    solo = _search_snapshot(
        ["solo"],
        {"solo": _search_provider(
            {}, "Solo", "https://solo.example/v1/chat/completions")})
    assert _search_text_provider_pairs(solo) == []
    assert _search_text_provider_pairs(solo, query="") == []
    assert _search_text_provider_pairs(snapshot, provider="empty") == []

    # A returned list is a fresh, non-aliasing snapshot: mutating it (or one of
    # its rows) cannot corrupt the snapshot or a later call, and the input query
    # string is untouched.
    query = "shared-id"
    first = _search_text_provider_pairs(snapshot, query=query)
    assert first == [("Alpha", "shared-id"), ("alpha", "shared-id")]
    first.clear()
    mutated = _search_text_provider_pairs(snapshot, query=query)
    mutated.append(("Alpha", "injected"))
    mutated[0] = ("alpha", "vendor/tool")
    assert _search_text_provider_pairs(snapshot, query=query) == \
        [("Alpha", "shared-id"), ("alpha", "shared-id")]
    assert query == "shared-id"
    assert snapshot == before


# -- inclusive per-pair minimum-context stage: unknown is never zero --

def test_search_min_context_pairs_boundary_controls():
    """A synthetic fully-validated registry pins None-threshold freedom,
    zero-vs-unknown distinction, the inclusive boundary, per-pair isolation (no
    raw-id borrowing across providers, no selection expansion), unpaginated
    completeness and mutation isolation. The active-min-context goldens
    themselves are executed through `search_models` by
    `tests/test_contract_parity.py`."""
    from watchdog_catalog.query import _search_min_context_pairs
    from watchdog_catalog.query import _search_text_provider_pairs

    # -- synthetic controls over a resealed, structurally valid registry.
    def model(raw_id, context_length):
        return _search_model(raw_id, "Context " + raw_id, None,
                              context_length=context_length)

    max_safe = _catalog.MAX_SAFE_INT
    gate_models = {
        "model-null": model("model-null", None),
        "model-zero": model("model-zero", 0),
        "model-one": model("model-one", 1),
        "model-low": model("model-low", 65535),
        "model-high": model("model-high", 65536),
        "model-higher": model("model-higher", 65537),
        "model-max": model("model-max", max_safe),
        "model-shared": model("model-shared", 65536),
    }
    other_models = {"model-shared": model("model-shared", 32768)}
    bulk_models = {"bulk-%02d" % index: model("bulk-%02d" % index, 7)
                   for index in range(60)}
    providers = {
        "gate": _search_provider(
            gate_models, "Gate", "https://gate.example/v1/chat/completions"),
        "other": _search_provider(
            other_models, "Other", "https://other.example/v1/chat/completions"),
        "bulk": _search_provider(
            bulk_models, "Bulk", "https://bulk.example/v1/chat/completions"),
        "zero-provd": _search_provider(
            {}, "Zero", "https://zero.example/v1/chat/completions"),
    }
    snapshot = _search_snapshot(["gate", "other", "bulk", "zero-provd"],
                                providers)
    before = copy.deepcopy(snapshot)

    # (supplied pairs, threshold, expected ordered pairs) compact rows.
    context_rows = (
        # None threshold is NO constraint: unknown and known contexts both stay.
        ([("gate", "model-null"), ("gate", "model-zero"),
          ("gate", "model-one")], None,
         [("gate", "model-null"), ("gate", "model-zero"),
          ("gate", "model-one")]),
        # Zero threshold IS active: it retains a known zero, rejects null.
        ([("gate", "model-null"), ("gate", "model-zero")], 0,
         [("gate", "model-zero")]),
        # Inclusive boundary: exact passes, one below fails, one above passes.
        ([("gate", "model-low"), ("gate", "model-high"),
          ("gate", "model-higher")], 65536,
         [("gate", "model-high"), ("gate", "model-higher")]),
        # Safe maximum boundary passes; a smaller context never reaches it.
        ([("gate", "model-max"), ("gate", "model-higher")], max_safe,
         [("gate", "model-max")]),
        # No matches at all for a threshold above every supplied context.
        ([("gate", "model-one"), ("gate", "model-zero")], max_safe, []),
    )
    for pairs, threshold, expected in context_rows:
        result = _search_min_context_pairs(
            snapshot, pairs, min_context_length=threshold)
        assert result == expected, (pairs, threshold)
        assert type(result) is list, (pairs, threshold)

    # A shared raw id is TWO pairs: provider A passes, provider B fails, and
    # the candidate set never borrows A's high context for B's pair.
    shared_text = _search_text_provider_pairs(snapshot, query="shared")
    assert shared_text == [("gate", "model-shared"), ("other", "model-shared")]
    assert _search_min_context_pairs(
        snapshot, shared_text, min_context_length=65536) == \
        [("gate", "model-shared")]
    assert _search_min_context_pairs(
        snapshot, [("other", "model-shared"), ("gate", "model-higher")],
        min_context_length=65536) == [("gate", "model-higher")]

    # A supplied strict subset is never expanded, even when excluded snapshot
    # models hold enough context; empty input stays empty.
    assert _search_min_context_pairs(
        snapshot, [("gate", "model-null"), ("gate", "model-zero")],
        min_context_length=max_safe) == []
    assert _search_min_context_pairs(snapshot, []) == []
    assert _search_min_context_pairs(snapshot, [], min_context_length=0) == []

    # Exact input order is preserved: never sorted, never deduplicated.
    unordered = [("gate", "model-higher"), ("gate", "model-zero"),
                 ("gate", "model-low")]
    assert _search_min_context_pairs(
        snapshot, unordered, min_context_length=0) == unordered

    # A zero-ID provider selects nothing, and an empty registry stays empty.
    assert _search_text_provider_pairs(snapshot, provider="zero-provd") == []
    assert _search_min_context_pairs(
        snapshot, _search_text_provider_pairs(snapshot, provider="zero-provd"),
        min_context_length=0) == []
    solo = _search_snapshot(
        ["solo"], {"solo": _search_provider(
            {}, "Solo", "https://solo.example/v1/chat/completions")})
    assert _search_min_context_pairs(solo, []) == []
    assert _search_min_context_pairs(solo, [], min_context_length=0) == []

    # More than the public default page (5) of eligible pairs stays complete.
    bulk_pairs = _search_text_provider_pairs(snapshot, provider="bulk")
    assert len(bulk_pairs) == 60
    assert _search_min_context_pairs(
        snapshot, bulk_pairs, min_context_length=7) == bulk_pairs

    # A fresh, non-aliasing result list: mutating it (or a returned pair) can
    # never corrupt the snapshot, the input sequence or another call.
    result = _search_min_context_pairs(
        snapshot, [("gate", "model-null"), ("gate", "model-zero")],
        min_context_length=0)
    result.clear()
    assert snapshot == before
    again = _search_min_context_pairs(
        snapshot, [("gate", "model-null"), ("gate", "model-zero")],
        min_context_length=0)
    again.append(("gate", "injected"))
    again[0] = ("gate", "model-null")
    assert _search_min_context_pairs(
        snapshot, [("gate", "model-null"), ("gate", "model-zero")],
        min_context_length=0) == [("gate", "model-zero")]
    assert unordered == [("gate", "model-higher"), ("gate", "model-zero"),
                         ("gate", "model-low")]
    assert snapshot == before

    # -- strict unknown handling by composition: hostile upstream contexts are
    # projected through the accepted projector into a resealed snapshot, so the
    # query stage never sees an invalid snapshot.
    raw_context_rows = (
        ("absent", {"name": "Absent"}, None),
        ("null", {"name": "Null", "context_length": None}, None),
        ("numeric-string", {"name": "String", "context_length": "65536"}, None),
        ("bool", {"name": "Bool", "context_length": True}, None),
        ("negative", {"name": "Negative", "context_length": -1}, None),
        ("float-one", {"name": "FloatOne", "context_length": 1.0}, None),
        ("float-fractional", {"name": "FloatHalf", "context_length": 2.5}, None),
        ("non-finite", {"name": "Infinity", "context_length": float("inf")}, None),
        ("unsafe", {"name": "Unsafe", "context_length": max_safe + 1}, None),
        ("known-zero", {"name": "KnownZero", "context_length": 0}, 0),
        ("known-positive", {"name": "KnownPositive",
                            "context_length": 4096}, 4096),
        ("known-max", {"name": "KnownMax", "context_length": max_safe}, max_safe),
    )
    projected_models = {}
    for raw_id, raw, expected_context in raw_context_rows:
        projected, warnings = _catalog.project_model(raw_id, raw)
        assert projected["context_length"] == expected_context, raw_id
        assert type(projected["context_length"]) is type(expected_context), raw_id
        # A degraded or missing context never drops the endpoint: the model
        # keeps its complete projected shape, and the warning list is fresh.
        assert set(projected) == {"id", "name", "description", "context_length",
                                  "architecture", "supported_parameters"}, raw_id
        assert type(warnings) is list, raw_id
        projected_models[raw_id] = projected
    raw_snapshot = _search_snapshot(
        ["raw"], {"raw": _search_provider(
            projected_models, "Raw",
            "https://raw.example/v1/chat/completions")})
    raw_pairs = _search_text_provider_pairs(raw_snapshot)
    assert len(raw_pairs) == len(raw_context_rows)
    assert _search_min_context_pairs(raw_snapshot, raw_pairs,
                                     min_context_length=0) == \
        [("raw", "known-max"), ("raw", "known-positive"), ("raw", "known-zero")]
    assert _search_min_context_pairs(raw_snapshot, raw_pairs,
                                     min_context_length=1) == \
        [("raw", "known-max"), ("raw", "known-positive")]
    assert _search_min_context_pairs(raw_snapshot, raw_pairs,
                                     min_context_length=max_safe) == \
        [("raw", "known-max")]
    assert _search_min_context_pairs(raw_snapshot, raw_pairs,
                                     min_context_length=None) == raw_pairs
    assert snapshot == before


# ---------- internal metadata search: shared capability AND stage ----------
#
# `watchdog_catalog.query._search_capability_pairs(snapshot, pairs, *,
# field, requested)` is the INTERNAL, UNPAGINATED capability filter that
# `search_models` composes twice -- once for `input_modalities`, once for
# `supported_parameters`. Preconditions are an already-VALIDATED snapshot, an
# ordered sequence of immutable tracked `(provider_key, raw_model_id)` tuples
# and an already-validated `None`/list-of-nonempty-strings request: public
# argument validation belongs to the transport unit, so this stage has no
# temporary argument envelope and no public tool.
#
# Expectations below are literal, hand-derived controls over a resealed,
# structurally valid registry. The public `search_models` whole-payload goldens
# are executed by `tests/test_contract_parity.py`; they remain the owners of
# stage wiring, success and error semantics and are not replayed here as a
# private intermediate pipeline.


CAPABILITY_FIELDS = (
    ("input_modalities", "text", "image"),
    ("supported_parameters", "temperature", "max_tokens"),
)


@pytest.mark.parametrize("field,first,second", CAPABILITY_FIELDS,
                         ids=[field for field, _, _ in CAPABILITY_FIELDS])
def test_search_capability_pairs_filter_controls(field, first, second):
    """One shared field-parametrised capability filter: None/[] freedom, the
    ALL-tokens-on-one-pair AND, ASCII-only folding with literal whitespace and
    non-ASCII casing, exact membership (no substring), isolation from every
    other field, no cross-provider borrowing, subset/order preservation,
    unpaginated completeness and mutation isolation."""
    from watchdog_catalog.query import _search_capability_pairs

    def model(raw_id, values):
        return _search_model(raw_id, "Capability " + raw_id, **{field: values})

    if field == "input_modalities":
        decoy = _search_model(
            "decoy-" + first, first, first, input_modalities=None,
            output_modalities=[first], supported_parameters=[first])
    else:
        decoy = _search_model(
            "decoy-" + first, first, first, input_modalities=[first],
            output_modalities=[first], supported_parameters=None)
    synth_models = {
        "both": model("both", [first, second]),
        "single": model("single", [first]),
        "empty-known": model("empty-known", []),
        "unknown": model("unknown", None),
        "cased-dupes": model(
            "cased-dupes", [first.upper(), first.title(), first]),
        "padded": model("padded", [" " + first + " "]),
        "unicode-upper": model("unicode-upper", ["\u00c9"]),
        "substring": model("substring", [first + "_" + second]),
        # Every NON-selected slot carries `first` while the selected field is
        # unknown: name, description, raw id and both other capability fields
        # must never become a substitute.
        "decoy-" + first: decoy,
        "shared-id": model("shared-id", [first, second]),
    }
    other_models = {"shared-id": model("shared-id", [first])}
    bulk_models = {"bulk-%02d" % index: model("bulk-%02d" % index, [first])
                   for index in range(60)}
    snapshot = _search_snapshot(
        ["gate", "other", "bulk", "zero-provd"],
        {"gate": _search_provider(
             synth_models, "Gate", "https://gate.example/v1/chat/completions"),
         "other": _search_provider(
             other_models, "Other",
             "https://other.example/v1/chat/completions"),
         "bulk": _search_provider(
             bulk_models, "Bulk", "https://bulk.example/v1/chat/completions"),
         "zero-provd": _search_provider(
             {}, "Zero", "https://zero.example/v1/chat/completions")})
    before = copy.deepcopy(snapshot)

    def run(pairs, requested):
        return _search_capability_pairs(
            snapshot, pairs, field=field, requested=requested)

    # None and [] are NO constraint: unknown (null) and known-empty ([]) pairs
    # stay, while a nonempty request retains neither.
    free = [("gate", "both"), ("gate", "unknown"), ("gate", "empty-known")]
    for requested in (None, []):
        result = run(free, requested)
        assert result == free
        assert result is not free
    assert run(free, [first]) == [("gate", "both")]

    # ALL requested tokens on the SAME pair, request order independent, and a
    # duplicated request is idempotent.
    two = [("gate", "both"), ("gate", "single")]
    assert run(two, [first]) == two
    assert run(two, [first, second]) == [("gate", "both")]
    assert run(two, [second, first]) == [("gate", "both")]
    assert run([("gate", "both")], [first, first, first.upper()]) == \
        [("gate", "both")]

    # ASCII-only folding on both sides; reported duplicates are harmless.
    assert run([("gate", "cased-dupes")], [first.upper()]) == \
        [("gate", "cased-dupes")]

    # Whitespace stays literal: a padded token matches only an identically
    # padded reported token.
    assert run([("gate", "padded")], [first]) == []
    assert run([("gate", "padded")], [" " + first + " "]) == \
        [("gate", "padded")]

    # Non-ASCII case pairs never collapse.
    assert run([("gate", "unicode-upper")], ["\u00e9"]) == []
    assert run([("gate", "unicode-upper")], ["\u00c9"]) == \
        [("gate", "unicode-upper")]

    # Exact membership only: a longer capability name is not a substring match.
    assert run([("gate", "substring")], [first]) == []
    assert run([("gate", "substring")], [first + "_" + second]) == \
        [("gate", "substring")]

    # No other field is a substitute: selected field unknown while every other
    # slot (name, description, raw id and both other capability fields)
    # carries the requested token.
    assert run([("gate", "decoy-" + first)], [first]) == []

    # A shared raw id stays TWO pairs with disjoint per-provider capability:
    # no union, no borrowing, incoming order preserved.
    shared = [("gate", "shared-id"), ("other", "shared-id")]
    assert run(shared, [second]) == [("gate", "shared-id")]
    assert run(shared, [first]) == shared
    assert run([("other", "shared-id"), ("gate", "shared-id")], [second]) == \
        [("gate", "shared-id")]

    # A supplied strict subset is never expanded; empty input and an empty
    # registry stay empty.
    assert run([("gate", "both")], [first]) == [("gate", "both")]
    assert run([], [first]) == []
    assert run([], None) == []
    empty_snapshot = _search_snapshot([], {})
    assert _search_capability_pairs(
        empty_snapshot, [], field=field, requested=[first]) == []

    # Incoming pair order and duplicates are preserved exactly.
    ordered = [("gate", "single"), ("gate", "both"), ("gate", "cased-dupes"),
               ("gate", "single")]
    assert run(ordered, [first]) == ordered

    # More than the default page (5) of matches stays complete.
    bulk_pairs = [("bulk", "bulk-%02d" % index) for index in range(60)]
    assert len(run(bulk_pairs, [first])) == 60

    # Fresh, non-aliasing list on both branches: mutating it cannot corrupt the
    # snapshot, the supplied sequence or a later call.
    supplied = [("gate", "both"), ("gate", "single")]
    for requested, expected in (
            (None, supplied), ([], supplied),
            ([first], supplied), ([second], [("gate", "both")])):
        result = run(supplied, requested)
        assert result is not supplied
        assert result == expected
        result.clear()
        assert run(supplied, requested) == expected
        assert supplied == [("gate", "both"), ("gate", "single")]
    assert snapshot == before


# ---------- public metadata search: pipeline, pagination, projection (3e) ----
#
# `watchdog_catalog.query.search_models(snapshot, arguments)` is the PUBLIC pure
# assembled search: it validates the WHOLE decoded argument object against the
# frozen tool contract, composes the four accepted internal stages in order,
# then slices and projects the page. The `cases.json` search goldens and the
# `argument-cases.json` table are executed by
# `tests/test_contract_parity.py`; the compact rows below add native edges.

SEARCH_PAYLOAD_FIELDS = frozenset({
    "ok", "snapshot_id", "tick_epoch", "provider_staleness", "total_matches",
    "next_offset", "endpoints",
})
SEARCH_ROW_FIELDS = frozenset({
    "provider", "model_id", "name", "context_length", "input_modalities",
    "output_modalities", "supported_parameters",
})


def _assert_search_strict_types(result, cid):
    """Exact payload/row field sets plus strict scalar types. Deep equality
    alone cannot distinguish ``1.0``/``True`` from a strict integer count."""
    assert type(result) is dict, cid
    assert set(result) == SEARCH_PAYLOAD_FIELDS, cid
    assert type(result["ok"]) is bool and result["ok"] is True, cid
    assert type(result["provider_staleness"]) is dict, cid
    assert type(result["endpoints"]) is list, cid
    assert type(result["total_matches"]) is int, cid
    assert result["total_matches"] >= 0, cid
    if result["next_offset"] is not None:
        assert type(result["next_offset"]) is int, cid
        assert result["next_offset"] >= 0, cid
    for name, value in result["provider_staleness"].items():
        assert type(name) is str and type(value) is bool, (cid, name)
    for row in result["endpoints"]:
        assert type(row) is dict and set(row) == SEARCH_ROW_FIELDS, cid
        assert type(row["provider"]) is str, cid
        assert type(row["model_id"]) is str, cid
        assert row["context_length"] is None or \
            type(row["context_length"]) is int, cid
        for name in ("input_modalities", "output_modalities",
                     "supported_parameters"):
            assert row[name] is None or type(row[name]) is list, (cid, name)



# ---------- public metadata search: the whole argument boundary (3e) ----------
#
# The frozen `argument-cases.json` rows (31 invalid envelopes and 13 valid
# controls) are executed by `tests/test_contract_parity.py`. The compact rows
# below add every parameter's wrong-type, numeric-safety, array-shape and
# precedence edge native to this runtime.

SEARCH_MIN_CONTEXT_FIELD = "min_context_length"


def test_search_models_argument_boundary_rules(monkeypatch):
    """Compact rows pin non-object rejection, every parameter's wrong types
    (both bools, numeric strings, fractions, huge native ints, NaN/Inf, unsafe
    boundaries), integral-float acceptance (raw inputs are never echoed),
    array member validation versus []/padded/cased/duplicate/unmatched tokens,
    exact provider case/padding/zero-ID/empty-registry rules, all six adjacent
    signature links and unknown-key precedence in BOTH insertion orders,
    fail-before-selection and fresh non-reflecting errors. The frozen argument
    table is executed by `tests/test_contract_parity.py`."""
    from watchdog_catalog.query import search_models

    lookup = _validated_snapshot("lookup.json")
    lookup_before = copy.deepcopy(lookup)

    # -- non-object argument objects: invalid_arguments with parameter null.
    for args in (None, [], 5, "x", False, 1.0, (), b"{}", set()):
        _assert_argument_envelope(search_models(lookup, args),
                                  INVALID_ARGUMENTS, None, repr(args))

    # -- every parameter's wrong non-null type / numeric edge.
    for field, value in (
            ("query", True), ("query", False), ("query", 0), ("query", 1.0),
            ("query", []), ("query", {}), ("query", ()), ("query", b"x"),
            ("provider", True), ("provider", False), ("provider", 0),
            ("provider", 1.5), ("provider", []), ("provider", {}),
            ("min_context_length", True), ("min_context_length", False),
            ("min_context_length", "65536"), ("min_context_length", " 1 "),
            ("min_context_length", 1.5), ("min_context_length", -1),
            ("min_context_length", -1.5),
            ("min_context_length", _catalog.MAX_SAFE_INT + 1),
            ("min_context_length", 10 ** 400),
            ("min_context_length", float("nan")),
            ("min_context_length", float("inf")),
            ("min_context_length", float("-inf")),
            ("min_context_length", []), ("min_context_length", {}),
            ("input_modalities", "text"), ("input_modalities", ("text",)),
            ("input_modalities", {"text"}), ("input_modalities", {"text": 1}),
            ("input_modalities", [None]), ("input_modalities", [5]),
            ("input_modalities", [True]), ("input_modalities", [""]),
            ("input_modalities", ["text", 5]),
            ("input_modalities", ["text", ""]),
            ("input_modalities", ["text", None]),
            ("input_modalities", [["text"]]),
            ("supported_parameters", "temperature"),
            ("supported_parameters", ("temperature",)),
            ("supported_parameters", {"temperature"}),
            ("supported_parameters", {"temperature": 1}),
            ("supported_parameters", [None]),
            ("supported_parameters", [5]),
            ("supported_parameters", [False]),
            ("supported_parameters", [""]),
            ("supported_parameters", ["temperature", 5]),
            ("supported_parameters", ["temperature", ""]),
            ("supported_parameters", ["temperature", None]),
            ("limit", True), ("limit", False), ("limit", None),
            ("limit", 0), ("limit", -1), ("limit", 201), ("limit", 200.5),
            ("limit", 1e308), ("limit", "50"), ("limit", []), ("limit", {}),
            ("limit", _catalog.MAX_SAFE_INT),
            ("limit", _catalog.MAX_SAFE_INT + 1), ("limit", 10 ** 400),
            ("limit", float("nan")), ("limit", float("inf")),
            ("limit", float("-inf")),
            ("offset", True), ("offset", False), ("offset", None),
            ("offset", -1), ("offset", 0.5), ("offset", "0"), ("offset", []),
            ("offset", {}), ("offset", _catalog.MAX_SAFE_INT + 1),
            ("offset", 10 ** 400), ("offset", float("nan")),
            ("offset", float("inf")), ("offset", float("-inf"))):
        _assert_argument_envelope(search_models(lookup, {field: value}),
                                  INVALID_ARGUMENTS, field,
                                  (field, repr(value)))

    # -- accepted values, including safe boundaries and integral floats.
    for args in ({}, {"query": None}, {"query": ""}, {"query": "   "},
                 {"query": "[ab].*"}, {"query": "(\u00c9CHO)"},
                 {"query": "\u00e9cho"}, {"query": "a" * 400},
                 {"provider": None}, {"provider": "synthetic-a"},
                 {"provider": "synthetic-c"},
                 {"min_context_length": None},
                 {"min_context_length": 0},
                 {"min_context_length": _catalog.MAX_SAFE_INT},
                 {"min_context_length": 1.0},
                 {"input_modalities": []}, {"input_modalities": [" "]},
                 {"input_modalities": [" text "]},
                 {"input_modalities": ["TEXT", "text", "IMAGE"]},
                 {"input_modalities": ["no-such-modality"]},
                 {"input_modalities": ["\u00c9"]},
                 {"supported_parameters": []},
                 {"supported_parameters": [" Temperature ", "TEMPERATURE"]},
                 {"supported_parameters": ["no-such-parameter"]},
                 {"limit": 1}, {"limit": 200}, {"limit": 1.0},
                 {"offset": 0}, {"offset": 0.0},
                 {"offset": _catalog.MAX_SAFE_INT}):
        result = search_models(lookup, dict(args))
        assert result["ok"] is True, args
        _assert_search_strict_types(result, repr(args))

    # Integral floats are the same JSON integer value; the page is identical to
    # the strict-int call and nothing echoes the raw input.
    float_result = search_models(
        lookup, {SEARCH_MIN_CONTEXT_FIELD: 1.0, "limit": 1.0, "offset": 1.0})
    int_result = search_models(
        lookup, {SEARCH_MIN_CONTEXT_FIELD: 1, "limit": 1, "offset": 1})
    assert float_result == int_result
    assert "filters" not in float_result
    assert type(float_result["total_matches"]) is int

    # -- provider membership: never trimmed, case-folded or repaired.
    for bad in ("", " synthetic-a ", "synthetic-a ", " synthetic-a",
                "Synthetic-A", "SYNTHETIC-A", "synthetic-z", "unknown"):
        _assert_argument_envelope(search_models(lookup, {"provider": bad}),
                                  UNKNOWN_PROVIDER, "provider", bad)
    # A registered zero-ID provider is VALID; an empty registry accepts {} and
    # rejects every named provider.
    zero_id = search_models(lookup, {"provider": "synthetic-c"})
    assert zero_id["ok"] is True
    assert zero_id["endpoints"] == [] and zero_id["total_matches"] == 0
    assert zero_id["provider_staleness"] == {"synthetic-c": False}
    empty_registry = _search_snapshot([], {})
    for args in ({}, {"provider": None}, {"query": "anything"}):
        result = search_models(empty_registry, dict(args))
        assert result["ok"] is True and result["endpoints"] == [], args
        assert result["total_matches"] == 0, args
        assert result["provider_staleness"] == {}, args
        assert result["next_offset"] is None, args
    _assert_argument_envelope(
        search_models(empty_registry, {"provider": "synthetic-a"}),
        UNKNOWN_PROVIDER, "provider", "empty registry")

    # -- frozen signature precedence, pinned with REVERSED insertion order.
    precedence_rows = (
        ({"bogus": 1, "query": []}, INVALID_ARGUMENTS, None),
        ({"query": [], "bogus": 1}, INVALID_ARGUMENTS, None),
        ({"query": [], "provider": "synthetic-z"}, INVALID_ARGUMENTS, "query"),
        ({"provider": "synthetic-z", "query": []}, INVALID_ARGUMENTS, "query"),
        ({"min_context_length": -1, "provider": "synthetic-z"},
         UNKNOWN_PROVIDER, "provider"),
        ({"provider": "synthetic-z", "min_context_length": -1},
         UNKNOWN_PROVIDER, "provider"),
        ({"query": 5, "min_context_length": -1}, INVALID_ARGUMENTS, "query"),
        ({"min_context_length": -1, "query": 5}, INVALID_ARGUMENTS, "query"),
        ({"limit": 0, "input_modalities": "x"}, INVALID_ARGUMENTS,
         "input_modalities"),
        ({"input_modalities": "x", "limit": 0}, INVALID_ARGUMENTS,
         "input_modalities"),
        ({"supported_parameters": "x", "input_modalities": "x"},
         INVALID_ARGUMENTS, "input_modalities"),
        ({"input_modalities": "x", "supported_parameters": "x"},
         INVALID_ARGUMENTS, "input_modalities"),
        ({"offset": -1, "limit": 0}, INVALID_ARGUMENTS, "limit"),
        ({"limit": 0, "offset": -1}, INVALID_ARGUMENTS, "limit"),
        ({"min_context_length": -1, "input_modalities": "x"},
         INVALID_ARGUMENTS, "min_context_length"),
        ({"input_modalities": "x", "min_context_length": -1},
         INVALID_ARGUMENTS, "min_context_length"),
        ({"supported_parameters": "x", "limit": 0}, INVALID_ARGUMENTS,
         "supported_parameters"),
        ({"limit": 0, "supported_parameters": "x"}, INVALID_ARGUMENTS,
         "supported_parameters"),
        ({"bogus": 1, "provider": "synthetic-a"}, INVALID_ARGUMENTS, None),
        ({"provider": "synthetic-a", "bogus": 1}, INVALID_ARGUMENTS, None),
    )
    for args, code, parameter in precedence_rows:
        _assert_argument_envelope(search_models(lookup, dict(args)),
                                  code, parameter, args)

    # -- validation runs BEFORE any selection stage.
    import watchdog_catalog.query as query_module

    def _boom(*args, **kwargs):
        raise AssertionError("selection stage ran before validation")

    for name in ("_search_text_provider_pairs", "_search_min_context_pairs",
                 "_search_capability_pairs"):
        monkeypatch.setattr(query_module, name, _boom)
    for args, code, parameter in (
            ({"limit": 0}, INVALID_ARGUMENTS, "limit"),
            ({"provider": "synthetic-z"}, UNKNOWN_PROVIDER, "provider"),
            ({"query": []}, INVALID_ARGUMENTS, "query")):
        _assert_argument_envelope(search_models(lookup, dict(args)),
                                  code, parameter, args)
    monkeypatch.undo()
    assert search_models(lookup, {"limit": 1})["ok"] is True

    # -- errors are fresh and non-reflecting on every call.
    first = search_models(lookup, {"limit": 0})
    first["error"]["parameter"] = "injected"
    first["error"]["code"] = "injected"
    first["ok"] = True
    second = search_models(lookup, {"limit": 0})
    assert second == _argument_envelope(INVALID_ARGUMENTS, "limit")
    assert second is not first
    assert lookup == lookup_before


# ---------- public metadata search: composition, pagination, isolation (3e) ---
#
# The public function's own page/projection behavior over synthetic, resealed,
# fully-validated registries: default and maximum pages, a population larger
# than any page, filter-before-slice, provider-crossing and exact-end pages,
# zero-ID and empty-registry success, staleness scope, cross-provider conflict
# rejection with same-pair positive controls, compact-row normalization and deep
# mutation isolation.


def _bulk_search_snapshot():
    """215 tracked pairs over three providers; provider ORDER differs from the
    providers-map insertion order and from alphabetic order."""
    omega = {}
    for index in range(120):
        raw_id = "om-%03d" % index
        omega[raw_id] = _search_model(
            raw_id, "Omega row %03d" % index,
            "Bulk population row for the omega gateway.",
            context_length=65536 if index >= 115 else 8192,
            input_modalities=["text"], output_modalities=["text"],
            supported_parameters=["temperature"])
    alpha = {}
    for index in range(95):
        raw_id = "al-%03d" % index
        alpha[raw_id] = _search_model(
            raw_id, "Alpha row %03d" % index,
            "Bulk population row for the alpha gateway.",
            context_length=8192, input_modalities=["text"],
            output_modalities=["text"], supported_parameters=["temperature"])
    providers = {
        "zero": _search_provider(
            {}, "Zero", "https://zero.example/v1/chat/completions"),
        "alpha": _search_provider(
            alpha, "Alpha", "https://alpha.example/v1/chat/completions"),
        "omega": _search_provider(
            omega, "Omega", "https://omega.example/v1/chat/completions"),
    }
    return _search_snapshot(["omega", "alpha", "zero"], providers)


def test_search_models_pages_filters_and_projection_controls():
    """Pages describe the sorted-then-sliced match set: default 5, maximum
    200, a 215-pair population with no hidden cap, provider-crossing and
    exact/beyond-end offsets, a filter that genuinely moves the window,
    zero-ID and empty-registry success, cross-provider conflicts with
    same-pair positive controls, compact-row normalization and deep mutation
    isolation."""
    from watchdog_catalog.query import search_models

    snapshot = _bulk_search_snapshot()
    before = copy.deepcopy(snapshot)
    assert snapshot["provider_order"] != list(snapshot["providers"])
    assert snapshot["provider_order"] != sorted(snapshot["provider_order"])
    omega_ids = ["om-%03d" % index for index in range(120)]
    alpha_ids = ["al-%03d" % index for index in range(95)]

    # Default page: 5 rows, provider order then natural raw-ID order.
    default = search_models(snapshot, {})
    _assert_search_strict_types(default, "default")
    assert default["total_matches"] == 215
    assert len(default["endpoints"]) == 5 and default["next_offset"] == 5
    assert [(row["provider"], row["model_id"])
            for row in default["endpoints"]] == \
        [("omega", raw_id) for raw_id in omega_ids[:5]]
    assert default["provider_staleness"] == {
        "omega": False, "alpha": False, "zero": False}

    # Maximum page and an over-200 population: no hidden stage cap or
    # truncation, and the page crosses the provider boundary exactly.
    max_page = search_models(snapshot, {"limit": 200})
    assert len(max_page["endpoints"]) == 200
    assert max_page["next_offset"] == 200
    assert max_page["endpoints"][119]["model_id"] == "om-119"
    assert max_page["endpoints"][120]["provider"] == "alpha"
    assert max_page["endpoints"][120]["model_id"] == "al-000"
    tail = search_models(snapshot, {"limit": 200, "offset": 200})
    assert len(tail["endpoints"]) == 15 and tail["next_offset"] is None
    assert [row["model_id"] for row in tail["endpoints"]] == alpha_ids[80:]
    assert tail["total_matches"] == 215

    # A page crossing the provider boundary mid-request.
    cross = search_models(snapshot, {"limit": 2, "offset": 119})
    assert [(row["provider"], row["model_id"])
            for row in cross["endpoints"]] == \
        [("omega", "om-119"), ("alpha", "al-000")]
    assert cross["next_offset"] == 121

    # Exact end, one-past end, far beyond end and the MAX_SAFE offset.
    exact_end = search_models(snapshot, {"limit": 2, "offset": 213})
    assert len(exact_end["endpoints"]) == 2
    assert exact_end["next_offset"] is None
    continuing = search_models(snapshot, {"limit": 2, "offset": 211})
    assert len(continuing["endpoints"]) == 2
    assert continuing["next_offset"] == 213
    for offset in (215, 999999, _catalog.MAX_SAFE_INT):
        page = search_models(snapshot, {"limit": 2, "offset": offset})
        assert page["endpoints"] == [], offset
        assert page["next_offset"] is None, offset
        assert page["total_matches"] == 215, offset
        assert page["provider_staleness"] == {
            "omega": False, "alpha": False, "zero": False}, offset

    # Filter BEFORE slice: the unfiltered first page holds no qualifying row,
    # and the filtered page starts at the LATER matching window.
    assert [row["model_id"] for row in default["endpoints"][:3]] == \
        omega_ids[:3]
    filtered = search_models(snapshot, {"min_context_length": 65536,
                                       "limit": 3})
    assert [row["model_id"] for row in filtered["endpoints"]] == \
        ["om-115", "om-116", "om-117"]
    assert filtered["total_matches"] == 5
    assert filtered["next_offset"] == 3

    # Zero-ID provider success keeps its own staleness scope; a true
    # zero-provider registry succeeds with empty scope, not an error.
    zero_stale = _search_snapshot(
        ["zero"], {"zero": _search_provider(
            {}, "Zero", "https://zero.example/v1/chat/completions",
            stale=True)})
    zero_page = search_models(zero_stale, {})
    assert zero_page["ok"] is True
    assert zero_page["endpoints"] == [] and zero_page["total_matches"] == 0
    assert zero_page["next_offset"] is None
    assert zero_page["provider_staleness"] == {"zero": True}
    empty_registry = _search_snapshot([], {})
    empty_page = search_models(empty_registry, {})
    assert empty_page == {
        "ok": True,
        "snapshot_id": empty_registry["snapshot_id"],
        "tick_epoch": empty_registry["tick_epoch"],
        "provider_staleness": {},
        "total_matches": 0,
        "next_offset": None,
        "endpoints": [],
    }

    # Natural/display order from the accepted selection controls.
    boundary = _boundary_snapshot()
    zeta_page = search_models(boundary, {"provider": "zeta", "limit": 200})
    assert [(row["provider"], row["model_id"])
            for row in zeta_page["endpoints"]] == SEARCH_ZETA_ORDER
    assert zeta_page["total_matches"] == len(SEARCH_ZETA_ORDER)
    assert boundary["provider_order"] != list(boundary["providers"])
    empty_selected = search_models(boundary, {"provider": "empty"})
    assert empty_selected["ok"] is True
    assert empty_selected["provider_staleness"] == {"empty": False}
    assert empty_selected["endpoints"] == []

    # -- cross-provider conflicts through the PUBLIC function, with same-pair
    # positive controls (no union by raw id, no borrowed capability).
    conflict_id = "vendor/conflict"
    conflict = _search_snapshot(
        ["gw-a", "gw-b"],
        {"gw-a": _search_provider(
             {conflict_id: _search_model(
                 conflict_id, "Conflict A", context_length=65536,
                 input_modalities=["text"], output_modalities=["text"],
                 supported_parameters=["temperature"])},
             "Gate A", "https://gw-a.example/v1/chat/completions"),
         "gw-b": _search_provider(
             {conflict_id: _search_model(
                 conflict_id, "Conflict B", context_length=8192,
                 input_modalities=["text", "image"],
                 output_modalities=["text"],
                 supported_parameters=["temperature"])},
             "Gate B", "https://gw-b.example/v1/chat/completions")})
    conflict_before = copy.deepcopy(conflict)
    neither = search_models(conflict, {"min_context_length": 65536,
                                      "input_modalities": ["image"]})
    assert neither["endpoints"] == [] and neither["total_matches"] == 0
    assert neither["next_offset"] is None
    assert neither["provider_staleness"] == {"gw-a": False, "gw-b": False}
    high_text = search_models(
        conflict, {"min_context_length": 65536, "input_modalities": ["text"],
                   "supported_parameters": ["temperature"]})
    assert [(row["provider"], row["model_id"])
            for row in high_text["endpoints"]] == [("gw-a", conflict_id)]
    low_image = search_models(
        conflict, {"min_context_length": 8192, "input_modalities": ["IMAGE"],
                   "supported_parameters": ["TEMPERATURE"]})
    assert [(row["provider"], row["model_id"])
            for row in low_image["endpoints"]] == [("gw-b", conflict_id)]

    split_id = "vendor/split"
    split = _search_snapshot(
        ["gw-a", "gw-b"],
        {"gw-a": _search_provider(
             {split_id: _search_model(
                 split_id, "Split A", context_length=65536,
                 input_modalities=["text"],
                 supported_parameters=["temperature"])},
             "Gate A", "https://gw-a.example/v1/chat/completions"),
         "gw-b": _search_provider(
             {split_id: _search_model(
                 split_id, "Split B", context_length=65536,
                 input_modalities=["text"],
                 supported_parameters=["max_tokens"])},
             "Gate B", "https://gw-b.example/v1/chat/completions")})
    split_before = copy.deepcopy(split)
    borrowed = search_models(
        split, {"supported_parameters": ["temperature", "max_tokens"]})
    assert borrowed["endpoints"] == [] and borrowed["total_matches"] == 0
    assert [(row["provider"], row["model_id"]) for row in search_models(
        split, {"supported_parameters": ["temperature"]})["endpoints"]] == \
        [("gw-a", split_id)]
    assert [(row["provider"], row["model_id"]) for row in search_models(
        split, {"supported_parameters": ["max_tokens"]})["endpoints"]] == \
        [("gw-b", split_id)]
    assert conflict == conflict_before and split == split_before

    # -- compact rows: normalized capabilities, no description/wiring/metadata,
    # and fresh mutable arrays on every call.
    cased = _search_model(
        "cased", "Cased Row", "Prose for the cased row.", context_length=100,
        input_modalities=["TEXT", "Image", "text"], output_modalities=[],
        supported_parameters=["Temperature", "TEMPERATURE"])
    unknown = _search_model("unknown", "Unknown Row")
    norm = _search_snapshot(
        ["norm"], {"norm": _search_provider(
            {"cased": cased, "unknown": unknown}, "Norm",
            "https://norm.example/v1/chat/completions")})
    norm_before = copy.deepcopy(norm)

    iso_args = {"input_modalities": ["TEXT", "text"],
                "supported_parameters": ["temperature"]}
    iso = search_models(norm, iso_args)
    assert [(row["provider"], row["model_id"])
            for row in iso["endpoints"]] == [("norm", "cased")]
    row = iso["endpoints"][0]
    assert set(row) == SEARCH_ROW_FIELDS
    assert row["name"] == "Cased Row"
    assert row["input_modalities"] == ["text", "image"]
    assert row["output_modalities"] == []
    assert row["supported_parameters"] == ["temperature"]
    assert "description" not in row
    assert "metadata" not in row
    assert "chat_completions_url" not in row

    unknown_row = search_models(
        norm, {"query": "unknown", "limit": 1})["endpoints"][0]
    assert unknown_row["context_length"] is None
    assert unknown_row["input_modalities"] is None
    assert unknown_row["output_modalities"] is None
    assert unknown_row["supported_parameters"] is None

    saved = copy.deepcopy(iso)
    iso["provider_staleness"]["norm"] = True
    iso["endpoints"][0]["input_modalities"].append("injected")
    iso["endpoints"].clear()
    assert search_models(norm, iso_args) == saved
    assert iso_args == {"input_modalities": ["TEXT", "text"],
                        "supported_parameters": ["temperature"]}
    assert norm == norm_before
    assert snapshot == before
