"""Tests for detection dispatch — string-keyed free-model detection."""

from detection import detect_free


def test_api_pricing_zero():
    assert detect_free({"pricing": {"prompt": "0", "completion": "0"}}, "api-pricing") == True


def test_api_pricing_nonzero():
    assert detect_free({"pricing": {"prompt": "0.1", "completion": "0.2"}}, "api-pricing") == False


def test_api_pricing_mixed():
    assert detect_free({"pricing": {"prompt": "0", "completion": "0.1"}}, "api-pricing") == False


def test_api_pricing_missing():
    assert detect_free({"pricing": {}}, "api-pricing") == False


def test_api_pricing_none_values():
    assert detect_free({"pricing": {"prompt": None, "completion": None}}, "api-pricing") == False


def test_api_flag_true():
    assert detect_free({"isFree": True}, "api-flag") == True


def test_api_flag_false():
    assert detect_free({"isFree": False}, "api-flag") == False


def test_api_flag_missing():
    assert detect_free({}, "api-flag") == False


def test_api_flag_truthy_not_bool():
    """Only True (exact bool) should pass; truthy values like 1 or 'yes' should not."""
    assert detect_free({"isFree": 1}, "api-flag") == False
    assert detect_free({"isFree": "yes"}, "api-flag") == False


def test_id_suffix_colon():
    assert detect_free({"id": "meituan/longcat-2.0:free"}, "id-suffix") == True


def test_id_suffix_dash():
    assert detect_free({"id": "z-ai/glm-5.3-free"}, "id-suffix") == True


def test_id_suffix_substring():
    assert detect_free({"id": "freetier-model"}, "id-suffix") == True


def test_id_suffix_real_model():
    assert detect_free({"id": "glm-5.3"}, "id-suffix") == False


def test_id_suffix_non_string():
    assert detect_free({"id": 123}, "id-suffix") == False


def test_id_suffix_missing_id():
    assert detect_free({}, "id-suffix") == False


def test_id_suffix_empty_string():
    assert detect_free({"id": ""}, "id-suffix") == False


def test_id_suffix_case_insensitive():
    assert detect_free({"id": "model-FREE"}, "id-suffix") == True
    assert detect_free({"id": "model-Free"}, "id-suffix") == True


def test_all_free():
    assert detect_free({}, "all-free") == True


def test_all_free_with_any_model():
    assert detect_free({"id": "anything", "isFree": False}, "all-free") == True


def test_unknown_method():
    assert detect_free({}, "unknown") == False


def test_unknown_method_with_model():
    assert detect_free({"isFree": True}, "bogus-method") == False

# ---------- catalog-pricing: bounded optional format dispatch (P1) ----------

import pytest  # noqa: E402

import catalog_adapters  # noqa: E402


def _zero_row():
    return {"id": "vendor/model", "type": "language",
            "pricing": {"input": "0", "output": "0"}}


def test_catalog_pricing_dispatches_to_the_registered_format():
    assert detect_free(_zero_row(), "catalog-pricing",
                       "input-output-models") is True


def test_catalog_pricing_rejects_a_paid_row_through_the_adapter():
    row = {"id": "vendor/model", "type": "language",
           "pricing": {"input": "0", "output": "0.000001"}}
    assert detect_free(row, "catalog-pricing", "input-output-models") is False


@pytest.mark.parametrize("catalog_format", [None, "", "openai-models",
                                            "not-a-catalog-format", 1])
def test_catalog_pricing_without_a_nonlegacy_format_is_false(catalog_format):
    """No format, the legacy format and unimplemented formats never dispatch."""
    assert detect_free(_zero_row(), "catalog-pricing", catalog_format) is False


@pytest.mark.parametrize("method", ["api-pricing", "api-flag", "id-suffix",
                                    "all-free", "zero-credit-probe"])
def test_legacy_methods_ignore_the_format_argument(method):
    """The two-argument meanings are untouched by the optional third argument."""
    model = {"id": "vendor/model:free", "isFree": True,
             "pricing": {"prompt": "0", "completion": "0"}}
    assert detect_free(model, method) == detect_free(
        model, method, "input-output-models")


def test_catalog_pricing_differs_from_the_legacy_price_detector():
    """The Vercel family is a distinct key: legacy api-pricing reads
    prompt/completion and must not qualify this shape by accident."""
    row = _zero_row()
    assert detect_free(row, "api-pricing") is False
    assert detect_free(row, "catalog-pricing", "input-output-models") is True
    assert catalog_adapters.INPUT_OUTPUT_FORMAT == "input-output-models"


def _felo_zero_row():
    return {
        "id": "vendor/felo",
        "deprecated": False,
        "model_kind": "chat",
        "output_modalities": ["text"],
        "channels": [{
            "runnable": True,
            "pricing_mode": "configured_rates",
            "discount_multiplier": "1",
            "standard_pricing": {"billing_kind": "token", "prompt": "0",
                                 "completion": "0", "tiers": []},
        }],
    }


