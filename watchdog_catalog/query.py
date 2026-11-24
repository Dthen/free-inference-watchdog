"""Pure, stdlib-only free-marker and catalogue query helpers.

This module is the single source of truth for the marker-insensitive free-marker
stripper and the frozen catalogue query cores. It imports only the stdlib
:mod:`math` and :mod:`re` plus pure structural primitives from
:mod:`watchdog_catalog.catalog`; it performs no I/O, configuration loading,
credential resolution or SDK import.
"""

import math
import re

from watchdog_catalog.catalog import (MAX_SAFE_INT, _ascii_lower,
                                      normalize_capabilities)

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


# -- shared frozen argument-error envelope --

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


# -- pure get-model query: exact provider + literal raw id --

#: The explicit ASCII blank set the required ``model_id`` predicate uses:
#: U+0009..U+000D (tab, LF, VT, FF, CR) and U+0020 (space). It is NEVER widened
#: with Python's default ``str.strip`` / ``str.isspace``, which also match
#: Unicode whitespace (NBSP U+00A0, EM SPACE U+2003, ...) and would wrongly
#: reject a literal non-ASCII query.
_ASCII_BLANKS = " \t\n\r\v\f"

#: The frozen ``not_found`` envelope for an exact id absent from a known
#: provider. It carries no ``parameter`` member and never reflects the caller's
#: value.
_NOT_FOUND = "not_found"
_NOT_FOUND_MESSAGE = "Model not found."


def _not_found_error():
    """The frozen ``not_found`` envelope: a fresh object on every call."""
    return {"ok": False,
            "error": {"code": _NOT_FOUND, "message": _NOT_FOUND_MESSAGE}}


def _endpoint_summary(provider_key, raw_id, model):
    """The shared flat search-row projection of one tracked endpoint.

    Exactly ``provider``, ``model_id``, ``name``, ``context_length``,
    ``input_modalities``, ``output_modalities`` and ``supported_parameters``.
    The capability values come from :func:`catalog.normalize_capabilities`
    (ASCII folds, first-occurrence dedup); unknown stays ``null`` and a known
    empty array stays ``[]``. The raw id is returned unchanged, and no
    description, wiring or ``metadata`` container is included.
    """
    normalized = normalize_capabilities(model)
    return {
        "provider": provider_key,
        "model_id": raw_id,
        "name": model["name"],
        "context_length": normalized["context_length"],
        "input_modalities": normalized["input_modalities"],
        "output_modalities": normalized["output_modalities"],
        "supported_parameters": normalized["supported_parameters"],
    }


def _endpoint_details(provider_key, entry, raw_id, model):
    """One ``get_model`` details endpoint: the shared summary plus the fields
    that compact search rows omit -- ``description`` flat, plus the provider's
    ``chat_completions_url`` and ``api_type``. One projection source, two
    consumers; the metadata containers themselves are never returned."""
    summary = _endpoint_summary(provider_key, raw_id, model)
    return {
        "provider": summary["provider"],
        "model_id": summary["model_id"],
        "name": summary["name"],
        "description": model["description"],
        "context_length": summary["context_length"],
        "input_modalities": summary["input_modalities"],
        "output_modalities": summary["output_modalities"],
        "supported_parameters": summary["supported_parameters"],
        "chat_completions_url": entry["chat_completions_url"],
        "api_type": entry["api_type"],
    }


