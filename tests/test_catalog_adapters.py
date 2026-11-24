"""Tests for catalog_adapters.py — the reusable catalogue seam.

Six formats are registered, all actually implemented: the legacy
``openai-models`` shape (whose envelope/identity handling stays in providers.py),
the Vercel ``input-output-models`` price/shape family, the Felo
``channel-catalog`` family, the TokenRA ``newapi-pricing`` family, whose
literal identity is ``model_name`` rather than ``id``, the Infron
``provider-models`` family, whose nested per-provider route must be wholly
known zero, and the AI/ML API ``typed-unit-models`` family, whose fixed
charge-unit set must be wholly zero. These tests pin the seam:

* the format registry is the actually-implemented set, not a placeholder list;
* every family's envelope and identity shape round-trips a small hand-authored
  body without aliasing the parsed rows;
* every price rule fails closed on unknown/malformed input and uses exact
  decimal arithmetic, so a tiny positive string never rounds to zero;
* the metadata view is a fresh, non-aliasing projection input, never a rewritten
  raw record.

No network, no config, no state: pure functions over hand-authored data.
"""
import copy
import json

import pytest

import catalog_adapters as adapters

_SENTINEL = object()


def _row(pricing=_SENTINEL, mid="vendor/model", **extra):
    row = {"id": mid, "type": "language"}
    if pricing is not _SENTINEL:
        row["pricing"] = pricing
    row.update(extra)
    return row


def _free(pricing, **extra):
    return adapters.is_free_row(_row(pricing, **extra),
                                adapters.INPUT_OUTPUT_FORMAT)


def _payload(*rows):
    return json.dumps({"object": "list", "data": list(rows)})


def _ids(rows):
    return [row["id"] for row in rows
            if adapters.is_free_row(row, adapters.INPUT_OUTPUT_FORMAT)]


# Small hand-authored Vercel-shaped rows: one per price/shape family the rule
# distinguishes (ordinary zero, tier ladder, provider-variable aggregate, paid
# rates, regional/peak modifiers, non-language product).
VERCEL_ROWS = {
    "vendor/free-plain": _row({"input": "0", "output": "0"},
                              mid="vendor/free-plain"),
    "vendor/stealth-launch": _row(
        {"input": "0", "output": "0"},
        mid="vendor/stealth-launch",
        name="Stealth Launch",
        description="Stealth Launch is an anonymous zero-priced model.",
        context_window=256000,
        supported_parameters=["max_tokens", "temperature", "stop", "tools",
                              "tool_choice", "reasoning", "include_reasoning"],
        modalities={"input": ["text"], "output": ["text"]},
        zdr=True, tags=["stealth"]),
    "vendor/free-suffix-free": _row({"input": "0", "output": "0"},
                                    mid="vendor/free-suffix-free"),
    "vendor/free-tiered": _row(
        {"input": "0", "output": "0",
         "input_tiers": [{"cost": "0", "min": 0, "max": 100},
                         {"cost": "0", "min": 100}]},
        mid="vendor/free-tiered"),
    "vendor/paid-rates": _row({"input": "0.000002", "output": "0.000006"},
                              mid="vendor/paid-rates"),
    "vendor/provider-variable": _row(
        {"input": "0", "output": "0", "varies_by_provider": True},
        mid="vendor/provider-variable"),
    "vendor/paid-tier": _row(
        {"input": "0", "output": "0",
         "input_tiers": [{"cost": "0", "min": 0, "max": 100},
                         {"cost": "0.0000024", "min": 100}]},
        mid="vendor/paid-tier"),
    "vendor/paid-regional": _row(
        {"input": "0", "output": "0",
         "regional": {"eu": {"input": "0.00000054"}}},
        mid="vendor/paid-regional"),
    "vendor/paid-peak": _row(
        {"input": "0.00000054", "output": "0",
         "peak_pricing": {"multiplier": 2}},
        mid="vendor/paid-peak"),
    "vendor/image-product": _row({}, mid="vendor/image-product",
                                 type="image"),
}

VERCEL_FREE_IDS = [
    "vendor/free-plain",
    "vendor/stealth-launch",
    "vendor/free-suffix-free",
    "vendor/free-tiered",
]
VERCEL_TEXT = _payload(*VERCEL_ROWS.values())
VERCEL_SUBSET = {"data": list(VERCEL_ROWS.values())}


# ---------- format registry ----------

def test_registered_formats_are_exactly_the_implemented_families():
    assert adapters.LEGACY_FORMAT == "openai-models"
    assert adapters.INPUT_OUTPUT_FORMAT == "input-output-models"
    assert adapters.CHANNEL_CATALOG_FORMAT == "channel-catalog"
    assert adapters.NEWAPI_PRICING_FORMAT == "newapi-pricing"
    assert adapters.PROVIDER_MODELS_FORMAT == "provider-models"
    assert adapters.TYPED_UNIT_MODELS_FORMAT == "typed-unit-models"
    assert adapters.REGISTERED_FORMATS == ("openai-models",
                                          "input-output-models",
                                          "channel-catalog",
                                          "newapi-pricing",
                                          "provider-models",
                                          "typed-unit-models")
    assert adapters.is_registered_format("openai-models") is True
    assert adapters.is_registered_format("input-output-models") is True
    assert adapters.is_registered_format("channel-catalog") is True
    assert adapters.is_registered_format("newapi-pricing") is True
    assert adapters.is_registered_format("provider-models") is True
    assert adapters.is_registered_format("typed-unit-models") is True


@pytest.mark.parametrize("value", ["not-a-catalog-format", "", "OpenAI-Models",
                                   None, 1, True])
def test_unimplemented_or_junk_formats_are_not_registered(value):
    """Later slices extend the registry; P5 registers no placeholder."""
    assert adapters.is_registered_format(value) is False


def test_legacy_format_is_recognised_but_not_dispatchable():
    assert adapters.is_legacy_format("openai-models") is True
    assert adapters.is_legacy_format(None) is True
    assert adapters.is_legacy_format("input-output-models") is False
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(VERCEL_TEXT, adapters.LEGACY_FORMAT)
    assert adapters.is_free_row(_row({"input": "0", "output": "0"}),
                                adapters.LEGACY_FORMAT) is False


# ---------- synthetic Vercel body ----------

def test_synthetic_subset_parses_to_the_original_rows():
    rows = adapters.parse_catalog(VERCEL_TEXT, adapters.INPUT_OUTPUT_FORMAT)
    assert [row["id"] for row in rows] == [row["id"]
                                           for row in VERCEL_SUBSET["data"]]
    for row in rows:
        assert row == VERCEL_ROWS[row["id"]]
    assert rows[0] is not VERCEL_SUBSET["data"][0]


def test_synthetic_subset_reproduces_the_free_ids():
    rows = adapters.parse_catalog(VERCEL_TEXT, adapters.INPUT_OUTPUT_FORMAT)
    assert sorted(_ids(rows)) == sorted(VERCEL_FREE_IDS)


@pytest.mark.parametrize("model_id", VERCEL_FREE_IDS)
def test_each_free_id_is_admitted(model_id):
    """Including unsuffixed stealth ids: no :free/-free suffix is required."""
    assert adapters.is_free_row(VERCEL_ROWS[model_id],
                                adapters.INPUT_OUTPUT_FORMAT) is True


@pytest.mark.parametrize("model_id,reason", [
    ("vendor/provider-variable", "nested varies_by_provider=true"),
    ("vendor/paid-rates", "paid input/output"),
    ("vendor/paid-tier", "paid tier ladder"),
    ("vendor/paid-regional", "paid regional branch"),
    ("vendor/paid-peak", "positive base under a peak modifier"),
    ("vendor/image-product", "non-language product"),
])
def test_synthetic_negatives_are_not_admitted(model_id, reason):
    assert adapters.is_free_row(VERCEL_ROWS[model_id],
                                adapters.INPUT_OUTPUT_FORMAT) is False, reason


def test_variable_provider_flag_is_the_nested_pricing_member():
    """``varies_by_provider`` lives under pricing, never at the model top."""
    variable = VERCEL_ROWS["vendor/provider-variable"]
    assert variable["pricing"]["varies_by_provider"] is True
    assert "varies_by_provider" not in variable
    paid_lookalike = _row({"input": "0", "output": "0"},
                          varies_by_provider=False)
    assert adapters.is_free_row(paid_lookalike,
                                adapters.INPUT_OUTPUT_FORMAT) is True


# ---------- explicit zero prices ----------

@pytest.mark.parametrize("zero", ["0", "0.0", "0.00", "0e5", 0, 0.0])
def test_explicit_zero_spellings_are_free(zero):
    assert _free({"input": zero, "output": zero}) is True


@pytest.mark.parametrize("pricing", [
    {},
    {"input": "0"},
    {"output": "0"},
    {"input": None, "output": "0"},
    {"input": "0", "output": None},
    None,
])
def test_missing_or_one_sided_prices_are_not_free(pricing):
    assert _free(pricing) is False


def test_absent_pricing_container_is_not_free():
    assert adapters.is_free_row(_row(), adapters.INPUT_OUTPUT_FORMAT) is False


@pytest.mark.parametrize("pricing", [
    {"input": "0.00000001", "output": "0"},
    {"input": "0", "output": "0.00000001"},
    {"input": "1e-400", "output": "0"},
    {"input": "0", "output": "1e-400"},
])
def test_positive_prices_are_not_free_even_when_tiny(pricing):
    """Exact Decimal comparison: a tiny positive string never rounds to zero."""
    assert _free(pricing) is False


@pytest.mark.parametrize("fee", ["0", "0.0", 0, None])
def test_zero_or_absent_request_fee_is_allowed(fee):
    assert _free({"input": "0", "output": "0", "request": fee}) is True


@pytest.mark.parametrize("fee", ["0.000001", "1", 0.5, "1e-400"])
def test_positive_compulsory_request_fee_disqualifies(fee):
    assert _free({"input": "0", "output": "0", "request": fee}) is False


MALFORMED_PRICES = [
    True, False, -1, -0.5, -1e-9, float("nan"), float("inf"),
    float("-inf"), [], {}, "", " ", " 0", "0 ", "abc", "1e", "NaN",
    "Infinity", "-Infinity",
]


@pytest.mark.parametrize("bad", MALFORMED_PRICES)
@pytest.mark.parametrize("slot", ["input", "output", "request"])
def test_malformed_price_values_raise_a_structural_fault(bad, slot):
    pricing = {"input": "0", "output": "0", "request": "0"}
    pricing[slot] = bad
    with pytest.raises(adapters.AdapterError):
        _free(pricing)


@pytest.mark.parametrize("container", ["0", [], 1, True])
def test_malformed_pricing_container_raises(container):
    with pytest.raises(adapters.AdapterError):
        _free(container)


@pytest.mark.parametrize("flag", [True, "false", 0, 1, None])
def test_present_variable_provider_flag_must_be_exactly_false(flag):
    assert _free({"input": "0", "output": "0",
                  "varies_by_provider": flag}) is False


# ---------- tier ladders ----------

def test_absent_or_empty_tier_ladders_are_allowed():
    assert _free({"input": "0", "output": "0"}) is True
    assert _free({"input": "0", "output": "0",
                  "input_tiers": [], "output_tiers": []}) is True


@pytest.mark.parametrize("slot", ["input_tiers", "output_tiers"])
def test_zero_base_with_positive_tier_is_not_free(slot):
    assert _free({"input": "0", "output": "0",
                  slot: [{"cost": "0", "min": 0, "max": 100},
                         {"cost": "0.0000024", "min": 100}]}) is False


@pytest.mark.parametrize("slot", ["input_tiers", "output_tiers"])
def test_all_zero_tier_ladder_is_free(slot):
    assert _free({"input": "0", "output": "0",
                  slot: [{"cost": "0", "min": 0, "max": 32001},
                         {"cost": "0.0", "min": 32001}]}) is True


@pytest.mark.parametrize("tier", [
    {"min": 0},
    {"cost": None, "min": 0},
    {},
])
def test_tier_without_a_known_zero_cost_is_not_free(tier):
    assert _free({"input": "0", "output": "0", "input_tiers": [tier]}) is False


@pytest.mark.parametrize("ladder", ["0", {}, [1], [{"cost": True}],
                                    [{"cost": "0"}, "x"]])
def test_malformed_tier_container_raises(ladder):
    with pytest.raises(adapters.AdapterError):
        _free({"input": "0", "output": "0", "input_tiers": ladder})


# ---------- service_tiers / regional partial-override inheritance ----------

@pytest.mark.parametrize("slot", ["service_tiers", "regional"])
def test_positive_modifier_branch_disqualifies(slot):
    assert _free({"input": "0", "output": "0",
                  slot: {"priority": {"input": "0.00000054"}}}) is False


@pytest.mark.parametrize("slot", ["service_tiers", "regional"])
def test_zero_modifier_branch_inherits_the_zero_base(slot):
    assert _free({"input": "0", "output": "0",
                  slot: {"priority": {"input": "0", "output": "0"}}}) is True
    # Partial override: an omitted axis inherits the containing zero rate.
    assert _free({"input": "0", "output": "0",
                  slot: {"us": {"output": "0"}}}) is True


@pytest.mark.parametrize("slot", ["service_tiers", "regional"])
def test_partial_override_inherits_then_checks_nested_tiers(slot):
    # Only the input axis is named: output must inherit the containing zero.
    assert _free({"input": "0", "output": "0",
                  slot: {"x": {"input": "0",
                               "input_tiers": [{"cost": "0.0", "min": 0}]}}}) is True
    # The same inherited fragment with a positive nested tier is not free.
    assert _free({"input": "0", "output": "0",
                  slot: {"x": {"input": "0",
                               "input_tiers": [{"cost": "0.1", "min": 0}]}}}) is False


@pytest.mark.parametrize("slot", ["service_tiers", "regional"])
def test_modifier_fragment_cannot_inherit_a_positive_base(slot):
    assert _free({"input": "0.1", "output": "0",
                  slot: {"x": {"output": "0"}}}) is False


@pytest.mark.parametrize("slot", ["service_tiers", "regional"])
def test_malformed_modifier_container_raises(slot):
    with pytest.raises(adapters.AdapterError):
        _free({"input": "0", "output": "0", slot: "cheap"})


@pytest.mark.parametrize("slot", ["service_tiers", "regional"])
def test_malformed_modifier_fragment_raises(slot):
    with pytest.raises(adapters.AdapterError):
        _free({"input": "0", "output": "0", slot: {"x": 1}})


# ---------- peak_pricing multiplier/window modifier ----------

@pytest.mark.parametrize("multiplier", [2, 1.5, "2", "0.5", 0])
def test_peak_multiplier_over_a_proven_zero_stays_zero(multiplier):
    assert _free({"input": "0", "output": "0",
                  "peak_pricing": {"multiplier": multiplier,
                                   "windows": [{"start_minute_utc": 540,
                                                "end_minute_utc": 90}]}}) is True


@pytest.mark.parametrize("peak", [
    {"multiplier": -1},
    {"multiplier": True},
    {"multiplier": "abc"},
    {"multiplier": None},
    {"multiplier": float("nan")},
    {},
    "2x",
    [],
    {"multiplier": 2, "windows": "peak"},
    {"multiplier": 2, "windows": [1]},
])
def test_unknown_or_malformed_peak_modifier_is_not_free(peak):
    """Valid-but-unsupported modifier shapes are nonqualifying, not zero."""
    assert _free({"input": "0", "output": "0", "peak_pricing": peak}) is False


def test_peak_modifier_cannot_rescue_a_paid_base():
    assert _free({"input": "0.5", "output": "0",
                  "peak_pricing": {"multiplier": 0}}) is False


# The supported shape is exactly the documented one: a `multiplier` and a
# `windows` list whose entries carry the two published minute keys. Every other
# member could be a rate override or an additive fee this format does not
# define, so it is nonqualifying rather than guessed (fail-closed).

