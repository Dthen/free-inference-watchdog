"""Explicit, byte-bounded catalogue-FILE and HTTP-200 loading with stable
failures.

This module is the SOURCE boundary of the catalogue layer: one
operator-selected local file (:func:`load_catalog_file`) or one
operator-selected URL (:func:`load_catalog_url`), and nothing more.

File boundary:

* the path is consumed LITERALLY (no trimming, case folding, variable/tilde
  expansion, resolution, rewriting, default inference, checkout scan or roster
  discovery), so an empty string is a failed explicit file source rather than a
  cwd/default selection;
* the file is opened read-only in BINARY mode and read with a real
  ``max_bytes + 1`` cap, so a file that grows between a size probe and the read
  can never slip past the limit and an exact-limit document still loads.

HTTP boundary:

* exactly ONE synchronous ``GET`` of the selected URL, consumed literally (no
  trimming, repair, scheme upgrade, mirroring or alternate-source selection),
  with explicit ``Accept: application/json`` and ``Accept-Encoding: identity``
  and no credential, cookie or provider input;
* a PRIVATE opener and connection path, so globally installed urllib openers,
  environment proxy discovery and global TLS state are neither consulted nor
  modified; production defaults are HTTPS with the platform's normal
  certificate and hostname verification;
* redirects are never followed (no redirect handler is installed), so a 3xx is
  a failure of THIS source rather than a second request to somewhere else;
  every status other than 200 -- including 204/206/304 -- fails;
* the body is read with a real ``max_bytes + 1`` cap enforced on RAW bytes
  (never on a declared length alone, and identically for chunked or
  length-delimited responses), so oversize content is rejected before any UTF-8
  decode or catalogue validation while exact-limit content still loads;
  unsupported content encodings are rejected rather than decompressed;
* a supplied ``Content-Length`` must be one unambiguous non-negative decimal
  value: an over-limit declaration fails before the body is trusted, and a body
  shorter than its declaration fails even when the bytes received so far happen
  to form a complete valid snapshot. ``HTTPResponse.read(amt)`` may report
  early EOF without raising, which is exactly why the declared/actual lengths
  are cross-checked here;
* ``timeout_s`` bounds the blocking connection/read socket operations. This is
  honestly a stdlib socket-operation timeout, not an end-to-end or DNS
  deadline, and it is the only clock this module reads.

Both boundaries share one acceptance path: accepted bytes are decoded as strict
UTF-8 (no BOM stripping, newline rewriting, replacement or ignored bytes) and
handed EXACTLY ONCE to :func:`watchdog_catalog.catalog.load_snapshot`, whose
return value is forwarded unmodified.

Conditional revalidation is a SEPARATE entry point,
:func:`fetch_catalog_url`. It is one synchronous ``GET`` that may carry exactly
ONE conditional header -- ``If-None-Match`` when the caller's candidate has a
usable entity-tag, otherwise ``If-Modified-Since`` when it has a usable HTTP
date -- derived ONLY from a candidate that was detached and fully re-validated
(version, source identity, fetch metadata, complete snapshot schema and body
hash) BEFORE the request. A candidate that is absent, foreign, a file-mode
record, a wrong version, malformed, oversized or hash-invalid is a MISS: no
conditional header is sent from it and no 304 can be authorized by it. A 304
has no representation, so its body is never read, decoded or parsed and its
``Content-Length`` describes a hypothetical 200 payload that never enters the
200 framing gate; the detached candidate snapshot is reused with its
``snapshot_id``, ``tick_epoch``, providers, metadata and warnings unchanged.
Response validators are captured literally only when they are one syntactically
usable value (one RFC entity-tag; one parseable HTTP date with an explicit
UTC/GMT interpretation); an absent, empty, duplicate or unusable optional
validator becomes ``None`` without discarding a valid body. This is the
documented conditional-GET subset, not a general-purpose HTTP cache.

The accepted validator owns JSON/schema/hash truth: malformed JSON, an invalid
snapshot, an invalid hash and an unsupported schema version keep their frozen
:class:`~watchdog_catalog.catalog.CatalogError` code and message (an invalid
UTF-8 document is the accepted ``invalid_json`` failure). Only transport-shaped
faults -- open/read/close/connection/protocol/timeout failure including an
invalid filesystem or URL string, a rejected status, an oversized document or
an invalid framing/encoding/length/timeout/byte-limit configuration -- raise
here.
Configuration TYPE errors are programming errors (:class:`TypeError` /
:class:`ValueError`) raised before any file or network access; a transport or
source fault is the fixed, non-reflective :class:`SourceError` with code
``source_unavailable``. No failure ever yields an invented empty catalogue.

Disk cache:

* :class:`DiskCache` persists ONE validated record per source under a
  source-scoped filename -- the SHA-256 of the canonical ``{kind, location}``
  identity -- so distinct selections, and the URL and file modes, can never
  share data accidentally and a raw location never reaches a filename;
* the record is the version-1 envelope ``cache_version``, ``source``,
  ``fetched_at``, ``etag``, ``last_modified`` and ``snapshot``. A load
  re-verifies that envelope, the embedded source identity and the FULL accepted
  snapshot (schema and body hash), so an unreadable, oversized, corrupt,
  wrong-source, wrong-version or unsupported-schema record is a MISS -- never a
  healthy empty catalogue -- and a legitimately empty snapshot stays a hit;
* writes are atomic: one securely created unique temp in the SAME directory as
  the final entry, written/flushed/fsynced/closed, then ``os.replace``d over
  it. A cache fault (including an unavailable or unwritable root) is a strict
  ``False`` from :meth:`DiskCache.store`, never a catalogue success or
  failure: optional persistence cannot invalidate a freshly validated snapshot;
* the root is an explicit ``cache_dir`` or the platform default from
  :func:`_default_cache_root` (XDG/HOME on Unix, ``Library/Caches`` on macOS,
  ``LOCALAPPDATA``/``USERPROFILE`` on Windows). No safe root means the disk
  cache is simply unavailable -- cwd, the filesystem root, the system temp
  directory and the project checkout are never invented as fallbacks.

Importing this module performs no file, environment, clock or network access.
Calling :func:`load_catalog_file` reads only the named file and calling
:func:`load_catalog_url` performs only its one unconditional GET; neither
consults, writes or creates a cache, falls back to another source, retries or
reads a clock beyond the socket timeout. :func:`fetch_catalog_url` performs at
most one GET of its selected URL and neither reads nor writes a cache file.
:class:`DiskCache` reads and writes only its own entry and never fetches,
retries, infers freshness, reseals a snapshot or reads a clock.

Last-good fallback and provenance (unit 5e):

* :class:`CatalogSource` is the synchronous acquisition/state layer over those
  accepted primitives. It freezes ONE selected source -- a literal URL, or a
  file whose lexical absolute path is selected at construction -- and owns one
  source-specific :class:`DiskCache`. Construction validates configuration and
  performs NO read, network access or directory creation;
* every call takes the caller's fixed ``now`` (a required keyword, native int,
  ``bool`` excluded). That argument is the ONLY clock this layer reads: the
  retrieval time (``fetched_at``) is this unit's statement about the download,
  while the observation age is derived by the accepted
  :func:`watchdog_catalog.query.watchdog_status` from the snapshot's own
  ``tick_epoch``/``cadence_s``. A recent download of old observations therefore
  stays stale, and a future tick or retrieval time stays ``None``-aged rather
  than being clamped to a healthy zero;
* :meth:`CatalogSource.refresh` bootstraps its disk entry ONCE per instance and
  then makes exactly ONE explicit refresh attempt of its selected source. A
  validated new body becomes the owned in-memory last-good entry (validators
  from that body); a 304 reuses the accepted snapshot unchanged and updates
  only the retrieval time. Persistence through :class:`DiskCache` is OPTIONAL:
  a failed store keeps the fresh memory entry usable and reports
  ``cache_write_failed``;
* a failed refresh with a valid last-good entry returns that body with its real
  ages and explicit ``refresh_failed``/``cache_fallback`` warnings; without one
  it returns the frozen :class:`SourceError` envelope. No failure ever becomes
  an ``ok`` empty catalogue, and no refresh implicitly switches source;
* :meth:`CatalogSource.current` is a memory-only view -- no source, cache,
  network or clock I/O -- and :func:`with_source` attaches the same provenance
  object to any pure query payload without mutating it.

Coalesced asynchronous acquisition (unit 5f):

* :class:`AsyncCatalogSource` is a small DEMAND-DRIVEN asynchronous owner around
  ONE private :class:`CatalogSource`. Its constructor validates the same literal
  source/cache/bounds configuration plus the two optional clock callables, and
  performs no clock read, source/cache load, directory creation or network
  access and starts no task, thread or event loop;
* every :meth:`AsyncCatalogSource.aget` runs the owned synchronous work in a
  worker thread (``asyncio.to_thread``), so no blocking stdlib HTTP, disk read
  or filesystem write runs on the event loop. Callers that overlap a DUE refresh
  join ONE strongly referenced in-flight attempt -- including arrivals after
  every earlier waiter was cancelled -- and each caller receives its own deeply
  detached acquisition even when that attempt is shared. Cancelling a caller
  cancels only that caller's wait, never the shared work;
* scheduling is process-local MONOTONIC control state, never persisted and never
  mixed with ``fetched_at``/``tick_epoch``/``cadence_s``: the first call is due
  immediately regardless of a disk record's fetch time, a source SUCCESS (a
  validated new body, an accepted 304, or a body whose only warning is
  ``cache_write_failed``) is revalidated no sooner than
  :data:`DEFAULT_REFRESH_INTERVAL_S` after the attempt COMPLETED, and a FAILED
  attempt (the structured source error, or an ``ok`` last-good fallback carrying
  ``refresh_failed``/``cache_fallback``) waits
  :data:`DEFAULT_RETRY_BACKOFF_S`. Inside a window the owner answers from
  :meth:`CatalogSource.current`, so warnings and ages are recalculated from the
  caller's captured wall epoch instead of replaying an old acquisition;
* one owner belongs to ONE running asyncio event loop, bound lazily on the first
  ``aget``; a different loop is a fixed ``RuntimeError`` raised before any
  dispatch. Wall-clock jumps change reported ages/future warnings but never move
  a monotonic deadline. There is no background timer, eager startup fetch,
  runtime sleep, immediate retry loop, ``Retry-After`` policy, interval
  configuration or force-refresh API: refresh happens only when asked for.

The synchronous loaders, transport and :class:`CatalogSource` remain the
authority for validation, persistence, fallback and freshness. CLI, SDK and
transport wiring remain later units and are deliberately absent.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import email.utils
import hashlib
import http.client
import math
import ntpath
import os
import posixpath
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import catalog as _catalog

#: Inclusive byte cap for one selected catalogue document (eight MiB),
#: shared by the local-file and HTTP boundaries.
MAX_SOURCE_BYTES = 8 * 1024 * 1024

#: The one published default catalogue URL (GitHub raw; the Pages URL is a
#: mirror, never an implicit fallback).
DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/Dthen/free-inference-watchdog/"
    "master/site/catalog.json")

#: Default blocking socket-operation timeout for one HTTP request, in seconds.
DEFAULT_TIMEOUT_S = 10

_SOURCE_UNAVAILABLE = "source_unavailable"
_SOURCE_UNAVAILABLE_MESSAGE = "Catalogue source unavailable."
_PATH_TYPE_ERROR = (
    "load_catalog_file() requires a filesystem path string or "
    "PathLike returning str")
_BYTE_LIMIT_ERROR = "load_catalog_file() requires a positive integer byte limit"
_URL_TYPE_ERROR = "load_catalog_url() requires an http or https URL string"
_URL_ERROR = (
    "load_catalog_url() requires an absolute http or https URL with a host")
_TIMEOUT_ERROR = (
    "load_catalog_url() requires a finite positive timeout in seconds")
_HTTP_BYTE_LIMIT_ERROR = (
    "load_catalog_url() requires a positive integer byte limit")

_ACCEPT = "application/json"
_IDENTITY = "identity"
_READ_CHUNK = 64 * 1024
_INFINITY = float("inf")


class SourceError(Exception):
    """The catalogue source (one explicit file or URL) is unavailable.

    The failure is deliberately NON-REFLECTIVE: it takes no message, path or
    URL argument, carries no OS/socket/HTTP exception text and exposes only the
    frozen ``source_unavailable`` code with its fixed public message. ``str()``
    is that message, and :meth:`to_payload` returns a FRESH error envelope on
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


