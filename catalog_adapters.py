"""Reusable catalogue seam — pure format adapters, stdlib only.

One fetcher (`providers.fetch_provider`) owns URL selection, timeout, the
injected getter, the User-Agent, ignored ids and the passive rate-limit
headers. This module owns only the part that differs between gateways: the
envelope/identity shape, the advertised-price rule and the ephemeral metadata
view handed to the accepted strict projector.

P3/P4/P5 register six formats, all actually implemented:

* ``openai-models`` — the legacy shape. Its envelope/identity parsing stays in
  ``providers.py`` exactly as it always was; this module only answers
  registry questions and passes raw metadata through, so a config with no
  ``catalog_format`` keeps the accepted legacy behaviour byte for byte.
* ``input-output-models`` — the Vercel AI Gateway family: a ``data`` list of
  ``type == "language"`` rows with ``pricing.input``/``pricing.output``.
* ``channel-catalog`` — the Felo platform family: a ``data.models`` list with a
  strict ``data.total``, per-channel runnability and documented discount
  arithmetic closed over every published tier branch.
* ``newapi-pricing`` — the TokenRA (New API lineage) family: a successful
  ``data`` list whose rows carry the literal identity ``model_name`` (no
  ``id``), a ``quota_type`` billing discriminator and per-token/per-call rates.
* ``provider-models`` — the Infron family: a successful ``data`` list of LLM
  rows whose model-level minima AND every nested per-provider offer must be a
  known zero, because the published minima describe an available offer rather
  than the route a bare request takes.
* ``typed-unit-models`` — the AI/ML API family: one row per ENDPOINT FAMILY
  keyed by the literal canonical ``id``. Only ``openai/chat-completions`` rows
  are selected (before duplicate comparison), and admission needs a present
  ``pricing`` block with ``kind == "fixed"``, ``variable is False`` and a
  nonempty COMPLETE charge-unit set — every unit's finite numeric price
  exactly zero, with a positive billing denominator and no contradictory unit
  flag — plus the same test on every context threshold. ``variable``,
  ``variants``, nonempty dimensions, schedule-bearing pricing and every other
  unimplemented price class are valid nonqualifying observations, never
  assumed zero.

Later gateway slices extend :data:`REGISTERED_FORMATS`; nothing here is a
placeholder rule and no JSON-path/query/metadata mapping language exists. A
family added to the registry without a branch in :func:`is_free_row` faults
loudly rather than reporting a healthy empty catalogue.

Two rules run through every new-format predicate:

* **Known zero or nothing.** A price must be an explicit finite non-negative
  number, parsed exactly through :class:`decimal.Decimal` so a tiny positive
  string (``"1e-400"``) can never round to zero. ``bool``, containers,
  malformed strings and non-finite values are structural faults.
* **Fail closed on structure, nonqualify on semantics.** A malformed required
  envelope/id/pricing/tier container raises :class:`AdapterError` (the fetcher
  turns it into ``FetchError``, so last-known ids are carried forward). A
  documented unpriced, variable-priced or paid branch is merely not free. A
  price modifier this format does not understand is nonqualifying, never
  assumed zero.

The metadata view is a FRESH container: the raw upstream record is never
mutated, aliased or rewritten, and remains what the roster persists.
"""

from __future__ import annotations

import json
import math
import urllib.parse
from decimal import Decimal, InvalidOperation
from typing import NoReturn

__all__ = [
    "AdapterError",
    "CHANNEL_CATALOG_FORMAT",
    "INPUT_OUTPUT_FORMAT",
    "LEGACY_FORMAT",
    "NEWAPI_PRICING_FORMAT",
    "PROVIDER_MODELS_FORMAT",
    "REGISTERED_FORMATS",
    "TYPED_UNIT_MODELS_FORMAT",
    "identity_field",
    "is_free_row",
    "is_legacy_format",
    "is_registered_format",
    "metadata_view",
    "parse_catalog",
    "selected_identity",
    "url_origin",
]

#: The pre-existing list/id/raw-metadata shape; absent `catalog_format` means
#: this one, and its parsing stays in ``providers.py``.
LEGACY_FORMAT = "openai-models"
#: Vercel AI Gateway: `data[]`, exact string `id`, `type == "language"`,
#: `pricing.input`/`pricing.output` plus the published rate modifiers.
INPUT_OUTPUT_FORMAT = "input-output-models"
#: Felo platform: `data.models[]` with a strict `data.total`, per-channel
#: `runnable`/`pricing_mode` and exact `standard_pricing` x discount rules.
CHANNEL_CATALOG_FORMAT = "channel-catalog"
#: TokenRA (New API lineage): `data[]` rows whose only identity is the literal
#: `model_name` (there is no `id`), priced per token by a zero `model_ratio`
#: plus a zero per-call `model_price`.
NEWAPI_PRICING_FORMAT = "newapi-pricing"
#: Infron: a successful `data[]` list of LLM rows keyed by literal `id`, with
#: model-level `min_*` minima over a nested `providers[]` offer set. Admission
#: requires the whole advertised provider route — not just the minimum — to be
#: a known zero.
PROVIDER_MODELS_FORMAT = "provider-models"
#: AI/ML API: a ``data`` list of endpoint rows keyed by literal ``id``, where
#: only ``openai/chat-completions`` rows are selected and a ``fixed``
#: ``variable: false`` block must publish a complete all-zero charge-unit set
#: (base and every context threshold) with positive billing denominators.
TYPED_UNIT_MODELS_FORMAT = "typed-unit-models"

#: The formats this slice ACTUALLY implements. Later slices extend this tuple.
REGISTERED_FORMATS = (LEGACY_FORMAT, INPUT_OUTPUT_FORMAT, CHANNEL_CATALOG_FORMAT,
                      NEWAPI_PRICING_FORMAT, PROVIDER_MODELS_FORMAT,
                      TYPED_UNIT_MODELS_FORMAT)

#: Distinguishes an absent member from an explicit JSON ``null``.
_MISSING = object()

#: Modifier containers whose fragments partially override the containing
#: prices (only the axes a fragment names are overridden).
_OVERRIDE_CONTAINERS = ("service_tiers", "regional")

#: Modifier containers that itemise the ordinary input/output rates.
_TIER_AXES = ("input_tiers", "output_tiers")

#: Bounded recursion: a hostile/degenerate nesting is unsupported, not free.
_MAX_MODIFIER_DEPTH = 8

#: The ONE `peak_pricing` shape the frozen capture publishes
#: (`particle/particle-1`): a multiplier plus a list of windows. Any other
#: member could be a rate override or an additive fee this format does not
#: define, so it makes the row nonqualifying rather than being ignored.
_PEAK_KEYS = frozenset({"multiplier", "windows"})

#: The observed window vocabulary, derived from the same capture. A window with
#: a different key set has unknown applicability: unsupported, not free.
_PEAK_WINDOW_KEYS = frozenset({"start_minute_utc", "end_minute_utc"})


