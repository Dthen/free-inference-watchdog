"""Pure, stdlib-only free-marker query helpers.

This module is the single source of truth for the marker-insensitive free-marker
stripper: it imports only :mod:`re` and performs no I/O, configuration loading,
credential resolution or SDK import. ``build_site.strip_free_marker`` and
``mcp_server.strip_free_marker`` re-export this exact function object rather
than carrying a second copy.
"""

import copy
import re

from watchdog_catalog.catalog import _ascii_lower, normalize_capabilities

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


# -- pure get-model query: literal membership + separate marker endpoints --

#: The explicit ASCII blank set the required ``model_id`` predicate uses:
#: U+0009..U+000D (tab, LF, VT, FF, CR) and U+0020 (space). It is NEVER widened
#: with Python's default ``str.strip`` / ``str.isspace``, which also match
#: Unicode whitespace (NBSP U+00A0, EM SPACE U+2003, ...) and would wrongly
#: reject a literal non-ASCII query.
_ASCII_BLANKS = " \t\n\r\v\f"

#: The frozen absence caveat carried on EVERY ``get_model`` success payload --
#: including a no-match success. It is a labelling caution, never a proof of
#: cross-gateway model identity.
_ABSENCE_CAVEAT = (
    "Cross-gateway ABSENCE is unreliable: gateways rename inconsistently, so a "
    "missing exact id does NOT prove the model is unavailable there."
)


def get_model(snapshot, arguments):
    """The pure core of the frozen ``get_model`` tool.

    ``snapshot`` is an already-VALIDATED public snapshot object -- the
    ``load_snapshot`` / source / adapter boundary owns raw, structural and hash
    validation, so this function performs NO validation, NO hash recompute, NO
    I/O and NO clock read. ``arguments`` is the whole DECODED argument object (a
    JSON object), passed explicitly rather than splatted as keyword arguments.

    Returns the frozen success payload directly -- never raising into the
    semantic caller for a malformed argument. The payload carries exactly
    ``ok``, ``query``, ``snapshot_id``, ``tick_epoch``, ``exact_matches``,
    ``exact_gateways``, ``provider_staleness``, ``endpoints``, ``caveats`` and
    ``counts``.

    ``exact_matches`` is LITERAL raw-ID membership -- case-sensitive, unpadded,
    exact -- one entry per snapshot provider in ``provider_order`` including
    zero-ID providers; ``exact_gateways`` lists the matching providers in that
    display order. ``endpoints`` is the SEPARATE marker-insensitive lookup:
    every tracked ``(provider, raw model_id)`` pair whose
    :func:`strip_free_marker` equals the stripped query, with raw ids unchanged
    and metadata kept attached to its own pair. Endpoints order by provider
    display order then the accepted :func:`natural_key` raw-ID order. A marker
    match is never proof of cross-gateway identity.

    Argument validation is deterministic and in the frozen order: the argument
    must be a JSON object; then ANY unknown key is rejected (``parameter``
    ``None``); then the required ``model_id`` must be a string containing at
    least one character outside the explicit ASCII blank set -- empty or
    ASCII-blank-only strings are ``invalid_arguments`` (``parameter``
    ``model_id``). The retained identifier is never trimmed, case-folded or
    repaired; the ASCII blank set is never widened to Unicode whitespace.

    Every returned container, array and matched endpoint metadata object (with
    its nested architecture/parameter/pricing containers) is freshly allocated
    (the metadata values are deep-copied), so a caller mutating a result can
    never corrupt the snapshot or another call. The inputs are never mutated and
    the caveat list is fresh on every success.
    """
    if not isinstance(arguments, dict):
        return _argument_error(_INVALID_ARGUMENTS, None)
    if any(key not in ("model_id",) for key in arguments):
        return _argument_error(_INVALID_ARGUMENTS, None)
    query = arguments.get("model_id")
    if not isinstance(query, str) or not query.strip(_ASCII_BLANKS):
        return _argument_error(_INVALID_ARGUMENTS, "model_id")

    order = snapshot["provider_order"]
    providers = snapshot["providers"]
    marker_query = strip_free_marker(query)

    exact_matches = {}
    exact_gateways = []
    endpoints = []
    for provider in order:
        entry = providers[provider]
        matched = query in entry["model_ids"]
        exact_matches[provider] = matched
        if matched:
            exact_gateways.append(provider)
        for raw_id in sorted(entry["model_ids"], key=natural_key):
            if strip_free_marker(raw_id) == marker_query:
                endpoints.append({
                    "provider": provider,
                    "model_id": raw_id,
                    "chat_completions_url": entry["chat_completions_url"],
                    "api_type": entry["api_type"],
                    "stale": entry["stale"],
                    "metadata": copy.deepcopy(entry["models"][raw_id]),
                })

    return {
        "ok": True,
        "query": query,
        "snapshot_id": snapshot["snapshot_id"],
        "tick_epoch": snapshot["tick_epoch"],
        "exact_matches": exact_matches,
        "exact_gateways": exact_gateways,
        "provider_staleness": {p: providers[p]["stale"] for p in order},
        "endpoints": endpoints,
        "caveats": [_ABSENCE_CAVEAT],
        "counts": {
            "exact_matches": len(exact_gateways),
            "endpoints": len(endpoints),
            "distinct_raw_ids": len({e["model_id"] for e in endpoints}),
            "marker_group_labels": len({strip_free_marker(e["model_id"])
                                        for e in endpoints}),
        },
    }