def _selected_url(url):
    """The literal URL string for one explicit HTTP/HTTPS selection.

    A ``str`` is required; ``bytes``/``bytearray``/``memoryview``, an integer
    (including ``bool``), ``None`` and every other type is one fixed
    ``TypeError``. An empty string, a non-ASCII or control/whitespace
    character, a fragment delimiter, a missing/empty host, userinfo, an
    unsupported scheme and an invalid or out-of-range port are one fixed
    non-reflective ``ValueError``. Nothing is trimmed, percent-repaired,
    upgraded or substituted, and the check runs before any network access.
    """
    if not isinstance(url, str):
        raise TypeError(_URL_TYPE_ERROR)
    if not url or any(not 0x21 <= ord(character) <= 0x7E for character in url):
        raise ValueError(_URL_ERROR)
    if "#" in url:
        raise ValueError(_URL_ERROR)
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise ValueError(_URL_ERROR) from None
    if parts.scheme not in ("http", "https"):
        raise ValueError(_URL_ERROR)
    if not parts.netloc or "@" in parts.netloc or not parts.hostname:
        raise ValueError(_URL_ERROR)
    if parts.netloc.endswith(":"):
        raise ValueError(_URL_ERROR)
    try:
        parts.port
    except ValueError:
        raise ValueError(_URL_ERROR) from None
    return url


def _timeout_seconds(timeout_s):
    """The configured finite positive native timeout (``bool`` excluded)."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ValueError(_TIMEOUT_ERROR)
    # NaN and infinity both fail this comparison, as does zero/negative.
    if not 0 < timeout_s < _INFINITY:
        raise ValueError(_TIMEOUT_ERROR)
    return timeout_s


def _http_byte_limit(max_bytes):
    """The configured positive native HTTP byte limit (``bool`` excluded)."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
            or max_bytes <= 0:
        raise ValueError(_HTTP_BYTE_LIMIT_ERROR)
    return max_bytes


def _private_opener():
    """A fresh urllib opener independent of every global urllib setting.

    ``ProxyHandler({})`` disables environment proxy discovery, no redirect
    handler is installed (a 3xx therefore fails instead of issuing a second
    request), and the HTTPS handler gets its own verified default context.
    Built per call, so no global opener, proxy or TLS state is read or changed.
    """
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.ProxyHandler({}))
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    opener.add_handler(urllib.request.HTTPSHandler(
        context=ssl.create_default_context()))
    return opener


def _header_values(headers, name):
    """Every raw value of one response header, case-insensitively."""
    if headers is None:
        return ()
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all(name)
        return tuple(values) if values else ()
    get = getattr(headers, "get", None)
    if not callable(get):
        return ()
    for candidate in (name, name.lower(), name.upper(), name.title()):
        value = get(candidate)
        if isinstance(value, str):
            return (value,)
    return ()


def _check_content_encoding(headers):
    """Accept only an absent or exactly ``identity`` content encoding."""
    values = _header_values(headers, "Content-Encoding")
    if not values:
        return
    if len(values) != 1 or not isinstance(values[0], str):
        raise SourceError()
    if values[0].strip().lower() != _IDENTITY:
        raise SourceError()


def _declared_length(headers, limit):
    """The single unambiguous ``Content-Length``, or ``None`` when absent.

    Any duplicate header, non-decimal value, or declaration above ``limit`` is
    a framing fault. Leading zeros are accepted; the digit count is compared
    before conversion so an absurd declaration cannot reach ``int()``.
    """
    values = _header_values(headers, "Content-Length")
    if not values:
        return None
    if len(values) != 1 or not isinstance(values[0], str):
        raise SourceError()
    text = values[0].strip()
    if not text or not text.isascii() or not text.isdigit():
        raise SourceError()
    digits = text.lstrip("0") or "0"
    if len(digits) > len(str(limit)):
        raise SourceError()
    declared = int(digits)
    if declared > limit:
        raise SourceError()
    return declared


def _read_body(response, limit):
    """Read at most ``limit`` raw bytes, rejecting anything longer.

    ``read(amt)`` may legitimately return fewer bytes than requested without
    being at EOF, so the loop continues until an empty chunk or the cap is
    exceeded. Nothing here decodes or validates the document.
    """
    body = bytearray()
    while len(body) <= limit:
        chunk = response.read(min(_READ_CHUNK, limit + 1 - len(body)))
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise SourceError()
        body += chunk
    if len(body) > limit:
        raise SourceError()
    return bytes(body)