class AdapterError(Exception):
    """A structural or malformed-value fault in a catalogue body.

    ``providers.fetch_provider`` converts it to the accepted ``FetchError`` so
    a broken feed is sticky carry-forward rather than an empty success.
    """


class _SourceFloat(float):
    """A JSON float that remembers the exact numeric literal it was parsed from.

    ``json.loads`` turns a JSON number such as ``1e-400`` into ``0.0`` before
    any adapter can inspect it. The channel-catalog parser installs this hook
    (via ``parse_float``) so a tiny positive price can still be compared
    exactly; the value stays a ``float`` for every other consumer, including
    the raw row persisted in state.
    """

    __slots__ = ("literal",)

    def __new__(cls, literal):
        value = super().__new__(cls, literal)
        value.literal = literal
        return value


def is_registered_format(value):
    """True for a format name this checkout actually implements."""
    return isinstance(value, str) and value in REGISTERED_FORMATS


def is_legacy_format(value):
    """True for the legacy shape: the format name itself or an absent field."""
    return value is None or value == LEGACY_FORMAT


#: The ONE identity member each new format publishes. Legacy stays absent from
#: this table because its accepted `row["id"]` expression (including its
#: KeyError/TypeError behaviour) lives in ``providers.py`` untouched.
_IDENTITY_FIELDS = {
    INPUT_OUTPUT_FORMAT: "id",
    CHANNEL_CATALOG_FORMAT: "id",
    NEWAPI_PRICING_FORMAT: "model_name",
    PROVIDER_MODELS_FORMAT: "id",
    TYPED_UNIT_MODELS_FORMAT: "id",
}


def identity_field(catalog_format):
    """The literal identity member for a new format, or ``None`` if it is legacy.

    This is a bounded table, not a mapping language: a family either publishes
    ``id`` or the New API ``model_name``. The legacy shape answers ``None`` and
    keeps its own accepted extraction.
    """
    return _IDENTITY_FIELDS.get(catalog_format)


def selected_identity(row, catalog_format):
    """The identity one catalogue row is tracked under.

    The SAME selection feeds new-format admission ids, duplicate validation and
    the raw-metadata keys, so a row cannot be admitted under ``model_name`` and
    stored under an ``id`` it does not have. Legacy rows return exactly the
    accepted ``row["id"]`` expression; new-format identities were already
    validated as nonempty literal strings by :func:`_selected_rows`.
    """
    if is_legacy_format(catalog_format):
        return row["id"]
    return row[identity_field(catalog_format)]


