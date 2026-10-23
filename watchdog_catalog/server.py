"""Typed MCP v2 transport over the published catalogue (unit 6b).

This is the ONLY module allowed to import the MCP SDK and Pydantic. Importing
:mod:`watchdog_catalog`, its catalogue/query/source core or its ``tools.json``
resource stays stdlib-only: the package root re-exports nothing from here.

Registration is annotation-derived. Every exposed callable is a named
``async def`` whose parameters, defaults and constrained aliases are the single
source of the advertised input schema, which the SDK derives with Pydantic.
The packaged ``tools.json`` is read through ``importlib.resources`` for the
reviewed tool descriptions and as the parity oracle; it is never assigned as a
replacement ``inputSchema``.

Boundary configuration (installed ``mcp`` 2.3.0, Pydantic 2.14.1) is deliberately
narrow and scoped to these five registrations:

* :class:`_StrictFuncMetadata` overrides the SDK's documented
  ``FuncMetadata.pre_parse_json`` convenience. ``Tool.run`` otherwise parses
  JSON-looking STRINGS into null/arrays/objects before Pydantic sees them, which
  would turn a literal ``query="[]"`` into a list, a literal ``provider="null"``
  into no-filter ``None`` and an encoded-list string into a real list. The
  reviewed contract requires the ORIGINAL values, so that pre-parse is a no-op
  here. All other SDK class/global defaults are untouched.
* the derived argument model is subclassed with exactly two config changes:
  ``extra="forbid"`` (unknown arguments are rejected, including on the
  zero-argument ``watchdog_status``) and ``strict=True`` (no lax string/number
  coercion).
* integer parameters use ``WrapValidator`` aliases that validate the original
  JSON value and RETURN IT UNCHANGED. Pydantic's strict int rejects the
  integral JSON number ``1.0`` the contract accepts, while a lax int would
  accept ``"5"``/``True``/``1.5``; the alias accepts strict ints and finite
  integral floats inside the reviewed inclusive bounds, rejects bools, numeric
  strings, fractions, non-finite and unsafe values, and keeps an integral float
  as ``1.0`` so the core's literal ``filters`` echo is preserved. The schema
  bounds come from the same bound constants the validator uses.

``create_server`` performs no acquisition, cache bootstrap/write, feed request,
provider probe or clock read. ``source`` and ``wall_clock`` are closure-captured
factory configuration and are never tool arguments. The five wrappers acquire
once with ``AsyncCatalogSource.aget()``, delegate entirely to the existing pure
query functions and attach provenance with
:func:`watchdog_catalog.source.with_source`. No query, matching, sorting,
normalization, retry or caching algorithm is reimplemented here.

A failed acquisition returns the unchanged semantic source error, detached. A
successful acquisition preserves the core payload (including an
``unknown_provider`` argument error) and adds exactly ``source``. For
``watchdog_status`` the closure's ``wall_clock`` reading is normalised to the
core's native non-negative safe-integer epoch and passed explicitly as ``now``;
an unusable reading is one fixed non-reflective ``ValueError`` raised before the
core sees it. There is no public clock argument and no clock lookup in the
query core.
"""

from __future__ import annotations

import importlib.resources
import json
import math
import time
from typing import Annotated, Any

from pydantic import ConfigDict, Field, WrapValidator

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata, func_metadata

from watchdog_catalog import catalog as _catalog
from watchdog_catalog import query as _query
from watchdog_catalog.source import with_source

_SERVER_NAME = "free-inference-watchdog-mcp"
_CONTRACT_RESOURCE = "tools.json"

#: The frozen required-``model_id`` predicate: at least one character outside
#: the explicit ASCII blank set U+0009..U+000D and U+0020. This is the reviewed
#: ``tools.json`` pattern verbatim -- never widened to Unicode whitespace.
_MODEL_ID_PATTERN = r"[^\u0009-\u000D\u0020]"

