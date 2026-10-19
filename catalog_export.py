"""Producer-side in-memory adapter: one captured active registry plus one
captured roster observation assembled into ONE validated public snapshot object.

This is the minimal local-to-public adapter for the standalone catalogue (unit
2i). It is a PURE function boundary: :func:`build_snapshot` performs no I/O, no
file, environment, network, clock or credential access, and it imports ONLY the
accepted pure :mod:`watchdog_catalog.catalog` module (plus ``__future__``). The
consumer package and its query module NEVER import this producer.

The two captured inputs are described as a boundary, not a config loader:

* ``registry`` -- a list or tuple of provider RECORDS in display order. Only the
  already-RESOLVED members ``key``, ``name``, ``chat_completions_url`` and
  ``api_type`` are ever read; every other member (``_token``, ``auth``,
  ``base_url``, ``enabled``, ``_stem``, private objects, ...) is dropped and
  never inspected or serialized. "Active only" is a CALLER precondition: the
  caller excludes disabled records before capture, and this adapter neither
  infers enablement nor rebuilds keys or URLs.
* ``roster`` -- one captured roster observation. ``roster['providers']`` maps
  every provider key to its raw tracked ID list; ``roster['provider_models']``
  is the raw per-gateway model metadata; ``roster['tick_epoch']`` and
  ``roster['stale_providers']`` carry the observation time and staleness.

Structural faults (a non-``dict`` record, a bad or duplicate resolved key, a
non-string or empty wiring member, a malformed roster provider map, a malformed
ID list, an invalid cadence) fail with the accepted frozen
:class:`watchdog_catalog.catalog.CatalogError` (``invalid_snapshot``) and its
literal message -- never a repaired, coerced or partially built snapshot and
never an incidental ``TypeError``/``KeyError`` from hashing. Only the ROOT
``registry``/``roster`` argument types are programming errors (``TypeError``).

The returned object is sealed with the accepted
:func:`watchdog_catalog.catalog.snapshot_hash` and handed to the accepted
:func:`watchdog_catalog.catalog.validate_snapshot` self-check, which returns the
SAME object. Every mutable container in the result is freshly allocated and
unaliased from either captured input, and neither input is ever mutated -- on
success OR on failure. Output adaptation back to the legacy local shape (raw
metadata restoration, one-read roster retention) is a LATER unit; this adapter
returns the public snapshot only and never puts raw objects into it.
"""

from __future__ import annotations

from watchdog_catalog import catalog

__all__ = ["build_snapshot"]

#: The frozen warning vocabulary, in its deduplicated publication ORDER. The
#: accepted validator re-enforces exactly this order on the sealed snapshot.
_WARNING_ORDER = ("tick_epoch_unknown", "metadata_missing", "metadata_invalid")

#: Fixed, non-reflective programming-error messages. They never interpolate the
#: offending input, so a hostile value can never echo through an exception.
_REGISTRY_ROOT_MESSAGE = (
    "build_snapshot() requires a registry list or tuple of provider records")
_ROSTER_ROOT_MESSAGE = (
    "build_snapshot() requires a captured roster mapping")


