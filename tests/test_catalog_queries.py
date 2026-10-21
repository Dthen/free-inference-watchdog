"""Runtime tests for the pure raw-JSON boundary, typed canonical bytes (unit
2a1) and the strict public snapshot validator / hash provenance (unit 2a2).

These tests exercise ONLY the public runtime API of the side-effect-free
package root and its pure raw + structural + query modules:

    watchdog_catalog.catalog.MAX_SAFE_INT
    watchdog_catalog.catalog.MAX_JSON_DEPTH
    watchdog_catalog.catalog.CatalogError
    watchdog_catalog.catalog.load_json
    watchdog_catalog.catalog.canonical_bytes
    watchdog_catalog.catalog.INVALID_SNAPSHOT
    watchdog_catalog.catalog.UNSUPPORTED_SCHEMA_VERSION
    watchdog_catalog.catalog.validate_snapshot
    watchdog_catalog.catalog.load_snapshot
    watchdog_catalog.catalog.snapshot_hash
    watchdog_catalog.query.strip_free_marker

They read the frozen, independently-authored RULE TABLES by path
(``tests/fixtures/catalog/raw-json-cases.json``, ``canonical-cases.json`` and
``structural-cases.json``) plus the committed snapshot fixtures, and compare
each literal row against the runtime functions. They never import
``tests/test_catalog.py`` or any of its private checkers, and no production
module imports them.

The structural layer is exercised through a SMALL TEST-LOCAL path setter and
reseal helper built on independent ``json.dumps``/``hashlib`` calls: expected
envelopes come from the literal table rows, never from runtime-derived hashes,
and every fixture file stays byte-identical. Grouped loops keep the added test
count proportional to the coherent boundaries rather than to the individual
table rows.

The real package is imported normally at module level. The frozen
contract-only suite proves its own isolation in a fresh interpreter (with the
implementation package blocked) rather than by inspecting this process's
``sys.modules``, so this module needs no lazy import and must not use one to
keep the process-global module table clean.
"""

import collections
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from watchdog_catalog import catalog as _catalog

REPO = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"
RAW_TABLE_DOC = FIXTURE_DIR / "raw-json-cases.json"
CANONICAL_TABLE_DOC = FIXTURE_DIR / "canonical-cases.json"
STRUCTURAL_TABLE_DOC = FIXTURE_DIR / "structural-cases.json"

INVALID_JSON = "invalid_json"
INVALID_JSON_MESSAGE = "Invalid catalogue JSON."
INVALID_SNAPSHOT = "invalid_snapshot"
UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
STRUCTURAL_MESSAGES = {
    INVALID_SNAPSHOT: "Invalid catalogue snapshot.",
    UNSUPPORTED_SCHEMA_VERSION: "Unsupported catalogue schema version.",
}

# The nine committed public snapshot fixtures: every one of them is a valid
# publication and must be accepted, unchanged, by the structural layer.
SNAPSHOT_FIXTURES = (
    "basic.json",
    "capability-unknown.json",
    "lookup.json",
    "metadata-edge-cases.json",
    "ordering.json",
    "provider-extension.json",
    "search-text.json",
    "status-short-cadence.json",
    "status-unknown-time.json",
)

# The five strict-float snapshot-case rows and the literal path of the offending
# float token; this unit's ``load_snapshot`` classifies their
# ``invalid_snapshot`` envelope.
STRICT_FLOAT_PATH = {
    "snap_case_schema_version_float": ("schema_version",),
    "snap_case_tick_epoch_float": ("tick_epoch",),
    "snap_case_cadence_float": ("cadence_s",),
    "snap_case_context_length_float": (
        "providers", "synthetic-a", "models", "alpha-1:free", "context_length"),
    "snap_case_schema_version_exponent": ("schema_version",),
}
STRICT_INT_PATHS = (
    ("schema_version",),
    ("tick_epoch",),
    ("cadence_s",),
    ("providers", "synthetic-a", "models", "alpha-1:free", "context_length"),
)


def _load_table(path):
    path = Path(path)
    assert path.is_file(), f"rule table fixture is missing: {path.name}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw_table():
    return _load_table(RAW_TABLE_DOC)


@pytest.fixture(scope="module")
def canonical_table():
    return _load_table(CANONICAL_TABLE_DOC)


def _row(rows, case_id):
    return next(r for r in rows if r["id"] == case_id)


def _doc_at(doc, path):
    node = doc
    for component in path:
        node = node[component]
    return node


def _decoded_depth(value):
    """The container depth of a freshly decoded value: a root container is
    depth 1, each nested array/object adds 1, a scalar is depth 0."""
    if isinstance(value, dict):
        inner = [_decoded_depth(v) for v in value.values()] or [0]
        return 1 + max(inner)
    if isinstance(value, list):
        inner = [_decoded_depth(v) for v in value] or [0]
        return 1 + max(inner)
    return 0


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


# ---------- raw boundary: frozen rule-table rows ----------

def test_raw_invalid_rows_reject_with_the_single_literal_envelope(raw_table):
    assert raw_table["invalid_cases"], "the raw rule table must carry rows"
    assert len(raw_table["invalid_cases"]) == 35
    for row in raw_table["invalid_cases"]:
        cid = row["id"]
        err = _assert_invalid(row["json_text"], cid)
        # str() is the fixed public message, never decoder text or raw content.
        assert str(err) == INVALID_JSON_MESSAGE, cid
        assert err.to_payload() == row["expect"], cid
        # Exactly ok+error and exactly code+message; a fresh object each call.
        payload = err.to_payload()
        assert set(payload) == {"ok", "error"}, cid
        assert payload["ok"] is False, cid
        assert set(payload["error"]) == {"code", "message"}, cid
        assert err.to_payload() is not err.to_payload(), cid


def test_raw_valid_rows_decode_to_expected_typed_values(raw_table):
    assert raw_table["valid_controls"], "the raw rule table must carry rows"
    assert len(raw_table["valid_controls"]) == 27
    catalog = _catalog
    load_json = catalog.load_json
    for row in raw_table["valid_controls"]:
        cid = row["id"]
        decoded = load_json(row["json_text"])
        if "expect" in row:
            expected = row["expect"]
            assert decoded == expected, cid
            # Strict per-token type: an integral float token stays float.
            assert type(decoded) is type(expected), cid
        else:
            assert row["expect_depth"] == catalog.MAX_JSON_DEPTH, cid
            assert _decoded_depth(decoded) == catalog.MAX_JSON_DEPTH, cid
            if row["expect_kind"] == "object":
                assert isinstance(decoded, dict), cid
            else:
                assert isinstance(decoded, list), cid
    # Grouped strict-type pins: integral float tokens keep FLOAT type and a
    # signed float zero retains its sign.
    assert type(load_json("1.0")) is float
    assert type(load_json("1e0")) is float
    zero = load_json("-0.0")
    assert type(zero) is float
    assert zero == 0.0 and math.copysign(1.0, zero) == -1.0


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


# ---------- typed canonical bytes: frozen rule-table rows ----------

def test_canonical_vectors_match_the_literal_text_and_sha(canonical_table):
    catalog = _catalog
    load_json = catalog.load_json
    canonical_bytes = catalog.canonical_bytes
    assert canonical_table["canonical_vectors"], "the table must carry rows"
    assert len(canonical_table["canonical_vectors"]) == 14
    for row in canonical_table["canonical_vectors"]:
        cid = row["id"]
        decoded = load_json(row["json_text"])
        if row["scope"] == "snapshot":
            # The caller excludes ONLY the root snapshot_id.
            body = {k: v for k, v in decoded.items() if k != "snapshot_id"}
        else:
            body = decoded
        raw = canonical_bytes(body)
        assert raw == row["canonical_text"].encode("utf-8"), cid
        assert hashlib.sha256(raw).hexdigest() == row["sha256"], cid


def test_canonical_lexeme_types_and_float_priced_snapshot_twins(canonical_table):
    catalog = _catalog
    load_json = catalog.load_json
    canonical_bytes = catalog.canonical_bytes
    vec = {r["id"]: r for r in canonical_table["canonical_vectors"]}
    i1 = load_json(vec["vec_int_lexeme_one"]["json_text"])["n"]
    f1 = load_json(vec["vec_float_lexeme_one"]["json_text"])["n"]
    fe = load_json(vec["vec_float_lexeme_one_exponent"]["json_text"])["n"]
    assert type(i1) is int and type(f1) is float and type(fe) is float
    # Integer `1` and float `1.0` are DIFFERENT typed values.
    assert canonical_bytes({"n": i1}) != canonical_bytes({"n": f1})
    # Float `1.0` and float `1e0` share ONE typed value.
    assert canonical_bytes({"n": f1}) == canonical_bytes({"n": fe})
    # Escaped and literal escapes collapse to one canonical body.
    esc = load_json(vec["vec_equivalent_escapes_escaped"]["json_text"])
    lit = load_json(vec["vec_equivalent_escapes_literal"]["json_text"])
    assert canonical_bytes(esc) == canonical_bytes(lit)
    # A nested snapshot_id in a general_value vector is RETAINED content.
    nested = load_json(vec["vec_nested_snapshot_id"]["json_text"])
    assert b'"snapshot_id"' in canonical_bytes(nested)
    # Float-priced snapshot twins: raw-accepted, identical body and hash, and
    # float pricing kept as floats (never dropped or stringified).
    prod = load_json(vec["vec_snapshot_float_priced_producer"]["json_text"])
    alt = load_json(vec["vec_snapshot_float_priced_alternate"]["json_text"])
    prod_body = canonical_bytes(
        {k: v for k, v in prod.items() if k != "snapshot_id"})
    alt_body = canonical_bytes(
        {k: v for k, v in alt.items() if k != "snapshot_id"})
    assert prod_body == alt_body
    assert prod["snapshot_id"] == alt["snapshot_id"] == \
        hashlib.sha256(prod_body).hexdigest()
    price = prod["providers"]["synthetic-a"]["models"]["alpha-1:free"]["pricing"]
    assert type(price["prompt"]) is float
    assert type(price["completion"]) is float


def test_canonical_snapshot_cases_are_raw_accepted_with_hash_preconditions(
        canonical_table):
    catalog = _catalog
    load_json = catalog.load_json
    canonical_bytes = catalog.canonical_bytes
    assert canonical_table["snapshot_cases"], "the table must carry rows"
    assert len(canonical_table["snapshot_cases"]) == 7
    for row in canonical_table["snapshot_cases"]:
        cid = row["id"]
        decoded = load_json(row["json_text"])  # raw guard accepts the numbers
        assert isinstance(decoded, dict), cid
        recomputed = hashlib.sha256(canonical_bytes(
            {k: v for k, v in decoded.items() if k != "snapshot_id"})).hexdigest()
        if cid == "snap_case_hash_tamper":
            assert decoded["snapshot_id"] != recomputed, cid
        else:
            assert decoded["snapshot_id"] == recomputed, cid
        if "expect" in row:
            # Structural classification stays a SEPARATE layer: the raw layer
            # accepts these and produces NO envelope itself, and this unit's
            # load_snapshot is what composes raw acceptance with the structural
            # envelope asserted in the structural group below.
            assert row["expect"]["error"]["code"] == "invalid_snapshot", cid
    # The strict-token inputs load with FLOAT tokens intact (never coerced to
    # int) -- the precondition the structural layer classifies invalid_snapshot.
    for cid, path in STRICT_FLOAT_PATH.items():
        row = _row(canonical_table["snapshot_cases"], cid)
        value = _doc_at(load_json(row["json_text"]), path)
        assert type(value) is float, cid
        assert value == int(value), cid
    # The accepted base case carries strict INT tokens.
    base = _row(canonical_table["snapshot_cases"], "snap_case_integer_token_base")
    doc = load_json(base["json_text"])
    for path in STRICT_INT_PATHS:
        assert type(_doc_at(doc, path)) is int, path


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
# (json/hashlib); no ``tests/test_catalog.py`` private helper is imported and no
# production module is used to build the expected value.


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


def _apply_changes(snap, changes):
    for change in changes:
        op = change["op"]
        if op == "replace_root":
            snap = change["value"]
            continue
        path = change["path"]
        parent = snap
        for component in path[:-1]:
            parent = parent[component]
        if op == "delete":
            del parent[path[-1]]
        else:
            assert op == "set", op
            parent[path[-1]] = change["value"]
    return snap


def _structural_snapshot(row):
    snap = _apply_changes(_decode_doc(FIXTURE_DIR / row["base_fixture"]),
                          row["changes"])
    return _reseal(snap) if row["reseal"] else snap


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


@pytest.fixture(scope="module")
def structural_table():
    return _decode_doc(STRUCTURAL_TABLE_DOC)


def test_structural_error_literals_and_envelopes_are_frozen():
    catalog = _catalog
    assert catalog.INVALID_SNAPSHOT == INVALID_SNAPSHOT
    assert catalog.UNSUPPORTED_SCHEMA_VERSION == UNSUPPORTED_SCHEMA_VERSION
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


def test_structural_invalid_rows_reject_with_the_frozen_envelope(
        structural_table):
    rows = structural_table["invalid_cases"]
    assert len(rows) == 57
    for row in rows:
        snap = _structural_snapshot(row)
        if row["reseal"]:
            body = {k: v for k, v in snap.items() if k != "snapshot_id"}
            assert snap["snapshot_id"] == \
                hashlib.sha256(_canonical_text(body)).hexdigest(), row["id"]
        err = _assert_rejected(snap, row["expect"]["error"]["code"], row["id"])
        # The literal, independently-authored envelope is reproduced exactly.
        assert err.to_payload() == row["expect"], row["id"]


def test_structural_valid_controls_are_accepted_identically_and_unmutated(
        structural_table):
    rows = structural_table["valid_controls"]
    assert len(rows) == 10
    catalog = _catalog
    for row in rows:
        snap = _structural_snapshot(row)
        before = json.loads(json.dumps(snap))
        assert catalog.validate_snapshot(snap) is snap, row["id"]
        assert snap == before, row["id"]
        assert catalog.load_snapshot(json.dumps(snap)) == snap, row["id"]