@pytest.mark.parametrize("extra", [
    {"input": "0.5"},
    {"output": "0.5"},
    {"request": "0.75"},
    {"input": "0"},
    {"output": "0"},
    {"request": "0"},
    {"additive_fee": "0.01"},
    {"additive_fee": "0"},
    {"unknown_modifier": None},
    {"windows": [{"start_minute_utc": 0}], "input": "0.5"},
])
def test_peak_unknown_or_rate_bearing_members_are_not_free(extra):
    peak = {"multiplier": 2}
    peak.update(extra)
    assert _free({"input": "0", "output": "0", "peak_pricing": peak}) is False


@pytest.mark.parametrize("window", [
    {"start_minute_utc": 0, "end_minute_utc": 60, "input": "0.5"},
    {"start_minute_utc": 0, "end_minute_utc": 60, "output": "0.5"},
    {"start_minute_utc": 0, "end_minute_utc": 60, "request": "0.75"},
    {"start_minute_utc": 0, "end_minute_utc": 60, "input": "0"},
    {"start_minute_utc": 0, "end_minute_utc": 60, "additive_fee": "0.01"},
    {"start_minute_utc": 0, "end_minute_utc": 60, "unknown": 0},
    {"start": 0, "end": 60},
    {},
])
def test_peak_window_unknown_or_rate_bearing_members_are_not_free(window):
    peak = {"multiplier": 2, "windows": [window]}
    assert _free({"input": "0", "output": "0", "peak_pricing": peak}) is False


@pytest.mark.parametrize("peak", [
    {"multiplier": 2},
    {"multiplier": 0},
    {"multiplier": 2, "windows": []},
    {"multiplier": 2, "windows": [{"start_minute_utc": 540,
                                   "end_minute_utc": 90}]},
    {"multiplier": 2, "windows": [{"start_minute_utc": 0,
                                   "end_minute_utc": 60},
                                  {"start_minute_utc": 540,
                                   "end_minute_utc": 90}]},
])
def test_supported_peak_shape_over_a_proven_zero_stays_free(peak):
    assert _free({"input": "0", "output": "0", "peak_pricing": peak}) is True


# ---------- optional charges are preserved, not required to be free ----------

@pytest.mark.parametrize("pricing", [
    {"input": "0", "output": "0", "web_search": "0.01"},
    {"input": "0", "output": "0", "maps_search": "25"},
    {"input": "0", "output": "0", "fast": "2"},
    {"input": "0", "output": "0", "input_cache_read": "0.000001"},
    {"input": "0", "output": "0", "input_cache_write_12h": "1"},
    {"input": "0", "output": "0", "future_charge": "9"},
])
def test_optional_charge_modes_do_not_disqualify_base_chat(pricing):
    assert _free(pricing) is True


# ---------- envelope, identity and duplicates ----------

@pytest.mark.parametrize("body", [
    "not json",
    "[]",
    '[{"id": "a", "type": "language"}]',
    '{"data": {"id": "a"}}',
    '{"data": null}',
    '{"object": "list"}',
    '{"data": [1]}',
    '{"data": ["a"]}',
    '{"data": [{"type": "language"}]}',
    '{"data": [{"id": "", "type": "language"}]}',
    '{"data": [{"id": 7, "type": "language"}]}',
])
def test_malformed_envelope_or_identity_is_a_structural_fault(body):
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(body, adapters.INPUT_OUTPUT_FORMAT)


def test_conflicting_duplicate_ids_raise():
    body = _payload(_row({"input": "0", "output": "0"}, mid="dup/model"),
                    _row({"input": "0.2", "output": "0"}, mid="dup/model"))
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(body, adapters.INPUT_OUTPUT_FORMAT)


def test_equivalent_duplicate_ids_are_allowed_and_deduplicated():
    row = _row({"input": "0", "output": "0"}, mid="dup/model")
    rows = adapters.parse_catalog(_payload(row, copy.deepcopy(row)),
                                  adapters.INPUT_OUTPUT_FORMAT)
    assert [r["id"] for r in rows] == ["dup/model"]
    assert adapters.is_free_row(rows[0], adapters.INPUT_OUTPUT_FORMAT) is True


def test_literal_ids_are_preserved():
    ids = ["  Vendor/Padded  ", "vendor/Ünicode-Δ", "vendor/zero", "Vendor/Case"]
    rows = [{"id": mid, "type": "language",
             "pricing": {"input": "0", "output": "0"}} for mid in ids]
    parsed = adapters.parse_catalog(_payload(*rows), adapters.INPUT_OUTPUT_FORMAT)
    assert [row["id"] for row in parsed] == ids
    assert sorted(_ids(parsed)) == sorted(ids)


def test_valid_empty_and_paid_only_catalogues_are_successes():
    assert adapters.parse_catalog('{"data": []}',
                                  adapters.INPUT_OUTPUT_FORMAT) == []
    paid = adapters.parse_catalog(
        _payload(_row({"input": "1", "output": "1"}, mid="paid/model")),
        adapters.INPUT_OUTPUT_FORMAT)
    assert len(paid) == 1 and _ids(paid) == []


# ---------- explicit upstream failure envelope ----------
#
# Plan §5: "Reject success-false payloads." A body whose `success` member is
# exactly JSON false describes a FAILED upstream response, so it is a structural
# fault even when `data` is empty or otherwise well-formed -- never a healthy
# empty observation. Only the exact boolean false is honoured; other members,
# including 0/"false"/null, are not coerced into meaning.

@pytest.mark.parametrize("payload", [
    {"success": False, "data": []},
    {"success": False, "data": [{"id": "vendor/free", "type": "language",
                                 "pricing": {"input": "0", "output": "0"}}]},
    {"success": False, "data": [{"id": "vendor/paid", "type": "language",
                                 "pricing": {"input": "1", "output": "1"}}]},
    {"success": False, "data": [], "object": "list"},
    {"success": False},
])
def test_explicit_success_false_envelope_is_a_structural_fault(payload):
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(json.dumps(payload),
                               adapters.INPUT_OUTPUT_FORMAT)


@pytest.mark.parametrize("payload,expected_ids", [
    ({"data": []}, []),
    ({"success": True, "data": []}, []),
    ({"success": True, "data": [{"id": "vendor/free", "type": "language",
                                 "pricing": {"input": "0", "output": "0"}}]},
     ["vendor/free"]),
    ({"success": True, "data": [{"id": "vendor/paid", "type": "language",
                                 "pricing": {"input": "1", "output": "0"}}]},
     []),
    ({"success": None, "data": []}, []),
    ({"success": 0, "data": []}, []),
    ({"success": "false", "data": []}, []),
])
def test_absent_true_or_non_boolean_success_envelopes_still_parse(
        payload, expected_ids):
    """Only exact JSON false is a failure; nothing is coerced into one."""
    rows = adapters.parse_catalog(json.dumps(payload),
                                  adapters.INPUT_OUTPUT_FORMAT)
    assert [row["id"] for row in rows
            if adapters.is_free_row(row, adapters.INPUT_OUTPUT_FORMAT)] == \
        expected_ids


def test_non_language_rows_are_out_of_scope_without_inspecting_pricing():
    image = {"id": "bfl/flux-2-flex", "type": "image", "pricing": {}}
    assert adapters.is_free_row(image, adapters.INPUT_OUTPUT_FORMAT) is False
    # Even a malformed pricing container on an out-of-scope row is not read.
    assert adapters.is_free_row({"id": "x", "type": "speech",
                                 "pricing": "n/a"},
                                adapters.INPUT_OUTPUT_FORMAT) is False


# ---------- metadata view ----------

def test_metadata_view_maps_the_vercel_fields():
    view = adapters.metadata_view(VERCEL_ROWS["vendor/stealth-launch"],
                                  adapters.INPUT_OUTPUT_FORMAT)
    assert view["context_length"] == 256000
    assert view["architecture"] == {"input_modalities": ["text"],
                                    "output_modalities": ["text"]}
    assert view["name"] == "Stealth Launch"
    assert view["description"].startswith("Stealth Launch is an anonymous")
    assert view["supported_parameters"] == [
        "max_tokens", "temperature", "stop", "tools", "tool_choice",
        "reasoning", "include_reasoning"]
    assert "pricing" not in view and "zdr" not in view and "tags" not in view


def test_metadata_view_does_not_alias_or_mutate_the_raw_row():
    raw = copy.deepcopy(VERCEL_ROWS["vendor/stealth-launch"])
    before = copy.deepcopy(raw)
    view = adapters.metadata_view(raw, adapters.INPUT_OUTPUT_FORMAT)
    assert view is not raw
    assert view["architecture"]["input_modalities"] is not raw["modalities"]["input"]
    assert view["supported_parameters"] is not raw["supported_parameters"]
    view["architecture"]["input_modalities"].append("image")
    view["supported_parameters"].append("tools")
    assert raw == before


def test_metadata_view_does_not_coerce_optional_values():
    row = _row({"input": "0", "output": "0"}, mid="vendor/model",
               context_window=1.0, name=7, description=("kept"),
               supported_parameters="tools",
               modalities={"input": "text", "output": ["text"]})
    view = adapters.metadata_view(row, adapters.INPUT_OUTPUT_FORMAT)
    assert view["context_length"] == 1.0 and isinstance(view["context_length"], float)
    assert view["name"] == 7
    assert view["description"] == "kept"
    assert view["supported_parameters"] == "tools"
    assert view["architecture"] == {"input_modalities": "text",
                                    "output_modalities": ["text"]}


def test_metadata_view_keeps_missing_and_malformed_containers_distinct():
    missing = adapters.metadata_view(
        {"id": "m", "type": "language", "pricing": {}},
        adapters.INPUT_OUTPUT_FORMAT)
    assert "architecture" not in missing and "context_length" not in missing
    malformed = adapters.metadata_view(
        {"id": "m", "type": "language", "pricing": {}, "modalities": "text"},
        adapters.INPUT_OUTPUT_FORMAT)
    assert malformed["architecture"] == "text"


def test_metadata_view_passes_non_dict_metadata_through_untouched():
    assert adapters.metadata_view(None, adapters.INPUT_OUTPUT_FORMAT) is None
    assert adapters.metadata_view("junk", adapters.INPUT_OUTPUT_FORMAT) == "junk"


def test_metadata_view_legacy_is_a_shallow_copy():
    raw = {"id": "m", "context_length": 8}
    view = adapters.metadata_view(raw, adapters.LEGACY_FORMAT)
    assert view == raw and view is not raw


# =====================================================================
# Felo channel catalogue (`channel-catalog`) — P2
# =====================================================================
#
# Small hand-authored rows mirror every branch of the ALL-runnable predicate:
# ordinary zero rates, an all-zero tier ladder, a zero made from a positive
# rate times an explicit zero discount, plus the retired/non-chat/paid
# counterexample families. Behaviour cases below exercise the remaining shapes
# (mixed runnable channels, malformed billing).


def _felo_channel(prompt="0", completion="0", request=_SENTINEL,
                  discount="1", billing="token", runnable=True,
                  pricing_mode="configured_rates", tiers=_SENTINEL,
                  **extra):
    sp = {"billing_kind": billing}
    if prompt is not _SENTINEL:
        sp["prompt"] = prompt
    if completion is not _SENTINEL:
        sp["completion"] = completion
    if request is not _SENTINEL:
        sp["request"] = request
    if tiers is not _SENTINEL:
        sp["tiers"] = tiers
    channel = {
        "code": "synthetic",
        "runnable": runnable,
        "pricing_mode": pricing_mode,
        "discount_multiplier": discount,
        "standard_pricing": sp,
    }
    channel.update(extra)
    return channel


def _felo_row(*channels, mid="vendor/chat", deprecated=False, kind="chat",
              outputs=("text",), **extra):
    row = {
        "id": mid,
        "deprecated": deprecated,
        "model_kind": kind,
        "output_modalities": list(outputs),
        "channels": list(channels),
    }
    row.update(extra)
    return row


def _felo_body(*rows, total=_SENTINEL, success=_SENTINEL, **extra):
    data = {"total": len(rows) if total is _SENTINEL else total,
            "models": list(rows)}
    payload = {"data": data}
    if success is not _SENTINEL:
        payload["success"] = success
    payload.update(extra)
    return json.dumps(payload)


def _felo_free(*channels, **extra):
    return adapters.is_free_row(_felo_row(*channels, **extra),
                                adapters.CHANNEL_CATALOG_FORMAT)


def _felo_tier(overrides, conditions=None):
    tier = {"overrides": overrides}
    if conditions is not _SENTINEL:
        tier["conditions"] = ([] if conditions is None else conditions)
    return tier


FELO_FREE_IDS = ["vendor/chat-plain", "vendor/chat-tiered",
                 "vendor/chat-discount-zero"]
FELO_ROWS = {
    "vendor/chat-plain": _felo_row(
        _felo_channel(), mid="vendor/chat-plain",
        name="Chat Plain", description="Plain zero-priced chat.",
        context_length=1000000,
        supported_parameters=["max_tokens", "temperature"],
        input_modalities=["text", "image", "video"],
        capabilities=[], parameter_definitions=[{"name": "temperature"}],
        pricing=None),
    "vendor/chat-tiered": _felo_row(
        _felo_channel(tiers=[_felo_tier({"prompt": "0",
                                         "completion": "0.0"})]),
        mid="vendor/chat-tiered"),
    "vendor/chat-discount-zero": _felo_row(
        _felo_channel(prompt="0.000002", completion="0.000006",
                      discount="0"), mid="vendor/chat-discount-zero"),
    "vendor/deprecated": _felo_row(
        _felo_channel(), mid="vendor/deprecated", deprecated=True),
    "vendor/image": _felo_row(
        _felo_channel(), mid="vendor/image", kind="image_generation",
        outputs=("image",)),
    "vendor/video-output": _felo_row(
        _felo_channel(), mid="vendor/video-output", outputs=("video",)),
    "vendor/speech": _felo_row(
        _felo_channel(), mid="vendor/speech", kind="audio_speech",
        outputs=("speech",)),
    "vendor/decisions": _felo_row(
        _felo_channel(), mid="vendor/decisions", kind="decisions"),
    "vendor/paid-chat": _felo_row(
        _felo_channel(prompt="0.000002", completion="0.000006"),
        mid="vendor/paid-chat"),
    "vendor/paid-tier": _felo_row(
        _felo_channel(tiers=[_felo_tier({"prompt": "0.000002"})]),
        mid="vendor/paid-tier"),
}
FELO_TEXT = _felo_body(*FELO_ROWS.values())
FELO_SUBSET = json.loads(FELO_TEXT)


# ---------- synthetic body, free ids and metadata ----------

def test_synthetic_felo_subset_parses_to_the_original_rows():
    rows = adapters.parse_catalog(FELO_TEXT, adapters.CHANNEL_CATALOG_FORMAT)
    assert [row["id"] for row in rows] == [
        row["id"] for row in FELO_SUBSET["data"]["models"]]
    for row in rows:
        assert row == FELO_ROWS[row["id"]]
    assert rows[0] is not FELO_SUBSET["data"]["models"][0]
    assert FELO_SUBSET["data"]["total"] == len(FELO_SUBSET["data"]["models"])


def test_synthetic_felo_subset_reproduces_the_free_ids():
    rows = adapters.parse_catalog(FELO_TEXT, adapters.CHANNEL_CATALOG_FORMAT)
    admitted = sorted(row["id"] for row in rows
                      if adapters.is_free_row(
                          row, adapters.CHANNEL_CATALOG_FORMAT))
    assert admitted == sorted(FELO_FREE_IDS)


@pytest.mark.parametrize("model_id", FELO_FREE_IDS)
def test_each_felo_free_id_is_admitted(model_id):
    assert adapters.is_free_row(FELO_ROWS[model_id],
                                adapters.CHANNEL_CATALOG_FORMAT) is True


@pytest.mark.parametrize("model_id,reason", [
    ("vendor/deprecated", "retired row"),
    ("vendor/image", "non-chat image row"),
    ("vendor/video-output", "chat kind whose output modality is video"),
    ("vendor/speech", "free speech, not free chat"),
    ("vendor/decisions", "free decisions, not chat"),
    ("vendor/paid-chat", "positive standard rates"),
    ("vendor/paid-tier", "positive token tier"),
])
def test_felo_synthetic_counterexamples_are_not_admitted(model_id, reason):
    assert adapters.is_free_row(FELO_ROWS[model_id],
                                adapters.CHANNEL_CATALOG_FORMAT) is False, reason