def _close_quietly(resource):
    """Close a failed response without letting a cleanup fault escape."""
    try:
        resource.close()
    except Exception:  # noqa: BLE001 - a cleanup fault is never the error
        pass


def _response_status(response):
    """The scripted/real status of one owned response, or ``None``."""
    if hasattr(response, "getcode"):
        return response.getcode()
    return getattr(response, "status", None)


def _request_headers(conditional=None):
    """The fixed request headers, plus at most ONE selected conditional pair."""
    headers = {"Accept": _ACCEPT, "Accept-Encoding": _IDENTITY}
    if conditional is not None:
        name, value = conditional
        headers[name] = value
    return headers


def _open_source(selected, timeout, headers):
    """Open ONE request through the private opener.

    Returns the owned response. A ``304`` arrives as
    :class:`urllib.error.HTTPError` (that is the real urllib path) and is
    handed back as an owned response for the conditional caller to accept or
    reject; every other HTTP error is closed and becomes the fixed failure, and
    so is any connection/protocol/timeout fault.
    """
    request = urllib.request.Request(selected, method="GET", headers=headers)
    try:
        return _private_opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        if _response_status(error) == 304:
            return error
        # HTTPError is itself a response: close it before reporting, and never
        # let its fp/URL/diagnostic text reach the caller.
        _close_quietly(error)
        raise SourceError() from None
    except (OSError, http.client.HTTPException):
        # URLError/connection/protocol/read/timeout failures are OSError or
        # HTTPException; the fixed failure reflects none of their text.
        raise SourceError() from None


def _validated_body(response, limit):
    """Frame, read, close and validate ONE owned non-304 response.

    The raw-byte cap, the single unambiguous ``Content-Length`` and the
    identity-only encoding gate are the accepted 200 rules; the accepted
    validator then owns JSON/schema/hash truth. Every transport-shaped fault
    becomes the fixed failure and every owned response is closed.
    """
    try:
        headers = getattr(response, "headers", None)
        _check_content_encoding(headers)
        declared = _declared_length(headers, limit)
        raw = _read_body(response, limit)
        if declared is not None and len(raw) != declared:
            # Early EOF: HTTPResponse.read() can report a short body without
            # raising, and a complete-looking prefix is not the promise made.
            raise SourceError()
    except SourceError:
        # Re-raise as a FRESH fixed failure so every escape path carries the
        # frozen envelope with no chained diagnostic context.
        _close_quietly(response)
        raise SourceError() from None
    except (OSError, http.client.HTTPException):
        _close_quietly(response)
        raise SourceError() from None
    try:
        response.close()
    except Exception:  # noqa: BLE001 - a cleanup fault is a source fault
        raise SourceError() from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _catalog.CatalogError(_catalog.INVALID_JSON) from None
    return _catalog.load_snapshot(text)


def load_catalog_url(url=DEFAULT_CATALOG_URL, *, timeout_s=DEFAULT_TIMEOUT_S,
                     max_bytes=MAX_SOURCE_BYTES):
    """One bounded, credential-free ``GET`` of ONE explicitly selected URL.

    ``url`` is consumed literally and must be an absolute ``http``/``https``
    URL with a host. ``timeout_s`` bounds blocking socket operations (not an
    end-to-end deadline) and ``max_bytes`` bounds the RAW response bytes.

    Returns the validated decoded snapshot -- the very object
    :func:`watchdog_catalog.catalog.load_snapshot` produced, never a copy,
    clone or reseal. Raises :class:`SourceError` for any non-200 status,
    redirect, transport/read/close/timeout fault, rejected framing/encoding,
    or oversized document; invalid URL/timeout/byte-limit CONFIGURATION is a
    programming error (one fixed ``TypeError``/``ValueError``) raised before
    any network access. Every other failure is the accepted core
    :class:`~watchdog_catalog.catalog.CatalogError` propagated unchanged.

    Synchronous by design and deliberately UNCONDITIONAL: off-event-loop
    dispatch, caching and refresh coalescing belong to later units, and
    :func:`fetch_catalog_url` is the separate revalidation entry point that may
    send one conditional header.
    """
    selected = _selected_url(url)
    timeout = _timeout_seconds(timeout_s)
    limit = _http_byte_limit(max_bytes)
    response = _open_source(selected, timeout, _request_headers())
    status = _response_status(response)
    if status is not None and status != 200:
        # Every non-200 -- including a 304, which this loader never requests --
        # is one fixed failure of THIS source.
        _close_quietly(response)
        raise SourceError() from None
    return _validated_body(response, limit)


# ---------------------------------------------------------------------------
# Source-scoped validated disk cache (unit 5c)
# ---------------------------------------------------------------------------
#
# One cache ENTRY per selected source. The entry is the version-1 envelope
# `cache_version, source, fetched_at, etag, last_modified, snapshot`; the
# snapshot is the complete accepted document, never a projection, and a load
# re-verifies the envelope, the embedded identity and the FULL accepted
# snapshot. Everything here is optional persistence: a cache fault is a MISS or
# a strict `False` from `store`, never a catalogue result.

#: Inclusive byte bound for one serialized cache ENTRY. Deliberately wider than
#: one direct source document, because an entry carries its snapshot plus
#: envelope metadata and canonical encoding overhead. This is an internal
#: reader/writer bound, not a new CLI knob.
_MAX_CACHE_BYTES = 4 * MAX_SOURCE_BYTES

#: The one cache document version this module writes and accepts.
_CACHE_VERSION = 1
_CACHE_KINDS = ("url", "file")
_CACHE_APP_DIR = "watchdog-catalog"
_CACHE_SUFFIX = ".json"
_CACHE_TEMP_PREFIX = ".watchdog-catalog-"
_CACHE_TEMP_SUFFIX = ".tmp"

#: Documented inclusive character bound for a stored ``ETag``/``Last-Modified``
#: validator. Real validators are tens of characters; the bound keeps one
#: record's metadata from being an unbounded string.
_MAX_VALIDATOR_CHARS = 1024

_ENTRY_FIELDS = frozenset({"cache_version", "source", "fetched_at", "etag",
                           "last_modified", "snapshot"})
_IDENTITY_FIELDS = frozenset({"kind", "location"})

_CACHE_KIND_TYPE_ERROR = "DiskCache() requires a cache kind string"
_CACHE_KIND_VALUE_ERROR = 'DiskCache() requires kind "url" or "file"'
_CACHE_LOCATION_ERROR = "DiskCache() requires a non-empty file source path"
_CACHE_DIR_ERROR = "DiskCache() requires a non-empty cache directory path"
_FETCHED_AT_ERROR = (
    "DiskCache.store() requires a finite non-negative fetch time")
_VALIDATOR_ERROR = (
    "DiskCache.store() requires etag and last_modified to be HTTP header "
    "values or None")


def _is_strict_int(value):
    """A native JSON integer: a ``bool`` is never an integer here."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_fetch_time(value):
    """Whether ``value`` is a caller-supplied fetch time.

    A finite, non-negative native ``int``/``float`` inside
    :data:`~watchdog_catalog.catalog.MAX_SAFE_INT`, with ``bool`` excluded. A
    FUTURE value is accepted exactly as given: this predicate never compares
    the value against a clock, so "fetched in the future" stays the caller's
    statement rather than a cache-layer repair.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= _catalog.MAX_SAFE_INT
    if isinstance(value, float):
        # NaN fails both comparisons; +/-infinity fails the upper bound.
        return 0 <= value <= _catalog.MAX_SAFE_INT
    return False


def _valid_header_validator(value):
    """Whether ``value`` is storable as an ``ETag``/``Last-Modified`` value.

    ``None`` (unknown) and a string of 1..:data:`_MAX_VALIDATOR_CHARS`
    characters are accepted; the string is preserved LITERALLY (no trim, case
    fold, quoting or unquoting). Every character must be printable ASCII
    (``U+0020``..``U+007E``) or printable Latin-1 (``U+00A0``..``U+00FF``),
    which rejects CR, LF, every C0/C1 control, DEL and anything an HTTP header
    field value could not carry.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    if not value or len(value) > _MAX_VALIDATOR_CHARS:
        return False
    for character in value:
        code = ord(character)
        if not (0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF):
            return False
    return True


def _absolute_default(value, *, windows):
    """One absolute default base, or ``None`` when it is unusable.

    Missing, empty, NUL-bearing and RELATIVE values are ignored rather than
    anchored to the current directory, so no default ever writes into cwd. A
    foreign-platform base is checked purely LEXICALLY (``ntpath`` for Windows,
    ``posixpath`` elsewhere); nothing is opened, statted or resolved.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    if windows:
        drive, _ = ntpath.splitdrive(value)
        # A drive-relative "C:foo" or a bare rooted "\foo" is not absolute.
        if not drive or not ntpath.isabs(value):
            return None
        return value
    if not posixpath.isabs(value):
        return None
    return value