def _is_strict_int(value):
    """A JSON integer token: a ``bool`` is NOT an integer here."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_safe_int(value):
    """A strict positive integer inside the JSON-safe range."""
    return _is_strict_int(value) and 0 < value <= catalog.MAX_SAFE_INT


def _is_nonnegative_safe_int(value):
    """A strict non-negative integer inside the JSON-safe range; ``0`` is known."""
    return _is_strict_int(value) and 0 <= value <= catalog.MAX_SAFE_INT


def _fail():
    """Raise the single frozen structural fault, with no input interpolation."""
    raise catalog.CatalogError(catalog.INVALID_SNAPSHOT)


def _read_record(record):
    """Read ONLY the four already-resolved members of one registry record.

    Returns ``(key, display_name, url, api_type)``. The key must be a nonempty
    string (case, padding and any literal characters preserved). A missing,
    empty or non-string ``name`` falls back to the key. The wiring members must
    be nonempty strings; their SYNTAX is left to the accepted final validator,
    so no second URL parser lives here. Every other record member is dropped and
    never traversed.
    """
    if not isinstance(record, dict):
        _fail()
    key = record.get("key")
    if not isinstance(key, str) or not key:
        _fail()
    name = record.get("name")
    if not isinstance(name, str) or not name:
        name = key
    url = record.get("chat_completions_url")
    if not isinstance(url, str) or not url:
        _fail()
    api_type = record.get("api_type")
    if not isinstance(api_type, str) or not api_type:
        _fail()
    return key, name, url, api_type


def _validate_roster_providers(roster_providers):
    """Validate EVERY roster provider entry BEFORE any filtering.

    Each key must be a nonempty string and each value a list of nonempty
    strings -- including entries for unknown or disabled providers, which are
    validated and then excluded from the assembled output. This is STRICTER than
    the legacy page builder on empty-string IDs and provider keys, because the
    accepted public validator cannot represent them; a malformed excluded entry
    therefore fails the export rather than silently vanishing.
    """
    for key, ids in roster_providers.items():
        if not isinstance(key, str) or not key:
            _fail()
        if not isinstance(ids, list):
            _fail()
        for raw_id in ids:
            if not isinstance(raw_id, str) or not raw_id:
                _fail()


def _pair_metadata(outer, key, raw_id):
    """The effective raw model container for one tracked ``(provider, id)`` pair.

    Implements the frozen metadata hierarchy: an absent or explicit-``None``
    outer ``provider_models`` is missing (``None``); an outer non-null non-dict
    is the ACTUAL malformed container; an absent/``None`` provider key is
    missing; a non-null non-dict provider container is the actual malformed
    container; and the per-model member is returned verbatim, so the accepted
    :func:`catalog.project_model` alone classifies missing, malformed and valid
    fragments. No sentinel, warning override or private object is manufactured.
    """
    if not isinstance(outer, dict):
        # Absent/None -> missing; any other non-dict -> the actual bad container.
        return None if outer is None else outer
    provider = outer.get(key)
    if provider is None:
        return None
    if not isinstance(provider, dict):
        return provider
    return provider.get(raw_id)


def build_snapshot(registry, roster, *, cadence_s):
    """Assemble and validate the public snapshot from captured in-memory inputs.

    ``registry`` is a list or tuple of already-resolved provider records in
    display order; ``roster`` is one captured roster observation. ``cadence_s``
    is a REQUIRED keyword-only strict positive JSON-safe integer (``bool``
    excluded); an invalid cadence is a structural fault, never a silent
    default. Returns ONLY the validated public snapshot object.

    Raises ``TypeError`` for a non-list/tuple ``registry`` or a non-dict
    ``roster`` (programming errors, checked before any content) and
    :class:`catalog.CatalogError` (``invalid_snapshot``) for every structural
    fault. Neither input is mutated, on success or on failure.
    """
    if not isinstance(registry, (list, tuple)):
        raise TypeError(_REGISTRY_ROOT_MESSAGE)
    if not isinstance(roster, dict):
        raise TypeError(_ROSTER_ROOT_MESSAGE)
    if not _is_positive_safe_int(cadence_s):
        _fail()

    roster_providers = roster.get("providers")
    if not isinstance(roster_providers, dict):
        # Direct raise (not `_fail`) so a static checker narrows the map type.
        raise catalog.CatalogError(catalog.INVALID_SNAPSHOT)
    _validate_roster_providers(roster_providers)

    outer_models = roster.get("provider_models")

    stale_raw = roster.get("stale_providers")
    stale_set = ({name for name in stale_raw if isinstance(name, str)}
                 if isinstance(stale_raw, list) else set())

    tick = roster.get("tick_epoch")
    tick_epoch = tick if _is_nonnegative_safe_int(tick) else None

    warnings = set()
    if tick_epoch is None:
        warnings.add("tick_epoch_unknown")

    provider_order = []
    providers = {}
    seen_keys = set()
    for record in registry:
        key, display_name, url, api_type = _read_record(record)
        if key in seen_keys:
            _fail()
        seen_keys.add(key)
        provider_order.append(key)

        # Correctly-typed input IDs are NORMALISED by literal code-point
        # sorted(set(...)); only the OUTPUT is canonical. A missing active key
        # yields a retained zero-ID provider, never a dropped one.
        model_ids = sorted(set(roster_providers.get(key, [])))
        models = {}
        for raw_id in model_ids:
            model, model_warnings = catalog.project_model(
                raw_id, _pair_metadata(outer_models, key, raw_id))
            models[raw_id] = model
            warnings.update(model_warnings)

        providers[key] = {
            "display_name": display_name,
            "chat_completions_url": url,
            "api_type": api_type,
            "model_ids": model_ids,
            "models": models,
            "stale": key in stale_set,
        }

    snapshot = {
        "schema_version": catalog.SCHEMA_VERSION,
        "tick_epoch": tick_epoch,
        "cadence_s": cadence_s,
        "provider_order": provider_order,
        "providers": providers,
        "warnings": [code for code in _WARNING_ORDER if code in warnings],
    }
    # Seal with the accepted reference digest (excluding only the root id), then
    # hand the SAME object to the accepted structural self-check.
    snapshot["snapshot_id"] = catalog.snapshot_hash(snapshot)
    return catalog.validate_snapshot(snapshot)