def test_felo_metadata_view_maps_the_published_fields():
    raw = FELO_ROWS["vendor/chat-plain"]
    view = adapters.metadata_view(raw, adapters.CHANNEL_CATALOG_FORMAT)
    assert view["name"] == "Chat Plain"
    assert view["description"] == "Plain zero-priced chat."
    assert view["context_length"] == 1000000
    assert view["supported_parameters"] == ["max_tokens", "temperature"]
    assert view["architecture"] == {
        "input_modalities": ["text", "image", "video"],
        "output_modalities": ["text"],
    }
    # `capabilities` is not a supported-parameter vocabulary, and neither is
    # `parameter_definitions`.
    assert raw["capabilities"] == []
    assert "capabilities" not in view
    assert "parameter_definitions" not in view
    assert "pricing" not in view and "channels" not in view


def test_felo_metadata_view_does_not_alias_or_mutate_the_raw_row():
    raw = copy.deepcopy(FELO_ROWS["vendor/chat-plain"])
    before = copy.deepcopy(raw)
    view = adapters.metadata_view(raw, adapters.CHANNEL_CATALOG_FORMAT)
    assert view is not raw
    assert view["supported_parameters"] is not raw["supported_parameters"]
    assert view["architecture"]["input_modalities"] is not raw["input_modalities"]
    view["supported_parameters"].append("invented")
    view["architecture"]["input_modalities"].append("audio")
    assert raw == before


def test_felo_metadata_view_keeps_missing_and_malformed_fields_distinct():
    missing = adapters.metadata_view({"id": "m"}, adapters.CHANNEL_CATALOG_FORMAT)
    assert missing == {}
    malformed = adapters.metadata_view(
        {"id": "m", "context_length": "128k",
         "input_modalities": "text", "output_modalities": None},
        adapters.CHANNEL_CATALOG_FORMAT)
    assert malformed["context_length"] == "128k"
    assert malformed["architecture"] == {"input_modalities": "text",
                                         "output_modalities": None}


# ---------- envelope: data.models + strict data.total ----------

@pytest.mark.parametrize("payload", [
    {"data": {"models": []}},                            # total missing
    {"data": {"total": None, "models": []}},             # null
    {"data": {"total": True, "models": []}},             # bool is not int
    {"data": {"total": False, "models": []}},
    {"data": {"total": -1, "models": []}},               # negative
    {"data": {"total": 1.0, "models": []}},              # not an integer
    {"data": {"total": "0", "models": []}},              # not an integer
    {"data": {"total": 1, "models": []}},                # mismatch
    {"data": {"total": 0, "models": [{}]}},              # mismatch
    {"data": {"total": 2, "models": [{"id": "a"}]}},     # truncated
    {"data": {"total": 1, "models": {"id": "a"}}},       # wrong container
    {"data": {"total": 0, "models": None}},
    {"total": 0, "models": []},                          # no data
    {"data": []},                                        # data not an object
    [],                                                  # not an envelope
    {"data": {"total": 1, "models": [1]}},
    {"data": {"total": 1, "models": ["a"]}},
    {"data": {"total": 1, "models": [{"id": ""}]}},
    {"data": {"total": 1, "models": [{"id": 7}]}},
    {"data": {"total": 1, "models": [{"id": None}]}},
])
def test_felo_malformed_envelope_or_identity_is_a_structural_fault(payload):
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(json.dumps(payload),
                               adapters.CHANNEL_CATALOG_FORMAT)


@pytest.mark.parametrize("payload", [
    {"success": False, "data": {"total": 0, "models": []}},
    {"success": False, "data": {"total": 1, "models": [{"id": "a"}]}},
    {"success": False},
])
def test_felo_explicit_success_false_envelope_is_a_structural_fault(payload):
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(json.dumps(payload),
                               adapters.CHANNEL_CATALOG_FORMAT)


def test_felo_valid_empty_and_paid_only_catalogues_are_successes():
    assert adapters.parse_catalog(_felo_body(),
                                  adapters.CHANNEL_CATALOG_FORMAT) == []
    paid = adapters.parse_catalog(
        _felo_body(_felo_row(_felo_channel(prompt="0.000001"),
                             mid="paid/model")),
        adapters.CHANNEL_CATALOG_FORMAT)
    assert len(paid) == 1
    assert adapters.is_free_row(paid[0],
                                adapters.CHANNEL_CATALOG_FORMAT) is False


def test_felo_conflicting_duplicate_ids_raise_but_equivalent_ones_collapse():
    free = _felo_row(_felo_channel(), mid="dup/model")
    paid = _felo_row(_felo_channel(prompt="0.1"), mid="dup/model")
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(_felo_body(free, paid),
                               adapters.CHANNEL_CATALOG_FORMAT)
    rows = adapters.parse_catalog(_felo_body(free, copy.deepcopy(free)),
                                  adapters.CHANNEL_CATALOG_FORMAT)
    assert [row["id"] for row in rows] == ["dup/model"]
    assert adapters.is_free_row(rows[0],
                                adapters.CHANNEL_CATALOG_FORMAT) is True


# ---------- duplicate equivalence uses exact source numeric semantics ----------
#
# `json.loads` turns a JSON number such as 1e-400 into 0.0 before any ordinary
# dict comparison. These cases go through the raw body text: every token below
# is an unquoted JSON number or boolean, never a Python float or a string.

def _felo_raw_record(prompt="0", deprecated="false", runnable="true"):
    return ('{"id":"dup/x","deprecated":%s,"model_kind":"chat",'
            '"output_modalities":["text"],"channels":[{"runnable":%s,'
            '"pricing_mode":"configured_rates","discount_multiplier":"1",'
            '"standard_pricing":{"billing_kind":"token","prompt":%s,'
            '"completion":0,"tiers":[]}}]}' % (deprecated, runnable, prompt))


def _felo_raw_body(*records):
    return '{"data":{"total":%d,"models":[%s]}}' % (
        len(records), ",".join(records))


def test_felo_raw_duplicate_body_carries_an_underflowing_numeric_token():
    body = _felo_raw_body(_felo_raw_record("1e-400"))
    assert '"prompt":1e-400' in body and '"1e-400"' not in body
    parsed = json.loads(body)["data"]["models"][0]["channels"][0][
        "standard_pricing"]["prompt"]
    assert isinstance(parsed, float) and parsed == 0.0, "raw token underflows"


FELO_NUMERIC_DUPLICATE_CONFLICTS = [
    ("0", "1e-400", "zero-vs-positive-subnormal"),
    ("1e-400", "2e-400", "distinct-positive-subnormals"),
    ("1.0000000000000000001", "1.0000000000000000002",
     "literals-rounding-to-one-float"),
]


@pytest.mark.parametrize(
    "left,right", [(a, b) for a, b, _ in FELO_NUMERIC_DUPLICATE_CONFLICTS],
    ids=[c for _, _, c in FELO_NUMERIC_DUPLICATE_CONFLICTS])
def test_felo_conflicting_numeric_duplicate_tokens_raise_in_both_orders(
        left, right):
    ordinary = []
    for token in (left, right):
        ordinary.append(json.loads(_felo_raw_body(
            _felo_raw_record(token)))["data"]["models"][0]["channels"][0][
                "standard_pricing"]["prompt"])
    assert ordinary[0] == ordinary[1], (
        "setup: ordinary float equality collapses this pair")
    for order in ((left, right), (right, left)):
        with pytest.raises(adapters.AdapterError, match="duplicate"):
            adapters.parse_catalog(
                _felo_raw_body(*[_felo_raw_record(t) for t in order]),
                adapters.CHANNEL_CATALOG_FORMAT)


def _felo_raw_tier_record(tier_completion):
    return ('{"id":"dup/x","deprecated":false,"model_kind":"chat",'
            '"output_modalities":["text"],"channels":[{"runnable":true,'
            '"pricing_mode":"configured_rates","discount_multiplier":"1",'
            '"standard_pricing":{"billing_kind":"token","prompt":0,'
            '"completion":0,"tiers":[{"conditions":[],"overrides":'
            '{"completion":%s}}]}}]}' % tier_completion)


def test_felo_nested_tier_numeric_difference_is_a_conflict_in_both_orders():
    """The exact comparator walks the whole record, not just top-level prompt."""
    ordinary = [json.loads(_felo_raw_body(_felo_raw_tier_record(token)))[
        "data"]["models"][0]["channels"][0]["standard_pricing"]["tiers"][0][
            "overrides"]["completion"] for token in ("0", "1e-400")]
    assert ordinary == [0.0, 0.0], "setup: nested literals collapse under float"
    for order in (("0", "1e-400"), ("1e-400", "0")):
        with pytest.raises(adapters.AdapterError, match="duplicate"):
            adapters.parse_catalog(
                _felo_raw_body(*[_felo_raw_tier_record(t) for t in order]),
                adapters.CHANNEL_CATALOG_FORMAT)


FELO_BOOLEAN_DUPLICATE_CONFLICTS = [
    ("deprecated", "false", "0", "deprecated-false-vs-zero"),
    ("runnable", "true", "1", "runnable-true-vs-one"),
]


@pytest.mark.parametrize(
    "field,left,right",
    [(f, a, b) for f, a, b, _ in FELO_BOOLEAN_DUPLICATE_CONFLICTS],
    ids=[c for _, _, _, c in FELO_BOOLEAN_DUPLICATE_CONFLICTS])
def test_felo_conflicting_boolean_numeric_duplicate_tokens_raise_in_both_orders(
        field, left, right):
    def record(token):
        return _felo_raw_record(
            "0",
            deprecated=token if field == "deprecated" else "false",
            runnable=token if field == "runnable" else "true")

    def observed(token):
        row = json.loads(_felo_raw_body(record(token)))["data"]["models"][0]
        if field == "runnable":
            return row["channels"][0]["runnable"]
        return row[field]

    ordinary = [observed(token) for token in (left, right)]
    assert ordinary[0] == ordinary[1], (
        "setup: ordinary equality collapses a strict boolean with 0/1")
    for order in ((left, right), (right, left)):
        with pytest.raises(adapters.AdapterError, match="duplicate"):
            adapters.parse_catalog(
                _felo_raw_body(*[record(t) for t in order]),
                adapters.CHANNEL_CATALOG_FORMAT)


def test_felo_numerically_equivalent_duplicate_tokens_still_collapse():
    zero = adapters.parse_catalog(
        _felo_raw_body(_felo_raw_record("0"), _felo_raw_record("0.0")),
        adapters.CHANNEL_CATALOG_FORMAT)
    assert [row["id"] for row in zero] == ["dup/x"]
    assert adapters.is_free_row(zero[0],
                                adapters.CHANNEL_CATALOG_FORMAT) is True
    tiny = adapters.parse_catalog(
        _felo_raw_body(_felo_raw_record("1e-400"),
                       _felo_raw_record("1.0e-400")),
        adapters.CHANNEL_CATALOG_FORMAT)
    assert [row["id"] for row in tiny] == ["dup/x"]
    assert adapters.is_free_row(tiny[0],
                                adapters.CHANNEL_CATALOG_FORMAT) is False


def test_felo_string_and_numeric_duplicate_tokens_remain_distinct():
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters.parse_catalog(
            _felo_raw_body(_felo_raw_record('"0"'), _felo_raw_record("0")),
            adapters.CHANNEL_CATALOG_FORMAT)


def _vercel_raw_input_row(token):
    return ('{"id":"dup/v","type":"language",'
            '"pricing":{"input":%s,"output":"0"}}' % token)


def _vercel_raw_input_body(*tokens):
    return '{"data":[%s]}' % ",".join(
        _vercel_raw_input_row(token) for token in tokens)


def test_vercel_boolean_and_zero_input_collapse_under_ordinary_equality():
    left, right = json.loads(_vercel_raw_input_body("0", "false"))["data"]
    assert left["pricing"]["input"] == right["pricing"]["input"]
    assert isinstance(right["pricing"]["input"], bool)


@pytest.mark.parametrize("order", [("0", "false"), ("false", "0")])
def test_vercel_conflicting_boolean_numeric_duplicate_raises_in_both_orders(
        order):
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters.parse_catalog(_vercel_raw_input_body(*order),
                               adapters.INPUT_OUTPUT_FORMAT)


def test_vercel_numerically_equivalent_numeric_duplicate_still_collapses():
    rows = adapters.parse_catalog(_vercel_raw_input_body("0", "0.0"),
                                  adapters.INPUT_OUTPUT_FORMAT)
    assert [row["id"] for row in rows] == ["dup/v"]
    assert adapters.is_free_row(rows[0],
                                adapters.INPUT_OUTPUT_FORMAT) is True


def test_felo_literal_ids_are_preserved():
    ids = ["  Vendor/Padded  ", "vendor/Ünicode-Δ", " Vendor/Case"]
    rows = adapters.parse_catalog(
        _felo_body(*[_felo_row(_felo_channel(), mid=mid) for mid in ids]),
        adapters.CHANNEL_CATALOG_FORMAT)
    assert [row["id"] for row in rows] == ids
    assert all(adapters.is_free_row(row, adapters.CHANNEL_CATALOG_FORMAT)
               for row in rows)


def test_felo_tiny_json_number_price_does_not_underflow_to_free():
    """A JSON-number 1e-400 must keep its exact value through the parser.

    ``json.loads`` alone turns that literal into ``0.0``, which would admit a
    paid row as free. The channel-catalog parser keeps the source literal so
    the exact Decimal rule can reject it.
    """
    body = (
        '{"data":{"total":1,"models":[{"id":"v/tiny","deprecated":false,'
        '"model_kind":"chat","output_modalities":["text"],"channels":['
        '{"runnable":true,"pricing_mode":"configured_rates",'
        '"discount_multiplier":"1","standard_pricing":{"billing_kind":"token",'
        '"prompt":1e-400,"completion":0,"tiers":[]}}]}]}}')
    rows = adapters.parse_catalog(body, adapters.CHANNEL_CATALOG_FORMAT)
    assert adapters.is_free_row(rows[0],
                                adapters.CHANNEL_CATALOG_FORMAT) is False


def test_felo_zero_times_an_arbitrarily_small_multiplier_is_exact_zero():
    assert _felo_free(_felo_channel(prompt="1e-400", completion="1e-400",
                                    discount="0")) is True
    assert _felo_free(_felo_channel(prompt="1e-400", completion="0",
                                    discount="1e-400")) is False


# ---------- scope: retired / kind / text modality ----------

@pytest.mark.parametrize("deprecated", [_SENTINEL, True, "false", 0, None])
def test_felo_deprecated_must_be_exactly_false(deprecated):
    row = _felo_row(_felo_channel())
    if deprecated is _SENTINEL:
        del row["deprecated"]
    else:
        row["deprecated"] = deprecated
    assert adapters.is_free_row(row, adapters.CHANNEL_CATALOG_FORMAT) is False


@pytest.mark.parametrize("kind", ["image_generation", "video_generation",
                                  "decisions", "audio_speech", "chat ",
                                  None, "CHAT"])
def test_felo_non_chat_kinds_are_out_of_scope(kind):
    row = _felo_row(_felo_channel())
    row["model_kind"] = kind
    assert adapters.is_free_row(row, adapters.CHANNEL_CATALOG_FORMAT) is False


@pytest.mark.parametrize("outputs,expected", [
    (["text"], True),
    (["text", "image"], True),
    (["image", "text"], True),
    (["video"], False),
    (["speech"], False),
    ([], False),
    ("text", False),        # a substring test would wrongly admit this
    (None, False),
])
def test_felo_requires_text_in_the_published_modality_list(outputs, expected):
    row = _felo_row(_felo_channel())
    row["output_modalities"] = outputs
    assert adapters.is_free_row(row, adapters.CHANNEL_CATALOG_FORMAT) is expected


def test_felo_id_suffix_or_marketing_name_is_not_evidence():
    assert _felo_free(_felo_channel(), mid="vendor/plain-id") is True
    assert _felo_free(_felo_channel(prompt="1"), mid="vendor/totally:free") is False


# ---------- channels: runnable, pricing mode, every runnable branch ----------