def _default_cache_root(*, platform=None, environ=None):
    """The platform default cache root, or ``None`` when none is safe.

    Pure: it reads the supplied ``platform``/``environ`` (the live
    ``sys.platform``/``os.environ`` by default) only when CALLED, and never
    creates, stats, reads or resolves a file. The app directory is
    :data:`_CACHE_APP_DIR`, placed under

    * Unix: an absolute non-empty ``XDG_CACHE_HOME``, else an absolute
      non-empty ``HOME`` with ``/.cache``;
    * macOS: ``HOME/Library/Caches``;
    * Windows: an absolute ``LOCALAPPDATA``, else
      ``USERPROFILE/AppData/Local``.

    A missing, empty, relative or otherwise unusable base yields ``None`` --
    disk caching is simply unavailable. cwd, the filesystem root, the system
    temp directory and the project checkout are NEVER invented as fallbacks,
    because a silent cwd write is exactly the accident this default must not
    cause.
    """
    system = sys.platform if platform is None else platform
    values = os.environ if environ is None else environ
    if not isinstance(system, str) or values is None:
        return None
    if system == "darwin":
        home = _absolute_default(values.get("HOME"), windows=False)
        if home is None:
            return None
        return Path(posixpath.join(home, "Library", "Caches", _CACHE_APP_DIR))
    if system == "win32":
        local = _absolute_default(values.get("LOCALAPPDATA"), windows=True)
        if local is not None:
            return Path(posixpath.join(local, _CACHE_APP_DIR))
        profile = _absolute_default(values.get("USERPROFILE"), windows=True)
        if profile is None:
            return None
        return Path(posixpath.join(profile, "AppData", "Local",
                                   _CACHE_APP_DIR))
    # Every other platform is treated as Unix (the XDG base-directory rule).
    xdg = _absolute_default(values.get("XDG_CACHE_HOME"), windows=False)
    if xdg is not None:
        return Path(posixpath.join(xdg, _CACHE_APP_DIR))
    home = _absolute_default(values.get("HOME"), windows=False)
    if home is None:
        return None
    return Path(posixpath.join(home, ".cache", _CACHE_APP_DIR))


def _valid_cache_entry(entry, identity):
    """Whether one decoded document is a valid version-1 record for ``identity``.

    The ONE accepted cache-entry predicate, shared by :meth:`DiskCache.load`
    and by the conditional-GET candidate gate: exact six-key envelope, native
    ``cache_version`` 1, the exact ``{kind, location}`` identity, a caller-shape
    fetch time, header-value validators and the COMPLETE accepted snapshot
    (schema and body hash). A filename, a prior load or a caller's assertion is
    never evidence, and a legitimately empty snapshot is still valid.
    """
    if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
        return False
    version = entry["cache_version"]
    if not _is_strict_int(version) or version != _CACHE_VERSION:
        return False
    embedded = entry["source"]
    if not isinstance(embedded, dict) or \
            set(embedded) != _IDENTITY_FIELDS:
        return False
    # A record copied into the RIGHT filename is still a miss when it says it
    # belongs to another source: neither the filename nor the claim suffices.
    if embedded != identity:
        return False
    if not _valid_fetch_time(entry["fetched_at"]):
        return False
    if not _valid_header_validator(entry["etag"]):
        return False
    if not _valid_header_validator(entry["last_modified"]):
        return False
    try:
        _catalog.validate_snapshot(entry["snapshot"])
    except _catalog.CatalogError:
        return False
    return True


class DiskCache:
    """One source-scoped, validated, atomically replaced disk-cache entry.

    ``kind`` is exactly ``"url"`` or ``"file"``. A URL ``location`` is accepted
    under the SAME literal policy as :func:`_selected_url` (no trimming,
    repair, scheme upgrade or substitution) and its text stays literal. A file
    ``location`` is a non-empty path string/PathLike whose LEXICAL absolute
    path is frozen at construction -- no tilde or environment expansion and no
    symlink resolution -- so a later ``chdir`` cannot redirect the entry.
    :func:`load_catalog_file`'s literal selected-path behaviour is untouched:
    this normalization exists only to scope a cache entry.

    The source identity is exactly ``{"kind": ..., "location": ...}`` and the
    entry filename is the SHA-256 of its accepted canonical bytes plus
    ``.json``. Raw locations therefore never appear in a filename, and distinct
    selections -- including the URL and file modes -- cannot share data
    accidentally. ``cache_dir`` is an explicit non-empty root (a relative
    override is anchored absolutely here); without one
    :func:`_default_cache_root` supplies the platform root. ``path`` is the
    absolute native entry path, or ``None`` when no safe root exists.

    Construction performs NO source/cache read, network access, environment
    mutation or directory creation. :meth:`load` is a strict read returning a
    freshly decoded validated entry or ``None``; :meth:`store` is the only
    writer. This class never fetches, falls back, retries, reseals a snapshot,
    infers freshness or reads a clock, and a cache fault is never an empty
    catalogue.
    """

    def __init__(self, kind, location, *, cache_dir=None):
        if not isinstance(kind, str):
            raise TypeError(_CACHE_KIND_TYPE_ERROR)
        if kind not in _CACHE_KINDS:
            raise ValueError(_CACHE_KIND_VALUE_ERROR)
        if kind == "url":
            selected = _selected_url(location)
        else:
            selected = _selected_path(location)
            if not selected:
                raise ValueError(_CACHE_LOCATION_ERROR)
            # Lexical absolute freeze: no expansion, no realpath.
            selected = os.path.abspath(selected)
        if cache_dir is None:
            root = _default_cache_root()
        else:
            override = _selected_path(cache_dir)
            if not override:
                raise ValueError(_CACHE_DIR_ERROR)
            root = os.path.abspath(override)
        self.kind = kind
        self.location = selected
        self._identity = {"kind": kind, "location": selected}
        if root is None:
            self.path = None
        else:
            digest = hashlib.sha256(
                _catalog.canonical_bytes(self._identity)).hexdigest()
            self.path = Path(root) / (digest + _CACHE_SUFFIX)

    # -- read -------------------------------------------------------------

    def load(self):
        """The validated cache entry, or ``None`` for a MISS.

        The entry is read with a real ``_MAX_CACHE_BYTES + 1`` byte cap and
        decoded by the accepted :func:`watchdog_catalog.catalog.load_json`
        (strict UTF-8, duplicate-key/number/depth/BOM truth) and
        :func:`watchdog_catalog.catalog.validate_snapshot` (full schema and
        body-hash truth). The cache version, the embedded source identity and
        the fetch metadata are re-verified on EVERY load, so a wrong-source,
        wrong-version, unsupported-schema, corrupt, truncated or oversized
        record is a miss even when its filename is correct.

        A miss is never an empty catalogue: an absent, unreadable or invalid
        entry is not evidence of zero providers. Nothing on disk is deleted,
        repaired or rewritten, and a legitimately EMPTY validated snapshot is a
        HIT. Every returned container is freshly decoded, so caller mutation
        cannot poison a later load.
        """
        path = self.path
        if path is None:
            return None
        try:
            with open(path, "rb") as handle:
                raw = handle.read(_MAX_CACHE_BYTES + 1)
        except (OSError, ValueError):
            return None
        if not isinstance(raw, bytes) or len(raw) > _MAX_CACHE_BYTES:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        try:
            entry = _catalog.load_json(text)
        except _catalog.CatalogError:
            return None
        if not self._valid_entry(entry):
            return None
        return entry

    def _valid_entry(self, entry):
        """Whether one decoded document is a valid record for THIS source.

        Delegates to the one shared entry predicate, so a conditional-GET
        candidate and a disk record can never drift apart.
        """
        return _valid_cache_entry(entry, self._identity)

    # -- write ------------------------------------------------------------

    def store(self, snapshot, *, fetched_at, etag=None, last_modified=None):
        """Validate and atomically persist ONE cache entry for this source.

        ``snapshot`` must be a complete accepted snapshot and is never mutated,
        resealed, repaired or merged: it is detached by encoding the whole
        entry to stable bytes BEFORE any directory, temp or final write.
        ``fetched_at`` is the caller's fetch time and is preserved exactly
        (including a future value); ``etag``/``last_modified`` are optional
        literal validators.

        Returns a strict ``bool``: ``True`` only for a completed atomic
        replacement, ``False`` whenever the cache is unavailable or unwritable,
        the record would exceed :data:`_MAX_CACHE_BYTES`, or a normal cache I/O
        step (mkdir/temp/write/flush/fsync/close/replace) failed. ``False`` is
        optional-persistence failure only -- it never invalidates an otherwise
        valid freshly fetched snapshot, and the caller owns every
        warning/fallback decision.

        An invalid snapshot raises its accepted core
        :class:`~watchdog_catalog.catalog.CatalogError`; invalid ``fetched_at``
        or validator metadata raises one fixed non-reflective ``ValueError``.
        Both fail before any filesystem access.
        """
        validated = _catalog.validate_snapshot(snapshot)
        if not _valid_fetch_time(fetched_at):
            raise ValueError(_FETCHED_AT_ERROR)
        if not _valid_header_validator(etag) or \
                not _valid_header_validator(last_modified):
            raise ValueError(_VALIDATOR_ERROR)
        entry = {
            "cache_version": _CACHE_VERSION,
            "source": dict(self._identity),
            "fetched_at": fetched_at,
            "etag": etag,
            "last_modified": last_modified,
            "snapshot": validated,
        }
        payload = _catalog.canonical_bytes(entry)
        if len(payload) > _MAX_CACHE_BYTES:
            # The writer never reports success for an entry its own reader
            # would refuse.
            return False
        path = self.path
        if path is None:
            return False
        return self._replace(path, payload)

    @staticmethod
    def _replace(path, payload):
        """Stage ``payload`` beside ``path`` and atomically replace ``path``.

        The temp entry is securely created with a unique name in the SAME
        directory as the final entry -- never a fixed name and never the global
        temp directory -- then written, flushed, fsynced and closed before
        ``os.replace`` publishes it, so every visible entry is one whole valid
        record with matching metadata. Ownership of the descriptor and the temp
        path starts at creation: a wrapper-open/write/flush/fsync/close/replace
        fault closes what THIS invocation opened, removes only THIS
        invocation's temp, leaves the last-good final untouched and returns
        ``False``.

        No rollback deletion of the final, no directory removal, no locking and
        no power-loss/directory-fsync durability claim: the last COMPLETED
        concurrent valid replacement wins.

        A filesystem boundary can also reject a path VALUE rather than fail a
        syscall: an embedded NUL or a name the filesystem encoding cannot
        represent raises ``ValueError`` (``UnicodeError`` is a subclass) from
        ``makedirs``/``mkstemp``/``replace`` before any I/O happens, and from
        the best-effort close/unlink cleanup. Those are contained here exactly
        like an ``OSError`` -- strict ``False`` for the write, never a path
        repair or re-encoding, and never an escaping cleanup exception. Input
        and snapshot validation stay outside this boundary and keep their own
        exceptions.
        """
        directory = path.parent
        try:
            os.makedirs(directory, exist_ok=True)
        except (OSError, ValueError):
            return False
        descriptor = None
        handle = None
        temp_path = None
        try:
            try:
                descriptor, temp_path = tempfile.mkstemp(
                    dir=str(directory), prefix=_CACHE_TEMP_PREFIX,
                    suffix=_CACHE_TEMP_SUFFIX)
            except (OSError, ValueError):
                return False
            try:
                handle = os.fdopen(descriptor, "wb")
            except (OSError, ValueError):
                return False
            descriptor = None            # the handle owns the descriptor now
            try:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                handle = None
            except (OSError, ValueError):
                return False
            try:
                os.replace(temp_path, path)
            except (OSError, ValueError):
                return False
            temp_path = None             # published: nothing left to clean
            return True
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001 - never masks the result
                    pass
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except (OSError, ValueError):
                    pass
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except (OSError, ValueError):
                    pass