def url_origin(url):
    """``(scheme, host, effective port)`` for an absolute URL, else ``None``.

    Scheme and host are ASCII-lowercased for COMPARISON only; the URL bytes a
    config carries are never rewritten. An explicit port wins, otherwise the
    scheme default applies, so ``https://h:443/`` and ``https://h/`` share one
    origin. Userinfo is not part of an origin (config validation rejects it).
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    if port is None:
        port = {"https": 443, "http": 80}.get(parts.scheme.lower())
    return (parts.scheme.lower(), parts.hostname.lower(), port)


def _fail(detail) -> NoReturn:
    raise AdapterError(f"malformed catalogue {detail}")


# ---------- numeric parsing (exact, non-negative, finite) ----------

def _price(value):
    """Return the exact non-negative finite :class:`Decimal` for ``value``.

    Accepts JSON integers, finite floats and numeric strings; a string is
    parsed by ``Decimal`` so ``"1e-400"`` stays a positive number instead of
    underflowing to ``0.0``. Rejects booleans (``True`` is not a price),
    containers, padded/blank/malformed strings, negatives and non-finite
    values with :class:`AdapterError`.
    """
    if isinstance(value, bool):
        _fail("price value (boolean is not a number)")
    if isinstance(value, int):
        price = Decimal(value)
    elif isinstance(value, float):
        literal = getattr(value, "literal", None)
        if literal is not None:
            # Exact source spelling from `_SourceFloat`; never re-render the
            # already-underflowed float.
            try:
                price = Decimal(literal)
            except InvalidOperation as exc:
                raise AdapterError("malformed catalogue price value") from exc
        else:
            if not math.isfinite(value):
                _fail("price value (non-finite)")
            price = Decimal(repr(value))
    elif isinstance(value, str):
        if not value or value != value.strip():
            _fail("price value (blank or padded string)")
        try:
            price = Decimal(value)
        except InvalidOperation as exc:
            raise AdapterError("malformed catalogue price value") from exc
    else:
        _fail("price value (expected a number or numeric string)")
    if not price.is_finite():
        _fail("price value (non-finite)")
    if price < 0:
        _fail("price value (negative)")
    return price


# ---------- the input/output price rule ----------

def _merged(inherited, fragment):
    """Partial override: the fragment wins only on the axes it names."""
    merged = dict(inherited)
    merged.update(fragment)
    return merged


def _tiers_are_known_zero(value):
    """Every published ``{cost,min,max?}`` tier must carry a known zero cost.

    An absent or explicit-null ladder means the gateway publishes no ladder and
    is allowed; a non-list container, a non-object tier or a non-zero/malformed
    cost is a fault, and a tier without a known cost is simply not free.
    """
    if value is _MISSING or value is None:
        return True
    if not isinstance(value, list):
        _fail("price tier container")
    for tier in value:
        if not isinstance(tier, dict):
            _fail("price tier")
        cost = tier.get("cost", _MISSING)
        if cost is _MISSING or cost is None:
            return False
        if _price(cost) != 0:
            return False
    return True


def _peak_modifier_is_a_known_zero_safe_shape(peak):
    """The observed ``{multiplier, windows[]}`` form over proven-zero rates.

    A known finite non-negative multiplier over a proven zero rate stays zero,
    so such a modifier cannot hide a charge. A missing/invalid multiplier, a
    non-object modifier or a malformed window list is an unsupported shape:
    the row is nonqualifying, never assumed free.

    Only `multiplier` and `windows` are supported. A window must contain
    exactly `start_minute_utc` and `end_minute_utc`. Extra fields on either
    object might represent new charges, so they disqualify the row.
    """
    if not isinstance(peak, dict):
        return False
    if set(peak) - _PEAK_KEYS:
        return False
    multiplier = peak.get("multiplier", _MISSING)
    if multiplier is _MISSING or multiplier is None:
        return False
    try:
        _price(multiplier)
    except AdapterError:
        return False
    windows = peak.get("windows", _MISSING)
    if windows is not _MISSING and windows is not None:
        if not isinstance(windows, list):
            return False
        for window in windows:
            if not isinstance(window, dict):
                return False
            if set(window) != _PEAK_WINDOW_KEYS:
                return False
    return True


def _prices_are_zero(fragment, inherited, depth=0):
    """True iff every effective ordinary chat rate and nested modifier is zero.

    ``fragment`` is a price container and ``inherited`` the containing prices a
    fragment partially overrides. Scalars and tier ladders are evaluated on the
    merged view; nested override containers are read from this fragment only,
    so recursion always descends into published structure.
    """
    if depth > _MAX_MODIFIER_DEPTH:
        return False
    if not isinstance(fragment, dict):
        _fail("pricing container")
    effective = _merged(inherited, fragment)

    # `varies_by_provider` is an UNAMBIGUOUSNESS flag, not a price: only its
    # exact absence or exact JSON false leaves the summary price meaningful.
    if "varies_by_provider" in effective:
        if effective["varies_by_provider"] is not False:
            return False

    for axis in ("input", "output"):
        value = effective.get(axis, _MISSING)
        if value is _MISSING or value is None:
            return False
        if _price(value) != 0:
            return False

    request = effective.get("request", _MISSING)
    if (request is not _MISSING and request is not None
            and _price(request) != 0):
        return False

    for axis in _TIER_AXES:
        if not _tiers_are_known_zero(effective.get(axis, _MISSING)):
            return False

    for container in _OVERRIDE_CONTAINERS:
        if container not in fragment:
            continue
        branches = fragment[container]
        if branches is None:
            continue
        if not isinstance(branches, dict):
            _fail("pricing modifier container")
        for branch in branches.values():
            if not isinstance(branch, dict):
                _fail("pricing modifier")
            if not _prices_are_zero(branch, effective, depth + 1):
                return False

    if "peak_pricing" in fragment and fragment["peak_pricing"] is not None:
        if not _peak_modifier_is_a_known_zero_safe_shape(
                fragment["peak_pricing"]):
            return False
    return True


# ---------- the Felo channel price rule ----------
#
# Felo publishes one `standard_pricing` object per channel plus a documented
# `discount_multiplier`. Admission requires EVERY runnable channel to have a
# known zero effective ordinary chat price, so a free primary cannot fall over
# into a paid fallback through routing the monitor does not represent. Optional
# cache/tool axes are preserved raw and are not ordinary input/output charges.

_FELO_TIER_KEYS = frozenset({"conditions", "overrides"})


def _felo_effective(rate, multiplier):
    """Exact effective rate after ONE documented discount multiplication.

    An explicit zero rate is zero whatever the multiplier. A positive rate
    with a missing/null multiplier is unknown, not free. Any present malformed
    multiplier is a structural fault, even over a zero rate.
    """
    if rate is _MISSING or rate is None:
        return None
    base = _price(rate)
    if multiplier is _MISSING or multiplier is None:
        return Decimal(0) if base == 0 else None
    return base * _price(multiplier)


def _felo_tier_axis(tier_overrides, pricing, axis):
    if axis in tier_overrides:
        return tier_overrides[axis]
    return pricing.get(axis, _MISSING)


def _felo_tiers_are_zero(pricing, multiplier, mode):
    """Check every potentially applicable `tiers[]` branch.

    Conditions are deliberately not evaluated: any published branch may apply,
    so each one inherits the axes it omits from the base and must prove zero
    under the same discount arithmetic. Unknown tier shapes are nonqualifying;
    malformed required containers fault.
    """
    tiers = pricing.get("tiers", _MISSING)
    if tiers is _MISSING or tiers is None:
        return True
    if not isinstance(tiers, list):
        _fail("tier container")
    for tier in tiers:
        if not isinstance(tier, dict):
            _fail("tier")
        if set(tier) - _FELO_TIER_KEYS:
            return False
        conditions = tier.get("conditions", _MISSING)
        if conditions is not _MISSING and conditions is not None:
            if not isinstance(conditions, list):
                _fail("tier conditions container")
            for condition in conditions:
                if not isinstance(condition, dict):
                    _fail("tier condition")
        overrides = tier.get("overrides", _MISSING)
        if overrides is _MISSING or overrides is None:
            return False
        if not isinstance(overrides, dict):
            _fail("tier overrides container")
        if mode == "token":
            for axis in ("prompt", "completion"):
                if _felo_effective(
                        _felo_tier_axis(overrides, pricing, axis),
                        multiplier) != 0:
                    return False
            fee = _felo_tier_axis(overrides, pricing, "request")
            if fee is not _MISSING and fee is not None:
                if _felo_effective(fee, multiplier) != 0:
                    return False
        else:
            fee = _felo_tier_axis(overrides, pricing, "request")
            if fee is _MISSING or fee is None:
                return False
            if _felo_effective(fee, multiplier) != 0:
                return False
    return True


def _felo_runnable_channel_is_zero(channel):
    """Known zero ordinary chat price for one runnable channel."""
    if channel.get("pricing_mode") != "configured_rates":
        # upstream_cost channels publish null standard pricing: unpriced, not
        # zero. They are still runnable, so they disqualify the whole row.
        return False
    pricing = channel.get("standard_pricing", _MISSING)
    if pricing is _MISSING or pricing is None:
        return False
    if not isinstance(pricing, dict):
        _fail("standard_pricing container")
    multiplier = channel.get("discount_multiplier", _MISSING)
    billing = pricing.get("billing_kind", _MISSING)
    if billing == "token":
        for axis in ("prompt", "completion"):
            if _felo_effective(pricing.get(axis, _MISSING),
                               multiplier) != 0:
                return False
        fee = pricing.get("request", _MISSING)
        # A null/absent per-request fee on the documented token shape means no
        # extra fee, NOT that a missing token rate becomes zero.
        if fee is not _MISSING and fee is not None:
            if _felo_effective(fee, multiplier) != 0:
                return False
        return _felo_tiers_are_zero(pricing, multiplier, "token")
    if billing == "call":
        fee = pricing.get("request", _MISSING)
        if fee is _MISSING or fee is None:
            return False
        if _felo_effective(fee, multiplier) != 0:
            return False
        return _felo_tiers_are_zero(pricing, multiplier, "call")
    return False


def _felo_row_is_free(row):
    """Scope + ALL-runnable-channel zero-price rule for one Felo row."""
    if not isinstance(row, dict):
        return False
    if row.get("deprecated") is not False:
        return False
    if row.get("model_kind") != "chat":
        return False
    outputs = row.get("output_modalities", _MISSING)
    if not isinstance(outputs, list) or "text" not in outputs:
        return False
    channels = row.get("channels", _MISSING)
    if channels is _MISSING or channels is None:
        return False
    if not isinstance(channels, list):
        _fail("channels container")
    runnable = []
    for channel in channels:
        if not isinstance(channel, dict):
            _fail("channel")
        if channel.get("runnable") is True:
            runnable.append(channel)
    if not runnable:
        return False
    return all(_felo_runnable_channel_is_zero(channel)
               for channel in runnable)


# ---------- the TokenRA (New API) price rule ----------
#
# TokenRA publishes a `data` list of pricing rows and NO `id`: the identity is
# the literal `model_name`. `quota_type` is the billing-mode discriminator
# (0 = per token, 1 = per request), `model_ratio` the per-token base multiplier
# and `model_price` the absolute per-call charge. `completion_ratio`,
# `cache_ratio`, `create_cache_ratio` and `audio_ratio` are multipliers ON that
# base, so over a proven zero base they cannot create a charge, while a
# per-resolution `video_resolution_prices` map IS an independent absolute
# charge and is therefore closed over.
#
# The frozen public group is `default`: the captured root publishes
# `auto_groups ["default"]`, `group_ratio {"default": 1}` and a `usable_group`
# entry for `default`, and every row carries `enable_groups ["default"]`. This
# is NOT a configurable group/routing language — a row is admitted only when
# the public default group can reach it, and no private per-account membership
# is inferred.

#: The one public group of the frozen capture (see the block comment above).
_NEWAPI_PUBLIC_GROUP = "default"
#: The Chat Completions endpoint label. Video rows ALSO declare it, so it is a
#: necessary but never a sufficient modality signal.
_NEWAPI_CHAT_ENDPOINT = "openai"
#: The only modality hint the feed publishes (sparse: string or list).
_NEWAPI_VIDEO_TAG = "video"


def _newapi_rate(value):
    """Exact non-negative :class:`Decimal` for a REQUIRED rate, or ``None``.

    An absent or explicit-null member is UNKNOWN/unpriced and is therefore not
    zero — it never qualifies a row by omission. A present value that is not a
    JSON number (``bool``, a numeric string, a container) is MALFORMED: this
    format publishes numbers only, so accepting ``"0"`` would invent a shape
    the feed does not use. ``_price`` keeps the exact source literal, so a raw
    ``1e-400`` stays a positive number instead of underflowing to zero.
    """
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("price value (expected a JSON number)")
    return _price(value)


def _newapi_billing_mode(row):
    """``quota_type`` as a strict integer, or ``None`` when unknown.

    ``bool`` is rejected outright (``True == 1`` would silently read as a
    per-request row and ``False == 0`` as a per-token row). A missing or null
    discriminator is unknown, not zero. Any other integer is returned verbatim
    so the caller can reject a non-token billing mode.
    """
    value = row.get("quota_type", _MISSING)
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("quota_type (expected an integer)")
    return value


def _newapi_member_list(row, member):
    """A published string-list member (groups/endpoints), or ``None`` if unknown.

    Unknown means absent or explicit null: reachability cannot be established,
    so the row is nonqualifying rather than faulting. A present non-list
    container or a non-string/empty entry is malformed and faults.
    """
    value = row.get(member, _MISSING)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, list):
        _fail(f"{member} container")
    for entry in value:
        if not isinstance(entry, str) or not entry:
            _fail(f"{member} entry")
    return value


def _newapi_tags_are_chat(tags):
    """False for a known video tag; True when no video tag is published.

    The published hint is a bare string on the capture (``"video"``, ``"glm"``,
    ``"kimi"``), and a list is tolerated. The comparison is case-insensitive on
    the TAG only (an exclusion, never a positive chat classifier), so an
    unknown tag stays non-disqualifying. A malformed container faults.
    """
    if tags is _MISSING or tags is None:
        return True
    if isinstance(tags, str):
        return tags.strip().lower() != _NEWAPI_VIDEO_TAG
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, str) or not tag:
                _fail("tags entry")
            if tag.strip().lower() == _NEWAPI_VIDEO_TAG:
                return False
        return True
    _fail("tags container")


def _newapi_video_map_is_known_zero(value):
    """Every present per-resolution absolute charge must be a known zero.

    An absent or explicit-null map means "not applicable" and is allowed — it
    never supplies the required token rates, which are checked independently.
    A non-mapping container, a non-string/empty resolution or a non-numeric
    charge is malformed; any positive charge disqualifies the row.
    """
    if value is _MISSING or value is None:
        return True
    if not isinstance(value, dict):
        _fail("video_resolution_prices container")
    for resolution, charge in value.items():
        if not isinstance(resolution, str) or not resolution:
            _fail("video_resolution_prices resolution")
        if isinstance(charge, bool) or not isinstance(charge, (int, float)):
            _fail("video_resolution_prices charge")
        if _price(charge) != 0:
            return False
    return True


def _newapi_row_is_free(row):
    """Billing mode + zero token/call rates + public group + chat endpoint.

    All three of `quota_type == 0`, a zero `model_ratio` and a zero
    `model_price` are required: the real per-call image rows carry a zero ratio
    with `quota_type 1` and a positive `model_price`, so a ratio-only predicate
    would admit them. Video tags and positive per-resolution charges are
    rejected as separate absolute charges.
    """
    if not isinstance(row, dict):
        return False
    if _newapi_billing_mode(row) != 0:
        return False
    for axis in ("model_ratio", "model_price"):
        rate = _newapi_rate(row.get(axis, _MISSING))
        if rate is None or rate != 0:
            return False
    groups = _newapi_member_list(row, "enable_groups")
    if groups is None or _NEWAPI_PUBLIC_GROUP not in groups:
        return False
    endpoints = _newapi_member_list(row, "supported_endpoint_types")
    if endpoints is None or _NEWAPI_CHAT_ENDPOINT not in endpoints:
        return False
    if not _newapi_tags_are_chat(row.get("tags", _MISSING)):
        return False
    return _newapi_video_map_is_known_zero(
        row.get("video_resolution_prices", _MISSING))


# ---------- the Infron provider-price rule ----------
#
# Infron publishes a `data` list of rows keyed by a literal `id`. Each row
# carries model-level minima (`min_prompt_price` / `min_completion_price` /
# `min_request_price`) AND a nested `providers[]` array of per-provider offers
# with their own `prompt_price` / `completion_price` / `request_price`. The
# minima are exactly the minimum over the nested offers (checked on all 555
# captured rows, 0 mismatches), so a zero minimum says "at least one provider
# offers it at zero", NOT "the route is free". Infron's default routing
# load-balances on uptime, price, throughput and latency together, so a bare
# request can be served — and billed — on any advertised provider. Admission
# therefore requires EVERY advertised provider to be a known zero instead of
# assuming the default lands on the cheapest one, and `:free` is evidence, not
# a rule: an unsuffixed zero-priced launch with the same complete evidence is
# admitted, while the real paid TTS id containing "free" is not.
#
# Required token rates are NUMBERS in this format (`int`/`float` only). An
# absent or explicit-null required rate is UNKNOWN and never zero; a present
# non-number is MALFORMED and faults. The request axes are OPTIONAL structure:
# the gateway omits them when a per-request charge does not apply, so absence is
# not-applicable — but a PRESENT null or malformed value is unknown, which
# cannot qualify, and is deliberately not a fault (plan §4.4's specific
# optional-request policy, distinct from plan §5's malformed-required rule).

_INFRON_CATEGORY = "LLM"
_INFRON_CHAT_ENDPOINT = "openai"
_INFRON_TEXT_OUTPUT = "text"


def _infron_rate(value):
    """Exact non-negative :class:`Decimal` for a REQUIRED rate, or ``None``.

    An absent or explicit-null member is UNKNOWN/unpriced and therefore not
    zero — it never qualifies a row by omission. A present value that is not a
    JSON number (``bool``, a numeric string, a container) is MALFORMED: this
    format publishes numbers only, so accepting ``"0"`` would invent a shape the
    feed does not use. ``_price`` keeps the exact source literal, so a raw
    ``1e-400`` stays a positive number instead of underflowing to zero.
    """
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("price value (expected a JSON number)")
    return _price(value)


def _infron_fee(value):
    """Classify an OPTIONAL per-request axis: absent, unknown, or exact.

    ``"absent"`` is the documented not-applicable case. Every present value that
    is not a usable non-negative finite number — ``null``, boolean, numeric
    string, container, negative, non-finite — is ``"unknown"``: it is neither a
    numeric zero nor a structural fault, so the row is merely nonqualifying.
    That is plan §4.4's OPTIONAL-request policy, deliberately different from
    §5's malformed-REQUIRED rule (which faults). Everything else is the exact
    :class:`Decimal` charge.
    """
    if value is _MISSING:
        return "absent"
    if (value is None or isinstance(value, bool)
            or not isinstance(value, (int, float))):
        return "unknown"
    try:
        return _price(value)
    except AdapterError:
        return "unknown"


def _infron_fee_is_zero_or_absent(value):
    """True only when a per-request charge is absent or a known zero."""
    fee = _infron_fee(value)
    return fee == "absent" or fee == 0


def _infron_member_list(row, member):
    """A published string-list member (endpoints/modalities), or ``None``.

    Unknown means absent or explicit null: the scope cannot be established, so
    the row is nonqualifying rather than faulting. A present non-list container
    or a non-string/empty entry is malformed and faults.
    """
    value = row.get(member, _MISSING)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, list):
        _fail(f"{member} container")
    for entry in value:
        if not isinstance(entry, str) or not entry:
            _fail(f"{member} entry")
    return value


def _infron_row_is_free(row):
    """Scope + model minima + the WHOLE advertised provider route at zero.

    The nested-provider requirement is what separates this family from a
    minima-only rule: twenty of the twenty-two captured rows with both model
    minima zero are video routes charging $0.04-$0.14 per request, and a
    mixed paid/free provider set would let a bare request reach the paid offer.
    """
    if not isinstance(row, dict):
        return False
    if row.get("category_type") != _INFRON_CATEGORY:
        return False
    if row.get("deprecated") is not False:
        return False
    if row.get("is_display_only") is not False:
        return False
    endpoints = _infron_member_list(row, "supported_endpoint_types")
    if endpoints is None or _INFRON_CHAT_ENDPOINT not in endpoints:
        return False
    outputs = _infron_member_list(row, "output_modalities")
    if outputs is None or _INFRON_TEXT_OUTPUT not in outputs:
        return False
    for axis in ("min_prompt_price", "min_completion_price"):
        rate = _infron_rate(row.get(axis, _MISSING))
        if rate is None or rate != 0:
            return False
    if not _infron_fee_is_zero_or_absent(row.get("min_request_price", _MISSING)):
        return False
    providers = row.get("providers", _MISSING)
    if providers is _MISSING or providers is None:
        return False
    if not isinstance(providers, list):
        _fail("providers container")
    if not providers:
        return False
    for provider in providers:
        if not isinstance(provider, dict):
            _fail("provider")
        for axis in ("prompt_price", "completion_price"):
            rate = _infron_rate(provider.get(axis, _MISSING))
            if rate is None or rate != 0:
                return False
        if not _infron_fee_is_zero_or_absent(
                provider.get("request_price", _MISSING)):
            return False
    return True


# ---------- the AI/ML API typed-unit price rule ----------
#
# AI/ML API publishes one row per ENDPOINT FAMILY (the same canonical id can
# appear under Anthropic Messages, OpenAI Responses and OpenAI Chat
# Completions), so selection MUST happen before duplicate comparison. Only the
# Chat Completions family is tracked here; a Responses-only or preprocessing
# row is never published as an OpenAI Chat Completions endpoint.
#
# `pricing.kind` is the documented discriminator: `fixed` quotes one rate set,
# `variants` quotes per parameter combination and `variable` "cannot be quoted
# ahead of the request". A `variable` block's zeros are therefore NOT a free
# price; they are the absence of a quote. Admission requires a present pricing
# block, `kind == "fixed"`, `variable is False`, a nonempty complete unit set
# and every published unit price exactly zero — including call/generation
# fees, because zero token rates alone do not make a per-call product free.
# Every context threshold replaces the base rate, so each threshold needs its
# own nonempty complete zero unit set; there is no free-under-a-ceiling rule.
#
# Required prices/denominators are NUMBERS in this format (the capture
# publishes `int`/`float`, never numeric strings). An absent or explicit-null
# price/denominator is UNKNOWN/unpriced and never zero; a present boolean,
# string, container, negative or non-finite value is MALFORMED and faults the
# observation. Empty dimensions/variants and an ABSENT schedule are the
# supported fixed class; a PRESENT-null thresholds/dimensions/variants/schedule
# is unknown and nonqualifying, and nonempty variants/dimensions or any
# schedule-bearing block are valid nonqualifying classes, never assumed zero.

#: The one endpoint family this tracker publishes.
_TYPED_UNIT_CHAT_TYPE = "openai/chat-completions"
#: A context threshold's documented shape is exactly `{from, units}`. Any
#: sibling member could be an unread charge override, so the shape is closed:
#: an unknown member makes the row nonqualifying, never assumed zero.
_TYPED_UNIT_THRESHOLD_KEYS = frozenset({"from", "units"})


def _typed_unit_rate(value):
    """Exact non-negative :class:`Decimal` for a REQUIRED numeric member, or None.

    Absent/``null`` is unknown (nonqualifying). A present value that is not a
    JSON number (``bool``, numeric string, container) is a structural fault:
    this format publishes numbers only, so accepting ``"0"`` would invent a
    shape the feed does not use. ``_price`` keeps the exact source literal, so
    a raw ``1e-400`` stays positive instead of underflowing to zero.
    """
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("price value (expected a JSON number)")
    return _price(value)


def _typed_unit_denominator(value):
    """Exact positive billing denominator, or ``None`` when unknown.

    A missing/``null`` denominator makes the unit incomplete (nonqualifying).
    A present non-number is malformed; zero is not a valid billing unit.
    """
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("billing denominator (expected a JSON number)")
    denominator = _price(value)
    if denominator == 0:
        _fail("billing denominator (must be positive)")
    return denominator


def _typed_unit_is_zero(unit):
    """True iff one published charge unit is complete and exactly zero-priced.

    Every unit in the list is read, whatever its ``name`` (token, call,
    generation, ...). The pricing block's flag is authoritative, but a unit
    that explicitly says ``variable: true`` contradicts a fixed quote and is
    therefore nonqualifying; a present non-boolean flag is malformed.
    """
    if not isinstance(unit, dict):
        _fail("charge unit")
    price = _typed_unit_rate(unit.get("price", _MISSING))
    denominator = _typed_unit_denominator(unit.get("per", _MISSING))
    if "variable" in unit:
        flag = unit["variable"]
        if not isinstance(flag, bool):
            _fail("charge unit variable flag")
        if flag is True:
            return False
    if price is None or denominator is None:
        return False
    return price == 0


def _typed_unit_thresholds_are_zero(pricing):
    """Every context threshold needs a valid shape and a complete zero set.

    An absent ``thresholds`` member is the supported no-tier class. A PRESENT
    ``null`` is unknown (it may hide an unread tier list), so it is
    nonqualifying rather than treated as absent.
    """
    thresholds = pricing.get("thresholds", _MISSING)
    if thresholds is _MISSING:
        return True
    if thresholds is None:
        return False
    if not isinstance(thresholds, list):
        _fail("threshold container")
    for threshold in thresholds:
        if not isinstance(threshold, dict):
            _fail("threshold")
        if set(threshold) - _TYPED_UNIT_THRESHOLD_KEYS:
            return False
        start = threshold.get("from", _MISSING)
        if (isinstance(start, bool) or not isinstance(start, int)
                or start < 0):
            _fail("threshold from")
        units = threshold.get("units", _MISSING)
        if units is _MISSING or units is None:
            _fail("threshold units container")
        if not isinstance(units, list):
            _fail("threshold units container")
        if not units:
            return False
        if not all(_typed_unit_is_zero(unit) for unit in units):
            return False
    return True


def _typed_unit_pricing_is_zero(pricing):
    """The fixed, variable-false, complete all-zero unit rule."""
    if not isinstance(pricing, dict):
        _fail("pricing container")
    kind = pricing.get("kind", _MISSING)
    if not isinstance(kind, str) or not kind:
        # `kind` is the documented discriminator and every captured row
        # publishes it. A missing/null/boolean/numeric kind is a malformed
        # required pricing structure, not a semantic nonqualifying class.
        _fail("pricing kind")
    if kind != "fixed":
        return False
    if pricing.get("variable") is not False:
        return False
    units = pricing.get("units", _MISSING)
    if units is _MISSING or units is None:
        _fail("units container")
    if not isinstance(units, list):
        _fail("units container")
    if not units:
        return False
    if not all(_typed_unit_is_zero(unit) for unit in units):
        return False
    for member in ("dimensions", "variants"):
        value = pricing.get(member, _MISSING)
        if value is _MISSING:
            continue
        if value is None:
            # Present-null is unknown, not an empty dimension/variant list.
            return False
        if not isinstance(value, list):
            _fail(f"{member} container")
        if value:
            return False
    if "schedule" in pricing:
        # Absent means no schedule; any PRESENT value (including null) is
        # schedule-bearing/unknown and therefore not a supported zero class.
        return False
    return _typed_unit_thresholds_are_zero(pricing)


def _typed_unit_row_is_free(row):
    """Chat-endpoint scope + the whole fixed all-zero charge-unit set."""
    if not isinstance(row, dict):
        return False
    if row.get("type") != _TYPED_UNIT_CHAT_TYPE:
        # Non-chat products (preprocessing, images, speech, video, ...) are
        # not free chat entries and their pricing is never inspected.
        return False
    pricing = row.get("pricing", _MISSING)
    if pricing is _MISSING or pricing is None:
        # A server silently ignoring include=pricing must fail the fetch, not
        # look like a healthy empty roster.
        _fail("pricing structure")
    return _typed_unit_pricing_is_zero(pricing)


# ---------- envelope and identity ----------

def _json_loads(body, *, exact_floats):
    try:
        if exact_floats:
            return json.loads(body, parse_float=_SourceFloat)
        return json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdapterError("catalogue response was not valid JSON") from exc


def _number_key(value):
    """Exact comparison key for one JSON number.

    ``_SourceFloat`` carries the original numeric spelling, so the key is the
    exact ``Decimal`` constructed from that literal -- never the already
    underflowed binary float. Plain floats (the default parser, or a
    hand-built row) use their exact shortest representation. ``bool`` is
    excluded by the caller so ``True`` cannot meet numeric ``1``.
    """
    if isinstance(value, int):
        return ("number", Decimal(value))
    if isinstance(value, _SourceFloat):
        try:
            return ("number", Decimal(value.literal))
        except InvalidOperation:
            return ("number-token", value.literal)
    if isinstance(value, float):
        if math.isnan(value):
            return ("number-nan",)
        if math.isinf(value):
            return ("number-infinity", value > 0)
        return ("number", Decimal(repr(value)))
    if isinstance(value, Decimal):
        if value.is_nan():
            return ("number-nan",)
        return ("number", value)
    raise TypeError(f"not a JSON number: {type(value).__name__}")


def _json_equivalent(left, right):
    """Exact structural equality for duplicate raw records.

    JSON types stay distinct (a boolean is not a number, a string is not a
    number) and numbers compare numerically without float rounding: ``0`` and
    ``0.0`` are equivalent, while ``0`` and a positive subnormal literal
    ``1e-400`` are not. Dict key order and list order follow JSON semantics
    (object members are unordered, arrays are ordered).
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return (isinstance(left, bool) and isinstance(right, bool)
                and left is right)
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, dict) or isinstance(right, dict):
        if not (isinstance(left, dict) and isinstance(right, dict)):
            return False
        if set(left) != set(right):
            return False
        return all(_json_equivalent(left[key], right[key]) for key in left)
    if isinstance(left, list) or isinstance(right, list):
        if not (isinstance(left, list) and isinstance(right, list)):
            return False
        return (len(left) == len(right)
                and all(_json_equivalent(a, b)
                        for a, b in zip(left, right)))
    if isinstance(left, str) or isinstance(right, str):
        return (isinstance(left, str) and isinstance(right, str)
                and left == right)
    numeric = (int, float, Decimal)
    if isinstance(left, numeric) or isinstance(right, numeric):
        if not (isinstance(left, numeric) and isinstance(right, numeric)):
            return False
        return _number_key(left) == _number_key(right)
    return type(left) is type(right) and left == right


