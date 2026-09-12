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