# ---------------------------------------------------------------------------
# Conditional GET revalidation (unit 5d)
# ---------------------------------------------------------------------------
#
# ONE synchronous GET that may carry ONE conditional header derived from a
# detached, fully re-validated same-source candidate. This is the documented
# conditional-GET subset -- not a general-purpose HTTP cache: no retries, no
# implicit fallback, no persistence, no clock and no observation-time
# promotion. Last-good/warning/freshness envelopes belong to a later unit.

#: One RFC 9110 entity-tag: an optional literal ``W/`` prefix, then a
#: double-quoted opaque value whose characters are ``%x21 / %x23-7E / obs-text``
#: (so no interior DQUOTE, no space, no control). Not a wildcard, not a list and
#: not unquoted text: none of those are "repaired" into a validator, and the
#: empty quoted value ``""`` is a legitimate tag.
_ETAG_SHAPE = re.compile(r'(?:W/)?"[\x21\x23-\x7E\x80-\xFF]*"\Z')

_SHORT_DAY = "Mon|Tue|Wed|Thu|Fri|Sat|Sun"
_LONG_DAY = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_MONTH = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"

#: The three HTTP-date shapes (RFC 9110 section 5.6.7), matched WHOLE. The shape
#: gate keeps a comma-separated list or a trailing-garbage value away from the
#: lenient email parser, and it makes the UTC/GMT interpretation explicit
#: (asctime is GMT by definition). Nothing here consults the locale or a clock.
_HTTP_DATE_SHAPES = tuple(re.compile(pattern) for pattern in (
    r"(?:%s), \d{2} (?:%s) \d{4} \d{2}:\d{2}:\d{2} GMT\Z"
    % (_SHORT_DAY, _MONTH),
    r"(?:%s), \d{2}-(?:%s)-\d{2} \d{2}:\d{2}:\d{2} GMT\Z"
    % (_LONG_DAY, _MONTH),
    r"(?:%s) (?:%s) [ \d]\d \d{2}:\d{2}:\d{2} \d{4}\Z"
    % (_SHORT_DAY, _MONTH),
))


def _usable_etag(value):
    """Whether ``value`` is ONE usable RFC entity-tag, preserved literally.

    The header-safety bound (:func:`_valid_header_validator`) and the entity-tag
    SHAPE are separate gates: passing the first says nothing about the second.
    """
    if not isinstance(value, str) or not _valid_header_validator(value):
        return False
    return _ETAG_SHAPE.match(value) is not None


def _usable_last_modified(value):
    """Whether ``value`` is ONE parseable HTTP date with an explicit UTC/GMT
    interpretation. The original string is preserved; nothing is reformatted."""
    if not isinstance(value, str) or not _valid_header_validator(value):
        return False
    if not any(shape.match(value) for shape in _HTTP_DATE_SHAPES):
        return False
    try:
        parsed = email.utils.parsedate_tz(value)
        if parsed is None or parsed[9] != 0:
            # No timezone, a non-zero offset or unparseable text: unusable.
            return False
        # Calendar truth (a 32nd day, month 13, second 60) is not accepted just
        # because the lenient parser returned a tuple.
        datetime.datetime(*parsed[:6])
    except (TypeError, ValueError):
        return False
    return True


def _opaque_tag(value):
    """The quoted opaque part of one already-usable entity-tag.

    The ``W/`` prefix is not part of the tag, so weak and strong spellings of
    the same opaque value compare EQUAL for a GET.
    """
    return value[2:] if value.startswith("W/") else value


def _detached_candidate(cached_entry, identity):
    """A detached, fully validated candidate entry, or ``None`` for a MISS.

    ``cached_entry`` is untrusted caller DATA: shape, version, source identity,
    fetch metadata and the COMPLETE snapshot (schema and body hash) are checked
    BEFORE any I/O, and the object retained for the rest of the call is a fresh
    decode of its own canonical bytes, so a caller (or an opener double) mutating
    the input afterwards cannot change the snapshot a 304 reuses. An entry that
    would exceed the accepted cache-entry bound is a miss too. Nothing is
    repaired, resealed or invented, and a legitimately empty snapshot stays
    usable.
    """
    if not _valid_cache_entry(cached_entry, identity):
        return None
    try:
        payload = _catalog.canonical_bytes(cached_entry)
    except (TypeError, ValueError):
        return None
    if len(payload) > _MAX_CACHE_BYTES:
        return None
    try:
        owned = _catalog.load_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, _catalog.CatalogError):
        return None
    if not _valid_cache_entry(owned, identity):
        return None
    return owned


