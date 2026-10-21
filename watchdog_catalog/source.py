"""Explicit, byte-bounded local catalogue-FILE loading with stable failures.

This module is the LOCAL-FILE boundary of the catalogue source layer and
nothing more. :func:`load_catalog_file` performs exactly one operator-selected
read:

* the path is consumed LITERALLY (no trimming, case folding, variable/tilde
  expansion, resolution, rewriting, default inference, checkout scan or roster
  discovery), so an empty string is a failed explicit file source rather than a
  cwd/default selection;
* the file is opened read-only in BINARY mode and read with a real
  ``max_bytes + 1`` cap, so a file that grows between a size probe and the read
  can never slip past the limit and an exact-limit document still loads;
* accepted bytes are decoded as strict UTF-8 (no BOM stripping, newline
  rewriting, replacement or ignored bytes) and handed EXACTLY ONCE to
  :func:`watchdog_catalog.catalog.load_snapshot`, whose return value is
  forwarded unmodified.

The accepted validator owns JSON/schema/hash truth: malformed JSON, an invalid
snapshot, an invalid hash and an unsupported schema version keep their frozen
:class:`~watchdog_catalog.catalog.CatalogError` code and message (an invalid
UTF-8 document is the accepted ``invalid_json`` failure). Only transport-shaped
faults -- open/read/close failure including an invalid filesystem string, an
oversized document, or an invalid path/byte-limit configuration -- raise here.
Path/limit TYPE errors are programming errors (:class:`TypeError` /
:class:`ValueError`) raised before anything is opened; a filesystem fault is
the fixed, non-reflective :class:`SourceError` with code
``source_unavailable``. No failure ever yields an invented empty catalogue.

Importing this module performs no file, environment, clock or network access.
Calling :func:`load_catalog_file` reads only the named file: it never writes,
consults a cache, falls back to another source, uses the network or reads a
clock. Remote HTTP, URL defaults/flags, cache/fallback/freshness wrappers,
source metadata envelopes, retries and async/coalesced refresh belong to later
units and are deliberately absent.
"""

from __future__ import annotations

import os

from . import catalog as _catalog

#: Inclusive byte cap for one selected catalogue document (eight MiB),
#: reusable by the later HTTP unit.
MAX_SOURCE_BYTES = 8 * 1024 * 1024

_SOURCE_UNAVAILABLE = "source_unavailable"
_SOURCE_UNAVAILABLE_MESSAGE = "Catalogue source unavailable."
_PATH_TYPE_ERROR = (
    "load_catalog_file() requires a filesystem path string or "
    "PathLike returning str")
_BYTE_LIMIT_ERROR = "load_catalog_file() requires a positive integer byte limit"


class SourceError(Exception):
    """The catalogue source (here: one explicit file) is unavailable.

    The failure is deliberately NON-REFLECTIVE: it takes no message or path
    argument, carries no OS exception text and exposes only the frozen
    ``source_unavailable`` code with its fixed public message. ``str()`` is
    that message, and :meth:`to_payload` returns a FRESH error envelope on
    every call. This is a source-layer failure, never a new core
    :class:`~watchdog_catalog.catalog.CatalogError` code.
    """

    def __init__(self):
        super().__init__(_SOURCE_UNAVAILABLE_MESSAGE)
        self.code = _SOURCE_UNAVAILABLE
        self.message = _SOURCE_UNAVAILABLE_MESSAGE

    def to_payload(self):
        """The source-failure envelope: exactly ``ok`` + ``error``, with
        ``ok`` strictly ``False``. A new object on every call."""
        return {"ok": False,
                "error": {"code": self.code, "message": self.message}}


def _selected_path(path):
    """The literal filesystem string for an explicit selected path.

    A ``str`` or a PathLike whose ``__fspath__`` returns ``str`` is accepted
    unchanged. ``bytes``/``bytearray``/``memoryview`` (a non-text filesystem
    representation), an integer (a raw file descriptor, including ``bool``),
    ``None`` and any invalid PathLike return type are rejected with one fixed
    ``TypeError`` BEFORE anything is opened.
    """
    if isinstance(path, str):
        return path
    if isinstance(path, (bytes, bytearray, memoryview, int)):
        raise TypeError(_PATH_TYPE_ERROR)
    try:
        selected = os.fspath(path)
    except TypeError:
        raise TypeError(_PATH_TYPE_ERROR) from None
    if not isinstance(selected, str):
        raise TypeError(_PATH_TYPE_ERROR)
    return selected


def _byte_limit(max_bytes):
    """The configured positive native byte limit (``bool`` excluded).

    Invalid configuration is one fixed non-reflective ``ValueError`` raised
    BEFORE anything is opened.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
            or max_bytes <= 0:
        raise ValueError(_BYTE_LIMIT_ERROR)
    return max_bytes


def load_catalog_file(path, *, max_bytes=MAX_SOURCE_BYTES):
    """Read, strictly decode and validate ONE explicitly selected file.

    ``path`` is an operator-owned ``str`` or ``PathLike`` returning ``str``,
    consumed literally. ``max_bytes`` bounds the RAW bytes read and defaults to
    :data:`MAX_SOURCE_BYTES`.

    Returns the validated decoded snapshot -- the very object
    :func:`watchdog_catalog.catalog.load_snapshot` produced, never a copy,
    clone or reseal. Raises :class:`SourceError` for an open/read/close fault
    or an oversized document; an invalid path or byte-limit CONFIGURATION is a
    programming error (one fixed ``TypeError``/``ValueError``) raised before
    anything is opened. Every other failure is the accepted core
    :class:`~watchdog_catalog.catalog.CatalogError` propagated unchanged.
    Synchronous by design: off-event-loop dispatch and refresh coalescing
    belong to a later unit.
    """
    selected = _selected_path(path)
    limit = _byte_limit(max_bytes)
    try:
        with open(selected, "rb") as handle:
            raw = handle.read(limit + 1)
    except (OSError, ValueError):
        # Open/read/close failure, including an invalid filesystem string. The
        # fixed failure deliberately reflects neither the path nor OS text.
        raise SourceError() from None
    if len(raw) > limit:
        raise SourceError() from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _catalog.CatalogError(_catalog.INVALID_JSON) from None
    return _catalog.load_snapshot(text)