def test_committed_snapshot_fixtures_validate_identically():
    catalog = _catalog
    for name in SNAPSHOT_FIXTURES:
        text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
        doc = catalog.load_json(text)
        before = json.loads(json.dumps(doc))
        assert catalog.validate_snapshot(doc) is doc, name
        assert doc == before, name
        assert catalog.load_snapshot(text) == doc, name


def test_canonical_snapshot_cases_classify_via_load_snapshot(canonical_table):
    rows = canonical_table["snapshot_cases"]
    assert len(rows) == 7
    catalog = _catalog
    base = _row(rows, "snap_case_integer_token_base")
    accepted = catalog.load_snapshot(base["json_text"])
    assert isinstance(accepted, dict) and base.get("expect") is None
    strict_float = 0
    for row in rows:
        if row["id"] == "snap_case_integer_token_base":
            continue
        if row["id"] in STRICT_FLOAT_PATH:
            strict_float += 1
        with pytest.raises(catalog.CatalogError) as excinfo:
            catalog.load_snapshot(row["json_text"])
        err = excinfo.value
        assert err.code == INVALID_SNAPSHOT, row["id"]
        assert err.to_payload() == row["expect"], row["id"]
    assert strict_float == 5
    # The tampered row is a genuine body/hash mismatch, not a shape accident.
    tamper = _row(rows, "snap_case_hash_tamper")
    doc = catalog.load_json(tamper["json_text"])
    body = {k: v for k, v in doc.items() if k != "snapshot_id"}
    assert doc["snapshot_id"] != catalog.snapshot_hash(body)


def test_raw_faults_precede_structural_validation(raw_table):
    catalog = _catalog
    for row in raw_table["invalid_cases"]:
        with pytest.raises(catalog.CatalogError) as excinfo:
            catalog.load_snapshot(row["json_text"])
        err = excinfo.value
        assert err.code == INVALID_JSON, row["id"]
        assert err.to_payload() == row["expect"], row["id"]


def test_raw_valid_documents_reach_the_structural_layer(raw_table):
    catalog = _catalog
    non_objects = 0
    for row in raw_table["valid_controls"]:
        decoded = catalog.load_json(row["json_text"])  # lexically accepted
        if not isinstance(decoded, dict):
            non_objects += 1
        with pytest.raises(catalog.CatalogError) as excinfo:
            catalog.load_snapshot(row["json_text"])
        assert excinfo.value.code == INVALID_SNAPSHOT, row["id"]
    assert non_objects, "the raw table must carry non-object roots"
    for text in ("null", "[]", "true", "42", "1.5", '"x"',
                 "-9007199254740991", "{}", '{"A":1,"a":2}'):
        _assert_rejected(catalog.load_json(text), INVALID_SNAPSHOT, text)


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

    # (d) A well-typed integer version != 1 is unsupported_schema_version EVEN
    #     with a malformed body and a wrong hash: the value check precedes
    #     every v1 body check and the hash.
    for value in (2, 0, -1, catalog.MAX_SAFE_INT):
        snap = _mutate(base, ("schema_version",), value)
        snap["providers"] = []
        snap["snapshot_id"] = "0" * 64
        _assert_rejected(snap, UNSUPPORTED_SCHEMA_VERSION, f"version {value}")

    # (e) Version 1: a malformed body, a wrong 64-hex hash and a malformed hash
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
        (model + ("pricing",), []),
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
        (model + ("pricing", "prompt"), float("nan")),
        (model + ("pricing", "prompt"), float("inf")),
        (model + ("pricing", "prompt"), catalog.MAX_SAFE_INT + 1),
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


def test_structural_pricing_literals_are_validated_but_never_normalized():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")
    path = ("providers", "synthetic-a", "models", "vendor/shared", "pricing")
    accepted = (
        None,
        {"prompt": 0.5, "completion": None},
        {"prompt": "0.001", "completion": "1e-5"},
        {"prompt": " 1.5 ", "completion": "1_000"},
        {"prompt": "-0", "completion": "+1.5"},
        {"prompt": "١٢٣", "completion": "１２３"},
        {"prompt": 9007199254740991, "completion": "9007199254740992"},
        {"prompt": 1.0, "completion": -0.0},
    )
    for pricing in accepted:
        snap = _mutate(base, path, pricing)
        _reseal(snap)
        assert catalog.validate_snapshot(snap) is snap, pricing
        stored = snap["providers"]["synthetic-a"]["models"]["vendor/shared"][
            "pricing"]
        assert stored == pricing, pricing
        if pricing is not None:
            for key in ("prompt", "completion"):
                assert type(stored[key]) is type(pricing[key]), pricing
                assert repr(stored[key]) == repr(pricing[key]), pricing
    rejected = (
        {"prompt": True, "completion": "0"},
        {"prompt": "abc", "completion": "0"},
        {"prompt": "nan", "completion": "0"},
        {"prompt": "inf", "completion": "0"},
        {"prompt": "1e400", "completion": "0"},
        {"prompt": float("nan"), "completion": "0"},
        {"prompt": float("inf"), "completion": "0"},
        {"prompt": [], "completion": "0"},
        {"prompt": {"a": 1}, "completion": "0"},
        {"prompt": "0"},
        {"prompt": "0", "completion": "0", "extra": "x"},
        "free",
        0,
    )
    for pricing in rejected:
        snap = _mutate(base, path, pricing)
        _reseal(snap)
        _assert_rejected(snap, INVALID_SNAPSHOT, f"pricing {pricing!r}")


def test_snapshot_hash_matches_literal_vectors_and_retains_nested_ids(
        canonical_table):
    catalog = _catalog
    vectors = {row["id"]: row for row in canonical_table["canonical_vectors"]}
    snapshot_scoped = [row for row in canonical_table["canonical_vectors"]
                       if row["scope"] == "snapshot"]
    assert len(snapshot_scoped) == 2
    for row in snapshot_scoped:
        doc = catalog.load_json(row["json_text"])
        body = {k: v for k, v in doc.items() if k != "snapshot_id"}
        assert '"snapshot_id"' not in row["canonical_text"], row["id"]
        assert catalog.snapshot_hash(doc) == row["sha256"], row["id"]
        assert catalog.snapshot_hash(body) == row["sha256"], row["id"]
        assert catalog.snapshot_hash(doc) == hashlib.sha256(
            row["canonical_text"].encode("utf-8")).hexdigest(), row["id"]
        # Sealing then setting the root id is not circular.
        sealed = dict(body)
        sealed["snapshot_id"] = catalog.snapshot_hash(body)
        assert catalog.snapshot_hash(sealed) == catalog.snapshot_hash(body)

    # ONLY the root snapshot_id is excluded: a NESTED one is content and stays
    # bound into the digest.
    nested = catalog.load_json(vectors["vec_nested_snapshot_id"]["json_text"])
    body = {k: v for k, v in nested.items() if k != "snapshot_id"}
    assert body == {"inner": {"snapshot_id": "cafe"}}
    assert catalog.snapshot_hash(nested) == hashlib.sha256(
        b'{"inner":{"snapshot_id":"cafe"}}').hexdigest()
    without_nested = {"inner": {}}
    assert catalog.snapshot_hash(nested) != catalog.snapshot_hash(
        without_nested)

    # A non-dict argument is a programming error, never a public code.
    for bad in ([], "x", None, 1, b"{}"):
        with pytest.raises(TypeError):
            catalog.snapshot_hash(bad)


# ---------- optional capability projection + separate normalization ----------
#
# `project_capabilities` (raw upstream fragment -> public capability shape +
# warnings) and `normalize_capabilities` (valid public capability projection ->
# query shape) are exercised against the SAME independently hand-authored
# `metadata-normalization.json` rule table that the contract suite's fixture
# checker pins. The literal row data is the authoritative expected value; the
# table is loaded with a plain ``json.loads`` (NOT the strict raw loader)
# because it deliberately carries an unsafe ``2**53`` context token, which the
# projection must classify unknown without ever coercing it.

NORMALIZATION_TABLE_DOC = FIXTURE_DIR / "metadata-normalization.json"

# The four normalized capability fields and their frozen order (unknown_fields
# names exactly these, in exactly this order).
CAPABILITY_FIELD_ORDER = (
    "context_length", "input_modalities", "output_modalities",
    "supported_parameters",
)
# The capability-only warning order (tick_epoch_unknown is snapshot-level and
# never arises here).
CAPABILITY_WARNING_ORDER = ("metadata_missing", "metadata_invalid")
PROJECTED_CAPABILITY_FIELDS = frozenset(
    {"context_length", "architecture", "supported_parameters"})
PROJECTED_ARCHITECTURE_FIELDS = frozenset(
    {"input_modalities", "output_modalities"})
NORMALIZED_CAPABILITY_FIELDS = frozenset(CAPABILITY_FIELD_ORDER) | {
    "unknown_fields"}


@pytest.fixture(scope="module")
def normalization_rows():
    doc = _load_table(NORMALIZATION_TABLE_DOC)
    return {row["id"]: row for row in doc["rows"]}


def test_project_capabilities_matches_every_literal_table_row(
        normalization_rows):
    catalog = _catalog
    assert len(normalization_rows) == 22, "the rule table must carry 22 rows"
    for cid, row in normalization_rows.items():
        raw = copy.deepcopy(row["raw"])
        before = copy.deepcopy(raw)
        projected, warnings = catalog.project_capabilities(raw)
        assert set(projected) == PROJECTED_CAPABILITY_FIELDS, cid
        assert set(projected["architecture"]) == \
            PROJECTED_ARCHITECTURE_FIELDS, cid
        assert projected == row["projected"], cid
        assert warnings == row["warnings"], cid
        assert raw == before, cid  # the raw fragment is never mutated