def _response_validator(headers, name, usable):
    """One literal, usable response validator, or ``None``.

    An absent, duplicated, empty, over-bound, control-bearing or syntactically
    unusable OPTIONAL validator is dropped -- never repaired and never a reason
    to discard an otherwise accepted body.
    """
    values = _header_values(headers, name)
    if len(values) != 1 or not usable(values[0]):
        return None
    return values[0]


def _not_modified_result(response, owned):
    """Close one owned 304 and reuse its detached candidate observation.

    A 304 has NO representation: nothing is read, decoded or parsed, and its
    ``Content-Length`` (which may describe a hypothetical 200 payload larger
    than any accepted body) never reaches the 200 framing gate. A response tag
    that names a DIFFERENT opaque tag than the one sent fails closed; the
    candidate's own validator metadata is preserved unchanged.
    """
    try:
        expected = owned["etag"] if _usable_etag(owned["etag"]) else None
        if expected is not None:
            received = _response_validator(
                getattr(response, "headers", None), "ETag", _usable_etag)
            if received is not None and \
                    _opaque_tag(received) != _opaque_tag(expected):
                raise SourceError()
    except SourceError:
        _close_quietly(response)
        raise SourceError() from None
    except (OSError, http.client.HTTPException):
        _close_quietly(response)
        raise SourceError() from None
    try:
        response.close()
    except Exception:  # noqa: BLE001 - a cleanup fault is a source fault
        raise SourceError() from None
    return {"snapshot": owned["snapshot"], "etag": owned["etag"],
            "last_modified": owned["last_modified"], "not_modified": True}


def fetch_catalog_url(url=DEFAULT_CATALOG_URL, *, cached_entry=None,
                      timeout_s=DEFAULT_TIMEOUT_S,
                      max_bytes=MAX_SOURCE_BYTES):
    """One bounded conditional ``GET`` of ONE explicitly selected URL.

    ``cached_entry`` is an OPTIONAL untrusted candidate: the version-1 cache
    document (``cache_version``, ``source``, ``fetched_at``, ``etag``,
    ``last_modified``, ``snapshot``) for exactly this URL. It is detached and
    fully re-validated BEFORE the request; anything else -- ``None``, a
    non-dict, a foreign source, a file-mode record, a wrong version, invalid
    metadata, an unsupported schema, a bad body hash or an oversized entry -- is
    a MISS, so no conditional header is derived from it and no 304 can be
    authorized by it. A miss is never an empty catalogue.

    Returns exactly ``snapshot``, ``etag``, ``last_modified`` and
    ``not_modified``: a complete validated snapshot, this result's literal
    optional validators and a strict bool that distinguishes a 304 from a new
    200 body. This is an internal TRANSPORT result -- it carries no fetch time,
    path, source/warning or freshness field, and it neither reads nor writes any
    cache: the caller owns persistence and every fallback decision.

    ``url`` is consumed literally and must be an absolute ``http``/``https`` URL
    with a host; ``timeout_s`` bounds blocking socket operations and
    ``max_bytes`` bounds the RAW 200 response bytes. Invalid configuration is one
    fixed ``TypeError``/``ValueError`` raised before any network access. Every
    non-200/non-304 status, redirect, transport/read/close/timeout fault,
    rejected framing/encoding, oversized body, unrequested 304 and contradictory
    response tag is the fixed :class:`SourceError`; an invalid 200 body keeps its
    accepted core :class:`~watchdog_catalog.catalog.CatalogError`. There is no
    retry, no second GET, no implicit fallback to another source and no fallback
    to the caller's candidate.
    """
    selected = _selected_url(url)
    timeout = _timeout_seconds(timeout_s)
    limit = _http_byte_limit(max_bytes)
    identity = {"kind": "url", "location": selected}
    owned = _detached_candidate(cached_entry, identity)
    conditional = None
    if owned is not None:
        if _usable_etag(owned["etag"]):
            conditional = ("If-None-Match", owned["etag"])
        elif _usable_last_modified(owned["last_modified"]):
            conditional = ("If-Modified-Since", owned["last_modified"])
    response = _open_source(selected, timeout, _request_headers(conditional))
    status = _response_status(response)
    if status == 304:
        if conditional is None:
            # An unsolicited 304 can authorize nothing: no second GET, no empty
            # success, no invented snapshot.
            _close_quietly(response)
            raise SourceError() from None
        return _not_modified_result(response, owned)
    if status != 200:
        _close_quietly(response)
        raise SourceError() from None
    response_headers = getattr(response, "headers", None)
    snapshot = _validated_body(response, limit)
    return {
        "snapshot": snapshot,
        "etag": _response_validator(response_headers, "ETag", _usable_etag),
        "last_modified": _response_validator(
            response_headers, "Last-Modified", _usable_last_modified),
        "not_modified": False,
    }


# ---------------------------------------------------------------------------
# Last-good fallback, provenance and observation freshness (unit 5e)
# ---------------------------------------------------------------------------
#
# ONE selected source, ONE owned in-memory last-good entry (bootstrapped once
# from that source's disk entry), ONE explicit refresh attempt per call. The
# caller's fixed `now` is the whole clock interface: retrieval time and
# observation age are separate statements, and the observation age is derived
# by the accepted core status query rather than a second freshness algorithm.
# Persistence stays optional, and no failure ever becomes an empty success.

#: The frozen source-warning order. The core status warning codes keep their
#: accepted relative order after the retrieval/cache codes.
_SOURCE_WARNING_ORDER = (
    "refresh_failed", "cache_fallback", "cache_write_failed",
    "fetch_time_future", "tick_epoch_unknown", "metadata_missing",
    "metadata_invalid", "tick_epoch_future", "tick_stale")

_NOW_ERROR = (
    "CatalogSource() requires now as a non-negative integer epoch in seconds")
_WITH_SOURCE_ERROR = (
    "with_source() requires a payload object and an acquisition object")


def _now_argument(now):
    """The caller's fixed clock: a native non-negative safe integer.

    ``bool`` is excluded (``True`` is not an epoch), as are floats, strings and
    values outside :data:`~watchdog_catalog.catalog.MAX_SAFE_INT`. Invalid
    input is ONE fixed non-reflective ``ValueError`` raised before any disk,
    source or state work. This function never reads a wall or monotonic clock.
    """
    if isinstance(now, bool) or not isinstance(now, int) \
            or not 0 <= now <= _catalog.MAX_SAFE_INT:
        raise ValueError(_NOW_ERROR)
    return now


def _detached_entry(entry, identity):
    """A fresh decode of one validated, SOURCE-MATCHED v1 cache entry.

    Returns ``None`` unless ``entry`` is a complete version-1 record for
    ``identity`` -- so a foreign record can never become this source's
    last-good body -- and detachment is a canonical encode/decode round trip,
    so a caller -- or a loader/revalidator double -- mutating its object
    afterwards cannot change what this layer retains. Unlike the transport's
    candidate gate this has NO cache-serialization size bound: a valid source
    body may be too large to persist while remaining a perfectly valid
    in-memory observation.
    """
    if not isinstance(entry, dict):
        return None
    if not _valid_cache_entry(entry, identity):
        return None
    try:
        payload = _catalog.canonical_bytes(entry)
        owned = _catalog.load_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        return None
    if not _valid_cache_entry(owned, identity):
        return None
    return owned


def _observation_status(snapshot, now):
    """The accepted core status payload for one retained snapshot.

    The query module is imported at CALL time, never at module import, so this
    module stays inert and query-free under the package's fresh-process import
    guard. This reuses the frozen inclusive cadence boundary, unknown/future
    tick handling and provider-independent observation health; the snapshot and
    the query result are never modified.
    """
    from . import query as _query
    return _query.watchdog_status(snapshot, {}, now=now)


