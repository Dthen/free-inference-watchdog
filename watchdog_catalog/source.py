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

Importing this module performs no file, environment, clock or network access.
Calling :func:`load_catalog_file` reads only the named file; calling
:func:`load_catalog_url` performs only its one GET. Neither writes, consults or
creates a cache, falls back to another source, retries or reads a clock beyond
the socket timeout. Cache, conditional GET, fallback/freshness wrappers, source
metadata envelopes, observation/fetch stamping, CLI wiring, async dispatch and
coalesced refresh belong to later units and are deliberately absent.
"""

from __future__ import annotations

import http.client
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

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