def get_model(snapshot, arguments):
    """The pure core of the ``get_model`` tool: exact provider + raw id.

    ``snapshot`` is an already-VALIDATED public snapshot object -- the
    ``load_snapshot`` / source boundary owns raw, structural and hash
    validation, so this function performs NO validation, NO hash recompute, NO
    I/O and NO clock read. ``arguments`` is the whole DECODED argument object,
    passed explicitly rather than splatted as keyword arguments.

    Returns either the frozen success payload or the shared error envelope
    directly -- never raising into the semantic caller for a malformed
    argument. A success carries exactly ``ok``, ``snapshot_id``, ``tick_epoch``,
    ``provider_staleness`` and ``endpoint``. ``provider_staleness`` covers
    exactly the requested provider; the endpoint is a flat details projection
    of that one ``(provider, raw model_id)`` pair.

    Argument validation is deterministic and in the frozen order: the argument
    must be a JSON object; then ANY unknown key is rejected (``parameter``
    ``None``); then ``provider`` must be present and a string; then it must be
    one exact ``provider_order`` key (otherwise ``unknown_provider``); then
    ``model_id`` must be present, a string and contain at least one character
    outside the explicit ASCII blank set. The raw id is never trimmed,
    case-folded, marker-stripped or repaired, and there is NO cross-provider or
    marker-insensitive lookup. A known provider with an absent exact id returns
    the ``not_found`` envelope, which echoes no caller value.

    Every returned container and endpoint is freshly allocated, so a caller
    mutating a result can never corrupt the snapshot or another call.
    """
    if not isinstance(arguments, dict):
        return _argument_error(_INVALID_ARGUMENTS, None)
    if any(key not in ("provider", "model_id") for key in arguments):
        return _argument_error(_INVALID_ARGUMENTS, None)

    provider = arguments.get("provider")
    if not isinstance(provider, str):
        return _argument_error(_INVALID_ARGUMENTS, "provider")
    order = snapshot["provider_order"]
    providers = snapshot["providers"]
    if provider not in order:
        return _argument_error(_UNKNOWN_PROVIDER, "provider")

    model_id = arguments.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip(_ASCII_BLANKS):
        return _argument_error(_INVALID_ARGUMENTS, "model_id")

    entry = providers[provider]
    if model_id not in entry["model_ids"]:
        return _not_found_error()

    return {
        "ok": True,
        "snapshot_id": snapshot["snapshot_id"],
        "tick_epoch": snapshot["tick_epoch"],
        "provider_staleness": {provider: entry["stale"]},
        "endpoint": _endpoint_details(
            provider, entry, model_id, entry["models"][model_id]),
    }


# -- pure whole-catalogue status query: fixed-clock freshness + coverage --

#: The explicit status-only warning order: the frozen snapshot THREE codes
#: (``tick_epoch_unknown``, ``metadata_missing``, ``metadata_invalid``) followed
#: by the two derived status codes (``tick_epoch_future``, ``tick_stale``). A
#: status payload unions the snapshot warnings with its derived codes and emits
#: this order; the two derived codes are mutually exclusive, so at most four
#: codes appear in one payload. Snapshot documents never carry the status-only
#: codes.
_STATUS_WARNING_ORDER = (
    "tick_epoch_unknown", "metadata_missing", "metadata_invalid",
    "tick_epoch_future", "tick_stale",
)