def test_normalize_capabilities_matches_every_literal_table_row(
        normalization_rows):
    catalog = _catalog
    assert len(normalization_rows) == 22, "the rule table must carry 22 rows"
    for cid, row in normalization_rows.items():
        projected = copy.deepcopy(row["projected"])
        before = copy.deepcopy(projected)
        normalized = catalog.normalize_capabilities(projected)
        assert set(normalized) == NORMALIZED_CAPABILITY_FIELDS, cid
        assert normalized == row["normalized"], cid
        assert normalized["unknown_fields"] == [
            field for field in CAPABILITY_FIELD_ORDER
            if normalized[field] is None], cid
        assert projected == before, cid  # never normalized in place
    # A valid full public MODEL (extra identity/display/pricing members) is a
    # valid projection input and those extra members are ignored.
    model = {
        "id": "vendor/example",
        "name": "Example",
        "description": "ignored",
        "context_length": 4096,
        "architecture": {"input_modalities": ["TEXT"],
                         "output_modalities": None},
        "supported_parameters": ["Temperature"],
        "pricing": {"prompt": "0", "completion": "0"},
    }
    before = copy.deepcopy(model)
    assert catalog.normalize_capabilities(model) == {
        "context_length": 4096,
        "input_modalities": ["text"],
        "output_modalities": None,
        "supported_parameters": ["temperature"],
        "unknown_fields": ["output_modalities"],
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
    # Unknown (null) and known-empty ([]) stay distinct: only the null field is
    # flagged unknown, and a known zero context is KNOWN.
    normalized = catalog.normalize_capabilities(
        {"context_length": 0,
         "architecture": {"input_modalities": [],
                          "output_modalities": None},
         "supported_parameters": []})
    assert normalized["context_length"] == 0
    assert normalized["input_modalities"] == []
    assert normalized["unknown_fields"] == ["output_modalities"]
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
    # Normalization returns new lists and a fresh unknown_fields each call.
    normalized = catalog.normalize_capabilities(projected2)
    normalized["input_modalities"].append("audio")
    normalized["unknown_fields"].append("context_length")
    again = catalog.normalize_capabilities(projected2)
    assert again["input_modalities"] == ["temperature"]
    assert again["unknown_fields"] == []
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
# `project_model` composes the accepted capability projection with the three
# identity/display/pricing members into ONE complete public model object. Every
# expected value below is an INDEPENDENT literal restatement of the frozen
# contract (the README projected-model field table), never derived from a
# runtime helper.

PUBLIC_MODEL_FIELDS = frozenset(
    {"id", "name", "description", "context_length", "architecture",
     "supported_parameters", "pricing"})
PUBLIC_ARCHITECTURE_FIELDS = frozenset(
    {"input_modalities", "output_modalities"})
PUBLIC_PRICING_FIELDS = frozenset({"prompt", "completion"})

# A complete, all-known upstream fragment used as the positive control.
_KNOWN_FRAGMENT = {
    "name": "Vendor Known",
    "description": "A known model.",
    "context_length": 4096,
    "architecture": {"input_modalities": ["text"],
                     "output_modalities": ["text"]},
    "supported_parameters": ["temperature"],
    "pricing": {"prompt": "0", "completion": 0},
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

    # (b) Always exactly the seven public fields.
    model, _ = project("vendor/key", _known_fragment())
    assert set(model) == PUBLIC_MODEL_FIELDS
    assert set(model["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS
    assert set(model["pricing"]) == PUBLIC_PRICING_FIELDS

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
        "pricing": None,
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
            "pricing": None,
        }, raw
        assert warnings == ["metadata_invalid"], raw


def test_project_model_pricing_projection_and_scalar_type_preservation():
    catalog = _catalog
    project = catalog.project_model

    def pricing_of(value):
        return project("k", _fragment(pricing=value))[0]["pricing"]

    # (a) Whole-object null when pricing is unknown overall: absent, explicit
    #     null or any non-object value.
    assert pricing_of(None) is None
    assert project("k", _fragment_without("pricing"))[0]["pricing"] is None
    for wrong in ("free", 0, 0.0, True, False, [], ["0"], ("x",)):
        assert pricing_of(wrong) is None, wrong

    # (b) A present pricing OBJECT always yields EXACTLY prompt+completion, even
    #     empty or both-null; extra upstream keys are dropped, never leaked.
    for value, expect in (
            ({}, {"prompt": None, "completion": None}),
            ({"prompt": None, "completion": None},
             {"prompt": None, "completion": None}),
            ({"prompt": "0", "completion": "0"},
             {"prompt": "0", "completion": "0"}),
            ({"prompt": "0", "completion": True},
             {"prompt": "0", "completion": None}),
            ({"prompt": "0.5", "completion": "abc"},
             {"prompt": "0.5", "completion": None}),
            ({"prompt": "0", "completion": "0", "image": "0", "request": 1},
             {"prompt": "0", "completion": "0"})):
        got = pricing_of(value)
        assert got == expect, value
        assert set(got) == PUBLIC_PRICING_FIELDS, value

    # (c) An invalid individual scalar degrades to null; a valid SIBLING
    #     survives unchanged.
    invalid = (True, False, float("nan"), float("inf"), float("-inf"),
               9007199254740992, -9007199254740992, 9007199254740992.0,
               "abc", "nan", "inf", "1e400", [], {}, ["0"])
    for bad in invalid:
        got = pricing_of({"prompt": bad, "completion": "0.5"})
        assert got["prompt"] is None, bad
        assert got["completion"] == "0.5", bad
        assert type(got["completion"]) is str, bad

    # (d) Valid scalars keep their ORIGINAL TYPE and spelling: int 1, float 1.0
    #     and string "1" are three DISTINCT retained values (equality alone
    #     cannot pin this -- assert type identity). No coercion, no zero default.
    got = pricing_of({"prompt": 1, "completion": 1.0})
    assert got["prompt"] == 1 and type(got["prompt"]) is int
    assert got["completion"] == 1.0 and type(got["completion"]) is float
    got = pricing_of({"prompt": "1", "completion": "0"})
    assert got["prompt"] == "1" and type(got["prompt"]) is str
    assert type(got["completion"]) is str
    # None vs 0 vs 0.0 vs "0" stay four DISTINCT values.
    got = pricing_of({"prompt": None, "completion": 0})
    assert got["prompt"] is None
    assert got["completion"] == 0 and type(got["completion"]) is int
    got = pricing_of({"prompt": 0.0, "completion": "0"})
    assert got["prompt"] == 0.0 and type(got["prompt"]) is float
    assert got["completion"] == "0" and type(got["completion"]) is str
    # Finite numeric-string spellings are kept VERBATIM (no trim, no rewrite):
    # the permissive float-probe edge is preserved, never tightened.
    for spelling in (" 1.5 ", "1_000", "\u0661\u0662\u0663", "-0", "+1.5",
                     "1e-5", "0.001"):
        got = pricing_of({"prompt": spelling, "completion": spelling})
        assert got["prompt"] == spelling, spelling
        assert type(got["prompt"]) is str, spelling
    # Negative finite prices are accepted (the existing predicate, unchanged).
    got = pricing_of({"prompt": -1.5, "completion": "-0.5"})
    assert got["prompt"] == -1.5 and type(got["prompt"]) is float
    assert got["completion"] == "-0.5"
    # No free-price inference: two unknown scalars stay null, never 0.
    assert pricing_of({"prompt": "abc", "completion": "abc"}) == {
        "prompt": None, "completion": None}


def test_project_model_composes_capabilities_and_isolates_warnings(
        normalization_rows):
    catalog = _catalog
    project = catalog.project_model
    assert len(normalization_rows) == 22

    # (a) Every literal capability row, projected through project_model with an
    #     explicit synthetic tracked id, reproduces its capability fragment, its
    #     warnings and its normalized fields EXACTLY. Each row's `raw` carries
    #     only capability members, so its warnings are the model's warnings.
    for cid, row in normalization_rows.items():
        model, warnings = project("synthetic/tracked-id", row["raw"])
        assert model["context_length"] == row["projected"]["context_length"], cid
        assert model["architecture"] == row["projected"]["architecture"], cid
        assert model["supported_parameters"] == \
            row["projected"]["supported_parameters"], cid
        assert warnings == row["warnings"], cid
        assert catalog.normalize_capabilities(model) == row["normalized"], cid

    # (b) Warning isolation: with all FOUR capabilities known, a missing,
    #     malformed or null name/description/pricing NEVER contributes a code.
    complete = {
        "context_length": 4096,
        "architecture": {"input_modalities": ["text"],
                         "output_modalities": ["text"]},
        "supported_parameters": ["temperature"],
    }
    for extra in ({}, {"name": None}, {"name": 5}, {"description": 5},
                  {"description": None}, {"pricing": "free"},
                  {"pricing": None}, {"pricing": {"prompt": True}},
                  {"name": [], "description": {}, "pricing": []}):
        model, warnings = project("k", dict(complete, **extra))
        assert warnings == [], extra
        assert set(model) == PUBLIC_MODEL_FIELDS, extra

    # (c) Mixed missing/invalid capability data warns in the frozen order, each
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
    pricing = {"prompt": "0", "completion": "0"}
    raw = {
        "name": "N",
        "description": "D",
        "context_length": 4096,
        "architecture": {"input_modalities": shared,
                         "output_modalities": shared},
        "supported_parameters": shared,
        "pricing": pricing,
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
    assert model["pricing"] is not pricing
    # Ignored extras (including nested extras) never leak into the projection.
    assert set(model) == PUBLIC_MODEL_FIELDS
    assert set(model["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS
    assert set(model["pricing"]) == PUBLIC_PRICING_FIELDS
    # Mutate the returned collections: input and a fresh call are unaffected.
    model["architecture"]["input_modalities"].append("vision")
    model["supported_parameters"].append("seed")
    model["pricing"]["prompt"] = "999"
    warnings.append("metadata_invalid")
    assert raw == before
    again, again_warnings = project("vendor/shared", raw)
    assert again["architecture"]["input_modalities"] == ["temperature"]
    assert again["supported_parameters"] == ["temperature"]
    assert again["pricing"] == {"prompt": "0", "completion": "0"}
    assert again_warnings == []
    # Nested extras on architecture/pricing are dropped, never interpreted.
    nested = project("k", _fragment(
        architecture={"input_modalities": ["text"],
                      "output_modalities": ["text"], "extra": 1},
        pricing={"prompt": "0", "completion": "0", "image": "0"}))[0]
    assert set(nested["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS
    assert set(nested["pricing"]) == PUBLIC_PRICING_FIELDS

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


# ---------- pure free-marker helper: literal corpus + compatibility edges ----------
#
# `watchdog_catalog.query.strip_free_marker` is the pure, stdlib-only marker
# helper extracted from build_site. The 17 expected values below are the
# INDEPENDENTLY authored rows of cases.json's `marker_strip_rule` table (never a
# runtime helper or another test module); the fallback controls and compatibility
# edges are literal characterizations of the frozen README contract.

MARKER_CASES_DOC = FIXTURE_DIR / "cases.json"

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


def test_strip_free_marker_matches_literal_corpus_and_compat_edges():
    """The pure helper reproduces all 17 frozen marker rows unchanged and holds
    the delimiter-only empty-result guard plus the characterized regex edges."""
    from watchdog_catalog.query import strip_free_marker

    rows = _load_table(MARKER_CASES_DOC)["marker_strip_rule"]
    # Count and uniqueness are asserted so a silently dropped or duplicated row
    # cannot shrink the loop and still pass.
    assert len(rows) == 17, len(rows)
    assert len({row["raw_id"] for row in rows}) == 17
    for row in rows:
        raw_id, stripped = row["raw_id"], row["stripped"]
        assert strip_free_marker(raw_id) == stripped, (
            f"marker rule mismatch for {raw_id!r}: want {stripped!r}")
    for raw_id, want in MARKER_FALLBACK_CONTROLS.items():
        assert strip_free_marker(raw_id) == want, (
            f"fallback control mismatch for {raw_id!r}: want {want!r}")
    for raw_id, want in MARKER_COMPAT_EDGES.items():
        assert strip_free_marker(raw_id) == want, (
            f"compat edge mismatch for {raw_id!r}: want {want!r}")


# ---------- pure ordering primitives: natural raw-ID + provider precedence ----------
#
# `natural_key` / `endpoint_key` are the two pure ordering primitives of the
# frozen README rule (README.md, "Total ordering rule" and "Natural comparator"):
# provider precedence is the index in `provider_order`, and within one provider
# raw IDs compare by maximal ASCII digit runs (ORIGINAL digit count, then lexical
# ASCII digits), case-sensitive Unicode code-point text, digit runs before text,
# empty boundary text tokens preserved and a strict prefix first.
#
# Every expected order below is INDEPENDENT LITERAL data: the seven rows of
# `ordering-cases.json` (hand-authored before any implementation) plus the literal
# `provider_order`/golden arrays of `cases.json`. No expectation is generated by a
# reference implementation, and `tests/test_catalog.py`'s private
# `_natural_key_contract` is never imported.

ORDERING_VECTOR_DOC = FIXTURE_DIR / "ordering-cases.json"

# Literal provider precedence: the fixture's `provider_order` array. It is
# asserted equal to the fixture field BEFORE any golden map is consulted, and the
# golden provider arrays are then flattened in THIS order -- never by iterating
# the golden object's own (meaningless) key order.
PROVIDER_PRECEDENCE = ("synthetic-b", "synthetic-a", "synthetic-c")
# The fixture's PHYSICAL provider-map insertion order: deliberately NOT the query
# order, and used as the input order the helper has to sort.
PROVIDER_MAP_INSERTION = ("synthetic-a", "synthetic-b", "synthetic-c")

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


def test_natural_key_matches_every_literal_ordering_vector_and_control():
    """The natural comparator reproduces all seven frozen vector rows from their
    literal input/expected arrays -- forward and reverse-supplied -- plus the
    literal empty/strict-prefix/multi-run/no-trim controls, without mutating the
    inputs and without ever raising a heterogeneous comparison TypeError."""
    from watchdog_catalog.query import natural_key

    rows = _load_table(ORDERING_VECTOR_DOC)["cases"]
    assert len(rows) == 7, len(rows)
    assert len({row["id"] for row in rows}) == 7
    corpus = set()
    for row in rows:
        cid = row["id"]
        supplied, expected = row["input"], row["expected"]
        assert len(supplied) == len(expected), cid
        # Guard: every vector is a genuine SORT, never an identity order.
        assert supplied != expected, cid
        before = copy.deepcopy(supplied)
        assert sorted(supplied, key=natural_key) == expected, cid
        assert supplied == before, cid  # the input list is never reordered
        # Reverse-supplied control: the same literal expectation still holds.
        assert sorted(list(reversed(supplied)), key=natural_key) == expected, cid
        corpus.update(supplied)
        corpus.update(expected)

    for supplied, expected in NATURAL_KEY_CONTROLS:
        assert sorted(supplied, key=natural_key) == expected, supplied
        corpus.update(supplied)

    # The frozen snapshot order is the OPPOSITE literal code-point order for the
    # divergence row: item02 precedes item1 there and follows it naturally.
    assert sorted(["vendor/item02", "vendor/item1"]) == \
        ["vendor/item02", "vendor/item1"]

    # Cross-vector comparability sweep: every key in the corpus compares with
    # every other (no heterogeneous TypeError), equal strings give equal keys and
    # the key order is one consistent total order over the whole corpus.
    assert len(corpus) >= 20, len(corpus)
    ordered = sorted(corpus, key=natural_key)
    assert [natural_key(raw_id) for raw_id in ordered] == \
        sorted(natural_key(raw_id) for raw_id in corpus)
    for left, right in zip(ordered, ordered[1:]):
        assert natural_key(left) <= natural_key(right), (left, right)
    assert natural_key("") == natural_key("")
    assert natural_key("vendor/item1") == natural_key("vendor/item1")


def test_endpoint_key_orders_by_provider_precedence_then_natural_raw_id():
    """`endpoint_key` ranks by the caller's `provider_order` index and then by the
    natural raw-ID order: all ten pairs of the ordering fixture reproduce the
    literal golden arrays from physical (wrong-order) input, forward and
    reversed; a shared raw ID stays two pairs; the zero-id provider is tolerated;
    and the snapshot data is never reordered."""
    from watchdog_catalog.query import endpoint_key

    ordering = _decode_doc(FIXTURE_DIR / "ordering.json")
    cases = {row["id"]: row for row in _load_table(MARKER_CASES_DOC)["cases"]}

    def pair_key(pair):
        return endpoint_key(ordering["provider_order"], pair[0], pair[1])

    # (a) Declared precedence is the literal array, the physical provider-map
    #     insertion order is a DIFFERENT order, and precedence is the supplied
    #     index -- reversing that list reverses provider precedence.
    assert ordering["provider_order"] == list(PROVIDER_PRECEDENCE)
    assert list(ordering["providers"]) == list(PROVIDER_MAP_INSERTION)
    assert list(ordering["providers"]) != list(PROVIDER_PRECEDENCE)
    flipped = ["synthetic-c", "synthetic-a", "synthetic-b"]
    assert endpoint_key(flipped, "synthetic-a", "vendor/item2") < \
        endpoint_key(flipped, "synthetic-b", "vendor/item2")

    golden = cases["list_free_ordering_all"]["expect"]["providers"]
    assert set(golden) == set(PROVIDER_PRECEDENCE)
    expected_pairs = [(provider, raw_id)
                      for provider in PROVIDER_PRECEDENCE
                      for raw_id in golden[provider]]
    assert len(expected_pairs) == 10, len(expected_pairs)
    assert len(set(expected_pairs)) == 10

    # (b) Snapshot model_ids stay in literal code-point order while the query
    #     order genuinely differs.
    for provider in PROVIDER_PRECEDENCE:
        ids = ordering["providers"][provider]["model_ids"]
        assert ids == sorted(ids), provider
    snapshot_b = ordering["providers"]["synthetic-b"]["model_ids"]
    natural_b = golden["synthetic-b"]
    assert snapshot_b.index("vendor/item02") < snapshot_b.index("vendor/item1")
    assert natural_b.index("vendor/item02") > natural_b.index("vendor/item1")

    # (c) Input pairs come from the ACTUAL physical snapshot order (provider-map
    #     insertion order, stored lexical model_ids), which is NOT the expected
    #     order; sorting with the helper -- forward and reversed -- reproduces the
    #     literal golden arrays, and nothing in the snapshot is touched.
    physical_pairs = [(provider, raw_id)
                      for provider in ordering["providers"]
                      for raw_id in ordering["providers"][provider]["model_ids"]]
    assert len(physical_pairs) == 10
    assert physical_pairs == [(provider, raw_id)
                              for provider in PROVIDER_MAP_INSERTION
                              for raw_id in ordering["providers"][provider]["model_ids"]]
    assert physical_pairs != expected_pairs  # guard: the input is unsorted
    providers_before = copy.deepcopy(ordering["providers"])
    order_before = copy.deepcopy(ordering["provider_order"])
    assert sorted(physical_pairs, key=pair_key) == expected_pairs
    assert sorted(list(reversed(physical_pairs)), key=pair_key) == expected_pairs
    assert ordering["providers"] == providers_before
    assert ordering["provider_order"] == order_before
    # Provider precedence is NOT alphabetic key order: the sorted provider
    # sequence is b...b, a, where the alphabetical one would be a, b, c.
    assert [pair[0] for pair in sorted(physical_pairs, key=pair_key)] != \
        sorted(pair[0] for pair in physical_pairs)
    # Within-provider natural order for the whole nine-id provider.
    assert sorted([("synthetic-b", raw_id) for raw_id in snapshot_b],
                  key=pair_key) == [("synthetic-b", raw_id)
                                    for raw_id in natural_b]

    # (d) A raw id shared by two providers stays TWO sortable pairs (never
    #     deduped) and orders by provider precedence; the get_model golden's
    #     literal endpoint order is the control.
    shared = cases[
        "get_model_ordering_identical_raw_id_provider_precedence"]["expect"]
    assert shared["exact_gateways"] == ["synthetic-b", "synthetic-a"]
    assert [endpoint["provider"] for endpoint in shared["endpoints"]] == \
        ["synthetic-b", "synthetic-a"]
    sorted_shared = sorted(
        [("synthetic-a", "vendor/item2"), ("synthetic-b", "vendor/item2")],
        key=pair_key)
    assert sorted_shared == [("synthetic-b", "vendor/item2"),
                             ("synthetic-a", "vendor/item2")]
    assert [pair[0] for pair in sorted_shared] == \
        [endpoint["provider"] for endpoint in shared["endpoints"]]
    assert len(sorted_shared) == 2 and len(set(sorted_shared)) == 2
    assert pair_key(sorted_shared[0]) != pair_key(sorted_shared[1])

    # (e) The existing search page is the provider's natural order seen through a
    #     TEST-ONLY offset/limit slice: query filtering and pagination runtime is
    #     deferred, so no query algorithm is implemented here.
    page = cases["search_ordering_provider_b_natural_page"]["expect"]
    assert (page["offset"], page["limit"]) == (1, 2)
    assert [(endpoint["provider"], endpoint["model_id"])
            for endpoint in page["endpoints"]] == \
        [("synthetic-b", "vendor/item2"), ("synthetic-b", "vendor/item02")]
    b_pairs = sorted([("synthetic-b", raw_id) for raw_id in snapshot_b],
                     key=pair_key)
    assert b_pairs[page["offset"]:page["offset"] + page["limit"]] == \
        [(endpoint["provider"], endpoint["model_id"])
         for endpoint in page["endpoints"]]

    # (f) The zero-id provider contributes no pair, its rank is still tolerated
    #     and sorts last; identical pairs give equal keys and a stable sort keeps
    #     the original references.
    assert golden["synthetic-c"] == []
    assert ordering["providers"]["synthetic-c"]["model_ids"] == []
    assert pair_key(("synthetic-b", "vendor/item1")) < \
        pair_key(("synthetic-c", "vendor/absent"))
    assert pair_key(("synthetic-a", "vendor/item2")) < \
        pair_key(("synthetic-c", "vendor/absent"))
    duplicates = [{"provider": "synthetic-b", "model_id": "vendor/item1"},
                  {"provider": "synthetic-b", "model_id": "vendor/item1"}]

    def record_key(record):
        return endpoint_key(ordering["provider_order"], record["provider"],
                            record["model_id"])

    assert record_key(duplicates[0]) == record_key(duplicates[1])
    stable = sorted(duplicates, key=record_key)
    assert stable[0] is duplicates[0] and stable[1] is duplicates[1]


# ---------- pure list_free_models: bulk/single success goldens ----------
#
# `watchdog_catalog.query.list_free_models(snapshot, arguments)` is the pure
# core of the frozen BULK `list_free_models` tool. It takes an already-VALIDATED
# public snapshot (the load_snapshot / source boundary owns validation -- this
# core performs no re-hash, no I/O and no clock) plus the whole DECODED argument
# object, and returns either the frozen success payload or the shared
# argument-error envelope. It never raises into the semantic caller for a
# malformed argument and never mutates its inputs.
#
# Every expected payload below is the INDEPENDENTLY authored `cases.json` golden
# for the `list_free_models` tool (hand-written from the contract, never from an
# implementation), and the two exact top-level field sets are the frozen README
# tables (README.md, "Success envelope -- list_free_models").

LIST_FREE_CASES_DOC = FIXTURE_DIR / "cases.json"

# The two DIFFERENT exact top-level field sets, one per variant.
LIST_FREE_ALL_FIELDS = frozenset({
    "ok", "providers", "models", "counts", "total_ids", "n_gateways",
    "snapshot_id", "tick_epoch", "provider_staleness"})
LIST_FREE_ONE_FIELDS = frozenset({
    "ok", "provider", "model_ids", "models", "count", "snapshot_id",
    "tick_epoch", "provider_staleness"})


def _list_free_cases(doc):
    return [case for case in doc["cases"] if case["tool"] == "list_free_models"]


def _validated_snapshot(name):
    """The load_snapshot-validated precondition snapshot for a fixture name."""
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return _catalog.load_snapshot(text)


def test_list_free_models_success_goldens_match_every_literal_case():
    """All five frozen `list_free_models` goldens reproduce EXACTLY -- deep
    semantic equality plus the exact top-level field set per variant -- with
    strict scalar types, omission/null equivalence on one shared fixture, the
    registered zero-ID success and a compact empty-registry control."""
    from watchdog_catalog.query import list_free_models

    doc = _decode_doc(LIST_FREE_CASES_DOC)
    cases = _list_free_cases(doc)
    # Count and id uniqueness are asserted so a dropped or duplicated golden
    # cannot shrink the loop and still pass.
    assert len(cases) == 5, len(cases)
    assert len({case["id"] for case in cases}) == 5
    all_provider = 0
    one_provider = 0
    for case in cases:
        cid = case["id"]
        # Per-case fixture override; NEVER a wrong-fixture fallback.
        fixture = case.get("fixture", doc["fixture"])
        snapshot = _validated_snapshot(fixture)
        before = copy.deepcopy(snapshot)
        result = list_free_models(snapshot, case["args"])
        expect = case["expect"]
        # Deep semantic equality AND the exact top-level field set.
        assert result == expect, cid
        assert set(result) == set(expect), cid
        if "providers" in expect:
            all_provider += 1
            assert set(result) == LIST_FREE_ALL_FIELDS, cid
            assert "provider" not in result and "model_ids" not in result, cid
        else:
            one_provider += 1
            assert set(result) == LIST_FREE_ONE_FIELDS, cid
            for leaked in ("providers", "counts", "total_ids", "n_gateways"):
                assert leaked not in result, (cid, leaked)
        # Strict scalar types: ok is exactly True; every count is a strict
        # non-negative int (a bool is never a count); every stale flag a bool.
        assert result["ok"] is True, cid
        assert type(result["snapshot_id"]) is str, cid
        assert result["snapshot_id"] == snapshot["snapshot_id"], cid
        assert result["tick_epoch"] == snapshot["tick_epoch"], cid
        for flag in result["provider_staleness"].values():
            assert type(flag) is bool, cid
        if "count" in result:
            assert type(result["count"]) is int, cid
            assert result["count"] >= 0, cid
        for value in result.get("counts", {}).values():
            assert type(value) is int, cid
            assert value >= 0, cid
        for key in ("total_ids", "n_gateways"):
            if key in result:
                assert type(result[key]) is int, cid
        assert snapshot == before, cid  # never mutated
    # Two all-provider goldens and three one-provider goldens, every one seen.
    assert (all_provider, one_provider) == (2, 3), (all_provider, one_provider)

    # Omission (`{}`) and explicit `{"provider": None}` are the SAME all-provider
    # export, asserted on ONE shared fixture (the two goldens above use
    # different fixtures, so this equivalence needs its own control).
    lookup = _validated_snapshot("lookup.json")
    omitted = list_free_models(lookup, {})
    null = list_free_models(lookup, {"provider": None})
    assert omitted == null
    assert set(omitted) == LIST_FREE_ALL_FIELDS

    # A registered zero-ID provider is a success with empty containers.
    zero = list_free_models(lookup, {"provider": "synthetic-c"})
    assert zero["model_ids"] == [] and zero["models"] == {} and zero["count"] == 0
    assert zero["provider_staleness"] == {"synthetic-c": False}

    # A legitimately EMPTY registry is a valid all-provider control, built from
    # an existing valid fixture and resealed with the test-local json/hashlib
    # oracle, then re-validated before the query.
    empty = _decode_doc(FIXTURE_DIR / "lookup.json")
    empty["provider_order"] = []
    empty["providers"] = {}
    _reseal(empty)
    assert _catalog.validate_snapshot(empty) is empty
    whole = list_free_models(empty, {})
    assert set(whole) == LIST_FREE_ALL_FIELDS
    assert whole["providers"] == {} and whole["models"] == {}
    assert whole["counts"] == {} and whole["provider_staleness"] == {}
    assert whole["total_ids"] == 0 and whole["n_gateways"] == 0
    assert whole["ok"] is True


# ---------- pure list_free_models: the provider argument boundary ----------
#
# The four `list_free_models` invalid rows of `argument-cases.json` carry the
# COMPLETE literal semantic envelope; the three valid controls record
# acceptance only. The programmatic rows below are compact literal restatements
# of the frozen README rules (object -> unknown-key -> provider type ->
# provider membership), including the unknown-key precedence that makes
# `parameter` null even when the provider itself would also be wrong.

LIST_FREE_ARG_DOC = FIXTURE_DIR / "argument-cases.json"

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


def test_list_free_models_argument_boundary_matches_literal_rows_and_rules():
    """The four frozen invalid rows and three valid controls reproduce their
    literal classification, and the compact programmatic rows pin the frozen
    object -> unknown-key -> provider type -> membership precedence, the
    no-coercion provider rules and the fresh non-reflecting envelope."""
    from watchdog_catalog.query import list_free_models

    doc = _decode_doc(LIST_FREE_ARG_DOC)
    invalid = [row for row in doc["invalid_cases"]
               if row["tool"] == "list_free_models"]
    valid = [row for row in doc["valid_controls"]
             if row["tool"] == "list_free_models"]
    assert len(invalid) == 4, [row["id"] for row in invalid]
    assert {row["id"] for row in invalid} == {
        "unknown_arg_list_free_models", "unknown_provider_list_free_models",
        "provider_wrong_type", "provider_wrong_case"}
    assert len(valid) == 3, [row["id"] for row in valid]

    for row in invalid:
        cid = row["id"]
        snapshot = _validated_snapshot(row["fixture"])
        args = copy.deepcopy(row["args"])
        before = copy.deepcopy(args)
        result = list_free_models(snapshot, args)
        # The literal, independently-authored envelope is reproduced exactly.
        assert result == row["expect"], cid
        assert args == before, cid  # the argument object is never mutated
        error = result["error"]
        assert result["ok"] is False, cid
        assert set(result) == {"ok", "error"}, cid
        assert set(error) == {"code", "message", "parameter"}, cid
        # No hostile key/value reflection and no extra provenance field.
        for hostile in ("tool", "state_root", "source", "path", "cache",
                        "snapshot_id", "count", "partial", "staleness"):
            assert hostile not in result and hostile not in error, (cid, hostile)
        assert error["code"] in ARGUMENT_MESSAGES, cid

    # The three valid controls are ACCEPTED (ok True); the complete success
    # oracles are the five independent goldens above, so this pins acceptance.
    for row in valid:
        cid = row["id"]
        snapshot = _validated_snapshot(row["fixture"])
        result = list_free_models(snapshot, copy.deepcopy(row["args"]))
        assert result["ok"] is True, cid

    # A whole argument object that is not a JSON object is invalid_arguments/
    # null -- None, a list, a number, a string, a bool, a tuple and bytes.
    lookup = _validated_snapshot("lookup.json")
    for args in (None, [], 5, "x", False, 1.0, (), b"{}"):
        _assert_argument_envelope(list_free_models(lookup, args),
                                  INVALID_ARGUMENTS, None, repr(args))

    # A non-null provider of the wrong type (bool/int/float/list/dict) is
    # invalid_arguments(provider); empty/padded/wrong-case/absent strings are
    # unknown_provider(provider) -- never coerced, trimmed or case-repaired.
    for bad in (True, False, 5, 1.5, [], ["synthetic-a"], {}, {"a": 1}):
        _assert_argument_envelope(list_free_models(lookup, {"provider": bad}),
                                  INVALID_ARGUMENTS, "provider", repr(bad))
    for bad in ("", " synthetic-a ", "synthetic-a ", " synthetic-a",
                "Synthetic-A", "SYNTHETIC-A", "synthetic-z", "unknown"):
        _assert_argument_envelope(list_free_models(lookup, {"provider": bad}),
                                  UNKNOWN_PROVIDER, "provider", bad)

    # Unknown-key precedence: ANY unknown key rejects with parameter null BEFORE
    # the provider type or membership check, so an unknown key combined with a
    # wrong-typed provider AND with an unknown provider both yield null.
    _assert_argument_envelope(
        list_free_models(lookup, {"provider": 5, "bogus": 1}),
        INVALID_ARGUMENTS, None, "unknown-key before provider type")
    _assert_argument_envelope(
        list_free_models(lookup, {"provider": "synthetic-z", "bogus": 1}),
        INVALID_ARGUMENTS, None, "unknown-key before provider membership")
    _assert_argument_envelope(
        list_free_models(lookup, {"bogus": 1}),
        INVALID_ARGUMENTS, None, "unknown-key alone")
    _assert_argument_envelope(
        list_free_models(lookup, {"provider": "synthetic-a", "bogus": 1}),
        INVALID_ARGUMENTS, None, "unknown-key with a valid provider")

    # `{}` and `{"provider": None}` are accepted, and the error envelope is a
    # FRESH object on every call (never a shared module-level constant).
    assert list_free_models(lookup, {})["ok"] is True
    assert list_free_models(lookup, {"provider": None})["ok"] is True
    first = list_free_models(lookup, {"provider": "synthetic-z"})
    first["error"]["code"] = "mutated"
    first["ok"] = True
    second = list_free_models(lookup, {"provider": "synthetic-z"})
    assert second == _argument_envelope(UNKNOWN_PROVIDER, "provider")
    assert second is not first


# ---------- pure list_free_models: freshness + deep output isolation ----------
#
# A public output is never a window onto the cached snapshot: every container,
# array and selected projected model object (with its nested architecture /
# parameter / pricing containers) is freshly allocated, so a caller mutating a
# returned payload can never corrupt the snapshot or another call.

def test_list_free_models_outputs_are_fresh_and_deeply_isolated():
    from watchdog_catalog.query import list_free_models

    lookup = _validated_snapshot("lookup.json")
    before = copy.deepcopy(lookup)

    # (a) All-provider form: fresh containers/arrays, and each selected model
    #     object and its nested containers unaliased to the snapshot and to a
    #     second call.
    whole = list_free_models(lookup, {})
    again = list_free_models(lookup, {})
    assert lookup == before
    assert whole == again
    assert whole["providers"] is not lookup["providers"]
    assert whole["models"] is not lookup["providers"]
    assert whole["counts"] is not lookup["providers"]
    assert whole["provider_staleness"] is not lookup["providers"]
    for provider, ids in whole["providers"].items():
        source = lookup["providers"][provider]
        assert ids is not source["model_ids"], provider
        assert whole["models"][provider] is not source["models"], provider
        assert whole["counts"][provider] == len(source["model_ids"]), provider
        for raw_id in source["model_ids"]:
            got = whole["models"][provider][raw_id]
            src = source["models"][raw_id]
            assert got is not src, (provider, raw_id)
            assert got["architecture"] is not src["architecture"], raw_id
            assert got["supported_parameters"] is not src["supported_parameters"]
            assert got["pricing"] is not src["pricing"]
            assert again["models"][provider][raw_id] is not got, raw_id

    # Mutating the returned payload leaves the snapshot, the earlier result and
    # a brand-new call all unchanged.
    shared = whole["models"]["synthetic-a"]["vendor/shared"]
    shared["architecture"]["input_modalities"].append("vision")
    shared["supported_parameters"].append("seed")
    shared["pricing"]["prompt"] = "999"
    whole["providers"]["synthetic-a"].append("injected")
    whole["counts"]["synthetic-a"] = 99
    whole["provider_staleness"]["synthetic-a"] = True
    assert lookup == before
    assert again["models"]["synthetic-a"]["vendor/shared"][
        "architecture"]["input_modalities"] == ["text"]
    assert again["models"]["synthetic-a"]["vendor/shared"][
        "supported_parameters"] == ["temperature", "max_tokens"]
    assert again["models"]["synthetic-a"]["vendor/shared"][
        "pricing"] == {"prompt": "0", "completion": "0"}
    assert again["providers"]["synthetic-a"] == [
        "vendor/model-renamed", "vendor/model:free", "vendor/shared"]
    third = list_free_models(lookup, {})
    assert third == again

    # (b) One-provider form and the registered zero-ID containers are fresh too,
    #     and the known metadata casing/order/type is preserved verbatim (no
    #     reprojection).
    single = list_free_models(lookup, {"provider": "synthetic-a"})
    source_a = lookup["providers"]["synthetic-a"]
    assert single["models"] is not source_a["models"]
    assert single["model_ids"] is not source_a["model_ids"]
    assert single["provider_staleness"] is not lookup["providers"]
    assert single["models"]["vendor/model:free"]["architecture"][
        "input_modalities"] == ["text", "image"]
    assert type(single["models"]["vendor/model:free"]["context_length"]) is int
    single["model_ids"].append("injected")
    single["models"]["vendor/model:free"]["architecture"][
        "input_modalities"].append("audio")
    single["provider_staleness"]["synthetic-a"] = True
    assert lookup == before
    assert single["model_ids"] is not list_free_models(
        lookup, {"provider": "synthetic-a"})["model_ids"]

    zero = list_free_models(lookup, {"provider": "synthetic-c"})
    source_c = lookup["providers"]["synthetic-c"]
    assert zero["model_ids"] is not source_c["model_ids"]
    assert zero["models"] is not source_c["models"]
    assert zero["provider_staleness"] is not lookup["providers"]
    zero["model_ids"].append("injected")
    zero["provider_staleness"]["synthetic-c"] = True
    assert lookup == before
    assert zero["model_ids"] is not list_free_models(
        lookup, {"provider": "synthetic-c"})["model_ids"]


# ---------- pure get_model: literal membership + separate marker endpoints ----------
#
# `watchdog_catalog.query.get_model(snapshot, arguments)` is the pure core of the
# frozen `get_model` tool. It takes an already-VALIDATED public snapshot (the
# load_snapshot / source boundary owns validation -- this core performs no
# re-hash, no I/O and no clock) plus the whole DECODED argument object, and
# returns either the frozen success payload or the shared argument-error
# envelope. It never raises into the semantic caller for a malformed argument
# and never mutates its inputs.
#
# Every expected payload below is the INDEPENDENTLY authored `cases.json` golden
# for the `get_model` tool (hand-written from the contract, never from an
# implementation), and the three exact field sets are the frozen README tables
# (README.md, "Success envelope -- get_model").

GET_MODEL_CASES_DOC = FIXTURE_DIR / "cases.json"
GET_MODEL_ARG_DOC = FIXTURE_DIR / "argument-cases.json"

GET_MODEL_FIELDS = frozenset({
    "ok", "query", "snapshot_id", "tick_epoch", "exact_matches",
    "exact_gateways", "provider_staleness", "endpoints", "caveats", "counts"})
GET_MODEL_ENDPOINT_FIELDS = frozenset({
    "provider", "model_id", "chat_completions_url", "api_type", "stale",
    "metadata"})
GET_MODEL_COUNT_FIELDS = frozenset({
    "exact_matches", "endpoints", "distinct_raw_ids", "marker_group_labels"})
GET_MODEL_CAVEAT = (
    "Cross-gateway ABSENCE is unreliable: gateways rename inconsistently, so a "
    "missing exact id does NOT prove the model is unavailable there.")


def _get_model_cases(doc):
    return [case for case in doc["cases"] if case["tool"] == "get_model"]


def test_get_model_success_goldens_match_every_literal_case():
    """All thirteen frozen `get_model` goldens reproduce EXACTLY -- deep
    semantic equality plus the exact top-level/endpoint/count field sets -- with
    strict scalar types, literal provenance, untouched inputs, and a compact
    empty-registry control."""
    from watchdog_catalog.query import get_model

    doc = _decode_doc(GET_MODEL_CASES_DOC)
    cases = _get_model_cases(doc)
    # Count and id uniqueness are asserted so a dropped or duplicated golden
    # cannot shrink the loop and still pass.
    assert len(cases) == 13, len(cases)
    assert len({case["id"] for case in cases}) == 13
    for case in cases:
        cid = case["id"]
        # Per-case fixture override; NEVER a wrong-fixture fallback.
        fixture = case.get("fixture", doc["fixture"])
        snapshot = _validated_snapshot(fixture)
        before = copy.deepcopy(snapshot)
        args = copy.deepcopy(case["args"])
        args_before = copy.deepcopy(args)
        result = get_model(snapshot, args)
        expect = case["expect"]
        # Deep semantic equality AND the exact top-level field set.
        assert result == expect, cid
        assert set(result) == set(expect), cid
        assert set(result) == GET_MODEL_FIELDS, cid
        # Strict scalar types and literal provenance: ok is exactly True, the
        # query is the raw argument verbatim, and the provenance slots echo the
        # consumed snapshot.
        assert result["ok"] is True, cid
        assert type(result["query"]) is str, cid
        assert result["query"] == args["model_id"], cid
        assert result["snapshot_id"] == snapshot["snapshot_id"], cid
        assert result["tick_epoch"] == snapshot["tick_epoch"], cid
        # exact_matches / provider_staleness carry ONE entry per provider_order
        # provider -- including the registered zero-ID provider.
        assert set(result["exact_matches"]) == \
            set(snapshot["provider_order"]), cid
        assert set(result["provider_staleness"]) == \
            set(snapshot["provider_order"]), cid
        for flag in result["exact_matches"].values():
            assert type(flag) is bool, cid
        for flag in result["provider_staleness"].values():
            assert type(flag) is bool, cid
        assert result["exact_gateways"] == \
            [p for p in snapshot["provider_order"]
             if result["exact_matches"][p]], cid
        for endpoint in result["endpoints"]:
            assert set(endpoint) == GET_MODEL_ENDPOINT_FIELDS, cid
            assert type(endpoint["stale"]) is bool, cid
            assert endpoint["stale"] == \
                result["provider_staleness"][endpoint["provider"]], cid
            source = snapshot["providers"][endpoint["provider"]]
            assert endpoint["chat_completions_url"] == \
                source["chat_completions_url"], cid
            assert endpoint["api_type"] == source["api_type"], cid
            assert endpoint["metadata"] == \
                source["models"][endpoint["model_id"]], cid
        assert set(result["counts"]) == GET_MODEL_COUNT_FIELDS, cid
        for value in result["counts"].values():
            assert type(value) is int, cid  # a bool is never a count
            assert value >= 0, cid
        assert result["counts"]["exact_matches"] == \
            len(result["exact_gateways"]), cid
        assert result["counts"]["endpoints"] == len(result["endpoints"]), cid
        assert result["counts"]["distinct_raw_ids"] == \
            len({e["model_id"] for e in result["endpoints"]}), cid
        assert result["caveats"] == [GET_MODEL_CAVEAT], cid
        assert snapshot == before, cid  # never mutated
        assert args == args_before, cid

    # A legitimately EMPTY registry is a valid control, built from an existing
    # valid fixture and resealed with the test-local json/hashlib oracle, then
    # re-validated before the query.
    empty = _decode_doc(FIXTURE_DIR / "lookup.json")
    empty["provider_order"] = []
    empty["providers"] = {}
    _reseal(empty)
    assert _catalog.validate_snapshot(empty) is empty
    whole = get_model(empty, {"model_id": "vendor/shared"})
    assert set(whole) == GET_MODEL_FIELDS
    assert whole["ok"] is True
    assert whole["exact_matches"] == {} and whole["exact_gateways"] == []
    assert whole["provider_staleness"] == {} and whole["endpoints"] == []
    assert whole["counts"] == {"exact_matches": 0, "endpoints": 0,
                               "distinct_raw_ids": 0,
                               "marker_group_labels": 0}
    assert whole["caveats"] == [GET_MODEL_CAVEAT]


# ---------- pure get_model: the required model_id argument boundary ----------
#
# The eight `get_model` invalid rows of `argument-cases.json` carry the COMPLETE
# literal semantic envelope; the two valid controls record acceptance only. The
# programmatic rows below are compact literal restatements of the frozen README
# rules (object -> unknown-key -> required model_id type/blankness), including
# the unknown-key precedence that makes `parameter` null even when the required
# id itself would also be wrong.

def test_get_model_argument_boundary_matches_literal_rows_and_rules():
    """The eight frozen invalid rows and two valid controls reproduce their
    literal classification, and the compact programmatic rows pin the frozen
    object -> unknown-key -> required-string -> blank precedence, the explicit
    ASCII blank set, the never-trimmed/case-folded identifier and the fresh
    non-reflecting envelope."""
    from watchdog_catalog.query import get_model

    doc = _decode_doc(GET_MODEL_ARG_DOC)
    invalid = [row for row in doc["invalid_cases"]
               if row["tool"] == "get_model"]
    valid = [row for row in doc["valid_controls"] if row["tool"] == "get_model"]
    assert len(invalid) == 8, [row["id"] for row in invalid]
    assert {row["id"] for row in invalid} == {
        "args_number_not_object", "unknown_arg_get_model", "model_id_missing",
        "model_id_null", "model_id_number", "model_id_empty",
        "model_id_ascii_blank", "precedence_unknown_key_before_required"}
    assert len(valid) == 2, [row["id"] for row in valid]

    for row in invalid:
        cid = row["id"]
        snapshot = _validated_snapshot(row["fixture"])
        args = copy.deepcopy(row["args"])
        before = copy.deepcopy(args)
        result = get_model(snapshot, args)
        # The literal, independently-authored envelope is reproduced exactly.
        assert result == row["expect"], cid
        assert args == before, cid  # the argument object is never mutated
        error = result["error"]
        assert result["ok"] is False, cid
        assert set(result) == {"ok", "error"}, cid
        assert set(error) == {"code", "message", "parameter"}, cid
        # No hostile key/value reflection and no extra provenance field.
        for hostile in ("tool", "state_root", "source", "path", "cache",
                        "snapshot_id", "count", "partial", "staleness",
                        "query", "endpoints"):
            assert hostile not in result and hostile not in error, (cid, hostile)
        assert error["code"] in ARGUMENT_MESSAGES, cid

    # The two valid controls are ACCEPTED (ok True); the complete success
    # oracles are the thirteen independent goldens above, so this pins
    # acceptance and the literal retained query.
    for row in valid:
        cid = row["id"]
        snapshot = _validated_snapshot(row["fixture"])
        result = get_model(snapshot, copy.deepcopy(row["args"]))
        assert result["ok"] is True, cid
        assert result["query"] == row["args"]["model_id"], cid

    lookup = _validated_snapshot("lookup.json")

    # A whole argument object that is not a JSON object is invalid_arguments/
    # null -- None, a list, a number, a string, a bool, a tuple and bytes.
    for args in (None, [], 5, "x", False, 1.0, (), b"{}"):
        _assert_argument_envelope(get_model(lookup, args),
                                  INVALID_ARGUMENTS, None, repr(args))

    # A missing or wrong-typed required model_id (null/bool/int/float/list/dict)
    # is invalid_arguments(model_id) -- never coerced.
    _assert_argument_envelope(get_model(lookup, {}),
                              INVALID_ARGUMENTS, "model_id", "missing")
    for bad in (None, True, False, 5, 1.5, [], ["vendor/shared"], {}, {"a": 1}):
        _assert_argument_envelope(get_model(lookup, {"model_id": bad}),
                                  INVALID_ARGUMENTS, "model_id", repr(bad))

    # Empty and ASCII-blank-only strings are invalid_arguments(model_id): every
    # class of the explicit U+0009..U+000D + U+0020 set, alone and combined.
    for blank in ("", " ", "\t", "\n", "\r", "\v", "\f", " \t\n\r\v\f",
                  "\t \n", "  "):
        _assert_argument_envelope(get_model(lookup, {"model_id": blank}),
                                  INVALID_ARGUMENTS, "model_id", repr(blank))

    # Unknown-key precedence: ANY unknown key rejects with parameter null BEFORE
    # the required model_id check -- even `provider`, which is NOT a get_model
    # key and is therefore never a filter.
    for args in ({"bogus": 1}, {"model_id": 5, "bogus": 1},
                 {"model_id": "", "bogus": 1}, {"provider": "synthetic-a"},
                 {"model_id": "vendor/shared", "provider": "synthetic-a"},
                 {"model_id": "vendor/shared", "bogus": 1},
                 {"bogus": 1, "provider": "synthetic-z"}):
        _assert_argument_envelope(get_model(lookup, args),
                                  INVALID_ARGUMENTS, None, repr(args))

    # The retained identifier is never trimmed, case-folded or repaired: a
    # padded nonblank query stays literal, wrong case is not folded, and
    # non-ASCII code points (NBSP U+00A0, EM SPACE U+2003, FILE SEPARATOR
    # U+001C) are literal NONBLANK queries -- ACCEPTED, a success with no match,
    # zero counts and the caveat. The ASCII blank set is never widened.
    for query in (" vendor/shared ", "VENDOR/SHARED", "Vendor/Shared",
                  "\u00a0", "\u2003", "\u001c", "\u00a0\u2003"):
        result = get_model(lookup, {"model_id": query})
        assert result["ok"] is True, repr(query)
        assert result["query"] == query, repr(query)
        assert result["counts"] == {"exact_matches": 0, "endpoints": 0,
                                    "distinct_raw_ids": 0,
                                    "marker_group_labels": 0}, repr(query)
        assert result["exact_gateways"] == [] and result["endpoints"] == [], \
            repr(query)
        assert result["caveats"] == [GET_MODEL_CAVEAT], repr(query)


# ---------- pure get_model: freshness + deep output isolation ----------
#
# A public output is never a window onto the cached snapshot: every container,
# array and matched endpoint metadata object (with its nested architecture /
# parameter / pricing containers) is freshly allocated, so a caller mutating a
# returned payload can never corrupt the snapshot or another call.

def test_get_model_outputs_are_fresh_and_deeply_isolated():
    from watchdog_catalog.query import get_model

    lookup = _validated_snapshot("lookup.json")
    before = copy.deepcopy(lookup)

    # (a) Two independent calls on the same shared raw ID: fresh containers and
    #     each endpoint metadata object (and its nested containers) unaliased to
    #     the snapshot and to the other call.
    first = get_model(lookup, {"model_id": "vendor/shared"})
    second = get_model(lookup, {"model_id": "vendor/shared"})
    assert lookup == before
    assert first == second
    assert first["exact_matches"] is not second["exact_matches"]
    assert first["exact_gateways"] is not second["exact_gateways"]
    assert first["provider_staleness"] is not second["provider_staleness"]
    assert first["endpoints"] is not second["endpoints"]
    assert first["counts"] is not second["counts"]
    assert first["caveats"] is not second["caveats"]
    for index, endpoint in enumerate(first["endpoints"]):
        provider = endpoint["provider"]
        raw_id = endpoint["model_id"]
        source = lookup["providers"][provider]["models"][raw_id]
        other = second["endpoints"][index]["metadata"]
        assert endpoint["metadata"] is not source, (provider, raw_id)
        assert endpoint["metadata"] is not other, (provider, raw_id)
        assert endpoint["metadata"]["architecture"] is not source["architecture"]
        assert endpoint["metadata"]["supported_parameters"] is not \
            source["supported_parameters"]
        assert endpoint["metadata"]["pricing"] is not source["pricing"]
        assert endpoint["metadata"]["architecture"] is not \
            other["architecture"]

    # Mutating the returned payload deeply leaves the snapshot, the earlier
    # independent result and a brand-new call all unchanged.
    shared_a = first["endpoints"][0]["metadata"]
    shared_a["architecture"]["input_modalities"].append("vision")
    shared_a["supported_parameters"].append("seed")
    shared_a["pricing"]["prompt"] = "999"
    first["exact_matches"]["synthetic-a"] = False
    first["exact_gateways"].append("injected")
    first["provider_staleness"]["synthetic-a"] = True
    first["endpoints"].append("injected")
    first["counts"]["endpoints"] = 99
    first["caveats"].append("injected")
    assert lookup == before
    meta_a = second["endpoints"][0]["metadata"]
    assert meta_a["architecture"]["input_modalities"] == ["text"]
    assert meta_a["supported_parameters"] == ["temperature", "max_tokens"]
    assert meta_a["pricing"] == {"prompt": "0", "completion": "0"}
    assert second["exact_matches"] == {"synthetic-a": True, "synthetic-b": True,
                                       "synthetic-c": False}
    assert second["counts"]["endpoints"] == 2
    assert second["caveats"] == [GET_MODEL_CAVEAT]
    third = get_model(lookup, {"model_id": "vendor/shared"})
    assert third == second

    # (b) The marker-insensitive lookup isolates each matched endpoint's
    #     metadata independently, and the two providers of a shared raw id never
    #     share a metadata object.
    base = get_model(lookup, {"model_id": "vendor/model"})
    assert [e["model_id"] for e in base["endpoints"]] == \
        ["vendor/model:free", "vendor/model-free"]
    a_meta = base["endpoints"][0]["metadata"]
    b_meta = base["endpoints"][1]["metadata"]
    assert a_meta is not b_meta
    assert a_meta is not \
        lookup["providers"]["synthetic-a"]["models"]["vendor/model:free"]
    assert b_meta is not \
        lookup["providers"]["synthetic-b"]["models"]["vendor/model-free"]
    a_meta["architecture"]["input_modalities"].append("audio")
    assert b_meta["architecture"]["input_modalities"] == ["text"]
    assert lookup == before
    assert get_model(lookup, {"model_id": "vendor/model"})["endpoints"][0][
        "metadata"]["architecture"]["input_modalities"] == ["text", "image"]

    # (c) A no-match success has fresh mutable containers and the caveat, and a
    #     fresh error envelope is returned on every call (mutate then recall).
    empty = get_model(lookup, {"model_id": "vendor/does-not-exist"})
    empty["exact_matches"]["synthetic-a"] = True
    empty["exact_gateways"].append("injected")
    empty["provider_staleness"]["synthetic-a"] = True
    empty["endpoints"].append("injected")
    empty["counts"]["endpoints"] = 7
    empty["caveats"].append("injected")
    assert lookup == before
    fresh = get_model(lookup, {"model_id": "vendor/does-not-exist"})
    assert fresh["exact_matches"] == {"synthetic-a": False,
                                      "synthetic-b": False,
                                      "synthetic-c": False}
    assert fresh["exact_gateways"] == [] and fresh["endpoints"] == []
    assert fresh["counts"] == {"exact_matches": 0, "endpoints": 0,
                               "distinct_raw_ids": 0, "marker_group_labels": 0}
    assert fresh["caveats"] == [GET_MODEL_CAVEAT]

    first_error = get_model(lookup, {"model_id": ""})
    first_error["error"]["code"] = "mutated"
    first_error["ok"] = True
    second_error = get_model(lookup, {"model_id": ""})
    assert second_error == _argument_envelope(INVALID_ARGUMENTS, "model_id")
    assert second_error is not first_error


# ---------- pure list_endpoints: selected-scope endpoints + counts alias ----------
#
# `watchdog_catalog.query.list_endpoints(snapshot, arguments)` is the pure core
# of the frozen `list_endpoints` tool. It takes an already-VALIDATED public
# snapshot (the load_snapshot / source boundary owns validation -- this core
# performs no re-hash, no I/O and no clock) plus the whole DECODED argument
# object, and returns either the frozen success payload or the shared
# argument-error envelope. It never raises into the semantic caller for a
# malformed argument and never mutates its inputs.
#
# Every expected payload below is the INDEPENDENTLY authored `cases.json` golden
# for the `list_endpoints` tool (hand-written from the contract, never from an
# implementation), and the three exact field sets are the frozen README tables
# (README.md, "Success envelope -- list_endpoints").

LIST_ENDPOINTS_CASES_DOC = FIXTURE_DIR / "cases.json"
LIST_ENDPOINTS_ARG_DOC = FIXTURE_DIR / "argument-cases.json"

# The ONE exact top-level field set for BOTH the all-provider and the
# provider-filtered variant, the exact gateway-entry field set and the exact
# count field set.
ENDPOINT_FIELDS = frozenset({
    "ok", "gateways", "counts", "provider", "snapshot_id", "tick_epoch",
    "provider_staleness"})
ENDPOINT_GATEWAY_FIELDS = frozenset({
    "chat_completions_url", "api_type", "model_ids", "models"})
ENDPOINT_COUNT_FIELDS = frozenset({
    "gateways", "endpoints", "distinct_raw_ids", "marker_group_labels",
    "models"})


def _endpoint_cases(doc):
    return [case for case in doc["cases"] if case["tool"] == "list_endpoints"]


def test_list_endpoints_success_goldens_match_every_literal_case():
    """All five frozen `list_endpoints` goldens reproduce EXACTLY -- deep
    semantic equality plus the exact top-level/gateway/count field sets -- with
    strict scalar types, literal provenance, untouched inputs, omission/null
    equivalence on one shared fixture, a programmatic arbitrary-data-key
    selection and a compact empty-registry control."""
    from watchdog_catalog.query import list_endpoints

    doc = _decode_doc(LIST_ENDPOINTS_CASES_DOC)
    cases = _endpoint_cases(doc)
    # Count and id uniqueness are asserted so a dropped or duplicated golden
    # cannot shrink the loop and still pass.
    assert len(cases) == 5, len(cases)
    assert len({case["id"] for case in cases}) == 5
    assert {case["id"] for case in cases} == {
        "endpoints_lookup_all", "endpoints_lookup_provider_a",
        "endpoints_lookup_zero_provider_c", "endpoints_ordering_all",
        "endpoints_extension_all"}
    for case in cases:
        cid = case["id"]
        # Per-case fixture override; NEVER a wrong-fixture fallback.
        fixture = case.get("fixture", doc["fixture"])
        snapshot = _validated_snapshot(fixture)
        before = copy.deepcopy(snapshot)
        result = list_endpoints(snapshot, case["args"])
        expect = case["expect"]
        # Deep semantic equality AND the exact field sets.
        assert result == expect, cid
        assert set(result) == set(expect), cid
        assert set(result) == ENDPOINT_FIELDS, cid
        # Strict scalar types and literal provenance: ok is exactly True and the
        # provenance slots echo the consumed snapshot verbatim.
        assert result["ok"] is True, cid
        assert type(result["snapshot_id"]) is str, cid
        assert result["snapshot_id"] == snapshot["snapshot_id"], cid
        assert result["tick_epoch"] == snapshot["tick_epoch"], cid
        # `provider` echoes the exact selected key, else strictly null.
        args_provider = case["args"].get("provider")
        assert result["provider"] == args_provider, cid
        assert result["provider"] is None or type(result["provider"]) is str
        # Selected scope: gateways and provider_staleness carry exactly the
        # selected provider keys, zero-ID providers INCLUDED.
        selected = [args_provider] if args_provider is not None \
            else list(snapshot["provider_order"])
        assert set(result["gateways"]) == set(selected), cid
        assert set(result["provider_staleness"]) == set(selected), cid
        for flag in result["provider_staleness"].values():
            assert type(flag) is bool, cid
        # Each gateway entry has the exact field set, the same raw-ID
        # membership as its source (order pinned by the golden above), its own
        # projected metadata map and never a cross-provider merge.
        for p in selected:
            entry = result["gateways"][p]
            assert set(entry) == ENDPOINT_GATEWAY_FIELDS, (cid, p)
            source = snapshot["providers"][p]
            assert entry["chat_completions_url"] == \
                source["chat_completions_url"], (cid, p)
            assert entry["api_type"] == source["api_type"], (cid, p)
            assert sorted(entry["model_ids"]) == sorted(source["model_ids"]), \
                (cid, p)
            assert set(entry["models"]) == set(source["model_ids"]), (cid, p)
            for raw_id in source["model_ids"]:
                assert entry["models"][raw_id] == source["models"][raw_id], \
                    (cid, p, raw_id)
        # The counts object has the exact field set; every value is a strict
        # non-negative int (a bool is never a count). Counts describe the
        # SELECTED scope only, and `models` is the documented legacy alias
        # equal to `marker_group_labels`.
        assert set(result["counts"]) == ENDPOINT_COUNT_FIELDS, cid
        pairs = [raw_id for p in selected
                 for raw_id in snapshot["providers"][p]["model_ids"]]
        for value in result["counts"].values():
            assert type(value) is int, cid
            assert value >= 0, cid
        assert result["counts"]["gateways"] == len(selected), cid
        assert result["counts"]["endpoints"] == len(pairs), cid
        assert result["counts"]["distinct_raw_ids"] == len(set(pairs)), cid
        assert result["counts"]["models"] == \
            result["counts"]["marker_group_labels"], cid
        assert snapshot == before, cid  # inputs never mutated

    # Omission (`{}`) and explicit `{"provider": None}` are the SAME
    # all-provider export, asserted on ONE shared fixture (the goldens above use
    # different fixtures, so this equivalence needs its own control).
    lookup = _validated_snapshot("lookup.json")
    omitted = list_endpoints(lookup, {})
    null = list_endpoints(lookup, {"provider": None})
    assert omitted == null
    assert set(omitted) == ENDPOINT_FIELDS
    assert omitted["provider"] is None

    # A programmatic selection by an ARBITRARY data key proves membership is
    # driven by `snapshot.provider_order` alone -- no provider enum or package
    # code knows "synthetic-d". Selecting it yields ONLY synthetic-d, its own
    # metadata and 1/1/1/1/1 counts.
    ext = _validated_snapshot("provider-extension.json")
    assert "synthetic-d" in ext["provider_order"]
    only_d = list_endpoints(ext, {"provider": "synthetic-d"})
    assert set(only_d["gateways"]) == {"synthetic-d"}
    assert set(only_d["provider_staleness"]) == {"synthetic-d"}
    assert only_d["provider"] == "synthetic-d"
    assert only_d["counts"] == {"gateways": 1, "endpoints": 1,
                                "distinct_raw_ids": 1,
                                "marker_group_labels": 1, "models": 1}
    assert only_d["gateways"]["synthetic-d"]["model_ids"] == ["vendor/shared"]
    assert only_d["gateways"]["synthetic-d"]["models"]["vendor/shared"] == \
        ext["providers"]["synthetic-d"]["models"]["vendor/shared"]

    # A registered zero-ID provider is a success with empty containers and a
    # zeroed counts object (the gateway entry still exists).
    zero = list_endpoints(lookup, {"provider": "synthetic-c"})
    assert set(zero["gateways"]) == {"synthetic-c"}
    assert zero["gateways"]["synthetic-c"]["model_ids"] == []
    assert zero["gateways"]["synthetic-c"]["models"] == {}
    assert zero["counts"] == {"gateways": 1, "endpoints": 0,
                              "distinct_raw_ids": 0, "marker_group_labels": 0,
                              "models": 0}
    assert zero["provider_staleness"] == {"synthetic-c": False}

    # A legitimately EMPTY registry is a valid all-provider control, built from
    # an existing valid fixture and resealed with the test-local json/hashlib
    # oracle, then re-validated before the query.
    empty = _decode_doc(FIXTURE_DIR / "lookup.json")
    empty["provider_order"] = []
    empty["providers"] = {}
    _reseal(empty)
    assert _catalog.validate_snapshot(empty) is empty
    whole = list_endpoints(empty, {})
    assert set(whole) == ENDPOINT_FIELDS
    assert whole["ok"] is True
    assert whole["gateways"] == {} and whole["provider_staleness"] == {}
    assert whole["provider"] is None
    assert whole["counts"] == {"gateways": 0, "endpoints": 0,
                               "distinct_raw_ids": 0, "marker_group_labels": 0,
                               "models": 0}


# ---------- pure list_endpoints: the provider argument boundary ----------
#
# The three `list_endpoints` invalid rows of `argument-cases.json` carry the
# COMPLETE literal semantic envelope; the two valid controls record acceptance
# only. The programmatic rows below are compact literal restatements of the
# frozen README rules (object -> unknown-key -> provider type -> provider
# membership), including the unknown-key precedence that makes `parameter` null
# even when the provider itself would also be wrong.

def test_list_endpoints_argument_boundary_matches_literal_rows_and_rules():
    """The three frozen invalid rows and two valid controls reproduce their
    literal classification, and the compact programmatic rows pin the frozen
    object -> unknown-key -> provider type -> membership precedence, the
    no-coercion provider rules and the fresh non-reflecting envelope."""
    from watchdog_catalog.query import list_endpoints

    doc = _decode_doc(LIST_ENDPOINTS_ARG_DOC)
    invalid = [row for row in doc["invalid_cases"]
               if row["tool"] == "list_endpoints"]
    valid = [row for row in doc["valid_controls"]
             if row["tool"] == "list_endpoints"]
    assert len(invalid) == 3, [row["id"] for row in invalid]
    assert {row["id"] for row in invalid} == {
        "unknown_arg_list_endpoints", "unknown_provider_list_endpoints",
        "provider_empty"}
    assert len(valid) == 2, [row["id"] for row in valid]

    for row in invalid:
        cid = row["id"]
        snapshot = _validated_snapshot(row["fixture"])
        args = copy.deepcopy(row["args"])
        before = copy.deepcopy(args)
        result = list_endpoints(snapshot, args)
        # The literal, independently-authored envelope is reproduced exactly.
        assert result == row["expect"], cid
        assert args == before, cid  # the argument object is never mutated
        error = result["error"]
        assert result["ok"] is False, cid
        assert set(result) == {"ok", "error"}, cid
        assert set(error) == {"code", "message", "parameter"}, cid
        # No hostile key/value reflection and no extra provenance/count field.
        for hostile in ("tool", "state_root", "source", "path", "cache",
                        "snapshot_id", "count", "gateways", "partial",
                        "staleness", "provider_staleness"):
            assert hostile not in result and hostile not in error, (cid, hostile)
        assert error["code"] in ARGUMENT_MESSAGES, cid

    # The two valid controls are ACCEPTED (ok True); the complete success
    # oracles are the five independent goldens above, so this pins acceptance.
    for row in valid:
        cid = row["id"]
        snapshot = _validated_snapshot(row["fixture"])
        result = list_endpoints(snapshot, copy.deepcopy(row["args"]))
        assert result["ok"] is True, cid

    lookup = _validated_snapshot("lookup.json")

    # A whole argument object that is not a JSON object is invalid_arguments/
    # null -- None, a list, a number, a string, a bool, a tuple and bytes.
    for args in (None, [], 5, "x", False, 1.0, (), b"{}"):
        _assert_argument_envelope(list_endpoints(lookup, args),
                                  INVALID_ARGUMENTS, None, repr(args))

    # A non-null provider of the wrong type (bool/int/float/list/dict) is
    # invalid_arguments(provider) -- never coerced.
    for bad in (True, False, 5, 1.5, [], ["synthetic-a"], {}, {"a": 1}):
        _assert_argument_envelope(list_endpoints(lookup, {"provider": bad}),
                                  INVALID_ARGUMENTS, "provider", repr(bad))

    # Empty/padded/wrong-case/absent strings are unknown_provider(provider) --
    # never trimmed, padded-repaired or case-folded.
    for bad in ("", " synthetic-a ", "synthetic-a ", " synthetic-a",
                "Synthetic-A", "SYNTHETIC-A", "synthetic-z", "unknown"):
        _assert_argument_envelope(list_endpoints(lookup, {"provider": bad}),
                                  UNKNOWN_PROVIDER, "provider", bad)

    # Unknown-key precedence: ANY unknown key rejects with parameter null BEFORE
    # the provider type or membership check, so an unknown key combined with a
    # wrong-typed provider AND with an unknown provider both yield null.
    _assert_argument_envelope(
        list_endpoints(lookup, {"provider": 5, "limit": 5}),
        INVALID_ARGUMENTS, None, "unknown-key before provider type")
    _assert_argument_envelope(
        list_endpoints(lookup, {"provider": "synthetic-z", "bogus": 1}),
        INVALID_ARGUMENTS, None, "unknown-key before provider membership")
    _assert_argument_envelope(
        list_endpoints(lookup, {"provider": None, "limit": 5}),
        INVALID_ARGUMENTS, None, "unknown-key with a null provider")
    _assert_argument_envelope(
        list_endpoints(lookup, {"provider": "synthetic-a", "limit": 5}),
        INVALID_ARGUMENTS, None, "unknown-key with a valid provider")

    # `{}` and `{"provider": None}` are accepted, and the error envelope is a
    # FRESH object on every call (never a shared module-level constant).
    assert list_endpoints(lookup, {})["ok"] is True
    assert list_endpoints(lookup, {"provider": None})["ok"] is True
    first = list_endpoints(lookup, {"provider": "synthetic-z"})
    first["error"]["code"] = "mutated"
    first["ok"] = True
    second = list_endpoints(lookup, {"provider": "synthetic-z"})
    assert second == _argument_envelope(UNKNOWN_PROVIDER, "provider")
    assert second is not first


# ---------- pure list_endpoints: freshness + deep output isolation ----------
#
# A public output is never a window onto the cached snapshot: every container,
# gateway entry, raw-ID array and projected model object (with its nested
# architecture / parameter / pricing containers) is freshly allocated, so a
# caller mutating a returned payload can never corrupt the snapshot or another
# call.

def test_list_endpoints_outputs_are_fresh_and_deeply_isolated():
    from watchdog_catalog.query import list_endpoints

    lookup = _validated_snapshot("lookup.json")
    before = copy.deepcopy(lookup)

    # (a) All-provider form: fresh containers, and each gateway's model_ids /
    #     models (and nested model containers) unaliased to the snapshot and to
    #     a second call.
    whole = list_endpoints(lookup, {})
    again = list_endpoints(lookup, {})
    assert lookup == before
    assert whole == again
    assert whole["gateways"] is not again["gateways"]
    assert whole["counts"] is not again["counts"]
    assert whole["provider_staleness"] is not again["provider_staleness"]
    for provider in lookup["provider_order"]:
        source = lookup["providers"][provider]
        entry = whole["gateways"][provider]
        assert entry is not source, provider
        assert entry["model_ids"] is not source["model_ids"], provider
        assert entry["models"] is not source["models"], provider
        assert again["gateways"][provider] is not entry, provider
        for raw_id in source["model_ids"]:
            got = entry["models"][raw_id]
            src = source["models"][raw_id]
            assert got is not src, (provider, raw_id)
            assert got["architecture"] is not src["architecture"], raw_id
            assert got["supported_parameters"] is not src["supported_parameters"]
            assert got["pricing"] is not src["pricing"]
            assert again["gateways"][provider]["models"][raw_id] is not got

    # Mutating the returned payload deeply leaves the snapshot, the earlier
    # independent result and a brand-new call all unchanged.
    shared = whole["gateways"]["synthetic-a"]["models"]["vendor/shared"]
    shared["architecture"]["input_modalities"].append("vision")
    shared["supported_parameters"].append("seed")
    shared["pricing"]["prompt"] = "999"
    whole["gateways"]["synthetic-a"]["model_ids"].append("injected")
    whole["counts"]["endpoints"] = 99
    whole["provider_staleness"]["synthetic-a"] = True
    assert lookup == before
    again_shared = again["gateways"]["synthetic-a"]["models"]["vendor/shared"]
    assert again_shared["architecture"]["input_modalities"] == ["text"]
    assert again_shared["supported_parameters"] == ["temperature", "max_tokens"]
    assert again_shared["pricing"] == {"prompt": "0", "completion": "0"}
    assert again["gateways"]["synthetic-a"]["model_ids"] == [
        "vendor/model-renamed", "vendor/model:free", "vendor/shared"]
    third = list_endpoints(lookup, {})
    assert third == again

    # (b) Single-provider and registered zero-ID containers are fresh too, and
    #     the known metadata casing/order/type is preserved verbatim (no
    #     reprojection).
    single = list_endpoints(lookup, {"provider": "synthetic-a"})
    source_a = lookup["providers"]["synthetic-a"]
    single_a = single["gateways"]["synthetic-a"]
    assert single_a["model_ids"] is not source_a["model_ids"]
    assert single_a["models"] is not source_a["models"]
    assert single["provider_staleness"] is not lookup["providers"]
    assert single_a["models"]["vendor/model:free"]["architecture"][
        "input_modalities"] == ["text", "image"]
    assert type(single_a["models"]["vendor/model:free"]["context_length"]) is int
    single_a["model_ids"].append("injected")
    single_a["models"]["vendor/model:free"]["architecture"][
        "input_modalities"].append("audio")
    single["provider_staleness"]["synthetic-a"] = True
    assert lookup == before
    assert single_a["model_ids"] is not list_endpoints(
        lookup, {"provider": "synthetic-a"})["gateways"]["synthetic-a"][
            "model_ids"]

    zero = list_endpoints(lookup, {"provider": "synthetic-c"})
    source_c = lookup["providers"]["synthetic-c"]
    zero_c = zero["gateways"]["synthetic-c"]
    assert zero_c["model_ids"] is not source_c["model_ids"]
    assert zero_c["models"] is not source_c["models"]
    assert zero["provider_staleness"] is not lookup["providers"]
    zero_c["model_ids"].append("injected")
    zero["provider_staleness"]["synthetic-c"] = True
    assert lookup == before
    assert zero_c["model_ids"] is not list_endpoints(
        lookup, {"provider": "synthetic-c"})["gateways"]["synthetic-c"][
            "model_ids"]

    # (c) A provider added by DATA alone (synthetic-d) isolates its own metadata
    #     exactly like the registry providers.
    ext = _validated_snapshot("provider-extension.json")
    ext_before = copy.deepcopy(ext)
    only_d = list_endpoints(ext, {"provider": "synthetic-d"})
    d_source = ext["providers"]["synthetic-d"]
    d_entry = only_d["gateways"]["synthetic-d"]
    assert d_entry["models"] is not d_source["models"]
    assert d_entry["models"]["vendor/shared"] is not \
        d_source["models"]["vendor/shared"]
    assert d_entry["models"]["vendor/shared"]["architecture"] is not \
        d_source["models"]["vendor/shared"]["architecture"]
    d_entry["models"]["vendor/shared"]["architecture"][
        "input_modalities"].append("audio")
    assert ext == ext_before

    # (d) A fresh error envelope is returned on every call (mutate then recall).
    first_error = list_endpoints(lookup, {"provider": ""})
    first_error["error"]["code"] = "mutated"
    first_error["ok"] = True
    second_error = list_endpoints(lookup, {"provider": ""})
    assert second_error == _argument_envelope(UNKNOWN_PROVIDER, "provider")
    assert second_error is not first_error


# ---------- pure watchdog_status: fixed-clock status + coverage ----------
#
# `watchdog_status` is the WHOLE-CATALOGUE status query: its payload reports
# published-catalogue health, never private operator health, so it carries no
# local path/state_root/source/cache timestamp. The fixed test clock is a
# case-level `now` injected as a REQUIRED keyword-only argument into the core --
# it is never a tool argument (`args` is `{}` on every golden) and never a
# wall-clock lookup. Every expected payload below is the INDEPENDENTLY authored
# `cases.json` golden (hand-written from the contract, never from an
# implementation); the exact field sets are the frozen README tables
# (README.md, "Success envelope -- watchdog_status").

STATUS_CASES_DOC = FIXTURE_DIR / "cases.json"
STATUS_ARG_DOC = FIXTURE_DIR / "argument-cases.json"

# The exact top-level field set, the exact whole-catalogue count field set and
# the exact metadata-coverage field set.
STATUS_FIELDS = frozenset({
    "ok", "snapshot_id", "tick_epoch", "cadence_s", "last_tick_age_s",
    "tick_fresh", "provider_staleness", "provider_counts", "metadata_coverage",
    "counts", "pending_alerts", "site_published", "site_publish_age_s",
    "warnings"})
STATUS_COUNT_FIELDS = frozenset({
    "gateways", "endpoints", "distinct_raw_ids", "marker_group_labels"})
STATUS_COVERAGE_FIELDS = frozenset({
    "endpoints", "context_length_known", "input_modalities_known",
    "output_modalities_known", "supported_parameters_known",
    "description_known"})
# The frozen status warning order: the snapshot THREE codes followed by the two
# derived status-only codes (five is unreachable in one payload because the
# derived codes are mutually exclusive).
STATUS_WARNING_ORDER = (
    "tick_epoch_unknown", "metadata_missing", "metadata_invalid",
    "tick_epoch_future", "tick_stale")


def test_watchdog_status_success_goldens_match_every_literal_case():
    """All nine frozen `watchdog_status` goldens reproduce EXACTLY -- deep
    semantic equality plus the exact top-level/count/coverage field sets -- with
    strict scalar types, literal provenance, independent counts/coverage
    recomputation, the inclusive one-cadence freshness boundary, every-provider
    (zero-ID included) staleness/count maps, strictly-null unobservables, the
    frozen warning order and untouched inputs, plus four valid in-memory
    controls."""
    from watchdog_catalog.query import watchdog_status, strip_free_marker

    doc = _decode_doc(STATUS_CASES_DOC)
    cases = [case for case in doc["cases"]
             if case["tool"] == "watchdog_status"]
    # Count and id uniqueness are asserted so a dropped or duplicated golden
    # cannot shrink the loop and still pass.
    assert len(cases) == 9, len(cases)
    assert len({case["id"] for case in cases}) == 9
    assert {case["id"] for case in cases} == {
        "status_capability_unknown_epoch_now_fresh",
        "status_metadata_age_below_cadence_fresh",
        "status_metadata_age_at_cadence_fresh",
        "status_metadata_age_just_over_cadence_stale",
        "status_lookup_old_tick_stale_healthy_providers",
        "status_lookup_future_epoch_not_stale",
        "status_unknown_time_missing_epoch",
        "status_short_cadence_age_at_cadence_fresh",
        "status_short_cadence_age_just_over_stale"}
    for case in cases:
        cid = case["id"]
        # Every status case is argument-free with its own literal case-level
        # clock.
        assert case["args"] == {}, cid
        assert type(case["now"]) is int and case["now"] >= 0, cid
        # Per-case fixture override; NEVER a wrong-fixture fallback.
        fixture = case.get("fixture", doc["fixture"])
        snapshot = _validated_snapshot(fixture)
        before = copy.deepcopy(snapshot)
        args = copy.deepcopy(case["args"])
        result = watchdog_status(snapshot, args, now=case["now"])
        expect = case["expect"]
        # Deep semantic equality AND the exact frozen field sets.
        assert result == expect, cid
        assert set(result) == set(expect), cid
        assert set(result) == STATUS_FIELDS, cid
        assert set(result["counts"]) == STATUS_COUNT_FIELDS, cid
        assert set(result["metadata_coverage"]) == STATUS_COVERAGE_FIELDS, cid
        # Strict scalar types: ok is exactly True, tick_fresh a strict bool,
        # the age null or a strict int.
        assert result["ok"] is True, cid
        assert type(result["tick_fresh"]) is bool, cid
        assert result["last_tick_age_s"] is None or \
            type(result["last_tick_age_s"]) is int, cid
        # Provenance echoes the consumed snapshot verbatim (a future epoch is
        # never repaired); cadence is snapshot data, never a hardcoded 3600.
        assert result["snapshot_id"] == snapshot["snapshot_id"], cid
        assert result["tick_epoch"] == snapshot["tick_epoch"], cid
        assert result["cadence_s"] == snapshot["cadence_s"], cid
        # Inclusive freshness: age 0 and age == cadence are fresh; a null or
        # future tick is null age and not fresh, never clamped to a healthy 0.
        tick = snapshot["tick_epoch"]
        cadence = snapshot["cadence_s"]
        now = case["now"]
        if tick is None or tick > now:
            assert result["last_tick_age_s"] is None, cid
            assert result["tick_fresh"] is False, cid
        else:
            assert result["last_tick_age_s"] == now - tick, cid
            assert result["tick_fresh"] is (now - tick <= cadence), cid
        # Provider maps cover EVERY provider_order key, zero-ID providers
        # included, in display order.
        assert list(result["provider_staleness"]) == \
            list(snapshot["provider_order"]), cid
        assert list(result["provider_counts"]) == \
            list(snapshot["provider_order"]), cid
        for provider in snapshot["provider_order"]:
            entry = snapshot["providers"][provider]
            assert result["provider_staleness"][provider] == entry["stale"], \
                (cid, provider)
            assert type(result["provider_staleness"][provider]) is bool, \
                (cid, provider)
            assert result["provider_counts"][provider] == \
                len(entry["model_ids"]), (cid, provider)
            assert type(result["provider_counts"][provider]) is int, \
                (cid, provider)
            assert result["provider_counts"][provider] >= 0, (cid, provider)
        # Whole-catalogue counts recomputed independently from the snapshot.
        pairs = [raw_id for provider in snapshot["provider_order"]
                 for raw_id in snapshot["providers"][provider]["model_ids"]]
        assert result["counts"] == {
            "gateways": len(snapshot["provider_order"]),
            "endpoints": len(pairs),
            "distinct_raw_ids": len(set(pairs)),
            "marker_group_labels": len({strip_free_marker(raw_id)
                                        for raw_id in pairs})}, cid
        # Metadata coverage recomputed independently over the global tracked-pair
        # denominator: a known 0 / [] / "" counts, a null does not.
        coverage = {"endpoints": len(pairs), "context_length_known": 0,
                    "input_modalities_known": 0, "output_modalities_known": 0,
                    "supported_parameters_known": 0, "description_known": 0}
        for provider in snapshot["provider_order"]:
            entry = snapshot["providers"][provider]
            for raw_id in entry["model_ids"]:
                model = entry["models"][raw_id]
                architecture = model["architecture"]
                if model["context_length"] is not None:
                    coverage["context_length_known"] += 1
                if architecture["input_modalities"] is not None:
                    coverage["input_modalities_known"] += 1
                if architecture["output_modalities"] is not None:
                    coverage["output_modalities_known"] += 1
                if model["supported_parameters"] is not None:
                    coverage["supported_parameters_known"] += 1
                if model["description"] is not None:
                    coverage["description_known"] += 1
        assert result["metadata_coverage"] == coverage, cid
        for value in result["metadata_coverage"].values():
            assert type(value) is int and value >= 0, cid
        assert result["metadata_coverage"]["endpoints"] == \
            result["counts"]["endpoints"], cid
        assert result["metadata_coverage"]["endpoints"] >= \
            max(result["metadata_coverage"].values()), cid
        # The three unobservables are STRICTLY null -- never 0 or False.
        assert result["pending_alerts"] is None, cid
        assert result["site_published"] is None, cid
        assert result["site_publish_age_s"] is None, cid
        # Warnings are the deduplicated snapshot-plus-derived union in the frozen
        # status order, stable strings only.
        assert result["warnings"] == [code for code in STATUS_WARNING_ORDER
                                      if code in set(result["warnings"])], cid
        assert len(result["warnings"]) == len(set(result["warnings"])), cid
        assert all(isinstance(code, str) for code in result["warnings"]), cid
        # Inputs are never mutated.
        assert snapshot == before, cid
        assert args == case["args"], cid

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
# (including `now`, `provider` and `limit`) is an unknown argument. The one
# invalid row of `argument-cases.json` carries the COMPLETE literal envelope;
# the one valid control records acceptance only. The programmatic rows are
# compact literal restatements of the frozen README rules.

def test_watchdog_status_argument_boundary_matches_literal_rows_and_rules():
    """The one frozen invalid row and one valid control reproduce their literal
    classification, and the compact programmatic rows pin the frozen object ->
    any-unknown-key precedence, the never-reflecting fresh envelope and the fact
    that the fixed clock is a keyword-only harness input, never an argument."""
    from watchdog_catalog.query import watchdog_status

    doc = _decode_doc(STATUS_ARG_DOC)
    invalid = [row for row in doc["invalid_cases"]
               if row["tool"] == "watchdog_status"]
    valid = [row for row in doc["valid_controls"]
             if row["tool"] == "watchdog_status"]
    assert len(invalid) == 1, [row["id"] for row in invalid]
    assert invalid[0]["id"] == "unknown_arg_watchdog_status"
    assert invalid[0]["args"] == {"now": 1759200000}
    assert len(valid) == 1, [row["id"] for row in valid]
    assert valid[0]["id"] == "valid_omitted_watchdog_status"
    assert valid[0]["args"] == {}

    for row in invalid:
        cid = row["id"]
        snapshot = _validated_snapshot(row["fixture"])
        args = copy.deepcopy(row["args"])
        before = copy.deepcopy(args)
        result = watchdog_status(snapshot, args, now=1759200000)
        # The literal, independently-authored envelope is reproduced exactly.
        assert result == row["expect"], cid
        assert args == before, cid  # the argument object is never mutated
        error = result["error"]
        assert result["ok"] is False, cid
        assert set(result) == {"ok", "error"}, cid
        assert set(error) == {"code", "message", "parameter"}, cid
        # No hostile key/value reflection and no provenance/count field.
        for hostile in ("tool", "state_root", "source", "path", "cache",
                        "snapshot_id", "count", "counts", "partial",
                        "staleness", "provider_staleness", "warnings"):
            assert hostile not in result and hostile not in error, (cid, hostile)
        assert error["code"] == INVALID_ARGUMENTS, cid
        assert error["parameter"] is None, cid

    for row in valid:
        cid = row["id"]
        snapshot = _validated_snapshot(row["fixture"])
        result = watchdog_status(snapshot, copy.deepcopy(row["args"]),
                                 now=1759200000)
        assert result["ok"] is True, cid

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
# Expected pairs below are taken from the INDEPENDENTLY authored `cases.json`
# goldens (never from another implementation, serializer or runtime query) and
# from literal, hand-derived synthetic sequences.

SEARCH_SELECTION_CASES_DOC = FIXTURE_DIR / "cases.json"

# The exact frozen goldens whose arguments are confined to query/provider and
# whose complete match set fits the default page. A paginated or
# capability-filtered case must never become a selection oracle here.
SEARCH_SELECTION_CASE_IDS = frozenset({
    "search_metadata_reasoning_text_only",
    "search_capability_defaults_both_models",
    "search_provider_b_shared_stale_scope",
    "search_provider_c_empty_stale_scope",
    "search_text_unicode_upper_echo_first",
    "search_text_unicode_lower_echo_second",
    "search_text_literal_brackets_not_regex",
    "search_text_metadata_array_not_searched",
    "search_text_raw_id_unknown_flags",
    "search_text_padded_query_not_trimmed",
})


def _selection_eligible(case):
    """A search golden usable as a candidate-selection oracle: only
    query/provider arguments and a whole match set inside the default page."""
    expect = case["expect"]
    return (set(case["args"]) <= {"query", "provider"}
            and expect["limit"] == 50
            and expect["offset"] == 0
            and expect["next_offset"] is None
            and expect["total_matches"] == len(expect["endpoints"]))


def test_search_text_provider_pairs_match_eligible_goldens():
    """The ten eligible frozen `search_models` goldens reproduce EXACTLY as
    ordered endpoint identities, with the case set itself pinned so a future
    paginated golden cannot silently widen this oracle."""
    from watchdog_catalog.query import _search_text_provider_pairs

    doc = _decode_doc(SEARCH_SELECTION_CASES_DOC)
    search_cases = [case for case in doc["cases"]
                    if case["tool"] == "search_models"]
    eligible = [case for case in search_cases if _selection_eligible(case)]
    assert len(search_cases) == 29, len(search_cases)
    assert len(eligible) == 10, len(eligible)
    assert {case["id"] for case in eligible} == SEARCH_SELECTION_CASE_IDS

    for case in eligible:
        cid = case["id"]
        fixture = case.get("fixture", doc["fixture"])
        snapshot = _validated_snapshot(fixture)
        before = copy.deepcopy(snapshot)
        query = case["args"].get("query")
        provider = case["args"].get("provider")
        expected = [(row["provider"], row["model_id"])
                    for row in case["expect"]["endpoints"]]
        result = _search_text_provider_pairs(
            snapshot, query=query, provider=provider)
        assert result == expected, cid
        assert type(result) is list, cid
        assert all(type(pair) is tuple and len(pair) == 2 for pair in result), cid
        # Every selected identity is a REAL tracked pair of the source
        # snapshot, and the selection never borrows another provider's row.
        for provider_key, raw_id in result:
            assert provider_key in snapshot["provider_order"], cid
            assert raw_id in snapshot["providers"][provider_key]["model_ids"], \
                (cid, provider_key, raw_id)
        assert snapshot == before, cid
        # A NEW list on every call: mutating one result can never corrupt the
        # snapshot, an earlier result or a later call.
        result.clear()
        assert snapshot == before, cid
        assert _search_text_provider_pairs(
            snapshot, query=query, provider=provider) == expected, cid
        assert snapshot == before, cid


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
    # Capability arrays, pricing, URL, provider key and object key names are
    # never searched.
    ("ImageToken", None, []),
    ("ParamToken", None, []),
    ("urltoken", None, []),
    ("apitoken", None, []),
    ("0.123", None, []),
    ("https", None, []),
    ("zeta", None, []),
    ("pricing", None, []),
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
                  supported_parameters=None, pricing=None):
    """One complete projected public model object (the accepted model shape)."""
    return {
        "id": raw_id,
        "name": name,
        "description": description,
        "context_length": context_length,
        "architecture": {"input_modalities": input_modalities,
                         "output_modalities": output_modalities},
        "supported_parameters": supported_parameters,
        "pricing": pricing,
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
        "schema_version": 1,
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
            supported_parameters=["ParamToken"],
            pricing={"prompt": "0.123", "completion": 0}),
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

    # More than the public default page (50) is returned WHOLE: the internal
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