@pytest.mark.parametrize("channels", [[], [1], None, "chan", {}])
def test_felo_missing_or_malformed_channel_container(channels):
    row = _felo_row(_felo_channel())
    row["channels"] = channels
    if channels is not None and not isinstance(channels, list):
        with pytest.raises(adapters.AdapterError):
            adapters.is_free_row(row, adapters.CHANNEL_CATALOG_FORMAT)
    elif channels and not isinstance(channels[0], dict):
        with pytest.raises(adapters.AdapterError):
            adapters.is_free_row(row, adapters.CHANNEL_CATALOG_FORMAT)
    else:
        assert adapters.is_free_row(row, adapters.CHANNEL_CATALOG_FORMAT) is False


def test_felo_requires_at_least_one_runnable_channel():
    assert _felo_free(_felo_channel(runnable=False)) is False
    assert _felo_free(_felo_channel(runnable="true")) is False
    assert _felo_free(_felo_channel(runnable=1)) is False


def test_felo_free_primary_plus_paid_runnable_fallback_is_not_free():
    """ALL runnable channels must be zero, not merely one of them."""
    assert _felo_free(_felo_channel(),
                      _felo_channel(prompt="0.000002", code="paid")) is False


def test_felo_free_non_runnable_channel_cannot_rescue_a_paid_route():
    assert _felo_free(_felo_channel(prompt="1"),
                      _felo_channel(runnable=False)) is False


def test_felo_non_runnable_paid_channel_does_not_disqualify_a_free_route():
    assert _felo_free(_felo_channel(),
                      _felo_channel(prompt="1", runnable=False)) is True


def test_felo_all_zero_runnable_channels_are_free():
    assert _felo_free(_felo_channel(), _felo_channel(completion="0.0")) is True


def test_felo_runnable_upstream_cost_channel_disqualifies():
    assert _felo_free(_felo_channel(),
                      _felo_channel(pricing_mode="upstream_cost")) is False
    assert _felo_free(_felo_channel(pricing_mode="upstream_cost")) is False


@pytest.mark.parametrize("sp", [_SENTINEL, None, "free", []])
def test_felo_null_or_malformed_standard_pricing(sp):
    channel = _felo_channel()
    if sp is _SENTINEL:
        del channel["standard_pricing"]
        assert _felo_free(channel) is False
    else:
        channel["standard_pricing"] = sp
        if sp is None:
            assert _felo_free(channel) is False
        else:
            with pytest.raises(adapters.AdapterError):
                _felo_free(channel)


@pytest.mark.parametrize("billing", [None, "request", "", 0, True])
def test_felo_unknown_billing_kind_is_nonqualifying(billing):
    assert _felo_free(_felo_channel(billing=billing)) is False


# ---------- token billing: prompt/completion/request ----------

@pytest.mark.parametrize("zero", ["0", "0.0", "0.00", "0e5", 0, 0.0])
def test_felo_explicit_zero_token_prices_are_free(zero):
    assert _felo_free(_felo_channel(prompt=zero, completion=zero)) is True


@pytest.mark.parametrize("prompt,completion", [
    (_SENTINEL, "0"), ("0", _SENTINEL), (None, "0"), ("0", None),
    ("0.0000001", "0"), ("0", "0.0000001"), ("1e-400", "0"),
])
def test_felo_missing_null_or_positive_token_rate_is_not_free(prompt, completion):
    assert _felo_free(_felo_channel(prompt=prompt,
                                    completion=completion)) is False


@pytest.mark.parametrize("bad", [
    True, False, -1, -0.5, -1e-9, float("nan"), float("inf"),
    float("-inf"), [], {}, "", " ", " 0", "0 ", "abc", "1e", "NaN",
    "Infinity", "-Infinity",
])
@pytest.mark.parametrize("slot", ["prompt", "completion", "request"])
def test_felo_malformed_token_price_values_raise(bad, slot):
    channel = _felo_channel()
    channel["standard_pricing"][slot] = bad
    with pytest.raises(adapters.AdapterError):
        _felo_free(channel)


@pytest.mark.parametrize("fee", [_SENTINEL, None, "0", 0])
def test_felo_absent_or_zero_token_request_fee_is_allowed(fee):
    assert _felo_free(_felo_channel(request=fee)) is True


@pytest.mark.parametrize("fee", ["0.000001", "1", 0.5, "1e-400"])
def test_felo_positive_token_request_fee_disqualifies(fee):
    assert _felo_free(_felo_channel(request=fee)) is False


def test_felo_positive_request_made_zero_by_discount_still_qualifies():
    assert _felo_free(_felo_channel(request="0.01",
                                    discount="0")) is True


# ---------- discount_multiplier arithmetic ----------

def test_felo_positive_standard_price_with_explicit_zero_discount_is_free():
    assert _felo_free(_felo_channel(prompt="0.000002",
                                    completion="0.000006",
                                    discount="0")) is True
    assert _felo_free(_felo_channel(prompt="0.000002",
                                    completion="0.000006",
                                    discount="0.0")) is True


@pytest.mark.parametrize("mult", ["1", "0.8", "0.35", "1e-3", 1, 0.5])
def test_felo_positive_multiplier_over_a_positive_rate_is_not_free(mult):
    assert _felo_free(_felo_channel(prompt="0.0005",
                                    discount=mult)) is False


@pytest.mark.parametrize("mult", [None, _SENTINEL])
def test_felo_unknown_multiplier_on_a_positive_rate_cannot_establish_zero(mult):
    channel = _felo_channel(prompt="0.0005")
    if mult is _SENTINEL:
        del channel["discount_multiplier"]
    else:
        channel["discount_multiplier"] = mult
    assert _felo_free(channel) is False


@pytest.mark.parametrize("mult", [None, _SENTINEL, "0.5", "0"])
def test_felo_an_explicit_zero_rate_is_zero_whatever_the_multiplier(mult):
    channel = _felo_channel()
    if mult is _SENTINEL:
        del channel["discount_multiplier"]
    else:
        channel["discount_multiplier"] = mult
    assert _felo_free(channel) is True


@pytest.mark.parametrize("bad", [
    True, False, -1, -0.5, float("nan"), float("inf"), [], {}, "", " ", " 1",
    "1 ", "abc", "1e",
])
def test_felo_malformed_discount_multiplier_raises(bad):
    with pytest.raises(adapters.AdapterError):
        _felo_free(_felo_channel(prompt="0.1", discount=bad))


def test_felo_markup_multiplier_never_changes_a_proven_zero_product():
    # The documented discount is applied once; markup >= 1 keeps zero zero.
    assert _felo_free(_felo_channel(prompt="0.1", discount="0",
                                    markup_multiplier="2")) is True
    assert _felo_free(_felo_channel(prompt="0.1", discount="0.5",
                                    markup_multiplier="2")) is False


# ---------- call billing ----------

def test_felo_call_billing_requires_an_explicit_zero_request_rate():
    assert _felo_free(_felo_channel(billing="call", prompt=_SENTINEL,
                                    completion=_SENTINEL,
                                    request="0")) is True
    assert _felo_free(_felo_channel(billing="call", prompt=_SENTINEL,
                                    completion=_SENTINEL,
                                    request=None)) is False
    assert _felo_free(_felo_channel(billing="call", prompt=_SENTINEL,
                                    completion=_SENTINEL,
                                    request=_SENTINEL)) is False
    assert _felo_free(_felo_channel(billing="call", prompt=_SENTINEL,
                                    completion=_SENTINEL,
                                    request="0.01")) is False


def test_felo_call_billing_zero_request_with_zero_discount_is_free():
    assert _felo_free(_felo_channel(billing="call", prompt=_SENTINEL,
                                    completion=_SENTINEL, request="0.5",
                                    discount="0")) is True


def test_felo_call_billing_does_not_read_token_rates():
    # A per-call row is charged per request; a token field is not the charge.
    assert _felo_free(_felo_channel(billing="call", prompt="9",
                                    completion="9", request="0")) is True


# ---------- standard_pricing.tiers: every applicable branch ----------

def test_felo_absent_or_empty_tiers_are_allowed():
    assert _felo_free(_felo_channel(tiers=[])) is True
    assert _felo_free(_felo_channel(tiers=None)) is True
    assert _felo_free(_felo_channel(tiers=_SENTINEL)) is True


def test_felo_zero_base_with_a_positive_tier_is_not_free():
    tier = _felo_tier({"prompt": "0.000002"},
                      [{"field": "input_context_tokens",
                        "operator": "gt", "value": 100000}])
    assert _felo_free(_felo_channel(tiers=[tier])) is False


def test_felo_all_zero_tier_is_free():
    tier = _felo_tier({"prompt": "0", "completion": "0.0",
                       "request": None})
    assert _felo_free(_felo_channel(tiers=[tier])) is True


def test_felo_positive_tier_made_zero_by_zero_discount_is_free():
    tier = _felo_tier({"prompt": "0.000002",
                       "completion": "0.000006"})
    assert _felo_free(_felo_channel(tiers=[tier], discount="0")) is True


def test_felo_tier_inherits_an_omitted_axis_from_the_base():
    # Base is zero; a tier naming only completion inherits the zero prompt.
    assert _felo_free(_felo_channel(
        tiers=[_felo_tier({"completion": "0"})])) is True
    assert _felo_free(_felo_channel(
        tiers=[_felo_tier({"completion": "0.000001"})])) is False
    # A positive base can never be rescued by a zero-only tier fragment.
    assert _felo_free(_felo_channel(prompt="0.000001",
                                    tiers=[_felo_tier({"prompt": "0"})])) is False


def test_felo_tier_request_axis_is_checked():
    assert _felo_free(_felo_channel(
        tiers=[_felo_tier({"request": "0.001"})])) is False
    assert _felo_free(_felo_channel(
        tiers=[_felo_tier({"request": "0"})])) is True


def test_felo_every_tier_branch_is_checked_not_just_the_first():
    tiers = [_felo_tier({"prompt": "0"}),
             _felo_tier({"prompt": "0"},
                        [{"field": "input_context_tokens",
                          "operator": "gt", "value": 1000000}])]
    assert _felo_free(_felo_channel(tiers=tiers)) is True
    tiers[1]["overrides"]["prompt"] = "0.000001"
    assert _felo_free(_felo_channel(tiers=tiers)) is False


@pytest.mark.parametrize("tiers", ["0", {}, 1, [1], [None], ["tier"]])
def test_felo_malformed_tier_container_raises(tiers):
    with pytest.raises(adapters.AdapterError):
        _felo_free(_felo_channel(tiers=tiers))


@pytest.mark.parametrize("tier", [
    {},
    {"conditions": []},
    {"overrides": None},
    {"conditions": [], "overrides": {}, "label": "unknown-shape"},
])
def test_felo_unknown_tier_shape_is_nonqualifying(tier):
    assert _felo_free(_felo_channel(tiers=[tier])) is False


@pytest.mark.parametrize("tier", [
    {"overrides": "cheap"},
    {"conditions": "threshold", "overrides": {}},
    {"conditions": [1], "overrides": {}},
])
def test_felo_malformed_tier_shape_raises(tier):
    with pytest.raises(adapters.AdapterError):
        _felo_free(_felo_channel(tiers=[tier]))


@pytest.mark.parametrize("overrides", [
    {"prompt": True}, {"completion": "abc"}, {"request": []},
])
def test_felo_malformed_tier_price_value_raises(overrides):
    with pytest.raises(adapters.AdapterError):
        _felo_free(_felo_channel(tiers=[_felo_tier(overrides)]))


def test_felo_conditions_are_not_evaluated_only_every_branch_is_checked():
    tier = _felo_tier({"prompt": "0.000002"},
                      [{"field": "unusual_future_field",
                        "operator": "eq", "value": "whatever"}])
    assert _felo_free(_felo_channel(tiers=[tier])) is False


# ---------- optional cache/tool charges stay optional ----------

@pytest.mark.parametrize("extra", [
    {"input_cache_write": "0.00000125"},
    {"input_cache_read": "0.0000001"},
    {"web_search": "0.01"},
    {"input_image": "0.02"},
    {"audio": "1"},
])
def test_felo_optional_charge_axes_do_not_create_or_destroy_chat_admission(extra):
    channel = _felo_channel()
    channel["standard_pricing"].update(extra)
    assert _felo_free(channel) is True
    channel["standard_pricing"]["prompt"] = "0.000001"
    assert _felo_free(channel) is False


def test_felo_tier_optional_charge_axes_are_not_ordinary_rates():
    tier = _felo_tier({"input_cache_write": "0.5", "web_search": "9"})
    assert _felo_free(_felo_channel(tiers=[tier])) is True


# ---------- synthetic TokenRA pricing subset (P3, newapi-pricing) ----------
#
# TokenRA publishes `model_name` and NO `id`, prices are per-token ratios, and
# the same `openai` endpoint label is used by video rows. Small hand-authored
# rows cover both JSON integer and float prices plus the counterexample
# families (per-call image, video tag, decisions endpoint, paid ratio).

_TOKENRA = adapters.NEWAPI_PRICING_FORMAT


def _tokenra_row(model_name="vendor/chat", **extra):
    row = {
        "model_name": model_name,
        "quota_type": 0,
        "model_ratio": 0,
        "model_price": 0,
        "enable_groups": ["default"],
        "supported_endpoint_types": ["openai"],
    }
    row.update(extra)
    return row


def _tokenra_free(row):
    return adapters.is_free_row(row, _TOKENRA)


def _tokenra_body(*rows, **envelope):
    payload = {"success": True, "data": list(rows)}
    payload.update(envelope)
    return json.dumps(payload)


TOKENRA_FREE_IDS = ["vendor/chat-plain", "vendor/chat-zero-prices"]
TOKENRA_ROWS = {
    "vendor/chat-plain": _tokenra_row("vendor/chat-plain"),
    "vendor/chat-zero-prices": _tokenra_row(
        "vendor/chat-zero-prices", model_ratio=0.0, model_price=0.0,
        tags="glm"),
    "vendor/image-call": _tokenra_row(
        "vendor/image-call", quota_type=1, model_price=0.012),
    "vendor/video-tagged": _tokenra_row(
        "vendor/video-tagged", tags="video",
        video_resolution_prices={"720p": 0}),
    "vendor/decisions-only": _tokenra_row(
        "vendor/decisions-only",
        supported_endpoint_types=["typesafe-decisions"]),
    "vendor/paid-ratio": _tokenra_row(
        "vendor/paid-ratio", model_ratio=0.0000001, completion_ratio=5),
    "vendor/paid-ratio-free:free": _tokenra_row(
        "vendor/paid-ratio-free:free", model_ratio=3.261),
}
TOKENRA_TEXT = _tokenra_body(*TOKENRA_ROWS.values())
TOKENRA_SUBSET = json.loads(TOKENRA_TEXT)


def test_synthetic_tokenra_subset_parses_to_the_original_rows():
    rows = adapters.parse_catalog(TOKENRA_TEXT, _TOKENRA)
    assert [row["model_name"] for row in rows] == [
        row["model_name"] for row in TOKENRA_SUBSET["data"]]
    for row in rows:
        assert row == TOKENRA_ROWS[row["model_name"]]
        assert "id" not in row, "the adapter must never insert an id"
    assert rows[0] is not TOKENRA_SUBSET["data"][0]
    # The synthetic body publishes BOTH JSON integer and float prices; the
    # predicate must handle each without re-rendering either. (A parsed
    # non-integer is a float subclass that keeps its source literal, hence
    # isinstance.)
    ratios = [row["model_ratio"] for row in rows]
    prices = [row["model_price"] for row in rows]
    assert any(type(value) is int for value in ratios), "integer ratio present"
    assert any(isinstance(value, float) for value in ratios), "float ratio present"
    assert any(type(value) is int for value in prices), "integer price present"
    assert any(isinstance(value, float) for value in prices), "float price present"


def test_synthetic_tokenra_subset_reproduces_the_free_ids():
    rows = adapters.parse_catalog(TOKENRA_TEXT, _TOKENRA)
    assert sorted(row["model_name"] for row in rows
                  if _tokenra_free(row)) == TOKENRA_FREE_IDS