def watchdog_status(snapshot, arguments, *, now):
    """The pure core of the frozen ``watchdog_status`` tool.

    ``snapshot`` is an already-VALIDATED public snapshot object -- the
    ``load_snapshot`` / source / adapter boundary owns raw, structural and hash
    validation, so this function performs NO validation, NO hash recompute, NO
    I/O and NO clock read. ``arguments`` is the whole DECODED argument object (a
    JSON object), passed explicitly rather than splatted as keyword arguments.

    ``now`` is the REQUIRED keyword-only fixed test clock: a strict non-negative
    safe-integer epoch in seconds. It is a programming PRECONDITION (mirroring
    the snapshot precondition), never a public argument -- a wrapper later owns
    capturing and normalizing ``time.time()`` into this int, and this core never
    reads a clock itself. Keyword-only and defaultless keep it structurally
    impossible to pass as a tool argument.

    Returns the frozen success payload directly -- never raising into the
    semantic caller for a malformed argument. The payload carries exactly
    ``ok``, ``snapshot_id``, ``tick_epoch``, ``cadence_s``, ``last_tick_age_s``,
    ``tick_fresh``, ``provider_staleness``, ``provider_counts``,
    ``metadata_coverage``, ``counts``, ``pending_alerts``, ``site_published``,
    ``site_publish_age_s`` and ``warnings``. ``ok`` is exactly ``True``: the
    valid snapshot was queried successfully, NOT that its observations are fresh
    or that every provider is healthy.

    ``last_tick_age_s`` is ``now - tick_epoch`` (a strict int) for a known
    non-future tick, else ``None`` for a null or future tick -- never clamped to
    a healthy zero. ``tick_fresh`` is the inclusive one-cadence boundary:
    ``True`` exactly when the age is known and ``<= cadence_s`` (age ``0`` and
    age ``== cadence_s`` are both fresh), else ``False``; it is always a strict
    bool. ``cadence_s`` is snapshot data, never a hardcoded ``3600``, and a
    recent fetch never replaces ``tick_epoch`` or resets the age.

    ``provider_staleness`` is a ``bool`` map for EVERY ``provider_order`` key
    (zero-ID providers included) equal to each gateway's carried-forward stale
    flag, unchanged by the clock; ``provider_counts`` is the matching strict
    non-negative int map of each gateway's ``len(model_ids)``. The two are
    INDEPENDENT health signals: staleness may be ``True`` during a fresh tick
    and all providers may be ``False`` during an old tick.

    ``counts`` is the WHOLE-CATALOGUE cardinality object (exactly ``gateways``,
    ``endpoints``, ``distinct_raw_ids`` and ``marker_group_labels``): every
    configured provider including zero-ID ones, every tracked ``(provider, raw
    ID)`` pair, the literal union of raw IDs and the distinct
    :func:`strip_free_marker` labels -- labels, never verified underlying model
    identities. ``metadata_coverage`` reports coverage of the stated metadata
    fields over the global tracked-pair denominator (exactly ``endpoints``,
    ``context_length_known``, ``input_modalities_known``,
    ``output_modalities_known``, ``supported_parameters_known`` and
    ``description_known``): a tracked pair counts when its projected field is
    non-``None`` -- a known ``0``, a known empty ``[]`` and a known ``""``
    description count, a ``null`` does not.

    ``pending_alerts``, ``site_published`` and ``site_publish_age_s`` are each
    STRICTLY ``None`` in this pure published-catalogue payload; ``False`` or
    ``0`` would falsely claim observed operator state. ``warnings`` unions the
    snapshot warnings (never removed) with the derived ``tick_epoch_unknown``
    (null epoch), ``tick_epoch_future`` (future epoch) and ``tick_stale`` (known
    age strictly greater than ``cadence_s``), deduplicated in the frozen
    ``_STATUS_WARNING_ORDER``; a fresh known epoch adds no status warning, and a
    null or future epoch is never ``tick_stale``.

    Argument validation is deterministic and in the frozen order: the argument
    must be a JSON object; then ANY unknown key is rejected (``parameter``
    ``None``) -- ``watchdog_status`` has NO public arguments, so only ``{}`` is
    accepted. Validation runs before any clock use.

    Every returned map and the warnings list is freshly allocated and the
    snapshot is never mutated (the warnings are copied through a ``set``); the
    metadata itself is never returned, so no registry deep-copy is needed and a
    caller mutating a result can never corrupt the snapshot or another call.
    """
    if not isinstance(arguments, dict):
        return _argument_error(_INVALID_ARGUMENTS, None)
    # No public arguments: only the empty object is accepted.
    if any(key not in () for key in arguments):
        return _argument_error(_INVALID_ARGUMENTS, None)

    order = snapshot["provider_order"]
    providers = snapshot["providers"]
    cadence = snapshot["cadence_s"]
    tick = snapshot["tick_epoch"]

    if tick is None or tick > now:
        last_tick_age_s = None
        tick_fresh = False
    else:
        last_tick_age_s = now - tick
        tick_fresh = last_tick_age_s <= cadence

    endpoints = 0
    distinct_raw_ids = set()
    marker_group_labels = set()
    coverage = {
        "endpoints": 0,
        "context_length_known": 0,
        "input_modalities_known": 0,
        "output_modalities_known": 0,
        "supported_parameters_known": 0,
        "description_known": 0,
    }
    for provider in order:
        entry = providers[provider]
        for raw_id in entry["model_ids"]:
            endpoints += 1
            distinct_raw_ids.add(raw_id)
            marker_group_labels.add(strip_free_marker(raw_id))
            model = entry["models"][raw_id]
            if model["context_length"] is not None:
                coverage["context_length_known"] += 1
            architecture = model["architecture"]
            if architecture["input_modalities"] is not None:
                coverage["input_modalities_known"] += 1
            if architecture["output_modalities"] is not None:
                coverage["output_modalities_known"] += 1
            if model["supported_parameters"] is not None:
                coverage["supported_parameters_known"] += 1
            if model["description"] is not None:
                coverage["description_known"] += 1
    coverage["endpoints"] = endpoints

    present = set(snapshot["warnings"])  # never mutate the snapshot warnings
    if tick is None:
        present.add("tick_epoch_unknown")
    elif tick > now:
        present.add("tick_epoch_future")
    elif now - tick > cadence:
        present.add("tick_stale")
    warnings = [code for code in _STATUS_WARNING_ORDER if code in present]

    return {
        "ok": True,
        "snapshot_id": snapshot["snapshot_id"],
        "tick_epoch": tick,
        "cadence_s": cadence,
        "last_tick_age_s": last_tick_age_s,
        "tick_fresh": tick_fresh,
        "provider_staleness": {provider: providers[provider]["stale"]
                               for provider in order},
        "provider_counts": {provider: len(providers[provider]["model_ids"])
                            for provider in order},
        "metadata_coverage": coverage,
        "counts": {
            "gateways": len(order),
            "endpoints": endpoints,
            "distinct_raw_ids": len(distinct_raw_ids),
            "marker_group_labels": len(marker_group_labels),
        },
        "pending_alerts": None,
        "site_published": None,
        "site_publish_age_s": None,
        "warnings": warnings,
    }


