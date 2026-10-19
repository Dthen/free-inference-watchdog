"""Pure, stdlib-only free-marker query helpers.

This module is the single source of truth for the marker-insensitive free-marker
stripper: it imports only :mod:`re` and performs no I/O, configuration loading,
credential resolution or SDK import. ``build_site.strip_free_marker`` and
``mcp_server.strip_free_marker`` re-export this exact function object rather
than carrying a second copy.
"""

import re

# Regexes for strip_free_marker: match "free" only at a segment boundary
# (preceded by a separator for suffix, followed by one for prefix). The
# separator set is [-:_], matching how gateways tag free ids in practice.
_FREE_SUFFIX = re.compile(r"[-:_]free$", re.I)
_FREE_PREFIX = re.compile(r"^free[-:_]", re.I)


def strip_free_marker(model_id: str) -> str:
    """Remove ONLY a free marker at a segment boundary. Never fuzzy, never
    a rename map. If stripping would empty the string, return the input.

    A "free marker" is the substring "free" when it appears as a standalone
    segment at the start or end of the id, delimited by '-', ':', or '_'.
    Mid-string occurrences (``freetier``) and occurrences in the namespace
    portion after a '/' with no further separator are NOT markers.
    """
    s = _FREE_SUFFIX.sub("", model_id)
    s = _FREE_PREFIX.sub("", s)
    return s or model_id


# -- pure ordering primitives: natural raw-ID key + endpoint key --

# ONLY maximal ASCII [0-9]+ runs are digit runs. The capture group keeps every
# intervening text piece -- including the empty boundary pieces -- in the split
# result, so the token sequence preserves the string's exact boundaries.
_DIGIT_RUN = re.compile(r"([0-9]+)")


def natural_key(raw_id: str):
    """The natural raw-ID sort key for one provider's raw model id.

    Tokenizes ONLY maximal ASCII ``[0-9]+`` runs, preserving every intervening
    text piece and both string boundaries (the empty boundary text tokens are
    real tokens). A digit run becomes ``("d", len(run), run)``: numeric runs
    compare by ORIGINAL digit count and then by lexical ASCII digits, with no
    int/float parse of the digit text (so a 64-digit run is never rounded) and no
    stripping of leading zeroes. Every other piece becomes ``("t", 0, piece)``
    and compares case-sensitively by Unicode code point -- no case folding, no
    locale collation, no Unicode normalization and never UTF-16 code-unit
    comparison -- with the shorter token sequence first on a strict prefix.

    The leading tag makes a digit-run token sort before a text token and keeps
    every position homogeneous (str, int, str), so mixed numeric/text ids are
    always mutually comparable and no heterogeneous ``TypeError`` can occur.
    Non-ASCII numeric-looking characters (``١``, ``²``) are TEXT, never digit
    runs, and the raw id itself is never rewritten, trimmed or replaced by a
    normalized label.

    Pure: reads only the supplied string; no I/O, configuration, state or SDK.
    """
    tokens = []
    for index, piece in enumerate(_DIGIT_RUN.split(raw_id)):
        if index % 2:
            tokens.append(("d", len(piece), piece))
        else:
            tokens.append(("t", 0, piece))
    return tuple(tokens)


def endpoint_key(provider_order, provider: str, raw_id: str):
    """The sort key for the ``(provider, raw model_id)`` endpoint identity.

    ``provider_order`` is the caller's already-validated ordered list of
    registered provider keys; provider precedence is that list's index for
    ``provider`` -- never alphabetic key order, provider-map insertion order or
    global raw-ID order -- and the second component is :func:`natural_key` of the
    raw id. The same provider+id therefore always compares equal, while a raw id
    shared by two providers stays TWO distinct sortable pairs (it is never
    deduplicated) that order by provider precedence.

    Pure: reads only its arguments; no I/O, configuration, state or SDK, and no
    import beyond the module's existing ``re``.
    """
    return (provider_order.index(provider), natural_key(raw_id))