# -- pure endpoint-list query: selected-scope endpoints + provider-filtered counts --


def list_endpoints(snapshot, arguments):
    """The pure core of the frozen ``list_endpoints`` tool.

    ``snapshot`` is an already-VALIDATED public snapshot object -- the
    ``load_snapshot`` / source / adapter boundary owns raw, structural and hash
    validation, so this function performs NO validation, NO hash recompute, NO
    I/O and NO clock read. ``arguments`` is the whole DECODED argument object (a
    JSON object), passed explicitly rather than splatted as keyword arguments.

    Returns the frozen success payload directly -- never raising into the
    semantic caller for a malformed argument. The payload carries exactly
    ``ok``, ``gateways``, ``counts``, ``provider``, ``snapshot_id``,
    ``tick_epoch`` and ``provider_staleness`` for BOTH the all-provider and the
    provider-filtered variant.

    ``gateways`` maps each SELECTED provider key -- all ``provider_order`` keys
    when ``provider`` is omitted or ``null``, else exactly the one known
    selected provider, a registered zero-ID provider included -- to the exact
    gateway entry ``chat_completions_url``, ``api_type``, ``model_ids`` and
    ``models``. ``model_ids`` is that provider's complete raw ID array in the
    accepted :func:`natural_key` order (unchanged strings, never marker labels),
    and ``models`` maps every tracked raw ID to its complete projected public
    model object -- never the legacy raw metadata and never a merged
    cross-provider object.

    ``counts`` carries exactly ``gateways`` (the number of selected providers,
    zero-ID providers included), ``endpoints`` (selected ``(provider, raw ID)``
    pairs), ``distinct_raw_ids`` (selected literal raw strings),
    ``marker_group_labels`` (selected delimiter-stripped labels using the frozen
    :func:`strip_free_marker` guard) and ``models`` -- the documented legacy
    alias equal to ``marker_group_labels``, never a verified underlying model
    identity. No registry-wide total leaks into a filtered response, and a
    zero-ID single provider yields ``gateways`` 1 with every other count zero.

    Argument validation is deterministic and in the frozen order: the argument
    must be a JSON object; then ANY unknown key is rejected (``parameter``
    ``None``); then a non-``null`` provider that is not a string is
    ``invalid_arguments``; then a string absent from ``provider_order`` is
    ``unknown_provider`` (empty, padded and wrong-case keys are never repaired).

    Every returned container, gateway entry, raw-ID array and selected model
    object (with its nested architecture/parameter/pricing containers) is
    freshly allocated (the model values are deep-copied), so a caller mutating a
    result can never corrupt the snapshot or another call. The inputs are never
    mutated.
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
    selected = [provider] if provider is not None else list(order)

    gateways = {}
    endpoints = 0
    distinct_raw_ids = set()
    marker_group_labels = set()
    for key in selected:
        entry = providers[key]
        model_ids = sorted(entry["model_ids"], key=natural_key)
        gateways[key] = {
            "chat_completions_url": entry["chat_completions_url"],
            "api_type": entry["api_type"],
            "model_ids": model_ids,
            "models": copy.deepcopy({raw_id: entry["models"][raw_id]
                                     for raw_id in entry["model_ids"]}),
        }
        for raw_id in entry["model_ids"]:
            endpoints += 1
            distinct_raw_ids.add(raw_id)
            marker_group_labels.add(strip_free_marker(raw_id))
    labels = len(marker_group_labels)
    return {
        "ok": True,
        "gateways": gateways,
        "counts": {
            "gateways": len(selected),
            "endpoints": endpoints,
            "distinct_raw_ids": len(distinct_raw_ids),
            "marker_group_labels": labels,
            "models": labels,
        },
        "provider": provider,
        "snapshot_id": snapshot["snapshot_id"],
        "tick_epoch": snapshot["tick_epoch"],
        "provider_staleness": {key: providers[key]["stale"]
                               for key in selected},
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
    identities. This scope deliberately differs from search's page-scoped
    counts. ``metadata_coverage`` reports coverage of the stated metadata fields
    over the global tracked-pair denominator (exactly ``endpoints``,
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
    pricing, wiring URLs, provider keys and object key names are never
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


# -- pure input-modality AND stage: every requested token on the same pair --


def _search_input_modality_pairs(snapshot, pairs, *, input_modalities=None):
    """The INTERNAL input-modality AND stage of ``search_models``.

    Preconditions: ``snapshot`` is an already-VALIDATED public snapshot and
    ``pairs`` is an ordered sequence of immutable ``(provider_key, raw_model_id)``
    tuples identifying tracked pairs of that snapshot -- it is the ALREADY
    selected candidate set (typically from :func:`_search_text_provider_pairs`
    composed with :func:`_search_min_context_pairs`), never re-derived here.
    ``input_modalities`` is ``None`` or an ALREADY-validated list of nonempty
    strings. The public decoded argument is validated and normalized by the
    later transport-level search unit before this stage is invoked; this stage
    never validates its argument, invents a temporary error, or normalizes the
    request or the snapshot in place.

    Returns a NEW list of only those supplied pairs whose OWN projected model
    ``architecture.input_modalities`` is KNOWN (not ``None``) and contains EVERY
    ASCII-folded requested token -- an AND over the requested entries, never a
    multiplicity constraint, so a duplicated request is idempotent. Matching is
    ASCII-only (A-Z to a-z) on BOTH sides: the reported array through the
    accepted :func:`catalog.normalize_capabilities` (which folds and
    first-occurrence deduplicates while keeping a known ``[]`` distinct from
    ``None``) and the requested tokens through the shared
    :func:`catalog._ascii_lower`. Non-ASCII code points and whitespace stay
    literal, so a padded token matches only an identically padded reported
    token and a non-ASCII case pair never collapses. Output modalities, names,
    descriptions, raw ids, provider keys, pricing and supported parameters are
    never substitutes for an input modality, and a pair's capability is never
    borrowed from a sibling provider sharing its raw id: endpoint identity is
    exactly ``(provider, raw model_id)``.

    ``input_modalities`` ``None`` or ``[]`` applies NO constraint and returns a
    fresh list with EVERY supplied pair unchanged -- including pairs whose input
    array is unknown (``null``) or known-empty (``[]``), which are retained here
    but fail any nonempty request. There is no truthiness test of the request or
    of the array, no wildcard for an unknown array, no sort, no deduplication, no
    roster/config read and no pagination: the incoming pair order is preserved
    exactly, the supplied selection is never expanded, and more than the default
    page's worth of eligible pairs stay complete.

    The snapshot, the input pair sequence and the requested list are never
    mutated, and only immutable identity tuples are returned, so a caller
    mutating the result can never corrupt the snapshot, the input sequence or
    another call.
    """
    if not input_modalities:
        return list(pairs)
    requested = [_ascii_lower(token) for token in input_modalities]
    providers = snapshot["providers"]
    kept = []
    for provider_key, raw_id in pairs:
        model = providers[provider_key]["models"][raw_id]
        available = normalize_capabilities(model)["input_modalities"]
        if available is not None and all(token in available
                                         for token in requested):
            kept.append((provider_key, raw_id))
    return kept


# -- pure supported-parameter AND stage: every requested token on the same pair --


def _search_supported_parameter_pairs(snapshot, pairs, *,
                                      supported_parameters=None):
    """The INTERNAL supported-parameter AND stage of ``search_models``.

    Preconditions: ``snapshot`` is an already-VALIDATED public snapshot and
    ``pairs`` is an ordered sequence of immutable ``(provider_key, raw_model_id)``
    tuples identifying tracked pairs of that snapshot -- it is the ALREADY
    selected candidate set (typically from
    :func:`_search_input_modality_pairs`, itself composed after the accepted
    text/provider and minimum-context stages), never re-derived here.
    ``supported_parameters`` is ``None`` or an ALREADY-validated list of nonempty
    strings. The public decoded argument is validated and normalized by the later
    transport-level search unit before this stage is invoked; this stage never
    validates its argument, invents a temporary error, or normalizes the request
    or the snapshot in place.

    Returns a NEW list of only those supplied pairs whose OWN projected model
    ``supported_parameters`` is KNOWN (not ``None``) and contains EVERY
    ASCII-folded requested token -- an AND over the requested entries on the same
    pair, never a multiplicity constraint, so a duplicated request is
    idempotent. Matching is ASCII-only (A-Z to a-z) on BOTH sides: the reported
    array through the accepted :func:`catalog.normalize_capabilities` (which
    folds and first-occurrence deduplicates while keeping a known ``[]``
    distinct from ``None``) and the requested tokens through the shared
    :func:`catalog._ascii_lower`. Non-ASCII code points and whitespace stay
    literal, so a padded token matches only an identically padded reported token
    and a non-ASCII case pair never collapses. Input and output modalities,
    names, descriptions, raw ids, provider keys, pricing and pricing/object names
    are never substitutes for a supported parameter, a capability name is never
    substring-matched, and a pair's parameters are never borrowed from a sibling
    provider sharing its raw id: endpoint identity is exactly
    ``(provider, raw_model_id)``.

    ``supported_parameters`` ``None`` or ``[]`` applies NO constraint and
    returns a fresh list with EVERY supplied pair unchanged -- including pairs
    whose parameter array is unknown (``null``) or known-empty (``[]``), which
    are retained here but fail any nonempty request. There is no truthiness test
    of the request or of the array, no wildcard for an unknown array, no sort, no
    deduplication, no roster/config read and no pagination: the incoming pair
    order is preserved exactly, the supplied selection is never expanded, and
    more than the default page's worth of eligible pairs stay complete.

    The snapshot, the input pair sequence and the requested list are never
    mutated, and only immutable identity tuples are returned, so a caller
    mutating the result can never corrupt the snapshot, the input sequence or
    another call.
    """
    if not supported_parameters:
        return list(pairs)
    requested = [_ascii_lower(token) for token in supported_parameters]
    providers = snapshot["providers"]
    kept = []
    for provider_key, raw_id in pairs:
        model = providers[provider_key]["models"][raw_id]
        available = normalize_capabilities(model)["supported_parameters"]
        if available is not None and all(token in available
                                         for token in requested):
            kept.append((provider_key, raw_id))
    return kept