def test_catalog_pricing_dispatches_the_felo_channel_family():
    assert detect_free(_felo_zero_row(), "catalog-pricing",
                       "channel-catalog") is True
    assert catalog_adapters.CHANNEL_CATALOG_FORMAT == "channel-catalog"
    paid = _felo_zero_row()
    paid["channels"][0]["standard_pricing"]["prompt"] = "0.000001"
    assert detect_free(paid, "catalog-pricing", "channel-catalog") is False


def _tokenra_zero_row():
    return {
        "model_name": "vendor/tokenra",
        "quota_type": 0,
        "model_ratio": 0,
        "model_price": 0,
        "enable_groups": ["default"],
        "supported_endpoint_types": ["openai"],
    }


def test_catalog_pricing_dispatches_the_tokenra_newapi_family():
    assert detect_free(_tokenra_zero_row(), "catalog-pricing",
                       "newapi-pricing") is True
    assert catalog_adapters.NEWAPI_PRICING_FORMAT == "newapi-pricing"
    paid = _tokenra_zero_row()
    paid["model_price"] = 0.012
    paid["quota_type"] = 1
    assert detect_free(paid, "catalog-pricing", "newapi-pricing") is False


def _infron_zero_row(model_id="vendor/infron"):
    return {
        "id": model_id,
        "category_type": "LLM",
        "supported_endpoint_types": ["openai"],
        "output_modalities": ["text"],
        "deprecated": False,
        "is_display_only": False,
        "min_prompt_price": 0,
        "min_completion_price": 0,
        "providers": [{"provider_slug": "infron/us", "prompt_price": 0,
                       "completion_price": 0}],
    }


def test_catalog_pricing_dispatches_the_infron_provider_family():
    assert detect_free(_infron_zero_row(), "catalog-pricing",
                       "provider-models") is True
    assert catalog_adapters.PROVIDER_MODELS_FORMAT == "provider-models"
    # One paid provider behind a zero minimum disqualifies the whole route.
    mixed = _infron_zero_row()
    mixed["providers"] = [{"provider_slug": "infron/us", "prompt_price": 0,
                           "completion_price": 0},
                          {"provider_slug": "azure", "prompt_price": 0.1,
                           "completion_price": 0.5}]
    assert detect_free(mixed, "catalog-pricing", "provider-models") is False


def test_infron_free_named_tts_row_keeps_the_legacy_id_suffix_semantics():
    """The legacy substring detector is unchanged for other providers.

    Infron's real paid Text To Speech id contains the literal string "free", so
    the legacy ``id-suffix`` rule still selects it while the provider-models
    rule rejects it on category and price. The format argument is ignored by
    the legacy key, which is exactly the accepted behaviour.
    """
    tts = {
        "id": "fish-audio/s2.1-pro-free/text-to-audio",
        "category_type": "Text To Speech",
        "supported_endpoint_types": ["openai"],
        "output_modalities": ["audio"],
        "deprecated": False,
        "is_display_only": False,
        "min_prompt_price": 15,
        "min_completion_price": None,
        "providers": [{"provider_slug": "fish-audio", "prompt_price": 15,
                       "completion_price": None}],
    }
    assert detect_free(tts, "id-suffix") is True
    assert detect_free(tts, "id-suffix", "provider-models") is True
    assert detect_free(tts, "catalog-pricing", "provider-models") is False


def _aimlapi_zero_row(model_id="vendor/aimlapi", kind="fixed",
                      variable=False, price=0):
    return {
        "id": model_id,
        "type": "openai/chat-completions",
        "info": {"name": model_id},
        "modalities": {"input": ["text"], "output": ["text"]},
        "pricing": {
            "kind": kind,
            "variable": variable,
            "thresholds": [],
            "dimensions": [],
            "variants": [],
            "units": [{"name": "token", "price": price, "per": 1000000,
                       "variable": False}],
        },
    }


def test_catalog_pricing_dispatches_the_aimlapi_typed_unit_family():
    assert detect_free(_aimlapi_zero_row(), "catalog-pricing",
                       "typed-unit-models") is True
    assert catalog_adapters.TYPED_UNIT_MODELS_FORMAT == "typed-unit-models"
    # A variable block's zeros are not a quote.
    assert detect_free(_aimlapi_zero_row(kind="variable", variable=True),
                       "catalog-pricing", "typed-unit-models") is False
    # A positive non-token charge disqualifies a zero token rate.
    row = _aimlapi_zero_row()
    row["pricing"]["units"].append(
        {"name": "call", "price": 0.01, "per": 1, "variable": False})
    assert detect_free(row, "catalog-pricing", "typed-unit-models") is False


def test_registered_family_without_a_predicate_fails_loudly(monkeypatch):
    """P1 QUALITY Q2: silence here would be a healthy empty catalogue.

    The family is synthetic and test-only; it exists solely to prove the
    dispatch boundary refuses a registry entry with no predicate branch.
    """
    monkeypatch.setattr(
        catalog_adapters, "REGISTERED_FORMATS",
        catalog_adapters.REGISTERED_FORMATS + ("synthetic-registered-family",))
    with pytest.raises(catalog_adapters.AdapterError, match="free-row rule"):
        detect_free(_zero_row(), "catalog-pricing",
                    "synthetic-registered-family")