@pytest.mark.parametrize("model_name", TOKENRA_FREE_IDS)
def test_each_tokenra_free_id_is_admitted(model_name):
    assert _tokenra_free(TOKENRA_ROWS[model_name]) is True


@pytest.mark.parametrize("model_name,reason", [
    ("vendor/image-call", "quota_type 1 per-call image, positive model_price"),
    ("vendor/video-tagged", "video tag on a zero-priced row"),
    ("vendor/decisions-only", "typesafe-decisions endpoint, not openai"),
    ("vendor/paid-ratio", "paid token ratio with completion_ratio 5"),
    ("vendor/paid-ratio-free:free", "paid ratio despite a ':free' id"),
])
def test_tokenra_synthetic_counterexamples_are_not_admitted(model_name, reason):
    assert model_name in TOKENRA_ROWS, reason
    assert _tokenra_free(TOKENRA_ROWS[model_name]) is False, reason


def test_tokenra_every_synthetic_row_but_the_two_positives_is_rejected():
    """Exhaustive over the synthetic subset: no other row qualifies."""
    admitted = [name for name, row in TOKENRA_ROWS.items()
                if _tokenra_free(row)]
    assert sorted(admitted) == TOKENRA_FREE_IDS
    assert len(TOKENRA_ROWS) - len(TOKENRA_FREE_IDS) == 5


# ---------- identity seam: model_name, never id ----------

def test_tokenra_identity_field_is_model_name_not_id():
    assert adapters.identity_field(_TOKENRA) == "model_name"
    assert adapters.identity_field(adapters.INPUT_OUTPUT_FORMAT) == "id"
    assert adapters.identity_field(adapters.LEGACY_FORMAT) is None
    row = _tokenra_row("real/name")
    row["id"] = "invented-id"
    assert adapters.selected_identity(row, _TOKENRA) == "real/name"
    assert adapters.selected_identity({"id": 7}, adapters.LEGACY_FORMAT) == 7


def test_tokenra_literal_identities_are_preserved():
    names = ["  Vendor/Padded  ", "vendor/\u00dcnicode-\u0394", "MixedCase"]
    body = _tokenra_body(*[_tokenra_row(name) for name in names])
    rows = adapters.parse_catalog(body, _TOKENRA)
    assert [row["model_name"] for row in rows] == names
    assert [row["model_name"] for row in rows if _tokenra_free(row)] == names


# ---------- envelope, success semantics and duplicates ----------

@pytest.mark.parametrize("body", [
    "[]",
    '{"data": {}}',
    '{"data": null}',
    '{}',
    '{"success": true, "data": ["not-a-row"]}',
    '{"data": [{"quota_type": 0}]}',
    '{"data": [{"model_name": "", "quota_type": 0}]}',
    '{"data": [{"model_name": 7, "quota_type": 0}]}',
    "not json",
])
def test_tokenra_malformed_envelope_or_identity_raises(body):
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(body, _TOKENRA)


def test_tokenra_explicit_success_false_is_a_structural_fault():
    """A failed upstream response is never a healthy empty observation."""
    for body in ('{"success": false, "data": []}',
                 '{"success": false, "data": [%s]}'
                 % json.dumps(_tokenra_row())):
        with pytest.raises(adapters.AdapterError):
            adapters.parse_catalog(body, _TOKENRA)


@pytest.mark.parametrize("envelope", [
    {"unused": 1},
    {"success": True},
    {"success": 0},
    {"success": "false"},
    {"success": None},
])
def test_tokenra_absent_true_or_non_boolean_success_still_parses(envelope):
    body = _tokenra_body(_tokenra_row(), **envelope)
    rows = adapters.parse_catalog(body, _TOKENRA)
    assert [row["model_name"] for row in rows] == ["vendor/chat"]


def test_tokenra_valid_empty_and_paid_only_catalogues_are_successes():
    assert adapters.parse_catalog('{"success": true, "data": []}',
                                  _TOKENRA) == []
    paid_only = _tokenra_body(
        _tokenra_row("image/call", quota_type=1, model_price=0.012))
    rows = adapters.parse_catalog(paid_only, _TOKENRA)
    assert [row["model_name"] for row in rows] == ["image/call"]
    assert not [row for row in rows if _tokenra_free(row)]


def test_tokenra_conflicting_duplicate_ids_raise_in_both_orders():
    for first, second in ((0, 1), (1, 0)):
        body = _tokenra_body(
            _tokenra_row("dup/model", model_ratio=first),
            _tokenra_row("dup/model", model_ratio=second))
        with pytest.raises(adapters.AdapterError):
            adapters.parse_catalog(body, _TOKENRA)


def test_tokenra_conflicting_duplicate_is_keyed_by_model_name_not_id():
    """One model_name with invented ids is still ONE identity."""
    body = _tokenra_body(
        dict(_tokenra_row("dup/model"), id="a"),
        dict(_tokenra_row("dup/model", model_price=0.5), id="b"))
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(body, _TOKENRA)


def test_tokenra_equivalent_duplicate_records_collapse():
    row = _tokenra_row("dup/model")
    rows = adapters.parse_catalog(_tokenra_body(row, dict(row)), _TOKENRA)
    assert [item["model_name"] for item in rows] == ["dup/model"]


def test_tokenra_numerically_equivalent_duplicates_still_collapse():
    """Raw `0` and raw `0.0` are equivalent JSON numbers, not a conflict."""
    body = ('{"success": true, "data": ['
            '{"model_name": "dup/x", "quota_type": 0, "model_ratio": 0, '
            '"model_price": 0, "enable_groups": ["default"], '
            '"supported_endpoint_types": ["openai"]}, '
            '{"model_name": "dup/x", "quota_type": 0, "model_ratio": 0.0, '
            '"model_price": 0.0, "enable_groups": ["default"], '
            '"supported_endpoint_types": ["openai"]}]}')
    rows = adapters.parse_catalog(body, _TOKENRA)
    assert len(rows) == 1


# ---------- numeric underflow is not free ----------
# The raw JSON tokens below are unquoted so the regression is the PARSER's:
# json.loads turns 1e-400 into 0.0 before any predicate can look at it.

_TOKENRA_TINY_RATIO = (
    '{"success": true, "data": [{"model_name": "tiny/x", "quota_type": 0, '
    '"model_ratio": 1e-400, "model_price": 0, "enable_groups": ["default"], '
    '"supported_endpoint_types": ["openai"]}]}')

_TOKENRA_TINY_PRICE = (
    '{"success": true, "data": [{"model_name": "tiny/call", "quota_type": 0, '
    '"model_ratio": 0, "model_price": 1e-400, "enable_groups": ["default"], '
    '"supported_endpoint_types": ["openai"]}]}')


def test_tokenra_tiny_json_ratio_does_not_underflow_to_free():
    assert json.loads(_TOKENRA_TINY_RATIO)["data"][0]["model_ratio"] == 0.0
    rows = adapters.parse_catalog(_TOKENRA_TINY_RATIO, _TOKENRA)
    assert rows[0]["model_ratio"] == 0.0
    assert _tokenra_free(rows[0]) is False


def test_tokenra_tiny_json_call_price_does_not_underflow_to_free():
    assert json.loads(_TOKENRA_TINY_PRICE)["data"][0]["model_price"] == 0.0
    rows = adapters.parse_catalog(_TOKENRA_TINY_PRICE, _TOKENRA)
    assert _tokenra_free(rows[0]) is False


# ---------- billing mode and required rates ----------

@pytest.mark.parametrize("quota_type", [True, False, "0", 0.0, [], {}])
def test_tokenra_malformed_quota_type_raises(quota_type):
    with pytest.raises(adapters.AdapterError):
        _tokenra_free(_tokenra_row(quota_type=quota_type))


@pytest.mark.parametrize("quota_type", [1, 2, -1])
def test_tokenra_nonzero_quota_type_is_nonqualifying(quota_type):
    assert _tokenra_free(_tokenra_row(quota_type=quota_type)) is False


@pytest.mark.parametrize("member", ["quota_type", "model_ratio", "model_price"])
def test_tokenra_missing_or_null_required_member_is_unknown_not_zero(member):
    absent = _tokenra_row()
    absent.pop(member)
    assert _tokenra_free(absent) is False
    present_null = _tokenra_row()
    present_null[member] = None
    assert _tokenra_free(present_null) is False


@pytest.mark.parametrize("member", ["model_ratio", "model_price"])
@pytest.mark.parametrize("bad", [
    True, False, "0", "1e-400", [], {}, -1, -0.5, float("inf"),
    float("nan"),
])
def test_tokenra_malformed_required_rate_raises(member, bad):
    with pytest.raises(adapters.AdapterError):
        _tokenra_free(_tokenra_row(**{member: bad}))


@pytest.mark.parametrize("zero", [0, 0.0])
def test_tokenra_explicit_zero_spellings_are_free(zero):
    assert _tokenra_free(
        _tokenra_row(model_ratio=zero, model_price=zero)) is True


@pytest.mark.parametrize("extra", [
    {"model_ratio": 0.0000001},
    {"model_ratio": 3.261},
    {"model_price": 0.012},
    {"model_price": 0.000001},
])
def test_tokenra_positive_or_tiny_charges_are_not_free(extra):
    assert _tokenra_free(_tokenra_row(**extra)) is False


@pytest.mark.parametrize("extra", [
    {"completion_ratio": 5},
    {"completion_ratio": 1.5, "cache_ratio": 0.5},
    {"cache_ratio": 3},
    {"create_cache_ratio": 1},
    {"audio_ratio": 2},
    {"completion_ratio": 1, "cache_ratio": 0, "audio_ratio": 0},
])
def test_tokenra_positive_multipliers_over_a_zero_base_stay_free(extra):
    assert _tokenra_free(_tokenra_row(**extra)) is True


def test_tokenra_multipliers_cannot_rescue_a_positive_base():
    assert _tokenra_free(
        _tokenra_row(model_ratio=0.5, completion_ratio=0)) is False


# ---------- public group and endpoint gating ----------

@pytest.mark.parametrize("groups,expected", [
    (["default"], True),
    (["vip", "default"], True),
    (["vip"], False),
    ([], False),
    (None, False),
])
def test_tokenra_default_public_group_membership(groups, expected):
    assert _tokenra_free(_tokenra_row(enable_groups=groups)) is expected


@pytest.mark.parametrize("groups", ["default", {"default": True}, [1], [""]])
def test_tokenra_malformed_group_container_raises(groups):
    with pytest.raises(adapters.AdapterError):
        _tokenra_free(_tokenra_row(enable_groups=groups))


@pytest.mark.parametrize("endpoints,expected", [
    (["openai"], True),
    (["openai", "typesafe-decisions"], True),
    (["typesafe-decisions"], False),
    ([], False),
    (None, False),
])
def test_tokenra_openai_endpoint_membership(endpoints, expected):
    assert _tokenra_free(
        _tokenra_row(supported_endpoint_types=endpoints)) is expected


@pytest.mark.parametrize("endpoints", ["openai", {"openai": 1}, [1]])
def test_tokenra_malformed_endpoint_container_raises(endpoints):
    with pytest.raises(adapters.AdapterError):
        _tokenra_free(_tokenra_row(supported_endpoint_types=endpoints))


# ---------- video category and the flat per-resolution charge map ----------

def test_tokenra_absent_or_null_tags_do_not_disqualify():
    assert _tokenra_free(_tokenra_row()) is True
    assert _tokenra_free(_tokenra_row(tags=None)) is True


@pytest.mark.parametrize("tags,expected", [
    ("glm", True),
    (["glm"], True),
    ("video", False),
    ("VIDEO", False),
    (["video"], False),
    (["glm", "video"], False),
])
def test_tokenra_known_video_tags_are_rejected(tags, expected):
    """SYNTHETIC zero-priced rows: the real video rows also carry a positive
    ratio, so only a synthetic row isolates the tag exclusion itself."""
    assert _tokenra_free(_tokenra_row(tags=tags)) is expected


@pytest.mark.parametrize("tags", [5, {"video": True}, [1]])
def test_tokenra_malformed_tags_raise(tags):
    with pytest.raises(adapters.AdapterError):
        _tokenra_free(_tokenra_row(tags=tags))


@pytest.mark.parametrize("prices,expected", [
    (None, True),
    ({}, True),
    ({"480p": 0, "720p": 0.0, "1080p": 0}, True),
    ({"1080p": 6.522}, False),
    ({"480p": 0, "1080p": 0.000001}, False),
])
def test_tokenra_video_charge_map_must_be_known_zero(prices, expected):
    assert _tokenra_free(
        _tokenra_row(video_resolution_prices=prices)) is expected


@pytest.mark.parametrize("prices", [
    "flat", [], {"1080p": True}, {"1080p": "0"}, {"1080p": None}, {1: 0},
])
def test_tokenra_malformed_video_charge_map_raises(prices):
    with pytest.raises(adapters.AdapterError):
        _tokenra_free(_tokenra_row(video_resolution_prices=prices))


def test_tokenra_missing_token_rate_is_not_rescued_by_a_zero_video_map():
    row = _tokenra_row(video_resolution_prices={"1080p": 0})
    row.pop("model_ratio")
    assert _tokenra_free(row) is False


def test_tokenra_zero_ratio_with_a_positive_video_map_is_not_free():
    """A synthetic flat fee on a zero base."""
    row = _tokenra_row(video_resolution_prices={"1080p": 6.522})
    assert row["model_ratio"] == 0 and row["model_price"] == 0
    assert _tokenra_free(row) is False


# ---------- metadata view: unknown capabilities stay unknown ----------

def test_tokenra_metadata_view_keeps_unknown_capabilities():
    raw = TOKENRA_ROWS["vendor/chat-plain"]
    view = adapters.metadata_view(raw, _TOKENRA)
    assert view == {"name": "vendor/chat-plain"}
    assert set(view) == {"name"}
    assert view is not raw
    assert raw == TOKENRA_ROWS["vendor/chat-plain"]


def test_tokenra_metadata_view_passes_only_a_genuine_description_string():
    row = _tokenra_row("desc/ok", description="Real prose")
    view = adapters.metadata_view(row, _TOKENRA)
    assert view == {"name": "desc/ok", "description": "Real prose"}
    for bad in ({"en": "prose"}, 7, None, ["prose"]):
        view = adapters.metadata_view(
            _tokenra_row("desc/bad", description=bad), _TOKENRA)
        assert "description" not in view


def test_tokenra_metadata_view_never_aliases_or_mutates_the_raw_row():
    raw = _tokenra_row("alias/x", description="prose")
    before = copy.deepcopy(raw)
    view = adapters.metadata_view(raw, _TOKENRA)
    view["name"] = "changed"
    view["invented"] = 1
    assert raw == before


# ---------- synthetic Infron provider catalogue (P4, provider-models) ----------
#
# Infron publishes a `data` list of model rows, each carrying model-level
# `min_*` minima AND a nested `providers[]` array of per-provider offers. The
# minima are exactly the minimum over the nested providers, so a zero minimum
# describes an AVAILABLE offer rather than the route a bare request takes.
# Small hand-authored rows cover the free zero-route, the paid minima, the
# per-request video trap and the paid TTS row whose id contains "free".

_INFRON = adapters.PROVIDER_MODELS_FORMAT


def _infron_provider(slug="synthetic/one", prompt=0, completion=0, **extra):
    provider = {"provider_slug": slug, "prompt_price": prompt,
                "completion_price": completion}
    provider.update(extra)
    return provider


def _infron_row(model_id="vendor/launch", providers=_SENTINEL, **extra):
    """A complete, unsuffixed, known-zero row: the ':free' suffix is NOT part
    of the reviewed rule."""
    row = {
        "id": model_id,
        "category_type": "LLM",
        "supported_endpoint_types": ["openai"],
        "output_modalities": ["text"],
        "input_modalities": ["text"],
        "deprecated": False,
        "is_display_only": False,
        "min_prompt_price": 0,
        "min_completion_price": 0,
    }
    row["providers"] = ([_infron_provider()] if providers is _SENTINEL
                        else providers)
    row.update(extra)
    return row


def _infron_free(row):
    return adapters.is_free_row(row, _INFRON)