#: Inclusive JSON-safe integer bounds, shared by the generated schema and the
#: runtime validator so neither can drift from the other.
_MAX_SAFE_INT = _catalog.MAX_SAFE_INT
_NON_NEGATIVE_BOUNDS = (0, _MAX_SAFE_INT)
_PAGE_LIMIT_BOUNDS = (1, 200)

_INTEGER_MESSAGE = "must be an integer within the reviewed inclusive bounds"
_STATUS_CLOCK_ERROR = (
    "create_server() requires wall_clock to return a finite non-negative "
    "safe integer epoch")


def _bounded_integer(minimum, maximum):
    """Build the strict integer boundary for ``minimum..maximum``.

    The ORIGINAL value is returned unchanged: an integral float stays ``1.0``
    so the pure core's literal filter echo is preserved, while bools, numeric
    strings, fractions, non-finite numbers and out-of-range values are rejected
    before the wrapper or the core can see them. ``handler`` is intentionally
    not called -- it is the conversion this alias exists to bypass.
    """

    def validate(value, handler):
        del handler
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(_INTEGER_MESSAGE)
        if isinstance(value, float) and (
                not math.isfinite(value) or not value.is_integer()):
            raise ValueError(_INTEGER_MESSAGE)
        if not minimum <= value <= maximum:
            raise ValueError(_INTEGER_MESSAGE)
        return value

    return validate


#: A nullable/inline JSON-safe integer >= 0 and <= 2**53 - 1.
_SafeInteger = Annotated[
    int,
    Field(ge=_NON_NEGATIVE_BOUNDS[0], le=_NON_NEGATIVE_BOUNDS[1]),
    WrapValidator(_bounded_integer(*_NON_NEGATIVE_BOUNDS)),
]

#: The pagination page size: a required integer 1..200 (explicit null invalid).
_PageLimit = Annotated[
    int,
    Field(ge=_PAGE_LIMIT_BOUNDS[0], le=_PAGE_LIMIT_BOUNDS[1]),
    WrapValidator(_bounded_integer(*_PAGE_LIMIT_BOUNDS)),
]

#: One capability-array member: a nonempty literal string.
_Capability = Annotated[str, Field(min_length=1)]

#: The required raw model id: a string matching the reviewed predicate.
_ModelId = Annotated[str, Field(pattern=_MODEL_ID_PATTERN)]


class _StrictFuncMetadata(FuncMetadata):
    """SDK function metadata that never rewrites the caller's original values.

    ``FuncMetadata.validate_arguments`` calls ``pre_parse_json`` before Pydantic
    validation to be forgiving about JSON-looking strings. The reviewed contract
    treats a string as a string, so this registration boundary keeps the
    arguments exactly as supplied; the strict argument model then validates the
    originals. This is scoped to the five tools created below -- the SDK class
    default is untouched.
    """

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        return data


def _load_contract():
    """Read the packaged, reviewed tool contract; never a checkout path."""
    resource = importlib.resources.files("watchdog_catalog").joinpath(
        _CONTRACT_RESOURCE)
    contract = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(contract, list) or not contract:
        raise ValueError("the packaged tool contract is not a non-empty list")
    return contract


def _strict_arguments_model(base):
    """The annotation-derived argument model with the narrow boundary config."""
    config = ConfigDict(**dict(base.model_config), extra="forbid",
                        strict=True)
    return type(f"Strict{base.__name__}", (base,),
                {"model_config": config, "__module__": __name__})


def _tool(fn, entry):
    """Register one annotated wrapper as a real SDK ``Tool``."""
    if entry["name"] != fn.__name__:
        raise ValueError(
            f"tool contract name {entry['name']!r} does not match {fn.__name__!r}")
    metadata = func_metadata(fn)
    arguments_model = _strict_arguments_model(metadata.arg_model)
    strict_metadata = _StrictFuncMetadata(
        arg_model=arguments_model,
        output_schema=metadata.output_schema,
        output_model=metadata.output_model,
        wrap_output=metadata.wrap_output,
    )
    return Tool(
        fn=fn,
        name=entry["name"],
        description=entry["description"],
        parameters=arguments_model.model_json_schema(by_alias=True),
        fn_metadata=strict_metadata,
        is_async=True,
    )


