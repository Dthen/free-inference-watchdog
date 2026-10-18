"""Contract-integrity tests — standalone catalogue snapshot v1 (units 1a+1b).

Contract-only stage: stdlib data operations only. These tests pin the
frozen v1 snapshot contract (README.md, "Published catalogue snapshot
(v1) — contract draft") against the synthetic golden fixtures under
tests/fixtures/catalog/. They import NO watchdog_catalog
implementation module — validator/query tests are introduced red
within their corresponding Task 2/3 implementation units and made
green there.

Unit 1a froze the top-level/provider/model snapshot contract against
basic.json (minimal valid snapshot, one zero-ID provider). Unit 1b
adds exact-ID and marker-lookup golden cases: lookup.json (shared raw
ids, marker variants, an unrelated renamed model, a second zero-ID
provider) and cases.json (independently hand-specified expected
get_model payloads). Test-local restatements of the contract
marker-strip rule and natural-order key cross-check the golden
payloads against the fixture data — no implementation code is
imported. Later units append their own fixtures/cases.

The fixtures are synthetic by contract: provider keys live in the
documented `synthetic-` namespace, so no fixture encodes the live
provider count or depends on production files.

The envelope-freeze correction closes the provisional `get_model`
response/count vocabulary: `cases.json` now carries the frozen
`provider_staleness` map and the four frozen `counts` cardinalities
(`exact_matches`, `endpoints`, `distinct_raw_ids`,
`marker_group_labels`), pinned here against the hand-authored goldens.
Provider staleness (a per-gateway carried-forward flag) stays distinct
from tick age (derived from `tick_epoch`/`cadence_s`); no source
retrieval/cache field is invented in the pure payload.

The shared-ID audit correction closes an oracle weakness: the two
providers tracking `vendor/shared` used to carry byte-identical metadata,
so the per-pair attachment check could not distinguish a wrong-provider
copy from a correct one. Those two entries now carry intentionally
conflicting, valid, per-provider metadata (synthetic-a: large text-only
context; synthetic-b: image input with a smaller context), the goldens
and canonical hash were recomputed, and in-memory mutation probes prove
the checker rejects both wrong-provider copies/swaps and merged
capabilities. This remains fixture/integrity work — no query
implementation behaviour is added here.

Unit 1c-a adds metadata-edge-cases.json, a small valid synthetic
snapshot that distinguishes UNKNOWN optional metadata (null) from a
KNOWN zero: an unreported context length versus a reported 0, a
whole-null pricing object versus individually-null scalars, and a null
description, plus a stale zero-ID second provider. Four complete,
literal get_model goldens pin one endpoint each and prove optional
degradation is unknown, never loss of the endpoint. The focused
positive controls and in-memory mutation probes below pin the actual
scenario values and the checker's sensitivity to conflating unknown with
zero. Search/normalization goldens follow in unit 1c-b and the
fixed-clock status contract in 1c-c.

Unit 1c-b1 resolves the remaining public-metadata ambiguity: an UNKNOWN
capability is now distinguishable from a KNOWN EMPTY one. `architecture`
stays an exact object, but each `input_modalities`/`output_modalities`
child and `supported_parameters` is now a list of nonempty strings OR
`null` (unknown); a valid empty array stays `[]` and is KNOWN empty.
`capability-unknown.json` is the valid synthetic snapshot that carries one
all-null-capability model next to one known-empty model (plus a stale
zero-ID second provider), pinned by two complete literal get_model goldens.
`metadata-normalization.json` is the independent
raw-input-to-expected-projection-to-expected-normalized data table — an
upstream capability fragment, explicitly NOT a snapshot or query response —
hand-authored and cross-checked by a clearly-labelled test-local rule
restatement (a fixture checker, never production normalization). JSON
Schema confirms the type model this rests on: `null` is a distinct type from
an absent property, and an empty array is always a valid array; those facts
explain the types, not this project's null-means-unknown semantics. Still
fixture/contract work only — no query/projection/validation implementation
is imported or exercised.

Unit 1c-b2 correction closes a filter-before-pagination oracle gap: the
earlier 27 complete search goldens all kept unfiltered pagination separate
from filtered queries, so a wrong "slice the registry, then filter the page"
algorithm still matched every one of them. The appended
search_shared_query_limit2_offset0 golden (query="shared", limit=2, offset=0
over lookup.json) pins a filtered page that crosses two gateways while sharing
one literal raw id (total_matches=2, returned_count=2, next_offset=null,
counts gateways=2 / endpoints=2 / distinct_raw_ids=1 / marker_group_labels=1).
The matrix's "slicing before filtering" entry now carries a GENUINE rule
mutant that derives the unfiltered ordered catalogue from snapshot data,
slices the window BEFORE filtering, and rebuilds a consistent (but wrongly
empty) page with the correct unpaginated total — so the rejection proves wrong
membership, never a truncated golden. Still fixture/contract work only.
"""

import copy
import hashlib
import json
import math
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"
FIXTURE = FIXTURE_DIR / "basic.json"
LOOKUP_FIXTURE = FIXTURE_DIR / "lookup.json"
CASES_DOC = FIXTURE_DIR / "cases.json"
CAPABILITY_FIXTURE_NAME = "capability-unknown.json"
CAPABILITY_FIXTURE = FIXTURE_DIR / CAPABILITY_FIXTURE_NAME
NORMALIZATION_FIXTURE_NAME = "metadata-normalization.json"
NORMALIZATION_FIXTURE = FIXTURE_DIR / NORMALIZATION_FIXTURE_NAME

CONTRACT_SECTION = "Published catalogue snapshot (v1)"

TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "snapshot_id", "tick_epoch", "cadence_s",
    "provider_order", "providers", "warnings",
})
PROVIDER_FIELDS = frozenset({
    "display_name", "chat_completions_url", "api_type",
    "model_ids", "models", "stale",
})
MODEL_FIELDS = frozenset({
    "id", "name", "description", "context_length",
    "architecture", "supported_parameters", "pricing",
})
ARCHITECTURE_FIELDS = frozenset({"input_modalities", "output_modalities"})
PRICING_FIELDS = frozenset({"prompt", "completion"})
GET_MODEL_RESPONSE_FIELDS = frozenset({
    "ok", "query", "snapshot_id", "tick_epoch", "exact_matches",
    "exact_gateways", "provider_staleness", "endpoints", "caveats",
    "counts",
})
ENDPOINT_FIELDS = frozenset({
    "provider", "model_id", "chat_completions_url", "api_type",
    "stale", "metadata",
})
COUNT_FIELDS = frozenset({
    "exact_matches", "endpoints", "distinct_raw_ids",
    "marker_group_labels",
})
WARNING_CODES = frozenset({
    "tick_epoch_unknown", "metadata_missing", "metadata_invalid",
})
# Frozen warning vocabulary ORDER (unit 1c-b1): warning codes are
# deduplicated in this order, so a payload never carries a permuted or
# duplicated code list.
WARNING_ORDER = ("tick_epoch_unknown", "metadata_missing", "metadata_invalid")
assert set(WARNING_ORDER) == WARNING_CODES

# The four normalized capability fields, in their frozen order (unit
# 1c-b1); `unknown_fields` names exactly these, in exactly this order.
CAPABILITY_FIELDS = (
    "context_length", "input_modalities", "output_modalities",
    "supported_parameters",
)

# JavaScript-safe integer ceiling: a context above this is unsafe and so
# projects to unknown (unit 1c-b1). Computed, never guessed.
MAX_SAFE_INT = 2 ** 53 - 1
UNSAFE_INT = 2 ** 53
assert MAX_SAFE_INT + 1 == UNSAFE_INT
# Keys that must never appear anywhere in the public snapshot: config
# dicts, auth/token material, roster internals, probe internals.
FORBIDDEN_KEYS = frozenset({
    "config", "_token", "auth", "env", "token_file", "access_token",
    "api_key", "pending_alerts", "ratelimits", "probe", "stale_providers",
    "transients", "unconfirmed",
})
SYNTHETIC_PROVIDER_PREFIX = "synthetic-"

# The absence caveat every get_model payload must carry (contract:
# "Preserve the absence caveat"). Pinned verbatim.
ABSENCE_CAVEAT = (
    "Cross-gateway ABSENCE is unreliable: gateways rename inconsistently, "
    "so a missing exact id does NOT prove the model is unavailable there."
)

# Marker-strip rule restated from the contract text for fixture
# cross-checks ONLY: strip a delimiter-bounded `free` segment at the
# start/end (`[-:_]` delimiters, case-insensitive), with the
# empty-result guard. NOT imported from build_site or any other
# implementation module.
_MARKER_SUFFIX = re.compile(r"[-:_]free$", re.I)
_MARKER_PREFIX = re.compile(r"^free[-:_]", re.I)


def _reject_constant(constant):
    raise ValueError(f"non-finite JSON constant: {constant}")


def _load_strict(path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      parse_constant=_reject_constant)


def _load_fixture():
    return _load_strict(FIXTURE)


@pytest.fixture(scope="module")
def snapshot():
    return _load_fixture()


@pytest.fixture(scope="module")
def lookup_snapshot():
    return _load_strict(LOOKUP_FIXTURE)


@pytest.fixture(scope="module")
def cases_document():
    return _load_strict(CASES_DOC)


def _is_finite_scalar(value):
    """Finite scalar preserved as returned: number or numeric string."""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            return math.isfinite(float(value))
        except ValueError:
            return False
    return False