def _infron_body(*rows, **envelope):
    payload = {"success": True, "data": list(rows)}
    payload.update(envelope)
    return json.dumps(payload)


INFRON_FREE_IDS = ["vendor/free-launch", "vendor/free-suffixed:free"]
INFRON_ROWS = {
    "vendor/free-launch": _infron_row(
        "vendor/free-launch",
        display_name="Free Launch", description="Zero-priced launch.",
        context_length=256000,
        input_modalities=["text", "image", "video"],
        providers=[_infron_provider("zero/one", context_length=204000)]),
    "vendor/free-suffixed:free": _infron_row(
        "vendor/free-suffixed:free",
        display_name="Free Suffixed", supports_vision=True,
        input_modalities=["text"],
        providers=[_infron_provider("zero/two")]),
    "vendor/tts-free-named": _infron_row(
        "vendor/tts-free-named", category_type="Text To Speech",
        min_prompt_price=15, min_completion_price=None,
        providers=[_infron_provider("paid/tts", prompt=15,
                                    completion=None)]),
    "vendor/video-request-fee": _infron_row(
        "vendor/video-request-fee", min_request_price=0.04,
        providers=[_infron_provider("video/fee", request_price=0.04)]),
    "vendor/paid-minima": _infron_row(
        "vendor/paid-minima", min_prompt_price=0.212,
        min_completion_price=0.636,
        providers=[_infron_provider("paid/one", prompt=0.212,
                                    completion=0.636)]),
    "vendor/paid-sibling": _infron_row(
        "vendor/paid-sibling",
        providers=[_infron_provider("paid/two", prompt=0.1,
                                    completion=0.2)]),
}
INFRON_TEXT = _infron_body(*INFRON_ROWS.values())
INFRON_SUBSET = json.loads(INFRON_TEXT)


def test_synthetic_infron_subset_parses_to_the_original_rows():
    rows = adapters.parse_catalog(INFRON_TEXT, _INFRON)
    assert [row["id"] for row in rows] == [
        row["id"] for row in INFRON_SUBSET["data"]]
    for row in rows:
        assert row == INFRON_ROWS[row["id"]]
        assert "endpoint_name" not in row, "no provider field is lifted"
    assert rows[0] is not INFRON_SUBSET["data"][0]
    # The synthetic body publishes both JSON integers and floats for the
    # required prices.
    prompts = [row["min_prompt_price"] for row in rows]
    assert any(type(value) is int for value in prompts), "integer minimum"
    paid = [p for row in rows for p in row["providers"]
            if isinstance(p["completion_price"], float)]
    assert paid, "float provider price"
    # `min_request_price` is optional, undocumented and decides video rows.
    assert sum("min_request_price" in row for row in rows) == 1


def test_synthetic_infron_subset_reproduces_the_free_ids():
    rows = adapters.parse_catalog(INFRON_TEXT, _INFRON)
    assert sorted(row["id"] for row in rows if _infron_free(row)) == (
        INFRON_FREE_IDS)


@pytest.mark.parametrize("model_id", INFRON_FREE_IDS)
def test_each_infron_free_id_is_admitted(model_id):
    assert _infron_free(INFRON_ROWS[model_id]) is True


@pytest.mark.parametrize("model_id,reason", [
    ("vendor/tts-free-named",
     "paid Text To Speech row whose id contains 'free' (min_prompt 15, "
     "min_completion null)"),
    ("vendor/video-request-fee",
     "zero token prices with a positive per-request charge"),
    ("vendor/paid-minima", "paid minima over a paid provider"),
    ("vendor/paid-sibling", "the paid sibling of a free id"),
])
def test_infron_synthetic_counterexamples_are_not_admitted(model_id, reason):
    assert model_id in INFRON_ROWS, reason
    assert _infron_free(INFRON_ROWS[model_id]) is False, reason


def test_infron_every_synthetic_row_but_the_two_positives_is_rejected():
    admitted = [model_id for model_id, row in INFRON_ROWS.items()
                if _infron_free(row)]
    assert sorted(admitted) == INFRON_FREE_IDS
    assert len(INFRON_ROWS) - len(INFRON_FREE_IDS) == 4


# ---------- identity seam: the literal row id ----------

def test_infron_identity_field_is_id():
    assert adapters.identity_field(_INFRON) == "id"
    row = {"id": "Vendor/Model", "model_id": "other/value"}
    assert adapters.selected_identity(row, _INFRON) == "Vendor/Model"


def test_infron_literal_ids_are_preserved():
    ids = ["  Vendor/Padded  ", "vendor/\u00dcnicode-\u0394", "MixedCase/Model",
           "vendor/model:free"]
    body = _infron_body(*[_infron_row(model_id) for model_id in ids])
    rows = adapters.parse_catalog(body, _INFRON)
    assert [row["id"] for row in rows] == ids
    assert [row["id"] for row in rows if _infron_free(row)] == ids


# ---------- envelope, success semantics and duplicates ----------

@pytest.mark.parametrize("body", [
    "[]",
    '{"data": {}}',
    '{"data": null}',
    "{}",
    '{"success": true, "data": ["not-a-row"]}',
    '{"data": [{"category_type": "LLM"}]}',
    '{"data": [{"id": "", "category_type": "LLM"}]}',
    '{"data": [{"id": 7, "category_type": "LLM"}]}',
    '"<html>not json</html>"',
    "not json",
])
def test_infron_malformed_envelope_or_identity_raises(body):
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(body, _INFRON)


def test_infron_explicit_success_false_is_a_structural_fault():
    """A failed upstream response is never a healthy empty observation."""
    for body in ('{"success": false, "data": []}',
                 '{"success": false, "data": [%s]}'
                 % json.dumps(_infron_row())):
        with pytest.raises(adapters.AdapterError):
            adapters.parse_catalog(body, _INFRON)


@pytest.mark.parametrize("envelope", [
    {"unused": 1},
    {"success": True},
    {"success": 0},
    {"success": "false"},
    {"success": None},
])
def test_infron_absent_true_or_non_boolean_success_still_parses(envelope):
    rows = adapters.parse_catalog(
        _infron_body(_infron_row(), **envelope), _INFRON)
    assert [row["id"] for row in rows] == ["vendor/launch"]


def test_infron_valid_empty_and_paid_only_catalogues_are_successes():
    assert adapters.parse_catalog('{"success": true, "data": []}',
                                  _INFRON) == []
    paid = adapters.parse_catalog(
        _infron_body(_infron_row(min_prompt_price=0.212,
                                 min_completion_price=0.636)),
        _INFRON)
    assert len(paid) == 1 and [_infron_free(row) for row in paid] == [False]


@pytest.mark.parametrize("order", [("0", "0.5"), ("0.5", "0")])
def test_infron_conflicting_duplicate_ids_raise_in_both_orders(order):
    rows = [('{"id":"dup/x","category_type":"LLM",'
             '"supported_endpoint_types":["openai"],'
             '"output_modalities":["text"],"deprecated":false,'
             '"is_display_only":false,"min_prompt_price":%s,'
             '"min_completion_price":0,"providers":[{"prompt_price":0,'
             '"completion_price":0}]}' % minimum) for minimum in order]
    body = '{"success": true, "data": [%s]}' % ",".join(rows)
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters.parse_catalog(body, _INFRON)


@pytest.mark.parametrize("order", [("false", "0"), ("0", "false")])
def test_infron_conflicting_boolean_numeric_duplicate_raises_in_both_orders(
        order):
    """A JSON boolean is not a number: ordinary equality would merge them."""
    rows = [('{"id":"dup/x","category_type":"LLM",'
             '"supported_endpoint_types":["openai"],'
             '"output_modalities":["text"],"deprecated":false,'
             '"is_display_only":false,"min_prompt_price":%s,'
             '"min_completion_price":0,"providers":[{"prompt_price":0,'
             '"completion_price":0}]}' % value) for value in order]
    body = '{"success": true, "data": [%s]}' % ",".join(rows)
    ordinary = [row["min_prompt_price"] for row in json.loads(body)["data"]]
    assert ordinary[0] == ordinary[1], "setup: False == 0 collapses the pair"
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters.parse_catalog(body, _INFRON)


@pytest.mark.parametrize("order", [("0", "1e-400"), ("1e-400", "0")])
def test_infron_conflicting_tiny_positive_duplicate_raises_in_both_orders(
        order):
    rows = [('{"id":"dup/x","category_type":"LLM",'
             '"supported_endpoint_types":["openai"],'
             '"output_modalities":["text"],"deprecated":false,'
             '"is_display_only":false,"min_prompt_price":0,'
             '"min_completion_price":%s,"providers":[{"prompt_price":0,'
             '"completion_price":0}]}' % value) for value in order]
    body = '{"success": true, "data": [%s]}' % ",".join(rows)
    ordinary = [row["min_completion_price"] for row in json.loads(body)["data"]]
    assert ordinary == [0.0, 0.0], "setup: the raw token underflows to zero"
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters.parse_catalog(body, _INFRON)


def test_infron_numerically_equivalent_duplicates_still_collapse():
    body_raw = ('{"success": true, "data": ['
                '{"id":"dup/x","category_type":"LLM",'
                '"supported_endpoint_types":["openai"],'
                '"output_modalities":["text"],"deprecated":false,'
                '"is_display_only":false,"min_prompt_price":0,'
                '"min_completion_price":0,"providers":[{"prompt_price":0,'
                '"completion_price":0}]},'
                '{"id":"dup/x","category_type":"LLM",'
                '"supported_endpoint_types":["openai"],'
                '"output_modalities":["text"],"deprecated":false,'
                '"is_display_only":false,"min_prompt_price":0.0,'
                '"min_completion_price":0.0,"providers":[{"prompt_price":0.0,'
                '"completion_price":0.0}]}]}')
    rows = adapters.parse_catalog(body_raw, _INFRON)
    assert [row["id"] for row in rows] == ["dup/x"]
    assert _infron_free(rows[0]) is True


# ---------- explicit zero, exact arithmetic ----------

@pytest.mark.parametrize("zero", [0, 0.0])
def test_infron_explicit_zero_spellings_are_free(zero):
    row = _infron_row(min_prompt_price=zero, min_completion_price=zero,
                      providers=[_infron_provider(prompt=zero,
                                                  completion=zero)])
    assert _infron_free(row) is True


def test_infron_tiny_json_number_price_does_not_underflow_to_free():
    """A genuine unquoted JSON literal, not `json.dumps` of a Python float.

    ``json.loads`` turns ``1e-400`` into ``0.0`` before any naive predicate can
    look, so the parsed float beside a real zero proves the fixture is
    discriminating while the adapter still rejects the row.
    """
    body = ('{"success": true, "data": [{'
            '"id":"tiny/model","category_type":"LLM",'
            '"supported_endpoint_types":["openai"],'
            '"output_modalities":["text"],"deprecated":false,'
            '"is_display_only":false,"min_prompt_price":0,'
            '"min_completion_price":0,"providers":['
            '{"provider_slug":"zero/one","prompt_price":0,'
            '"completion_price":0},'
            '{"provider_slug":"tiny/two","prompt_price":1e-400,'
            '"completion_price":0}]}]}')
    ordinary = json.loads(body)["data"][0]["providers"][1]["prompt_price"]
    assert ordinary == 0.0, "setup: the raw token underflows to a float zero"
    assert json.loads(json.dumps({"p": ordinary}))["p"] == 0.0
    rows = adapters.parse_catalog(body, _INFRON)
    assert rows[0]["providers"][1]["prompt_price"] == 0.0
    assert _infron_free(rows[0]) is False


def test_infron_unsuffixed_known_zero_launch_is_admitted():
    """The ':free' suffix supports the evidence but is NOT the rule."""
    row = _infron_row("vendor/stealth-launch")
    assert not row["id"].endswith(":free") and "free" not in row["id"].lower()
    assert _infron_free(row) is True
    # ... and a ':free' id with a paid route is still rejected.
    assert _infron_free(_infron_row(
        "vendor/paid:free",
        providers=[_infron_provider(prompt=0.4)])) is False


# ---------- scope: LLM, openai endpoint, text output, strict flags ----------

@pytest.mark.parametrize("overrides,expected", [
    ({"category_type": "Text To Speech"}, False),
    ({"category_type": "Text to Video"}, False),
    ({"category_type": "Embeddings"}, False),
    ({"category_type": "LLM"}, True),
    ({"supported_endpoint_types": ["anthropic"]}, False),
    ({"supported_endpoint_types": []}, False),
    ({"supported_endpoint_types": ["gemini", "openai"]}, True),
    ({"supported_endpoint_types": ["openai-response"]}, False),
    ({"output_modalities": ["image"]}, False),
    ({"output_modalities": ["audio"]}, False),
    ({"output_modalities": ["text", "audio"]}, True),
    ({"deprecated": True}, False),
    ({"deprecated": None}, False),
    ({"deprecated": 0}, False),
    ({"is_display_only": True}, False),
    ({"is_display_only": "false"}, False),
    ({"is_display_only": False}, True),
])
def test_infron_scope_controls(overrides, expected):
    assert _infron_free(_infron_row(**overrides)) is expected


@pytest.mark.parametrize("member", ["supported_endpoint_types",
                                    "output_modalities"])
@pytest.mark.parametrize("bad", ["openai", 7, {"openai": True},
                                 [None], [""]])
def test_infron_malformed_scope_container_raises(member, bad):
    with pytest.raises(adapters.AdapterError):
        _infron_free(_infron_row(**{member: bad}))


@pytest.mark.parametrize("member", ["supported_endpoint_types",
                                    "output_modalities"])
def test_infron_missing_scope_member_is_unknown_not_free(member):
    row = _infron_row()
    del row[member]
    assert _infron_free(row) is False
    assert _infron_free(_infron_row(**{member: None})) is False


# ---------- the model-level minimum rule ----------

@pytest.mark.parametrize("prompt,completion", [
    (0, 0.25),          # split minimum: only one side is zero
    (0.05, 0),
    (0.05, 0.25),
    ("0", "0"),         # a numeric string is not this format's zero
])
def test_infron_model_minima_must_both_be_known_zero(prompt, completion):
    if isinstance(prompt, str):
        with pytest.raises(adapters.AdapterError):
            _infron_free(_infron_row(min_prompt_price=prompt,
                                     min_completion_price=completion))
    else:
        assert _infron_free(_infron_row(min_prompt_price=prompt,
                                        min_completion_price=completion)) is False


@pytest.mark.parametrize("member", ["min_prompt_price", "min_completion_price"])
def test_infron_missing_or_null_model_minimum_is_unknown_not_zero(member):
    row = _infron_row()
    del row[member]
    assert _infron_free(row) is False
    assert _infron_free(_infron_row(**{member: None})) is False


@pytest.mark.parametrize("member", ["min_prompt_price", "min_completion_price"])
@pytest.mark.parametrize("bad", [True, False, [0], {"value": 0}, -0.01,
                                 float("nan"), float("inf")])
def test_infron_malformed_required_model_minimum_raises(member, bad):
    with pytest.raises(adapters.AdapterError):
        _infron_free(_infron_row(**{member: bad}))


@pytest.mark.parametrize("fee,expected", [
    (_SENTINEL, True),   # absent: not applicable
    (0, True),
    (0.0, True),
    (0.04, False),       # a positive compulsory request charge
    (None, False),       # present null: unknown, not numeric zero
    (True, False),       # malformed optional: unknown, not zero and not a fault
    ("0", False),
    ({"amount": 0}, False),
    (-0.01, False),      # negative: unusable, so unknown rather than a fault
    (float("nan"), False),
    (float("inf"), False),
])
def test_infron_model_request_fee_policy(fee, expected):
    row = _infron_row()
    if fee is _SENTINEL:
        assert "min_request_price" not in row
    else:
        row["min_request_price"] = fee
    assert _infron_free(row) is expected


# ---------- every advertised provider must be a known zero ----------

@pytest.mark.parametrize("providers", [[], None])
def test_infron_requires_at_least_one_provider(providers):
    assert _infron_free(_infron_row(providers=providers)) is False


def test_infron_missing_provider_container_is_not_free():
    row = _infron_row()
    del row["providers"]
    assert _infron_free(row) is False