def _failed(acquisition):
    """The unchanged semantic source error, freshly detached per call."""
    return with_source({}, acquisition)


def _status_epoch(clock):
    """One validated capture of the closure's wall clock as a native epoch.

    ``time.time`` semantics: a native ``int``/``float`` (``bool`` excluded),
    finite, non-negative and inside the JSON-safe ceiling, truncated toward
    zero. An unusable reading is ONE fixed non-reflective ``ValueError``; the
    clock's own exceptions are never caught or rewritten.
    """
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_STATUS_CLOCK_ERROR)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(_STATUS_CLOCK_ERROR)
    if not 0 <= value <= _MAX_SAFE_INT:
        raise ValueError(_STATUS_CLOCK_ERROR)
    return int(value)


def create_server(source, *, wall_clock=time.time):
    """Build the real SDK server around one configured async source owner.

    ``source`` is already constructed (an :class:`AsyncCatalogSource` in
    production, a narrow acquisition double in tests); ``wall_clock`` supplies
    only the ``watchdog_status`` epoch. Both stay closure-captured configuration
    and neither is read here: construction performs no acquisition, cache
    bootstrap/write, feed request, provider probe or clock invocation.
    """

    async def list_free_models(provider: str | None = None) -> dict:
        acquisition = await source.aget()
        if acquisition.get("ok") is not True:
            return _failed(acquisition)
        return with_source(
            _query.list_free_models(acquisition["snapshot"],
                                    {"provider": provider}),
            acquisition)

    async def get_model(model_id: _ModelId) -> dict:
        acquisition = await source.aget()
        if acquisition.get("ok") is not True:
            return _failed(acquisition)
        return with_source(
            _query.get_model(acquisition["snapshot"], {"model_id": model_id}),
            acquisition)

    async def list_endpoints(provider: str | None = None) -> dict:
        acquisition = await source.aget()
        if acquisition.get("ok") is not True:
            return _failed(acquisition)
        return with_source(
            _query.list_endpoints(acquisition["snapshot"],
                                  {"provider": provider}),
            acquisition)

    async def watchdog_status() -> dict:
        acquisition = await source.aget()
        if acquisition.get("ok") is not True:
            return _failed(acquisition)
        return with_source(
            _query.watchdog_status(acquisition["snapshot"], {},
                                   now=_status_epoch(wall_clock)),
            acquisition)

    async def search_models(
            query: str | None = None,
            provider: str | None = None,
            min_context_length: _SafeInteger | None = None,
            input_modalities: list[_Capability] | None = None,
            supported_parameters: list[_Capability] | None = None,
            limit: _PageLimit = 50,
            offset: _SafeInteger = 0) -> dict:
        acquisition = await source.aget()
        if acquisition.get("ok") is not True:
            return _failed(acquisition)
        return with_source(
            _query.search_models(acquisition["snapshot"], {
                "query": query,
                "provider": provider,
                "min_context_length": min_context_length,
                "input_modalities": input_modalities,
                "supported_parameters": supported_parameters,
                "limit": limit,
                "offset": offset,
            }),
            acquisition)

    wrappers = {
        "list_free_models": list_free_models,
        "get_model": get_model,
        "list_endpoints": list_endpoints,
        "watchdog_status": watchdog_status,
        "search_models": search_models,
    }
    contract = _load_contract()
    contract_names = [entry["name"] for entry in contract]
    if sorted(contract_names) != sorted(wrappers) or len(
            contract_names) != len(wrappers):
        raise ValueError(
            "the packaged tool contract does not match the typed wrappers")
    tools = [_tool(wrappers[entry["name"]], entry) for entry in contract]
    return MCPServer(name=_SERVER_NAME, tools=tools)
