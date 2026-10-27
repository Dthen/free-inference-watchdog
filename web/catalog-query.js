/*!
 * free-inference-watchdog — dependency-free catalogue validation and
 * projection counterpart.
 *
 * This file is the JavaScript counterpart of the raw-JSON acceptance, typed
 * canonical-encoding/hash and strict structural validation boundary in
 * `watchdog_catalog/catalog.py`, plus the pure public capability/model
 * projection. It is a CLASSIC script with a small namespaced export so the
 * same source can be inlined into the dashboard at build time and required by
 * Node's CommonJS tests: no `import`, no bundler, no runtime package, and no
 * browser/MCP registration in this unit.
 *
 * Typed numbers. Python keeps an integer token and a float token as different
 * typed values, and the canonical hash depends on that distinction (`1` and
 * `1.0` hash differently). JavaScript has one number type, so this module
 * keeps integer tokens as plain safe-integer `number` values and float tokens
 * as `JsonFloat` wrappers. A wrapper is deliberately NOT an integer for
 * structural validation, JSON-encodes back to its number, and preserves
 * signed zero. Values that never travelled through this module's decoder
 * (hand-built JS objects) are treated as: an integer when `Number.isInteger`,
 * otherwise a float.
 *
 * Nothing here reads a clock, the environment, the network, a file or a
 * provider configuration, and nothing here registers a browser tool.
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.WatchdogCatalog = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  //: The JSON-safe integer ceiling: 2**53 - 1.
  var MAX_SAFE_INT = 9007199254740991;

  //: Inclusive container-depth bound (a root container is depth 1).
  var MAX_JSON_DEPTH = 64;

  //: The only published schema version this module understands.
  var SCHEMA_VERSION = 1;

  //: The frozen error codes and their literal public messages.
  var ERROR_MESSAGES = {
    invalid_json: "Invalid catalogue JSON.",
    invalid_snapshot: "Invalid catalogue snapshot.",
    unsupported_schema_version: "Unsupported catalogue schema version."
  };

  var INVALID_JSON = "invalid_json";
  var INVALID_SNAPSHOT = "invalid_snapshot";
  var UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version";

  /**
   * A rejected raw or structural document.
   *
   * `code`/`message` carry the frozen public literal; `toString()` is the
   * fixed public message and never the offending text, an offset or a path.
   * `toPayload()` returns a FRESH two-field envelope on every call.
   */
  function CatalogError(code, message) {
    if (!(this instanceof CatalogError)) {
      return new CatalogError(code, message);
    }
    var resolved = message === undefined ? ERROR_MESSAGES[code] : message;
    Error.call(this, resolved);
    this.name = "CatalogError";
    this.code = code;
    this.message = resolved;
  }
  CatalogError.prototype = Object.create(Error.prototype);
  CatalogError.prototype.constructor = CatalogError;
  CatalogError.prototype.toPayload = function () {
    return { ok: false, error: { code: this.code, message: this.message } };
  };

  // ---------- typed value model ------------------------------------------
  //
  // Python's decoder keeps an integer token and a float token as different
  // typed values, and the canonical hash depends on it. JavaScript has one
  // number type, so a float TOKEN decodes to a `JsonFloat`: it is not an
  // integer for the strict snapshot fields, it JSON-encodes back to its
  // number, and it preserves signed zero.

  /**
   * A float-token number. `value` is the binary64 value; the wrapper is what
   * keeps "1.0" distinguishable from the integer token "1".
   */
  function JsonFloat(value) {
    this.value = value;
  }
  JsonFloat.prototype.valueOf = function () { return this.value; };
  JsonFloat.prototype.toJSON = function () { return this.value; };
  JsonFloat.prototype.toString = function () { return String(this.value); };

  /** True iff `value` is a float TOKEN decoded by this module. */
  function isFloatToken(value) {
    return value instanceof JsonFloat;
  }

  // ---------- strict raw JSON syntax -------------------------------------

  var _SAFE_INT_TEXT = String(MAX_SAFE_INT);
  var _ESCAPES = {
    0x22: "\"", 0x5c: "\\", 0x2f: "/", 0x62: "\b",
    0x66: "\f", 0x6e: "\n", 0x72: "\r", 0x74: "\t"
  };

  function _isWhitespace(code) {
    return code === 0x20 || code === 0x09 || code === 0x0a || code === 0x0d;
  }

  function _isDigit(code) {
    return code >= 0x30 && code <= 0x39;
  }

  function _defineMember(target, key, value) {
    // `Object.defineProperty` makes a decoded "__proto__" an ordinary OWN
    // member instead of replacing the object's prototype.
    Object.defineProperty(target, key, {
      value: value, enumerable: true, writable: true, configurable: true
    });
  }

  function _reject() {
    throw new CatalogError(INVALID_JSON);
  }

  function _checkSafeIntegerToken(token) {
    var digits = token.charAt(0) === "-" ? token.slice(1) : token;
    digits = digits.replace(/^0+(?=[0-9])/, "");
    if (digits.length > _SAFE_INT_TEXT.length) {
      _reject();
    }
    if (digits.length === _SAFE_INT_TEXT.length && digits > _SAFE_INT_TEXT) {
      _reject();
    }
  }

  /**
   * A recursive-descent JSON decoder. `guards` enables the published-document
   * numeric-safety rules; strict SYNTAX, duplicate-member rejection and the
   * container-depth bound always apply.
   */
  function _Parser(text, guards) {
    this.text = text;
    this.index = 0;
    this.length = text.length;
    this.guards = guards;
  }

  _Parser.prototype.fail = function () {
    _reject();
  };

  _Parser.prototype.skipWhitespace = function () {
    while (this.index < this.length &&
           _isWhitespace(this.text.charCodeAt(this.index))) {
      this.index += 1;
    }
  };

  _Parser.prototype.parseDocument = function () {
    this.skipWhitespace();
    var value = this.parseValue(0);
    this.skipWhitespace();
    if (this.index !== this.length) {
      this.fail();
    }
    return value;
  };

  _Parser.prototype.parseValue = function (depth) {
    if (this.index >= this.length) {
      this.fail();
    }
    var code = this.text.charCodeAt(this.index);
    if (code === 0x7b) {                       // {
      if (depth + 1 > MAX_JSON_DEPTH) {
        this.fail();
      }
      return this.parseObject(depth + 1);
    }
    if (code === 0x5b) {                       // [
      if (depth + 1 > MAX_JSON_DEPTH) {
        this.fail();
      }
      return this.parseArray(depth + 1);
    }
    if (code === 0x22) {                       // "
      return this.parseString();
    }
    if (code === 0x74) {                       // t
      return this.parseLiteral("true", true);
    }
    if (code === 0x66) {                       // f
      return this.parseLiteral("false", false);
    }
    if (code === 0x6e) {                       // n
      return this.parseLiteral("null", null);
    }
    if (code === 0x2d || _isDigit(code)) {     // - or digit
      return this.parseNumber();
    }
    this.fail();
  };

  _Parser.prototype.parseLiteral = function (word, value) {
    if (this.text.slice(this.index, this.index + word.length) !== word) {
      this.fail();
    }
    this.index += word.length;
    return value;
  };

  _Parser.prototype.parseObject = function (depth) {
    this.index += 1;                           // {
    var result = {};
    var seen = Object.create(null);
    this.skipWhitespace();
    if (this.text.charCodeAt(this.index) === 0x7d) {
      this.index += 1;
      return result;
    }
    for (;;) {
      this.skipWhitespace();
      if (this.text.charCodeAt(this.index) !== 0x22) {
        this.fail();
      }
      var key = this.parseString();
      if (Object.prototype.hasOwnProperty.call(seen, key)) {
        // No last-write-wins repair: a decoded duplicate is rejected at every
        // object nesting, including escape-equivalent names.
        this.fail();
      }
      seen[key] = true;
      this.skipWhitespace();
      if (this.text.charCodeAt(this.index) !== 0x3a) {
        this.fail();
      }
      this.index += 1;                         // :
      this.skipWhitespace();
      _defineMember(result, key, this.parseValue(depth));
      this.skipWhitespace();
      var code = this.text.charCodeAt(this.index);
      if (code === 0x2c) {                     // ,
        this.index += 1;
        continue;
      }
      if (code === 0x7d) {                     // }
        this.index += 1;
        return result;
      }
      this.fail();
    }
  };

  _Parser.prototype.parseArray = function (depth) {
    this.index += 1;                           // [
    var result = [];
    this.skipWhitespace();
    if (this.text.charCodeAt(this.index) === 0x5d) {
      this.index += 1;
      return result;
    }
    for (;;) {
      this.skipWhitespace();
      result.push(this.parseValue(depth));
      this.skipWhitespace();
      var code = this.text.charCodeAt(this.index);
      if (code === 0x2c) {                     // ,
        this.index += 1;
        continue;
      }
      if (code === 0x5d) {                     // ]
        this.index += 1;
        return result;
      }
      this.fail();
    }
  };

  _Parser.prototype.parseString = function () {
    this.index += 1;                           // opening quote
    var chunks = [];
    var start = this.index;
    for (;;) {
      if (this.index >= this.length) {
        this.fail();
      }
      var code = this.text.charCodeAt(this.index);
      if (code === 0x22) {                     // closing quote
        chunks.push(this.text.slice(start, this.index));
        this.index += 1;
        return chunks.join("");
      }
      if (code < 0x20) {
        // Raw control characters are never accepted inside a string, and no
        // sanitizing or BOM/whitespace preprocessing happens anywhere.
        this.fail();
      }
      if (code !== 0x5c) {                     // ordinary character
        this.index += 1;
        continue;
      }
      chunks.push(this.text.slice(start, this.index));
      this.index += 1;                         // backslash
      if (this.index >= this.length) {
        this.fail();
      }
      var escape = this.text.charCodeAt(this.index);
      if (escape === 0x75) {                   // u
        var hex = this.text.slice(this.index + 1, this.index + 5);
        if (hex.length !== 4 || !/^[0-9A-Fa-f]{4}$/.test(hex)) {
          this.fail();
        }
        chunks.push(String.fromCharCode(parseInt(hex, 16)));
        this.index += 5;
      } else if (Object.prototype.hasOwnProperty.call(_ESCAPES, escape)) {
        chunks.push(_ESCAPES[escape]);
        this.index += 1;
      } else {
        this.fail();
      }
      start = this.index;
    }
  };

  _Parser.prototype.parseNumber = function () {
    var start = this.index;
    if (this.text.charCodeAt(this.index) === 0x2d) {  // -
      this.index += 1;
    }
    if (!_isDigit(this.text.charCodeAt(this.index))) {
      this.fail();
    }
    if (this.text.charCodeAt(this.index) === 0x30) {  // 0
      this.index += 1;
    } else {
      while (_isDigit(this.text.charCodeAt(this.index))) {
        this.index += 1;
      }
    }
    var isFloat = false;
    if (this.text.charCodeAt(this.index) === 0x2e) {  // .
      isFloat = true;
      this.index += 1;
      if (!_isDigit(this.text.charCodeAt(this.index))) {
        this.fail();
      }
      while (_isDigit(this.text.charCodeAt(this.index))) {
        this.index += 1;
      }
    }
    var marker = this.text.charCodeAt(this.index);
    if (marker === 0x65 || marker === 0x45) {         // e / E
      isFloat = true;
      this.index += 1;
      var sign = this.text.charCodeAt(this.index);
      if (sign === 0x2b || sign === 0x2d) {           // + / -
        this.index += 1;
      }
      if (!_isDigit(this.text.charCodeAt(this.index))) {
        this.fail();
      }
      while (_isDigit(this.text.charCodeAt(this.index))) {
        this.index += 1;
      }
    }
    var token = this.text.slice(start, this.index);
    if (!isFloat) {
      if (this.guards) {
        _checkSafeIntegerToken(token);
      }
      return Number(token);
    }
    var value = Number(token);
    if (this.guards) {
      if (!isFinite(value)) {
        _reject();
      }
      if (value === Math.trunc(value) &&
          !(-MAX_SAFE_INT <= value && value <= MAX_SAFE_INT)) {
        _reject();
      }
    }
    return new JsonFloat(value);
  };

  /**
   * Strictly decode a raw JSON document string into typed values.
   *
   * Returns the decoded value on lexical acceptance (booleans, integer tokens
   * as plain safe integers, float tokens as `JsonFloat`, strings, null,
   * arrays and objects), preserving the integer-versus-float distinction,
   * signed zero and escaped strings (including lone surrogates). Raises
   * `CatalogError("invalid_json")` on any intended raw failure. A non-string
   * argument raises `TypeError` -- a programming error, never a public code.
   */
  function loadJson(text) {
    if (typeof text !== "string") {
      throw new TypeError("loadJson() requires a raw JSON document string");
    }
    return new _Parser(text, true).parseDocument();
  }

  /**
   * The same typed decoder WITHOUT the published-document numeric-safety
   * guards, for non-published rule tables and producer-side fragments.
   *
   * Strict syntax, duplicate-member rejection and the container-depth bound
   * still apply; `NaN`/`Infinity`, unsafe integers and unsafe float tokens do
   * not. An integer beyond 2**53 is only representable approximately in
   * JavaScript, so this is NEVER a public acceptance path -- published
   * documents must use `loadJson`/`loadSnapshot`.
   */
  function decodeTypedJson(text) {
    if (typeof text !== "string") {
      throw new TypeError("decodeTypedJson() requires a raw JSON document string");
    }
    return new _Parser(text, false).parseDocument();
  }

  // ---------- typed canonical encoding -----------------------------------

  var _SHORT_ESCAPES = {
    0x22: "\\\"", 0x5c: "\\\\", 0x08: "\\b",
    0x09: "\\t", 0x0a: "\\n", 0x0c: "\\f", 0x0d: "\\r"
  };

  function _hexEscape(code) {
    return "\\u" + ("000" + code.toString(16)).slice(-4);
  }

  function _encodeString(text) {
    var out = "\"";
    for (var index = 0; index < text.length; index += 1) {
      var code = text.charCodeAt(index);
      if (Object.prototype.hasOwnProperty.call(_SHORT_ESCAPES, code)) {
        out += _SHORT_ESCAPES[code];
      } else if (code >= 0x20 && code < 0x7f) {
        out += text.charAt(index);
      } else {
        // ensure_ascii=True: DEL, C0 controls, BMP and every half of an
        // astral pair are written as lowercase \uXXXX escapes.
        out += _hexEscape(code);
      }
    }
    return out + "\"";
  }

  /**
   * Compare two strings by Unicode CODE POINT (Python's key order), never by
   * UTF-16 code unit: an astral key must sort after U+E000, not beside D800.
   */
  function _compareCodePoints(left, right) {
    var i = 0;
    var j = 0;
    while (i < left.length && j < right.length) {
      var leftCode = left.codePointAt(i);
      var rightCode = right.codePointAt(j);
      if (leftCode !== rightCode) {
        return leftCode < rightCode ? -1 : 1;
      }
      i += leftCode > 0xffff ? 2 : 1;
      j += rightCode > 0xffff ? 2 : 1;
    }
    return (left.length - i) - (right.length - j);
  }

  /** Python `repr(float)`-compatible spelling of a finite binary64 value. */
  function _formatFloat(value) {
    if (!isFinite(value)) {
      throw new TypeError("canonical encoding requires finite numbers");
    }
    if (value === 0) {
      return Object.is(value, -0) ? "-0.0" : "0.0";
    }
    var magnitude = Math.abs(value);
    if (magnitude < 1e-4 || magnitude >= 1e16) {
      var parts = value.toExponential().split("e");
      var exponent = parseInt(parts[1], 10);
      var digits = String(Math.abs(exponent));
      if (digits.length < 2) {
        digits = "0" + digits;
      }
      return parts[0] + "e" + (exponent < 0 ? "-" : "+") + digits;
    }
    var text = String(value);
    if (text.indexOf(".") === -1) {
      text += ".0";
    }
    return text;
  }

  function _encodeInteger(value) {
    if (Object.is(value, -0)) {
      return "0";
    }
    var text = String(value);
    if (text.indexOf("e") !== -1 || text.indexOf("E") !== -1) {
      throw new TypeError(
        "canonical encoding requires a representable JSON integer");
    }
    return text;
  }

  function _encodeValue(value) {
    if (value === null) {
      return "null";
    }
    if (value === true) {
      return "true";
    }
    if (value === false) {
      return "false";
    }
    if (value instanceof JsonFloat) {
      return _formatFloat(value.value);
    }
    if (typeof value === "number") {
      if (!isFinite(value)) {
        throw new TypeError("canonical encoding requires finite numbers");
      }
      return Number.isInteger(value) ? _encodeInteger(value)
                                     : _formatFloat(value);
    }
    if (typeof value === "string") {
      return _encodeString(value);
    }
    if (Array.isArray(value)) {
      var items = [];
      for (var index = 0; index < value.length; index += 1) {
        items.push(_encodeValue(value[index]));
      }
      return "[" + items.join(",") + "]";
    }
    if (typeof value === "object") {
      var prototype = Object.getPrototypeOf(value);
      if (prototype !== null && prototype !== Object.prototype) {
        // A Date/Map/Set/custom instance is not a JSON value; encoding it as
        // an empty object would silently disagree with the reference encoder.
        throw new TypeError("canonical encoding requires a JSON value");
      }
      var keys = Object.keys(value).sort(_compareCodePoints);
      var members = [];
      for (var position = 0; position < keys.length; position += 1) {
        var key = keys[position];
        members.push(_encodeString(key) + ":" + _encodeValue(value[key]));
      }
      return "{" + members.join(",") + "}";
    }
    throw new TypeError("canonical encoding requires a JSON value");
  }

  /**
   * The typed canonical encoding of an already raw-accepted JSON value.
   *
   * UTF-8, object keys sorted by Unicode code point, compact `,`/`:` separators,
   * finite values and `ensure_ascii=True` -- the reference `json.dumps` flags.
   * This is a GENERIC whole-value encoder: it excludes NO keys, so a root or
   * nested `snapshot_id` is ordinary content. Callers that want a snapshot
   * body must exclude that member themselves.
   */
  function canonicalText(value) {
    return _encodeValue(value);
  }

  /**
   * `canonicalText` as UTF-8 bytes. The canonical text is pure ASCII by
   * construction (`ensure_ascii=True`), so one byte per code unit is exact.
   */
  function canonicalBytes(value) {
    var text = canonicalText(value);
    var bytes = new Uint8Array(text.length);
    for (var index = 0; index < text.length; index += 1) {
      bytes[index] = text.charCodeAt(index) & 0xff;
    }
    return bytes;
  }

  // ---------- SHA-256 ----------------------------------------------------
  //
  // A small, dependency-free digest so the canonical body hash works
  // synchronously in both the browser and Node without a runtime package or
  // an async Web Crypto call.

  var _SHA256_K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ];

  function _rotateRight(value, bits) {
    return (value >>> bits) | (value << (32 - bits));
  }

  function _sha256Hex(bytes) {
    var length = bytes.length;
    // `length + 9` = message + 0x80 + the 8-byte bit length. When that total is
    // ALREADY a multiple of 64 it needs no extra block, so this is a ceiling,
    // not "truncate then add one block".
    var padded = Math.ceil((length + 9) / 64) * 64;
    var block = new Uint8Array(padded);
    block.set(bytes);
    block[length] = 0x80;
    var bitsHigh = Math.floor(length / 0x20000000);
    var bitsLow = (length << 3) >>> 0;
    block[padded - 8] = (bitsHigh >>> 24) & 0xff;
    block[padded - 7] = (bitsHigh >>> 16) & 0xff;
    block[padded - 6] = (bitsHigh >>> 8) & 0xff;
    block[padded - 5] = bitsHigh & 0xff;
    block[padded - 4] = (bitsLow >>> 24) & 0xff;
    block[padded - 3] = (bitsLow >>> 16) & 0xff;
    block[padded - 2] = (bitsLow >>> 8) & 0xff;
    block[padded - 1] = bitsLow & 0xff;
    var hash = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    ]);
    var schedule = new Uint32Array(64);
    for (var offset = 0; offset < padded; offset += 64) {
      for (var index = 0; index < 16; index += 1) {
        var at = offset + index * 4;
        schedule[index] = ((block[at] << 24) | (block[at + 1] << 16) |
                           (block[at + 2] << 8) | block[at + 3]) >>> 0;
      }
      for (var slot = 16; slot < 64; slot += 1) {
        var previous = schedule[slot - 15];
        var earlier = schedule[slot - 2];
        var sigma0 = _rotateRight(previous, 7) ^ _rotateRight(previous, 18) ^
                     (previous >>> 3);
        var sigma1 = _rotateRight(earlier, 17) ^ _rotateRight(earlier, 19) ^
                     (earlier >>> 10);
        schedule[slot] = (schedule[slot - 16] + sigma0 + schedule[slot - 7] +
                          sigma1) >>> 0;
      }
      var a = hash[0];
      var b = hash[1];
      var c = hash[2];
      var d = hash[3];
      var e = hash[4];
      var f = hash[5];
      var g = hash[6];
      var h = hash[7];
      for (var round = 0; round < 64; round += 1) {
        var sum1 = _rotateRight(e, 6) ^ _rotateRight(e, 11) ^
                   _rotateRight(e, 25);
        var choice = (e & f) ^ (~e & g);
        var temp1 = (h + sum1 + choice + _SHA256_K[round] +
                     schedule[round]) >>> 0;
        var sum0 = _rotateRight(a, 2) ^ _rotateRight(a, 13) ^
                   _rotateRight(a, 22);
        var majority = (a & b) ^ (a & c) ^ (b & c);
        var temp2 = (sum0 + majority) >>> 0;
        h = g;
        g = f;
        f = e;
        e = (d + temp1) >>> 0;
        d = c;
        c = b;
        b = a;
        a = (temp1 + temp2) >>> 0;
      }
      hash[0] = (hash[0] + a) >>> 0;
      hash[1] = (hash[1] + b) >>> 0;
      hash[2] = (hash[2] + c) >>> 0;
      hash[3] = (hash[3] + d) >>> 0;
      hash[4] = (hash[4] + e) >>> 0;
      hash[5] = (hash[5] + f) >>> 0;
      hash[6] = (hash[6] + g) >>> 0;
      hash[7] = (hash[7] + h) >>> 0;
    }
    var hex = "";
    for (var word = 0; word < 8; word += 1) {
      hex += ("00000000" + hash[word].toString(16)).slice(-8);
    }
    return hex;
  }

  /**
   * The canonical body digest: SHA-256 of `canonicalBytes` over the mapping
   * with ONLY the ROOT `snapshot_id` removed.
   *
   * Precondition: a JSON-compatible, raw-safe mapping. A NESTED `snapshot_id`
   * is ordinary content and is hashed normally. The argument is never mutated
   * and the same value works before and after the root id is set.
   */
  function snapshotHash(snapshot) {
    if (!_isPlainObject(snapshot)) {
      throw new TypeError("snapshotHash() requires a JSON-compatible mapping");
    }
    var body = {};
    var keys = Object.keys(snapshot);
    for (var index = 0; index < keys.length; index += 1) {
      if (keys[index] !== "snapshot_id") {
        _defineMember(body, keys[index], snapshot[keys[index]]);
      }
    }
    return _sha256Hex(canonicalBytes(body));
  }

  // ---------- strict structural validation of a published snapshot -------

  var _TOP_LEVEL_FIELDS = [
    "cadence_s", "provider_order", "providers", "schema_version",
    "snapshot_id", "tick_epoch", "warnings"
  ];
  var _PROVIDER_FIELDS = [
    "api_type", "chat_completions_url", "display_name", "model_ids",
    "models", "stale"
  ];
  var _MODEL_FIELDS = [
    "architecture", "context_length", "description", "id", "name",
    "pricing", "supported_parameters"
  ];
  var _ARCHITECTURE_FIELDS = ["input_modalities", "output_modalities"];
  var _PRICING_FIELDS = ["completion", "prompt"];

  //: The frozen warning vocabulary in its deduplicated publication ORDER.
  var _WARNING_ORDER = [
    "tick_epoch_unknown", "metadata_missing", "metadata_invalid"
  ];

  //: The `snapshot_id` shape: exactly 64 LOWERCASE hex characters.
  var _SNAPSHOT_ID = /^[0-9a-f]{64}$/;

  function _isPlainObject(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value) &&
           !(value instanceof JsonFloat);
  }

  function _hasExactFields(value, fields) {
    var keys = Object.keys(value);
    if (keys.length !== fields.length) {
      return false;
    }
    for (var index = 0; index < fields.length; index += 1) {
      if (!Object.prototype.hasOwnProperty.call(value, fields[index])) {
        return false;
      }
    }
    return true;
  }

  function _isStrictInt(value) {
    return typeof value === "number" && Number.isInteger(value);
  }

  function _isSafeNonNegInt(value) {
    return _isStrictInt(value) && value >= 0 && value <= MAX_SAFE_INT;
  }

  function _isNonEmptyString(value) {
    return typeof value === "string" && value.length > 0;
  }

  // Python whitespace: str.strip() removes exactly these code points.
  var _PY_WHITESPACE_CODES = [
    "\\t", "\\n", "\\u000b", "\\u000c", "\\r", " ", "\\u0085", "\\u00a0",
    "\\u1680", "\\u2000-\\u200a", "\\u2028", "\\u2029", "\\u202f",
    "\\u205f", "\\u3000"
  ].join("");
  var _PY_WHITESPACE = new RegExp(
    "^[" + _PY_WHITESPACE_CODES + "]+|[" + _PY_WHITESPACE_CODES + "]+$", "g");

  function _decimalDigitValue(character) {
    if (!/\p{Nd}/u.test(character)) {
      return -1;
    }
    // Nd characters always form contiguous runs of ten starting at zero, so
    // walking back to the run start yields the digit value.
    var code = character.codePointAt(0);
    var start = code;
    while (start > 0 && /\p{Nd}/u.test(String.fromCodePoint(start - 1))) {
      start -= 1;
    }
    return code - start;
  }

  /**
   * Python `float(text)` acceptance, reduced to the question the pricing
   * predicate asks: does the string parse to a FINITE binary64 value?
   *
   * Surrounding Python whitespace is stripped, a single underscore is allowed
   * between digits, Unicode decimal digits are accepted, and the special
   * `inf`/`infinity`/`nan` spellings parse but are not finite.
   */
  function _numericStringIsFinite(text) {
    var stripped = text.replace(_PY_WHITESPACE, "");
    if (stripped.length === 0) {
      return false;
    }
    var body = stripped;
    if (body.charAt(0) === "+" || body.charAt(0) === "-") {
      body = body.slice(1);
    }
    if (/^(inf|infinity|nan)$/i.test(body)) {
      return false;
    }
    var folded = "";
    for (const character of body) {
      var digit = _decimalDigitValue(character);
      folded += digit >= 0 ? String(digit) : character;
    }
    // Underscores are legal ONLY between digits.
    if (folded.indexOf("_") !== -1) {
      if (/(^|[^0-9])_|_([^0-9]|$)/.test(folded)) {
        return false;
      }
      folded = folded.replace(/_/g, "");
    }
    if (!/^([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/.test(folded)) {
      return false;
    }
    return isFinite(Number((stripped.charAt(0) === "-" ? "-" : "") + folded));
  }

  function _isFiniteScalar(value) {
    if (value === null) {
      return true;
    }
    if (typeof value === "boolean") {
      return false;
    }
    if (value instanceof JsonFloat) {
      return _isFiniteNumber(value.value);
    }
    if (typeof value === "number") {
      return _isFiniteNumber(value);
    }
    if (typeof value === "string") {
      return _numericStringIsFinite(value);
    }
    return false;
  }

  function _isFiniteNumber(value) {
    if (!isFinite(value)) {
      return false;
    }
    if (value === Math.trunc(value) &&
        !(-MAX_SAFE_INT <= value && value <= MAX_SAFE_INT)) {
      return false;
    }
    return true;
  }

  // ---- bracketed authority hosts -----------------------------------------
  //
  // Python's `urlsplit` refuses a netloc that carries only one of `[`/`]`, and
  // its `_check_bracketed_netloc`/`_check_bracketed_host` pair allows nothing
  // before `[`, nothing after the closing `]` except an empty or `:`-prefixed
  // port, and a bracketed host that is either an IPvFuture literal or a real
  // IPv6 address -- a bracketed IPv4 literal is refused. The helpers below
  // restate that acceptance set dependency-free. They are purely syntactic.

  /** ASCII decimal digits only: `str.isdigit()` would also admit Unicode. */
  function _isAsciiDigits(text) {
    return text.length > 0 && /^[0-9]+$/.test(text);
  }

  /** A dotted-quad IPv4 literal, with `ipaddress.IPv4Address` string rules. */
  function _isIpv4Text(text) {
    var octets = text.split(".");
    if (octets.length !== 4) {
      return false;
    }
    for (var index = 0; index < octets.length; index += 1) {
      var octet = octets[index];
      if (!_isAsciiDigits(octet) || octet.length > 3) {
        return false;
      }
      if (octet.length > 1 && octet.charAt(0) === "0") {
        return false;                      // no leading zeros
      }
      if (Number(octet) > 255) {
        return false;
      }
    }
    return true;
  }

  function _isHextet(text) {
    return text.length > 0 && text.length <= 4 &&
           /^[0-9A-Fa-f]+$/.test(text);
  }

  /** `ipaddress.IPv6Address` string acceptance, scope id included. */
  function _isIpv6Text(text) {
    var address = text;
    var scopeAt = address.indexOf("%");
    if (scopeAt !== -1) {
      var scope = address.slice(scopeAt + 1);
      if (scope.length === 0 || scope.indexOf("%") !== -1) {
        return false;                      // RFC 4007 scope id rules
      }
      address = address.slice(0, scopeAt);
    }
    if (address.length === 0 || address.length > 45) {
      return false;
    }
    // Python splits on ':' with maxsplit=9, so a longer colon run collapses
    // into the tenth part and is refused by the part-count check.
    var parts = [];
    var rest = address;
    while (parts.length < 9) {
      var at = rest.indexOf(":");
      if (at === -1) {
        break;
      }
      parts.push(rest.slice(0, at));
      rest = rest.slice(at + 1);
    }
    parts.push(rest);
    if (parts.length < 3) {
      return false;
    }
    if (parts[parts.length - 1].indexOf(".") !== -1) {
      // A dotted-quad tail stands in for the final two hextets. Only the LAST
      // part is converted, exactly as the reference parser does.
      var dotted = parts.pop();
      if (!_isIpv4Text(dotted)) {
        return false;
      }
      parts.push("0");
      parts.push("0");
    }
    if (parts.length > 9) {
      return false;
    }
    var skipAt = -1;
    for (var index = 1; index < parts.length - 1; index += 1) {
      if (parts[index] !== "") {
        continue;
      }
      if (skipAt !== -1) {
        return false;                      // at most one '::'
      }
      skipAt = index;
    }
    var high;
    var low;
    if (skipAt !== -1) {
      high = skipAt;
      low = parts.length - skipAt - 1;
      if (parts[0] === "") {
        high -= 1;
        if (high !== 0) {
          return false;                    // ':' only as part of '::'
        }
      }
      if (parts[parts.length - 1] === "") {
        low -= 1;
        if (low !== 0) {
          return false;
        }
      }
      if (8 - (high + low) < 1) {
        return false;                      // '::' must cover a hextet
      }
    } else {
      if (parts.length !== 8) {
        return false;
      }
      if (parts[0] === "" || parts[parts.length - 1] === "") {
        return false;
      }
      high = parts.length;
      low = 0;
    }
    for (index = 0; index < high; index += 1) {
      if (!_isHextet(parts[index])) {
        return false;
      }
    }
    for (index = parts.length - low; index < parts.length; index += 1) {
      if (!_isHextet(parts[index])) {
        return false;
      }
    }
    return true;
  }

  /** `_check_bracketed_host`: IPvFuture literal, otherwise IPv6 only. */
  function _isBracketedHost(hostname) {
    if (hostname.charAt(0) === "v") {
      // The reference payload class is `\Av[a-fA-F0-9]+\..+\Z`: Python's dot
      // excludes ONLY LF, so U+2028/U+2029 (and CR) are legal payload
      // characters. JavaScript's dot also excludes those line terminators, so
      // the payload is spelled `[^\n]` -- the reference class, not a blacklist
      // of the separators observed to differ. The whole-URL ASCII
      // whitespace/control scan still refuses LF/CR before this helper runs.
      return /^v[0-9A-Fa-f]+\.[^\n]+$/.test(hostname);
    }
    return _isIpv6Text(hostname);
  }

  /**
   * `urllib.parse._checknetloc`: a REJECTION-ONLY safety check, never a
   * rewrite. A non-ASCII netloc is NFKC-normalized after the delimiters it
   * already carries (`@ : # ?`) are dropped; if that changes the text and the
   * result introduces a `/ ? # @ :` delimiter, the authority has no
   * unambiguous hostname under the IDNA normalisation hostnames use, so the
   * URL is refused. NFKC-neutral names and NFKC changes that introduce no
   * delimiter stay valid, and the accepted string is preserved verbatim.
   */
  function _isNfkcSafeAuthority(authority) {
    if (!/[^\x00-\x7f]/.test(authority)) {
      return true;
    }
    var stripped = authority.replace(/[@:#?]/g, "");
    var normalized = stripped.normalize("NFKC");
    if (normalized === stripped) {
      return true;
    }
    return !/[/?#@:]/.test(normalized);
  }

  /**
   * The frozen SYNTACTIC wiring predicate: a nonempty absolute http/https URL
   * with a hostname, no userinfo, no fragment and no ASCII whitespace or
   * control character. Purely syntactic -- no DNS, no allowlist, no rewriting.
   * A non-ASCII authority whose NFKC normalization introduces a delimiter is
   * refused (`_isNfkcSafeAuthority`), mirroring `urlsplit`'s `_checknetloc`.
   * An EMPTY fragment component (a trailing `#`) is accepted.
   */
  function _isWiringUrl(url) {
    if (typeof url !== "string" || url.length === 0) {
      return false;
    }
    for (var index = 0; index < url.length; index += 1) {
      var code = url.charCodeAt(index);
      if (code <= 0x20 || code === 0x7f) {
        return false;
      }
    }
    var schemeMatch = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(url);
    if (!schemeMatch) {
      return false;
    }
    var scheme = schemeMatch[1].toLowerCase();
    if (scheme !== "http" && scheme !== "https") {
      return false;
    }
    var rest = url.slice(schemeMatch[0].length);
    if (rest.slice(0, 2) !== "//") {
      return false;
    }
    var afterSlashes = rest.slice(2);
    var cut = afterSlashes.search(/[/?#]/);
    var authority = cut === -1 ? afterSlashes : afterSlashes.slice(0, cut);
    var tail = cut === -1 ? "" : afterSlashes.slice(cut);
    if (authority.length === 0) {
      return false;
    }
    var fragmentAt = tail.indexOf("#");
    var fragment = fragmentAt === -1 ? "" : tail.slice(fragmentAt + 1);
    // Any `@` means a userinfo component is present (an empty username is
    // still a username), and userinfo is never valid wiring.
    if (authority.indexOf("@") !== -1) {
      return false;
    }
    var hostname;
    var hasOpen = authority.indexOf("[") !== -1;
    var hasClose = authority.indexOf("]") !== -1;
    if (hasOpen !== hasClose) {
      return false;                       // Python urlsplit: Invalid IPv6 URL
    }
    if (hasOpen) {
      // `_check_bracketed_netloc`: no data before the bracket, and only an
      // empty or `:`-prefixed (never validated) port after the closing one.
      if (authority.charAt(0) !== "[") {
        return false;
      }
      var closing = authority.indexOf("]");
      var afterClosing = authority.slice(closing + 1);
      if (afterClosing.length !== 0 && afterClosing.charAt(0) !== ":") {
        return false;
      }
      hostname = authority.slice(1, closing);
      if (!_isBracketedHost(hostname)) {
        return false;
      }
    } else {
      hostname = authority.split(":")[0];
    }
    // `urlsplit` runs `_checknetloc` on the whole netloc, after the bracketed
    // host was validated; it is rejection-only and never rewrites the URL.
    if (!_isNfkcSafeAuthority(authority)) {
      return false;
    }
    if (hostname.length === 0) {
      return false;
    }
    return fragment.length === 0;
  }

  function _isStringListOrNull(value) {
    if (value === null) {
      return true;
    }
    if (!Array.isArray(value)) {
      return false;
    }
    return value.every((item) => _isNonEmptyString(item));
  }

  function _isPricing(value) {
    if (value === null) {
      return true;
    }
    if (!_isPlainObject(value) || !_hasExactFields(value, _PRICING_FIELDS)) {
      return false;
    }
    return _isFiniteScalar(value.prompt) && _isFiniteScalar(value.completion);
  }

  function _isModel(rawId, model) {
    if (!_isPlainObject(model) || !_hasExactFields(model, _MODEL_FIELDS)) {
      return false;
    }
    if (model.id !== rawId) {
      return false;
    }
    if (!_isNonEmptyString(model.name)) {
      return false;
    }
    if (model.description !== null && typeof model.description !== "string") {
      return false;
    }
    if (model.context_length !== null &&
        !_isSafeNonNegInt(model.context_length)) {
      return false;
    }
    var architecture = model.architecture;
    if (!_isPlainObject(architecture) ||
        !_hasExactFields(architecture, _ARCHITECTURE_FIELDS)) {
      return false;
    }
    if (!_isStringListOrNull(architecture.input_modalities) ||
        !_isStringListOrNull(architecture.output_modalities) ||
        !_isStringListOrNull(model.supported_parameters)) {
      return false;
    }
    return _isPricing(model.pricing);
  }

  function _sameStringSequence(left, right) {
    if (left.length !== right.length) {
      return false;
    }
    for (var index = 0; index < left.length; index += 1) {
      if (left[index] !== right[index]) {
        return false;
      }
    }
    return true;
  }

  function _sameKeySet(left, right) {
    var leftKeys = left.slice().sort(_compareCodePoints);
    var rightKeys = right.slice().sort(_compareCodePoints);
    return _sameStringSequence(leftKeys, rightKeys);
  }

  function _isProvider(entry) {
    if (!_isPlainObject(entry) || !_hasExactFields(entry, _PROVIDER_FIELDS)) {
      return false;
    }
    if (!_isNonEmptyString(entry.display_name) ||
        !_isWiringUrl(entry.chat_completions_url) ||
        !_isNonEmptyString(entry.api_type) ||
        typeof entry.stale !== "boolean") {
      return false;
    }
    var ids = entry.model_ids;
    if (!Array.isArray(ids) || !ids.every(_isNonEmptyString)) {
      return false;
    }
    // Literal Unicode code-point order and uniqueness -- not a natural sort.
    if (new Set(ids).size !== ids.length ||
        !_sameStringSequence(ids.slice().sort(_compareCodePoints), ids)) {
      return false;
    }
    var models = entry.models;
    if (!_isPlainObject(models)) {
      return false;
    }
    var keys = Object.keys(models);
    if (!keys.every(_isNonEmptyString) || !_sameKeySet(keys, ids)) {
      return false;
    }
    return ids.every((rawId) => _isModel(rawId, models[rawId]));
  }

  function _isBody(snapshot) {
    var warnings = snapshot.warnings;
    if (!Array.isArray(warnings) || !warnings.every((code) =>
        typeof code === "string")) {
      return false;
    }
    var present = new Set(warnings);
    if (present.size !== warnings.length) {
      return false;
    }
    var expected = _WARNING_ORDER.filter((code) => present.has(code));
    if (expected.length !== warnings.length ||
        !_sameStringSequence(expected, warnings)) {
      return false;
    }
    var tick = snapshot.tick_epoch;
    if (tick === null) {
      if (!present.has("tick_epoch_unknown")) {
        return false;
      }
    } else if (!_isSafeNonNegInt(tick)) {
      return false;
    }
    var cadence = snapshot.cadence_s;
    if (!_isStrictInt(cadence) || !(cadence > 0) || cadence > MAX_SAFE_INT) {
      return false;
    }
    var order = snapshot.provider_order;
    var providers = snapshot.providers;
    if (!Array.isArray(order) || !_isPlainObject(providers)) {
      return false;
    }
    if (!order.every(_isNonEmptyString)) {
      return false;
    }
    var keys = Object.keys(providers);
    if (!keys.every(_isNonEmptyString)) {
      return false;
    }
    if (new Set(order).size !== order.length || !_sameKeySet(order, keys)) {
      return false;
    }
    return order.every((key) => _isProvider(providers[key]));
  }

  /**
   * Strictly validate an already-PUBLISHED snapshot object.
   *
   * Returns the SAME object on acceptance -- never a copy, never repaired,
   * resealed or canonicalized, and never mutated on success OR failure.
   * Raises `CatalogError` with the frozen structural code on rejection; it
   * never raises `invalid_json` and never an incidental TypeError.
   *
   * Precedence, exactly as frozen: top-level object, required-field set and
   * `schema_version` TYPE gates, then the `schema_version` VALUE gate, then
   * every v1-specific body check, and only then the body-hash verification.
   */
  function validateSnapshot(snapshot) {
    if (!_isPlainObject(snapshot)) {
      throw new CatalogError(INVALID_SNAPSHOT);
    }
    if (!_hasExactFields(snapshot, _TOP_LEVEL_FIELDS)) {
      throw new CatalogError(INVALID_SNAPSHOT);
    }
    var version = snapshot.schema_version;
    if (!_isStrictInt(version)) {
      throw new CatalogError(INVALID_SNAPSHOT);
    }
    if (version !== SCHEMA_VERSION) {
      throw new CatalogError(UNSUPPORTED_SCHEMA_VERSION);
    }
    if (!_isBody(snapshot)) {
      throw new CatalogError(INVALID_SNAPSHOT);
    }
    var snapshotId = snapshot.snapshot_id;
    if (typeof snapshotId !== "string" || !_SNAPSHOT_ID.test(snapshotId)) {
      throw new CatalogError(INVALID_SNAPSHOT);
    }
    // The body checks proved every remaining value JSON-compatible and
    // raw-safe, so the canonical encoder below cannot fail on this object.
    if (snapshotId !== snapshotHash(snapshot)) {
      throw new CatalogError(INVALID_SNAPSHOT);
    }
    return snapshot;
  }

  /** Exactly `validateSnapshot(loadJson(text))`: raw faults before structure. */
  function loadSnapshot(text) {
    return validateSnapshot(loadJson(text));
  }

  // ---------- optional capability projection and normalization ----------
  //
  // Two small PURE functions extend this module for the optional capability
  // fields. `projectCapabilities` degrades one upstream model fragment to the
  // public capability shape -- unknown and malformed optional metadata both
  // become null and NEVER drop an endpoint, coerce a value or leak a private
  // field -- and `normalizeCapabilities` derives the query shape from an
  // already-valid projection. Neither reads a clock, file, network or config.

  var _CAPABILITY_FIELD_ORDER = [
    "context_length", "input_modalities", "output_modalities",
    "supported_parameters"
  ];
  var _CAPABILITY_WARNING_ORDER = ["metadata_missing", "metadata_invalid"];
  //: Distinguishes an ABSENT upstream member from an explicit JSON null.
  var _MISSING = {};

  function _member(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key)
      ? object[key] : _MISSING;
  }

  function _readMember(object, key) {
    var value = _member(object, key);
    return value === _MISSING ? undefined : value;
  }

  function _dedupCapabilityWarnings(codes) {
    var present = new Set(codes.filter((code) => code !== null &&
                                        code !== undefined));
    return _CAPABILITY_WARNING_ORDER.filter((code) => present.has(code));
  }

  function _projectCapabilityContext(value) {
    if (value === _MISSING || value === null) {
      return [null, "metadata_missing"];
    }
    if (_isSafeNonNegInt(value)) {
      return [value, null];
    }
    return [null, "metadata_invalid"];
  }

  function _projectCapabilityArray(value) {
    if (value === _MISSING || value === null) {
      return [null, "metadata_missing"];
    }
    if (_isStringListOrNull(value)) {
      // A fresh copy: order, duplicates and casing are preserved.
      return [value.slice(), null];
    }
    return [null, "metadata_invalid"];
  }

  /**
   * Project one upstream capability fragment to the public capability shape.
   *
   * The result is a NEW `[projected, warnings]` pair. `projected` carries
   * exactly `context_length`, `architecture` (exactly `input_modalities` and
   * `output_modalities`) and `supported_parameters`; every container is fresh
   * and every retained array is a copy with its original casing, order and
   * duplicates. Unknown and malformed optional metadata degrade to null and
   * never drop an endpoint, coerce a value or serialize a private field.
   */
  function projectCapabilities(raw) {
    if (!_isPlainObject(raw)) {
      throw new TypeError(
        "projectCapabilities() requires an upstream capability mapping");
    }
    var context = _projectCapabilityContext(_member(raw, "context_length"));
    var architecture = _member(raw, "architecture");
    var inputPair;
    var outputPair;
    if (_isPlainObject(architecture)) {
      inputPair = _projectCapabilityArray(_member(architecture,
                                                  "input_modalities"));
      outputPair = _projectCapabilityArray(_member(architecture,
                                                   "output_modalities"));
    } else if (architecture === _MISSING || architecture === null) {
      // An absent or explicitly-null parent: BOTH children are unknown.
      inputPair = [null, "metadata_missing"];
      outputPair = [null, "metadata_missing"];
    } else {
      // A malformed non-null parent makes both children unknown.
      inputPair = [null, "metadata_invalid"];
      outputPair = [null, "metadata_invalid"];
    }
    var parameters = _projectCapabilityArray(_member(raw,
                                                     "supported_parameters"));
    var projected = {
      context_length: context[0],
      architecture: {
        input_modalities: inputPair[0],
        output_modalities: outputPair[0]
      },
      supported_parameters: parameters[0]
    };
    var warnings = _dedupCapabilityWarnings(
      [context[1], inputPair[1], outputPair[1], parameters[1]]);
    return [projected, warnings];
  }

  /** ASCII-only lowercasing: A-Z fold to a-z; everything else stays literal. */
  function _asciiLower(text) {
    return text.replace(/[A-Z]/g, (character) => character.toLowerCase());
  }

  function _normalizeCapabilityArray(values) {
    if (values === null) {
      return null;
    }
    var normalized = [];
    var seen = new Set();
    for (var index = 0; index < values.length; index += 1) {
      var folded = _asciiLower(values[index]);
      if (!seen.has(folded)) {
        seen.add(folded);
        normalized.push(folded);
      }
    }
    return normalized;
  }

  /**
   * Normalize a valid public capability projection to the query shape.
   *
   * Precondition: a valid public capability projection (or a validated public
   * model object, whose extra members are ignored). The result is a NEW
   * mapping with exactly `context_length`, `input_modalities`,
   * `output_modalities`, `supported_parameters` and `unknown_fields`. Known
   * arrays are ASCII-folded and first-occurrence deduplicated; null stays
   * null; a known empty `[]` is not unknown. The input is never mutated.
   */
  function normalizeCapabilities(projected) {
    var normalized = {
      context_length: projected.context_length,
      input_modalities: _normalizeCapabilityArray(
        projected.architecture.input_modalities),
      output_modalities: _normalizeCapabilityArray(
        projected.architecture.output_modalities),
      supported_parameters: _normalizeCapabilityArray(
        projected.supported_parameters)
    };
    normalized.unknown_fields = _CAPABILITY_FIELD_ORDER.filter(
      (field) => normalized[field] === null);
    return normalized;
  }

  // ---------- full public model projection -------------------------------

  function _projectPricingScalar(value) {
    // Preserved VERBATIM -- original type and spelling -- or null when the
    // frozen finite-scalar predicate rejects it.
    return _isFiniteScalar(value) ? value : null;
  }

  function _projectPricing(value) {
    if (!_isPlainObject(value)) {
      // Absent, null or non-object pricing is unknown OVERALL.
      return null;
    }
    return {
      prompt: _projectPricingScalar(_member(value, "prompt")),
      completion: _projectPricingScalar(_member(value, "completion"))
    };
  }

  function _projectName(rawId, fragment) {
    var name = _readMember(fragment, "name");
    if (typeof name === "string" && name.length > 0) {
      return name;
    }
    return rawId;
  }

  function _projectDescription(fragment) {
    var description = _readMember(fragment, "description");
    return typeof description === "string" ? description : null;
  }

  /**
   * Project one tracked raw id and its upstream fragment to the public model.
   *
   * `rawId` is the TRACKED key and is authoritative: an upstream `id` member
   * is ignored regardless of value, type or absence. The result is a NEW
   * `[model, warnings]` pair whose model carries exactly `id`, `name`,
   * `description`, `context_length`, `architecture`, `supported_parameters`
   * and `pricing`. Warnings describe ONLY the four capability fields: a
   * missing/null metadata container is `metadata_missing`; a non-null
   * non-object container is `metadata_invalid` alone.
   */
  function projectModel(rawId, raw) {
    if (typeof rawId !== "string" || rawId.length === 0) {
      throw new TypeError(
        "projectModel() requires a nonempty tracked model id");
    }
    var fragment;
    var overrideWarnings;
    if (_isPlainObject(raw)) {
      fragment = raw;
      overrideWarnings = null;
    } else {
      // Absent, explicit null and an ACTUAL empty object are all unknown; a
      // non-null non-object container is malformed. The endpoint is never
      // dropped or salvaged.
      fragment = {};
      overrideWarnings = (raw === undefined || raw === null)
        ? null : ["metadata_invalid"];
    }
    var pair = projectCapabilities(fragment);
    var projected = pair[0];
    var model = {
      id: rawId,
      name: _projectName(rawId, fragment),
      description: _projectDescription(fragment),
      context_length: projected.context_length,
      architecture: projected.architecture,
      supported_parameters: projected.supported_parameters,
      pricing: _projectPricing(_readMember(fragment, "pricing"))
    };
    return [model, overrideWarnings === null ? pair[1] : overrideWarnings];
  }

  // ---------- pure ordering and free-marker helpers -----------------------
  //
  // Ports of the dependency-free primitives in `watchdog_catalog/query.py`:
  // `strip_free_marker`, `natural_key` and `endpoint_key` (README, "Total
  // ordering rule" and "Natural comparator"). They are PURE: each reads only
  // its arguments, returns fresh data, never mutates its inputs and never
  // touches a clock, the environment, configuration, the network or a
  // provider SDK. They carry no tool-argument validation or error envelope --
  // that belongs to the later query units.

  //: Python `$` (non-MULTILINE) also matches before ONE final LF, while JS `$`
  //: is strict end-of-input; `(?=\n?$)` restores the reference semantics.
  var FREE_SUFFIX = /[-:_]free(?=\n?$)/i;
  var FREE_PREFIX = /^free[-:_]/i;

  /**
   * Remove ONLY a delimiter-bounded free marker, exactly like the reference.
   *
   * ONE suffix pass, then ONE prefix pass -- never recursive or global
   * stripping -- then fall back to the original input when a strip would empty
   * it. Only `-`, `:` and `_` bound a marker, so bare `free`, delimiter-only
   * labels, `vendor/freebie` and `vendor/freetier` are returned verbatim. Every
   * unstripped character survives: case, whitespace and namespace text are
   * preserved and the id is never renamed or normalised.
   */
  function stripFreeMarker(rawId) {
    var stripped = rawId.replace(FREE_SUFFIX, "");
    stripped = stripped.replace(FREE_PREFIX, "");
    return stripped || rawId;
  }

  //: ONLY maximal ASCII `[0-9]+` runs are digit runs. The capture group keeps
  //: every intervening text piece -- including the empty boundary pieces -- so
  //: the token sequence preserves the string's exact boundaries.
  var ASCII_DIGIT_RUN = /([0-9]+)/;

  /**
   * The natural raw-ID sort key: the Python `natural_key` tuple as an array of
   * tagged triples.
   *
   * A digit run becomes `["d", originalLength, run]` (digit count, then lexical
   * ASCII digits: no int/float parse of long runs, no leading-zero stripping)
   * and every other piece becomes `["t", 0, piece]`, compared case-sensitively
   * by Unicode code point with the shorter sequence first on a strict prefix.
   * Non-ASCII numeric-looking characters (`١`, `²`) are TEXT, never digit runs,
   * and the raw id is never rewritten or trimmed. Keys are fresh data.
   */
  function naturalKey(rawId) {
    var pieces = rawId.split(ASCII_DIGIT_RUN);
    var key = [];
    for (var index = 0; index < pieces.length; index += 1) {
      var piece = pieces[index];
      if (index % 2) {
        key.push(["d", piece.length, piece]);
      } else {
        key.push(["t", 0, piece]);
      }
    }
    return key;
  }

  /** Lexicographic triplet comparison of two already-built natural keys. */
  function _compareNaturalKeys(leftKey, rightKey) {
    var shared = Math.min(leftKey.length, rightKey.length);
    for (var index = 0; index < shared; index += 1) {
      var leftToken = leftKey[index];
      var rightToken = rightKey[index];
      if (leftToken[0] !== rightToken[0]) {
        // `d` < `t`: a digit-run token sorts before a text token.
        return leftToken[0] < rightToken[0] ? -1 : 1;
      }
      if (leftToken[1] !== rightToken[1]) {
        return leftToken[1] < rightToken[1] ? -1 : 1;
      }
      var order = _compareCodePoints(leftToken[2], rightToken[2]);
      if (order !== 0) {
        return order < 0 ? -1 : 1;
      }
    }
    if (leftKey.length === rightKey.length) {
      return 0;
    }
    return leftKey.length < rightKey.length ? -1 : 1;
  }

  /**
   * Compare two raw ids under the frozen natural order: negative, zero or
   * positive, with equal ids comparing zero. Suitable for `Array.prototype
   * .sort`; no locale, no case folding and no UTF-16 code-unit ordering.
   */
  function compareNaturalIds(left, right) {
    return _compareNaturalKeys(naturalKey(left), naturalKey(right));
  }

  /**
   * The sort key for the `(provider, raw model id)` endpoint identity:
   * `[providerOrder index, naturalKey(rawId)]`.
   *
   * `providerOrder` is the caller's already-validated ordered list of
   * registered provider keys, and precedence is that list's INDEX -- never
   * alphabetic key order, provider-map insertion order or global raw-ID order.
   * A raw id shared by two providers stays TWO distinct pairs ordered by
   * provider precedence. An unregistered provider is outside the documented
   * precondition and is rejected instead of silently ranking at -1.
   */
  function endpointKey(providerOrder, provider, rawId) {
    var index = providerOrder.indexOf(provider);
    if (index === -1) {
      throw new TypeError("endpointKey() requires a registered provider");
    }
    return [index, naturalKey(rawId)];
  }

  /**
   * Compare two `[provider, rawId]` pairs by supplied provider precedence and
   * then by natural raw-ID order; identical pairs compare zero.
   */
  function compareEndpointPairs(providerOrder, leftPair, rightPair) {
    var left = endpointKey(providerOrder, leftPair[0], leftPair[1]);
    var right = endpointKey(providerOrder, rightPair[0], rightPair[1]);
    if (left[0] !== right[0]) {
      return left[0] < right[0] ? -1 : 1;
    }
    return _compareNaturalKeys(left[1], right[1]);
  }

  // ---------- pure bulk list/endpoint queries ---------------------------
  //
  // The dependency-free counterpart of the pure `list_free_models` and
  // `list_endpoints` cores in `watchdog_catalog/query.py`. Both take an
  // already-VALIDATED public snapshot -- the load/validate boundary owns raw,
  // structural and hash checks, so nothing here re-validates, rehashes, reads a
  // clock or performs I/O -- plus the whole DECODED argument object (never a
  // splatted provider and never an adapter wrapper). Both return the frozen
  // payload directly and never raise into the caller for a malformed argument.

  //: The two frozen argument-error codes and their literal public messages.
  var INVALID_ARGUMENTS = "invalid_arguments";
  var UNKNOWN_PROVIDER = "unknown_provider";
  var ARGUMENT_MESSAGES = {
    invalid_arguments: "Invalid tool arguments.",
    unknown_provider: "Unknown provider."
  };

  /**
   * The shared frozen argument-error envelope.
   *
   * Top-level fields are exactly `ok` and `error`; `ok` is strictly `false`;
   * `error` carries exactly `code`, `message` and `parameter`. `parameter` is
   * the offending KNOWN signature parameter name, or `null` for a non-object
   * argument or an unknown argument name, and no supplied key or hostile value
   * is ever reflected. The envelope carries no `tool`, path, source, cache,
   * provenance, staleness, partial-data or count field. A NEW object is
   * returned on every call, so a caller can never mutate shared state.
   */
  function _argumentError(code, parameter) {
    return {ok: false,
            error: {code: code, message: ARGUMENT_MESSAGES[code],
                    parameter: parameter}};
  }

  /**
   * The frozen provider-argument boundary shared by the two bulk queries.
   *
   * Deterministic and in the frozen order: the argument must be a JSON object;
   * then ANY unknown key rejects with `parameter` `null`; then a non-`null`
   * provider that is not a string is `invalid_arguments`; then a string absent
   * from this snapshot's exact `provider_order` is `unknown_provider`. Empty,
   * padded and wrong-case keys are never repaired, and membership is driven by
   * the snapshot's ordered data alone -- never a provider enum, truthiness or
   * an object prototype member. `undefined` is this language's absent value and
   * is treated exactly like an omitted or `null` provider.
   */
  function _providerSelection(snapshot, args) {
    if (!_isPlainObject(args)) {
      return {error: _argumentError(INVALID_ARGUMENTS, null)};
    }
    var keys = Object.keys(args);
    for (var index = 0; index < keys.length; index += 1) {
      // `limit`/`offset` are unknown here, not bulk truncation controls.
      if (keys[index] !== "provider") {
        return {error: _argumentError(INVALID_ARGUMENTS, null)};
      }
    }
    var supplied = _member(args, "provider");
    var provider = supplied === _MISSING || supplied === null ||
                   supplied === undefined ? null : supplied;
    if (provider !== null && typeof provider !== "string") {
      return {error: _argumentError(INVALID_ARGUMENTS, "provider")};
    }
    if (provider !== null && snapshot.provider_order.indexOf(provider) === -1) {
      return {error: _argumentError(UNKNOWN_PROVIDER, "provider")};
    }
    return {error: null, provider: provider};
  }

  /** A fresh naturally ordered copy of one provider's raw-ID array. */
  function _naturalIds(modelIds) {
    return modelIds.slice().sort(compareNaturalIds);
  }

  /**
   * A fresh deep copy of one JSON-shaped value.
   *
   * Strings, booleans, `null` and numbers are immutable and are returned as-is;
   * an accepted typed `JsonFloat` gets its own wrapper so a caller mutating a
   * result can never reach the snapshot's typed representation. Arrays and
   * plain objects are copied member by member through `_defineMember`, so an
   * own `__proto__` member survives as data instead of becoming a prototype.
   */
  function _copyValue(value) {
    if (value === null || typeof value !== "object") {
      return value;
    }
    if (value instanceof JsonFloat) {
      return new JsonFloat(value.value);
    }
    if (Array.isArray(value)) {
      var items = [];
      for (var index = 0; index < value.length; index += 1) {
        items.push(_copyValue(value[index]));
      }
      return items;
    }
    var copy = {};
    var keys = Object.keys(value);
    for (var position = 0; position < keys.length; position += 1) {
      _defineMember(copy, keys[position], _copyValue(value[keys[position]]));
    }
    return copy;
  }

  /**
   * The flat `rawId -> projected public model` map for ONE provider.
   *
   * Members follow the provider's own `model_ids` order, exactly like the
   * reference comprehension, and each value (with its nested architecture,
   * parameter and pricing containers) is deep-copied verbatim -- never
   * reprojected, re-derived from raw provider state or stringified/parsed away.
   * Model-map key order is not semantic; the raw-ID array order is.
   */
  function _modelMap(entry) {
    var models = {};
    var ids = entry.model_ids;
    for (var index = 0; index < ids.length; index += 1) {
      _defineMember(models, ids[index], _copyValue(entry.models[ids[index]]));
    }
    return models;
  }

  /** A fresh `{provider: staleFlag}` map over the selected providers. */
  function _staleFlags(providers, keys) {
    var flags = {};
    for (var index = 0; index < keys.length; index += 1) {
      _defineMember(flags, keys[index], providers[keys[index]].stale);
    }
    return flags;
  }

  /**
   * The pure core of the frozen BULK `list_free_models` tool.
   *
   * With `provider` omitted or `null` the all-provider export carries exactly
   * `ok`, `providers`, `models`, `counts`, `total_ids`, `n_gateways`,
   * `snapshot_id`, `tick_epoch` and `provider_staleness`; with one exact
   * registered `provider` the one-provider export carries exactly `ok`,
   * `provider`, `model_ids`, `models`, `count`, `snapshot_id`, `tick_epoch` and
   * `provider_staleness`.
   *
   * `providers`/`models`/`counts`/`provider_staleness` carry the SAME provider
   * set -- every `provider_order` key, a registered zero-ID provider included
   * (as an empty array, an empty map and count `0`). `total_ids` is the count
   * of distinct LITERAL raw-ID strings across the selected registry, never the
   * endpoint-pair count and never a marker-label count, and `n_gateways` is
   * `provider_order`'s length including zero-ID providers. Metadata stays
   * attached to its own `(provider, rawId)` pair, so two providers sharing an
   * ID keep their own conflicting projected objects.
   *
   * Every returned container, array and copied model object is freshly
   * allocated, the inputs are never mutated, and raw-ID arrays are in the
   * accepted natural order -- never the legacy sort and never the snapshot's
   * literal code-point order.
   */
  function listFreeModels(snapshot, args) {
    var selection = _providerSelection(snapshot, args);
    if (selection.error) {
      return selection.error;
    }
    var provider = selection.provider;
    var order = snapshot.provider_order;
    var providers = snapshot.providers;
    if (provider !== null) {
      var entry = providers[provider];
      var modelIds = _naturalIds(entry.model_ids);
      return {
        ok: true,
        provider: provider,
        model_ids: modelIds,
        models: _modelMap(entry),
        count: modelIds.length,
        snapshot_id: snapshot.snapshot_id,
        tick_epoch: snapshot.tick_epoch,
        provider_staleness: _staleFlags(providers, [provider])
      };
    }
    var providerMap = {};
    var models = {};
    var counts = {};
    var totalIds = new Set();
    for (var index = 0; index < order.length; index += 1) {
      var key = order[index];
      var current = providers[key];
      _defineMember(providerMap, key, _naturalIds(current.model_ids));
      _defineMember(models, key, _modelMap(current));
      _defineMember(counts, key, current.model_ids.length);
      for (var position = 0; position < current.model_ids.length;
           position += 1) {
        totalIds.add(current.model_ids[position]);
      }
    }
    return {
      ok: true,
      providers: providerMap,
      models: models,
      counts: counts,
      total_ids: totalIds.size,
      n_gateways: order.length,
      snapshot_id: snapshot.snapshot_id,
      tick_epoch: snapshot.tick_epoch,
      provider_staleness: _staleFlags(providers, order)
    };
  }

  /**
   * The pure core of the frozen `list_endpoints` tool.
   *
   * BOTH variants carry exactly `ok`, `gateways`, `counts`, `provider`,
   * `snapshot_id`, `tick_epoch` and `provider_staleness`; `provider` is `null`
   * for the all-provider form and the exact selected key otherwise.
   *
   * `gateways` and `provider_staleness` contain exactly the SELECTED providers
   * (all `provider_order` keys, or the one known selection, a registered
   * zero-ID provider included). Each gateway entry carries exactly
   * `chat_completions_url`, `api_type`, `model_ids` and `models`: wiring from
   * that same snapshot provider, its complete naturally ordered raw-ID array
   * (unchanged literal ids, never marker labels) and the complete projected
   * metadata for each of its own endpoints.
   *
   * `counts` carries exactly `gateways` (selected providers), `endpoints`
   * (selected `(provider, rawId)` pairs), `distinct_raw_ids` (selected literal
   * raw strings), `marker_group_labels` (selected delimiter-stripped labels via
   * the accepted `stripFreeMarker` guard) and the documented legacy `models`
   * alias equal to `marker_group_labels` -- a label count, never a verified
   * model identity and never the endpoint count. Counts describe the SELECTED
   * scope: a filtered response leaks no registry-wide total, and a zero-ID
   * single selection yields `gateways` 1 with every other count zero.
   */
  function listEndpoints(snapshot, args) {
    var selection = _providerSelection(snapshot, args);
    if (selection.error) {
      return selection.error;
    }
    var provider = selection.provider;
    var order = snapshot.provider_order;
    var providers = snapshot.providers;
    var selected = provider === null ? order.slice() : [provider];
    var gateways = {};
    var endpoints = 0;
    var distinctRawIds = new Set();
    var markerGroupLabels = new Set();
    for (var index = 0; index < selected.length; index += 1) {
      var key = selected[index];
      var entry = providers[key];
      _defineMember(gateways, key, {
        chat_completions_url: entry.chat_completions_url,
        api_type: entry.api_type,
        model_ids: _naturalIds(entry.model_ids),
        models: _modelMap(entry)
      });
      for (var position = 0; position < entry.model_ids.length;
           position += 1) {
        var rawId = entry.model_ids[position];
        endpoints += 1;
        distinctRawIds.add(rawId);
        markerGroupLabels.add(stripFreeMarker(rawId));
      }
    }
    var labels = markerGroupLabels.size;
    return {
      ok: true,
      gateways: gateways,
      counts: {
        gateways: selected.length,
        endpoints: endpoints,
        distinct_raw_ids: distinctRawIds.size,
        marker_group_labels: labels,
        models: labels
      },
      provider: provider,
      snapshot_id: snapshot.snapshot_id,
      tick_epoch: snapshot.tick_epoch,
      provider_staleness: _staleFlags(providers, selected)
    };
  }

  // ---------- pure exact/marker lookup query ------------------------------
  //
  // The dependency-free counterpart of the pure `get_model` core in
  // `watchdog_catalog/query.py`. Like the bulk queries it takes an
  // already-VALIDATED public snapshot (nothing here re-validates, rehashes,
  // reads a clock or performs I/O) plus the whole DECODED argument object, and
  // returns the frozen payload directly -- never raising into the caller for a
  // malformed argument.

  //: The explicit ASCII blank set the required `model_id` predicate uses:
  //: U+0009..U+000D (tab, LF, VT, FF, CR) and U+0020 (space). It is NEVER
  //: widened with `String.prototype.trim` or a `\s` class: MDN defines those
  //: over WhiteSpace plus LineTerminator, so they also strip NBSP (U+00A0),
  //: EM SPACE (U+2003), U+2028/U+2029, U+3000 and a BOM -- every one of which
  //: is a literal, NONBLANK query here.
  var _ASCII_BLANKS = " \t\n\r\u000b\f";

  //: The frozen absence caveat carried on EVERY `getModel` success payload --
  //: including a no-match success. It is a labelling caution, never a proof of
  //: cross-gateway model identity.
  var ABSENCE_CAVEAT = "Cross-gateway ABSENCE is unreliable: gateways rename " +
    "inconsistently, so a missing exact id does NOT prove the model is " +
    "unavailable there.";

  /** True iff every code unit belongs to the explicit ASCII blank set. */
  function _isAsciiBlankOnly(text) {
    for (var index = 0; index < text.length; index += 1) {
      if (_ASCII_BLANKS.indexOf(text.charAt(index)) === -1) {
        return false;
      }
    }
    return true;
  }

  /**
   * The pure core of the frozen `get_model` tool.
   *
   * The success payload carries exactly `ok`, `query`, `snapshot_id`,
   * `tick_epoch`, `exact_matches`, `exact_gateways`, `provider_staleness`,
   * `endpoints`, `caveats` and `counts`; `ok` is exactly `true` even when the
   * lookup finds nothing, and the retained `query` is the raw argument verbatim
   * (never trimmed, case-folded or repaired). Snapshot provenance is echoed
   * literally, a `null` epoch included -- no current/fetch clock and no
   * invented freshness.
   *
   * `exact_matches` is LITERAL raw-ID membership -- case-sensitive, unpadded,
   * exact -- one entry per `provider_order` key including zero-ID providers;
   * `exact_gateways` lists the true providers in that declared display order.
   * `endpoints` is the SEPARATE marker-insensitive lookup: every tracked
   * `(provider, rawId)` pair whose accepted `stripFreeMarker` label equals the
   * stripped query, ordered by provider display order then the accepted natural
   * raw-ID order, with raw strings unchanged and the complete projected
   * metadata of its OWN pair attached. A marker match is never proof of
   * cross-gateway identity, so it never feeds `exact_matches`.
   *
   * `provider_staleness` covers EVERY snapshot provider with its carried-
   * forward snapshot stale flag (provider staleness, never tick age). `counts`
   * describes only the returned lookup -- matching gateways, endpoint pairs,
   * distinct literal raw ids and distinct marker labels -- and `caveats`
   * carries the frozen absence caveat on every success. Argument validation is
   * the frozen order: object, then ANY unknown key (parameter `null`), then the
   * required string `model_id` with at least one character outside the explicit
   * ASCII blank set. Every returned container, endpoint and metadata object
   * (nested architecture/parameter/pricing containers and typed `JsonFloat`
   * wrappers included) is freshly allocated; the inputs are never mutated.
   */
  function getModel(snapshot, args) {
    if (!_isPlainObject(args)) {
      return _argumentError(INVALID_ARGUMENTS, null);
    }
    var keys = Object.keys(args);
    for (var key = 0; key < keys.length; key += 1) {
      // `provider`/`limit`/`offset`/`query` are unknown here, not aliases.
      if (keys[key] !== "model_id") {
        return _argumentError(INVALID_ARGUMENTS, null);
      }
    }
    var supplied = _member(args, "model_id");
    if (supplied === _MISSING || typeof supplied !== "string" ||
        _isAsciiBlankOnly(supplied)) {
      return _argumentError(INVALID_ARGUMENTS, "model_id");
    }
    var query = supplied;
    var order = snapshot.provider_order;
    var providers = snapshot.providers;
    var markerQuery = stripFreeMarker(query);
    var exactMatches = {};
    var exactGateways = [];
    var endpoints = [];
    var distinctRawIds = new Set();
    var markerLabels = new Set();
    for (var index = 0; index < order.length; index += 1) {
      var provider = order[index];
      var entry = providers[provider];
      var matched = entry.model_ids.indexOf(query) !== -1;
      _defineMember(exactMatches, provider, matched);
      if (matched) {
        exactGateways.push(provider);
      }
      var ids = _naturalIds(entry.model_ids);
      for (var position = 0; position < ids.length; position += 1) {
        var rawId = ids[position];
        var label = stripFreeMarker(rawId);
        if (label !== markerQuery) {
          continue;
        }
        endpoints.push({
          provider: provider,
          model_id: rawId,
          chat_completions_url: entry.chat_completions_url,
          api_type: entry.api_type,
          stale: entry.stale,
          metadata: _copyValue(entry.models[rawId])
        });
        distinctRawIds.add(rawId);
        markerLabels.add(label);
      }
    }
    return {
      ok: true,
      query: query,
      snapshot_id: snapshot.snapshot_id,
      tick_epoch: snapshot.tick_epoch,
      exact_matches: exactMatches,
      exact_gateways: exactGateways,
      provider_staleness: _staleFlags(providers, order),
      endpoints: endpoints,
      caveats: [ABSENCE_CAVEAT],
      counts: {
        exact_matches: exactGateways.length,
        endpoints: endpoints.length,
        distinct_raw_ids: distinctRawIds.size,
        marker_group_labels: markerLabels.size
      }
    };
  }

  // ---------- pure whole-catalogue status: fixed-clock freshness -----------
  //
  // The dependency-free counterpart of the pure `watchdog_status` core in
  // `watchdog_catalog/query.py`. `snapshot` is an already-VALIDATED public
  // snapshot, so nothing here re-validates, rehashes, reads a file, the
  // network, a provider or a clock. `now` is the REQUIRED separately supplied
  // fixed clock (a strict non-negative safe-integer epoch in seconds): a
  // programming precondition mirroring the snapshot precondition, never a
  // public argument, so this core has no default, no fallback and no ambient
  // time source. `args` is the COMPLETE decoded JSON argument value.

  //: The frozen status warning order: the snapshot THREE codes followed by the
  //: two derived status codes. `tick_epoch_future` and `tick_stale` are
  //: mutually exclusive, so at most four codes appear in one payload.
  var STATUS_WARNING_ORDER = [
    "tick_epoch_unknown", "metadata_missing", "metadata_invalid",
    "tick_epoch_future", "tick_stale"
  ];

  /**
   * The pure core of the frozen `watchdog_status` tool.
   *
   * `watchdog_status` has NO public arguments: only the empty decoded object
   * succeeds, and ANY own key (including `now`, `provider` and `limit`) is an
   * unknown argument rejecting with the shared non-reflecting
   * `invalid_arguments` envelope and `parameter` `null`. Validation runs before
   * any clock use, so a hostile value under an unknown key is never coerced and
   * never reflected.
   *
   * The payload carries exactly `ok`, `snapshot_id`, `tick_epoch`, `cadence_s`,
   * `last_tick_age_s`, `tick_fresh`, `provider_staleness`, `provider_counts`,
   * `metadata_coverage`, `counts`, `pending_alerts`, `site_published`,
   * `site_publish_age_s` and `warnings`. `ok` is exactly `true` -- the valid
   * snapshot was queried, never that its observations are fresh or its
   * providers healthy. `snapshot_id`, `tick_epoch` (a future epoch included)
   * and `cadence_s` are echoed literally: no clock-derived repair, no
   * restamping and no hardcoded cadence.
   *
   * `last_tick_age_s` is `now - tick_epoch` for a known non-future tick and
   * strictly `null` for a null or future tick -- never clamped to a healthy
   * zero. `tick_fresh` is the inclusive one-cadence boundary: `true` exactly
   * when the age is known and `<= cadence_s`, so age `0` and age `=== cadence_s`
   * are both fresh and one second more is stale. Both are strict scalars.
   *
   * `provider_staleness` is a strict-bool map for EVERY `provider_order` key
   * (zero-ID providers included) carrying each gateway's stale flag unchanged
   * by the clock, and `provider_counts` is the matching non-negative integer
   * map of each gateway's `model_ids` length, zero included, in display order.
   * The two are independent signals: staleness may be `true` during a fresh
   * tick and every provider may be `false` during an old one.
   *
   * `counts` is the WHOLE-CATALOGUE cardinality object -- every configured
   * provider, every tracked `(provider, raw id)` pair, the literal union of raw
   * ids and the distinct `stripFreeMarker` labels -- never a page or provider
   * filtered count. `metadata_coverage` reports coverage over that same global
   * tracked-pair denominator by NON-NULL presence: a known `0`, a known empty
   * `[]` and a known `""` description count, a `null` does not. The metadata
   * itself is never returned, so no registry deep copy is needed.
   *
   * `pending_alerts`, `site_published` and `site_publish_age_s` are each
   * STRICTLY `null`: private operator state is not observable through this
   * payload. `warnings` unions the snapshot warnings (never removed, even when
   * redundant) with the derived `tick_epoch_unknown` (null epoch),
   * `tick_epoch_future` (future epoch) and `tick_stale` (known age strictly
   * greater than `cadence_s`), deduplicated in `STATUS_WARNING_ORDER`; a fresh
   * known epoch adds none and a null or future epoch is never `tick_stale`.
   *
   * Every returned map and the warnings array is freshly allocated and the
   * snapshot and arguments are never mutated, so a caller mutating a result can
   * never corrupt the snapshot or another call.
   */
  function watchdogStatus(snapshot, args, now) {
    if (!_isPlainObject(args)) {
      return _argumentError(INVALID_ARGUMENTS, null);
    }
    // No public arguments: any own key is unknown and rejects before the clock.
    if (Object.keys(args).length !== 0) {
      return _argumentError(INVALID_ARGUMENTS, null);
    }

    var order = snapshot.provider_order;
    var providers = snapshot.providers;
    var cadence = snapshot.cadence_s;
    var tick = snapshot.tick_epoch;

    var lastTickAgeS;
    var tickFresh;
    if (tick === null || tick > now) {
      lastTickAgeS = null;
      tickFresh = false;
    } else {
      lastTickAgeS = now - tick;
      tickFresh = lastTickAgeS <= cadence;
    }

    var endpoints = 0;
    var distinctRawIds = new Set();
    var markerLabels = new Set();
    var coverage = {
      endpoints: 0,
      context_length_known: 0,
      input_modalities_known: 0,
      output_modalities_known: 0,
      supported_parameters_known: 0,
      description_known: 0
    };
    for (var index = 0; index < order.length; index += 1) {
      var entry = providers[order[index]];
      var ids = entry.model_ids;
      for (var position = 0; position < ids.length; position += 1) {
        var rawId = ids[position];
        endpoints += 1;
        distinctRawIds.add(rawId);
        markerLabels.add(stripFreeMarker(rawId));
        var model = entry.models[rawId];
        // NON-NULL presence, never truthiness: 0, [] and "" are known.
        if (model.context_length !== null) {
          coverage.context_length_known += 1;
        }
        var architecture = model.architecture;
        if (architecture.input_modalities !== null) {
          coverage.input_modalities_known += 1;
        }
        if (architecture.output_modalities !== null) {
          coverage.output_modalities_known += 1;
        }
        if (model.supported_parameters !== null) {
          coverage.supported_parameters_known += 1;
        }
        if (model.description !== null) {
          coverage.description_known += 1;
        }
      }
    }
    coverage.endpoints = endpoints;

    var providerCounts = {};
    for (var key = 0; key < order.length; key += 1) {
      _defineMember(providerCounts, order[key],
                    providers[order[key]].model_ids.length);
    }

    // A fresh set: the snapshot warnings array is never mutated.
    var present = new Set(snapshot.warnings);
    if (tick === null) {
      present.add("tick_epoch_unknown");
    } else if (tick > now) {
      present.add("tick_epoch_future");
    } else if (now - tick > cadence) {
      present.add("tick_stale");
    }
    var warnings = [];
    for (var code = 0; code < STATUS_WARNING_ORDER.length; code += 1) {
      if (present.has(STATUS_WARNING_ORDER[code])) {
        warnings.push(STATUS_WARNING_ORDER[code]);
      }
    }

    return {
      ok: true,
      snapshot_id: snapshot.snapshot_id,
      tick_epoch: tick,
      cadence_s: cadence,
      last_tick_age_s: lastTickAgeS,
      tick_fresh: tickFresh,
      provider_staleness: _staleFlags(providers, order),
      provider_counts: providerCounts,
      metadata_coverage: coverage,
      counts: {
        gateways: order.length,
        endpoints: endpoints,
        distinct_raw_ids: distinctRawIds.size,
        marker_group_labels: markerLabels.size
      },
      pending_alerts: null,
      site_published: null,
      site_publish_age_s: null,
      warnings: warnings
    };
  }

  // ---------- pure metadata-search pipeline stages -------------------------
  //
  // The dependency-free counterpart of the pure `_search_text_provider_pairs`,
  // `_search_min_context_pairs`, `_search_input_modality_pairs` and
  // `_search_supported_parameter_pairs` stages in `watchdog_catalog/query.py`.
  // Together they are the INTERNAL pipeline of the future public `search_models`
  // tool: candidate selection by literal text and exact provider, then an
  // INCLUSIVE minimum context, then an input-modality AND, then a
  // supported-parameter AND. Public argument decoding, success/error envelopes
  // and pagination belong to a later unit, so these stages validate nothing:
  // invalid preconditions have no newly promised error contract.
  //
  // Every stage takes an already-VALIDATED snapshot and never re-validates,
  // re-hashes, or reads a clock, file, network, configuration, state or SDK. The
  // three filter stages take an ALREADY selected ordered array of
  // `[provider, rawId]` identities -- possibly a subset, in non-natural order or
  // with repeated entries -- and never re-derive a global selection. Each returns
  // a NEW outer array of NEW two-string pair arrays, so mutating a result can
  // never corrupt the snapshot, an input sequence, a request array or an earlier
  // or later result.

  /** A fresh copy of one `[provider, rawId]` endpoint identity pair. */
  function _copyPair(pair) {
    return [pair[0], pair[1]];
  }

  /** A fresh copy of an ordered `[provider, rawId]` selection. */
  function _copyPairs(pairs) {
    var copied = [];
    for (var index = 0; index < pairs.length; index += 1) {
      copied.push(_copyPair(pairs[index]));
    }
    return copied;
  }

  /** One option member, with an omitted or `undefined` value resolved to null. */
  function _stageOption(options, key) {
    var value = _member(options, key);
    return value === _MISSING || value === undefined ? null : value;
  }

  /**
   * The OWN projected model of one supplied pair, or `_MISSING`.
   *
   * Endpoint identity is exactly `(provider, raw id)`: the lookup goes to that
   * provider's own model map, so a raw id shared by two providers always reads
   * the metadata of the provider in the pair and never a sibling's.
   */
  function _stageModel(snapshot, pair) {
    var entry = _member(snapshot.providers, pair[0]);
    if (entry === _MISSING) {
      return _MISSING;
    }
    return _member(entry.models, pair[1]);
  }

  /**
   * True when `text` holds a UTF-16 surrogate code unit -- i.e. when a
   * code-unit search and a code-point search can disagree, because every
   * astral character is a surrogate PAIR and an unpaired surrogate is itself
   * one code unit.
   */
  function _hasSurrogateCodeUnit(text) {
    for (var index = 0; index < text.length; index += 1) {
      var code = text.charCodeAt(index);
      if (code >= 0xd800 && code <= 0xdfff) {
        return true;
      }
    }
    return false;
  }

  /**
   * `text` split into one string per Unicode CODE POINT, in order.
   *
   * A valid surrogate pair becomes ONE element and an unpaired surrogate stays
   * one element, which is exactly Python's `str` model. `codePointAt` is already
   * used by `_compareCodePoints()`, so this adds no new platform prerequisite.
   */
  function _codePointParts(text) {
    var parts = [];
    var index = 0;
    while (index < text.length) {
      var width = text.codePointAt(index) > 0xffff ? 2 : 1;
      parts.push(text.slice(index, index + width));
      index += width;
    }
    return parts;
  }

  /**
   * Literal substring test with the accepted Python `in` semantics.
   *
   * The needle must match a run of whole CODE POINTS of `field`, so an isolated
   * surrogate never matches one half of a valid astral character -- the one
   * place where UTF-16 code-unit search diverges from Python. When neither side
   * contains a surrogate code unit the two rules provably agree, so the plain
   * code-unit search stays the fast path.
   */
  function _textIncludes(field, needle) {
    if (!_hasSurrogateCodeUnit(field) && !_hasSurrogateCodeUnit(needle)) {
      return field.indexOf(needle) !== -1;
    }
    var haystack = _codePointParts(field);
    var wanted = _codePointParts(needle);
    for (var start = 0; start + wanted.length <= haystack.length;
         start += 1) {
      var offset = 0;
      while (offset < wanted.length &&
             haystack[start + offset] === wanted[offset]) {
        offset += 1;
      }
      if (offset === wanted.length) {
        return true;
      }
    }
    return false;
  }

  /**
   * The pure candidate-selection stage: literal text plus exact provider.
   *
   * `snapshot` is an already-VALIDATED public snapshot. `options.query` is null
   * or a string and `options.provider` is null or one exact `provider_order`
   * key; an omitted member means null. With a provider it scopes to exactly that
   * key, else it walks every `provider_order` key in that precedence -- never
   * provider-map, model-map or `model_ids` insertion order and never alphabetic
   * key order.
   *
   * The result is a NEW list of `[provider, rawId]` pairs in the frozen endpoint
   * order (provider precedence, then the accepted natural raw-ID order within
   * each provider). It is UNPAGINATED and carries no page cap, no deduplication
   * and no metadata union, so a later sort-then-slice stage sees the complete
   * match set: a raw ID shared by two providers stays TWO distinct pairs and a
   * selected zero-ID provider yields `[]`.
   *
   * `query` is a LITERAL, UNTRIMMED substring compared separately against the
   * pair's own raw id, `name` and non-null `description` after the shared
   * ASCII-only fold (A-Z to a-z; every other code point -- including a non-ASCII
   * uppercase letter -- stays literal). The comparison is over Unicode CODE
   * POINTS, exactly like Python's `in`: a valid astral character is one code
   * point, so a query holding only one surrogate half of it is NOT a substring
   * of it, while an unpaired-surrogate query matches only a field holding that
   * same literal unpaired code unit. The query type is
   * never narrowed, rejected or rewritten to get that agreement. There is no
   * regular expression, fuzzy alias, marker stripping or JSON/metadata
   * serialization: capability arrays,
   * pricing, wiring URLs, provider keys and object key names are never searched,
   * a query spanning two separately concatenated fields never matches, and one
   * pair's fields are never borrowed from a sibling provider. Null or `""`
   * applies no text constraint; a whitespace-only query is a literal constraint.
   * Matching ONE field suffices.
   */
  function searchTextProviderPairs(snapshot, options) {
    var settings = options === undefined || options === null ? {} : options;
    var query = _stageOption(settings, "query");
    var provider = _stageOption(settings, "provider");
    var providers = snapshot.providers;
    var selected = provider === null ? snapshot.provider_order.slice()
                                     : [provider];
    var needle = query ? _asciiLower(query) : null;
    var pairs = [];
    for (var index = 0; index < selected.length; index += 1) {
      var providerKey = selected[index];
      var entry = _member(providers, providerKey);
      if (entry === _MISSING) {
        continue;
      }
      var ids = _naturalIds(entry.model_ids);
      for (var position = 0; position < ids.length; position += 1) {
        var rawId = ids[position];
        if (needle !== null) {
          var model = _member(entry.models, rawId);
          var fields = [rawId, model.name, model.description];
          var matched = false;
          for (var field = 0; field < fields.length; field += 1) {
            var value = fields[field];
            if (value !== null && value !== undefined &&
                _textIncludes(_asciiLower(value), needle)) {
              matched = true;
              break;
            }
          }
          if (!matched) {
            continue;
          }
        }
        pairs.push([providerKey, rawId]);
      }
    }
    return pairs;
  }

  /**
   * The pure INCLUSIVE minimum-context stage.
   *
   * `pairs` is the ALREADY selected ordered selection of tracked
   * `[provider, rawId]` identities (typically from
   * `searchTextProviderPairs()`), never re-derived here; `minContextLength` is
   * null or an already-normalized nonnegative SAFE integer.
   *
   * The result is a NEW list holding only the supplied pairs whose OWN
   * `context_length` is known (not null) and `>=` the threshold, inclusively. A
   * null threshold applies NO constraint and returns every supplied pair
   * freshly; `0` is an ACTIVE threshold that retains a known zero and rejects an
   * unknown null. There is no truthiness test of the threshold or the context, no
   * `or 0` fallback, no inferred name/description context, no raw-ID sibling
   * borrowing, no sort, no deduplication and no pagination: the supplied order
   * and multiplicity are preserved exactly, the selection is never expanded, and
   * more than the default page's worth of eligible pairs stays complete.
   */
  function searchMinContextPairs(snapshot, pairs, minContextLength) {
    var threshold = minContextLength === undefined ? null : minContextLength;
    if (threshold === null) {
      return _copyPairs(pairs);
    }
    var kept = [];
    for (var index = 0; index < pairs.length; index += 1) {
      var model = _stageModel(snapshot, pairs[index]);
      if (model === _MISSING) {
        continue;
      }
      var context = model.context_length;
      if (context !== null && context !== undefined && context >= threshold) {
        kept.push(_copyPair(pairs[index]));
      }
    }
    return kept;
  }

  /** The requested tokens folded by the shared ASCII-only rule, in order. */
  function _foldRequestedTokens(tokens) {
    var folded = [];
    for (var index = 0; index < tokens.length; index += 1) {
      folded.push(_asciiLower(tokens[index]));
    }
    return folded;
  }

  /** Whether one KNOWN normalized capability array contains every token. */
  function _containsEveryToken(available, requested) {
    for (var index = 0; index < requested.length; index += 1) {
      if (available.indexOf(requested[index]) === -1) {
        return false;
      }
    }
    return true;
  }

  /**
   * The shared capability AND filter behind the two public filter stages.
   *
   * A null or empty request is INACTIVE and returns every supplied pair freshly,
   * unknown (null) and known-empty (`[]`) capability arrays included. A nonempty
   * request keeps only pairs whose OWN normalized array is KNOWN and contains
   * EVERY ASCII-folded requested token -- an AND over the requested entries on
   * the SAME pair, never a multiplicity constraint, so a duplicate request is
   * idempotent. Matching is exact element equality, never a substring or
   * wildcard, and the request list is never normalized in place.
   */
  function _filterByCapability(snapshot, pairs, tokens, field) {
    if (tokens === null || tokens === undefined || tokens.length === 0) {
      return _copyPairs(pairs);
    }
    var requested = _foldRequestedTokens(tokens);
    var kept = [];
    for (var index = 0; index < pairs.length; index += 1) {
      var model = _stageModel(snapshot, pairs[index]);
      if (model === _MISSING) {
        continue;
      }
      // The accepted projection/normalization: known arrays are ASCII-folded
      // and first-occurrence deduplicated, null stays null and a known `[]` is
      // NOT unknown.
      var available = normalizeCapabilities(model)[field];
      if (available !== null && _containsEveryToken(available, requested)) {
        kept.push(_copyPair(pairs[index]));
      }
    }
    return kept;
  }

  /**
   * The pure input-modality AND stage.
   *
   * `inputModalities` is null or an already-validated list of nonempty strings.
   * The result keeps only supplied pairs whose OWN
   * `architecture.input_modalities` is known and contains EVERY ASCII-folded
   * requested token. Output modalities, names, descriptions, raw ids, provider
   * keys, pricing and supported parameters are never substitutes, and a pair's
   * capability is never borrowed from a sibling provider sharing its raw id.
   * See `_filterByCapability()` for the shared null/empty, known-vs-unknown,
   * duplicate-idempotent and order/multiplicity rules.
   */
  function searchInputModalityPairs(snapshot, pairs, inputModalities) {
    return _filterByCapability(
      snapshot, pairs,
      inputModalities === undefined ? null : inputModalities,
      "input_modalities");
  }

  /**
   * The pure supported-parameter AND stage.
   *
   * `supportedParameters` is null or an already-validated list of nonempty
   * strings. The result keeps only supplied pairs whose OWN normalized
   * `supported_parameters` is known and contains EVERY ASCII-folded requested
   * token. Input and output modalities are never substitutes, a capability name
   * is never substring-matched, and a pair can neither borrow a parameter from
   * another provider sharing its raw id nor union such providers to satisfy a
   * conjunction. See `_filterByCapability()` for the shared rules.
   */
  function searchSupportedParameterPairs(snapshot, pairs,
                                         supportedParameters) {
    return _filterByCapability(
      snapshot, pairs,
      supportedParameters === undefined ? null : supportedParameters,
      "supported_parameters");
  }

  //: The frozen public `search_models` argument signature, in validation order.
  //: Validation walks THIS array after the whole-object and unknown-key checks,
  //: so precedence is independent of the caller's member insertion order.
  var SEARCH_SIGNATURE = [
    "query", "provider", "min_context_length", "input_modalities",
    "supported_parameters", "limit", "offset"
  ];

  //: The frozen pagination defaults and inclusive maximum.
  var SEARCH_DEFAULT_LIMIT = 50;
  var SEARCH_DEFAULT_OFFSET = 0;
  var SEARCH_MAX_LIMIT = 200;

  /**
   * The public tool-argument integer normalization: a strict non-negative safe
   * integer VALUE, else null.
   *
   * A JSON integer argument is a finite numeric value with zero fractional part
   * in `0..MAX_SAFE_INT`, so the numeric `1.0` IS the integer `1` -- including a
   * typed `JsonFloat` decoded argument, whose wrapper keeps the token a float
   * for the raw `filters` echo while validating by value. A bool is rejected
   * BEFORE the number branch (`true` is never `1`); a numeric string, fraction,
   * unsafe/huge numeric and non-finite value are rejected too. `Number.isInteger`
   * alone is NOT a safe test -- MDN documents that values around 2**53 lose
   * precision and still report as integers -- so the `MAX_SAFE_INT` bound is
   * checked separately, exactly like the reference. A negative zero normalizes
   * to `0`, matching Python `int(-0.0)`.
   *
   * This is the TOOL-INPUT boundary rule only; it never relaxes the strict-int
   * snapshot/context projection, which stays `_isSafeNonNegInt`.
   */
  function _searchSafeInteger(value) {
    var number;
    if (value instanceof JsonFloat) {
      number = value.value;
    } else if (typeof value === "number") {
      number = value;
    } else {
      return null;
    }
    if (!Number.isFinite(number) || !Number.isInteger(number) ||
        number < 0 || number > MAX_SAFE_INT) {
      return null;
    }
    return number === 0 ? 0 : number;
  }

  /**
   * The public capability-array rule: an ARRAY whose EVERY member is a nonempty
   * string. `[]` is accepted; a bare string, object or any non-string or
   * empty-string member rejects the WHOLE parameter -- never a partial salvage.
   * Casing, padding and duplicates stay literal.
   */
  function _searchNonemptyStringList(value) {
    if (!Array.isArray(value)) {
      return false;
    }
    for (var index = 0; index < value.length; index += 1) {
      if (typeof value[index] !== "string" || value[index].length === 0) {
        return false;
      }
    }
    return true;
  }

  /**
   * The pure core of the frozen public `search_models` tool.
   *
   * `snapshot` is an already-VALIDATED public snapshot: the `loadSnapshot`,
   * source and adapter boundary owns raw, structural and hash validation, so
   * this function performs NO validation, NO hash recompute, NO I/O and NO
   * clock/environment/config/SDK read. `args` is the ENTIRE DECODED argument
   * value, passed explicitly -- never splatted -- and the same implementation
   * runs in Node and in the browser.
   *
   * Argument validation is deterministic and in the frozen order: `args` must
   * be a JSON object; then ANY unknown OWN key rejects with parameter null
   * (exact own-key membership, so a literal `__proto__`, `constructor` or
   * `toString` member is unknown rather than inherited); then the frozen
   * signature order -- `query`, `provider`, `min_context_length`,
   * `input_modalities`, `supported_parameters`, `limit`, `offset` -- stopping
   * at the FIRST error regardless of the object's key insertion order.
   * `query` is omitted/null or ANY string (kept literally, never trimmed);
   * `provider` is omitted/null or one exact `provider_order` key (a non-string
   * is invalid_arguments, an absent string -- empty, padded or wrong-case --
   * is unknown_provider, and a registered zero-ID provider is valid);
   * `min_context_length` is omitted/null or a safe non-negative integer value;
   * the two capability arrays are omitted/null or arrays of nonempty strings
   * (invalid members reject the WHOLE parameter, never a partial salvage);
   * `limit` is omitted (50) or an integer 1..200 (explicit null is invalid);
   * `offset` is omitted (0) or a safe integer (explicit null is invalid). A
   * numeric `1.0` -- including a typed `JsonFloat` decoded argument -- is
   * accepted as the integer VALUE while a bool, numeric string, fraction,
   * unsafe integer or non-finite number is rejected with its own parameter
   * name; the RAW supplied value is still echoed in `filters`.
   *
   * Only after EVERY argument validates does it compose the accepted stages in
   * order -- `searchTextProviderPairs`, `searchMinContextPairs`,
   * `searchInputModalityPairs`, `searchSupportedParameterPairs` -- on the SAME
   * `(provider, rawId)` pairs, then count `total_matches`, then take ONE global
   * `offset:offset + limit` slice of the already ordered complete match set.
   * Pagination is never an earlier filter, never a per-provider operation and
   * never capped by a stage; `slice` clamps a start at or beyond the end to an
   * empty page, exactly like Python slicing.
   *
   * The success payload carries exactly `ok`, `query`, `filters`, `limit`,
   * `offset`, `snapshot_id`, `tick_epoch`, `provider_staleness`, `endpoints`,
   * `total_matches`, `returned_count`, `next_offset`, `counts` and `caveats`.
   * `query` and every `filters` member echo the RAW supplied value (arrays
   * freshly copied, order/case/duplicates preserved, `[]` distinct from null)
   * while `limit`/`offset` echo the effective strict integer.
   * `provider_staleness` covers every `provider_order` key when `provider` is
   * omitted, else exactly the selected key -- retained on empty, exhausted and
   * beyond-end pages. `endpoints` is the returned page with the exact fourteen
   * projected fields: pair-specific wiring and display values, the raw id
   * unchanged, the five normalized capability fields from
   * `normalizeCapabilities` and `metadata` as a deep copy of that pair's
   * complete projected public model (original casing/order/duplicates, pricing
   * types and typed `JsonFloat` wrappers included). `next_offset` is
   * `offset + returned_count` only when that is LESS than `total_matches`, else
   * null. `counts` is PAGE scoped -- exactly `gateways`, `endpoints`,
   * `distinct_raw_ids` and `marker_group_labels` -- and all-zero on an empty
   * page even when matches exist; marker labels are the frozen
   * `stripFreeMarker` labels, never verified model identities. `caveats`
   * freshly carries the accepted absence caveat.
   *
   * Every returned container, array, row, filter echo, caveat and metadata
   * object (with its nested architecture/parameter/pricing containers) is
   * freshly allocated, so a caller mutating a result can never corrupt the
   * snapshot, the argument object or another call. The inputs are never
   * mutated.
   */
  function searchModels(snapshot, args) {
    if (!_isPlainObject(args)) {
      return _argumentError(INVALID_ARGUMENTS, null);
    }
    var keys = Object.keys(args);
    for (var key = 0; key < keys.length; key += 1) {
      // `SEARCH_SIGNATURE.indexOf` is exact own-name membership: `in` or a
      // prototype lookup would accept `toString`/`constructor` as known.
      if (SEARCH_SIGNATURE.indexOf(keys[key]) === -1) {
        return _argumentError(INVALID_ARGUMENTS, null);
      }
    }

    var suppliedQuery = _member(args, "query");
    var query = suppliedQuery === _MISSING || suppliedQuery === null ||
                suppliedQuery === undefined ? null : suppliedQuery;
    if (query !== null && typeof query !== "string") {
      return _argumentError(INVALID_ARGUMENTS, "query");
    }

    var suppliedProvider = _member(args, "provider");
    var provider = suppliedProvider === _MISSING || suppliedProvider === null ||
                   suppliedProvider === undefined ? null : suppliedProvider;
    if (provider !== null && typeof provider !== "string") {
      return _argumentError(INVALID_ARGUMENTS, "provider");
    }
    var order = snapshot.provider_order;
    if (provider !== null && order.indexOf(provider) === -1) {
      return _argumentError(UNKNOWN_PROVIDER, "provider");
    }

    var suppliedMinContext = _member(args, "min_context_length");
    var rawMinContext = suppliedMinContext === _MISSING ||
                        suppliedMinContext === undefined
      ? null : suppliedMinContext;
    var minContextLength = null;
    if (rawMinContext !== null) {
      minContextLength = _searchSafeInteger(rawMinContext);
      if (minContextLength === null) {
        return _argumentError(INVALID_ARGUMENTS, "min_context_length");
      }
    }

    var requested = {};
    var capabilityNames = ["input_modalities", "supported_parameters"];
    for (var name = 0; name < capabilityNames.length; name += 1) {
      var suppliedArray = _member(args, capabilityNames[name]);
      var requestedArray = suppliedArray === _MISSING ||
                           suppliedArray === undefined ? null : suppliedArray;
      if (requestedArray !== null &&
          !_searchNonemptyStringList(requestedArray)) {
        return _argumentError(INVALID_ARGUMENTS, capabilityNames[name]);
      }
      requested[capabilityNames[name]] = requestedArray;
    }

    var limit = SEARCH_DEFAULT_LIMIT;
    if (_member(args, "limit") !== _MISSING) {
      limit = _searchSafeInteger(_member(args, "limit"));
      if (limit === null || limit < 1 || limit > SEARCH_MAX_LIMIT) {
        return _argumentError(INVALID_ARGUMENTS, "limit");
      }
    }

    var offset = SEARCH_DEFAULT_OFFSET;
    if (_member(args, "offset") !== _MISSING) {
      offset = _searchSafeInteger(_member(args, "offset"));
      if (offset === null) {
        return _argumentError(INVALID_ARGUMENTS, "offset");
      }
    }

    var matches = searchTextProviderPairs(
      snapshot, {query: query, provider: provider});
    matches = searchMinContextPairs(snapshot, matches, minContextLength);
    matches = searchInputModalityPairs(
      snapshot, matches, requested.input_modalities);
    matches = searchSupportedParameterPairs(
      snapshot, matches, requested.supported_parameters);

    var totalMatches = matches.length;
    var page = matches.slice(offset, offset + limit);

    var providers = snapshot.providers;
    var endpoints = [];
    for (var index = 0; index < page.length; index += 1) {
      var providerKey = page[index][0];
      var rawId = page[index][1];
      var entry = _member(providers, providerKey);
      var model = _member(entry.models, rawId);
      var normalized = normalizeCapabilities(model);
      endpoints.push({
        provider: providerKey,
        model_id: rawId,
        chat_completions_url: entry.chat_completions_url,
        api_type: entry.api_type,
        stale: entry.stale,
        name: model.name,
        description: model.description,
        context_length: normalized.context_length,
        input_modalities: normalized.input_modalities,
        output_modalities: normalized.output_modalities,
        supported_parameters: normalized.supported_parameters,
        unknown_fields: normalized.unknown_fields,
        metadata: _copyValue(model)
      });
    }

    var returnedCount = endpoints.length;
    var nextOffset = offset + returnedCount;
    if (nextOffset >= totalMatches) {
      nextOffset = null;
    }

    var selected = provider === null ? order.slice() : [provider];
    var gateways = new Set();
    var distinctRawIds = new Set();
    var markerLabels = new Set();
    for (var row = 0; row < endpoints.length; row += 1) {
      gateways.add(endpoints[row].provider);
      distinctRawIds.add(endpoints[row].model_id);
      markerLabels.add(stripFreeMarker(endpoints[row].model_id));
    }

    return {
      ok: true,
      query: query,
      filters: {
        provider: provider,
        min_context_length: _copyValue(rawMinContext),
        input_modalities: requested.input_modalities === null
          ? null : requested.input_modalities.slice(),
        supported_parameters: requested.supported_parameters === null
          ? null : requested.supported_parameters.slice()
      },
      limit: limit,
      offset: offset,
      snapshot_id: snapshot.snapshot_id,
      tick_epoch: snapshot.tick_epoch,
      provider_staleness: _staleFlags(providers, selected),
      endpoints: endpoints,
      total_matches: totalMatches,
      returned_count: returnedCount,
      next_offset: nextOffset,
      counts: {
        gateways: gateways.size,
        endpoints: returnedCount,
        distinct_raw_ids: distinctRawIds.size,
        marker_group_labels: markerLabels.size
      },
      caveats: [ABSENCE_CAVEAT]
    };
  }

  return {
    MAX_SAFE_INT: MAX_SAFE_INT,
    MAX_JSON_DEPTH: MAX_JSON_DEPTH,
    SCHEMA_VERSION: SCHEMA_VERSION,
    CatalogError: CatalogError,
    JsonFloat: JsonFloat,
    loadJson: loadJson,
    decodeTypedJson: decodeTypedJson,
    canonicalText: canonicalText,
    canonicalBytes: canonicalBytes,
    snapshotHash: snapshotHash,
    validateSnapshot: validateSnapshot,
    loadSnapshot: loadSnapshot,
    projectCapabilities: projectCapabilities,
    normalizeCapabilities: normalizeCapabilities,
    projectModel: projectModel,
    isFloatToken: isFloatToken,
    stripFreeMarker: stripFreeMarker,
    naturalKey: naturalKey,
    compareNaturalIds: compareNaturalIds,
    endpointKey: endpointKey,
    compareEndpointPairs: compareEndpointPairs,
    listFreeModels: listFreeModels,
    listEndpoints: listEndpoints,
    getModel: getModel,
    watchdogStatus: watchdogStatus,
    searchTextProviderPairs: searchTextProviderPairs,
    searchMinContextPairs: searchMinContextPairs,
    searchInputModalityPairs: searchInputModalityPairs,
    searchSupportedParameterPairs: searchSupportedParameterPairs,
    searchModels: searchModels
  };
});