@pytest.mark.parametrize("providers", [
    {"provider_slug": "one"},          # a mapping, not a list
    "synthetic/one",
    ["synthetic/one"],                 # a string, not an object
    [None],
    [{"prompt_price": 0, "completion_price": 0}, "second"],
])
def test_infron_malformed_provider_container_raises(providers):
    with pytest.raises(adapters.AdapterError):
        _infron_free(_infron_row(providers=providers))


@pytest.mark.parametrize("providers", [
    [_infron_provider("a"), _infron_provider("b", prompt=0.000001)],
    [_infron_provider("a"), _infron_provider("b", completion=0.2)],
    [_infron_provider("a", prompt=0.1)],
])
def test_infron_mixed_or_paid_provider_set_is_not_free(providers):
    """Default routing is not 'cheapest', so one paid offer disqualifies."""
    assert _infron_free(_infron_row(providers=providers)) is False


def test_infron_all_zero_multiple_providers_is_free():
    row = _infron_row(providers=[
        _infron_provider("infron/us"),
        _infron_provider("infron/cn"),
        _infron_provider("mirror/eu"),
    ])
    assert _infron_free(row) is True


@pytest.mark.parametrize("member", ["prompt_price", "completion_price"])
def test_infron_missing_or_null_provider_rate_is_unknown_not_zero(member):
    provider = _infron_provider()
    del provider[member]
    assert _infron_free(_infron_row(providers=[provider])) is False
    assert _infron_free(_infron_row(
        providers=[_infron_provider(**{member: None})])) is False


@pytest.mark.parametrize("member", ["prompt_price", "completion_price"])
@pytest.mark.parametrize("bad", [True, [0], -1])
def test_infron_malformed_required_provider_rate_raises(member, bad):
    with pytest.raises(adapters.AdapterError):
        _infron_free(_infron_row(
            providers=[_infron_provider(**{member: bad})]))


@pytest.mark.parametrize("fee,expected", [
    (_SENTINEL, True),
    (0, True),
    (0.14, False),
    (None, False),
    (True, False),
    ("0.14", False),
    (-0.14, False),
    (float("nan"), False),
])
def test_infron_provider_request_fee_policy(fee, expected):
    provider = _infron_provider()
    if fee is _SENTINEL:
        assert "request_price" not in provider
    else:
        provider["request_price"] = fee
    assert _infron_free(_infron_row(providers=[provider])) is expected


def test_infron_zero_token_prices_plus_a_paid_request_row_is_not_free():
    """The real video trap, as an LLM-shaped synthetic control."""
    row = _infron_row(providers=[_infron_provider(request_price=0.04)],
                      min_request_price=0.04)
    assert _infron_free(row) is False
    with_fee_only_nested = _infron_row(
        providers=[_infron_provider(request_price=0.04)])
    assert _infron_free(with_fee_only_nested) is False


# ---------- metadata view: genuine names, prose and modalities ----------

def test_infron_metadata_view_maps_the_published_fields():
    raw = INFRON_ROWS["vendor/free-launch"]
    view = adapters.metadata_view(raw, _INFRON)
    assert view["name"] == "Free Launch"
    assert view["description"] == raw["description"]
    assert view["context_length"] == 256000
    assert view["architecture"] == {"input_modalities": ["text", "image",
                                                        "video"],
                                    "output_modalities": ["text"]}
    assert "supported_parameters" not in view, "the feed publishes none"
    # The model and its single provider publish DIFFERENT context windows; both
    # raw facts survive and no clamping/enrichment is invented.
    assert raw["providers"][0]["context_length"] == 204000
    assert raw["context_length"] == 256000


def test_infron_metadata_view_prefers_the_modality_array_over_booleans():
    """A row publishes supports_vision true with a text-only input array."""
    raw = INFRON_ROWS["vendor/free-suffixed:free"]
    assert raw["supports_vision"] is True
    view = adapters.metadata_view(raw, _INFRON)
    assert view["architecture"] == {"input_modalities": ["text"],
                                    "output_modalities": ["text"]}
    assert "supports_vision" not in view
    assert "supports_function_calling" not in view
    assert "supports_json_mode" not in view


def test_infron_metadata_view_does_not_alias_or_mutate_the_raw_row():
    raw = copy.deepcopy(INFRON_ROWS["vendor/free-launch"])
    before = copy.deepcopy(raw)
    view = adapters.metadata_view(raw, _INFRON)
    view["name"] = "changed"
    view["architecture"]["input_modalities"].append("audio")
    assert raw == before


@pytest.mark.parametrize("raw,expected", [
    # Neither modality member published: no architecture key at all, so the
    # accepted projector reports both children missing exactly as it would for
    # an absent parent.
    ({"id": "only/id"}, {}),
    ({"id": "x", "input_modalities": ["text"]},
     {"architecture": {"input_modalities": ["text"],
                       "output_modalities": None}}),
    ({"id": "x", "input_modalities": "text", "output_modalities": ["text"]},
     {"architecture": {"input_modalities": "text",
                       "output_modalities": ["text"]}}),
    # Present junk is COPIED verbatim, never coerced: the accepted strict
    # projector is what degrades it, and the admitted identity survives.
    ({"id": "x", "display_name": 7, "description": {"en": "prose"}},
     {"name": 7, "description": {"en": "prose"}}),
])
def test_infron_metadata_view_keeps_missing_and_malformed_optional_fields(
        raw, expected):
    """Degradation belongs to the strict projector, not to this view."""
    view = adapters.metadata_view(raw, _INFRON)
    assert view == expected


def test_infron_metadata_view_passes_non_dict_metadata_through_untouched():
    for raw in (None, "not-a-record", ["row"]):
        assert adapters.metadata_view(raw, _INFRON) == raw


# ---------- synthetic AI/ML API typed-unit catalogue (P5, typed-unit-models) ----------
#
# AI/ML API publishes one row per ENDPOINT FAMILY, so the same canonical id
# appears under `anthropic/messages`, `openai/chat-completions` and
# `openai/responses/submit`. Small hand-authored rows cover endpoint selection,
# a variable quote, a fixed all-zero quote, a missing context window and
# preprocessing rows that are fixed all-zero but out of scope.

_AIML = adapters.TYPED_UNIT_MODELS_FORMAT
_AIML_CHAT = "openai/chat-completions"


def _aiml_unit(price=0, per=1000000, name="token", **extra):
    unit = {"type": "charge", "name": name, "content": "text",
            "measure": "output", "origin": "provided", "phase": "inference",
            "author": "user", "price": price, "per": per, "default": 256,
            "variable": False}
    unit.update(extra)
    return unit


def _aiml_pricing(units=_SENTINEL, **extra):
    pricing = {"currency": "USD", "kind": "fixed", "dimensions": [],
               "variants": [], "thresholds": [], "variable": False,
               "units": [_aiml_unit()] if units is _SENTINEL else units}
    pricing.update(extra)
    return pricing


def _aiml_row(mid="vendor/zero-chat", *, units=_SENTINEL, kind="fixed",
              variable=False, pricing=_SENTINEL, row_type=_AIML_CHAT, **extra):
    row = {
        "id": mid,
        "info": {"name": f"Name {mid}", "description": "prose",
                 "contextLength": 1000},
        "type": row_type,
        "modalities": {"input": ["text"], "output": ["text"]},
    }
    if pricing is _SENTINEL:
        row["pricing"] = _aiml_pricing(units, kind=kind, variable=variable)
    else:
        row["pricing"] = pricing
    row.update(extra)
    return row


def _aiml_free(row):
    return adapters.is_free_row(row, _AIML)


def _aiml_body(*rows, **envelope):
    payload = {"object": "list", "data": list(rows)}
    payload.update(envelope)
    return json.dumps(payload)


AIMLAPI_ROWS = {}


def _register_aiml(row):
    AIMLAPI_ROWS[(row["id"], row["type"])] = row
    return row


# The same canonical id published under three endpoint families.
_register_aiml(_aiml_row(
    "stealth/crouton-alpha", row_type="anthropic/messages",
    kind="variable", variable=True,
    info={"name": "Crouton Alpha", "description": "Chat via Anthropic.",
          "contextLength": 1048576},
    modalities={"input": ["image", "text", "video"], "output": ["text"]}))
_register_aiml(_aiml_row(
    "stealth/crouton-alpha", kind="variable", variable=True,
    info={"name": "Crouton Alpha", "description": "Chat via OpenAI.",
          "contextLength": 1048576},
    modalities={"input": ["image", "text", "video"], "output": ["text"]}))
_register_aiml(_aiml_row(
    "stealth/crouton-alpha", row_type="openai/responses/submit",
    kind="variable", variable=True,
    info={"name": "Crouton Alpha", "description": "Responses.",
          "contextLength": 1048576},
    modalities={"input": ["image", "text", "video"], "output": ["text"]}))
_register_aiml(_aiml_row(
    "vendor/zero-chat",
    info={"name": "Zero Chat", "description": "Fixed all-zero chat.",
          "contextLength": 1048576},
    modalities={"input": ["image", "text", "video"], "output": ["text"]}))
_register_aiml(_aiml_row(
    "minimax/music-cover",
    row_type="internal/audio-generations/preprocess"))
_register_aiml(_aiml_row(
    "mureka/file-upload",
    row_type="internal/audio-generations/preprocess"))
_register_aiml(_aiml_row(
    "vendor/variable-chat", kind="variable", variable=True,
    info={"name": "Variable Chat", "description": "Unquotable.",
          "contextLength": 4096}))
_register_aiml(_aiml_row(
    "vendor/unknown-context",
    info={"name": "Unknown Context", "description": "No window."},
    modalities={"input": ["image", "text"], "output": ["text"]}))
_register_aiml(_aiml_row(
    "vendor/pricing-only",
    info={"name": "Pricing Only", "description": "No modalities.",
          "contextLength": 1048576}))
del AIMLAPI_ROWS[("vendor/pricing-only", _AIML_CHAT)]["modalities"]

AIMLAPI_TEXT = _aiml_body(*AIMLAPI_ROWS.values())
AIMLAPI_SUBSET = json.loads(AIMLAPI_TEXT)
AIMLAPI_CHAT = {row["id"]: row for row in AIMLAPI_ROWS.values()
                if row["type"] == _AIML_CHAT}


def test_synthetic_aimlapi_subset_parses_only_the_chat_endpoint_family():
    rows = adapters.parse_catalog(AIMLAPI_TEXT, _AIML)
    assert [row["id"] for row in rows] == [
        "stealth/crouton-alpha", "vendor/zero-chat", "vendor/variable-chat",
        "vendor/unknown-context", "vendor/pricing-only"]
    assert all(row["type"] == _AIML_CHAT for row in rows)
    for row in rows:
        assert row == AIMLAPI_ROWS[(row["id"], row["type"])]
        assert row is not AIMLAPI_ROWS[(row["id"], row["type"])]
    # The preprocessing rows really are in the body and really are dropped.
    assert ("minimax/music-cover",
            "internal/audio-generations/preprocess") in AIMLAPI_ROWS
    assert ("mureka/file-upload",
            "internal/audio-generations/preprocess") in AIMLAPI_ROWS


def test_synthetic_aimlapi_roster_admits_only_fixed_zero_chat_rows():
    rows = adapters.parse_catalog(AIMLAPI_TEXT, _AIML)
    admitted = sorted(row["id"] for row in rows if _aiml_free(row))
    assert admitted == ["vendor/pricing-only", "vendor/unknown-context",
                        "vendor/zero-chat"]


@pytest.mark.parametrize("model_id", ["stealth/crouton-alpha",
                                     "vendor/variable-chat"])
def test_aimlapi_variable_zero_chat_rows_are_not_admitted(model_id):
    """Zeros on an unquotable `variable` block are the absence of a quote."""
    row = AIMLAPI_CHAT[model_id]
    assert row["pricing"]["kind"] == "variable"
    assert row["pricing"]["variable"] is True
    assert all(unit["price"] == 0 for unit in row["pricing"]["units"])
    assert _aiml_free(row) is False


def test_aimlapi_fixed_zero_preprocess_rows_are_excluded_by_endpoint():
    """The only fixed all-zero rows are free STEPS, not free chat models."""
    for key in (("minimax/music-cover",
                 "internal/audio-generations/preprocess"),
                ("mureka/file-upload",
                 "internal/audio-generations/preprocess")):
        row = AIMLAPI_ROWS[key]
        assert row["pricing"]["kind"] == "fixed"
        assert all(unit["price"] == 0 for unit in row["pricing"]["units"])
        assert _aiml_free(row) is False


def test_aimlapi_endpoint_filtering_precedes_duplicate_comparison():
    """Three synthetic rows share one id and differ only by endpoint family."""
    families = [AIMLAPI_ROWS[("stealth/crouton-alpha", row_type)]["type"]
                for row_type in ("anthropic/messages",
                                 "openai/chat-completions",
                                 "openai/responses/submit")]
    assert families == ["anthropic/messages", "openai/chat-completions",
                        "openai/responses/submit"]
    rows = adapters.parse_catalog(AIMLAPI_TEXT, _AIML)
    crouton = [row for row in rows if row["id"] == "stealth/crouton-alpha"]
    assert len(crouton) == 1
    assert crouton[0]["type"] == _AIML_CHAT
    # A non-chat row missing an id is filtered out, not faulted as an identity.
    assert adapters.parse_catalog(
        '{"object":"list","data":[{"type":"anthropic/messages"}]}',
        _AIML) == []


# ---------- identity seam: the literal row id ----------

def test_aimlapi_identity_field_is_id():
    assert adapters.identity_field(_AIML) == "id"
    row = {"id": "Vendor/Model", "model_id": "other/value"}
    assert adapters.selected_identity(row, _AIML) == "Vendor/Model"


def test_aimlapi_literal_ids_are_preserved():
    ids = ["  Vendor/Padded  ", "vendor/\u00dcnicode-\u0394",
           "MixedCase/Model", "vendor/model:free"]
    rows = adapters.parse_catalog(
        _aiml_body(*[_aiml_row(model_id) for model_id in ids]), _AIML)
    assert [row["id"] for row in rows] == ids
    assert [row["id"] for row in rows if _aiml_free(row)] == ids


# ---------- envelope, success semantics and duplicates ----------

@pytest.mark.parametrize("body", [
    "[]",
    '{"data": {}}',
    '{"data": null}',
    "{}",
    '{"object": "list", "data": ["not-a-row"]}',
    '{"data": [{"id": "vendor/x"}]}',
    '{"data": [{"id": "vendor/x", "type": null}]}',
    '{"data": [{"id": "vendor/x", "type": 7}]}',
    '{"data": [{"id": "vendor/x", "type": ""}]}',
    '{"data": [{"id": "", "type": "openai/chat-completions"}]}',
    '{"data": [{"id": 7, "type": "openai/chat-completions"}]}',
    '"<html>not json</html>"',
    "not json",
])
def test_aimlapi_malformed_envelope_row_or_type_raises(body):
    with pytest.raises(adapters.AdapterError):
        adapters.parse_catalog(body, _AIML)


def test_aimlapi_explicit_success_false_is_a_structural_fault():
    """A failed upstream response is never a healthy empty observation."""
    for body in ('{"object":"list","success":false,"data":[]}',
                 '{"object":"list","success":false,"data":[%s]}'
                 % json.dumps(_aiml_row())):
        with pytest.raises(adapters.AdapterError):
            adapters.parse_catalog(body, _AIML)


@pytest.mark.parametrize("envelope", [
    {"unused": 1},
    {"success": True},
    {"success": 0},
    {"success": "false"},
    {"success": None},
])
def test_aimlapi_absent_true_or_non_boolean_success_still_parses(envelope):
    rows = adapters.parse_catalog(
        _aiml_body(_aiml_row(), **envelope), _AIML)
    assert [row["id"] for row in rows] == ["vendor/zero-chat"]


def test_aimlapi_valid_empty_and_paid_only_catalogues_are_successes():
    assert adapters.parse_catalog(
        '{"object":"list","data":[]}', _AIML) == []
    paid = adapters.parse_catalog(
        _aiml_body(_aiml_row(units=[_aiml_unit(price=0.25)])), _AIML)
    assert len(paid) == 1 and [_aiml_free(row) for row in paid] == [False]


