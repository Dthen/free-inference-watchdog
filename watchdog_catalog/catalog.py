"""Pure, side-effect-free raw-JSON acceptance, typed canonical encoding and
strict public-snapshot structural validation.

This module is the RAW and STRUCTURAL boundary of the published catalogue
snapshot. It deliberately knows nothing about query selection, the marker
helper, ordering or the root-script local-to-public compatibility adapter:
those arrive in later units and must EXTEND this module rather than be assumed
here. Nothing in this module imports the operator SDK, provider configuration,
the roster/state layer, the page renderer or any local adapter; there is no
file, environment, network or clock access at import time or at call time.

The public names this unit fixes are:

* :data:`MAX_SAFE_INT` -- the JSON-safe integer ceiling ``2**53 - 1``;
* :data:`MAX_JSON_DEPTH` -- the inclusive container-depth bound ``64``;
* :class:`CatalogError` -- the single failure exception, carrying the frozen
  ``invalid_json``, ``invalid_snapshot`` or ``unsupported_schema_version``
  literal;
* :func:`load_json` -- strict raw-JSON acceptance;
* :func:`canonical_bytes` -- typed canonical JSON encoding;
* :func:`validate_snapshot` -- strict structural acceptance of an already
  PUBLISHED snapshot object (the same object back, or a frozen envelope);
* :func:`load_snapshot` -- ``validate_snapshot(load_json(text))``;
* :func:`snapshot_hash` -- the canonical body digest, excluding ONLY the root
  ``snapshot_id``.

``load_json`` is a strict, quote-aware predicate over the raw TEXT: it rejects
non-finite and unsafe numbers (in every notation, at every depth), duplicate
member names (at every object level, including decoded escape-equivalents),
over-deep nesting and every malformed lexeme, and it performs no BOM stripping,
trimming or other silent preprocessing. Lexical acceptance is NOT public
snapshot acceptance: a scalar, array or ``null`` is accepted here and rejected
``invalid_snapshot`` by ``validate_snapshot``.

``validate_snapshot`` rejects an already-PUBLISHED malformed object WHOLE --
never repaired, coerced or degraded -- and keeps hash verification load-bearing,
so ``snapshot_id`` is never treated as a merely well-shaped opaque string. It
performs no I/O, no DNS/network probing and no URL rewriting, and returns the
SAME object it was handed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from urllib.parse import urlsplit

#: The JSON-safe integer ceiling: ``2**53 - 1``.
MAX_SAFE_INT = 9007199254740991

#: Inclusive container-depth bound (a root container is depth 1).
MAX_JSON_DEPTH = 64

#: The single frozen raw-failure code and its literal public message.
INVALID_JSON = "invalid_json"
#: The two frozen STRUCTURAL failure codes and their literal public messages.
INVALID_SNAPSHOT = "invalid_snapshot"
UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
_ERROR_MESSAGES = {
    INVALID_JSON: "Invalid catalogue JSON.",
    INVALID_SNAPSHOT: "Invalid catalogue snapshot.",
    UNSUPPORTED_SCHEMA_VERSION: "Unsupported catalogue schema version.",
}

#: Decimal spelling of the safe ceiling, used for exact pre-conversion checks.
_SAFE_INT_TEXT = str(MAX_SAFE_INT)


class CatalogError(ValueError):
    """A rejected raw-JSON document.

    ``code`` and ``message`` carry the frozen public error literal; ``str()``
    is the fixed public message and never the offending JSON text, decoder
    output, an offset or a local path. :meth:`to_payload` returns a FRESH
    error envelope so a caller can never mutate shared state.
    """

    def __init__(self, code=INVALID_JSON, message=None):
        if message is None:
            message = _ERROR_MESSAGES[code]
        super().__init__(message)
        self.code = code
        self.message = message

    def to_payload(self):
        """The raw-failure envelope: exactly ``ok`` + ``error`` (code+message),
        with ``ok`` strictly ``False``. A new object on every call."""
        return {"ok": False,
                "error": {"code": self.code, "message": self.message}}


def _container_depth(text):
    """Quote-aware container-depth preflight: the maximum count of NESTED
    ``[``/``{`` outside strings (a root container is depth 1, a root scalar
    depth 0), so quoted brackets/braces and escaped quotes/backslashes never
    count. The native JSON decoder remains the syntax authority; this is only
    the bound guard that runs BEFORE any recursive decode."""
    depth = 0
    high = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[" or ch == "{":
            depth += 1
            if depth > high:
                high = depth
        elif ch == "]" or ch == "}":
            depth -= 1
    return high


def _parse_int(token):
    """Integer tokens are checked EXACTLY, by signed-digit magnitude, BEFORE
    any precision-losing (or limit-bounded) ``int`` conversion, so an unsafe
    integer -- including a thousands-of-digits lexeme -- rejects without ever
    raising the interpreter's integer-string-conversion ValueError."""
    digits = token[1:] if token[:1] == "-" else token
    digits = digits.lstrip("0") or "0"
    if len(digits) > len(_SAFE_INT_TEXT) or (
            len(digits) == len(_SAFE_INT_TEXT)
            and digits > _SAFE_INT_TEXT):
        raise CatalogError(INVALID_JSON)
    return int(token)


def _parse_float(token):
    """Floating/exponent tokens convert with normal binary64 semantics; a
    non-finite result, or an integer-valued result outside the safe range, is
    rejected. The returned value KEEPS float type even when integral."""
    value = float(token)
    if not math.isfinite(value):
        raise CatalogError(INVALID_JSON)
    if value == math.trunc(value) and not (
            -MAX_SAFE_INT <= value <= MAX_SAFE_INT):
        raise CatalogError(INVALID_JSON)
    return value


def _parse_constant(token):
    """The ``NaN``/``Infinity``/``-Infinity`` constants are never accepted."""
    raise CatalogError(INVALID_JSON)


def _object_pairs(pairs):
    """Reject a decoded-duplicate member name at EVERY object nesting, BEFORE
    any dict conversion, so there is no last-write-wins repair."""
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise CatalogError(INVALID_JSON)
        seen.add(key)
    return dict(pairs)


def load_json(text):
    """Strictly decode a raw JSON document string into typed Python values.

    Returns the decoded value on lexical acceptance (``bool``/``int``/``float``/
    ``str``/``None``/``list``/``dict``), preserving the integer-versus-float
    distinction, signed zero and escaped strings (including lone surrogates).
    Raises :class:`CatalogError` (``invalid_json``) on any intended raw failure.
    A non-``str`` argument raises ``TypeError`` -- a programming error, never a
    public error code.
    """
    if not isinstance(text, str):
        raise TypeError("load_json() requires a raw JSON document string")
    if _container_depth(text) > MAX_JSON_DEPTH:
        raise CatalogError(INVALID_JSON)
    try:
        return json.loads(
            text,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_parse_constant,
            object_pairs_hook=_object_pairs,
        )
    except CatalogError:
        raise
    except json.JSONDecodeError as exc:
        raise CatalogError(INVALID_JSON) from exc


def canonical_bytes(value):
    """The typed canonical encoding of an already raw-accepted JSON value.

    Exactly one reference path: UTF-8 object keys sorted by Unicode code point,
    compact ``,``/``:`` separators, ``ensure_ascii=True`` and finite values
    only. This is a GENERIC whole-value encoder -- it excludes NO keys, so a
    root or nested ``snapshot_id`` is encoded as ordinary content; any caller
    that wants a snapshot body must exclude that key itself. Native non-finite
    Python inputs are rejected by ``allow_nan=False`` (``ValueError``).
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


# ---------- strict structural validation of a published snapshot ----------

#: The only published schema version this module understands.
SCHEMA_VERSION = 1

# The exact v1 field sets. Every container is guarded BEFORE any set/sort/index/
# hash use, so a malformed JSON-compatible object yields the frozen structural
# envelope and never an incidental TypeError/KeyError.
_TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "snapshot_id", "tick_epoch", "cadence_s",
    "provider_order", "providers", "warnings",
})
_PROVIDER_FIELDS = frozenset({
    "display_name", "chat_completions_url", "api_type",
    "model_ids", "models", "stale",
})
_MODEL_FIELDS = frozenset({
    "id", "name", "description", "context_length",
    "architecture", "supported_parameters", "pricing",
})
_ARCHITECTURE_FIELDS = frozenset({"input_modalities", "output_modalities"})
_PRICING_FIELDS = frozenset({"prompt", "completion"})

#: The frozen warning vocabulary, in its deduplicated publication ORDER.
_WARNING_ORDER = ("tick_epoch_unknown", "metadata_missing", "metadata_invalid")
_WARNING_CODES = frozenset(_WARNING_ORDER)

#: The `snapshot_id` shape: exactly 64 LOWERCASE hex characters.
_SNAPSHOT_ID_RE = re.compile(r"[0-9a-f]{64}")


def _is_strict_int(value):
    """A JSON integer token: a `bool` is NOT an integer here."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_safe_nonneg_int(value):
    """A strict non-negative integer inside the JSON-safe range."""
    return _is_strict_int(value) and 0 <= value <= MAX_SAFE_INT


def _is_finite_scalar(value):
    """The projected pricing-scalar predicate, matching the contract exactly.

    `None` (unknown) and a finite numeric or numeric-string scalar are accepted;
    a `bool` is never a number. A native numeric must be finite AND raw-safe
    when it is integer-valued (ordinary non-integral float rounding is
    unchanged). A string is only probed with `float()` -- whitespace,
    underscores and Unicode decimal digits parse, which is exactly the frozen
    permissive edge -- and its ORIGINAL spelling is never converted or
    normalized.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -MAX_SAFE_INT <= value <= MAX_SAFE_INT
    if isinstance(value, float):
        if not math.isfinite(value):
            return False
        if value == math.trunc(value) and not (
                -MAX_SAFE_INT <= value <= MAX_SAFE_INT):
            return False
        return True
    if isinstance(value, str):
        try:
            return math.isfinite(float(value))
        except (ValueError, OverflowError):
            return False
    return False


def _is_wiring_url(url):
    """The frozen SYNTACTIC wiring predicate.

    A nonempty absolute `http`/`https` URL with a hostname, no userinfo, no
    fragment and no ASCII whitespace or control character. The character scan
    runs BEFORE `urlsplit` because the parser silently strips some control
    characters. Purely syntactic: no DNS/network probing, no public-IP or
    hostname allowlist, no provider inference, no port validation and no URL
    rewriting -- the original string is preserved verbatim. An EMPTY fragment
    component (a trailing `#`) is accepted; only a nonempty fragment is
    rejected. The port is deliberately never read, so port TEXT is not
    validated -- a preserved edge, not a reachability or security guarantee.
    """
    if not isinstance(url, str) or not url:
        return False
    for ch in url:
        code = ord(ch)
        if code <= 0x20 or code == 0x7F:
            return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.netloc or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    return not parts.fragment


def _is_string_list_or_null(value):
    """An optional capability array: a list of NONEMPTY strings, or `null`
    (unknown). `[]` is valid KNOWN empty. Nothing is deduplicated, casefolded
    or otherwise normalized at this layer."""
    if value is None:
        return True
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) and item for item in value)


def _is_pricing(value):
    """`null`, or exactly `prompt`+`completion` with finite scalars."""
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != _PRICING_FIELDS:
        return False
    return all(_is_finite_scalar(item) for item in value.values())


def _is_model(raw_id, model):
    """The exact projected-model field set, types and id/map-key identity."""
    if not isinstance(model, dict) or set(model) != _MODEL_FIELDS:
        return False
    if model["id"] != raw_id:
        return False
    name = model["name"]
    if not isinstance(name, str) or not name:
        return False
    description = model["description"]
    if description is not None and not isinstance(description, str):
        return False
    context = model["context_length"]
    if context is not None and not _is_safe_nonneg_int(context):
        return False
    architecture = model["architecture"]
    if not isinstance(architecture, dict) or \
            set(architecture) != _ARCHITECTURE_FIELDS:
        return False
    for modality in ("input_modalities", "output_modalities"):
        if not _is_string_list_or_null(architecture[modality]):
            return False
    if not _is_string_list_or_null(model["supported_parameters"]):
        return False
    return _is_pricing(model["pricing"])


def _is_provider(entry):
    """The exact provider field set, scalar types, literal-sorted unique
    `model_ids` and a `models` map keyed by EXACTLY those raw ids."""
    if not isinstance(entry, dict) or set(entry) != _PROVIDER_FIELDS:
        return False
    display_name = entry["display_name"]
    if not isinstance(display_name, str) or not display_name:
        return False
    if not _is_wiring_url(entry["chat_completions_url"]):
        return False
    api_type = entry["api_type"]
    if not isinstance(api_type, str) or not api_type:
        return False
    if not isinstance(entry["stale"], bool):
        return False
    ids = entry["model_ids"]
    if not isinstance(ids, list):
        return False
    if not all(isinstance(raw_id, str) and raw_id for raw_id in ids):
        return False
    # Literal Unicode code-point order and uniqueness -- NOT a natural sort.
    if len(ids) != len(set(ids)) or ids != sorted(ids):
        return False
    models = entry["models"]
    if not isinstance(models, dict):
        return False
    if not all(isinstance(raw_id, str) and raw_id for raw_id in models):
        return False
    if set(models) != set(ids):
        return False
    return all(_is_model(raw_id, models[raw_id]) for raw_id in ids)


def _is_body(snapshot):
    """Every v1-specific body check, run BEFORE the hash computation.

    Because the schema is exact and shallow, each recursive position is reached
    only after its container and member types were asserted -- so an invalid,
    cyclic or over-deep native structure is rejected by shape, and an invalid
    object never reaches `json.dumps`.
    """
    warnings = snapshot["warnings"]
    if not isinstance(warnings, list) or not all(
            isinstance(code, str) for code in warnings):
        return False
    if not set(warnings) <= _WARNING_CODES:
        return False
    if len(warnings) != len(set(warnings)):
        return False
    # Deduplicated in the frozen vocabulary order, never a permuted list.
    if warnings != [code for code in _WARNING_ORDER if code in set(warnings)]:
        return False

    tick = snapshot["tick_epoch"]
    if tick is None:
        # An unknown tick must say so, or a null reads as a healthy tick.
        if "tick_epoch_unknown" not in warnings:
            return False
    elif not _is_safe_nonneg_int(tick):
        return False

    cadence = snapshot["cadence_s"]
    if not _is_strict_int(cadence) or not 0 < cadence <= MAX_SAFE_INT:
        return False

    order = snapshot["provider_order"]
    providers = snapshot["providers"]
    if not isinstance(order, list) or not isinstance(providers, dict):
        return False
    if not all(isinstance(key, str) and key for key in order):
        return False
    if not all(isinstance(key, str) and key for key in providers):
        return False
    if len(order) != len(set(order)) or set(order) != set(providers):
        return False
    return all(_is_provider(providers[key]) for key in order)


def validate_snapshot(snapshot):
    """Strictly validate an already-PUBLISHED snapshot object.

    Returns the SAME object on acceptance -- never a copy, never repaired,
    resealed or canonicalized, and never mutated, on success OR on failure.
    Raises :class:`CatalogError` carrying the frozen structural code/message on
    rejection; it never raises ``invalid_json`` (that belongs to the raw layer)
    and never an incidental ``TypeError``/``KeyError``.

    Precedence, exactly as frozen: the top-level object, required-field-set and
    `schema_version`-TYPE gates, then the `schema_version` VALUE gate, then
    every v1-specific body check, and only then the body-hash verification.

    Domain: JSON-compatible native values -- what :func:`load_json` (or
    ``json.loads``) returns. A tuple/bytes/``UserDict`` root, a non-string key,
    a non-finite or unsafe number and a non-JSON native in an otherwise-shaped
    slot are all ``invalid_snapshot``.
    """
    if not isinstance(snapshot, dict):
        raise CatalogError(INVALID_SNAPSHOT)
    if set(snapshot) != _TOP_LEVEL_FIELDS:
        raise CatalogError(INVALID_SNAPSHOT)
    version = snapshot["schema_version"]
    if not _is_strict_int(version):
        raise CatalogError(INVALID_SNAPSHOT)
    if version != SCHEMA_VERSION:
        raise CatalogError(UNSUPPORTED_SCHEMA_VERSION)
    if not _is_body(snapshot):
        raise CatalogError(INVALID_SNAPSHOT)
    snapshot_id = snapshot["snapshot_id"]
    if not isinstance(snapshot_id, str) or \
            _SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None:
        raise CatalogError(INVALID_SNAPSHOT)
    # `_is_body` proved every remaining value JSON-compatible and raw-safe, so
    # the canonical encoder below cannot fail on an invalid object.
    if snapshot_id != snapshot_hash(snapshot):
        raise CatalogError(INVALID_SNAPSHOT)
    return snapshot


def load_snapshot(text):
    """Strictly accept a raw published-snapshot DOCUMENT.

    Exactly ``validate_snapshot(load_json(text))``: a raw lexical fault is the
    ``invalid_json`` envelope and precedes every structural check, and only a
    raw-accepted document reaches the structural layer (where a scalar, array
    or ``null`` root is ``invalid_snapshot``). A non-``str`` argument keeps the
    ``load_json`` programming-error ``TypeError``.
    """
    return validate_snapshot(load_json(text))


def snapshot_hash(snapshot):
    """The canonical body digest: SHA-256 of :func:`canonical_bytes` over the
    mapping with ONLY the ROOT ``snapshot_id`` removed.

    Precondition: a JSON-compatible, raw-safe mapping. This is the shared
    reference digest for a producer that seals a body and for a consumer that
    re-verifies one -- NOT a full schema validator. A NESTED ``snapshot_id`` is
    ordinary content and IS hashed normally. It works equally before and after
    the root id is set, so seal-then-set is never circular. The argument is
    never mutated; a non-``dict`` argument is a programming-error ``TypeError``.
    """
    if not isinstance(snapshot, dict):
        raise TypeError("snapshot_hash() requires a JSON-compatible mapping")
    body = {key: value for key, value in snapshot.items()
            if key != "snapshot_id"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


# ---------- optional capability projection and separate normalization ----------
#
# Two small PURE functions extend this module for the optional capability
# fields. `project_capabilities` degrades one upstream model fragment to the
# public capability shape (unknown and malformed optional metadata both become
# ``null`` and NEVER drop an endpoint, coerce a value or leak a private field);
# `normalize_capabilities` derives the query shape from an already-valid
# projection. They reuse the module's strict validators without changing their
# accepted meanings and add no I/O, clock, environment or network access.

#: The four normalized capability fields, in their frozen order; `unknown_fields`
#: names exactly these, in exactly this order.
_CAPABILITY_FIELD_ORDER = (
    "context_length", "input_modalities", "output_modalities",
    "supported_parameters",
)

#: The capability-only warning vocabulary, in deduplicated publication order.
#: `tick_epoch_unknown` is a snapshot-level code and never arises here.
_CAPABILITY_WARNING_ORDER = ("metadata_missing", "metadata_invalid")

#: Distinguishes an ABSENT upstream member from an explicit JSON ``null``.
_CAPABILITY_MISSING = object()

#: ASCII-only case fold: A-Z to a-z; every other code point (including an
#: accented or uppercase non-ASCII letter) stays LITERAL. Deliberately not
#: ``str.lower``/``str.casefold`` -- Unicode casefolding would collapse a
#: non-ASCII pair the contract keeps distinct.
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                            "abcdefghijklmnopqrstuvwxyz")


def _dedup_capability_warnings(codes):
    """The capability warning codes present in `codes`, deduplicated in the
    frozen vocabulary order. A fresh list on every call."""
    present = set(codes)
    return [code for code in _CAPABILITY_WARNING_ORDER if code in present]


def _project_capability_context(value):
    """Project an upstream context length: a strict non-negative JSON-safe
    integer is KNOWN; absent/``null`` is unknown (``metadata_missing``) and any
    other value -- including a float ``1.0``, a bool, a negative, an unsafe
    integer and a numeric string -- is malformed (``metadata_invalid``). The
    value is NEVER coerced to a number."""
    if value is _CAPABILITY_MISSING or value is None:
        return None, "metadata_missing"
    if _is_safe_nonneg_int(value):
        return value, None
    return None, "metadata_invalid"


def _project_capability_array(value):
    """Project an optional upstream capability array: a list of nonempty
    strings is COPIED (order, duplicates and casing preserved), ``[]`` is KNOWN
    empty, absent/``null`` is unknown (``metadata_missing``) and anything else
    is malformed and rejected WHOLE (``metadata_invalid``) -- never partially
    salvaged."""
    if value is _CAPABILITY_MISSING or value is None:
        return None, "metadata_missing"
    if _is_string_list_or_null(value):
        return list(value), None
    return None, "metadata_invalid"


def project_capabilities(raw):
    """Project one upstream capability fragment to the public capability shape.

    `raw` is an upstream model metadata mapping; any other upstream member is
    ignored and never returned. The result is a NEW ``(projected, warnings)``
    pair. ``projected`` carries exactly ``context_length``, ``architecture``
    (exactly ``input_modalities``/``output_modalities``) and
    ``supported_parameters``; every container is freshly allocated and every
    retained array is a copy with its original casing, order and duplicates.
    ``warnings`` is a fresh, deduplicated list in the frozen vocabulary order.

    Unknown and malformed optional metadata degrade to ``null`` (unknown) and
    NEVER drop an endpoint, coerce a value or serialize a private/secret field.
    A non-mapping `raw` is a programming-error ``TypeError``.
    """
    if not isinstance(raw, dict):
        raise TypeError(
            "project_capabilities() requires an upstream capability mapping")
    context, context_warn = _project_capability_context(
        raw.get("context_length", _CAPABILITY_MISSING))
    architecture = raw.get("architecture", _CAPABILITY_MISSING)
    if isinstance(architecture, dict):
        input_, input_warn = _project_capability_array(
            architecture.get("input_modalities", _CAPABILITY_MISSING))
        output_, output_warn = _project_capability_array(
            architecture.get("output_modalities", _CAPABILITY_MISSING))
    elif architecture is _CAPABILITY_MISSING or architecture is None:
        # An absent or explicitly-null parent: BOTH children are unknown.
        input_ = output_ = None
        input_warn = output_warn = "metadata_missing"
    else:
        # A malformed non-null parent makes both children unknown.
        input_ = output_ = None
        input_warn = output_warn = "metadata_invalid"
    params, params_warn = _project_capability_array(
        raw.get("supported_parameters", _CAPABILITY_MISSING))
    projected = {
        "context_length": context,
        "architecture": {"input_modalities": input_,
                         "output_modalities": output_},
        "supported_parameters": params,
    }
    warnings = _dedup_capability_warnings(
        (context_warn, input_warn, output_warn, params_warn))
    return projected, warnings


def _ascii_lower(text):
    """ASCII-only lowercasing: A-Z fold to a-z; every other code point stays
    literal. No whitespace trimming and no Unicode casefolding."""
    return text.translate(_ASCII_FOLD)


def _normalize_capability_array(values):
    """Normalize a KNOWN capability array: ASCII-fold each token and keep the
    FIRST occurrence of each folded value, preserving order. ``None`` (unknown)
    stays ``None``; a NEW list is always returned for a known array."""
    if values is None:
        return None
    normalized = []
    seen = set()
    for item in values:
        folded = _ascii_lower(item)
        if folded not in seen:
            seen.add(folded)
            normalized.append(folded)
    return normalized


def normalize_capabilities(projected):
    """Normalize a valid public capability projection to the query shape.

    Precondition: a valid public capability projection -- either the
    ``projected`` half of :func:`project_capabilities` or a validated public
    model object, whose extra identity/display/pricing members are ignored.
    This is NOT a second raw projection or validator.

    The result is a NEW mapping with exactly ``context_length``,
    ``input_modalities``, ``output_modalities``, ``supported_parameters`` and
    ``unknown_fields``. Context is unchanged; each known array is ASCII-folded
    and first-occurrence deduplicated into a new list; ``None`` stays ``None``.
    ``unknown_fields`` lists exactly the capability fields whose normalized
    value is ``None``, in the frozen order -- a known empty ``[]`` is not
    unknown. The input mapping is never normalized in place.
    """
    normalized = {
        "context_length": projected["context_length"],
        "input_modalities": _normalize_capability_array(
            projected["architecture"]["input_modalities"]),
        "output_modalities": _normalize_capability_array(
            projected["architecture"]["output_modalities"]),
        "supported_parameters": _normalize_capability_array(
            projected["supported_parameters"]),
    }
    normalized["unknown_fields"] = [
        field for field in _CAPABILITY_FIELD_ORDER
        if normalized[field] is None]
    return normalized