def _selected_rows(rows, catalog_format, *, row_filter=None):
    """Literal-id rows, deduplicated only when the records are equivalent.

    Equivalence is exact at the JSON boundary (see :func:`_json_equivalent`):
    ordinary Python equality would merge a JSON boolean with 0/1 and an
    underflowed source number with zero, turning a conflicting duplicate into
    a source-order choice. The identity member comes from
    :func:`identity_field`, so `newapi-pricing` deduplicates by `model_name`
    exactly as it admits and stores by `model_name`. ``row_filter`` runs AFTER
    the row-container check and BEFORE identity/duplicate handling, so a family
    that publishes one row per endpoint can discard other endpoint families
    without treating their repeated ids as conflicting records.
    """
    if not isinstance(rows, list):
        _fail("envelope")
    field = identity_field(catalog_format)
    parsed = []
    seen = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail("row")
        if row_filter is not None and not row_filter(row):
            continue
        model_id = row.get(field)
        if not isinstance(model_id, str) or not model_id:
            _fail("model id")
        if model_id in seen:
            if not _json_equivalent(seen[model_id], row):
                _fail("duplicate model id (conflicting records)")
            continue
        seen[model_id] = row
        parsed.append(row)
    return parsed


def _parse_channel_catalog(body):
    """Felo: `data.models` with a strict `data.total` equal to its length."""
    payload = _json_loads(body, exact_floats=True)
    if not isinstance(payload, dict):
        _fail("envelope")
    if payload.get("success") is False:
        _fail("envelope (success is false)")
    data = payload.get("data")
    if not isinstance(data, dict):
        _fail("envelope")
    total = data.get("total", _MISSING)
    # Strict nonnegative integer, bool excluded (True == 1 would otherwise
    # pass). No pagination is documented, so a mismatch is a truncated or
    # fabricated success and must fail rather than look healthy.
    if (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        _fail("data.total")
    rows = data.get("models")
    if not isinstance(rows, list):
        _fail("envelope")
    if total != len(rows):
        _fail("data.total (does not match the returned model list)")
    return _selected_rows(rows, CHANNEL_CATALOG_FORMAT)


def _parse_newapi_pricing(body):
    """TokenRA: a successful `data` list keyed by the literal `model_name`."""
    payload = _json_loads(body, exact_floats=True)
    if not isinstance(payload, dict):
        _fail("envelope")
    if payload.get("success") is False:
        _fail("envelope (success is false)")
    return _selected_rows(payload.get("data"), NEWAPI_PRICING_FORMAT)


def _parse_provider_models(body):
    """Infron: a successful `data` list keyed by the literal row `id`."""
    payload = _json_loads(body, exact_floats=True)
    if not isinstance(payload, dict):
        _fail("envelope")
    if payload.get("success") is False:
        _fail("envelope (success is false)")
    return _selected_rows(payload.get("data"), PROVIDER_MODELS_FORMAT)


def _typed_unit_type_is_chat(row):
    """Validate the required endpoint type and keep only Chat Completions.

    A missing/non-string/empty ``type`` is a malformed required row member and
    faults; a different nonempty family is a valid non-chat row and is simply
    not selected. This runs BEFORE identity/duplicate handling, because the
    full feed repeats one canonical id across three chat families.
    """
    row_type = row.get("type", _MISSING)
    if not isinstance(row_type, str) or not row_type:
        _fail("model type")
    return row_type == _TYPED_UNIT_CHAT_TYPE


def _parse_typed_unit_models(body):
    """AI/ML API: object/data list; select Chat Completions, then deduplicate."""
    payload = _json_loads(body, exact_floats=True)
    if not isinstance(payload, dict):
        _fail("envelope")
    if payload.get("success") is False:
        _fail("envelope (success is false)")
    return _selected_rows(payload.get("data"), TYPED_UNIT_MODELS_FORMAT,
                          row_filter=_typed_unit_type_is_chat)


def parse_catalog(body, catalog_format):
    """Parse one new-format catalogue body into its raw rows.

    Only the implemented nonlegacy formats are accepted here: the legacy
    envelope stays in ``providers._parse_model_list`` so its accepted behaviour
    cannot drift. The result is a list of the ORIGINAL row objects (deduplicated
    only when a duplicate id carries an equivalent record, which is safe to
    collapse); every malformed required container or conflicting duplicate id
    raises :class:`AdapterError`. A valid empty or paid-only list is a success,
    but an envelope whose ``success`` member is exactly JSON ``false`` is a
    FAILED upstream response and always raises (plan §5, "reject success-false
    payloads") — even when its ``data`` list is empty or otherwise valid.
    """
    if catalog_format == CHANNEL_CATALOG_FORMAT:
        return _parse_channel_catalog(body)
    if catalog_format == NEWAPI_PRICING_FORMAT:
        return _parse_newapi_pricing(body)
    if catalog_format == PROVIDER_MODELS_FORMAT:
        return _parse_provider_models(body)
    if catalog_format == TYPED_UNIT_MODELS_FORMAT:
        return _parse_typed_unit_models(body)
    if catalog_format != INPUT_OUTPUT_FORMAT:
        _fail("format (not an implemented adapter)")
    payload = _json_loads(body, exact_floats=False)
    if not isinstance(payload, dict):
        _fail("envelope")
    if payload.get("success") is False:
        # Explicit upstream failure. `is False` is deliberate: only the exact
        # JSON boolean false is honoured, so 0 / "false" / null are neither
        # treated as failure nor silently coerced into one.
        _fail("envelope (success is false)")
    return _selected_rows(payload.get("data"), INPUT_OUTPUT_FORMAT)


def is_free_row(row, catalog_format):
    """Does one parsed row advertise an unambiguous zero ordinary chat price?

    Out-of-scope product types (image, speech, video, embedding, ...) are not
    free CHAT entries, and their pricing is never inspected. A documented
    unpriced or provider-variable row is nonqualifying; a malformed price
    container/value raises :class:`AdapterError`.

    A family present in :data:`REGISTERED_FORMATS` but missing its predicate
    branch here raises: silently answering ``False`` would turn a missed
    implementation into a healthy empty catalogue (the hardest failure to
    notice). An absent/legacy/unregistered format is still intentionally not
    free, because those callers have their own detection keys.
    """
    if is_legacy_format(catalog_format):
        return False
    if catalog_format == CHANNEL_CATALOG_FORMAT:
        return _felo_row_is_free(row)
    if catalog_format == NEWAPI_PRICING_FORMAT:
        return _newapi_row_is_free(row)
    if catalog_format == PROVIDER_MODELS_FORMAT:
        return _infron_row_is_free(row)
    if catalog_format == TYPED_UNIT_MODELS_FORMAT:
        return _typed_unit_row_is_free(row)
    if catalog_format != INPUT_OUTPUT_FORMAT:
        if is_registered_format(catalog_format):
            _fail(f"format {catalog_format!r} (registered without a free-row rule)")
        return False
    if not isinstance(row, dict):
        return False
    if row.get("type") != "language":
        return False
    pricing = row.get("pricing", _MISSING)
    if pricing is _MISSING or pricing is None:
        return False
    return _prices_are_zero(pricing, {})


# ---------- ephemeral metadata view ----------

def _copied_list(value):
    """A fresh list for a published array; every other value stays literal."""
    return list(value) if isinstance(value, list) else value


def _channel_catalog_metadata_view(raw):
    """Felo: copy published prose/limits and the direct modality lists.

    `capabilities` is an empty array on every captured row and
    `parameter_definitions` is not a request-parameter vocabulary, so neither
    becomes `supported_parameters`. Values are copied, never coerced; absent
    members stay absent so the accepted projector keeps its null/warning
    distinctions.
    """
    view = {}
    for key in ("name", "description", "context_length"):
        if key in raw:
            view[key] = raw[key]
    if "supported_parameters" in raw:
        view["supported_parameters"] = _copied_list(
            raw["supported_parameters"])
    if "input_modalities" in raw or "output_modalities" in raw:
        view["architecture"] = {
            "input_modalities": _copied_list(raw.get("input_modalities")),
            "output_modalities": _copied_list(raw.get("output_modalities")),
        }
    return view


def metadata_view(raw, catalog_format):
    """The ephemeral view the strict projector should read for one raw record.

    The legacy format returns a shallow copy, i.e. the exact raw mapping the
    accepted path already projected. The Vercel family renames only the members
    it really publishes (``context_window`` -> ``context_length``,
    ``modalities.input/output`` -> ``architecture``) and copies name,
    description and supported parameters; unknown, malformed and absent values
    pass through unchanged so ``catalog.project_model`` keeps its accepted
    null/warning distinctions. No value is coerced and no capability is
    invented. A non-mapping raw record (``None`` = no metadata, or a malformed
    container) is returned untouched.
    """
    if is_legacy_format(catalog_format):
        return dict(raw) if isinstance(raw, dict) else raw
    if catalog_format == CHANNEL_CATALOG_FORMAT:
        return (_channel_catalog_metadata_view(raw)
                if isinstance(raw, dict) else raw)
    if catalog_format == NEWAPI_PRICING_FORMAT:
        return (_newapi_metadata_view(raw)
                if isinstance(raw, dict) else raw)
    if catalog_format == PROVIDER_MODELS_FORMAT:
        return (_provider_models_metadata_view(raw)
                if isinstance(raw, dict) else raw)
    if catalog_format == TYPED_UNIT_MODELS_FORMAT:
        return (_typed_unit_metadata_view(raw)
                if isinstance(raw, dict) else raw)
    if catalog_format != INPUT_OUTPUT_FORMAT:
        _fail("format (not an implemented adapter)")
    if not isinstance(raw, dict):
        return raw
    view = {}
    if "name" in raw:
        view["name"] = raw["name"]
    if "description" in raw:
        view["description"] = raw["description"]
    if "context_window" in raw:
        view["context_length"] = raw["context_window"]
    if "supported_parameters" in raw:
        view["supported_parameters"] = _copied_list(
            raw["supported_parameters"])
    if "modalities" in raw:
        modalities = raw["modalities"]
        if isinstance(modalities, dict):
            view["architecture"] = {
                "input_modalities": _copied_list(modalities.get("input")),
                "output_modalities": _copied_list(modalities.get("output")),
            }
        else:
            view["architecture"] = modalities
    return view


def _newapi_metadata_view(raw):
    """TokenRA: identity and genuine prose only; every capability stays unknown.

    The pricing feed publishes no context length, modality array or parameter
    list, and the vendor's marketing pages are not a spec source: importing a
    "1M context" claim, guessing vision from a `-vl` name or inferring
    parameters from the `openai` endpoint label would be invented capability.
    The view therefore copies the exact `model_name` as the display name and a
    non-empty string `description` verbatim, and nothing else. Absent members
    stay absent so the accepted projector keeps its `metadata_missing`
    warnings without dropping the confirmed free identity.
    """
    view = {}
    model_name = raw.get("model_name")
    if isinstance(model_name, str) and model_name:
        view["name"] = model_name
    description = raw.get("description")
    if isinstance(description, str) and description:
        view["description"] = description
    return view


def _provider_models_metadata_view(raw):
    """Infron: rename the display name and map the DIRECT modality arrays.

    `display_name` becomes `name`; a genuine `description` and
    `context_length` are copied; the published `input_modalities` /
    `output_modalities` lists become `architecture`. `supported_parameters`
    stays absent because the feed publishes no request-parameter vocabulary —
    `supports_function_calling` / `supports_vision` / `supports_json_mode` are
    capability booleans, not parameters. `supports_vision: true` on a row whose
    input array is `["text"]` is NOT promoted into a vision claim: the array is
    the richer, specific field.

    The model `context_length` is the published model field, not a provider's
    (they can differ by tens of thousands of tokens). Both raw facts stay in the
    stored row; this view performs no clamping, enrichment or "fix".

    Values are copied, never coerced: present junk passes through so the
    accepted strict projector decides the degradation and the admitted identity
    is never dropped. A non-mapping raw record is returned untouched by the
    caller.
    """
    view = {}
    name = raw.get("display_name", _MISSING)
    if name is not _MISSING:
        view["name"] = name
    if "description" in raw:
        view["description"] = raw["description"]
    if "context_length" in raw:
        view["context_length"] = raw["context_length"]
    if "input_modalities" in raw or "output_modalities" in raw:
        view["architecture"] = {
            "input_modalities": _copied_list(raw.get("input_modalities")),
            "output_modalities": _copied_list(raw.get("output_modalities")),
        }
    return view


def _typed_unit_metadata_view(raw):
    """AI/ML API: `info.name`/`description`/`contextLength` and direct modalities.

    The feed's `info` object is not a fixed schema (`contextLength` is absent on
    many rows), so a missing member stays absent and the accepted strict
    projector reports genuine unknown. `modalities.input`/`output` become the
    architecture arrays; a present non-mapping modalities value is copied
    through so the projector degrades it visibly instead of this view coercing
    or silently dropping the admitted identity. `capabilities` is NOT a
    request-parameter vocabulary, so `supported_parameters` stays absent.

    Values are copied, never clamped or enriched. The raw upstream row is never
    mutated, aliased or rewritten; every list is freshly allocated.
    """
    view = {}
    info = raw.get("info", _MISSING)
    if isinstance(info, dict):
        for key, public_key in (("name", "name"),
                                ("description", "description"),
                                ("contextLength", "context_length")):
            if key in info:
                view[public_key] = info[key]
    modalities = raw.get("modalities", _MISSING)
    if isinstance(modalities, dict):
        if "input" in modalities or "output" in modalities:
            view["architecture"] = {
                "input_modalities": _copied_list(modalities.get("input")),
                "output_modalities": _copied_list(modalities.get("output")),
            }
    elif modalities is not _MISSING and modalities is not None:
        # Present junk is preserved for the strict projector's metadata_invalid
        # warning; it never drops or rewrites the raw record.
        view["architecture"] = modalities
    return view
