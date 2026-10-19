"""Pure, side-effect-free raw-JSON acceptance and typed canonical encoding.

This module is the RAW boundary only. It deliberately knows nothing about the
published-snapshot STRUCTURAL schema, snapshot provenance/hashing policy,
query selection, or the root-script local-to-public compatibility adapter:
those arrive in later units and must EXTEND this module rather than be assumed
here. Nothing in this module imports the operator SDK, provider configuration,
the roster/state layer, the page renderer or any local adapter; there is no
file, environment, network or clock access at import time or at call time.

The public names this unit fixes are:

* :data:`MAX_SAFE_INT` -- the JSON-safe integer ceiling ``2**53 - 1``;
* :data:`MAX_JSON_DEPTH` -- the inclusive container-depth bound ``64``;
* :class:`CatalogError` -- the single raw-failure exception (``invalid_json``);
* :func:`load_json` -- strict raw-JSON acceptance;
* :func:`canonical_bytes` -- typed canonical JSON encoding.

``load_json`` is a strict, quote-aware predicate over the raw TEXT: it rejects
non-finite and unsafe numbers (in every notation, at every depth), duplicate
member names (at every object level, including decoded escape-equivalents),
over-deep nesting and every malformed lexeme, and it performs no BOM stripping,
trimming or other silent preprocessing. Lexical acceptance is NOT public
snapshot acceptance: a scalar, array or ``null`` is accepted here and any
structural rejection belongs to a later layer.
"""

from __future__ import annotations

import json
import math

#: The JSON-safe integer ceiling: ``2**53 - 1``.
MAX_SAFE_INT = 9007199254740991

#: Inclusive container-depth bound (a root container is depth 1).
MAX_JSON_DEPTH = 64

#: The single frozen raw-failure code and its literal public message.
INVALID_JSON = "invalid_json"
_ERROR_MESSAGES = {INVALID_JSON: "Invalid catalogue JSON."}

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