# -- pure inclusive minimum-context stage: per-pair known context --


def _search_min_context_pairs(snapshot, pairs, *, min_context_length=None):
    """The INTERNAL inclusive minimum-context stage of ``search_models``.

    Preconditions: ``snapshot`` is an already-VALIDATED public snapshot and
    ``pairs`` is an ordered sequence of immutable ``(provider_key, raw_model_id)``
    tuples identifying tracked pairs of that snapshot -- it is the ALREADY
    selected candidate set (typically from :func:`_search_text_provider_pairs`),
    never re-derived here. ``min_context_length`` is ``None`` or an
    already-validated/normalized non-negative SAFE native int. The public decoded
    argument is validated and normalized by the later transport-level search
    unit before this stage is invoked; this stage never validates its argument,
    parses a numeric string, coerces a bool, or invents a temporary error.

    Returns a NEW list of only those supplied pairs whose OWN projected model
    ``context_length`` is not ``None`` and is ``>=`` the threshold -- an
    INCLUSIVE comparison on each pair's own metadata. A pair's context is never
    borrowed from a sibling provider sharing its raw id, inferred from a name or
    description, defaulted to zero, or merged across providers: endpoint identity
    is exactly ``(provider, raw model_id)``.

    ``min_context_length`` ``None`` applies NO constraint and returns a fresh
    list with every supplied pair unchanged; ``0`` is an ACTIVE threshold that
    retains a known zero and rejects an unknown (``null``) context. There is no
    truthiness test of the threshold or of the context, no ``or 0`` fallback, no
    sort, no deduplication, no roster/config read and no pagination: the incoming
    pair order is preserved exactly, the supplied selection is never expanded,
    and more than the default page's worth of eligible pairs stay complete.

    The snapshot and the input pair sequence are never mutated, and only
    immutable identity tuples are returned, so a caller mutating the result can
    never corrupt the snapshot, the input sequence or another call.
    """
    if min_context_length is None:
        return list(pairs)
    providers = snapshot["providers"]
    kept = []
    for provider_key, raw_id in pairs:
        context = providers[provider_key]["models"][raw_id]["context_length"]
        if context is not None and context >= min_context_length:
            kept.append((provider_key, raw_id))
    return kept


# -- pure metadata-search candidate selection: literal text + exact provider --


