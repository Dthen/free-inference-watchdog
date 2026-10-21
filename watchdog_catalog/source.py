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
:func:`load_catalog_url` performs only its one GET; neither consults, writes or
creates a cache, falls back to another source, retries or reads a clock beyond
the socket timeout. :class:`DiskCache` reads and writes only its own entry and
never fetches, retries, infers freshness, reseals a snapshot or reads a clock.
Conditional GET, last-good/warning/freshness envelopes, async dispatch and
coalesced refresh belong to later units and are deliberately absent.
"""

from __future__ import annotations

import hashlib
import http.client
import ntpath
import os
import posixpath
import ssl
import sys
import tempfile
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

    Synchronous by design: off-event-loop dispatch, caching, conditional GET,
    retries and refresh coalescing belong to later units.
    """
    selected = _selected_url(url)
    timeout = _timeout_seconds(timeout_s)
    limit = _http_byte_limit(max_bytes)
    request = urllib.request.Request(
        selected, method="GET",
        headers={"Accept": _ACCEPT, "Accept-Encoding": _IDENTITY})
    try:
        response = _private_opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        # HTTPError is itself a response: close it before reporting, and never
        # let its fp/URL/diagnostic text reach the caller.
        _close_quietly(error)
        raise SourceError() from None
    except (OSError, http.client.HTTPException):
        # URLError/connection/protocol/read/timeout failures are OSError or
        # HTTPException; the fixed failure reflects none of their text.
        raise SourceError() from None
    try:
        status = response.getcode() if hasattr(response, "getcode") else \
            getattr(response, "status", None)
        if status is not None and status != 200:
            raise SourceError()
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
        """Whether one decoded document is a valid record for THIS source."""
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            return False
        version = entry["cache_version"]
        if not _is_strict_int(version) or version != _CACHE_VERSION:
            return False
        identity = entry["source"]
        if not isinstance(identity, dict) or \
                set(identity) != _IDENTITY_FIELDS:
            return False
        # A record copied into the RIGHT filename is still a miss when it says
        # it belongs to another source: the filename is not the identity.
        if identity != self._identity:
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