@pytest.mark.parametrize("order", [(0, 0.5), (0.5, 0)])
def test_aimlapi_conflicting_duplicate_ids_raise_in_both_orders(order):
    rows = [_aiml_row("dup/x", units=[_aiml_unit(price=price)])
            for price in order]
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters.parse_catalog(_aiml_body(*rows), _AIML)


@pytest.mark.parametrize("order", [(False, 0), (0, False)])
def test_aimlapi_conflicting_boolean_numeric_duplicate_raises_in_both_orders(
        order):
    """A JSON boolean is not a number: ordinary equality would merge them."""
    rows = [{"id": "dup/x", "type": _AIML_CHAT, "marker": value}
            for value in order]
    ordinary = [row["marker"] for row in rows]
    assert ordinary[0] == ordinary[1], "setup: False == 0 collapses the pair"
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters.parse_catalog(_aiml_body(*rows), _AIML)


@pytest.mark.parametrize("order", [(0, "1e-400"), ("1e-400", 0)])
def test_aimlapi_tiny_positive_duplicate_raises_in_both_orders(order):
    rows = [('{"id":"dup/x","type":"openai/chat-completions",'
             '"pricing":{"kind":"fixed","variable":false,"thresholds":[],'
             '"dimensions":[],"variants":[],"units":[{"name":"token",'
             '"price":%s,"per":1000000,"variable":false}]}}' % price)
            for price in order]
    body = '{"object":"list","data":[%s]}' % ",".join(rows)
    ordinary = [row["pricing"]["units"][0]["price"]
                for row in json.loads(body)["data"]]
    assert ordinary == [0.0, 0.0], "setup: the raw token underflows to zero"
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters.parse_catalog(body, _AIML)


def test_aimlapi_numerically_equivalent_duplicates_still_collapse():
    rows = [_aiml_row("dup/x", units=[_aiml_unit(price=0)]),
            _aiml_row("dup/x", units=[_aiml_unit(price=0.0, per=1000000.0)])]
    parsed = adapters.parse_catalog(_aiml_body(*rows), _AIML)
    assert [row["id"] for row in parsed] == ["dup/x"]
    assert _aiml_free(parsed[0]) is True


# ---------- explicit zero, exact arithmetic, every charge unit ----------

@pytest.mark.parametrize("zero,per", [(0, 1000000), (0.0, 1000000),
                                      (0, 1), (0.0, 1)])
def test_aimlapi_explicit_zero_spellings_are_free(zero, per):
    assert _aiml_free(_aiml_row(
        units=[_aiml_unit(price=zero, per=per)])) is True


def test_aimlapi_future_fixed_zero_chat_row_is_admitted():
    """A clearly labelled synthetic future quote enters without an allowlist."""
    row = _aiml_row("vendor/future-fixed-zero", units=[
        _aiml_unit(name="token", per=1000000),
        _aiml_unit(name="call", per=1),
        _aiml_unit(name="generation", per=1),
    ])
    assert _aiml_free(row) is True
    parsed = adapters.parse_catalog(_aiml_body(row), _AIML)
    assert [item["id"] for item in parsed if _aiml_free(item)] == [
        "vendor/future-fixed-zero"]


@pytest.mark.parametrize("overrides", [
    {"kind": "variable"},
    {"kind": "variants"},
    {"kind": "unknown"},
    {"variable": True},
    {"variable": None},
    {"variable": "false"},
])
def test_aimlapi_nonfixed_or_variable_blocks_are_not_free(overrides):
    assert _aiml_free(_aiml_row(**overrides)) is False


@pytest.mark.parametrize("kind", [_SENTINEL, None, True, 7, "", []])
def test_aimlapi_malformed_or_missing_kind_is_a_structural_fault(kind):
    """`kind` is the required discriminator; only a real string is a class."""
    pricing = _aiml_pricing()
    if kind is _SENTINEL:
        del pricing["kind"]
    else:
        pricing["kind"] = kind
    with pytest.raises(adapters.AdapterError, match="pricing kind"):
        _aiml_free(_aiml_row(pricing=pricing))


@pytest.mark.parametrize("name", ["token", "call", "generation"])
def test_aimlapi_every_charge_unit_must_be_zero(name):
    """A zero token rate alone cannot make a per-call charge free."""
    units = [_aiml_unit(name="token"),
             _aiml_unit(name=name, per=1, price=0.01)]
    assert _aiml_free(_aiml_row(units=units)) is False


def test_aimlapi_positive_threshold_beside_a_zero_base_is_not_free():
    pricing = _aiml_pricing(thresholds=[
        {"from": 0, "units": [_aiml_unit()]},
        {"from": 128000, "units": [_aiml_unit(price=2)]},
    ])
    assert _aiml_free(_aiml_row(pricing=pricing)) is False


def test_aimlapi_every_threshold_branch_is_checked_not_just_the_first():
    pricing = _aiml_pricing(thresholds=[
        {"from": 0, "units": [_aiml_unit()]},
        {"from": 128000, "units": [_aiml_unit()]},
        {"from": 256000, "units": [_aiml_unit(price=0.000001)]},
    ])
    assert _aiml_free(_aiml_row(pricing=pricing)) is False


def test_aimlapi_all_zero_threshold_beside_a_zero_base_is_free():
    pricing = _aiml_pricing(thresholds=[
        {"from": 0, "units": [_aiml_unit()]},
        {"from": 128000, "units": [_aiml_unit(price=0.0)]},
    ])
    assert _aiml_free(_aiml_row(pricing=pricing)) is True


@pytest.mark.parametrize("threshold", [
    {"from": 0, "units": []},
    {"from": 0, "units": None},
    {"from": 0},
    {"units": [_aiml_unit()]},
    {"from": True, "units": [_aiml_unit()]},
    {"from": -1, "units": [_aiml_unit()]},
    {"from": 0.5, "units": [_aiml_unit()]},
])
def test_aimlapi_incomplete_or_malformed_threshold(threshold):
    pricing = _aiml_pricing(thresholds=[threshold])
    if (isinstance(threshold.get("from"), int)
            and not isinstance(threshold.get("from"), bool)
            and threshold["from"] >= 0
            and isinstance(threshold.get("units"), list)):
        assert _aiml_free(_aiml_row(pricing=pricing)) is False
    else:
        with pytest.raises(adapters.AdapterError):
            _aiml_free(_aiml_row(pricing=pricing))


@pytest.mark.parametrize("thresholds", ["tiers", {}, [None], [1]])
def test_aimlapi_malformed_threshold_container_raises(thresholds):
    with pytest.raises(adapters.AdapterError):
        _aiml_free(_aiml_row(pricing=_aiml_pricing(thresholds=thresholds)))


def test_aimlapi_threshold_with_an_unknown_charge_member_is_not_free():
    """The documented tier shape is closed; an unread override is not zero."""
    pricing = _aiml_pricing(thresholds=[
        {"from": 0, "units": [_aiml_unit()], "price": 2}])
    assert _aiml_free(_aiml_row(pricing=pricing)) is False


def test_aimlapi_discount_and_batch_metadata_neither_create_nor_block_zero():
    """A promotion marker is presentational; batch is a separate price."""
    pricing = _aiml_pricing(batch={"units": [_aiml_unit(price=0.5)]})
    row = _aiml_row(pricing=pricing,
                    info={"name": "promo", "contextLength": 1000,
                          "discount_percent_x100": 10000})
    assert _aiml_free(row) is True
    # A batch-only zero cannot rescue a positive synchronous base rate.
    paid = _aiml_row(pricing=_aiml_pricing(
        units=[_aiml_unit(price=2.5)], batch={"units": [_aiml_unit()]}))
    assert _aiml_free(paid) is False


def test_aimlapi_empty_units_are_incomplete_not_free():
    assert _aiml_free(_aiml_row(units=[])) is False
    assert _aiml_free(_aiml_row(
        pricing=_aiml_pricing(thresholds=[{"from": 0, "units": []}]))) is False


@pytest.mark.parametrize("units", [None, "units", {}, [None], ["unit"]])
def test_aimlapi_malformed_units_container_raises(units):
    with pytest.raises(adapters.AdapterError):
        _aiml_free(_aiml_row(units=units))


@pytest.mark.parametrize("pricing", [_SENTINEL, None, "pricing", [], 0, {}])
def test_aimlapi_missing_or_malformed_pricing_is_a_structural_fault(pricing):
    """A server ignoring include=pricing must fail the fetch, not look empty."""
    row = _aiml_row()
    if pricing is _SENTINEL:
        del row["pricing"]
    else:
        row["pricing"] = pricing
    with pytest.raises(adapters.AdapterError):
        _aiml_free(row)


def test_aimlapi_missing_pricing_include_faults_even_with_no_free_rows():
    body = ('{"object":"list","data":[{"id":"vendor/x",'
            '"type":"openai/chat-completions","info":{"name":"X"}}]}')
    rows = adapters.parse_catalog(body, _AIML)
    assert len(rows) == 1
    with pytest.raises(adapters.AdapterError, match="pricing"):
        _aiml_free(rows[0])


@pytest.mark.parametrize("price", [_SENTINEL, None])
def test_aimlapi_missing_or_null_price_is_unknown_not_zero(price):
    unit = _aiml_unit()
    if price is _SENTINEL:
        del unit["price"]
    else:
        unit["price"] = None
    assert _aiml_free(_aiml_row(units=[unit])) is False


@pytest.mark.parametrize("bad", [True, False, "0", [], [0], {}, -1, -0.5,
                                 float("nan"), float("inf")])
def test_aimlapi_malformed_required_price_raises(bad):
    with pytest.raises(adapters.AdapterError):
        _aiml_free(_aiml_row(units=[_aiml_unit(price=bad)]))


@pytest.mark.parametrize("per", [_SENTINEL, None])
def test_aimlapi_missing_or_null_denominator_is_unknown_not_zero(per):
    unit = _aiml_unit()
    if per is _SENTINEL:
        del unit["per"]
    else:
        unit["per"] = None
    assert _aiml_free(_aiml_row(units=[unit])) is False


@pytest.mark.parametrize("bad", [0, -1, True, "1000000", [], {},
                                 float("nan"), float("inf")])
def test_aimlapi_malformed_denominator_raises(bad):
    with pytest.raises(adapters.AdapterError):
        _aiml_free(_aiml_row(units=[_aiml_unit(per=bad)]))


def test_aimlapi_unit_variable_true_is_a_contradiction_not_free():
    assert _aiml_free(_aiml_row(
        units=[_aiml_unit(variable=True)])) is False


@pytest.mark.parametrize("bad", ["false", 0, 1, None])
def test_aimlapi_malformed_unit_variable_flag_raises(bad):
    with pytest.raises(adapters.AdapterError):
        _aiml_free(_aiml_row(units=[_aiml_unit(variable=bad)]))


@pytest.mark.parametrize("member,value", [
    ("dimensions", ["resolution"]),
    ("variants", [{"when": {"resolution": "2K"}, "price": 0}]),
])
def test_aimlapi_nonempty_dimensions_or_variants_are_not_free(member, value):
    assert _aiml_free(_aiml_row(
        pricing=_aiml_pricing(**{member: value}))) is False


@pytest.mark.parametrize("member", ["dimensions", "variants"])
@pytest.mark.parametrize("bad", ["resolution", {}, 1])
def test_aimlapi_malformed_dimension_container_raises(member, bad):
    with pytest.raises(adapters.AdapterError):
        _aiml_free(_aiml_row(pricing=_aiml_pricing(**{member: bad})))


@pytest.mark.parametrize("member,value,expected", [
    # Absent members and supported empty lists remain the fixed class.
    ("thresholds", _SENTINEL, True),
    ("dimensions", _SENTINEL, True),
    ("variants", _SENTINEL, True),
    ("schedule", _SENTINEL, True),
    ("thresholds", [], True),
    ("dimensions", [], True),
    ("variants", [], True),
    # A present null is unknown, not absent: it must not be admitted.
    ("thresholds", None, False),
    ("dimensions", None, False),
    ("variants", None, False),
    ("schedule", None, False),
    # A present schedule object remains a nonqualifying class.
    ("schedule", {"id": "deepseek-flash"}, False),
])
def test_aimlapi_modifier_presence_boundary(member, value, expected):
    """Modifier presence is the boundary: absent/empty vs present-null/object."""
    pricing = _aiml_pricing()
    if value is _SENTINEL:
        pricing.pop(member, None)
    else:
        pricing[member] = value
    assert _aiml_free(_aiml_row(pricing=pricing)) is expected


def test_aimlapi_nonchat_row_is_out_of_scope_without_reading_its_pricing():
    row = {"id": "internal/x",
           "type": "internal/audio-generations/preprocess",
           "pricing": {"kind": "fixed"}}
    assert _aiml_free(row) is False


# ---------- metadata view: genuine info/modalities, everything else unknown ----------

def test_aimlapi_metadata_view_maps_the_published_fields():
    raw = AIMLAPI_CHAT["stealth/crouton-alpha"]
    view = adapters.metadata_view(raw, _AIML)
    assert view["name"] == "Crouton Alpha"
    assert view["description"] == raw["info"]["description"]
    assert view["context_length"] == 1048576
    assert view["architecture"] == {
        "input_modalities": ["image", "text", "video"],
        "output_modalities": ["text"]}
    assert "supported_parameters" not in view
    assert "capabilities" not in view


def test_aimlapi_metadata_view_keeps_a_genuine_unknown_context():
    """A row has known modalities but no `info.contextLength` at all."""
    raw = AIMLAPI_CHAT["vendor/unknown-context"]
    assert "contextLength" not in raw["info"]
    view = adapters.metadata_view(raw, _AIML)
    assert view["name"] == "Unknown Context"
    assert "context_length" not in view
    assert view["architecture"] == {
        "input_modalities": ["image", "text"],
        "output_modalities": ["text"]}
    assert "supported_parameters" not in view


def test_aimlapi_pricing_only_source_cannot_claim_known_modalities():
    raw = AIMLAPI_CHAT["vendor/pricing-only"]
    assert "modalities" not in raw
    view = adapters.metadata_view(raw, _AIML)
    assert view["name"] == "Pricing Only"
    assert view["context_length"] == 1048576
    assert "architecture" not in view


def test_aimlapi_synthetic_missing_modalities_stays_unknown():
    row = _aiml_row()
    del row["modalities"]
    view = adapters.metadata_view(row, _AIML)
    assert view["name"] == "Name vendor/zero-chat"
    assert "architecture" not in view


def test_aimlapi_metadata_view_copies_optional_junk_verbatim():
    row = _aiml_row()
    row["info"]["contextLength"] = "1M"
    row["modalities"] = "text"
    view = adapters.metadata_view(row, _AIML)
    assert view["context_length"] == "1M"
    assert view["architecture"] == "text"


def test_aimlapi_metadata_view_does_not_alias_or_mutate_the_raw_row():
    raw = copy.deepcopy(AIMLAPI_CHAT["stealth/crouton-alpha"])
    before = copy.deepcopy(raw)
    view = adapters.metadata_view(raw, _AIML)
    view["name"] = "changed"
    view["architecture"]["input_modalities"].append("audio")
    assert raw == before


# ---------- genuine tiny JSON literals stay positive ----------

def test_aimlapi_tiny_json_number_does_not_underflow_to_free():
    """A genuine unquoted JSON literal, not `json.dumps` of a Python float."""
    body = ('{"object":"list","data":[{"id":"tiny/model",'
            '"type":"openai/chat-completions","pricing":{"kind":"fixed",'
            '"variable":false,"thresholds":[],"dimensions":[],"variants":[],'
            '"units":[{"name":"token","price":0,"per":1000000,'
            '"variable":false},{"name":"token","price":1e-400,'
            '"per":1000000,"variable":false}]}}]}')
    ordinary = json.loads(body)["data"][0]["pricing"]["units"][1]["price"]
    assert ordinary == 0.0, "setup: the raw token underflows to a float zero"
    rows = adapters.parse_catalog(body, _AIML)
    assert rows[0]["pricing"]["units"][1]["price"] == 0.0
    assert _aiml_free(rows[0]) is False
