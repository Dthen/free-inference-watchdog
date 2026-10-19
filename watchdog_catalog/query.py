"""Pure, stdlib-only free-marker query helpers.

This module is the single source of truth for the marker-insensitive free-marker
stripper: it imports only :mod:`re` and performs no I/O, configuration loading,
credential resolution or SDK import. ``build_site.strip_free_marker`` and
``mcp_server.strip_free_marker`` re-export this exact function object rather
than carrying a second copy.
"""

import copy
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


# -- pure list-free-models query: bulk/single success + argument boundary --

#: The two frozen argument-error codes and their literal public messages.
_INVALID_ARGUMENTS = "invalid_arguments"
_UNKNOWN_PROVIDER = "unknown_provider"
_ARGUMENT_MESSAGES = {
    _INVALID_ARGUMENTS: "Invalid tool arguments.",
    _UNKNOWN_PROVIDER: "Unknown provider.",
}


def _argument_error(code, parameter):
    """The shared frozen argument-error envelope.

    Top-level fields are exactly ``ok`` and ``error``; ``ok`` is strictly
    ``False``; ``error`` carries exactly ``code``, ``message`` and ``parameter``.
    ``parameter`` is the offending KNOWN signature parameter name, or ``None``
    for a non-object argument or an unknown argument name. The envelope never
    reflects an arbitrary supplied key or hostile value and carries no ``tool``,
    path, ``source``, cache, provenance, staleness, partial-data or count field.
    A NEW object is returned on every call, so a caller can never mutate shared
    state.
    """
    return {"ok": False,
            "error": {"code": code, "message": _ARGUMENT_MESSAGES[code],
                      "parameter": parameter}}


def list_free_models(snapshot, arguments):
    """The pure core of the frozen BULK ``list_free_models`` tool.

    ``snapshot`` is an already-VALIDATED public snapshot object -- the
    ``load_snapshot`` / source / adapter boundary owns raw, structural and hash
    validation, so this function performs NO validation, NO hash recompute, NO
    I/O and NO clock read. ``arguments`` is the whole DECODED argument object (a
    JSON object), passed explicitly rather than splatted as keyword arguments.

    Returns the frozen success payload directly -- never raising into the
    semantic caller for a malformed argument. With ``provider`` omitted or
    ``null`` the all-provider export carries exactly ``ok``, ``providers``,
    ``models``, ``counts``, ``total_ids``, ``n_gateways``, ``snapshot_id``,
    ``tick_epoch`` and ``provider_staleness``; with one exact registered
    ``provider`` the one-provider export carries exactly ``ok``, ``provider``,
    ``model_ids``, ``models``, ``count``, ``snapshot_id``, ``tick_epoch`` and
    ``provider_staleness``.

    Argument validation is deterministic and in the frozen order: the argument
    must be a JSON object; then ANY unknown key is rejected (``parameter``
    ``None``); then a non-``null`` provider that is not a string is
    ``invalid_arguments``; then a string absent from ``provider_order`` is
    ``unknown_provider`` (empty, padded and wrong-case keys are never repaired).

    Every returned container, array and selected projected model object (with
    its nested architecture/parameter/pricing containers) is freshly allocated
    (the model values are deep-copied), so a caller mutating a result can never
    corrupt the snapshot or another call. The inputs are never mutated, and the
    array order is the accepted :func:`natural_key` order -- never the legacy
    sort and never the snapshot's literal code-point order.
    """
    if not isinstance(arguments, dict):
        return _argument_error(_INVALID_ARGUMENTS, None)
    if any(key not in ("provider",) for key in arguments):
        return _argument_error(_INVALID_ARGUMENTS, None)
    provider = arguments.get("provider")
    if provider is not None and not isinstance(provider, str):
        return _argument_error(_INVALID_ARGUMENTS, "provider")
    order = snapshot["provider_order"]
    providers = snapshot["providers"]
    if provider is not None and provider not in order:
        return _argument_error(_UNKNOWN_PROVIDER, "provider")
    if provider is not None:
        entry = providers[provider]
        model_ids = sorted(entry["model_ids"], key=natural_key)
        models = copy.deepcopy(
            {raw_id: entry["models"][raw_id] for raw_id in entry["model_ids"]})
        return {
            "ok": True,
            "provider": provider,
            "model_ids": model_ids,
            "models": models,
            "count": len(model_ids),
            "snapshot_id": snapshot["snapshot_id"],
            "tick_epoch": snapshot["tick_epoch"],
            "provider_staleness": {provider: entry["stale"]},
        }
    return {
        "ok": True,
        "providers": {p: sorted(providers[p]["model_ids"], key=natural_key)
                      for p in order},
        "models": {
            p: copy.deepcopy({raw_id: providers[p]["models"][raw_id]
                              for raw_id in providers[p]["model_ids"]})
            for p in order},
        "counts": {p: len(providers[p]["model_ids"]) for p in order},
        "total_ids": len({raw_id for p in order
                          for raw_id in providers[p]["model_ids"]}),
        "n_gateways": len(order),
        "snapshot_id": snapshot["snapshot_id"],
        "tick_epoch": snapshot["tick_epoch"],
        "provider_staleness": {p: providers[p]["stale"] for p in order},
    }