def _search_text_provider_pairs(snapshot, *, query=None, provider=None):
    """The INTERNAL candidate-selection stage of ``search_models``.

    Preconditions: ``snapshot`` is an already-VALIDATED public snapshot, and
    ``query``/``provider`` are ALREADY validated -- ``query`` is ``None`` or a
    string and ``provider`` is ``None`` or one exact ``provider_order`` key.
    The public argument envelope (including ``unknown_provider``) belongs to the
    transport-level search unit; this stage never validates, repairs or reports
    on its arguments, and never reads a clock, the filesystem or the network.

    Returns a NEW list of immutable ``(provider_key, raw_model_id)`` tuples in
    the frozen endpoint order: ``provider_order`` precedence, then the accepted
    :func:`natural_key` raw-ID order within each provider. The result is
    UNPAGINATED and carries no page cap, so a later sort-then-slice stage sees
    the complete match set; a raw ID shared by two providers stays TWO distinct
    pairs, and a selected zero-ID provider yields no pairs.

    ``provider`` scopes to exactly that one key when supplied, else every
    ``provider_order`` key. ``query`` is a LITERAL, UNTRIMMED substring
    compared separately against that pair's raw ID, name and non-``None``
    description after the shared ASCII-only fold (A-Z to a-z; every other code
    point -- including a non-ASCII uppercase letter -- stays literal). There is
    no regular expression, fuzzy alias, marker stripping, JSON/metadata
    serialization or capability/price/branding inference: capability arrays,
    wiring URLs, provider keys and object key names are never
    searched. ``None`` or ``""`` applies no text constraint; a whitespace-only
    query is a literal constraint. A pair with no matching text field --
    including one whose only candidate field is a ``null`` description -- is
    excluded, and one pair's fields are never borrowed from a sibling provider.

    The inputs are never mutated and no snapshot metadata is aliased: only the
    two identity strings are returned, so a later row projection works from
    those identities rather than from a cross-provider metadata union.
    """
    order = snapshot["provider_order"]
    providers = snapshot["providers"]
    selected = [provider] if provider is not None else list(order)
    needle = _ascii_lower(query) if query else None
    pairs = []
    for provider_key in selected:
        entry = providers[provider_key]
        models = entry["models"]
        for raw_id in sorted(entry["model_ids"], key=natural_key):
            if needle is not None:
                model = models[raw_id]
                fields = (raw_id, model["name"], model["description"])
                if not any(field is not None and needle in _ascii_lower(field)
                           for field in fields):
                    continue
            pairs.append((provider_key, raw_id))
    return pairs


# -- pure capability AND stage: every requested token on the same pair's field --


def _search_capability_pairs(snapshot, pairs, *, field, requested):
    """The INTERNAL, field-parametrised capability AND stage of ``search_models``.

    ``field`` is one normalized capability name -- ``"input_modalities"`` or
    ``"supported_parameters"`` -- selecting the SAME pair's projected array
    through :func:`catalog.normalize_capabilities`. The other capability field,
    output modalities, names, descriptions, raw ids and provider keys are
    never substitutes, a capability name is never substring-matched, and a
    pair's capability is never borrowed from a sibling provider sharing its raw
    id: endpoint identity is exactly ``(provider, raw_model_id)``.

    Preconditions: ``snapshot`` is an already-VALIDATED public snapshot,
    ``pairs`` is an ordered sequence of immutable ``(provider_key, raw_model_id)``
    tuples identifying tracked pairs of that snapshot (the ALREADY selected
    candidate set, never re-derived or expanded here), and ``requested`` is
    ``None`` or an ALREADY-validated list of nonempty strings. This stage never
    validates its arguments, normalizes in place, or reads a clock, filesystem,
    roster or network.

    Returns a NEW list of only those supplied pairs whose OWN selected field is
    KNOWN (not ``None``) and contains EVERY ASCII-folded requested token -- an
    AND over the requested entries on the SAME pair, so a duplicated request is
    idempotent and request order is irrelevant. Matching folds A-Z to a-z on
    both sides (the reported array and the request); non-ASCII code points and
    whitespace stay literal, so a padded token matches only an identically
    padded reported token and a non-ASCII case pair never collapses.

    ``requested`` ``None`` or ``[]`` applies NO constraint and returns a fresh
    list with EVERY supplied pair unchanged -- including pairs whose field is
    unknown (``null``) or known-empty (``[]``); a nonempty request retains
    neither. There is no sort, deduplication or pagination: incoming pair order
    and duplicates are preserved exactly, the supplied selection is never
    expanded, and more than the default page's worth of eligible pairs stay
    complete. The snapshot, input sequence and request list are never mutated.
    """
    if not requested:
        return list(pairs)
    needles = [_ascii_lower(token) for token in requested]
    providers = snapshot["providers"]
    kept = []
    for provider_key, raw_id in pairs:
        model = providers[provider_key]["models"][raw_id]
        available = normalize_capabilities(model)[field]
        if available is not None and all(token in available
                                         for token in needles):
            kept.append((provider_key, raw_id))
    return kept