class CatalogSource:
    """ONE selected catalogue source with last-good fallback and provenance.

    ``kind`` is exactly ``"url"`` or ``"file"``. A URL ``location`` is consumed
    under the accepted literal policy and stays literal; a file ``location`` is
    frozen to its lexical absolute path at construction -- no tilde/environment
    expansion and no symlink resolution -- and that SAME path is read on every
    refresh, so a later ``chdir`` cannot mix two files. ``cache_dir`` and the
    optional bounds are the accepted :class:`DiskCache`,
    :func:`fetch_catalog_url` and loader configurations.

    Construction only validates configuration and selects paths: it performs no
    source/cache read, no network access and no directory creation. The disk
    entry is bootstrapped lazily and ONCE by the first :meth:`refresh`.

    State is deliberately small: one owned validated entry (or ``None``), a
    refresh-failure flag and an optional-persistence flag. Every acquisition is
    a freshly allocated container, so mutating a result -- including its nested
    snapshot or warnings -- cannot poison retained memory, the disk entry or
    another instance.
    """

    def __init__(self, kind, location, *, cache_dir=None,
                 timeout_s=DEFAULT_TIMEOUT_S,
                 max_bytes=MAX_SOURCE_BYTES):
        if not isinstance(kind, str):
            raise TypeError(_CACHE_KIND_TYPE_ERROR)
        if kind not in _CACHE_KINDS:
            raise ValueError(_CACHE_KIND_VALUE_ERROR)
        if kind == "url":
            selected = _selected_url(location)
            limit = _http_byte_limit(max_bytes)
        else:
            selected = _selected_path(location)
            if not selected:
                raise ValueError(_CACHE_LOCATION_ERROR)
            # Lexical absolute freeze: no expansion and no realpath, so the
            # identity, the cache key and the file actually read stay one path.
            selected = os.path.abspath(selected)
            limit = _byte_limit(max_bytes)
        timeout = _timeout_seconds(timeout_s)
        self.kind = kind
        self.location = selected
        self._url = selected if kind == "url" else None
        self._path = None if kind == "url" else selected
        self._timeout_s = timeout
        self._max_bytes = limit
        self._identity = {"kind": kind, "location": selected}
        self._cache = DiskCache(kind, selected, cache_dir=cache_dir)
        self._entry = None
        self._bootstrapped = False
        self._refresh_failed = False
        self._cache_write_failed = False

    # -- state ------------------------------------------------------------

    def _bootstrap(self):
        """Load this source's disk entry at most ONCE per instance.

        A miss, a corrupt/foreign/old-version record and an unavailable cache
        are all simply "no last-good" -- nothing is deleted, repaired or
        searched across sources -- and the attempt is not repeated.
        """
        if self._bootstrapped:
            return
        loaded = self._cache.load()
        if loaded is not None:
            owned = _detached_entry(loaded, self._identity)
            if owned is not None:
                self._entry = owned
        self._bootstrapped = True

    def _read_source(self):
        """Make exactly ONE explicit refresh attempt of the selected source."""
        if self.kind == "url":
            return fetch_catalog_url(
                self._url, cached_entry=self._entry,
                timeout_s=self._timeout_s, max_bytes=self._max_bytes)
        return {
            "snapshot": load_catalog_file(
                self._path, max_bytes=self._max_bytes),
            "etag": None,
            "last_modified": None,
            "not_modified": False,
        }

    def _candidate(self, result, moment):
        """The owned six-key v1 memory entry for one accepted refresh result."""
        entry = {
            "cache_version": _CACHE_VERSION,
            "source": dict(self._identity),
            "fetched_at": moment,
            "etag": result["etag"],
            "last_modified": result["last_modified"],
            "snapshot": result["snapshot"],
        }
        return _detached_entry(entry, self._identity)

    def _acquisition(self, moment, *, cached, not_modified, refresh_failed):
        """Build ONE fresh successful acquisition from retained memory."""
        entry = self._entry
        fetched_at = entry["fetched_at"]
        future = fetched_at > moment
        status = _observation_status(entry["snapshot"], moment)
        present = set()
        if refresh_failed:
            # A cached fallback is exactly a failed refresh served from memory.
            present.update(("refresh_failed", "cache_fallback"))
        if self._cache_write_failed:
            present.add("cache_write_failed")
        if future:
            present.add("fetch_time_future")
        present.update(status["warnings"])
        return {
            "ok": True,
            "snapshot": copy.deepcopy(entry["snapshot"]),
            "source": {
                "mode": self.kind,
                "url": self._url,
                "cached": cached,
                "fetched_at": fetched_at,
                "fetch_age_s": None if future else moment - fetched_at,
                "observation_age_s": status["last_tick_age_s"],
                "observation_fresh": status["tick_fresh"],
                "not_modified": not_modified,
                "warnings": [code for code in _SOURCE_WARNING_ORDER
                             if code in present],
            },
        }

    def _failed(self, moment):
        """The documented outcome of one failed source attempt."""
        if self._entry is None:
            return SourceError().to_payload()
        self._refresh_failed = True
        return self._acquisition(moment, cached=True, not_modified=False,
                                 refresh_failed=True)

    # -- public API -------------------------------------------------------

    def refresh(self, *, now):
        """Bootstrap if needed, then make ONE explicit source refresh attempt.

        ``now`` is the caller-supplied fixed clock. A validated new 200/file
        body becomes the retained last-good entry with ``fetched_at=now``; a
        304 reuses the accepted complete snapshot unchanged, updates ONLY the
        retrieval time and retains its validators. Persistence is attempted
        after the memory entry is owned: a ``False`` store keeps the snapshot
        usable and reports ``cache_write_failed``.

        A :class:`SourceError` or accepted
        :class:`~watchdog_catalog.catalog.CatalogError` is a failed attempt: the
        last-good entry (memory or the once-bootstrapped disk entry) is returned
        with its original body, validators and retrieval time plus explicit
        ``refresh_failed``/``cache_fallback`` warnings, or the frozen source
        error envelope when nothing valid was ever captured. Programming and
        configuration errors keep their accepted exceptions.
        """
        moment = _now_argument(now)
        self._bootstrap()
        try:
            result = self._read_source()
        except (SourceError, _catalog.CatalogError):
            return self._failed(moment)
        candidate = self._candidate(result, moment)
        if candidate is None:
            return self._failed(moment)
        # Publish only an owned, fully validated candidate ...
        self._entry = candidate
        self._refresh_failed = False
        # ... then let optional persistence fail without touching the data.
        stored = self._cache.store(
            candidate["snapshot"], fetched_at=candidate["fetched_at"],
            etag=candidate["etag"], last_modified=candidate["last_modified"])
        self._cache_write_failed = not stored
        revalidated = result["not_modified"] is True
        return self._acquisition(moment, cached=revalidated,
                                 not_modified=revalidated,
                                 refresh_failed=False)

    def current(self, *, now):
        """A memory-only view: no source, cache, network or clock I/O.

        Before the first :meth:`refresh` there is no memory entry, so the
        frozen structured source error is returned; this method never triggers
        the one-time disk bootstrap. A retained entry returns a detached result
        with ``cached=True`` and the current failure/persistence warnings, which
        only a documented successful refresh clears.
        """
        moment = _now_argument(now)
        if self._entry is None:
            return SourceError().to_payload()
        return self._acquisition(moment, cached=True, not_modified=False,
                                 refresh_failed=self._refresh_failed)


def with_source(payload, acquisition):
    """Attach one acquisition's provenance to a pure query payload.

    This is the thin, transport-independent attachment point: it neither
    queries nor acquires. A successful acquisition returns a deep copy of
    ``payload`` with only ``source`` added (itself a fresh copy), preserving the
    payload's ``ok``/``error`` shape, counts, metadata, snapshot identity and
    existing top-level warnings. A failed acquisition returns a fresh copy of
    that structured source error instead -- an argument-error payload therefore
    keeps its own shape rather than being promoted to a success.

    ``payload`` and ``acquisition`` are the programming interface: anything but
    a ``dict`` is one fixed non-reflective ``TypeError``. Neither argument is
    ever mutated.
    """
    if not isinstance(payload, dict) or not isinstance(acquisition, dict):
        raise TypeError(_WITH_SOURCE_ERROR)
    if acquisition.get("ok") is True:
        attached = copy.deepcopy(payload)
        attached["source"] = copy.deepcopy(acquisition["source"])
        return attached
    return copy.deepcopy(acquisition)


# ---------------------------------------------------------------------------
# Coalesced asynchronous refresh, monotonic scheduling and off-loop dispatch
# (unit 5f)
# ---------------------------------------------------------------------------
#
# ONE demand-driven asynchronous owner around ONE private CatalogSource. Every
# source, cache, network and memory-view operation runs in a worker thread, an
# overlapping DUE refresh is JOINED rather than repeated, and eligibility is
# process-local MONOTONIC control state -- never persisted and never mixed with
# the acquisition's own observation/retrieval times.