def _counts_are_strict_int(value):
    """Counts are non-negative ints; bool is NOT an integer here."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def _assert_string_list_or_null(value):
    """Unit 1c-b1 nullable-array contract: an optional capability array is
    a list of nonempty strings OR `null` (unknown). Any other non-null
    type — a bare string, a dict, a number — and any list element that is
    not a nonempty string are rejected. `[]` is accepted as KNOWN empty."""
    if value is None:
        return
    assert isinstance(value, list), \
        "optional capability array must be a list or null"
    assert all(isinstance(item, str) and item for item in value), \
        "array elements must be nonempty strings"


def _assert_pricing_contract(pricing):
    """Projected `pricing` contract (resolved nullability): the whole
    object is `null` when pricing is unknown overall; otherwise exactly
    `prompt` + `completion`, each a finite scalar preserved as returned
    (number or numeric string) or `null` when individually unknown.
    Booleans and non-finite numbers are invalid."""
    if pricing is None:
        return
    assert isinstance(pricing, dict), \
        "pricing must be the two-scalar object or null"
    assert set(pricing) == PRICING_FIELDS, \
        f"pricing must carry exactly {sorted(PRICING_FIELDS)}"
    for value in pricing.values():
        assert _is_finite_scalar(value), \
            "pricing scalars must be finite numbers/strings or null"


def _strip_marker_contract(raw_id):
    """Contract restatement of the free-marker stripper (above)."""
    stripped = _MARKER_SUFFIX.sub("", raw_id)
    stripped = _MARKER_PREFIX.sub("", stripped)
    return stripped or raw_id


def _natural_key_contract(raw_id):
    """Contract restatement of result ordering: provider display order,
    then natural raw-ID order — ASCII digit runs compared by digit
    length and lexical order, never locale-dependent sorting or
    floating-point conversion of long digit sequences."""
    return [(len(part), part) if part.isdigit() else part
            for part in re.split(r"(\d+)", raw_id)]


# ---------- shared snapshot-contract integrity ----------


def _assert_snapshot_contract(snap):
    """Every integrity assertion of the frozen v1 snapshot contract.
    Applied to any fixture snapshot: unit 1a pins basic.json, unit 1b
    pins lookup.json, later units pin their own fixtures."""
    assert set(snap) == TOP_LEVEL_FIELDS

    version = snap["schema_version"]
    assert isinstance(version, int) and not isinstance(version, bool)
    assert version == 1

    snapshot_id = snap["snapshot_id"]
    assert isinstance(snapshot_id, str)
    assert re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
    body = {k: v for k, v in snap.items() if k != "snapshot_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    assert snapshot_id == hashlib.sha256(canonical).hexdigest()

    tick = snap["tick_epoch"]
    assert isinstance(tick, int) and not isinstance(tick, bool)
    assert tick >= 0

    cadence = snap["cadence_s"]
    assert isinstance(cadence, int) and not isinstance(cadence, bool)
    assert cadence > 0

    order = snap["provider_order"]
    assert isinstance(order, list)
    assert order, "provider_order must list active providers"
    assert all(isinstance(key, str) and key for key in order)
    assert len(order) == len(set(order)), "provider_order has duplicates"
    assert set(order) == set(snap["providers"])

    # Contract: provider_order includes providers with zero tracked IDs.
    empty = [key for key, prov in snap["providers"].items()
             if not prov["model_ids"]]
    assert empty, "fixture must include a zero-ID provider"
    for key in empty:
        assert key in order

    for key, prov in snap["providers"].items():
        assert key.startswith(SYNTHETIC_PROVIDER_PREFIX)
        assert set(prov) == PROVIDER_FIELDS
        assert isinstance(prov["display_name"], str) and prov["display_name"]
        assert isinstance(prov["chat_completions_url"], str)
        assert isinstance(prov["api_type"], str) and prov["api_type"]
        ids = prov["model_ids"]
        assert isinstance(ids, list)
        assert all(isinstance(i, str) and i for i in ids)
        assert ids == sorted(set(ids)), "model_ids must be sorted+deduplicated"
        assert set(prov["models"]) == set(ids), \
            "models must be keyed by exactly the tracked raw IDs"
        assert isinstance(prov["stale"], bool)

    for prov in snap["providers"].values():
        for raw_id, model in prov["models"].items():
            assert set(model) == MODEL_FIELDS
            assert model["id"] == raw_id
            assert isinstance(model["name"], str) and model["name"]
            assert model["description"] is None or \
                isinstance(model["description"], str)
            ctx = model["context_length"]
            assert ctx is None or (
                isinstance(ctx, int) and not isinstance(ctx, bool)
                and 0 <= ctx <= MAX_SAFE_INT), \
                "context_length must be a safe non-negative int or null"
            assert set(model["architecture"]) == ARCHITECTURE_FIELDS
            for modality in ("input_modalities", "output_modalities"):
                _assert_string_list_or_null(model["architecture"][modality])
            _assert_string_list_or_null(model["supported_parameters"])
            _assert_pricing_contract(model["pricing"])

    warnings = snap["warnings"]
    assert isinstance(warnings, list)
    assert all(isinstance(w, str) for w in warnings)
    assert set(warnings) <= WARNING_CODES

    keys = set(order)
    assert keys, "fixture must contain providers"
    assert all(key.startswith(SYNTHETIC_PROVIDER_PREFIX) for key in keys)

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in FORBIDDEN_KEYS, \
                    f"private/roster key leaked into snapshot: {key}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(snap)


# ---------- fixture parsing ----------


def test_fixture_parses_as_strict_json():
    snap = _load_fixture()  # raises on NaN/Infinity constants
    assert isinstance(snap, dict)


# ---------- top-level contract (unit 1a: basic.json) ----------


def test_top_level_field_set_is_exact(snapshot):
    _assert_snapshot_contract(snapshot)


def test_schema_version_is_v1(snapshot):
    _assert_snapshot_contract(snapshot)


def test_snapshot_id_is_sha256_of_canonical_body(snapshot):
    _assert_snapshot_contract(snapshot)


def test_tick_epoch_is_fixed_non_negative_int(snapshot):
    _assert_snapshot_contract(snapshot)


def test_cadence_s_is_positive_int(snapshot):
    _assert_snapshot_contract(snapshot)


def test_provider_order_matches_provider_map(snapshot):
    _assert_snapshot_contract(snapshot)


def test_provider_order_includes_zero_id_providers(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- provider entries ----------


def test_provider_entries_match_contract(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- projected model fields ----------


def test_model_projections_match_contract(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- warnings ----------


def test_warnings_use_frozen_codes(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- synthetic, self-contained, credential-free ----------


def test_fixture_is_synthetic_not_live(snapshot):
    _assert_snapshot_contract(snapshot)


def test_fixture_carries_no_private_or_roster_internal_keys(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- unit 1b: lookup.json fixture integrity ----------


def test_lookup_fixture_matches_snapshot_contract(lookup_snapshot):
    _assert_snapshot_contract(lookup_snapshot)


# ---------- unit 1b: cases.json integrity ----------


def _iter_get_model_cases(cases_doc):
    return [case for case in cases_doc["cases"]
            if case["tool"] == "get_model"]


def _case_fixture(case, cases_doc):
    name = case.get("fixture", cases_doc["fixture"])
    return _load_strict(FIXTURE_DIR / name)


def test_cases_document_parses_as_strict_json():
    # _load_strict would have raised on NaN/Infinity constants.
    doc = _load_strict(CASES_DOC)
    assert isinstance(doc["cases"], list) and doc["cases"]
    assert isinstance(doc["marker_strip_rule"], list) \
        and doc["marker_strip_rule"]


def test_cases_reference_snapshots_matching_contract(cases_document):
    default = cases_document["fixture"]
    assert isinstance(default, str) and default
    for case in cases_document["cases"]:
        name = case.get("fixture", default)
        _assert_snapshot_contract(_load_strict(FIXTURE_DIR / name))


def test_get_model_golden_payloads_are_wellformed(cases_document):
    for case in _iter_get_model_cases(cases_document):
        expect = case["expect"]
        snap = _case_fixture(case, cases_document)
        assert expect["ok"] is True
        assert expect["query"] == case["args"]["model_id"]
        assert expect["snapshot_id"] == snap["snapshot_id"]
        assert expect["tick_epoch"] == snap["tick_epoch"]
        assert set(expect["exact_matches"]) == set(snap["provider_order"])
        assert all(isinstance(v, bool)
                   for v in expect["exact_matches"].values())
        assert expect["exact_gateways"] == [
            p for p in snap["provider_order"] if expect["exact_matches"][p]]
        # Contract: "Preserve the absence caveat" on every payload.
        assert ABSENCE_CAVEAT in expect["caveats"]
        counts = expect["counts"]
        assert counts["endpoints"] == len(expect["endpoints"])
        assert counts["exact_matches"] == len(expect["exact_gateways"])


def test_get_model_endpoints_are_tracked_pairs_with_attached_metadata(
        cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        for endpoint in case["expect"]["endpoints"]:
            provider = endpoint["provider"]
            assert provider in snap["provider_order"]
            entry = snap["providers"][provider]
            raw_id = endpoint["model_id"]
            assert raw_id in entry["model_ids"]
            assert endpoint["chat_completions_url"] == \
                entry["chat_completions_url"]
            assert endpoint["api_type"] == entry["api_type"]
            assert endpoint["stale"] == entry["stale"]
            # Metadata stays attached to its own (provider, raw id) pair.
            assert endpoint["metadata"] == entry["models"][raw_id]


def test_get_model_endpoints_match_marker_rule_and_order(cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        query = case["args"]["model_id"]
        needle = _strip_marker_contract(query)
        expected = []
        for provider in snap["provider_order"]:
            for raw_id in snap["providers"][provider]["model_ids"]:
                if _strip_marker_contract(raw_id) == needle:
                    expected.append((provider, raw_id))
        expected.sort(key=lambda pair: (
            snap["provider_order"].index(pair[0]),
            _natural_key_contract(pair[1])))
        golden = [(e["provider"], e["model_id"])
                  for e in case["expect"]["endpoints"]]
        assert golden == expected, \
            f"{case['id']}: endpoints violate marker rule or ordering"


def test_no_inferred_identity_renamed_models_never_marker_match(
        cases_document):
    base = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == "get_model_marker_base_no_exact")
    endpoint_ids = [e["model_id"] for e in base["expect"]["endpoints"]]
    assert "vendor/model-renamed" not in endpoint_ids
    # The renamed model differs from the oracle base by more than a
    # delimiter-bounded free marker, so no marker rule may group them.
    assert _strip_marker_contract("vendor/model-renamed") == \
        "vendor/model-renamed"
    assert _strip_marker_contract("vendor/model-renamed") != \
        _strip_marker_contract("vendor/model")


def test_marker_strip_rule_matches_contract_restatement(cases_document):
    for row in cases_document["marker_strip_rule"]:
        raw_id, stripped = row["raw_id"], row["stripped"]
        assert isinstance(raw_id, str) and raw_id
        assert isinstance(stripped, str) and stripped
        assert _strip_marker_contract(raw_id) == stripped, (
            f"marker rule mismatch for {raw_id!r}: "
            f"contract restates {stripped!r}")


def test_unknown_id_absence_is_success_not_error(cases_document):
    unknown = next(c for c in _iter_get_model_cases(cases_document)
                   if c["id"] == "get_model_unknown_id_empty_success")
    expect = unknown["expect"]
    assert expect["ok"] is True
    assert expect["endpoints"] == []
    assert expect["exact_gateways"] == []
    # The id is genuinely absent from the fixture: a no-match lookup
    # degrades to an empty successful result, never an error payload.
    tracked = {
        raw_id
        for prov in _case_fixture(unknown, cases_document)["providers"].values()
        for raw_id in prov["model_ids"]}
    assert unknown["args"]["model_id"] not in tracked


def test_padded_query_golden_pins_no_trimming(cases_document):
    padded = next(c for c in _iter_get_model_cases(cases_document)
                  if c["id"] == "get_model_padded_query_not_trimmed")
    expect = padded["expect"]
    assert expect["ok"] is True
    assert expect["endpoints"] == []
    # The pin bites: the unpadded base DOES have marker matches.
    snap = _case_fixture(padded, cases_document)
    needle = _strip_marker_contract(padded["args"]["model_id"].strip())
    assert any(
        _strip_marker_contract(raw_id) == needle
        for prov in snap["providers"].values()
        for raw_id in prov["model_ids"])


# ---------- frozen get_model success envelope (envelope-freeze correction) ----------


def _assert_get_model_payload(case, snap):
    """Every integrity assertion of the frozen successful PURE get_model
    payload (README "Success envelope — `get_model`"). Validates the
    hand-authored golden against its own snapshot fixture with stdlib
    data operations only — no query implementation is imported, and the
    expected result is never generated by a future implementation."""
    expect = case["expect"]

    # Exact top-level field set: a stray or legacy key is corruption.
    assert set(expect) == GET_MODEL_RESPONSE_FIELDS, (
        f"{case['id']}: unexpected response fields "
        f"{sorted(set(expect) ^ GET_MODEL_RESPONSE_FIELDS)}")

    # Success is explicit — never an error envelope.
    assert expect["ok"] is True
    assert isinstance(expect["query"], str)
    assert expect["query"] == case["args"]["model_id"], \
        "query must be the raw argument, never trimmed or repaired"

    # Observation provenance — the payload mirrors the consumed snapshot.
    assert isinstance(expect["snapshot_id"], str)
    assert re.fullmatch(r"[0-9a-f]{64}", expect["snapshot_id"])
    assert expect["snapshot_id"] == snap["snapshot_id"]
    assert expect["tick_epoch"] == snap["tick_epoch"]
    tick = expect["tick_epoch"]
    assert tick is None or (isinstance(tick, int)
                            and not isinstance(tick, bool))

    # Literal raw-ID membership covers ALL snapshot providers.
    assert set(expect["exact_matches"]) == set(snap["provider_order"])
    assert all(isinstance(v, bool)
               for v in expect["exact_matches"].values())
    assert expect["exact_gateways"] == [
        p for p in snap["provider_order"] if expect["exact_matches"][p]]

    # Provider staleness covers all snapshot providers and mirrors the
    # roster's carried-forward stale flags (a distinct signal from tick
    # age, which is derived from tick_epoch/cadence_s).
    assert set(expect["provider_staleness"]) == set(snap["provider_order"])
    assert all(isinstance(v, bool)
               for v in expect["provider_staleness"].values())
    for provider in snap["provider_order"]:
        assert expect["provider_staleness"][provider] == \
            snap["providers"][provider]["stale"], \
            f"{case['id']}: provider staleness mismatch for {provider}"

    # Endpoints: tracked pairs, per-pair projected metadata, order.
    assert isinstance(expect["endpoints"], list)
    for endpoint in expect["endpoints"]:
        assert set(endpoint) == ENDPOINT_FIELDS, (
            f"{case['id']}: unexpected endpoint fields "
            f"{sorted(set(endpoint) ^ ENDPOINT_FIELDS)}")
        provider = endpoint["provider"]
        raw_id = endpoint["model_id"]
        assert provider in snap["providers"]
        entry = snap["providers"][provider]
        assert isinstance(raw_id, str) and raw_id in entry["model_ids"], \
            f"{case['id']}: endpoint pair ({provider!r}, {raw_id!r}) " \
            "is not a tracked pair"
        assert endpoint["chat_completions_url"] == \
            entry["chat_completions_url"]
        assert endpoint["api_type"] == entry["api_type"]
        assert isinstance(endpoint["stale"], bool)
        assert endpoint["stale"] == entry["stale"] == \
            expect["provider_staleness"][provider], \
            f"{case['id']}: endpoint staleness does not match provider " \
            "staleness"
        # `metadata` is the projected public model object of THIS exact
        # (provider, raw model_id) pair — never the legacy raw object.
        metadata = endpoint["metadata"]
        assert isinstance(metadata, dict)
        assert set(metadata) == MODEL_FIELDS
        assert metadata == entry["models"][raw_id], (
            f"{case['id']}: metadata for pair ({provider!r}, "
            f"{raw_id!r}) is not attached to that exact "
            "(provider, raw model_id) pair")
        assert metadata["id"] == raw_id
        _assert_pricing_contract(metadata["pricing"])

    # The absence caveat is preserved on every payload.
    assert isinstance(expect["caveats"], list) and expect["caveats"]
    assert ABSENCE_CAVEAT in expect["caveats"]

    # Counts: exact set, strict non-negative ints (booleans invalid),
    # and the frozen cardinality definitions over the returned result.
    counts = expect["counts"]
    assert set(counts) == COUNT_FIELDS, (
        f"{case['id']}: unexpected count fields "
        f"{sorted(set(counts) ^ COUNT_FIELDS)}")
    for name, value in counts.items():
        assert _counts_are_strict_int(value), \
            f"{case['id']}: counts.{name} must be a non-negative int"
    assert counts["exact_matches"] == len(expect["exact_gateways"])
    assert counts["endpoints"] == len(expect["endpoints"])
    assert counts["distinct_raw_ids"] == \
        len({e["model_id"] for e in expect["endpoints"]})
    assert counts["marker_group_labels"] == \
        len({_strip_marker_contract(e["model_id"])
             for e in expect["endpoints"]})

    # Array order (provider display order, then natural raw-ID order)
    # and marker-rule membership, recomputed independently from the
    # snapshot — never read back from the golden.
    needle = _strip_marker_contract(expect["query"])
    expected_pairs = []
    for provider in snap["provider_order"]:
        for raw_id in snap["providers"][provider]["model_ids"]:
            if _strip_marker_contract(raw_id) == needle:
                expected_pairs.append((provider, raw_id))
    expected_pairs.sort(key=lambda pair: (
        snap["provider_order"].index(pair[0]),
        _natural_key_contract(pair[1])))
    golden_pairs = [(e["provider"], e["model_id"])
                    for e in expect["endpoints"]]
    assert golden_pairs == expected_pairs, \
        f"{case['id']}: endpoints violate marker rule or ordering"


def _copy_case(cases_document, case_id):
    """Deep-copied golden case + its snapshot, for in-memory mutation
    positive controls. No committed fixture is ever corrupted."""
    case = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == case_id)
    return copy.deepcopy(case), _case_fixture(case, cases_document)


def test_get_model_response_envelope_field_sets_are_frozen(cases_document):
    assert set(GET_MODEL_RESPONSE_FIELDS) == {
        "ok", "query", "snapshot_id", "tick_epoch", "exact_matches",
        "exact_gateways", "provider_staleness", "endpoints", "caveats",
        "counts"}
    assert set(ENDPOINT_FIELDS) == {
        "provider", "model_id", "chat_completions_url", "api_type",
        "stale", "metadata"}
    assert set(COUNT_FIELDS) == {
        "exact_matches", "endpoints", "distinct_raw_ids",
        "marker_group_labels"}
    for case in _iter_get_model_cases(cases_document):
        _assert_get_model_payload(case, _case_fixture(case, cases_document))


def test_get_model_counts_use_strict_int_types(cases_document):
    for case in _iter_get_model_cases(cases_document):
        counts = case["expect"]["counts"]
        assert set(counts) == COUNT_FIELDS
        for name, value in counts.items():
            assert _counts_are_strict_int(value), \
                f"{case['id']}: counts.{name}={value!r} is not a strict int"
    # bool subclasses int, so the guard must be tested explicitly.
    assert not _counts_are_strict_int(True)
    assert not _counts_are_strict_int(False)


def test_get_model_provider_staleness_covers_all_providers(cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        staleness = case["expect"]["provider_staleness"]
        assert set(staleness) == set(snap["provider_order"])
        for provider in snap["provider_order"]:
            assert staleness[provider] is \
                snap["providers"][provider]["stale"]


def test_get_model_endpoint_staleness_matches_provider_staleness(
        cases_document):
    for case in _iter_get_model_cases(cases_document):
        expect = case["expect"]
        for endpoint in expect["endpoints"]:
            assert endpoint["stale"] is \
                expect["provider_staleness"][endpoint["provider"]]


def test_get_model_metadata_is_projected_pair_object(cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        for endpoint in case["expect"]["endpoints"]:
            metadata = endpoint["metadata"]
            # Non-null projected public object; the legacy raw metadata
            # object is never exposed (exact field set only).
            assert isinstance(metadata, dict)
            assert set(metadata) == MODEL_FIELDS
            assert metadata["id"] == endpoint["model_id"]
            assert metadata == snap["providers"][endpoint["provider"]][
                "models"][endpoint["model_id"]]


def test_get_model_observation_provenance_matches_snapshot(cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        expect = case["expect"]
        assert expect["snapshot_id"] == snap["snapshot_id"]
        assert expect["tick_epoch"] == snap["tick_epoch"]
        assert re.fullmatch(r"[0-9a-f]{64}", expect["snapshot_id"])


def test_get_model_absence_is_zero_count_success(cases_document):
    unknown = next(c for c in _iter_get_model_cases(cases_document)
                   if c["id"] == "get_model_unknown_id_empty_success")
    expect = unknown["expect"]
    snap = _case_fixture(unknown, cases_document)
    assert expect["ok"] is True
    assert expect["exact_gateways"] == [] and expect["endpoints"] == []
    assert expect["counts"] == {
        "exact_matches": 0, "endpoints": 0,
        "distinct_raw_ids": 0, "marker_group_labels": 0}
    # Zero results are still a success, and provider_staleness still
    # covers every snapshot provider.
    assert set(expect["provider_staleness"]) == set(snap["provider_order"])


def test_get_model_shared_raw_id_is_two_endpoints_one_distinct_id(
        cases_document):
    shared = next(c for c in _iter_get_model_cases(cases_document)
                  if c["id"] == "get_model_shared_raw_id_two_endpoints")
    counts = shared["expect"]["counts"]
    assert counts["endpoints"] == 2
    assert counts["distinct_raw_ids"] == 1
    assert counts["exact_matches"] == 2
    raw_ids = {e["model_id"] for e in shared["expect"]["endpoints"]}
    assert raw_ids == {"vendor/shared"}


def test_get_model_marker_variant_separates_raw_ids_from_marker_label(
        cases_document):
    base = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == "get_model_marker_base_no_exact")
    counts = base["expect"]["counts"]
    # Two marker variants, two distinct raw ids, one marker-group label,
    # and no literal raw-ID membership (the query id itself is absent).
    assert counts["endpoints"] == 2
    assert counts["distinct_raw_ids"] == 2
    assert counts["marker_group_labels"] == 1
    assert counts["exact_matches"] == 0
    assert counts["distinct_raw_ids"] != counts["marker_group_labels"]


def test_pricing_contract_accepts_documented_null_forms():
    # Whole object unknown.
    _assert_pricing_contract(None)
    # Individually unknown scalars.
    _assert_pricing_contract({"prompt": None, "completion": None})
    _assert_pricing_contract({"prompt": "0", "completion": None})
    _assert_pricing_contract({"prompt": None, "completion": 0})
    # Provider-returned valid scalar types are preserved.
    _assert_pricing_contract({"prompt": "0", "completion": 0})
    _assert_pricing_contract({"prompt": 0.5, "completion": "1"})


def test_pricing_contract_rejects_bools_and_non_finite():
    for bad in ({"prompt": True, "completion": "0"},
                {"prompt": "0", "completion": False},
                {"prompt": float("inf"), "completion": "0"},
                {"prompt": float("nan"), "completion": "0"},
                {"prompt": "free", "completion": "0"}):
        with pytest.raises(AssertionError):
            _assert_pricing_contract(bad)
    with pytest.raises(AssertionError):
        _assert_pricing_contract({"prompt": "0"})  # missing scalar


def test_get_model_integrity_rejects_unexpected_response_key(cases_document):
    case, snap = _copy_case(cases_document, "get_model_marker_base_no_exact")
    case["expect"]["surprise"] = 1
    with pytest.raises(AssertionError, match="unexpected response fields"):
        _assert_get_model_payload(case, snap)


def test_get_model_integrity_rejects_bool_count(cases_document):
    case, snap = _copy_case(cases_document, "get_model_marker_base_no_exact")
    case["expect"]["counts"]["endpoints"] = True
    with pytest.raises(AssertionError, match="non-negative int"):
        _assert_get_model_payload(case, snap)


def test_get_model_integrity_rejects_endpoint_raw_id_conflation(
        cases_document):
    case, snap = _copy_case(cases_document, "get_model_marker_base_no_exact")
    # Claim synthetic-a's raw id on the synthetic-b endpoint pair.
    assert case["expect"]["endpoints"][1]["provider"] == "synthetic-b"
    case["expect"]["endpoints"][1]["model_id"] = "vendor/model:free"
    with pytest.raises(AssertionError, match="not a tracked pair"):
        _assert_get_model_payload(case, snap)


def test_get_model_integrity_rejects_staleness_mismatch(cases_document):
    case, snap = _copy_case(cases_document,
                            "get_model_shared_raw_id_two_endpoints")
    # Baseline is all-false; flip ONLY the payload's provider flag.
    assert snap["providers"]["synthetic-a"]["stale"] is False
    case["expect"]["provider_staleness"]["synthetic-a"] = True
    with pytest.raises(AssertionError, match="provider staleness mismatch"):
        _assert_get_model_payload(case, snap)


def test_get_model_integrity_positive_control_recognises_true_staleness(
        cases_document):
    # The all-false baseline cannot prove TRUE-value handling, so mutate
    # the snapshot copy AND the payload to a consistent genuine stale=true
    # pair and require the assertion to PASS...
    case, snap = _copy_case(cases_document,
                            "get_model_shared_raw_id_two_endpoints")
    snap["providers"]["synthetic-a"]["stale"] = True
    case["expect"]["provider_staleness"]["synthetic-a"] = True
    case["expect"]["endpoints"][0]["stale"] = True
    _assert_get_model_payload(case, snap)
    # ...then break only the payload flag: the assertion must catch it.
    case["expect"]["endpoints"][0]["stale"] = False
    with pytest.raises(AssertionError, match="endpoint staleness"):
        _assert_get_model_payload(case, snap)


# ---------- shared raw-ID per-pair metadata contrast (audit correction) ----------
#
# The shared raw id `vendor/shared` is tracked by BOTH synthetic-a and
# synthetic-b. Before this correction the two metadata objects were
# byte-identical, so a per-raw-id (or bare dict-equality) check could not
# tell a wrong-provider attachment from a correct one — an oracle
# weakness, not a runtime bug. The two entries now carry intentionally
# conflicting, valid, per-provider metadata so that attachment by
# (provider, raw model_id) is testable, and the mutation probes below
# prove the checker bites.

SHARED_CASE_ID = "get_model_shared_raw_id_two_endpoints"

# Positive controls pinned by the audit correction. synthetic-a carries a
# large context but text-only input; synthetic-b carries image input but a
# smaller context. Deliberately NO endpoint satisfies BOTH a 65536
# context minimum and image input — that pair of facts is data for later
# search tests, not search behaviour now.
SHARED_A_CONTEXT = 131072
SHARED_A_MODALITIES = ["text"]
SHARED_A_PARAMS = ["temperature", "max_tokens"]
SHARED_B_CONTEXT = 32768
SHARED_B_MODALITIES = ["text", "image"]
SHARED_B_PARAMS = ["temperature", "top_p"]

# Substring of the per-pair metadata assertion message in
# `_assert_get_model_payload`; the mutation probes below must trip THAT
# assertion specifically, not a field-set, membership or order assertion.
METADATA_ATTACHMENT_MARKER = "is not attached to that exact"


def _shared_case(cases_document):
    case = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == SHARED_CASE_ID)
    return case, _case_fixture(case, cases_document)


def test_shared_raw_id_case_pairs_carry_conflicting_per_provider_metadata(
        cases_document):
    case, snap = _shared_case(cases_document)
    endpoints = case["expect"]["endpoints"]
    assert [(e["provider"], e["model_id"]) for e in endpoints] == [
        ("synthetic-a", "vendor/shared"),
        ("synthetic-b", "vendor/shared")]

    a_meta = endpoints[0]["metadata"]
    b_meta = endpoints[1]["metadata"]

    # The actual contrast — not merely "the two dicts differ".
    assert a_meta["context_length"] == SHARED_A_CONTEXT
    assert a_meta["architecture"]["input_modalities"] == SHARED_A_MODALITIES
    assert a_meta["supported_parameters"] == SHARED_A_PARAMS
    assert b_meta["context_length"] == SHARED_B_CONTEXT
    assert b_meta["architecture"]["input_modalities"] == SHARED_B_MODALITIES
    assert b_meta["supported_parameters"] == SHARED_B_PARAMS
    assert a_meta["description"] != b_meta["description"]

    # Each object is bound to its own provider's stored model and would
    # NOT be a valid attachment for the other provider.
    a_entry = snap["providers"]["synthetic-a"]["models"]["vendor/shared"]
    b_entry = snap["providers"]["synthetic-b"]["models"]["vendor/shared"]
    assert a_meta == a_entry
    assert b_meta == b_entry
    assert a_meta != b_entry
    assert b_meta != a_entry

    # Both correctly attached objects still pass the full payload
    # integrity checker (envelope, membership, projection, counts).
    _assert_get_model_payload(case, snap)


def test_shared_raw_id_case_has_no_endpoint_with_large_context_and_image(
        cases_document):
    case, _ = _shared_case(cases_document)
    endpoints = case["expect"]["endpoints"]
    for endpoint in endpoints:
        meta = endpoint["metadata"]
        has_image = "image" in meta["architecture"]["input_modalities"]
        has_large_context = meta["context_length"] >= 65536
        assert not (has_image and has_large_context), (
            f"{endpoint['provider']}: shared pair must not satisfy both "
            "the 65536 context minimum and image input")
    # Non-vacuous: each satisfiable half exists somewhere in the pair.
    assert any(e["metadata"]["context_length"] >= 65536 for e in endpoints)
    assert any("image" in e["metadata"]["architecture"]["input_modalities"]
               for e in endpoints)


def test_shared_raw_id_integrity_rejects_a_metadata_on_b_endpoint(
        cases_document):
    case, snap = _copy_case(cases_document, SHARED_CASE_ID)
    endpoints = case["expect"]["endpoints"]
    endpoints[1]["metadata"] = copy.deepcopy(endpoints[0]["metadata"])
    # Provider/raw-id identity is untouched: only the attachment is wrong.
    assert [(e["provider"], e["model_id"]) for e in endpoints] == [
        ("synthetic-a", "vendor/shared"),
        ("synthetic-b", "vendor/shared")]
    assert endpoints[1]["metadata"]["id"] == "vendor/shared"
    with pytest.raises(AssertionError, match=METADATA_ATTACHMENT_MARKER):
        _assert_get_model_payload(case, snap)


def test_shared_raw_id_integrity_rejects_b_metadata_on_a_endpoint(
        cases_document):
    case, snap = _copy_case(cases_document, SHARED_CASE_ID)
    endpoints = case["expect"]["endpoints"]
    endpoints[0]["metadata"] = copy.deepcopy(endpoints[1]["metadata"])
    assert [(e["provider"], e["model_id"]) for e in endpoints] == [
        ("synthetic-a", "vendor/shared"),
        ("synthetic-b", "vendor/shared")]
    assert endpoints[0]["metadata"]["id"] == "vendor/shared"
    with pytest.raises(AssertionError, match=METADATA_ATTACHMENT_MARKER):
        _assert_get_model_payload(case, snap)


def test_shared_raw_id_integrity_rejects_swapped_metadata(cases_document):
    case, snap = _copy_case(cases_document, SHARED_CASE_ID)
    endpoints = case["expect"]["endpoints"]
    a_meta = copy.deepcopy(endpoints[0]["metadata"])
    b_meta = copy.deepcopy(endpoints[1]["metadata"])
    endpoints[0]["metadata"] = b_meta
    endpoints[1]["metadata"] = a_meta
    assert [(e["provider"], e["model_id"]) for e in endpoints] == [
        ("synthetic-a", "vendor/shared"),
        ("synthetic-b", "vendor/shared")]
    with pytest.raises(AssertionError, match=METADATA_ATTACHMENT_MARKER):
        _assert_get_model_payload(case, snap)


def test_shared_raw_id_integrity_rejects_merged_capabilities(cases_document):
    case, snap = _copy_case(cases_document, SHARED_CASE_ID)
    endpoint = case["expect"]["endpoints"][0]  # synthetic-a
    assert endpoint["provider"] == "synthetic-a"
    merged = copy.deepcopy(endpoint["metadata"])
    # Manufacture the single endpoint the contract forbids: synthetic-a's
    # large context combined with synthetic-b's image input.
    assert merged["context_length"] == SHARED_A_CONTEXT
    assert "image" not in merged["architecture"]["input_modalities"]
    merged["architecture"]["input_modalities"] = copy.deepcopy(
        SHARED_B_MODALITIES)
    endpoint["metadata"] = merged
    assert endpoint["model_id"] == "vendor/shared"
    assert endpoint["metadata"]["id"] == "vendor/shared"
    assert endpoint["metadata"] != \
        snap["providers"]["synthetic-a"]["models"]["vendor/shared"]
    with pytest.raises(AssertionError, match=METADATA_ATTACHMENT_MARKER):
        _assert_get_model_payload(case, snap)


# ---------- unit 1c-a: unknown versus known-zero optional metadata ----------
#
# metadata-edge-cases.json is a small valid synthetic snapshot whose
# provider `synthetic-a` tracks four models that deliberately distinguish
# UNKNOWN optional metadata (null) from a KNOWN zero (0): an unreported
# context length versus a reported 0, a whole-null pricing object versus
# individually-null scalars, and a null description. `synthetic-b` tracks
# zero IDs but is stale, so a correct payload must still carry it in the
# exact-membership and staleness maps. The four get_model goldens below
# are complete, literal, hand-authored payloads (never generated from an
# implementation or from the fixture at run time) — this is fixture/oracle
# work only; the oracle's search behaviour is pinned separately in 1c-b and
# fixed-clock status in 1c-c.

METADATA_FIXTURE_NAME = "metadata-edge-cases.json"
METADATA_FIXTURE = FIXTURE_DIR / METADATA_FIXTURE_NAME

METADATA_UNKNOWN_CONTEXT = "vendor/unknown-context"
METADATA_KNOWN_CONTEXT = "vendor/known-context"
METADATA_ZERO_CONTEXT = "vendor/zero-context"
METADATA_UNKNOWN_DESCRIPTION = "vendor/unknown-description"
# Sorted raw-ID order, matching the snapshot's sorted+deduplicated list.
METADATA_TRACKED_IDS = (
    METADATA_KNOWN_CONTEXT,
    METADATA_UNKNOWN_CONTEXT,
    METADATA_UNKNOWN_DESCRIPTION,
    METADATA_ZERO_CONTEXT,
)
METADATA_CASE_IDS = {
    METADATA_UNKNOWN_CONTEXT: "get_model_metadata_unknown_context_null",
    METADATA_KNOWN_CONTEXT: "get_model_metadata_known_context_inclusive_boundary",
    METADATA_ZERO_CONTEXT: "get_model_metadata_zero_context_known_zero",
    METADATA_UNKNOWN_DESCRIPTION: "get_model_metadata_unknown_description_null",
}

# The one word every non-null scenario description must carry: the oracle
# text-search case later matches on exactly this word.
REASONING_WORD = "reasoning"
# Positive-control pins — the actual scenario values, restated so a helper
# that merely "passes" cannot hide a dropped or conflated value.
METADATA_INCLUSIVE_CONTEXT = 65536
METADATA_ARCHITECTURE = {
    "input_modalities": ["text"],
    "output_modalities": ["text"],
}
METADATA_SUPPORTED_PARAMETERS = ["temperature"]


@pytest.fixture(scope="module")
def metadata_snapshot():
    return _load_strict(METADATA_FIXTURE)


def _metadata_case(cases_document, case_id):
    case = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == case_id)
    return case, _case_fixture(case, cases_document)


def _metadata_endpoint(case):
    endpoints = case["expect"]["endpoints"]
    assert len(endpoints) == 1, \
        f"{case['id']}: expected exactly one a-provider endpoint"
    return endpoints[0]


def _metadata_entry(snap, raw_id):
    return snap["providers"]["synthetic-a"]["models"][raw_id]


def test_metadata_fixture_matches_snapshot_contract(metadata_snapshot):
    _assert_snapshot_contract(metadata_snapshot)


def test_metadata_fixture_shape_is_frozen(metadata_snapshot):
    assert metadata_snapshot["schema_version"] == 1
    assert metadata_snapshot["tick_epoch"] == 1759200000
    assert metadata_snapshot["cadence_s"] == 3600
    assert metadata_snapshot["warnings"] == ["metadata_missing"]
    assert metadata_snapshot["provider_order"] == ["synthetic-a", "synthetic-b"]
    a = metadata_snapshot["providers"]["synthetic-a"]
    b = metadata_snapshot["providers"]["synthetic-b"]
    assert a["stale"] is False and b["stale"] is True
    assert a["model_ids"] == list(METADATA_TRACKED_IDS)
    assert sorted(a["models"]) == list(METADATA_TRACKED_IDS)
    assert b["model_ids"] == [] and b["models"] == {}


def test_metadata_fixture_snapshot_id_is_canonical(metadata_snapshot):
    body = {k: v for k, v in metadata_snapshot.items() if k != "snapshot_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    assert metadata_snapshot["snapshot_id"] == \
        hashlib.sha256(canonical).hexdigest()


def test_metadata_goldens_use_new_fixture_and_pass_complete_checkers(
        cases_document):
    expected_id = _load_strict(METADATA_FIXTURE)["snapshot_id"]
    for raw_id in METADATA_TRACKED_IDS:
        case, snap = _metadata_case(cases_document, METADATA_CASE_IDS[raw_id])
        # Explicit fixture override — never the cases.json default.
        assert case["fixture"] == METADATA_FIXTURE_NAME
        assert case["args"]["model_id"] == raw_id
        assert snap["snapshot_id"] == expected_id
        # Reuse the frozen snapshot + get_model payload checkers verbatim.
        _assert_snapshot_contract(snap)
        _assert_get_model_payload(case, snap)


def test_metadata_goldens_pin_envelope_and_zero_id_stale_provider(
        cases_document):
    for raw_id in METADATA_TRACKED_IDS:
        case, snap = _metadata_case(cases_document, METADATA_CASE_IDS[raw_id])
        expect = case["expect"]
        assert expect["exact_matches"] == {
            "synthetic-a": True, "synthetic-b": False}
        assert expect["exact_gateways"] == ["synthetic-a"]
        # `synthetic-b` tracks zero ids yet must still appear, stale.
        assert expect["provider_staleness"] == {
            "synthetic-a": False, "synthetic-b": True}
        assert expect["counts"] == {
            "exact_matches": 1, "endpoints": 1,
            "distinct_raw_ids": 1, "marker_group_labels": 1}
        endpoint = _metadata_endpoint(case)
        assert endpoint["provider"] == "synthetic-a"
        assert endpoint["stale"] is False
        # The endpoint survives with metadata attached to THIS exact pair.
        assert endpoint["metadata"] == _metadata_entry(snap, raw_id)


def test_metadata_unknown_context_is_null_not_known_zero(cases_document):
    case, snap = _metadata_case(cases_document,
                                METADATA_CASE_IDS[METADATA_UNKNOWN_CONTEXT])
    meta = _metadata_endpoint(case)["metadata"]
    assert meta["context_length"] is None
    assert meta["context_length"] != 0
    assert _metadata_entry(snap, METADATA_UNKNOWN_CONTEXT)[
        "context_length"] is None
    assert REASONING_WORD in meta["description"]
    # Whole-object pricing is unknown here, never an invented zero.
    assert meta["pricing"] is None


def test_metadata_known_context_pins_inclusive_boundary_and_scalar_types(
        cases_document):
    case, snap = _metadata_case(cases_document,
                                METADATA_CASE_IDS[METADATA_KNOWN_CONTEXT])
    meta = _metadata_endpoint(case)["metadata"]
    assert meta["context_length"] == METADATA_INCLUSIVE_CONTEXT == 65536
    assert REASONING_WORD in meta["description"]
    pricing = meta["pricing"]
    # Provider-returned scalar TYPES are preserved: string "0" and int 0.
    assert pricing["prompt"] == "0"
    assert isinstance(pricing["prompt"], str)
    assert pricing["completion"] == 0
    assert isinstance(pricing["completion"], int) \
        and not isinstance(pricing["completion"], bool)
    assert meta == _metadata_entry(snap, METADATA_KNOWN_CONTEXT)


def test_metadata_zero_context_is_known_zero_not_unknown(cases_document):
    case, snap = _metadata_case(cases_document,
                                METADATA_CASE_IDS[METADATA_ZERO_CONTEXT])
    meta = _metadata_endpoint(case)["metadata"]
    assert meta["context_length"] == 0
    assert meta["context_length"] is not None
    assert REASONING_WORD in meta["description"]
    # Known zero context with individually-null / known pricing scalars.
    assert meta["pricing"]["prompt"] is None
    assert meta["pricing"]["completion"] == "0"
    assert isinstance(meta["pricing"]["completion"], str)
    assert meta == _metadata_entry(snap, METADATA_ZERO_CONTEXT)


def test_metadata_unknown_description_is_null(cases_document):
    case, snap = _metadata_case(
        cases_document, METADATA_CASE_IDS[METADATA_UNKNOWN_DESCRIPTION])
    meta = _metadata_endpoint(case)["metadata"]
    assert meta["description"] is None
    assert meta["context_length"] == METADATA_INCLUSIVE_CONTEXT
    assert meta["pricing"] == {"prompt": None, "completion": None}
    assert meta == _metadata_entry(snap, METADATA_UNKNOWN_DESCRIPTION)


def test_metadata_all_four_ids_survive_with_known_architecture(
        cases_document):
    _, snap = _metadata_case(cases_document,
                             METADATA_CASE_IDS[METADATA_KNOWN_CONTEXT])
    a = snap["providers"]["synthetic-a"]
    assert a["model_ids"] == list(METADATA_TRACKED_IDS)
    for raw_id in METADATA_TRACKED_IDS:
        case, _ = _metadata_case(cases_document, METADATA_CASE_IDS[raw_id])
        endpoint = _metadata_endpoint(case)
        assert endpoint["model_id"] == raw_id
        meta = endpoint["metadata"]
        assert meta["id"] == raw_id
        assert isinstance(meta["name"], str) and meta["name"]
        assert meta["architecture"] == METADATA_ARCHITECTURE
        assert meta["supported_parameters"] == METADATA_SUPPORTED_PARAMETERS
        # The tracked id reaches its endpoint; nothing is dropped.
        assert meta == a["models"][raw_id]


def test_metadata_integrity_rejects_unknown_context_zeroed(cases_document):
    case, snap = _copy_case(
        cases_document, METADATA_CASE_IDS[METADATA_UNKNOWN_CONTEXT])
    endpoint = _metadata_endpoint(case)
    assert endpoint["metadata"]["context_length"] is None
    endpoint["metadata"]["context_length"] = 0  # unknown -> invented zero
    with pytest.raises(AssertionError, match=METADATA_ATTACHMENT_MARKER):
        _assert_get_model_payload(case, snap)


def test_metadata_integrity_rejects_known_zero_as_unknown(cases_document):
    case, snap = _copy_case(
        cases_document, METADATA_CASE_IDS[METADATA_ZERO_CONTEXT])
    endpoint = _metadata_endpoint(case)
    assert endpoint["metadata"]["context_length"] == 0
    endpoint["metadata"]["context_length"] = None  # known zero -> unknown
    with pytest.raises(AssertionError, match=METADATA_ATTACHMENT_MARKER):
        _assert_get_model_payload(case, snap)


def test_metadata_integrity_rejects_dropped_endpoint(cases_document):
    case, snap = _copy_case(
        cases_document, METADATA_CASE_IDS[METADATA_UNKNOWN_CONTEXT])
    endpoint = _metadata_endpoint(case)
    assert endpoint["model_id"] == METADATA_UNKNOWN_CONTEXT
    case["expect"]["endpoints"] = []  # endpoint loss, counts/ids untouched
    with pytest.raises(AssertionError):
        _assert_get_model_payload(case, snap)


def test_metadata_integrity_rejects_altered_pricing_scalar_type(
        cases_document):
    case, snap = _copy_case(
        cases_document, METADATA_CASE_IDS[METADATA_KNOWN_CONTEXT])
    endpoint = _metadata_endpoint(case)
    assert endpoint["metadata"]["pricing"]["completion"] == 0
    # Change the numeric scalar's TYPE in BOTH the golden and the snapshot
    # copy, so attachment still matches and the pricing contract itself
    # must reject the boolean.
    endpoint["metadata"]["pricing"]["completion"] = True
    _metadata_entry(snap, METADATA_KNOWN_CONTEXT)[
        "pricing"]["completion"] = True
    with pytest.raises(AssertionError, match="pricing scalars"):
        _assert_get_model_payload(case, snap)


def test_metadata_integrity_rejects_missing_zero_id_stale_provider(
        cases_document):
    case, snap = _copy_case(
        cases_document, METADATA_CASE_IDS[METADATA_UNKNOWN_CONTEXT])
    staleness = case["expect"]["provider_staleness"]
    assert staleness["synthetic-b"] is True
    del staleness["synthetic-b"]  # drop the zero-ID stale provider
    with pytest.raises(AssertionError):
        _assert_get_model_payload(case, snap)


# ---------- unit 1c-b1: capability-unknown.json fixture integrity ----------
#
# capability-unknown.json is a valid synthetic snapshot whose provider
# `synthetic-a` tracks an all-null-capability model next to a known-empty
# model, and whose `synthetic-b` tracks zero IDs but is stale (so a correct
# payload must still carry it in the exact-membership and staleness maps).
# The two complete get_model goldens are literal, hand-authored payloads
# (never generated from an implementation or from the fixture at run time).
# This pins UNKNOWN (null) versus KNOWN EMPTY ([] / 0) with no runtime
# normalization: the normalization table below is separate fixture data.

CAPABILITY_UNKNOWN_ID = "vendor/unknown-capabilities"
CAPABILITY_KNOWN_EMPTY_ID = "vendor/known-empty-capabilities"
CAPABILITY_TRACKED_IDS = (CAPABILITY_KNOWN_EMPTY_ID, CAPABILITY_UNKNOWN_ID)
CAPABILITY_CASE_IDS = {
    CAPABILITY_UNKNOWN_ID: "get_model_capability_unknown_all_null",
    CAPABILITY_KNOWN_EMPTY_ID: "get_model_capability_known_empty_lists",
}
# Positive-control pins: the actual scenario values, restated so a helper
# that merely "passes" cannot hide a dropped or conflated value.
CAPABILITY_UNKNOWN_NAME = "Vendor Unknown Capabilities"
CAPABILITY_KNOWN_EMPTY_NAME = "Vendor Known Empty Capabilities"


@pytest.fixture(scope="module")
def capability_snapshot():
    return _load_strict(CAPABILITY_FIXTURE)


def _capability_case(cases_document, case_id):
    case = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == case_id)
    return case, _case_fixture(case, cases_document)


def _capability_entry(snap, raw_id):
    return snap["providers"]["synthetic-a"]["models"][raw_id]


def _capability_endpoint(case):
    endpoints = case["expect"]["endpoints"]
    assert len(endpoints) == 1, \
        f"{case['id']}: expected exactly one a-provider endpoint"
    return endpoints[0]


def _reseal_snapshot(snap):
    """Recompute a snapshot copy's canonical `snapshot_id` from its (mutated)
    body, so a CONSISTENT mutation keeps the hash check honest and the
    scenario pin — not the hash — is what must bite."""
    body = {k: v for k, v in snap.items() if k != "snapshot_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    snap["snapshot_id"] = hashlib.sha256(canonical).hexdigest()
    return snap


def _assert_capability_unknown_semantics(snap):
    """Scenario pin: the all-null model's capabilities are UNKNOWN — exactly
    null, never a known zero or an empty list."""
    entry = _capability_entry(snap, CAPABILITY_UNKNOWN_ID)
    assert entry["name"] == CAPABILITY_UNKNOWN_NAME
    assert entry["description"] is None
    assert entry["context_length"] is None
    assert entry["architecture"]["input_modalities"] is None
    assert entry["architecture"]["output_modalities"] is None
    assert entry["supported_parameters"] is None
    assert entry["pricing"] is None


def _assert_capability_known_empty_semantics(snap):
    """Scenario pin: the known-empty model's capabilities are KNOWN empty —
    exactly [] and 0, never null."""
    entry = _capability_entry(snap, CAPABILITY_KNOWN_EMPTY_ID)
    assert entry["name"] == CAPABILITY_KNOWN_EMPTY_NAME
    assert entry["context_length"] == 0
    assert entry["architecture"]["input_modalities"] == []
    assert entry["architecture"]["output_modalities"] == []
    assert entry["supported_parameters"] == []
    assert entry["pricing"] == {"prompt": "0", "completion": "0"}


def test_capability_fixture_matches_snapshot_contract(capability_snapshot):
    _assert_snapshot_contract(capability_snapshot)


def test_capability_fixture_shape_is_frozen(capability_snapshot):
    assert capability_snapshot["schema_version"] == 1
    assert capability_snapshot["tick_epoch"] == 1759200000
    assert capability_snapshot["cadence_s"] == 3600
    assert capability_snapshot["warnings"] == ["metadata_missing"]
    assert capability_snapshot["provider_order"] == [
        "synthetic-a", "synthetic-b"]
    a = capability_snapshot["providers"]["synthetic-a"]
    b = capability_snapshot["providers"]["synthetic-b"]
    assert a["stale"] is False and b["stale"] is True
    assert a["model_ids"] == list(CAPABILITY_TRACKED_IDS)
    assert sorted(a["models"]) == list(CAPABILITY_TRACKED_IDS)
    assert b["model_ids"] == [] and b["models"] == {}


def test_capability_fixture_snapshot_id_is_canonical(capability_snapshot):
    body = {k: v for k, v in capability_snapshot.items()
            if k != "snapshot_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    assert capability_snapshot["snapshot_id"] == \
        hashlib.sha256(canonical).hexdigest()


def test_capability_goldens_use_new_fixture_and_pass_complete_checkers(
        cases_document):
    expected_id = _load_strict(CAPABILITY_FIXTURE)["snapshot_id"]
    for raw_id in CAPABILITY_TRACKED_IDS:
        case, snap = _capability_case(cases_document,
                                      CAPABILITY_CASE_IDS[raw_id])
        # Explicit fixture override — never the cases.json default.
        assert case["fixture"] == CAPABILITY_FIXTURE_NAME
        assert case["args"]["model_id"] == raw_id
        assert snap["snapshot_id"] == expected_id
        _assert_snapshot_contract(snap)
        _assert_get_model_payload(case, snap)


def test_capability_goldens_pin_envelope_and_zero_id_stale_provider(
        cases_document):
    for raw_id in CAPABILITY_TRACKED_IDS:
        case, snap = _capability_case(cases_document,
                                      CAPABILITY_CASE_IDS[raw_id])
        expect = case["expect"]
        assert expect["exact_matches"] == {
            "synthetic-a": True, "synthetic-b": False}
        assert expect["exact_gateways"] == ["synthetic-a"]
        # `synthetic-b` tracks zero ids yet must still appear, stale.
        assert expect["provider_staleness"] == {
            "synthetic-a": False, "synthetic-b": True}
        assert expect["counts"] == {
            "exact_matches": 1, "endpoints": 1,
            "distinct_raw_ids": 1, "marker_group_labels": 1}
        endpoint = _capability_endpoint(case)
        assert endpoint["provider"] == "synthetic-a"
        assert endpoint["stale"] is False
        assert endpoint["metadata"] == _capability_entry(snap, raw_id)


def test_capability_unknown_model_is_all_null_not_empty(cases_document):
    case, snap = _capability_case(cases_document,
                                  CAPABILITY_CASE_IDS[CAPABILITY_UNKNOWN_ID])
    assert case["fixture"] == CAPABILITY_FIXTURE_NAME
    meta = _capability_endpoint(case)["metadata"]
    assert meta["context_length"] is None and meta["context_length"] != 0
    assert meta["architecture"]["input_modalities"] is None
    assert meta["architecture"]["output_modalities"] is None
    assert meta["supported_parameters"] is None
    assert isinstance(meta["name"], str) and meta["name"]
    assert meta["description"] is None
    assert meta["pricing"] is None
    assert meta == _capability_entry(snap, CAPABILITY_UNKNOWN_ID)
    _assert_capability_unknown_semantics(snap)


def test_capability_known_empty_model_is_known_empty_not_unknown(
        cases_document):
    case, snap = _capability_case(
        cases_document, CAPABILITY_CASE_IDS[CAPABILITY_KNOWN_EMPTY_ID])
    meta = _capability_endpoint(case)["metadata"]
    assert meta["context_length"] == 0
    assert meta["architecture"]["input_modalities"] == []
    assert meta["architecture"]["output_modalities"] == []
    assert meta["supported_parameters"] == []
    assert isinstance(meta["name"], str) and meta["name"]
    assert isinstance(meta["description"], str) and meta["description"]
    assert meta["pricing"] == {"prompt": "0", "completion": "0"}
    assert meta == _capability_entry(snap, CAPABILITY_KNOWN_EMPTY_ID)
    _assert_capability_known_empty_semantics(snap)


def test_capability_unknown_and_empty_are_distinguishable(cases_document):
    _, snap = _capability_case(cases_document,
                               CAPABILITY_CASE_IDS[CAPABILITY_UNKNOWN_ID])
    unknown = _capability_entry(snap, CAPABILITY_UNKNOWN_ID)
    known = _capability_entry(snap, CAPABILITY_KNOWN_EMPTY_ID)
    assert unknown["context_length"] is None
    assert known["context_length"] == 0
    assert unknown["supported_parameters"] is None
    assert known["supported_parameters"] == []
    assert unknown["architecture"]["input_modalities"] is None
    assert known["architecture"]["input_modalities"] == []
    # The shared snapshot checker accepts BOTH distinct forms.
    _assert_snapshot_contract(snap)


def test_capability_integrity_rejects_null_capability_replaced_by_empty(
        cases_document):
    case, snap = _copy_case(cases_document,
                            CAPABILITY_CASE_IDS[CAPABILITY_UNKNOWN_ID])
    # CONSISTENT fixture+golden mutation: every null capability becomes a
    # known zero/empty in BOTH the snapshot copy and the golden, and the
    # hash is re-sealed, so the generic integrity checkers still pass...
    entry = _capability_entry(snap, CAPABILITY_UNKNOWN_ID)
    meta = _capability_endpoint(case)["metadata"]
    for target in (entry, meta):
        target["context_length"] = 0
        target["architecture"]["input_modalities"] = []
        target["architecture"]["output_modalities"] = []
        target["supported_parameters"] = []
    _reseal_snapshot(snap)
    case["expect"]["snapshot_id"] = snap["snapshot_id"]
    _assert_snapshot_contract(snap)
    _assert_get_model_payload(case, snap)
    assert _capability_entry(snap, CAPABILITY_UNKNOWN_ID)[
        "context_length"] == 0
    # ...but the unknown-versus-empty scenario pin must bite.
    with pytest.raises(AssertionError):
        _assert_capability_unknown_semantics(snap)


def test_capability_integrity_rejects_known_empty_marked_unknown(
        cases_document):
    case, snap = _copy_case(cases_document,
                            CAPABILITY_CASE_IDS[CAPABILITY_KNOWN_EMPTY_ID])
    entry = _capability_entry(snap, CAPABILITY_KNOWN_EMPTY_ID)
    meta = _capability_endpoint(case)["metadata"]
    for target in (entry, meta):
        target["architecture"]["input_modalities"] = None
        target["architecture"]["output_modalities"] = None
        target["supported_parameters"] = None
    _reseal_snapshot(snap)
    case["expect"]["snapshot_id"] = snap["snapshot_id"]
    _assert_snapshot_contract(snap)
    _assert_get_model_payload(case, snap)
    with pytest.raises(AssertionError):
        _assert_capability_known_empty_semantics(snap)


def test_capability_integrity_rejects_one_sided_null_to_empty(cases_document):
    case, snap = _copy_case(cases_document,
                            CAPABILITY_CASE_IDS[CAPABILITY_UNKNOWN_ID])
    assert _capability_endpoint(case)["metadata"]["context_length"] is None
    _capability_endpoint(case)["metadata"]["context_length"] = 0  # golden only
    with pytest.raises(AssertionError, match=METADATA_ATTACHMENT_MARKER):
        _assert_get_model_payload(case, snap)


def test_capability_integrity_rejects_null_capability_as_bare_string(
        cases_document):
    _, snap = _copy_case(cases_document,
                         CAPABILITY_CASE_IDS[CAPABILITY_UNKNOWN_ID])
    entry = _capability_entry(snap, CAPABILITY_UNKNOWN_ID)
    entry["supported_parameters"] = "temperature"  # not a list, not null
    _reseal_snapshot(snap)  # isolate the array rule from the hash check
    with pytest.raises(AssertionError, match="list or null"):
        _assert_snapshot_contract(snap)


def test_string_list_or_null_checker_accepts_null_and_empty_rejects_bad():
    _assert_string_list_or_null(None)
    _assert_string_list_or_null([])
    _assert_string_list_or_null(["text"])
    for bad in ("text", 5, {"input": "text"}, ["text", ""], ["text", 5],
                [None], [[]]):
        with pytest.raises(AssertionError,
                           match="list or null|nonempty strings"):
            _assert_string_list_or_null(bad)


def test_capability_integrity_rejects_dropped_zero_id_stale_provider(
        cases_document):
    case, snap = _copy_case(cases_document,
                            CAPABILITY_CASE_IDS[CAPABILITY_UNKNOWN_ID])
    staleness = case["expect"]["provider_staleness"]
    assert staleness["synthetic-b"] is True
    del staleness["synthetic-b"]  # drop the zero-ID stale provider
    with pytest.raises(AssertionError):
        _assert_get_model_payload(case, snap)


# ---------- unit 1c-b1: normalization data table (FIXTURE CHECKER) ----------
#
# metadata-normalization.json is an upstream capability-fragment table: each
# row names a raw upstream fragment plus the literal expected public
# capability projection, the literal expected normalized capability data,
# the `unknown_fields` list and the deduplicated warning codes. It is
# explicitly NOT a snapshot and NOT a public query response.
#
# The helpers below are a TEST-LOCAL restatement of the contract's
# projection/normalization rules, used ONLY to check that the fixture's
# hand-authored literal data is a correct application of those rules. They
# are a fixture checker, NOT production normalization; no implementation
# module is imported, and the fixture's literal data — not this code — is
# the authoritative expected value.

_MISSING = object()
ROW_FIELDS = frozenset(
    {"id", "note", "raw", "projected", "normalized", "warnings"})
PROJECTED_FIELDS = frozenset(
    {"context_length", "architecture", "supported_parameters"})
NORMALIZED_FIELDS = frozenset(CAPABILITY_FIELDS) | {"unknown_fields"}


def _ascii_lower(text):
    """ASCII-only lowercasing: A-Z fold to a-z; every other code point
    (including accented or uppercase non-ASCII letters) stays literal."""
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch
                   for ch in text)


def _checker_dedup_warnings(codes):
    present = {code for code in codes if code}
    return [code for code in WARNING_ORDER if code in present]


def _checker_project_context(value):
    if value is _MISSING or value is None:
        return None, "metadata_missing"
    if isinstance(value, bool):
        return None, "metadata_invalid"
    if isinstance(value, int) and 0 <= value <= MAX_SAFE_INT:
        return value, None
    return None, "metadata_invalid"


def _checker_project_array(value):
    if value is _MISSING or value is None:
        return None, "metadata_missing"
    if not isinstance(value, list):
        return None, "metadata_invalid"
    if any(not isinstance(item, str) or not item for item in value):
        return None, "metadata_invalid"
    return list(value), None


def _checker_project(raw):
    ctx, ctx_warn = _checker_project_context(
        raw.get("context_length", _MISSING))
    arch = raw.get("architecture", _MISSING)
    if isinstance(arch, dict):
        in_, in_warn = _checker_project_array(
            arch.get("input_modalities", _MISSING))
        out_, out_warn = _checker_project_array(
            arch.get("output_modalities", _MISSING))
    else:
        in_ = out_ = None
        parent_warn = ("metadata_missing" if arch is _MISSING
                       else "metadata_invalid")
        in_warn = out_warn = parent_warn
    params, params_warn = _checker_project_array(
        raw.get("supported_parameters", _MISSING))
    projected = {
        "context_length": ctx,
        "architecture": {"input_modalities": in_, "output_modalities": out_},
        "supported_parameters": params,
    }
    warnings = _checker_dedup_warnings(
        [ctx_warn, in_warn, out_warn, params_warn])
    return projected, warnings


def _checker_normalize_array(values):
    if values is None:
        return None
    out, seen = [], set()
    for item in values:
        folded = _ascii_lower(item)
        if folded not in seen:
            seen.add(folded)
            out.append(folded)
    return out


def _checker_normalize(projected):
    normalized = {
        "context_length": projected["context_length"],
        "input_modalities": _checker_normalize_array(
            projected["architecture"]["input_modalities"]),
        "output_modalities": _checker_normalize_array(
            projected["architecture"]["output_modalities"]),
        "supported_parameters": _checker_normalize_array(
            projected["supported_parameters"]),
    }
    normalized["unknown_fields"] = [
        field for field in CAPABILITY_FIELDS if normalized[field] is None]
    return normalized


def _assert_normalization_row(row):
    assert set(row) == ROW_FIELDS, f"{row['id']}: row field set"
    assert set(row["projected"]) == PROJECTED_FIELDS, \
        f"{row['id']}: projected field set"
    assert set(row["projected"]["architecture"]) == ARCHITECTURE_FIELDS, \
        f"{row['id']}: projected architecture field set"
    assert set(row["normalized"]) == NORMALIZED_FIELDS, \
        f"{row['id']}: normalized field set"
    projected, warnings = _checker_project(row["raw"])
    assert row["projected"] == projected, f"{row['id']}: projection mismatch"
    assert row["warnings"] == warnings, f"{row['id']}: warning mismatch"
    normalized = _checker_normalize(row["projected"])
    assert row["normalized"] == normalized, \
        f"{row['id']}: normalization mismatch"
    unknown = row["normalized"]["unknown_fields"]
    assert unknown == [f for f in CAPABILITY_FIELDS if f in set(unknown)], \
        f"{row['id']}: unknown_fields order"
    assert unknown == [f for f in CAPABILITY_FIELDS
                       if row["normalized"][f] is None], \
        f"{row['id']}: unknown_fields content"


def _normalization_rows():
    doc = _load_strict(NORMALIZATION_FIXTURE)
    return {row["id"]: copy.deepcopy(row) for row in doc["rows"]}


def test_normalization_table_is_fragment_not_snapshot():
    doc = _load_strict(NORMALIZATION_FIXTURE)
    assert doc["kind"] == "upstream-capability-fragment-table"
    assert doc["not_a_snapshot"] is True
    assert doc["capability_fields"] == list(CAPABILITY_FIELDS)
    assert doc["warning_vocabulary"] == list(WARNING_ORDER)
    # It is NOT a snapshot: no snapshot top-level field may appear.
    assert not (set(doc) & TOP_LEVEL_FIELDS)
    assert isinstance(doc["rows"], list) and doc["rows"]


def test_normalization_rows_match_the_documented_rules():
    doc = _load_strict(NORMALIZATION_FIXTURE)
    ids = [row["id"] for row in doc["rows"]]
    assert len(ids) == len(set(ids)), "duplicate normalization row id"
    for row in doc["rows"]:
        _assert_normalization_row(row)


def test_normalization_table_covers_each_rule():
    ids = set(_normalization_rows())
    for required in (
            "context_missing", "context_explicit_null",
            "context_numeric_string", "context_boolean", "context_negative",
            "context_unsafe", "context_safe_max_known", "context_zero_known",
            "context_inclusive_boundary_known", "arrays_missing",
            "arrays_explicit_null", "array_string_not_list",
            "array_mixed_types", "array_contains_empty_string",
            "array_invalid_architecture_parent", "array_known_empty",
            "array_valid_retained_ordering_duplicates",
            "array_mixed_ascii_case_duplicates",
            "array_non_ascii_case_pair_not_folded",
            "array_whitespace_not_trimmed",
            "warnings_missing_then_invalid_dedup",
            "warnings_invalid_dedup_multiple"):
        assert required in ids, \
            f"normalization table missing rule row {required}"


def test_normalization_positive_controls():
    rows = _normalization_rows()
    # Valid raw casing/order/duplicates survive in the PUBLIC fragment.
    r = rows["array_valid_retained_ordering_duplicates"]
    assert r["projected"]["supported_parameters"] == [
        "temperature", "temperature", "max_tokens"]
    assert r["normalized"]["supported_parameters"] == [
        "temperature", "max_tokens"]
    # Mixed ASCII case folds and first-occurrence dedups.
    r = rows["array_mixed_ascii_case_duplicates"]
    assert r["projected"]["supported_parameters"] == ["TEXT", "text", "Text"]
    assert r["normalized"]["supported_parameters"] == ["text"]
    assert r["projected"]["supported_parameters"] != \
        r["normalized"]["supported_parameters"]
    # A non-ASCII case pair is NOT Unicode-folded; it stays two entries.
    r = rows["array_non_ascii_case_pair_not_folded"]
    assert r["projected"]["architecture"]["input_modalities"] == ["É", "é"]
    assert r["normalized"]["input_modalities"] == ["É", "é"]
    assert len(r["normalized"]["input_modalities"]) == 2
    # Whitespace is not trimmed; only ASCII case is folded.
    r = rows["array_whitespace_not_trimmed"]
    assert r["projected"]["supported_parameters"] == [" Text "]
    assert r["normalized"]["supported_parameters"] == [" text "]
    # Unknown versus known-empty flags are correct.
    r = rows["array_known_empty"]
    assert r["normalized"]["input_modalities"] == []
    assert r["normalized"]["output_modalities"] == []
    assert r["normalized"]["supported_parameters"] == []
    assert r["normalized"]["unknown_fields"] == []
    assert r["warnings"] == []
    r = rows["arrays_missing"]
    assert r["normalized"]["input_modalities"] is None
    assert r["normalized"]["unknown_fields"] == [
        "input_modalities", "output_modalities", "supported_parameters"]
    assert r["warnings"] == ["metadata_missing"]
    # Warnings dedup in the frozen vocabulary order.
    assert rows["warnings_missing_then_invalid_dedup"]["warnings"] == \
        ["metadata_missing", "metadata_invalid"]
    assert rows["warnings_invalid_dedup_multiple"]["warnings"] == \
        ["metadata_invalid"]
    # The safe bound is inclusive; one above it is unknown.
    assert rows["context_safe_max_known"]["normalized"]["context_length"] == \
        MAX_SAFE_INT
    assert rows["context_unsafe"]["normalized"]["context_length"] is None
    assert rows["context_unsafe"]["warnings"] == ["metadata_invalid"]
    # Known zero and the inclusive context boundary are KNOWN.
    assert rows["context_zero_known"]["normalized"]["context_length"] == 0
    assert rows["context_inclusive_boundary_known"][
        "normalized"]["context_length"] == 65536


def test_normalization_integrity_rejects_missing_unknown_flag():
    row = _normalization_rows()["arrays_missing"]
    assert row["normalized"]["unknown_fields"]
    row["normalized"]["unknown_fields"] = \
        row["normalized"]["unknown_fields"][:-1]
    with pytest.raises(AssertionError, match="normalization mismatch"):
        _assert_normalization_row(row)


def test_normalization_integrity_rejects_unicode_folded_token():
    row = _normalization_rows()["array_non_ascii_case_pair_not_folded"]
    assert row["normalized"]["input_modalities"] == ["É", "é"]
    # Accidental Unicode folding would collapse the pair to one entry.
    row["normalized"]["input_modalities"] = ["é"]
    with pytest.raises(AssertionError, match="normalization mismatch"):
        _assert_normalization_row(row)


def test_normalization_integrity_rejects_partially_salvaged_malformed_array():
    row = _normalization_rows()["array_mixed_types"]
    assert row["projected"]["architecture"]["input_modalities"] is None
    # Salvage the one valid token out of the malformed array.
    row["projected"]["architecture"]["input_modalities"] = ["text"]
    with pytest.raises(AssertionError, match="projection mismatch"):
        _assert_normalization_row(row)


def test_normalization_integrity_rejects_known_empty_marked_unknown():
    row = _normalization_rows()["array_known_empty"]
    assert row["normalized"]["supported_parameters"] == []
    row["normalized"]["supported_parameters"] = None
    with pytest.raises(AssertionError, match="normalization mismatch"):
        _assert_normalization_row(row)


def test_normalization_integrity_rejects_null_replaced_by_empty():
    row = _normalization_rows()["arrays_missing"]
    assert row["projected"]["supported_parameters"] is None
    row["projected"]["supported_parameters"] = []
    with pytest.raises(AssertionError, match="projection mismatch"):
        _assert_normalization_row(row)


def test_normalization_integrity_rejects_wrong_warning_code():
    row = _normalization_rows()["array_known_empty"]
    assert row["warnings"] == []
    row["warnings"] = ["metadata_missing"]
    with pytest.raises(AssertionError, match="warning mismatch"):
        _assert_normalization_row(row)


# ---------- unit 1c-b2: search success contract (FIXTURE CHECKER + goldens) ----------
#
# `search_models` is the metadata-search half of the frozen tool surface. The
# successful PURE payload, its filter echo, its provider-staleness scope, its
# endpoint-row projection and its PAGE-scoped counts are frozen here as an
# executable specification, independently of any runtime query code (which
# ships in a later unit; nothing here imports or exercises an implementation).
# Every committed expected payload is LITERAL and hand-authored; the helpers
# below are a clearly-labelled TEST-LOCAL restatement of the membership,
# filter, order and cardinality rules over snapshot data (a fixture checker,
# never production search and never an authoring oracle). No source/cache
# field appears in the pure payload, and a search miss is still a success.

SEARCH_FIXTURE_NAME = "search-text.json"
SEARCH_FIXTURE = FIXTURE_DIR / SEARCH_FIXTURE_NAME

# Exact PURE search success payload fields (source/cache data stays OUTSIDE).
SEARCH_RESPONSE_FIELDS = frozenset({
    "ok", "query", "filters", "limit", "offset", "snapshot_id",
    "tick_epoch", "provider_staleness", "endpoints", "total_matches",
    "returned_count", "next_offset", "counts", "caveats",
})
# Exact echoed-filter fields: every supplied raw filter, or null.
SEARCH_FILTER_FIELDS = frozenset({
    "provider", "min_context_length", "input_modalities",
    "supported_parameters",
})
# Exact endpoint-row fields: identity+wiring, normalized capability columns,
# ordered `unknown_fields`, and the complete projected pair `metadata`.
SEARCH_ENDPOINT_FIELDS = frozenset({
    "provider", "model_id", "chat_completions_url", "api_type", "stale",
    "name", "description", "context_length", "input_modalities",
    "output_modalities", "supported_parameters", "unknown_fields",
    "metadata",
})
# Exact PAGE-scoped count fields (never the global registry or the
# unpaginated match set).
SEARCH_COUNT_FIELDS = frozenset({
    "gateways", "endpoints", "distinct_raw_ids", "marker_group_labels",
})
SEARCH_DEFAULT_LIMIT = 50
SEARCH_DEFAULT_OFFSET = 0
SEARCH_MAX_LIMIT = 200

# Every hand-authored search scenario that must exist, so an empty search-case
# set can never make the suite pass vacuously. Never asserts a TOTAL case
# count (future tools may append cases).
SEARCH_REQUIRED_CASE_IDS = (
    "search_metadata_reasoning_text_only",
    "search_metadata_reasoning_min_context_inclusive",
    "search_metadata_reasoning_min_context_zero",
    "search_capability_defaults_both_models",
    "search_capability_empty_filters_no_constraint",
    "search_capability_positive_modality_yields_none",
    "search_capability_positive_parameter_yields_none",
    "search_capability_min_context_zero_known_only",
    "search_shared_min_context_a_only",
    "search_shared_modalities_and_b_only",
    "search_shared_context_and_image_neither",
    "search_shared_params_and_a_only",
    "search_shared_context_and_top_p_neither",
    "search_provider_b_shared_stale_scope",
    "search_provider_c_empty_stale_scope",
    "search_pagination_limit2_offset0",
    "search_pagination_limit2_offset2_crosses_provider",
    "search_pagination_limit2_offset4_exhausted",
    "search_pagination_limit2_offset99_beyond_end",
    "search_provider_a_limit200_empty_query",
    "search_shared_query_limit2_offset0",
    "search_text_case_extended_filters_normalize",
    "search_text_unicode_upper_echo_first",
    "search_text_unicode_lower_echo_second",
    "search_text_literal_brackets_not_regex",
    "search_text_metadata_array_not_searched",
    "search_text_raw_id_unknown_flags",
    "search_text_padded_query_not_trimmed",
)

# Full ordered registry of lookup.json (no filters): provider display order,
# then natural raw-ID order. Restated here so the pagination pins are
# non-vacuous and independent of the goldens.
LOOKUP_ORDERED_PAIRS = (
    ("synthetic-a", "vendor/model-renamed"),
    ("synthetic-a", "vendor/model:free"),
    ("synthetic-a", "vendor/shared"),
    ("synthetic-b", "vendor/model-free"),
    ("synthetic-b", "vendor/shared"),
)


@pytest.fixture(scope="module")
def search_snapshot():
    return _load_strict(SEARCH_FIXTURE)


def _iter_search_cases(cases_doc):
    return [c for c in cases_doc["cases"] if c["tool"] == "search_models"]


def _search_case(cases_document, case_id):
    case = next(c for c in _iter_search_cases(cases_document)
                if c["id"] == case_id)
    return case, _case_fixture(case, cases_document)


def _copy_search_case(cases_document, case_id):
    """Deep-copied search golden + its snapshot, for in-memory mutation
    positive controls. No committed fixture is ever corrupted."""
    case = next(c for c in _iter_search_cases(cases_document)
                if c["id"] == case_id)
    return copy.deepcopy(case), _case_fixture(case, cases_document)


# -- test-local search rule restatement (FIXTURE CHECKER) --

def _search_normalize_array(values):
    """Reuse the unit 1c-b1 checker: null stays null, else ASCII-lowercase
    and first-occurrence dedup. NOT production normalization."""
    return _checker_normalize_array(values)


def _search_text_match(needle, raw_id, model):
    """Literal SUBSTRING over raw ID, name and description only; ASCII-only
    folding; no regex, no metadata-JSON search. An empty/None query applies
    no text constraint."""
    if needle is None or needle == "":
        return True
    folded = _ascii_lower(needle)
    for field in (raw_id, model["name"], model["description"]):
        if field is not None and folded in _ascii_lower(field):
            return True
    return False


def _search_context_ok(value, minimum):
    """Minimum-context predicate: a non-negative filter qualifies only KNOWN
    safe non-negative integer contexts and compares inclusively. null is
    UNKNOWN and fails a positive filter (never coerced to 0)."""
    if minimum is None:
        return True
    return (isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= MAX_SAFE_INT and value >= minimum)


def _search_array_ok(values, requested):
    """Modalities/parameters predicate: every requested entry must be present
    in the normalized upstream array (AND). A null/missing array is UNKNOWN
    and fails a nonempty positive filter; an empty request applies no
    constraint; a known empty array satisfies no nonempty request."""
    if requested is None or len(requested) == 0:
        return True
    if values is None:
        return False
    normalized = set(_search_normalize_array(values) or [])
    return all(_ascii_lower(entry) in normalized for entry in requested)


def _search_reference_pairs(snap, args):
    """TEST-LOCAL restatement of the search predicate and the frozen
    provider/natural ordering over the snapshot. Returns the ordered
    (provider, raw_id) pairs satisfying ALL active predicates, before
    pagination. Never authors an expected fixture."""
    query = args.get("query")
    provider = args.get("provider")
    minimum = args.get("min_context_length")
    req_in = args.get("input_modalities")
    req_params = args.get("supported_parameters")
    pairs = []
    for key in snap["provider_order"]:
        if provider is not None and key != provider:
            continue
        entry = snap["providers"][key]
        for raw_id in entry["model_ids"]:
            model = entry["models"][raw_id]
            if not _search_text_match(query, raw_id, model):
                continue
            if not _search_context_ok(model["context_length"], minimum):
                continue
            if not _search_array_ok(
                    model["architecture"]["input_modalities"], req_in):
                continue
            if not _search_array_ok(model["supported_parameters"], req_params):
                continue
            pairs.append((key, raw_id))
    pairs.sort(key=lambda pair: (
        snap["provider_order"].index(pair[0]),
        _natural_key_contract(pair[1])))
    return pairs


def _assert_search_endpoint_row(endpoint, snap, cid):
    """Every integrity assertion of one frozen search endpoint row."""
    assert set(endpoint) == SEARCH_ENDPOINT_FIELDS, (
        f"{cid}: unexpected endpoint fields "
        f"{sorted(set(endpoint) ^ SEARCH_ENDPOINT_FIELDS)}")
    provider = endpoint["provider"]
    raw_id = endpoint["model_id"]
    assert provider in snap["providers"], f"{cid}: unknown provider {provider!r}"
    entry = snap["providers"][provider]
    assert isinstance(raw_id, str) and raw_id in entry["model_ids"], \
        f"{cid}: endpoint pair ({provider!r}, {raw_id!r}) is not tracked"
    model = entry["models"][raw_id]
    assert endpoint["chat_completions_url"] == entry["chat_completions_url"]
    assert endpoint["api_type"] == entry["api_type"]
    assert endpoint["stale"] is entry["stale"]
    # name/description mirror PUBLIC metadata (description may be null).
    assert endpoint["name"] == model["name"]
    assert endpoint["description"] == model["description"]
    # Normalized capability columns: context echoed unchanged; arrays are the
    # ASCII-lowercased, first-occurrence-deduplicated unit 1c-b1 values.
    norm_ctx = model["context_length"]
    norm_in = _search_normalize_array(
        model["architecture"]["input_modalities"])
    norm_out = _search_normalize_array(
        model["architecture"]["output_modalities"])
    norm_params = _search_normalize_array(model["supported_parameters"])
    assert endpoint["context_length"] == norm_ctx, \
        f"{cid}: context_length must be the normalized (echoed) value"
    assert endpoint["input_modalities"] == norm_in, \
        f"{cid}: input_modalities must be the normalized value"
    assert endpoint["output_modalities"] == norm_out, \
        f"{cid}: output_modalities must be the normalized value"
    assert endpoint["supported_parameters"] == norm_params, \
        f"{cid}: supported_parameters must be the normalized value"
    normalized = {
        "context_length": norm_ctx,
        "input_modalities": norm_in,
        "output_modalities": norm_out,
        "supported_parameters": norm_params,
    }
    assert endpoint["unknown_fields"] == [
        field for field in CAPABILITY_FIELDS if normalized[field] is None], \
        f"{cid}: unknown_fields must be the ordered normalized-null flag list"
    # metadata is the COMPLETE projected public model of THIS exact pair,
    # retaining valid original array casing/order/duplicates.
    assert endpoint["metadata"] == model, (
        f"{cid}: metadata for pair ({provider!r}, {raw_id!r}) is not the "
        "projected public model of that exact pair")


def _assert_search_payload(case, snap):
    """Every integrity assertion of the frozen successful PURE search_models
    payload. Validates the hand-authored golden against its own snapshot with
    stdlib data operations only — no query implementation is imported and the
    expected result is never generated by a future implementation."""
    expect = case["expect"]
    args = case["args"]
    cid = case["id"]

    # Exact top-level field set: a stray, source or legacy key is corruption.
    assert set(expect) == SEARCH_RESPONSE_FIELDS, (
        f"{cid}: unexpected response fields "
        f"{sorted(set(expect) ^ SEARCH_RESPONSE_FIELDS)}")

    # Success is explicit — never an error envelope, even for no matches.
    assert expect["ok"] is True
    assert expect["query"] == args.get("query"), \
        f"{cid}: query echo must be the raw argument, never trimmed"

    # Filter echo: each supplied raw filter, or null when omitted; [] stays [].
    filters = expect["filters"]
    assert set(filters) == SEARCH_FILTER_FIELDS, \
        f"{cid}: unexpected filter fields {sorted(set(filters))}"
    assert filters["provider"] == args.get("provider")
    assert filters["min_context_length"] == args.get("min_context_length")
    assert filters["input_modalities"] == args.get("input_modalities")
    assert filters["supported_parameters"] == args.get("supported_parameters")

    # Effective pagination echo.
    limit = args.get("limit", SEARCH_DEFAULT_LIMIT)
    offset = args.get("offset", SEARCH_DEFAULT_OFFSET)
    assert expect["limit"] == limit
    assert expect["offset"] == offset
    assert isinstance(limit, int) and not isinstance(limit, bool) \
        and 1 <= limit <= SEARCH_MAX_LIMIT, f"{cid}: limit out of range"
    assert isinstance(offset, int) and not isinstance(offset, bool) \
        and offset >= 0, f"{cid}: offset out of range"

    # Observation provenance mirrors the consumed snapshot.
    assert expect["snapshot_id"] == snap["snapshot_id"]
    assert expect["tick_epoch"] == snap["tick_epoch"]

    # Provider staleness covers the SELECTED provider scope: all snapshot
    # providers when no provider filter, else exactly the selected provider;
    # it is retained even on an empty/beyond-end page.
    selected = (snap["provider_order"] if filters["provider"] is None
                else [filters["provider"]])
    assert set(expect["provider_staleness"]) == set(selected), \
        f"{cid}: provider_staleness scope mismatch"
    for key in selected:
        assert expect["provider_staleness"][key] is \
            snap["providers"][key]["stale"], \
            f"{cid}: provider staleness mismatch for {key}"

    # Membership, order BEFORE slice, totals, next_offset.
    all_pairs = _search_reference_pairs(snap, args)
    page = all_pairs[offset:offset + limit]
    golden_pairs = [(e["provider"], e["model_id"]) for e in expect["endpoints"]]
    assert golden_pairs == page, (
        f"{cid}: search endpoint membership/order violates the reference "
        "predicate over the snapshot")
    assert expect["total_matches"] == len(all_pairs), \
        f"{cid}: total_matches must be the unpaginated match cardinality"
    assert _counts_are_strict_int(expect["total_matches"]), \
        f"{cid}: total_matches must be a strict non-negative int"
    assert expect["returned_count"] == len(expect["endpoints"]), \
        f"{cid}: returned_count must equal len(endpoints)"
    assert _counts_are_strict_int(expect["returned_count"]), \
        f"{cid}: returned_count must be a strict non-negative int"
    next_off = expect["next_offset"]
    if offset + len(expect["endpoints"]) < len(all_pairs):
        assert next_off == offset + len(expect["endpoints"]), \
            f"{cid}: next_offset must be offset + returned_count"
    else:
        assert next_off is None, \
            f"{cid}: next_offset must be null when exhausted/beyond-end"
    if next_off is not None:
        assert _counts_are_strict_int(next_off), \
            f"{cid}: next_offset must be null or a strict non-negative int"

    # Endpoint rows: exact fields, pair metadata, normalized columns, flags.
    assert isinstance(expect["endpoints"], list)
    for endpoint in expect["endpoints"]:
        _assert_search_endpoint_row(endpoint, snap, cid)

    # Page-scoped counts: describe the RETURNED PAGE, never the registry or
    # the unpaginated match set; an empty page is all zeros.
    counts = expect["counts"]
    assert set(counts) == SEARCH_COUNT_FIELDS, (
        f"{cid}: unexpected count fields "
        f"{sorted(set(counts) ^ SEARCH_COUNT_FIELDS)}")
    for name, value in counts.items():
        assert _counts_are_strict_int(value), \
            f"{cid}: counts.{name} must be a strict non-negative int"
    assert counts["gateways"] == len({p for p, _ in golden_pairs}), \
        f"{cid}: counts.gateways must be distinct page providers"
    assert counts["endpoints"] == len(expect["endpoints"])
    assert counts["distinct_raw_ids"] == len({i for _, i in golden_pairs})
    assert counts["marker_group_labels"] == len(
        {_strip_marker_contract(i) for _, i in golden_pairs}), \
        f"{cid}: counts.marker_group_labels must be distinct page labels"

    # The absence caveat is preserved on every search payload.
    assert isinstance(expect["caveats"], list) and expect["caveats"]
    assert ABSENCE_CAVEAT in expect["caveats"]


def _search_corrupt_row(snap, provider, raw_id, *, context_length=_MISSING,
                        input_modalities=_MISSING, output_modalities=_MISSING,
                        supported_parameters=_MISSING, metadata=_MISSING,
                        unknown_fields=_MISSING):
    """CORRUPTION-PROBE ONLY: build one plausible row from snapshot data so a
    mutation can append/attach a row the reference predicate does not select.
    Never used to author a committed fixture."""
    entry = snap["providers"][provider]
    model = entry["models"][raw_id]
    ctx = model["context_length"]
    norm_in = _search_normalize_array(
        model["architecture"]["input_modalities"])
    norm_out = _search_normalize_array(
        model["architecture"]["output_modalities"])
    norm_params = _search_normalize_array(model["supported_parameters"])
    if context_length is not _MISSING:
        ctx = context_length
    if input_modalities is not _MISSING:
        norm_in = input_modalities
    if output_modalities is not _MISSING:
        norm_out = output_modalities
    if supported_parameters is not _MISSING:
        norm_params = supported_parameters
    normalized = {
        "context_length": ctx,
        "input_modalities": norm_in,
        "output_modalities": norm_out,
        "supported_parameters": norm_params,
    }
    if unknown_fields is _MISSING:
        unknown = [f for f in CAPABILITY_FIELDS if normalized[f] is None]
    else:
        unknown = unknown_fields
    return {
        "provider": provider,
        "model_id": raw_id,
        "chat_completions_url": entry["chat_completions_url"],
        "api_type": entry["api_type"],
        "stale": entry["stale"],
        "name": model["name"],
        "description": model["description"],
        "context_length": ctx,
        "input_modalities": norm_in,
        "output_modalities": norm_out,
        "supported_parameters": norm_params,
        "unknown_fields": unknown,
        "metadata": model if metadata is _MISSING else metadata,
    }


# -- fixture integrity --

def test_search_fixture_matches_snapshot_contract(search_snapshot):
    _assert_snapshot_contract(search_snapshot)


def test_search_fixture_shape_is_frozen(search_snapshot):
    assert search_snapshot["schema_version"] == 1
    assert search_snapshot["tick_epoch"] == 1759200000
    assert search_snapshot["cadence_s"] == 3600
    assert search_snapshot["warnings"] == ["metadata_missing"]
    assert search_snapshot["provider_order"] == [
        "synthetic-a", "synthetic-b"]
    a = search_snapshot["providers"]["synthetic-a"]
    b = search_snapshot["providers"]["synthetic-b"]
    assert a["stale"] is False and b["stale"] is True
    assert a["model_ids"] == ["vendor/literal[ab].*", "vendor/plain"]
    assert sorted(a["models"]) == a["model_ids"]
    assert b["model_ids"] == [] and b["models"] == {}
    # The documented raw text/casing of the two models is frozen literally.
    literal = a["models"]["vendor/literal[ab].*"]
    assert literal["name"] == "ÉCHO TEXT"
    assert literal["description"] == \
        "Reasoning literal [ab].* token; synthetic prose."
    assert literal["architecture"]["input_modalities"] == \
        ["TEXT", "Image", "text"]
    assert literal["supported_parameters"] == \
        ["Temperature", "MAX_TOKENS", "temperature"]
    plain = a["models"]["vendor/plain"]
    assert plain["name"] == "écho plain"
    assert plain["context_length"] is None
    assert plain["pricing"] is None


def test_search_fixture_snapshot_id_is_canonical(search_snapshot):
    body = {k: v for k, v in search_snapshot.items() if k != "snapshot_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    assert search_snapshot["snapshot_id"] == \
        hashlib.sha256(canonical).hexdigest()


def test_search_fixture_adds_no_package_code(search_snapshot):
    # A new synthetic provider is addable by DATA alone: the fixture declares
    # both providers with no python change, and no implementation module is
    # imported by this file.
    import sys
    assert not any(name.startswith("watchdog_catalog")
                   for name in sys.modules)
    assert set(search_snapshot["providers"]) == set(
        search_snapshot["provider_order"])


# -- envelope field-set freeze --

def test_search_response_envelope_field_sets_are_frozen(cases_document):
    assert set(SEARCH_RESPONSE_FIELDS) == {
        "ok", "query", "filters", "limit", "offset", "snapshot_id",
        "tick_epoch", "provider_staleness", "endpoints", "total_matches",
        "returned_count", "next_offset", "counts", "caveats"}
    assert set(SEARCH_FILTER_FIELDS) == {
        "provider", "min_context_length", "input_modalities",
        "supported_parameters"}
    assert set(SEARCH_ENDPOINT_FIELDS) == {
        "provider", "model_id", "chat_completions_url", "api_type", "stale",
        "name", "description", "context_length", "input_modalities",
        "output_modalities", "supported_parameters", "unknown_fields",
        "metadata"}
    assert set(SEARCH_COUNT_FIELDS) == {
        "gateways", "endpoints", "distinct_raw_ids", "marker_group_labels"}
    for case in _iter_search_cases(cases_document):
        _assert_search_payload(case, _case_fixture(case, cases_document))


def test_search_goldens_present_unique_and_use_explicit_fixtures(
        cases_document):
    cases = _iter_search_cases(cases_document)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate search case id"
    missing = [cid for cid in SEARCH_REQUIRED_CASE_IDS if cid not in ids]
    assert not missing, f"missing hand-authored search scenarios: {missing}"
    for case in cases:
        # Explicit fixture override — never the cases.json default.
        assert "fixture" in case, f"{case['id']}: needs an explicit fixture"
        assert "args" in case and isinstance(case["args"], dict)
        assert "expect" in case and isinstance(case["expect"], dict)


def test_search_goldens_pass_complete_checker(cases_document):
    for case in _iter_search_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        _assert_snapshot_contract(snap)
        _assert_search_payload(case, snap)


# -- scenario pins (positive controls: the actual values, not self-consistency) --

def test_search_metadata_reasoning_pins_context_filters(cases_document):
    # query=reasoning alone: known + unknown + zero context, NOT the null-desc.
    case, snap = _search_case(cases_document,
                              "search_metadata_reasoning_text_only")
    assert case["fixture"] == "metadata-edge-cases.json"
    assert case["args"] == {"query": "reasoning"}
    expect = case["expect"]
    assert [(e["provider"], e["model_id"]) for e in expect["endpoints"]] == [
        ("synthetic-a", "vendor/known-context"),
        ("synthetic-a", "vendor/unknown-context"),
        ("synthetic-a", "vendor/zero-context")]
    assert {e["model_id"] for e in expect["endpoints"]} == {
        "vendor/known-context", "vendor/unknown-context", "vendor/zero-context"}
    assert expect["total_matches"] == 3
    assert expect["next_offset"] is None
    assert expect["provider_staleness"] == {
        "synthetic-a": False, "synthetic-b": True}
    # unknown-context keeps its null context and carries the unknown flag.
    unknown = expect["endpoints"][1]
    assert unknown["context_length"] is None
    assert unknown["unknown_fields"] == ["context_length"]
    # zero-context is a KNOWN zero, not unknown.
    zero = expect["endpoints"][2]
    assert zero["context_length"] == 0
    assert zero["unknown_fields"] == []

    # query + min_context_length=65536: inclusive boundary keeps only known.
    case, snap = _search_case(
        cases_document, "search_metadata_reasoning_min_context_inclusive")
    expect = case["expect"]
    assert case["args"]["min_context_length"] == 65536
    assert [e["model_id"] for e in expect["endpoints"]] == \
        ["vendor/known-context"]
    assert expect["total_matches"] == 1
    assert expect["filters"]["min_context_length"] == 65536

    # query + min_context_length=0: known + zero, EXCLUDING null (unknown).
    case, snap = _search_case(cases_document,
                              "search_metadata_reasoning_min_context_zero")
    expect = case["expect"]
    assert case["args"]["min_context_length"] == 0
    assert [e["model_id"] for e in expect["endpoints"]] == [
        "vendor/known-context", "vendor/zero-context"]
    assert "vendor/unknown-context" not in \
        {e["model_id"] for e in expect["endpoints"]}
    assert expect["total_matches"] == 2


def test_search_capability_defaults_empty_filters_and_unknown(cases_document):
    # Omitted query/filters: both models, effective defaults 50/0.
    case, snap = _search_case(cases_document,
                              "search_capability_defaults_both_models")
    assert case["fixture"] == "capability-unknown.json"
    assert case["args"] == {}
    expect = case["expect"]
    assert expect["query"] is None
    assert expect["filters"] == {
        "provider": None, "min_context_length": None,
        "input_modalities": None, "supported_parameters": None}
    assert expect["limit"] == 50 and expect["offset"] == 0
    assert [e["model_id"] for e in expect["endpoints"]] == [
        "vendor/known-empty-capabilities", "vendor/unknown-capabilities"]
    assert expect["total_matches"] == 2
    assert expect["next_offset"] is None

    # query="" with empty filter arrays: both, echoing the raw empty filters.
    case, snap = _search_case(cases_document,
                              "search_capability_empty_filters_no_constraint")
    expect = case["expect"]
    assert case["args"] == {"query": "", "input_modalities": [],
                            "supported_parameters": []}
    assert expect["query"] == ""
    assert expect["filters"]["input_modalities"] == []
    assert expect["filters"]["supported_parameters"] == []
    assert expect["total_matches"] == 2

    # A positive input_modalities filter yields none (unknown fails; [] empty).
    case, snap = _search_case(
        cases_document, "search_capability_positive_modality_yields_none")
    expect = case["expect"]
    assert case["args"] == {"input_modalities": ["text"]}
    assert expect["endpoints"] == []
    assert expect["total_matches"] == 0
    assert expect["counts"] == {
        "gateways": 0, "endpoints": 0, "distinct_raw_ids": 0,
        "marker_group_labels": 0}
    assert expect["provider_staleness"] == {
        "synthetic-a": False, "synthetic-b": True}

    # A positive supported_parameters filter yields none.
    case, snap = _search_case(
        cases_document, "search_capability_positive_parameter_yields_none")
    assert case["expect"]["endpoints"] == []
    assert case["expect"]["total_matches"] == 0

    # minimum=0 alone keeps only the KNOWN-empty/zero model.
    case, snap = _search_case(
        cases_document, "search_capability_min_context_zero_known_only")
    expect = case["expect"]
    assert case["args"] == {"min_context_length": 0}
    assert [e["model_id"] for e in expect["endpoints"]] == \
        ["vendor/known-empty-capabilities"]
    assert expect["total_matches"] == 1
    assert expect["endpoints"][0]["context_length"] == 0


def test_search_shared_raw_id_and_without_cross_provider_merge(
        cases_document):
    # shared + minimum 65536 -> a only (b context is smaller).
    case, snap = _search_case(cases_document,
                              "search_shared_min_context_a_only")
    assert case["fixture"] == "lookup.json"
    assert [(e["provider"], e["model_id"]) for e in case["expect"]["endpoints"]] \
        == [("synthetic-a", "vendor/shared")]
    # shared + ["text","IMAGE"] -> b only (AND within the array).
    case, snap = _search_case(cases_document,
                              "search_shared_modalities_and_b_only")
    assert [(e["provider"], e["model_id"]) for e in case["expect"]["endpoints"]] \
        == [("synthetic-b", "vendor/shared")]
    assert case["args"]["input_modalities"] == ["text", "IMAGE"]
    # shared + 65536 + ["image"] -> NEITHER (no cross-provider combination).
    case, snap = _search_case(cases_document,
                              "search_shared_context_and_image_neither")
    assert case["expect"]["endpoints"] == []
    assert case["expect"]["total_matches"] == 0
    # shared + ["temperature","MAX_TOKENS"] -> a only (AND across entries).
    case, snap = _search_case(cases_document,
                              "search_shared_params_and_a_only")
    assert [(e["provider"], e["model_id"]) for e in case["expect"]["endpoints"]] \
        == [("synthetic-a", "vendor/shared")]
    assert case["args"]["supported_parameters"] == \
        ["temperature", "MAX_TOKENS"]
    # shared + 65536 + ["top_p"] -> NEITHER.
    case, snap = _search_case(cases_document,
                              "search_shared_context_and_top_p_neither")
    assert case["expect"]["endpoints"] == []
    # The a-context + b-image merged hit is explicitly excluded above; prove
    # each satisfiable half exists so the failures are non-vacuous.
    _, snap = _search_case(cases_document, "search_shared_min_context_a_only")
    a_meta = snap["providers"]["synthetic-a"]["models"]["vendor/shared"]
    b_meta = snap["providers"]["synthetic-b"]["models"]["vendor/shared"]
    assert a_meta["context_length"] >= 65536
    assert "image" in b_meta["architecture"]["input_modalities"]
    assert "image" not in a_meta["architecture"]["input_modalities"]


def test_search_provider_filter_narrows_staleness_scope(cases_document):
    # provider=synthetic-b + query=shared -> b only; staleness map is b only.
    case, snap = _search_case(cases_document,
                              "search_provider_b_shared_stale_scope")
    assert case["args"] == {"provider": "synthetic-b", "query": "shared"}
    expect = case["expect"]
    assert [(e["provider"], e["model_id"]) for e in expect["endpoints"]] == [
        ("synthetic-b", "vendor/shared")]
    assert expect["provider_staleness"] == {"synthetic-b": False}
    assert expect["filters"]["provider"] == "synthetic-b"

    # provider=synthetic-c (zero IDs) -> empty result, staleness maps still c.
    case, snap = _search_case(cases_document,
                              "search_provider_c_empty_stale_scope")
    expect = case["expect"]
    assert case["args"] == {"provider": "synthetic-c"}
    assert expect["endpoints"] == []
    assert expect["total_matches"] == 0
    assert expect["provider_staleness"] == {"synthetic-c": False}
    assert expect["counts"] == {
        "gateways": 0, "endpoints": 0, "distinct_raw_ids": 0,
        "marker_group_labels": 0}


def test_search_pagination_pages_cross_provider_and_page_counts(
        cases_document):
    # Independently derive the ordered registry so the page pins are non-vacuous.
    _, snap = _search_case(cases_document, "search_pagination_limit2_offset0")
    derived = tuple(
        (key, raw_id)
        for key in snap["provider_order"]
        for raw_id in snap["providers"][key]["model_ids"])
    assert derived == LOOKUP_ORDERED_PAIRS
    assert len(derived) == 5

    page1, _ = _search_case(cases_document, "search_pagination_limit2_offset0")
    assert [(e["provider"], e["model_id"]) for e in page1["expect"]["endpoints"]] \
        == [( "synthetic-a", "vendor/model-renamed"),
            ("synthetic-a", "vendor/model:free")]
    assert page1["expect"]["total_matches"] == 5
    assert page1["expect"]["returned_count"] == 2
    assert page1["expect"]["next_offset"] == 2
    assert page1["expect"]["counts"] == {
        "gateways": 1, "endpoints": 2, "distinct_raw_ids": 2,
        "marker_group_labels": 2}

    page2, _ = _search_case(
        cases_document, "search_pagination_limit2_offset2_crosses_provider")
    assert [(e["provider"], e["model_id"]) for e in page2["expect"]["endpoints"]] \
        == [("synthetic-a", "vendor/shared"),
            ("synthetic-b", "vendor/model-free")]
    assert page2["expect"]["total_matches"] == 5
    assert page2["expect"]["next_offset"] == 4
    assert page2["expect"]["counts"] == {
        "gateways": 2, "endpoints": 2, "distinct_raw_ids": 2,
        "marker_group_labels": 2}

    page3, _ = _search_case(
        cases_document, "search_pagination_limit2_offset4_exhausted")
    assert [(e["provider"], e["model_id"]) for e in page3["expect"]["endpoints"]] \
        == [("synthetic-b", "vendor/shared")]
    assert page3["expect"]["total_matches"] == 5
    assert page3["expect"]["next_offset"] is None
    assert page3["expect"]["counts"] == {
        "gateways": 1, "endpoints": 1, "distinct_raw_ids": 1,
        "marker_group_labels": 1}

    page4, _ = _search_case(
        cases_document, "search_pagination_limit2_offset99_beyond_end")
    assert page4["expect"]["endpoints"] == []
    assert page4["expect"]["total_matches"] == 5
    assert page4["expect"]["returned_count"] == 0
    assert page4["expect"]["next_offset"] is None
    assert page4["expect"]["counts"] == {
        "gateways": 0, "endpoints": 0, "distinct_raw_ids": 0,
        "marker_group_labels": 0}

    # query="" + provider=synthetic-a + limit=200: empty query no constraint,
    # permitted upper limit, FILTERED (not global) counts.
    upper, _ = _search_case(cases_document,
                            "search_provider_a_limit200_empty_query")
    expect = upper["expect"]
    assert upper["args"] == {"query": "", "provider": "synthetic-a",
                             "limit": 200}
    assert expect["query"] == ""
    assert expect["limit"] == 200
    assert [e["model_id"] for e in expect["endpoints"]] == [
        "vendor/model-renamed", "vendor/model:free", "vendor/shared"]
    assert expect["total_matches"] == 3
    assert expect["returned_count"] == 3
    assert expect["next_offset"] is None
    assert expect["provider_staleness"] == {"synthetic-a": False}
    assert expect["counts"] == {
        "gateways": 1, "endpoints": 3, "distinct_raw_ids": 3,
        "marker_group_labels": 3}

    # query="shared" + limit=2 + offset=0: the filter must run BEFORE the slice,
    # so the page is the two shared endpoints (a then b), NOT the first two
    # unfiltered registry rows. total_matches is the unpaginated match
    # cardinality; one raw id crossing two gateways gives gateways=2 with
    # distinct_raw_ids=1 / marker_group_labels=1.
    filtered, _ = _search_case(cases_document,
                               "search_shared_query_limit2_offset0")
    assert filtered["fixture"] == "lookup.json"
    assert filtered["args"] == {"query": "shared", "limit": 2, "offset": 0}
    fexpect = filtered["expect"]
    assert [(e["provider"], e["model_id"])
            for e in fexpect["endpoints"]] == [
        ("synthetic-a", "vendor/shared"), ("synthetic-b", "vendor/shared")]
    assert fexpect["total_matches"] == 2
    assert fexpect["returned_count"] == 2
    assert fexpect["next_offset"] is None
    assert fexpect["counts"] == {
        "gateways": 2, "endpoints": 2, "distinct_raw_ids": 1,
        "marker_group_labels": 1}
    # Non-vacuous: the wrong slice-before-filter window is the first two
    # UNFILTERED registry rows, which contain NO shared endpoint, so a weaker
    # self-consistent scenario could not masquerade as this filtered page.
    assert LOOKUP_ORDERED_PAIRS[:2] == (
        ("synthetic-a", "vendor/model-renamed"),
        ("synthetic-a", "vendor/model:free"))


def test_search_text_normalization_and_literal_substring(cases_document):
    case, snap = _search_case(
        cases_document, "search_text_case_extended_filters_normalize")
    assert case["fixture"] == "search-text.json"
    expect = case["expect"]
    assert case["args"] == {
        "query": "REASONING", "min_context_length": 65536,
        "input_modalities": ["IMAGE", "text", "IMAGE"],
        "supported_parameters": ["TEMPERATURE"]}
    assert expect["query"] == "REASONING"
    assert expect["filters"]["input_modalities"] == ["IMAGE", "text", "IMAGE"]
    assert expect["filters"]["supported_parameters"] == ["TEMPERATURE"]
    assert [e["model_id"] for e in expect["endpoints"]] == \
        ["vendor/literal[ab].*"]
    row = expect["endpoints"][0]
    # Normalized columns but ORIGINAL public metadata + echoed raw filters.
    assert row["input_modalities"] == ["text", "image"]
    assert row["output_modalities"] == ["text"]
    assert row["supported_parameters"] == ["temperature", "max_tokens"]
    assert row["unknown_fields"] == []
    assert row["metadata"]["architecture"]["input_modalities"] == \
        ["TEXT", "Image", "text"]
    assert row["metadata"]["supported_parameters"] == \
        ["Temperature", "MAX_TOKENS", "temperature"]
    assert expect["provider_staleness"] == {
        "synthetic-a": False, "synthetic-b": True}

    # ÉCHO matches only the first; écho matches only the second (ASCII-only).
    upper, _ = _search_case(cases_document,
                            "search_text_unicode_upper_echo_first")
    assert upper["args"] == {"query": "ÉCHO"}
    assert [e["model_id"] for e in upper["expect"]["endpoints"]] == \
        ["vendor/literal[ab].*"]
    lower, _ = _search_case(cases_document,
                            "search_text_unicode_lower_echo_second")
    assert lower["args"] == {"query": "écho"}
    assert [e["model_id"] for e in lower["expect"]["endpoints"]] == \
        ["vendor/plain"]

    # "[ab].*" is a literal substring, never a regex.
    regex, _ = _search_case(cases_document,
                            "search_text_literal_brackets_not_regex")
    assert regex["args"] == {"query": "[ab].*"}
    assert [e["model_id"] for e in regex["expect"]["endpoints"]] == \
        ["vendor/literal[ab].*"]

    # "Image" occurs only inside the metadata array -> never searched.
    meta, _ = _search_case(cases_document,
                           "search_text_metadata_array_not_searched")
    assert meta["args"] == {"query": "Image"}
    assert meta["expect"]["endpoints"] == []
    assert meta["expect"]["total_matches"] == 0

    # vendor/plain via raw ID with all four unknown flags.
    rawid, _ = _search_case(cases_document,
                            "search_text_raw_id_unknown_flags")
    assert rawid["args"] == {"query": "vendor/plain"}
    row = rawid["expect"]["endpoints"][0]
    assert row["model_id"] == "vendor/plain"
    assert row["unknown_fields"] == [
        "context_length", "input_modalities", "output_modalities",
        "supported_parameters"]
    assert row["context_length"] is None
    assert row["input_modalities"] is None

    # " REASONING " (padded) matches nothing: no trimming.
    padded, _ = _search_case(cases_document,
                             "search_text_padded_query_not_trimmed")
    assert padded["args"] == {"query": " REASONING "}
    assert padded["expect"]["endpoints"] == []
    # Non-vacuous: the trimmed query does match.
    trimmed = " REASONING ".strip().lower()
    assert trimmed in snap["providers"]["synthetic-a"]["models"][
        "vendor/literal[ab].*"]["description"].lower()


# -- semantic corruption matrix (deep-copied scratch; committed data intact) --

def _corrupt_null_context_as_zero(case, snap):
    case["expect"]["endpoints"].append(
        _search_corrupt_row(snap, "synthetic-a", "vendor/unknown-context",
                            context_length=0, unknown_fields=[]))


def _corrupt_null_arrays_to_empty_lost_flags(case, snap):
    row = case["expect"]["endpoints"][0]
    row["input_modalities"] = []
    row["unknown_fields"] = [
        "context_length", "output_modalities", "supported_parameters"]


def _corrupt_cross_provider_merged_hit(case, snap):
    case["expect"]["endpoints"].append(
        _search_corrupt_row(snap, "synthetic-a", "vendor/shared",
                            input_modalities=["text", "image"]))


def _corrupt_wrong_provider_metadata(case, snap):
    row = case["expect"]["endpoints"][0]
    row["metadata"] = copy.deepcopy(
        snap["providers"]["synthetic-a"]["models"]["vendor/shared"])


def _corrupt_and_replaced_by_or(case, snap):
    case["expect"]["endpoints"].append(
        _search_corrupt_row(snap, "synthetic-a", "vendor/shared"))


def _corrupt_unicode_folding(case, snap):
    case["expect"]["endpoints"].append(
        _search_corrupt_row(snap, "synthetic-a", "vendor/plain"))


def _corrupt_regex_match(case, snap):
    case["expect"]["endpoints"].append(
        _search_corrupt_row(snap, "synthetic-a", "vendor/plain"))


def _corrupt_metadata_text_match(case, snap):
    case["expect"]["endpoints"].append(
        _search_corrupt_row(snap, "synthetic-a", "vendor/literal[ab].*"))


def _corrupt_slice_before_filter(case, snap):
    """Genuine wrong-order rule mutant (not a golden truncation): derive the
    UNFILTERED ordered catalogue from snapshot DATA, take the requested
    offset/limit window BEFORE evaluating the active filters, then keep only
    matching pairs from that window. Totals stay the CORRECT unpaginated match
    cardinality; the wrong page's rows/counts/returned_count/next_offset are
    rebuilt from snapshot data so rejection proves wrong membership, not an
    unrelated stale count.

    On the appended filtered-page golden (query="shared", limit=2, offset=0)
    the pre-sliced window is the first two registry rows, which match no
    "shared" endpoint, so the emitted buggy page is EMPTY while total stays 2.
    The checker rejects that empty page on membership/order -- the buggy result
    is never accepted as a valid page."""
    args = case["args"]
    offset = args.get("offset", SEARCH_DEFAULT_OFFSET)
    limit = args.get("limit", SEARCH_DEFAULT_LIMIT)
    registry = sorted(
        ((key, raw_id)
         for key in snap["provider_order"]
         for raw_id in snap["providers"][key]["model_ids"]),
        key=lambda pair: (snap["provider_order"].index(pair[0]),
                          _natural_key_contract(pair[1])))
    window = registry[offset:offset + limit]
    matched = set(_search_reference_pairs(snap, args))
    page = [pair for pair in window if pair in matched]
    expect = case["expect"]
    expect["endpoints"] = [
        _search_corrupt_row(snap, provider, raw_id)
        for provider, raw_id in page]
    expect["total_matches"] = len(matched)
    expect["returned_count"] = len(page)
    expect["next_offset"] = (
        offset + len(page) if offset + len(page) < len(matched) else None)
    expect["counts"] = {
        "gateways": len({provider for provider, _ in page}),
        "endpoints": len(page),
        "distinct_raw_ids": len({raw_id for _, raw_id in page}),
        "marker_group_labels": len(
            {_strip_marker_contract(raw_id) for _, raw_id in page}),
    }


def _corrupt_unsorted_page_window(case, snap):
    case["expect"]["endpoints"].reverse()


def _corrupt_next_offset_at_exhausted(case, snap):
    case["expect"]["next_offset"] = 5


def _corrupt_next_offset_beyond_end(case, snap):
    case["expect"]["next_offset"] = 99


def _corrupt_bool_count(case, snap):
    case["expect"]["counts"]["endpoints"] = True


def _corrupt_page_counts_use_global(case, snap):
    case["expect"]["counts"]["distinct_raw_ids"] = 5


def _corrupt_drop_zero_id_staleness(case, snap):
    del case["expect"]["provider_staleness"]["synthetic-b"]


SEARCH_CORRUPTIONS = (
    ("null context treated as zero",
     "search_metadata_reasoning_min_context_zero", _corrupt_null_context_as_zero),
    ("null arrays made [] with lost flags",
     "search_text_raw_id_unknown_flags",
     _corrupt_null_arrays_to_empty_lost_flags),
    ("cross-provider a-context+b-image merged",
     "search_shared_context_and_image_neither",
     _corrupt_cross_provider_merged_hit),
    ("wrong-provider metadata on shared raw id",
     "search_shared_modalities_and_b_only", _corrupt_wrong_provider_metadata),
    ("AND replaced by OR",
     "search_shared_modalities_and_b_only", _corrupt_and_replaced_by_or),
    ("accidental Unicode folding of É/é",
     "search_text_unicode_upper_echo_first", _corrupt_unicode_folding),
    ("regex instead of literal substring",
     "search_text_literal_brackets_not_regex", _corrupt_regex_match),
    ("JSON-metadata text match",
     "search_text_metadata_array_not_searched", _corrupt_metadata_text_match),
    ("slicing before filtering",
     "search_shared_query_limit2_offset0", _corrupt_slice_before_filter),
    ("unsorted page window (slice before sort)",
     "search_pagination_limit2_offset2_crosses_provider",
     _corrupt_unsorted_page_window),
    ("next_offset wrong at exhausted page",
     "search_pagination_limit2_offset4_exhausted",
     _corrupt_next_offset_at_exhausted),
    ("next_offset non-null beyond end",
     "search_pagination_limit2_offset99_beyond_end",
     _corrupt_next_offset_beyond_end),
    ("bool count",
     "search_capability_defaults_both_models", _corrupt_bool_count),
    ("page counts use global totals",
     "search_pagination_limit2_offset0", _corrupt_page_counts_use_global),
    ("dropped zero-ID staleness entry",
     "search_capability_defaults_both_models", _corrupt_drop_zero_id_staleness),
)


@pytest.mark.parametrize(
    "label,case_id,mutate", SEARCH_CORRUPTIONS,
    ids=[c[0] for c in SEARCH_CORRUPTIONS])
def test_search_integrity_rejects_semantic_corruption(
        cases_document, label, case_id, mutate):
    case, snap = _copy_search_case(cases_document, case_id)
    # The unmutated golden is well-formed before corruption.
    _assert_search_payload(case, snap)
    mutate(case, snap)
    with pytest.raises(AssertionError):
        _assert_search_payload(case, snap)


def test_search_integrity_rejects_bool_scalars_and_extra_keys(cases_document):
    case, snap = _copy_search_case(cases_document,
                                   "search_capability_defaults_both_models")
    case["expect"]["returned_count"] = True
    with pytest.raises(AssertionError, match="returned_count"):
        _assert_search_payload(case, snap)

    case, snap = _copy_search_case(cases_document,
                                   "search_capability_defaults_both_models")
    case["expect"]["surprise"] = 1
    with pytest.raises(AssertionError, match="unexpected response fields"):
        _assert_search_payload(case, snap)

    case, snap = _copy_search_case(cases_document,
                                   "search_capability_defaults_both_models")
    case["expect"]["source"] = {"mode": "file"}
    with pytest.raises(AssertionError, match="unexpected response fields"):
        _assert_search_payload(case, snap)

    # The critical bool-vs-int guard on a missing page.
    case, snap = _copy_search_case(cases_document,
                                   "search_pagination_limit2_offset99_beyond_end")
    case["expect"]["total_matches"] = True
    with pytest.raises(AssertionError):
        _assert_search_payload(case, snap)


def test_search_positive_control_consistent_stale_mutation_passes(
        cases_document):
    # A CONSISTENT mutation (snapshot + golden) keeps the checker honest:
    # flip synthetic-b's stale flag everywhere and re-seal, then require PASS.
    case, snap = _copy_search_case(
        cases_document, "search_capability_defaults_both_models")
    assert case["expect"]["provider_staleness"]["synthetic-b"] is True
    snap["providers"]["synthetic-b"]["stale"] = False
    _reseal_snapshot(snap)
    case["expect"]["snapshot_id"] = snap["snapshot_id"]
    case["expect"]["provider_staleness"]["synthetic-b"] = False
    _assert_snapshot_contract(snap)
    _assert_search_payload(case, snap)


# ---------- README contract-draft pins ----------


def _contract_section_text():
    """The README body of the v1 contract section ONLY (from the
    `## Published catalogue snapshot (v1)` heading to the next `## `
    heading). Scoping the pins here means unrelated prose elsewhere in
    README.md can never accidentally satisfy a contract assertion."""
    text = README.read_text(encoding="utf-8")
    heading = f"## {CONTRACT_SECTION}"
    start = text.index(heading)
    body = text[start:]
    end = body.find("\n## ", len(heading))
    return body if end == -1 else body[:end]


def test_readme_contract_section_freezes_snapshot_fields():
    text = _contract_section_text()
    assert CONTRACT_SECTION in text
    for field in sorted(TOP_LEVEL_FIELDS | PROVIDER_FIELDS | MODEL_FIELDS
                        | ARCHITECTURE_FIELDS | PRICING_FIELDS):
        assert f"`{field}`" in text, f"contract draft missing field {field}"


def test_readme_contract_freezes_tool_surface():
    text = _contract_section_text()
    for tool in ("list_free_models", "get_model", "list_endpoints",
                 "watchdog_status", "search_models"):
        assert f"`{tool}(" in text, f"contract draft missing tool {tool}"


def test_readme_contract_freezes_warning_vocabulary():
    text = _contract_section_text()
    for code in sorted(WARNING_CODES):
        assert f"`{code}`" in text, f"contract draft missing warning {code}"


def test_readme_contract_freezes_error_rules():
    text = _contract_section_text()
    for phrase in ("fail export", "NaN", "Infinity",
                   "duplicate registry keys", "last published artifact"):
        assert phrase in text, f"contract draft missing error rule: {phrase}"


def test_readme_contract_freezes_identity_rules():
    text = _contract_section_text()
    for phrase in ("literal raw-ID membership", "[-:_]",
                   "empty-result guard",
                   "not proof of cross-gateway model identity",
                   "(provider, raw model_id)", "never silently trimmed"):
        assert phrase in text, \
            f"contract draft missing identity rule: {phrase}"


def test_readme_contract_freezes_get_model_envelope_field_sets():
    text = _contract_section_text()
    for field in sorted(GET_MODEL_RESPONSE_FIELDS | ENDPOINT_FIELDS
                        | COUNT_FIELDS):
        assert f"`{field}`" in text, \
            f"contract section missing envelope field {field}"


def test_readme_contract_freezes_count_definitions():
    text = _contract_section_text()
    for phrase in ("Matching gateways", "distinct raw", "marker group",
                   "never the global registry", "all zeros",
                   "provider display order"):
        assert phrase in text, \
            f"contract section missing count rule: {phrase}"


def test_readme_contract_distinguishes_provider_staleness_from_tick_age():
    text = _contract_section_text()
    for phrase in ("provider staleness, never tick age",
                   "carried-forward stale flag", "provider_staleness"):
        assert phrase in text, \
            f"contract section missing staleness rule: {phrase}"


def test_readme_contract_freezes_metadata_projection_boundary():
    text = _contract_section_text()
    for phrase in ("projected public model object",
                   "never the legacy raw metadata",
                   "PRESERVES raw tracked metadata"):
        assert phrase in text, \
            f"contract section missing metadata boundary: {phrase}"


def test_readme_contract_resolves_pricing_nullability():
    text = _contract_section_text()
    for phrase in ("whole object", "individually unknown", "Booleans"):
        assert phrase in text, \
            f"contract section missing pricing rule: {phrase}"


def test_readme_contract_freezes_nullable_capability_and_normalization_rules():
    text = _contract_section_text()
    for phrase in (
            "each a list of nonempty strings or `null`",
            "KNOWN empty",
            "safe non-negative integer",
            "never partially salvaged",
            "ASCII-only lowercased and first-occurrence deduplicated",
            "Unicode casefolding repairs a token",
            "empty FILTER array applies no constraint",
            "no schema-version bump",
    ):
        assert phrase in text, \
            f"contract section missing nullable/normalization rule: {phrase}"


def test_readme_contract_enumerates_normalized_capability_fields_in_order():
    text = _contract_section_text()
    ordered = ("normalized capability fields are `context_length`, "
               "`input_modalities`, `output_modalities`, "
               "`supported_parameters` plus `unknown_fields`")
    assert ordered in text, \
        "normalization capability fields must be listed in the stated order"


def test_readme_contract_freezes_search_envelope_field_sets():
    text = _contract_section_text()
    for field in sorted(SEARCH_RESPONSE_FIELDS | SEARCH_FILTER_FIELDS
                        | SEARCH_ENDPOINT_FIELDS | SEARCH_COUNT_FIELDS):
        assert f"`{field}`" in text, \
            f"contract section missing search field {field}"


def test_readme_contract_freezes_search_semantics():
    text = _contract_section_text()
    for phrase in (
            "search_models(query=None",
            "literal substring",
            "ASCII case-insensitive",
            "no regular expression",
            "never trimmed",
            "applies no text constraint",
            "AND",
            "Sort, then slice",
            "returned page",
            "unpaginated",
            "zero tracked IDs",
            "absence caveat",
            "unsafe UPSTREAM context",
            "unsafe integers in PUBLIC JSON are rejected",
    ):
        assert phrase in text, \
            f"contract section missing search rule: {phrase}"