# -- pure public metadata search: validated arguments + sorted/page projection --


#: The frozen public `search_models` argument signature, in validation order.
#: Validation walks THIS tuple after the whole-object and unknown-key checks,
#: so precedence is independent of the caller's dict insertion order.
_SEARCH_SIGNATURE = ("query", "provider", "min_context_length",
                     "input_modalities", "supported_parameters", "limit",
                     "offset")

#: The frozen pagination defaults and inclusive maximum.
_SEARCH_DEFAULT_LIMIT = 5
_SEARCH_DEFAULT_OFFSET = 0
_SEARCH_MAX_LIMIT = 200


def _search_safe_integer(value):
    """The PUBLIC tool-argument integer normalization: a strict non-negative
    safe int, else ``None``.

    A JSON ``integer`` argument is a finite numeric value with zero fractional
    part in ``0..MAX_SAFE_INT`` -- so the numeric ``1.0`` IS the integer ``1``
    and normalizes to a native ``int``. A ``bool`` is rejected BEFORE the int
    branch (``True`` is never ``1``), and a numeric string, a fraction, a
    non-finite float and an unsafe/huge native integer are rejected too. Huge
    native ints are range-checked directly, and non-finite floats are rejected
    BEFORE any ``int()`` conversion, so neither can overflow or crash.

    This is the TOOL-INPUT boundary rule only; it never relaxes the strict-int
    snapshot/context projection, which stays a separate strict check.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        integer = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        integer = int(value)
    else:
        return None
    if 0 <= integer <= MAX_SAFE_INT:
        return integer
    return None


def _search_nonempty_string_list(value):
    """The PUBLIC capability-array rule: a LIST whose EVERY member is a
    nonempty string. ``[]`` is accepted; a bare string, tuple, object or any
    non-string/empty-string member rejects the WHOLE parameter -- never a
    partial salvage. Casing, padding and duplicates stay literal."""
    return (isinstance(value, list)
            and all(isinstance(item, str) and item for item in value))


def search_models(snapshot, arguments):
    """The pure core of the frozen ``search_models`` tool.

    ``snapshot`` is an already-VALIDATED public snapshot object -- the
    ``load_snapshot`` / source / adapter boundary owns raw, structural and hash
    validation, so this function performs NO validation, NO hash recompute, NO
    I/O and NO clock/environment/auth read. ``arguments`` is the whole DECODED
    argument object (a JSON object), passed explicitly rather than splatted as
    keyword arguments.

    Argument validation is deterministic and in the frozen order: the argument
    must be a JSON object; then ANY unknown key is rejected (``parameter``
    ``None``); then the frozen signature order -- ``query``, ``provider``,
    ``min_context_length``, ``input_modalities``, ``supported_parameters``,
    ``limit``, ``offset`` -- stopping at the FIRST error regardless of the
    input dict's insertion order. ``query`` is omitted/``null`` or any string
    (kept literally, never trimmed); ``provider`` is omitted/``null`` or one
    exact ``provider_order`` key (a non-string is ``invalid_arguments``, an
    absent string -- including empty, padded or wrong-case -- is
    ``unknown_provider``, and a registered zero-ID provider is valid);
    ``min_context_length`` is omitted/``null`` or a safe integer numeric;
    the two capability arrays are omitted/``null`` or lists of nonempty
    strings; ``limit`` is omitted (5) or an integer 1..200 (explicit ``null``
    is invalid); ``offset`` is omitted (0) or a safe integer (explicit ``null``
    is invalid). A numeric ``1.0`` is accepted as the integer value while a
    bool, numeric string, fraction, unsafe integer or non-finite number is
    rejected as ``invalid_arguments`` with its own parameter name.

    Only after EVERY argument validates does it compose the accepted stages in
    order -- :func:`_search_text_provider_pairs`,
    :func:`_search_min_context_pairs` and the shared
    :func:`_search_capability_pairs` twice (first ``input_modalities``, then
    ``supported_parameters``) -- so each capability is tested on its own
    ``(provider, raw model_id)`` pair and active filters combine with AND.
    ``total_matches`` is counted AFTER all filters and BEFORE the single
    ``offset:offset + limit`` slice; pagination is never an earlier filter,
    never a per-provider operation and never capped by a stage.

    The success payload carries exactly ``ok``, ``snapshot_id``, ``tick_epoch``,
    ``provider_staleness``, ``total_matches``, ``next_offset`` and
    ``endpoints``. Filters, page size/offset and matched text are never echoed;
    ``ok`` is exactly ``True`` even when nothing matches. ``provider_staleness``
    covers every ``provider_order`` key when ``provider`` is omitted, else
    exactly the selected provider. ``endpoints`` is the returned page of flat
    summary rows -- provider, raw id, display name and the four normalized
    capability values; description, wiring and ``metadata`` belong to
    ``get_model`` details only. ``next_offset`` is ``offset + len(endpoints)``
    only when that is LESS than ``total_matches``, else ``None``.

    Every returned container, array and row is freshly allocated, so a caller
    mutating a result can never corrupt the snapshot, the argument object or
    another call. The inputs are never mutated.
    """
    if not isinstance(arguments, dict):
        return _argument_error(_INVALID_ARGUMENTS, None)
    if any(key not in _SEARCH_SIGNATURE for key in arguments):
        return _argument_error(_INVALID_ARGUMENTS, None)

    query = arguments.get("query")
    if query is not None and not isinstance(query, str):
        return _argument_error(_INVALID_ARGUMENTS, "query")

    provider = arguments.get("provider")
    if provider is not None and not isinstance(provider, str):
        return _argument_error(_INVALID_ARGUMENTS, "provider")
    order = snapshot["provider_order"]
    providers = snapshot["providers"]
    if provider is not None and provider not in order:
        return _argument_error(_UNKNOWN_PROVIDER, "provider")

    raw_min_context = arguments.get("min_context_length")
    min_context_length = None
    if raw_min_context is not None:
        min_context_length = _search_safe_integer(raw_min_context)
        if min_context_length is None:
            return _argument_error(_INVALID_ARGUMENTS, "min_context_length")

    requested_arrays = {}
    for name in ("input_modalities", "supported_parameters"):
        value = arguments.get(name)
        if value is not None and not _search_nonempty_string_list(value):
            return _argument_error(_INVALID_ARGUMENTS, name)
        requested_arrays[name] = value

    limit = _SEARCH_DEFAULT_LIMIT
    if "limit" in arguments:
        limit = _search_safe_integer(arguments["limit"])
        if limit is None or not 1 <= limit <= _SEARCH_MAX_LIMIT:
            return _argument_error(_INVALID_ARGUMENTS, "limit")

    offset = _SEARCH_DEFAULT_OFFSET
    if "offset" in arguments:
        offset = _search_safe_integer(arguments["offset"])
        if offset is None:
            return _argument_error(_INVALID_ARGUMENTS, "offset")

    matches = _search_text_provider_pairs(
        snapshot, query=query, provider=provider)
    matches = _search_min_context_pairs(
        snapshot, matches, min_context_length=min_context_length)
    matches = _search_capability_pairs(
        snapshot, matches, field="input_modalities",
        requested=requested_arrays["input_modalities"])
    matches = _search_capability_pairs(
        snapshot, matches, field="supported_parameters",
        requested=requested_arrays["supported_parameters"])

    total_matches = len(matches)
    page = matches[offset:offset + limit]

    endpoints = [
        _endpoint_summary(provider_key, raw_id,
                          providers[provider_key]["models"][raw_id])
        for provider_key, raw_id in page
    ]

    next_offset = offset + len(endpoints)
    if next_offset >= total_matches:
        next_offset = None

    selected = [provider] if provider is not None else list(order)

    return {
        "ok": True,
        "snapshot_id": snapshot["snapshot_id"],
        "tick_epoch": snapshot["tick_epoch"],
        "provider_staleness": {key: providers[key]["stale"]
                               for key in selected},
        "total_matches": total_matches,
        "next_offset": next_offset,
        "endpoints": endpoints,
    }