#: Revalidate a successful source at most this often, in seconds.
DEFAULT_REFRESH_INTERVAL_S = 300

#: Wait this long after a FAILED source attempt before trying again, in seconds.
DEFAULT_RETRY_BACKOFF_S = 30

_LOOP_ERROR = "AsyncCatalogSource() must be used from one asyncio event loop"
_CLOCK_CALLABLE_ERROR = "AsyncCatalogSource() requires callable clocks or None"
_WALL_CLOCK_ERROR = (
    "AsyncCatalogSource() requires wall_clock to return a finite non-negative "
    "safe integer epoch")
_MONOTONIC_CLOCK_ERROR = (
    "AsyncCatalogSource() requires monotonic_clock to return a finite number")


def _wall_epoch(clock):
    """One validated capture of the supplied wall clock as a native epoch.

    ``time.time`` semantics: a native ``int``/``float`` (``bool`` excluded),
    finite, non-negative and inside
    :data:`~watchdog_catalog.catalog.MAX_SAFE_INT`, truncated toward zero into a
    native ``int`` so a positive fractional part is dropped. Every read -- the
    captured operation time and an in-window view time alike -- goes through
    this single gate, so an unusable clock is ONE fixed non-reflective
    ``ValueError`` raised BEFORE new source/cache I/O instead of a repaired
    epoch. The clock's own exceptions are never caught here.
    """
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_WALL_CLOCK_ERROR)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(_WALL_CLOCK_ERROR)
    if not 0 <= value <= _catalog.MAX_SAFE_INT:
        raise ValueError(_WALL_CLOCK_ERROR)
    return int(value)


def _monotonic_reading(clock):
    """One validated capture of the supplied monotonic clock.

    ``time.monotonic`` semantics: a finite native ``int``/``float`` (``bool``
    excluded) with an ARBITRARY reference point, so a negative origin is
    accepted. The value is returned natively and is only ever compared or added
    to, so scheduling arithmetic stays in the clock's own units. An unusable
    reading is ONE fixed non-reflective ``ValueError``; nothing invents a
    substitute deadline, and the clock's own exceptions are never caught.
    """
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_MONOTONIC_CLOCK_ERROR)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(_MONOTONIC_CLOCK_ERROR)
    return value


def _source_attempt_failed(result):
    """Whether one :class:`CatalogSource` result is a FAILED source attempt.

    A structured no-source error (``ok`` not ``True``) and an ``ok`` last-good
    fallback carrying ``refresh_failed``/``cache_fallback`` are both failures:
    ``ok`` alone never proves the source answered. A validated new body, an
    accepted 304 and a body whose only warning is ``cache_write_failed`` are
    successes. Anything unrecognisable counts as a failure, so a malformed
    result can only ever shorten the next attempt to the retry window.
    """
    if not isinstance(result, dict) or result.get("ok") is not True:
        return True
    metadata = result.get("source")
    if not isinstance(metadata, dict):
        return True
    warnings = metadata.get("warnings")
    if not isinstance(warnings, list):
        return True
    return "refresh_failed" in warnings or "cache_fallback" in warnings


def _observe_task(task):
    """Retrieve a completed flight's exception so none is left unobserved.

    Every waiter still receives the exception through its own ``await``; this
    callback only keeps a flight whose waiters all cancelled from being reported
    as an unretrieved task exception at collection time.
    """
    if task.cancelled():
        return
    task.exception()


class AsyncCatalogSource:
    """ONE demand-driven asynchronous owner around ONE synchronous source.

    ``kind``, ``location``, ``cache_dir``, ``timeout_s`` and ``max_bytes`` are
    exactly :class:`CatalogSource`'s accepted literal source/cache/bounds
    configuration; this object owns one PRIVATE instance of it and never shares
    that instance. ``wall_clock`` (default ``time.time``) supplies the captured
    operation epoch and ``monotonic_clock`` (default ``time.monotonic``) supplies
    scheduling readings; both must be callable and neither is read at
    construction.

    Construction validates configuration and nothing else: no clock read, no
    source/cache load, no directory creation, no network access, and no task,
    thread or event loop is started. The owner binds itself to the running
    asyncio event loop of its FIRST :meth:`aget`; use from another loop is a
    fixed ``RuntimeError`` raised before any dispatch, so no foreign future is
    awaited and the owned source's state is never touched from two loops. This
    is a single-loop owner, not a cross-loop or cross-thread service.

    Normal event-loop or process shutdown is NOT a promise to cancel an
    executing stdlib thread, DNS lookup or socket immediately: the accepted
    socket-operation ``timeout_s`` remains the only bound on a request that is
    already in flight.
    """

    def __init__(self, kind, location, *, cache_dir=None,
                 timeout_s=DEFAULT_TIMEOUT_S, max_bytes=MAX_SOURCE_BYTES,
                 wall_clock=None, monotonic_clock=None):
        selected_wall = time.time if wall_clock is None else wall_clock
        selected_monotonic = (
            time.monotonic if monotonic_clock is None else monotonic_clock)
        if not callable(selected_wall) or not callable(selected_monotonic):
            raise TypeError(_CLOCK_CALLABLE_ERROR)
        self._source = CatalogSource(
            kind, location, cache_dir=cache_dir, timeout_s=timeout_s,
            max_bytes=max_bytes)
        self._wall_clock = selected_wall
        self._monotonic_clock = selected_monotonic
        # One lock for the WHOLE owned object: a view and a refresh can never
        # race on last-good state, and no worker thread enters a refresh while
        # another is still finishing.
        self._sync_lock = threading.Lock()
        self._loop = None
        self._flight = None
        self._next_eligible = None

    # -- worker-thread operations -----------------------------------------

    def _refresh_in_worker(self, moment):
        """ONE refresh of the owned source, serialized with every view."""
        with self._sync_lock:
            return self._source.refresh(now=moment)

    def _view_in_worker(self, moment):
        """ONE memory-only current view, serialized with every refresh."""
        with self._sync_lock:
            return self._source.current(now=moment)

    # -- scheduling -------------------------------------------------------

    async def _run_flight(self, moment):
        """Run ONE shared source attempt and publish its monotonic deadline.

        The interval is anchored at the COMPLETION monotonic reading, taken
        after the worker returns. An unusable completion reading raises instead
        of inventing a schedule: the accepted acquisition stays in memory (its
        own truth is untouched), the flight is still cleared, and the next call
        remains due because no deadline was published.
        """
        try:
            result = await asyncio.to_thread(self._refresh_in_worker, moment)
            completed = _monotonic_reading(self._monotonic_clock)
            interval = (DEFAULT_RETRY_BACKOFF_S if _source_attempt_failed(result)
                        else DEFAULT_REFRESH_INTERVAL_S)
            self._next_eligible = completed + interval
            return result
        finally:
            if self._flight is asyncio.current_task():
                self._flight = None

    @staticmethod
    async def _joined(flight):
        """Await one shared flight and hand THIS caller its own deep copy.

        The flight is awaited through :func:`asyncio.shield`, so cancelling the
        awaiting caller raises ``CancelledError`` here without cancelling the
        shared attempt: awaiting a task directly would make that task the
        waiter's own ``_fut_waiter``, and ``Task.cancel()`` would then cancel the
        attempt for every other caller. Ownership of the flight therefore
        outlives any individual waiter, and each successful waiter gets a freshly
        allocated acquisition, so mutated snapshots, metadata or warnings can
        never reach another waiter, retained memory, the disk entry or a later
        view.
        """
        return copy.deepcopy(await asyncio.shield(flight))

    async def aget(self):
        """The acquisition for this source: one refresh, or a fresh view.

        The first call is due immediately. A call that overlaps a due refresh
        joins that one attempt; a call inside a success or failure window
        returns an off-loop :meth:`CatalogSource.current` view instead, with the
        surviving warnings and the real observation age of the caller's captured
        wall epoch. There is no automatic retry, timer or force-refresh.
        """
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError(_LOOP_ERROR)
        flight = self._flight
        if flight is not None:
            return await self._joined(flight)
        moment = _wall_epoch(self._wall_clock)
        monotonic = _monotonic_reading(self._monotonic_clock)
        if self._next_eligible is not None and monotonic < self._next_eligible:
            return await asyncio.to_thread(self._view_in_worker, moment)
        flight = loop.create_task(self._run_flight(moment))
        self._flight = flight
        flight.add_done_callback(_observe_task)
        return await self._joined(flight)
