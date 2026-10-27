"use strict";

/**
 * Node tests for the dependency-free catalogue validation/projection
 * counterpart (`web/catalog-query.js`).
 *
 * The expected values come from the reviewed golden fixture tables under
 * `tests/fixtures/catalog/` and from hand-authored literals in this file. They
 * are NEVER produced by the implementation under test, and no validator,
 * comparator or canonical encoder is reimplemented here.
 *
 * Run: node --test tests/js/catalog-query.test.cjs
 */

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const catalog = require("../../web/catalog-query.js");

const FIXTURE_DIR = path.join(__dirname, "..", "fixtures", "catalog");

function fixturePath(name) {
  return path.join(FIXTURE_DIR, name);
}

function readFixtureText(name) {
  return fs.readFileSync(fixturePath(name), "utf8");
}

function readFixtureDocument(name) {
  return JSON.parse(readFixtureText(name));
}

const REPO_ROOT = path.join(__dirname, "..", "..");

// Test-owned parity probe against the unchanged Python reference helper. The
// committed literal fixtures remain the correctness oracle; this only checks
// that the dependency-free port did not drift from `watchdog_catalog/query.py`.
const REFERENCE_SCRIPT = [
  "import json, sys",
  'sys.path.insert(0, ".")',
  "from watchdog_catalog.query import (strip_free_marker, natural_key,",
  "                                   endpoint_key)",
  "payload = json.load(sys.stdin)",
  'order = payload["provider_order"]',
  "print(json.dumps({",
  '    "markers": [strip_free_marker(x) for x in payload["ids"]],',
  '    "keys": [[list(t) for t in natural_key(x)] for x in payload["ids"]],',
  '    "order": sorted(payload["ids"], key=natural_key),',
  '    "pairs": [list(p) for p in sorted(',
  '        [tuple(p) for p in payload["pairs"]],',
  "        key=lambda p: endpoint_key(order, p[0], p[1]))],",
  "}))"
].join("\n");

function referencePython() {
  for (const candidate of [process.env.CATALOG_PYTHON,
                           "/home/kimbo/.local/bin/python3.11"]) {
    if (candidate && fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return "python3";
}

function runPythonReference(payload) {
  const { execFileSync } = require("node:child_process");
  const env = Object.assign({}, process.env);
  delete env.PYTHONPATH;
  const stdout = execFileSync(referencePython(), ["-c", REFERENCE_SCRIPT], {
    cwd: REPO_ROOT, env, input: JSON.stringify(payload), encoding: "utf8"
  });
  return JSON.parse(stdout);
}

const RAW_ERROR_MESSAGE = "Invalid catalogue JSON.";
const INVALID_SNAPSHOT_MESSAGE = "Invalid catalogue snapshot.";
const UNSUPPORTED_VERSION_MESSAGE = "Unsupported catalogue schema version.";

/** True when both values are the same JSON-shaped value (signed zero pinned). */
function deepSame(actual, expected, where) {
  const at = where || "$";
  const actualFloat = catalog.isFloatToken(actual);
  const expectedFloat = catalog.isFloatToken(expected);
  if (actualFloat || expectedFloat) {
    if (typeof actual === "boolean" || typeof expected === "boolean") {
      return false;
    }
    const left = actualFloat ? actual.value : actual;
    const right = expectedFloat ? expected.value : expected;
    if (typeof left !== "number" || typeof right !== "number") {
      return false;
    }
    return Object.is(left, right);
  }
  if (actual === null || expected === null) {
    return actual === expected;
  }
  if (Array.isArray(actual) || Array.isArray(expected)) {
    if (!Array.isArray(actual) || !Array.isArray(expected)) {
      return false;
    }
    if (actual.length !== expected.length) {
      return false;
    }
    for (let index = 0; index < actual.length; index += 1) {
      if (!deepSame(actual[index], expected[index], `${at}[${index}]`)) {
        return false;
      }
    }
    return true;
  }
  if (typeof actual === "object" && typeof expected === "object") {
    const actualKeys = Object.keys(actual).sort();
    const expectedKeys = Object.keys(expected).sort();
    if (actualKeys.length !== expectedKeys.length) {
      return false;
    }
    for (let index = 0; index < actualKeys.length; index += 1) {
      if (actualKeys[index] !== expectedKeys[index]) {
        return false;
      }
    }
    return actualKeys.every((key) => deepSame(actual[key], expected[key],
                                               `${at}.${key}`));
  }
  if (typeof actual === "number" && typeof expected === "number") {
    return Object.is(actual, expected);
  }
  return actual === expected;
}

function assertSameValue(actual, expected, where) {
  assert.ok(deepSame(actual, expected, where),
            `${where || "$"}: decoded value does not match the golden value`);
}

/** The frozen literal envelope produced by a caught `CatalogError`. */
function errorEnvelope(error) {
  assert.ok(error instanceof catalog.CatalogError,
            `expected a CatalogError, got ${error && error.constructor.name}: ` +
            `${error && error.message}`);
  const payload = error.toPayload();
  assert.deepEqual(Object.keys(payload).sort(), ["error", "ok"],
                   "a failure envelope carries exactly ok + error");
  assert.deepEqual(Object.keys(payload.error).sort(), ["code", "message"],
                   "a failure error carries exactly code + message");
  assert.equal(payload.ok, false, "`ok` is strictly false");
  return payload;
}

function assertRejects(run, code, message, where) {
  let caught = null;
  try {
    run();
  } catch (error) {
    caught = error;
  }
  assert.notEqual(caught, null, `${where}: expected a rejection`);
  const payload = errorEnvelope(caught);
  assert.equal(payload.error.code, code, `${where}: wrong error code`);
  assert.equal(payload.error.message, message, `${where}: wrong literal message`);
  return payload;
}

function assertAccepts(run, where) {
  try {
    return run();
  } catch (error) {
    assert.fail(`${where}: expected acceptance, got ${error && error.message}`);
  }
}

// -- literal depth builders (grouped edge rows, not one helper per row) -----

function nestedArray(depth) {
  return "[".repeat(depth) + "0" + "]".repeat(depth);
}

function nestedObject(depth) {
  return '{"a":'.repeat(depth) + "0" + "}".repeat(depth);
}

function nestedMixed(depth) {
  let open = "";
  let close = "";
  for (let index = 0; index < depth; index += 1) {
    if (index % 2 === 0) {
      open += "[";
      close = "]" + close;
    } else {
      open += '{"a":';
      close = "}" + close;
    }
  }
  return open + "0" + close;
}

// --------------------------------------------------------------------------
// raw JSON acceptance/rejection boundary
// --------------------------------------------------------------------------
describe("raw JSON acceptance boundary", () => {
  const rawTable = readFixtureDocument("raw-json-cases.json");

  it("rejects every invalid row with the frozen literal envelope", () => {
    assert.ok(rawTable.invalid_cases.length >= 30, "raw table is populated");
    for (const row of rawTable.invalid_cases) {
      const payload = assertRejects(() => catalog.loadJson(row.json_text),
                                    "invalid_json", RAW_ERROR_MESSAGE, row.id);
      assertSameValue(payload, row.expect, row.id);
    }
  });

  it("decodes every valid control to the golden value and token kind", () => {
    assert.ok(rawTable.valid_controls.length >= 20, "controls are populated");
    for (const row of rawTable.valid_controls) {
      assertAccepts(() => catalog.loadJson(row.json_text), row.id);
      if (Object.prototype.hasOwnProperty.call(row, "expect_depth")) {
        assert.equal(row.expect_depth, catalog.MAX_JSON_DEPTH);
        assert.ok(["array", "object", "mixed"].includes(row.expect_kind));
        const decoded = catalog.loadJson(row.json_text);
        if (row.expect_kind === "object") {
          assert.ok(decoded && typeof decoded === "object" &&
                    !Array.isArray(decoded), `${row.id}: expected an object`);
        } else {
          assert.ok(Array.isArray(decoded), `${row.id}: expected an array`);
        }
        continue;
      }
      const decoded = catalog.loadJson(row.json_text);
      assertSameValue(decoded, row.expect, row.id);
      // Token identity is the fixture's own classification: every `float`
      // row must stay a float token even when its value is integral.
      if (row.category === "float") {
        assert.ok(catalog.isFloatToken(decoded),
                  `${row.id}: a float token must keep float identity`);
      } else if (typeof row.expect === "number") {
        assert.equal(typeof decoded, "number",
                     `${row.id}: an integer token must stay a plain number`);
        assert.ok(catalog.isFloatToken(decoded) === false,
                  `${row.id}: an integer token is not a float token`);
      }
    }
  });

  it("enforces the inclusive container-depth bound 64 and ignores quoted brackets", () => {
    for (const build of [nestedArray, nestedObject, nestedMixed]) {
      assertAccepts(() => catalog.loadJson(build(64)), `depth 64 ${build.name}`);
      assertRejects(() => catalog.loadJson(build(65)), "invalid_json",
                    RAW_ERROR_MESSAGE, `depth 65 ${build.name}`);
    }
    assertAccepts(() => catalog.loadJson("42"), "root scalar");
    assert.equal(catalog.loadJson("42"), 42);
    const quoted = "\"" + "[".repeat(70) + "\"";
    assertAccepts(() => catalog.loadJson(quoted), "quoted brackets");
    assert.equal(catalog.loadJson(quoted).length, 70);
  });

  it("keeps the safe-integer boundary inclusive in every notation", () => {
    assert.equal(catalog.loadJson("9007199254740991"), 9007199254740991);
    assert.equal(catalog.loadJson("-9007199254740991"), -9007199254740991);
    for (const bad of ["9007199254740992", "-9007199254740992",
                       "9007199254740992.0", "-9007199254740992.0",
                       "1e16", "-1e16", '{"a":[{"b":9007199254740993}]}']) {
      assertRejects(() => catalog.loadJson(bad), "invalid_json",
                    RAW_ERROR_MESSAGE, bad);
    }
    // Numeric-looking STRINGS are never inspected as numbers.
    assert.equal(catalog.loadJson('"9007199254740992"'), "9007199254740992");
    assert.equal(catalog.loadJson('"NaN"'), "NaN");
    assert.equal(catalog.loadJson('"1e16"'), "1e16");
  });

  it("keeps float identity, signed zero and binary64 rounding", () => {
    const integral = catalog.loadJson("1.0");
    assert.ok(catalog.isFloatToken(integral), "1.0 stays a float token");
    assert.ok(Object.is(integral.value, 1));
    const negativeZero = catalog.loadJson("-0.0");
    assert.ok(catalog.isFloatToken(negativeZero), "-0.0 stays a float token");
    assert.ok(Object.is(negativeZero.value, -0), "-0.0 keeps its sign");
    assert.equal(catalog.loadJson("1.0000000000000001").value, 1);
    assert.equal(catalog.loadJson("9007199254740991.1").value, 9007199254740991);
    assert.equal(catalog.loadJson("1e-400").value, 0);
    assert.equal(catalog.loadJson("5e-324").value, 5e-324);
    assert.equal(catalog.loadJson("0.5").value, 0.5);
  });

  it("rejects a duplicate member name at every level, escapes included", () => {
    for (const bad of ['{"a":1,"a":2}', '{"a":1,"\\u0061":2}',
                       '{"x":{"b":1,"b":2}}', '[{"c":1,"c":1}]',
                       '{"a":1,"b":{"c":1,"c":2}}']) {
      assertRejects(() => catalog.loadJson(bad), "invalid_json",
                    RAW_ERROR_MESSAGE, bad);
    }
    assertSameValue(catalog.loadJson('{"A":1,"a":2}'), { A: 1, a: 2 },
                    "case-distinct keys");
    assertSameValue(catalog.loadJson('{"x":{"a":1},"y":{"a":2}}'),
                    { x: { a: 1 }, y: { a: 2 } },
                    "the same name in separate objects");
  });

  it("treats prototype-looking member names as ordinary identifiers", () => {
    const decoded = catalog.loadJson(
      '{"__proto__":{"polluted":true},"constructor":1,"prototype":2}');
    assert.deepEqual(Object.keys(decoded).sort(),
                     ["__proto__", "constructor", "prototype"]);
    assert.equal(Object.getPrototypeOf(decoded), Object.prototype,
                 "decoding never replaces an object's prototype");
    assert.equal(decoded.constructor, 1);
    assert.equal({}.polluted, undefined,
                 "decoding never pollutes Object.prototype");
    const snapshotShaped = catalog.loadJson(
      '{"schema_version":1,"providers":{"__proto__":{"polluted":true}}}');
    assert.equal(snapshotShaped.providers.polluted, undefined);
    assert.deepEqual(Object.keys(snapshotShaped.providers), ["__proto__"]);
  });
});

// --------------------------------------------------------------------------
// typed canonical encoding
// --------------------------------------------------------------------------
describe("typed canonical encoding", () => {
  const canonicalTable = readFixtureDocument("canonical-cases.json");
  const digest = (bytes) =>
    require("node:crypto").createHash("sha256").update(bytes).digest("hex");

  it("re-derives every general-value vector text and SHA-256 digest", () => {
    const vectors = canonicalTable.canonical_vectors
      .filter((row) => row.scope === "general_value");
    assert.ok(vectors.length >= 10, "general-value vectors are populated");
    for (const row of vectors) {
      const value = catalog.loadJson(row.json_text);
      assert.equal(catalog.canonicalText(value), row.canonical_text,
                   `${row.id}: canonical text`);
      const bytes = catalog.canonicalBytes(value);
      assert.ok(bytes instanceof Uint8Array,
                `${row.id}: canonicalBytes returns bytes`);
      assert.equal(digest(bytes), row.sha256, `${row.id}: canonical digest`);
    }
  });

  it("keeps integer and float tokens distinct and float spellings equivalent", () => {
    const integer = canonicalTable.canonical_vectors
      .find((row) => row.id === "vec_int_lexeme_one");
    const float = canonicalTable.canonical_vectors
      .find((row) => row.id === "vec_float_lexeme_one");
    const exponent = canonicalTable.canonical_vectors
      .find((row) => row.id === "vec_float_lexeme_one_exponent");
    const integerDigest = digest(catalog.canonicalBytes(
      catalog.loadJson(integer.json_text)));
    const floatDigest = digest(catalog.canonicalBytes(
      catalog.loadJson(float.json_text)));
    const exponentDigest = digest(catalog.canonicalBytes(
      catalog.loadJson(exponent.json_text)));
    assert.equal(integerDigest, integer.sha256);
    assert.equal(floatDigest, float.sha256);
    assert.notEqual(integerDigest, floatDigest,
                    "an integer token and a float token hash differently");
    assert.equal(exponentDigest, floatDigest,
                 "equivalent float spellings hash identically");
    // Hand-authored restatement: `1` is an integer, `1.0`/`1e0` are floats.
    assert.equal(catalog.canonicalText(catalog.loadJson("1")), "1");
    assert.equal(catalog.canonicalText(catalog.loadJson("1.0")), "1.0");
    assert.equal(catalog.canonicalText(catalog.loadJson("1e0")), "1.0");
  });

  it("sorts keys by Unicode code point, not UTF-16 code unit", () => {
    const value = catalog.loadJson(
      '{"\\uD83D\\uDE80":"astral","2":"two","\\uFF21":"bmp","10":"ten","\\uE000":"pua"}');
    assert.equal(
      catalog.canonicalText(value),
      '{"10":"ten","2":"two","\\ue000":"pua","\\uff21":"bmp","\\ud83d\\ude80":"astral"}',
      "code-point order places the astral key last");
    const row = canonicalTable.canonical_vectors
      .find((item) => item.id === "vec_key_sorting_codepoint");
    assert.equal(catalog.canonicalText(catalog.loadJson(row.json_text)),
                 row.canonical_text);
  });

  it("escapes to ASCII with the frozen short escapes", () => {
    const row = canonicalTable.canonical_vectors
      .find((item) => item.id === "vec_ensure_ascii_escapes");
    const text = catalog.canonicalText(catalog.loadJson(row.json_text));
    assert.equal(text, row.canonical_text);
    assert.ok([...text].every((character) => character.charCodeAt(0) < 0x7f),
              "canonical text is pure ASCII");
    // Hand-authored escapes: DEL, vertical tab, lone surrogate, astral pair.
    assert.equal(catalog.canonicalText("\u007f"), '"\\u007f"');
    assert.equal(catalog.canonicalText("\u000b"), '"\\u000b"');
    assert.equal(catalog.canonicalText("\ud800"), '"\\ud800"');
    assert.equal(catalog.canonicalText("\u{1f680}"), '"\\ud83d\\ude80"');
    assert.equal(catalog.canonicalText("/"), '"/"');
  });

  it("formats float tokens with the Python reference repr thresholds", () => {
    const floatOf = (literal) => catalog.loadJson(literal);
    assert.equal(catalog.canonicalText(floatOf("0.0001")), "0.0001");
    assert.equal(catalog.canonicalText(floatOf("1e-05")), "1e-05");
    assert.equal(catalog.canonicalText(floatOf("5e-324")), "5e-324");
    assert.equal(catalog.canonicalText(floatOf("1e15")), "1000000000000000.0");
    // 1e16 and above are unsafe float tokens and never reach encoding through
    // the raw loader; the encoder still spells an already-typed float value.
    assert.equal(catalog.canonicalText(new catalog.JsonFloat(1e16)), "1e+16");
    assert.equal(catalog.canonicalText(new catalog.JsonFloat(1.5e16)),
                 "1.5e+16");
    assert.equal(catalog.canonicalText(floatOf("-0.0")), "-0.0");
    assert.equal(catalog.canonicalText(floatOf("1.0")), "1.0");
    assert.equal(catalog.canonicalText(floatOf("0.5")), "0.5");
    assert.equal(catalog.canonicalText(floatOf("-1.25")), "-1.25");
  });

  it("refuses a non-JSON value instead of silently encoding it", () => {
    assert.throws(() => catalog.canonicalText(new Date(0)), TypeError);
    assert.throws(() => catalog.canonicalText(new Map()), TypeError);
    assert.throws(() => catalog.canonicalText(undefined), TypeError);
    assert.throws(() => catalog.canonicalText({ nested: undefined }), TypeError);
  });
});

// --------------------------------------------------------------------------
// SHA-256 padding and block boundaries
// --------------------------------------------------------------------------
describe("SHA-256 canonical body padding", () => {
  const crypto = require("node:crypto");
  const digest = (bytes) =>
    crypto.createHash("sha256").update(bytes).digest("hex");

  // An INDEPENDENTLY sealed control, not a snapshot the implementation under
  // test produced: this canonical body text and its `snapshot_id` come from
  // Python `hashlib` over the reference `json.dumps` encoding. The body is
  // 1143 bytes, i.e. 55 mod 64 -- exactly the class where padding that adds a
  // whole extra block when `length + 9` is already a multiple of 64 goes
  // wrong. Both values are literal file content here.
  const PYTHON_SEALED_BODY =
    `{"cadence_s":3600,"provider_order":["synthetic-a","synthetic-b"],"providers":{"synthetic-a":{"api_type":"openai","chat_completions_url":"https://synthetic-alpha.example/v1/chat/completions","display_name":"Synthetic Alpha","model_ids":["alpha-1:free","alpha-2-free"],"models":{"alpha-1:free":{"architecture":{"input_modalities":["text"],"output_modalities":["text"]},"context_length":32768,"description":"Synthetic model one for the contract freeze.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","id":"alpha-1:free","name":"Alpha One","pricing":{"completion":"0","prompt":"0"},"supported_parameters":["temperature","max_tokens"]},"alpha-2-free":{"architecture":{"input_modalities":["text"],"output_modalities":["text"]},"context_length":8192,"description":null,"id":"alpha-2-free","name":"Alpha Two","pricing":{"completion":"0","prompt":"0"},"supported_parameters":["temperature"]}},"stale":false},"synthetic-b":{"api_type":"openai","chat_completions_url":"https://synthetic-beta.example/v1/chat/completions","display_name":"Synthetic Beta","model_ids":[],"models":{},"stale":false}},"schema_version":1,"tick_epoch":1759200000,"warnings":[]}`;
  const PYTHON_SEALED_ID =
    "61b303d4d06d4a367aec6b13223dbb21599d37794ce5a7c1d5235dec4b8fa2fb";
  // The digest the spurious-extra-block encoder produced for that same body.
  const SPURIOUS_BLOCK_DIGEST =
    "b7340b0f76cd7e0d13e4be2fb2b5cb2eb6c87c7da6e62fd8d0f57f90007b1395";

  function withoutId(document) {
    const body = {};
    for (const key of Object.keys(document)) {
      if (key !== "snapshot_id") {
        body[key] = document[key];
      }
    }
    return body;
  }

  function skeleton(filler) {
    return {
      schema_version: 1,
      tick_epoch: 1759200000,
      cadence_s: 3600,
      provider_order: ["synthetic-a"],
      providers: {
        "synthetic-a": {
          display_name: "Synthetic Alpha",
          chat_completions_url:
            "https://synthetic-alpha.example/v1/chat/completions",
          api_type: "openai",
          model_ids: ["alpha-1:free"],
          models: {
            "alpha-1:free": {
              id: "alpha-1:free",
              name: "Alpha One",
              description: filler,
              context_length: 32768,
              architecture: {
                input_modalities: ["text"], output_modalities: ["text"]
              },
              supported_parameters: ["temperature"],
              pricing: { prompt: "0", completion: "0" }
            }
          },
          stale: false
        }
      },
      warnings: []
    };
  }

  const BARE_BODY_LENGTH = catalog.canonicalBytes(skeleton("")).length;

  /** A snapshot whose canonical body is exactly `target` bytes, sealed with
   * `node:crypto` -- an oracle independent of the module's own digest. */
  function sealedAt(target) {
    const document = skeleton("x".repeat(target - BARE_BODY_LENGTH));
    const bytes = catalog.canonicalBytes(withoutId(document));
    assert.equal(bytes.length, target, `${target}: canonical body length`);
    document.snapshot_id = digest(bytes);
    return document;
  }

  it("matches Python hashlib on an independently sealed 55-mod-64 body", () => {
    const bodyBytes = Buffer.from(PYTHON_SEALED_BODY, "utf8");
    assert.equal(bodyBytes.length, 1143, "the control body is 1143 bytes");
    assert.equal(bodyBytes.length % 64, 55,
                 "the control body is in the 55-mod-64 padding class");
    assert.equal(digest(bodyBytes), PYTHON_SEALED_ID,
                 "node:crypto reproduces the Python hashlib seal");
    assert.notEqual(SPURIOUS_BLOCK_DIGEST, PYTHON_SEALED_ID,
                    "the extra-block digest is not the Python seal");
    const body = catalog.loadJson(PYTHON_SEALED_BODY);
    assert.equal(catalog.canonicalText(body), PYTHON_SEALED_BODY,
                 "the typed body re-encodes to the exact sealed text");
    const document = Object.assign({}, body, {
      snapshot_id: PYTHON_SEALED_ID
    });
    const accepted = assertAccepts(() => catalog.validateSnapshot(document),
                                   "python-sealed 55-mod-64 body");
    assert.equal(accepted.snapshot_id, PYTHON_SEALED_ID);
    assert.equal(catalog.snapshotHash(accepted), PYTHON_SEALED_ID,
                 "a valid Python-sealed snapshot is accepted, not rejected");
  });

  it("matches node:crypto across padding and block boundaries", () => {
    // Offsets 55, 119, 183, 247 and 311 mod 64 are the lengths where
    // `length + 9` lands exactly on a block multiple; their neighbours are
    // the ordinary classes that were already right.
    const block = 64;
    const base = Math.ceil((BARE_BODY_LENGTH + 9) / block) * block;
    const offsets = [55, 56, 63, 64, 119, 120, 183, 184, 247, 248, 311, 312];
    const targets = offsets.map((offset) => base + offset);
    targets.push((base * 2) + 55, (base * 2) + 119);
    for (const target of targets) {
      const document = sealedAt(target);
      const expected = digest(catalog.canonicalBytes(withoutId(document)));
      assert.equal(catalog.snapshotHash(document), expected,
                   `${target}: the encoder reproduces the independent digest`);
      assertAccepts(() => catalog.validateSnapshot(document),
                    `${target}-byte canonical body`);
    }
  });

  it("still rejects a wrong, truncated or uppercase id at those boundaries", () => {
    const document = sealedAt(55 + Math.ceil((BARE_BODY_LENGTH + 9) / 64) * 64);
    for (const [label, value] of [
      ["all-zero", "0".repeat(64)],
      ["truncated", document.snapshot_id.slice(1)],
      ["uppercased", document.snapshot_id.toUpperCase()],
      ["malformed", "not-a-digest"]
    ]) {
      const tampered = Object.assign({}, document, { snapshot_id: value });
      assertRejects(() => catalog.validateSnapshot(tampered),
                    "invalid_snapshot", INVALID_SNAPSHOT_MESSAGE,
                    `${label} id at a 55-mod-64 body`);
    }
  });
});

// --------------------------------------------------------------------------
// strict structural validation of a published snapshot
// --------------------------------------------------------------------------
describe("strict structural validation", () => {
  const structuralTable =
    catalog.decodeTypedJson(readFixtureText("structural-cases.json"));
  const canonicalTable = readFixtureDocument("canonical-cases.json");

  function defineMember(target, key, value) {
    Object.defineProperty(target, key, {
      value: value, enumerable: true, writable: true, configurable: true
    });
  }

  function cloneTyped(value) {
    if (catalog.isFloatToken(value)) {
      return new catalog.JsonFloat(value.value);
    }
    if (Array.isArray(value)) {
      return value.map(cloneTyped);
    }
    if (value && typeof value === "object") {
      const copy = {};
      for (const key of Object.keys(value)) {
        defineMember(copy, key, cloneTyped(value[key]));
      }
      return copy;
    }
    return value;
  }

  function applyChanges(root, changes, cid) {
    let current = root;
    for (const change of changes) {
      assert.ok(change && typeof change.op === "string",
                `${cid}: each change names an op`);
      if (change.op === "replace_root") {
        current = cloneTyped(change.value);
        continue;
      }
      assert.ok(Array.isArray(change.path) && change.path.length > 0,
                `${cid}: a change path is a nonempty component list`);
      let parent = current;
      for (const component of change.path.slice(0, -1)) {
        parent = parent[component];
      }
      const last = change.path[change.path.length - 1];
      if (change.op === "delete") {
        if (Array.isArray(parent)) {
          parent.splice(last, 1);
        } else {
          delete parent[last];
        }
      } else if (change.op === "set") {
        if (Array.isArray(parent)) {
          parent[last] = cloneTyped(change.value);
        } else {
          defineMember(parent, last, cloneTyped(change.value));
        }
      } else {
        throw new Error(`${cid}: unknown structural change op ${change.op}`);
      }
    }
    return current;
  }

  function mutate(row) {
    const base = catalog.loadJson(readFixtureText(row.base_fixture));
    const snapshot = applyChanges(base, row.changes, row.id);
    if (row.reseal) {
      assert.ok(snapshot && typeof snapshot === "object" &&
                !Array.isArray(snapshot),
                `${row.id}: only an object body can be resealed`);
      snapshot.snapshot_id = catalog.snapshotHash(snapshot);
    }
    return snapshot;
  }

  it("rejects every invalid structural row with its frozen envelope", () => {
    assert.ok(structuralTable.invalid_cases.length >= 50,
              "structural invalid rows are populated");
    for (const row of structuralTable.invalid_cases) {
      const snapshot = mutate(row);
      if (row.reseal) {
        // The hash is TRUE before the semantic defect is tested, so no row
        // can pass merely by failing the hash check.
        assert.equal(catalog.snapshotHash(snapshot), snapshot.snapshot_id,
                     `${row.id}: resealed body hashes consistently`);
      }
      const payload = assertRejects(
        () => catalog.validateSnapshot(snapshot),
        row.expect.error.code, row.expect.error.message, row.id);
      assertSameValue(payload, row.expect, row.id);
    }
  });

  it("accepts every valid control, returning the same unmutated object", () => {
    assert.ok(structuralTable.valid_controls.length >= 8,
              "structural valid controls are populated");
    for (const row of structuralTable.valid_controls) {
      const snapshot = mutate(row);
      const before = catalog.canonicalText(snapshot);
      const accepted = assertAccepts(
        () => catalog.validateSnapshot(snapshot), row.id);
      assert.equal(accepted, snapshot,
                   `${row.id}: validation returns the same object`);
      assert.equal(catalog.canonicalText(snapshot), before,
                   `${row.id}: validation never mutates the snapshot`);
      if (row.reseal) {
        assert.equal(catalog.snapshotHash(snapshot), snapshot.snapshot_id,
                     `${row.id}: resealed body hashes consistently`);
      }
    }
  });

  it("classifies lexically accepted roots as invalid_snapshot", () => {
    for (const text of ["null", "true", "false", "42", '"hello"', "[]",
                        "[1,2]", "{}", '{"a": 1}']) {
      assertAccepts(() => catalog.loadJson(text), `${text}: raw`);
      const payload = assertRejects(() => catalog.loadSnapshot(text),
                                    "invalid_snapshot",
                                    INVALID_SNAPSHOT_MESSAGE, text);
      assertSameValue(payload, { ok: false, error: {
        code: "invalid_snapshot", message: INVALID_SNAPSHOT_MESSAGE } }, text);
    }
    // A raw lexical fault precedes every structural check.
    assertRejects(() => catalog.loadSnapshot("{\"schema_version\": 1,"),
                  "invalid_json", RAW_ERROR_MESSAGE, "truncated document");
  });

  it("reserves unsupported_schema_version for a strict integer other than 1", () => {
    const unsupported = mutate({
      id: "probe-version-2", base_fixture: "lookup.json", reseal: true,
      changes: [{ op: "set", path: ["schema_version"], value: 2 }]
    });
    assertRejects(() => catalog.validateSnapshot(unsupported),
                  "unsupported_schema_version", UNSUPPORTED_VERSION_MESSAGE,
                  "schema_version 2");
    for (const wrongType of [true, "1", null, catalog.loadJson("1.0")]) {
      const probe = mutate({
        id: "probe-version-type", base_fixture: "lookup.json", reseal: true,
        changes: [{ op: "set", path: ["schema_version"], value: wrongType }]
      });
      assertRejects(() => catalog.validateSnapshot(probe), "invalid_snapshot",
                    INVALID_SNAPSHOT_MESSAGE,
                    `schema_version ${String(wrongType)}`);
    }
  });

  it("keeps the body hash load-bearing", () => {
    const wrongId = mutate({
      id: "probe-wrong-hash", base_fixture: "lookup.json", reseal: false,
      changes: [{ op: "set", path: ["snapshot_id"], value: "0".repeat(64) }]
    });
    assertRejects(() => catalog.validateSnapshot(wrongId), "invalid_snapshot",
                  INVALID_SNAPSHOT_MESSAGE, "well-shaped wrong hash");
    const malformed = mutate({
      id: "probe-malformed-hash", base_fixture: "lookup.json", reseal: false,
      changes: [{ op: "set", path: ["snapshot_id"], value: "not-a-hash" }]
    });
    assertRejects(() => catalog.validateSnapshot(malformed), "invalid_snapshot",
                  INVALID_SNAPSHOT_MESSAGE, "malformed hash");
    // A nested snapshot_id is ordinary hashed content, never excluded.
    const nested = catalog.loadJson(
      '{"snapshot_id":"a","inner":{"snapshot_id":"b"}}');
    const other = catalog.loadJson(
      '{"snapshot_id":"a","inner":{"snapshot_id":"c"}}');
    assert.notEqual(catalog.snapshotHash(nested), catalog.snapshotHash(other),
                    "a nested snapshot_id changes the body digest");
    assert.equal(
      catalog.canonicalText(nested),
      '{"inner":{"snapshot_id":"b"},"snapshot_id":"a"}',
      "the generic encoder excludes no key");
  });

  it("pins the strict snapshot integer tokens from the canonical table", () => {
    for (const row of canonicalTable.snapshot_cases) {
      assertAccepts(() => catalog.loadJson(row.json_text), `${row.id}: raw`);
      if (row.accepted === true) {
        const accepted = assertAccepts(
          () => catalog.loadSnapshot(row.json_text), row.id);
        assert.equal(catalog.snapshotHash(accepted), accepted.snapshot_id,
                     `${row.id}: accepted document re-hashes`);
        continue;
      }
      const payload = assertRejects(
        () => catalog.loadSnapshot(row.json_text),
        row.expect.error.code, row.expect.error.message, row.id);
      assertSameValue(payload, row.expect, row.id);
      if (row.category !== "snapshot_hash_tamper") {
        // Every other case is correctly sealed: the rejection is not a hash
        // failure but the strict token rule.
        const decoded = catalog.loadJson(row.json_text);
        assert.equal(catalog.snapshotHash(decoded), decoded.snapshot_id,
                     `${row.id}: hash is TRUE before the token rule bites`);
      }
    }
  });

  it("keeps identifier namespaces, epochs and zero values honest", () => {
    // A prototype-looking provider/model id is an ordinary identifier
    // namespace, not a private field name.
    const providerKey = "__proto__";
    const modelId = "constructor";
    const base = catalog.loadJson(readFixtureText("lookup.json"));
    const provider = cloneTyped(base.providers["synthetic-a"]);
    provider.model_ids = [modelId];
    const model = cloneTyped(base.providers["synthetic-a"].models[
      "vendor/model:free"]);
    model.id = modelId;
    const models = {};
    defineMember(models, modelId, model);
    provider.models = models;
    const providers = {};
    defineMember(providers, providerKey, provider);
    const snapshot = {
      schema_version: 1, tick_epoch: 0, cadence_s: 3600,
      provider_order: [providerKey], providers: providers, warnings: []
    };
    snapshot.snapshot_id = catalog.snapshotHash(snapshot);
    const accepted = assertAccepts(() => catalog.validateSnapshot(snapshot),
                                   "prototype-looking identifiers");
    assert.equal(accepted.providers[providerKey].models[modelId].id, modelId);
    assert.deepEqual(Object.keys(accepted.providers), [providerKey]);
    assert.equal({}.polluted, undefined, "no prototype pollution");

    // A far-future epoch and a zero epoch are valid strict integers.
    for (const epoch of [0, 32503680000]) {
      const probe = mutate({
        id: `probe-epoch-${epoch}`, base_fixture: "lookup.json", reseal: true,
        changes: [{ op: "set", path: ["tick_epoch"], value: epoch }]
      });
      assertAccepts(() => catalog.validateSnapshot(probe),
                    `tick_epoch ${epoch}`);
    }
    // A null tick still demands the unknown-warning code.
    const nullTick = mutate({
      id: "probe-null-tick", base_fixture: "lookup.json", reseal: true,
      changes: [{ op: "set", path: ["tick_epoch"], value: null },
                { op: "set", path: ["warnings"], value: [] }]
    });
    assertRejects(() => catalog.validateSnapshot(nullTick), "invalid_snapshot",
                  INVALID_SNAPSHOT_MESSAGE, "null tick without the warning");
  });

  it("is pure across interleaved calls", () => {
    const first = mutate({ id: "probe-interleave-a",
                           base_fixture: "lookup.json", reseal: false,
                           changes: [] });
    const second = mutate({ id: "probe-interleave-b",
                            base_fixture: "basic.json", reseal: false,
                            changes: [] });
    const firstText = catalog.canonicalText(first);
    const secondText = catalog.canonicalText(second);
    for (let round = 0; round < 50; round += 1) {
      assertAccepts(() => catalog.validateSnapshot(first), `round ${round} a`);
      assertAccepts(() => catalog.validateSnapshot(second), `round ${round} b`);
    }
    assert.equal(catalog.canonicalText(first), firstText);
    assert.equal(catalog.canonicalText(second), secondText);
  });
});

// --------------------------------------------------------------------------
// wiring URL authority validation
// --------------------------------------------------------------------------
describe("wiring URL authority", () => {
  const crypto = require("node:crypto");
  const digest = (bytes) =>
    crypto.createHash("sha256").update(bytes).digest("hex");
  // 1100 + 9 is not a multiple of 64, so a URL verdict here can never be
  // produced by the separate SHA-256 padding class.
  const WIRING_BODY_LENGTH = 1100;

  function bodyBytes(snapshot) {
    const body = {};
    for (const key of Object.keys(snapshot)) {
      if (key !== "snapshot_id") {
        body[key] = snapshot[key];
      }
    }
    return catalog.canonicalBytes(body);
  }

  /** `basic.json` with one wiring URL swapped in, padded back to a fixed
   * canonical body length and sealed with `node:crypto` -- so the only thing
   * under test is the wiring predicate itself. */
  function sealedWiring(url) {
    const snapshot = catalog.loadJson(readFixtureText("basic.json"));
    const provider = snapshot.providers["synthetic-a"];
    provider.chat_completions_url = url;
    const model = provider.models["alpha-1:free"];
    model.description = "";
    const bare = bodyBytes(snapshot).length;
    model.description = "x".repeat(WIRING_BODY_LENGTH - bare);
    const bytes = bodyBytes(snapshot);
    assert.equal(bytes.length, WIRING_BODY_LENGTH,
                 `${url}: the fixture keeps a fixed canonical body length`);
    snapshot.snapshot_id = digest(bytes);
    return snapshot;
  }

  function assertWiringRejects(url) {
    const snapshot = sealedWiring(url);
    assert.equal(catalog.snapshotHash(snapshot), snapshot.snapshot_id,
                 `${url}: the reseal is hash-clean, so only the URL can fail`);
    assertRejects(() => catalog.validateSnapshot(snapshot), "invalid_snapshot",
                  INVALID_SNAPSHOT_MESSAGE, url);
  }

  function assertWiringAccepts(url) {
    const snapshot = sealedWiring(url);
    const accepted = assertAccepts(() => catalog.validateSnapshot(snapshot), url);
    assert.equal(accepted.providers["synthetic-a"].chat_completions_url, url,
                 `${url}: the original string is preserved verbatim`);
  }

  it("rejects stray, extra and misplaced brackets in the authority", () => {
    for (const url of ["http://foo[bar", "http://foo]bar", "http://foo[bar]",
                       "http://]", "http://[", "http://x[y]z/", "http://x[y/",
                       "http://x]y/", "http://[[::1]/", "http://]x[",
                       "http://a]b[c"]) {
      assertWiringRejects(url);
    }
  });

  it("rejects material after the closing bracket that is not a port", () => {
    for (const url of ["http://[::1]x/", "http://[::1]]/"]) {
      assertWiringRejects(url);
    }
  });

  it("requires the bracketed host to be IPv6 or an IPvFuture literal", () => {
    for (const url of ["http://[]/", "http://[::gg]/", "http://[vG.x]/",
                       "http://[V1.x]/", "http://[v1.]/",
                       "http://[::ffff:999.2.3.4]/", "http://[1.2.3.4]/",
                       "http://[12345::]/", "http://[::g]/",
                       "http://[1:2:3:4:5:6:7]/",
                       "http://[1:2:3:4:5:6:7:8:9]/", "http://[:::1]/",
                       "http://[1::2::3]/", "http://[1.2.3.4::]/",
                       "http://[::1%]/", "http://[::1%a%b]/"]) {
      assertWiringRejects(url);
    }
  });

  it("keeps valid bracketed and near-neighbour wiring verbatim", () => {
    for (const url of ["http://[::1]/v1", "http://[::1]:8080/v1",
                       "http://[v1.x]/", "http://[vF.a]/", "http://[::]/",
                       "http://[2001:db8::1]/", "http://[::ffff:1.2.3.4]/",
                       "http://[fe80::1%25eth0]/", "http://[::1]:80x/",
                       "http://[::1]:/x", "http://[::1]:99999/",
                       "http://[::1]", "http://[::1]#", "HTTP://[::1]/v1",
                       "http://example.com#", "http://example.com:bad",
                       "http://example.com?q=1", "http://example.com./",
                       "http://exa_mple.com/", "http://a..b/",
                       "http://.example.com/", "http://-example.com/",
                       "http://\u00c9XAMPLE.com/",
                       "https://\u4f8b\u3048.\u30c6\u30b9\u30c8/",
                       "http://example.com\\path", "http://exa%20mple.com/"]) {
      assertWiringAccepts(url);
    }
  });

  // The reference `_check_bracketed_host` is `\Av[a-fA-F0-9]+\..+\Z`, and
  // Python's `.` excludes ONLY `\n`. A payload carrying U+2028/U+2029 is
  // therefore ACCEPTED by the reference, while JavaScript's `.` excludes those
  // two separators (and `\r`) as line terminators. The expected verdicts below
  // were taken from the reference -- floor 3.11.15 `_is_wiring_url` AND
  // `validate_snapshot` over a Python-sealed body -- never from this module.
  // The whole-URL ASCII whitespace/control scan runs FIRST, so the payload
  // class is never consulted for LF, CR, FF or any other forbidden control;
  // those rows pin that containment, and the malformed rows pin that the
  // bracket/version/userinfo/fragment rules still win around the payload.
  it("keeps IPvFuture payload characters in step with the reference dot", () => {
    // Accepted by the reference: the two Unicode separators in both payload
    // positions and together, with ordinary neighbours that already agreed.
    for (const url of ["http://[v1.\u2028x]/", "http://[v1.\u2029x]/",
                       "http://[v1.x\u2028]/", "http://[v1.x\u2029]/",
                       "http://[v1.a\u2028b\u2029c]/", "http://[vA.\u2028]",
                       "http://[v1.\u2028x]#", "http://[v1.xy]/",
                       "http://[v1.\u00e9]/", "http://[v1.\u0085x]/"]) {
      assertWiringAccepts(url);
    }
    // Refused by the reference: forbidden ASCII controls in the payload, a
    // separator outside the payload (version, before the dot, before the
    // bracket, after the bracket), and an empty payload.
    for (const url of ["http://[v1.x\u000ay]/", "http://[v1.x\u000dy]/",
                       "http://[v1.\u2028x\u000a]/",
                       "http://[v1.x\u000cy]/", "http://[v1\u2028.x]/",
                       "http://[v\u20281.x]/", "http://[\u2028v1.x]/",
                       "http://[v1.\u2028x]z/",
                       "http://[v1.\u2028x]#frag",
                       "http://user@[v1.\u2028x]/", "http://[v1.\u2028x/",
                       "http://[v1.]/"]) {
      assertWiringRejects(url);
    }
  });

  // Python's `urlsplit` -> `_checknetloc` is a REJECTION-ONLY safety check on a
  // non-ASCII netloc: drop the delimiters already present (`@ : # ?`), NFKC
  // normalize, and refuse the URL when the normalization changed the text AND
  // introduced any of `/ ? # @ :`. It never rewrites the URL and never rejects
  // non-ASCII on its own, so NFKC-neutral names and NFKC changes that introduce
  // no delimiter stay acceptable. The expected verdicts below were taken from
  // the reference (Python 3.11.15 `_is_wiring_url` + `validate_snapshot`), not
  // from this module.
  it("rejects authorities whose NFKC normalization introduces a delimiter", () => {
    for (const url of [
      "http://example.com\uff0f.evil/path",     // fullwidth solidus
      "http://example\uff0f80/path",
      "http://ex\uff0fample.com/",
      "http://example.com\uff1a80/path",        // fullwidth colon
      "http://example.com:80\uff0fx/",
      "http://example.com\uff20evil/",          // fullwidth commercial at
      "http://example.com\uff03frag",           // fullwidth number sign
      "http://example.com\uff1fq=1",            // fullwidth question mark
      "http://example.com\uff20\uff1a/\uff03",  // several at once
      "http://[v1.\uff0f]/",                   // IPvFuture host
      "http://[vF.\uff1a]/",
      "http://ex\u2100mple.com/",              // ACCOUNT OF normalizes to a/c
      "http://example.com\ufe5fx/",            // small number sign -> #
      "http://ex\u00e4mple.com\uff0fx/",       // NFKC-neutral part + confusable
      "http://\uff0f?[",
    ]) {
      assertWiringRejects(url);
    }
  });

  it("keeps NFKC-neutral and delimiter-free Unicode authorities verbatim", () => {
    for (const url of [
      "http://m\u00fcnchen.de/v1",                  // NFKC-neutral
      "http://xn--m\u00fcnchen.de/",
      "http://\u4f8b\u3048.jp/v1",                  // NFKC-neutral
      "http://\u00e4.example.com/",
      "http://\u00c4\u00d6\u00dc.example/",
      "http://exa\u00a0mple.com/",                  // NBSP -> space, no delimiter
      "http://example.com\u3000/",                  // ideographic space
      "http://\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.com/",  // -> example
      "http://\ufb01le.example/",                   // ligature fi
      "http://\u2163.example/",                     // Roman numeral IV
      "http://example.com:80/",
    ]) {
      assertWiringAccepts(url);
    }
  });
});

// --------------------------------------------------------------------------
// public capability and model projection
// --------------------------------------------------------------------------
describe("public capability and model projection", () => {
  const normalizationTable = readFixtureDocument("metadata-normalization.json");

  it("projects and normalizes every golden capability row", () => {
    assert.ok(normalizationTable.rows.length >= 20,
              "normalization rows are populated");
    assert.equal(normalizationTable.not_a_snapshot, true);
    for (const row of normalizationTable.rows) {
      const [projected, warnings] = catalog.projectCapabilities(row.raw);
      assertSameValue(projected, row.projected, `${row.id}: projected`);
      assert.deepEqual(warnings, row.warnings, `${row.id}: warnings`);
      assert.deepEqual(Object.keys(projected).sort(),
                       ["architecture", "context_length",
                        "supported_parameters"],
                       `${row.id}: projected field set`);
      const normalized = catalog.normalizeCapabilities(projected);
      assertSameValue(normalized, row.normalized, `${row.id}: normalized`);
      assert.deepEqual(
        Object.keys(normalized),
        ["context_length", "input_modalities", "output_modalities",
         "supported_parameters", "unknown_fields"],
        `${row.id}: normalized field order`);
    }
  });

  it("keeps unknown, known-empty and malformed capability data distinct", () => {
    const [missing, missingWarnings] =
      catalog.projectCapabilities({ context_length: 4096 });
    assert.equal(missing.architecture.input_modalities, null);
    assert.equal(missing.supported_parameters, null);
    assert.deepEqual(missingWarnings, ["metadata_missing"]);

    const [empty, emptyWarnings] = catalog.projectCapabilities({
      context_length: 4096,
      architecture: { input_modalities: [], output_modalities: [] },
      supported_parameters: []
    });
    assert.deepEqual(empty.architecture.input_modalities, []);
    assert.deepEqual(emptyWarnings, []);
    const emptyNormalized = catalog.normalizeCapabilities(empty);
    assert.deepEqual(emptyNormalized.unknown_fields, []);

    const [malformed, malformedWarnings] = catalog.projectCapabilities({
      context_length: 4096, supported_parameters: "temperature"
    });
    assert.equal(malformed.supported_parameters, null,
                 "a malformed array is rejected whole");
    // The architecture parent is ABSENT here, so its two children are
    // unknown (metadata_missing) beside the invalid array.
    assert.deepEqual(malformedWarnings,
                     ["metadata_missing", "metadata_invalid"]);

    // A float TOKEN context is malformed, never coerced to an integer.
    const typed = catalog.decodeTypedJson(
      '{"context_length": 1.0, "architecture": {"input_modalities": ["text"],' +
      ' "output_modalities": ["text"]}, "supported_parameters": ["temperature"]}');
    const [typedProjected, typedWarnings] =
      catalog.projectCapabilities(typed);
    assert.equal(typedProjected.context_length, null);
    assert.deepEqual(typedWarnings, ["metadata_invalid"]);

    assert.throws(() => catalog.projectCapabilities([]), TypeError);
    assert.throws(() => catalog.projectCapabilities("oops"), TypeError);
  });

  it("folds only ASCII case and never trims or Unicode-casefolds", () => {
    const [projected] = catalog.projectCapabilities({
      context_length: 4096,
      architecture: { input_modalities: ["TEXT", "text", "Text"],
                      output_modalities: ["\u00c9", "\u00e9"] },
      supported_parameters: [" Text ", "TEXT"]
    });
    const normalized = catalog.normalizeCapabilities(projected);
    assert.deepEqual(normalized.input_modalities, ["text"]);
    assert.deepEqual(normalized.output_modalities, ["\u00c9", "\u00e9"],
                     "non-ASCII case pairs stay literal");
    assert.deepEqual(normalized.supported_parameters, [" text ", "text"],
                     "whitespace is never trimmed, so the fold keeps both");
  });

  it("projects a complete public model with the frozen field set", () => {
    const [model, warnings] = catalog.projectModel("vendor/model:free", {
      id: "ignored-upstream-id",
      name: "Vendor Model",
      description: "A description.",
      context_length: 65536,
      architecture: { input_modalities: ["text"],
                      output_modalities: ["text"] },
      supported_parameters: ["temperature"],
      pricing: { prompt: "0", completion: 0 },
      private_field: "never serialized",
      _token: "never serialized"
    });
    assert.deepEqual(Object.keys(model).sort(),
                     ["architecture", "context_length", "description", "id",
                      "name", "pricing", "supported_parameters"]);
    assert.deepEqual(warnings, []);
    assertSameValue(model, {
      id: "vendor/model:free",
      name: "Vendor Model",
      description: "A description.",
      context_length: 65536,
      architecture: { input_modalities: ["text"],
                      output_modalities: ["text"] },
      supported_parameters: ["temperature"],
      pricing: { prompt: "0", completion: 0 }
    }, "complete projection");
  });

  it("applies the name, description and pricing degradation rules", () => {
    const [fallback] = catalog.projectModel("vendor/raw-id", { name: "",
                                                              description: 5 });
    assert.equal(fallback.name, "vendor/raw-id",
                 "an empty name falls back to the tracked key");
    assert.equal(fallback.description, null,
                 "a non-string description is unknown");
    const [emptyDescription] = catalog.projectModel("vendor/x",
                                                    { description: "" });
    assert.equal(emptyDescription.description, "",
                 "an empty string description is retained");

    for (const pricing of [undefined, null, "free", []]) {
      const [model] = catalog.projectModel("vendor/x", { pricing: pricing });
      assert.equal(model.pricing, null, `pricing ${String(pricing)} -> null`);
    }
    const [emptyPricing] = catalog.projectModel("vendor/x", { pricing: {} });
    assertSameValue(emptyPricing.pricing,
                    { prompt: null, completion: null },
                    "an empty pricing object projects both scalars null");
    const [partial] = catalog.projectModel("vendor/x", {
      pricing: { prompt: "0", completion: true, currency: "USD" }
    });
    assertSameValue(partial.pricing, { prompt: "0", completion: null },
                    "a valid sibling survives and extra keys are dropped");
    const [spelling] = catalog.projectModel("vendor/x", {
      pricing: { prompt: "0.000001", completion: 1.5 }
    });
    assert.equal(spelling.pricing.prompt, "0.000001",
                 "a numeric string keeps its spelling");
    assert.equal(spelling.pricing.completion, 1.5);
  });

  it("reports only capability warnings for the metadata container", () => {
    const cases = [
      [undefined, ["metadata_missing"]],
      [null, ["metadata_missing"]],
      [{}, ["metadata_missing"]],
      ["oops", ["metadata_invalid"]],
      [[], ["metadata_invalid"]],
      [{ context_length: null, supported_parameters: "temperature" },
       ["metadata_missing", "metadata_invalid"]]
    ];
    for (const [container, expected] of cases) {
      const [, warnings] = catalog.projectModel("vendor/x", container);
      assert.deepEqual(warnings, expected,
                       `container ${JSON.stringify(container)} warnings`);
    }
    assert.throws(() => catalog.projectModel("", {}), TypeError);
    assert.throws(() => catalog.projectModel(5, {}), TypeError);
  });

  it("never aliases its input or leaks a private member", () => {
    const raw = {
      name: "Aliased",
      architecture: { input_modalities: ["text"],
                      output_modalities: ["text"] },
      supported_parameters: ["temperature"],
      pricing: { prompt: "0", completion: "0" }
    };
    const [model] = catalog.projectModel("vendor/x", raw);
    assert.notEqual(model.architecture.input_modalities,
                    raw.architecture.input_modalities,
                    "retained arrays are fresh copies");
    model.architecture.input_modalities.push("image");
    model.supported_parameters.push("tools");
    assert.deepEqual(raw.architecture.input_modalities, ["text"]);
    assert.deepEqual(raw.supported_parameters, ["temperature"]);

    const typedRaw = catalog.decodeTypedJson(
      '{"name": "M", "__proto__": {"leak": true}, "api_key": "secret"}');
    const [typedModel] = catalog.projectModel("vendor/x", typedRaw);
    assert.deepEqual(Object.keys(typedModel).sort(),
                     ["architecture", "context_length", "description", "id",
                      "name", "pricing", "supported_parameters"]);
    assert.equal(typedModel.api_key, undefined);
    assert.equal(typedModel.leak, undefined);
    assert.equal({}.leak, undefined, "projection never pollutes a prototype");
  });
});

// --------------------------------------------------------------------------
// pure ordering and free-marker helpers
// --------------------------------------------------------------------------
// The frozen README contracts: (a) the marker stripper removes ONE delimiter-
// bounded suffix then ONE prefix pass and falls back to the input when a strip
// would empty it -- Python's `$` also matches before ONE final LF; (b) provider
// precedence is the INDEX in `provider_order`; (c) within a provider, raw ids
// compare by maximal ASCII `[0-9]+` runs (ORIGINAL digit count, then lexical
// ASCII digits), with case-sensitive Unicode CODE POINT text. Every expectation
// below is a literal fixture row or a hand-authored literal, never an array
// produced by the comparator under test. `array.sort` is always called with the
// PRODUCTION comparator, so no second comparator is implemented in the tests.
describe("ordering and free-marker helpers", () => {
  const orderingCases = readFixtureDocument("ordering-cases.json");
  const orderingSnapshot = readFixtureDocument("ordering.json");
  const markerCases = readFixtureDocument("cases.json");

  // Literal delimiter-only fallbacks and the characterised regex edges of
  // `watchdog_catalog/query.py` (one suffix pass, one prefix pass, Python `$`).
  const MARKER_FALLBACK_CONTROLS = [
    [":free", ":free"], ["-free", "-free"], ["_free", "_free"],
    ["free:", "free:"], ["free-", "free-"], ["free_", "free_"],
    ["free", "free"]
  ];
  const MARKER_COMPAT_EDGES = [
    ["free-free-X-free-free", "free-X-free"],
    ["vendor/x:free\n", "vendor/x\n"],
    ["vendor/x:free\n\n", "vendor/x:free\n\n"],
    ["free:", "free:"],
    [" free-x ", " free-x "],
    ["vendor/freebie", "vendor/freebie"],
    ["FREE-X:free", "X"],
    // The final-LF nuance is Python `$`, not a JS `$`: ONE final LF is still an
    // end-of-input anchor for the suffix, a CRLF is not, and neither are the
    // Unicode line separators that a dot/all-`\s` guess would sweep up.
    ["vendor/x:free\r\n", "vendor/x:free\r\n"],
    ["Vendor/X:FREE\n", "Vendor/X\n"],
    ["free:x\n", "x\n"],
    ["-free\n", "\n"],
    ["_free\n", "\n"],
    ["free\n", "free\n"],
    ["vendor/x:free\u2028", "vendor/x:free\u2028"],
    ["vendor/x:free\u2029", "vendor/x:free\u2029"],
    ["vendor/x:free\u0085", "vendor/x:free\u0085"],
    ["vendor/x:free\r", "vendor/x:free\r"],
    [" vendor/x:free ", " vendor/x:free "],
    ["x_free_\r\n", "x_free_\r\n"]
  ];

  const NATURAL_KEY_CONTROLS = [
    [["", "vendor/x"], ["", "vendor/x"]],
    [["vendor/x0", "vendor/x"], ["vendor/x", "vendor/x0"]],
    [["a1b10", "a1b02", "a1b2"], ["a1b2", "a1b02", "a1b10"]],
    [["vendor/model", " vendor/model "], [" vendor/model ", "vendor/model"]],
    [["vendor/item02", "vendor/item1"],
     ["vendor/item1", "vendor/item02"]]
  ];

  function sortedBy(ids, compare) {
    return ids.slice().sort(compare);
  }

  it("strips only a delimiter-bounded marker in one suffix and one prefix pass",
     () => {
       const rows = markerCases.marker_strip_rule;
       assert.equal(rows.length, 17, "the frozen marker table has 17 rows");
       assert.equal(new Set(rows.map((row) => row.raw_id)).size, 17);
       for (const row of rows) {
         assert.equal(catalog.stripFreeMarker(row.raw_id), row.stripped,
                      `marker rule mismatch for ${JSON.stringify(row.raw_id)}`);
       }
       for (const [rawId, want] of MARKER_FALLBACK_CONTROLS) {
         assert.equal(catalog.stripFreeMarker(rawId), want,
                      `fallback control for ${JSON.stringify(rawId)}`);
       }
       for (const [rawId, want] of MARKER_COMPAT_EDGES) {
         assert.equal(catalog.stripFreeMarker(rawId), want,
                      `compat edge for ${JSON.stringify(rawId)}`);
       }
       // One pass only, never recursive/global stripping.
       assert.equal(catalog.stripFreeMarker("free-free-X-free-free"),
                    "free-X-free");
       // Raw strings are returned verbatim, never normalised or retyped.
       assert.equal(catalog.stripFreeMarker("vendor/model-renamed"),
                    "vendor/model-renamed");
       assert.equal(catalog.stripFreeMarker("vendor/Ａ:free"), "vendor/Ａ");
     });

  it("builds the tagged natural key without rewriting the raw id", () => {
    // A digit run keeps its ORIGINAL text and digit count; text pieces keep
    // every intervening character and both empty boundary tokens.
    assert.deepEqual(catalog.naturalKey("vendor/item02"),
                     [["t", 0, "vendor/item"], ["d", 2, "02"], ["t", 0, ""]]);
    assert.deepEqual(catalog.naturalKey("10"),
                     [["t", 0, ""], ["d", 2, "10"], ["t", 0, ""]]);
    assert.deepEqual(catalog.naturalKey(""), [["t", 0, ""]]);
    assert.deepEqual(catalog.naturalKey("a1b2"),
                     [["t", 0, "a"], ["d", 1, "1"], ["t", 0, "b"],
                      ["d", 1, "2"], ["t", 0, ""]]);
    // Non-ASCII numeric-looking characters are TEXT, never digit runs.
    assert.deepEqual(catalog.naturalKey("١"), [["t", 0, "١"]]);
    assert.deepEqual(catalog.naturalKey("²"), [["t", 0, "²"]]);
    // A 64-digit run is never parsed as a number: length and text survive.
    const longDigits = "9".repeat(63) + "1";
    const longKey = catalog.naturalKey(`vendor/long${longDigits}`);
    assert.deepEqual(longKey[1], ["d", 64, longDigits]);
    // Fresh keys: two calls are equal but never the same arrays.
    const first = catalog.naturalKey("vendor/item1");
    const second = catalog.naturalKey("vendor/item1");
    assert.deepEqual(first, second);
    assert.notEqual(first, second);
    assert.notEqual(first[1], second[1]);
  });

  it("reproduces all seven literal ordering vectors forward and reversed",
     () => {
       const rows = orderingCases.cases;
       assert.equal(rows.length, 7, "the frozen vector table has 7 rows");
       assert.equal(new Set(rows.map((row) => row.id)).size, 7);
       const corpus = new Set();
       for (const row of rows) {
         const supplied = row.input.slice();
         const expected = row.expected.slice();
         assert.equal(supplied.length, expected.length, row.id);
         assert.notDeepEqual(supplied, expected, `${row.id}: a real sort`);
         const before = JSON.stringify(supplied);
         assert.deepEqual(sortedBy(supplied, catalog.compareNaturalIds),
                          expected, row.id);
         assert.equal(JSON.stringify(supplied), before, `${row.id}: no mutation`);
         assert.deepEqual(
           sortedBy(supplied.slice().reverse(), catalog.compareNaturalIds),
           expected, `${row.id}: reverse-supplied`);
         for (const id of supplied) {
           corpus.add(id);
         }
         for (const id of expected) {
           corpus.add(id);
         }
       }
       for (const [supplied, expected] of NATURAL_KEY_CONTROLS) {
         assert.deepEqual(sortedBy(supplied, catalog.compareNaturalIds),
                          expected, JSON.stringify(supplied));
         for (const id of supplied) {
           corpus.add(id);
         }
       }
       assert.ok(corpus.size >= 20, `corpus size ${corpus.size}`);
     });

  it("compares by digit length, code point and strict prefix", () => {
    const cmp = catalog.compareNaturalIds;
    // NO integer/float conversion and NO leading-zero stripping.
    assert.ok(cmp("vendor/item1", "vendor/item2") < 0);
    assert.ok(cmp("vendor/item2", "vendor/item02") < 0);
    assert.ok(cmp("vendor/item02", "vendor/item10") < 0);
    assert.ok(cmp("vendor/item10", "vendor/item000") < 0);
    assert.ok(cmp("vendor/x1", "vendor/x01") < 0);
    assert.ok(cmp("vendor/x01", "vendor/x001") < 0);
    // Equal-length long runs fall back to lexical ASCII digits.
    const tail = "long" + "9".repeat(63);
    assert.ok(cmp(`vendor/${tail}1`, `vendor/${tail}2`) < 0);
    assert.ok(cmp(`vendor/${tail}2`, `vendor/${tail}1`) > 0);
    // Adjacent run lengths: SHORTER digit count first, even when every digit
    // is the same character (numeric values would be equal here).
    assert.ok(cmp("vendor/long1", "vendor/long11") < 0);
    // Code-point text order, case-sensitive, no folding/locale/normalisation.
    assert.ok(cmp("vendor/B", "vendor/a") < 0);
    assert.ok(cmp("vendor/a", "vendor/Ｂ") < 0);
    assert.ok(cmp("vendor/Ａ", "vendor/Ｂ") < 0);
    assert.ok(cmp("vendor/Ａ", "vendor/😀") < 0);
    assert.ok(cmp("vendor/😀", "vendor/Ａ") > 0,
              "astral sorts after U+FF21, not by UTF-16 lead unit");
    assert.ok(cmp("Iy", "iy") < 0, "no case folding");
    // Digit-run text vs non-ASCII numeric-looking text.
    assert.ok(cmp("2", "١") < 0);
    assert.ok(cmp("10", "a") < 0);
    // Strict prefix sorts first; empty string is a real key value.
    assert.ok(cmp("vendor/x", "vendor/x0") < 0);
    assert.ok(cmp("", "vendor/x") < 0);
    // Surrounding space is never trimmed.
    assert.ok(cmp(" vendor/model ", "vendor/model") < 0);
    // Equality is exactly string identity.
    assert.equal(cmp("vendor/item1", "vendor/item1"), 0);
    assert.equal(cmp("", ""), 0);
    assert.ok(cmp("vendor/item1", "vendor/item01") < 0);
  });

  it("is a total order over the combined boundary corpus without mutation",
     () => {
       const corpus = [];
       for (const row of orderingCases.cases) {
         corpus.push(...row.input, ...row.expected);
       }
       for (const [supplied] of NATURAL_KEY_CONTROLS) {
         corpus.push(...supplied);
       }
       corpus.push("", "vendor/x", " vendor/model ", "vendor/model",
                   "vendor/😀", "vendor/Ａ", "vendor/a", "vendor/B",
                   "vendor/item١", "²", "١", "1", "01", "10", "a1b2",
                   "__proto__", "constructor", "toString", "hasOwnProperty",
                   "vendor/" + "9".repeat(64), "vendor/" + "9".repeat(65));
       const unique = Array.from(new Set(corpus));
       const before = JSON.stringify(unique);
       const cmp = catalog.compareNaturalIds;
       // Prototype-looking ids are plain strings, never object keys.
       assert.ok(cmp("__proto__", "constructor") < 0);
       assert.equal(sortedBy(["toString", "__proto__", "constructor"], cmp)
                      .length, 3);
       for (const left of unique) {
         assert.equal(cmp(left, left), 0, `${left} reflexive`);
         for (const right of unique) {
           const forward = Math.sign(cmp(left, right));
           const backward = Math.sign(cmp(right, left));
           assert.equal(forward, backward === 0 ? 0 : -backward,
                        `${left} ~ ${right} antisymmetry`);
           if (left !== right) {
             assert.notEqual(forward, 0, `${left} vs ${right} distinguishes`);
           }
         }
       }
       // Transitivity along the produced total order, and no comparator that
       // disagrees with itself when the corpus is sorted twice.
       const ordered = sortedBy(unique, cmp);
       assert.equal(JSON.stringify(unique), before, "input array untouched");
       for (let index = 1; index < ordered.length; index += 1) {
         assert.ok(cmp(ordered[index - 1], ordered[index]) < 0,
                   `${ordered[index - 1]} < ${ordered[index]}`);
       }
       assert.deepEqual(sortedBy(ordered.slice().reverse(), cmp), ordered);
     });

  it("orders endpoint pairs by supplied provider precedence then natural id",
     () => {
       const providerOrder = orderingSnapshot.provider_order;
       const physicalOrder = Object.keys(orderingSnapshot.providers);
       assert.deepEqual(providerOrder,
                        ["synthetic-b", "synthetic-a", "synthetic-c"]);
       assert.deepEqual(physicalOrder,
                        ["synthetic-a", "synthetic-b", "synthetic-c"]);
       assert.notDeepEqual(providerOrder, physicalOrder,
                           "precedence is not map insertion order");
       const golden = markerCases.cases
         .find((row) => row.id === "list_free_ordering_all")
         .expect.providers;
       assert.deepEqual(Object.keys(golden).sort(),
                        providerOrder.slice().sort());
       const expectedPairs = [];
       for (const provider of providerOrder) {
         for (const rawId of golden[provider]) {
           expectedPairs.push([provider, rawId]);
         }
       }
       assert.equal(expectedPairs.length, 10, "ten endpoints");
       assert.equal(new Set(expectedPairs.map((pair) => pair.join("\u0000")))
                      .size, 10);
       const physicalPairs = [];
       for (const provider of physicalOrder) {
         for (const rawId of orderingSnapshot.providers[provider].model_ids) {
           physicalPairs.push([provider, rawId]);
         }
       }
       assert.equal(physicalPairs.length, 10);
       assert.notDeepEqual(physicalPairs, expectedPairs,
                           "the fixture input is genuinely unsorted");
       const compare = (left, right) =>
         catalog.compareEndpointPairs(providerOrder, left, right);
       const snapshotBefore = JSON.stringify(orderingSnapshot);
       assert.deepEqual(sortedBy(physicalPairs, compare), expectedPairs);
       assert.deepEqual(sortedBy(physicalPairs.slice().reverse(), compare),
                        expectedPairs);
       assert.equal(JSON.stringify(orderingSnapshot), snapshotBefore,
                    "fixture document untouched");
       // Not alphabetic provider order: b...b, then a, then nothing from c.
       assert.deepEqual(Array.from(new Set(expectedPairs.map((p) => p[0]))),
                        ["synthetic-b", "synthetic-a"]);
       // The caller-supplied order is authoritative: reversing it reverses
       // provider precedence.
       const flipped = ["synthetic-c", "synthetic-a", "synthetic-b"];
       assert.ok(catalog.compareEndpointPairs(
         flipped, ["synthetic-a", "vendor/item2"],
         ["synthetic-b", "vendor/item2"]) < 0);
       assert.ok(catalog.compareEndpointPairs(
         providerOrder, ["synthetic-a", "vendor/item2"],
         ["synthetic-b", "vendor/item2"]) > 0);
       // A shared raw id stays TWO distinct pairs ordered by precedence.
       const shared = markerCases.cases
         .find((row) => row.id ===
                       "get_model_ordering_identical_raw_id_provider_precedence")
         .expect;
       assert.deepEqual(shared.exact_gateways, ["synthetic-b", "synthetic-a"]);
       const sharedPairs = [["synthetic-a", "vendor/item2"],
                            ["synthetic-b", "vendor/item2"]];
       const sortedShared = sortedBy(sharedPairs, compare);
       assert.deepEqual(sortedShared.map((pair) => pair[0]),
                        ["synthetic-b", "synthetic-a"]);
       assert.deepEqual(sortedShared.map((pair) => pair[0]),
                        shared.endpoints.map((endpoint) => endpoint.provider));
       assert.notEqual(compare(sortedShared[0], sortedShared[1]), 0);
       // Identical pairs compare equal; the zero-id provider still ranks.
       assert.equal(compare(["synthetic-b", "vendor/item1"],
                            ["synthetic-b", "vendor/item1"]), 0);
       assert.deepEqual(golden["synthetic-c"], []);
       assert.deepEqual(
         orderingSnapshot.providers["synthetic-c"].model_ids, []);
       assert.ok(compare(["synthetic-b", "vendor/item1"],
                         ["synthetic-c", "vendor/absent"]) < 0);
       assert.ok(compare(["synthetic-a", "vendor/item2"],
                         ["synthetic-c", "vendor/absent"]) < 0);
     });

  it("exposes endpointKey as index plus natural key under the same rules", () => {
    const providerOrder = orderingSnapshot.provider_order;
    const key = catalog.endpointKey(providerOrder, "synthetic-b",
                                    "vendor/item1");
    assert.deepEqual(key, [0, [["t", 0, "vendor/item"], ["d", 1, "1"],
                              ["t", 0, ""]]]);
    assert.deepEqual(catalog.endpointKey(providerOrder, "synthetic-c", "x"),
                     [2, [["t", 0, "x"]]]);
    // Same pair, same key value; keys are fresh data.
    const again = catalog.endpointKey(providerOrder, "synthetic-b",
                                      "vendor/item1");
    assert.deepEqual(again, key);
    assert.notEqual(again, key);
    assert.notEqual(again[1], key[1]);
    // The index is a plain array lookup, so a prototype-named provider is a
    // normal registered key rather than an object-member hazard.
    assert.deepEqual(catalog.endpointKey(["__proto__", "a"], "__proto__", "z"),
                     [0, [["t", 0, "z"]]]);
    // An unregistered provider is outside the documented precondition and is
    // rejected loudly instead of silently ranking at -1.
    assert.throws(() => catalog.endpointKey(providerOrder, "nope", "vendor/x"),
                  TypeError);
    assert.throws(() => catalog.compareEndpointPairs(
      providerOrder, ["nope", "vendor/x"], ["synthetic-a", "vendor/item2"]),
                  TypeError);
  });

  it("agrees with the unchanged Python reference helper", () => {
    // Test-owned parity check (NOT the later permanent 7g harness): the
    // committed literal fixtures above stay the correctness oracle; this run
    // only proves the port did not drift from `watchdog_catalog/query.py`.
    const ids = [];
    for (const row of markerCases.marker_strip_rule) {
      ids.push(row.raw_id);
    }
    for (const [rawId] of MARKER_FALLBACK_CONTROLS) {
      ids.push(rawId);
    }
    for (const [rawId] of MARKER_COMPAT_EDGES) {
      ids.push(rawId);
    }
    for (const row of orderingCases.cases) {
      ids.push(...row.input);
    }
    for (const [supplied] of NATURAL_KEY_CONTROLS) {
      ids.push(...supplied);
    }
    ids.push("", "vendor/😀", " vendor/model ", "vendor/item١", "²", "١",
             "__proto__", "vendor/" + "9".repeat(64),
             "vendor/" + "9".repeat(65));
    const uniqueIds = Array.from(new Set(ids));
    const providerOrder = orderingSnapshot.provider_order;
    const pairs = [];
    for (const provider of Object.keys(orderingSnapshot.providers)) {
      for (const rawId of orderingSnapshot.providers[provider].model_ids) {
        pairs.push([provider, rawId]);
      }
    }
    pairs.push(["synthetic-a", "vendor/item2"],
               ["synthetic-b", "vendor/item2"],
               ["synthetic-b", "__proto__"]);
    const result = runPythonReference({
      ids: uniqueIds, pairs: pairs, provider_order: providerOrder
    });
    assert.deepEqual(
      uniqueIds.map((rawId) => catalog.stripFreeMarker(rawId)),
      result.markers, "marker outputs agree with Python");
    assert.deepEqual(uniqueIds.map((rawId) => catalog.naturalKey(rawId)),
                     result.keys, "natural keys agree with Python");
    assert.deepEqual(sortedBy(uniqueIds, catalog.compareNaturalIds),
                     result.order, "natural order agrees with Python");
    assert.deepEqual(
      sortedBy(pairs, (left, right) =>
        catalog.compareEndpointPairs(providerOrder, left, right)),
      result.pairs, "endpoint order agrees with Python");
  });
});

// --------------------------------------------------------------------------
// pure list_free_models / list_endpoints queries
// --------------------------------------------------------------------------
//
// Every success payload is compared against the INDEPENDENTLY authored
// `cases.json` golden (deep semantic equality, object key order ignored, array
// order and scalar types retained) plus the exact frozen README field sets. The
// small Python comparison at the end of each group is a DRIFT probe against the
// unchanged reference helper -- agreement alone is never taken as proof: the
// committed literals remain the correctness oracle.

const QUERY_CASES = readFixtureDocument("cases.json").cases;
const QUERY_ARGUMENTS = readFixtureDocument("argument-cases.json");

const LIST_ALL_FIELDS = ["counts", "models", "n_gateways", "ok",
                         "provider_staleness", "providers", "snapshot_id",
                         "tick_epoch", "total_ids"];
const LIST_ONE_FIELDS = ["count", "model_ids", "models", "ok", "provider",
                         "provider_staleness", "snapshot_id", "tick_epoch"];
const ENDPOINT_FIELDS = ["counts", "gateways", "ok", "provider",
                         "provider_staleness", "snapshot_id", "tick_epoch"];
const GATEWAY_FIELDS = ["api_type", "chat_completions_url", "model_ids",
                        "models"];
const ENDPOINT_COUNT_FIELDS = ["distinct_raw_ids", "endpoints", "gateways",
                               "marker_group_labels", "models"];
const GET_MODEL_FIELDS = ["caveats", "counts", "endpoints", "exact_gateways",
                          "exact_matches", "ok", "provider_staleness", "query",
                          "snapshot_id", "tick_epoch"];
const GET_MODEL_ENDPOINT_FIELDS = ["api_type", "chat_completions_url",
                                   "metadata", "model_id", "provider", "stale"];
const GET_MODEL_COUNT_FIELDS = ["distinct_raw_ids", "endpoints",
                                "exact_matches", "marker_group_labels"];
// The frozen `watchdog_status` field sets and status-only warning order, copied
// from README's `Success envelope — watchdog_status` tables: the snapshot THREE
// codes followed by the two derived status codes.
const STATUS_FIELDS = ["cadence_s", "counts", "last_tick_age_s",
                       "metadata_coverage", "ok", "pending_alerts",
                       "provider_counts", "provider_staleness",
                       "site_publish_age_s", "site_published", "snapshot_id",
                       "tick_epoch", "tick_fresh", "warnings"];
const STATUS_COUNT_FIELDS = ["distinct_raw_ids", "endpoints", "gateways",
                             "marker_group_labels"];
const STATUS_COVERAGE_FIELDS = ["context_length_known", "description_known",
                                "endpoints", "input_modalities_known",
                                "output_modalities_known",
                                "supported_parameters_known"];
const STATUS_WARNING_ORDER = ["tick_epoch_unknown", "metadata_missing",
                              "metadata_invalid", "tick_epoch_future",
                              "tick_stale"];
// The frozen absence caveat literal, copied from README's `get_model` table.
const GET_MODEL_CAVEAT =
  "Cross-gateway ABSENCE is unreliable: gateways rename inconsistently, so a " +
  "missing exact id does NOT prove the model is unavailable there.";
const ARGUMENT_MESSAGES = {
  invalid_arguments: "Invalid tool arguments.",
  unknown_provider: "Unknown provider."
};

function queryRows(tool) {
  return QUERY_CASES.filter((row) => row.tool === tool);
}

function argumentRows(section, tool) {
  return QUERY_ARGUMENTS[section].filter((row) => row.tool === tool);
}

/** A validated snapshot decoded straight from a committed fixture document. */
function acceptedSnapshot(name) {
  return catalog.loadSnapshot(readFixtureText(name));
}

/** A typed deep copy through the module's own canonical text. */
function typedClone(value) {
  return catalog.loadJson(catalog.canonicalText(value));
}

/** Re-seal a test-built snapshot body and prove the validator accepts it. */
function resealSnapshot(body) {
  body.snapshot_id = catalog.snapshotHash(body);
  return catalog.validateSnapshot(body);
}

/** The legitimately EMPTY registry control, re-sealed from a valid fixture. */
function emptyRegistrySnapshot() {
  const body = typedClone(acceptedSnapshot("lookup.json"));
  body.provider_order = [];
  body.providers = {};
  return resealSnapshot(body);
}

/**
 * An explicit OWN data member. Assigning `object["__proto__"] = value` would
 * replace the prototype instead of creating a member, so snapshots that
 * legitimately contain prototype-looking KEYS are built this way -- never with
 * an object literal's special `__proto__` syntax.
 */
function withOwnKey(target, key, value) {
  Object.defineProperty(target, key, {
    value: value, enumerable: true, writable: true, configurable: true
  });
  return target;
}

/** A `{key: boolean}` map with `key` created as an own data member. */
function flagMap(entries) {
  const flags = {};
  for (const entry of entries) {
    withOwnKey(flags, entry[0], entry[1]);
  }
  return flags;
}

/** A structurally valid projected model object for the lookalike fixture. */
function lookalikeModel(rawId, name) {
  return {
    id: rawId,
    name: name,
    description: null,
    context_length: 0,
    architecture: {input_modalities: ["text"], output_modalities: ["text"]},
    supported_parameters: null,
    pricing: {prompt: "0", completion: "0"}
  };
}

/**
 * A WELL-FORMED snapshot whose provider keys and raw model ids are literally
 * `__proto__`, `constructor`, `toString` and `constructor-free`. The body is
 * re-sealed with the canonical digest and re-validated, so it is accepted input
 * rather than a hand-waved object.
 */
function lookalikeKeySnapshot() {
  const protoModels = {};
  withOwnKey(protoModels, "__proto__", lookalikeModel("__proto__", "Proto"));
  withOwnKey(protoModels, "constructor-free",
             lookalikeModel("constructor-free", "Constructor Group"));
  withOwnKey(protoModels, "shared-lookalike",
             lookalikeModel("shared-lookalike", "Shared Lookalike"));
  const ctorModels = {};
  withOwnKey(ctorModels, "toString", lookalikeModel("toString", "ToString"));
  const providers = {};
  withOwnKey(providers, "__proto__", {
    display_name: "Proto Gateway",
    chat_completions_url: "https://proto.example/v1/chat/completions",
    api_type: "openai",
    model_ids: ["__proto__", "constructor-free", "shared-lookalike"],
    models: protoModels,
    stale: true
  });
  withOwnKey(providers, "constructor", {
    display_name: "Constructor Gateway",
    chat_completions_url: "https://constructor.example/v1/chat/completions",
    api_type: "anthropic",
    model_ids: ["toString"],
    models: ctorModels,
    stale: false
  });
  return resealSnapshot({
    cadence_s: 300,
    provider_order: ["__proto__", "constructor"],
    providers: providers,
    schema_version: 1,
    snapshot_id: "0".repeat(64),
    tick_epoch: null,
    warnings: ["tick_epoch_unknown"]
  });
}

/** A validated snapshot re-sealed after one controlled body change. */
function resealedSnapshot(name, changes) {
  const body = typedClone(acceptedSnapshot(name));
  changes(body);
  return resealSnapshot(body);
}

/**
 * A WELL-FORMED one-provider snapshot whose raw ids pin the accepted marker
 * boundaries: a bare `free`, an all-marker `-free`, a PREFIX marker, a single
 * final LF, a CRLF neighbour and a `-freely` separator neighbour. The raw list
 * is in literal code-point order (as the validator requires) and its `models`
 * map is deliberately inserted in a DIFFERENT order, so an endpoint array can
 * only come from sorting `model_ids`, never from map iteration order.
 */
function markerBoundarySnapshot() {
  const ids = ["-free", "free", "free-vendor/z", "vendor/z",
               "vendor/z-free\n", "vendor/z-free\n\n",
               "vendor/z-free\r\n", "vendor/z-freely"];
  const models = {};
  for (const rawId of ids.slice().reverse()) {
    withOwnKey(models, rawId, lookalikeModel(rawId, `Marker ${rawId}`));
  }
  return resealSnapshot({
    cadence_s: 3600,
    provider_order: ["synthetic-z"],
    providers: {
      "synthetic-z": {
        display_name: "Synthetic Marker",
        chat_completions_url:
          "https://synthetic-marker.example/v1/chat/completions",
        api_type: "openai",
        model_ids: ids,
        models: models,
        stale: true
      }
    },
    schema_version: 1,
    snapshot_id: "0".repeat(64),
    tick_epoch: 1759200000,
    warnings: []
  });
}

/**
 * A validated snapshot whose matched endpoint carries a typed FLOAT pricing
 * token (`0.0`) instead of the string the shared fixtures use, re-sealed from
 * the module's own canonical text so the accepted typed-number path is real
 * accepted input and not a hand-waved object.
 */
function typedPricingSnapshot() {
  const body = typedClone(acceptedSnapshot("lookup.json"));
  body.providers["synthetic-a"].models["vendor/shared"].pricing.prompt =
    new catalog.JsonFloat(0);
  body.snapshot_id = catalog.snapshotHash(body);
  return catalog.loadSnapshot(catalog.canonicalText(body));
}

/** A validated snapshot with a FUTURE observation epoch and nothing else new. */
function futureTickSnapshot() {
  return resealedSnapshot("lookup.json", (body) => {
    body.tick_epoch = 4102444800;
  });
}

/** The frozen literal argument-error envelope, asserted field by field. */
function assertArgumentEnvelope(result, code, parameter, where) {
  assert.equal(result.ok, false, `${where}: ok is strictly false`);
  assert.deepEqual(Object.keys(result).sort(), ["error", "ok"],
                   `${where}: top-level fields are exactly ok + error`);
  assert.deepEqual(Object.keys(result.error).sort(),
                   ["code", "message", "parameter"],
                   `${where}: error fields are exactly code + message + parameter`);
  assert.equal(result.error.code, code, `${where}: code`);
  assert.equal(result.error.message, ARGUMENT_MESSAGES[code],
               `${where}: literal message`);
  assert.equal(result.error.parameter, parameter, `${where}: parameter`);
  for (const hostile of ["tool", "state_root", "path", "source", "cache",
                         "snapshot_id", "count", "gateways", "partial",
                         "staleness", "provider_staleness"]) {
    assert.equal(Object.prototype.hasOwnProperty.call(result, hostile), false,
                 `${where}: no ${hostile} field on an argument error`);
    assert.equal(Object.prototype.hasOwnProperty.call(result.error, hostile),
                 false, `${where}: no ${hostile} field inside error`);
  }
}

// Test-owned drift probe against the UNCHANGED reference helper. It proves the
// imported module really is this repository's copy, and compares the whole
// semantic payload as typed canonical JSON (object keys sorted, array order and
// scalar types preserved) -- never a re-derived "expected" value.
const QUERY_REFERENCE_SCRIPT = [
  "import json, sys",
  'sys.path.insert(0, ".")',
  "import watchdog_catalog.query as query_module",
  "from watchdog_catalog.catalog import load_snapshot",
  "payload = json.load(sys.stdin)",
  "handlers = {'list_free_models': query_module.list_free_models,",
  "            'list_endpoints': query_module.list_endpoints,",
  "            'get_model': query_module.get_model,",
  "            'watchdog_status': query_module.watchdog_status}",
  "results = []",
  "for case in payload['cases']:",
  "    snapshot = load_snapshot(case['snapshot_text'])",
  "    handler = handlers[case['tool']]",
  "    extra = {} if case.get('now') is None else {'now': case['now']}",
  "    results.append(json.dumps(handler(snapshot, case['args'], **extra),",
  "                              sort_keys=True, ensure_ascii=True,",
  "                              separators=(',', ':'), allow_nan=False))",
  "print(json.dumps({'origin': query_module.__file__, 'results': results}))"
].join("\n");

function runQueryReference(cases) {
  const { execFileSync } = require("node:child_process");
  const env = Object.assign({}, process.env);
  delete env.PYTHONPATH;
  env.PYTHONDONTWRITEBYTECODE = "1";
  const stdout = execFileSync(referencePython(),
                              ["-c", QUERY_REFERENCE_SCRIPT], {
    cwd: REPO_ROOT, env, input: JSON.stringify({cases: cases}),
    encoding: "utf8", maxBuffer: 32 * 1024 * 1024
  });
  const parsed = JSON.parse(stdout);
  assert.ok(fs.realpathSync(parsed.origin).startsWith(
              fs.realpathSync(REPO_ROOT)),
            `the reference module is this repository's copy: ${parsed.origin}`);
  assert.equal(parsed.results.length, cases.length,
               "one reference result per case");
  return parsed.results;
}

function assertQueryParity(tool, cases, javascriptResults) {
  const pythonResults = runQueryReference(cases.map((entry) => ({
    tool: tool, snapshot_text: entry.snapshot_text, args: entry.args,
    now: entry.now
  })));
  for (let index = 0; index < cases.length; index += 1) {
    assert.equal(catalog.canonicalText(javascriptResults[index]),
                 pythonResults[index],
                 `${cases[index].label}: typed canonical payload matches Python`);
  }
}

describe("pure list_free_models query", () => {
  it("reproduces every independent success golden exactly", () => {
    const rows = queryRows("list_free_models");
    // Row identity is asserted so a dropped or duplicated golden cannot shrink
    // the loop and still pass.
    assert.equal(rows.length, 5, rows.map((row) => row.id).join(","));
    assert.deepEqual(rows.map((row) => row.id).sort(), [
      "list_free_extension_provider_d", "list_free_lookup_all",
      "list_free_lookup_provider_a", "list_free_lookup_zero_provider_c",
      "list_free_ordering_all"
    ]);
    const fixtures = new Set();
    for (const row of rows) {
      const snapshot = acceptedSnapshot(row.fixture);
      const snapshotText = catalog.canonicalText(snapshot);
      const args = JSON.parse(JSON.stringify(row.args));
      const argsText = catalog.canonicalText(args);
      const result = catalog.listFreeModels(snapshot, args);
      // Deep semantic equality with the separately authored golden.
      assertSameValue(result, row.expect, row.id);
      assert.equal(result.ok, true, row.id);
      const allForm = Object.prototype.hasOwnProperty.call(row.expect,
                                                           "providers");
      assert.deepEqual(Object.keys(result).sort(),
                       allForm ? LIST_ALL_FIELDS : LIST_ONE_FIELDS, row.id);
      // Literal provenance and untouched inputs.
      assert.equal(result.snapshot_id, snapshot.snapshot_id, row.id);
      assert.equal(result.tick_epoch, snapshot.tick_epoch, row.id);
      assert.equal(catalog.canonicalText(snapshot), snapshotText,
                   `${row.id}: the snapshot is never mutated`);
      assert.equal(catalog.canonicalText(args), argsText,
                   `${row.id}: the argument object is never mutated`);
      fixtures.add(row.fixture);
      if (allForm) {
        assert.deepEqual(Object.keys(result.providers).sort(),
                         snapshot.provider_order.slice().sort(), row.id);
        for (const provider of snapshot.provider_order) {
          const source = snapshot.providers[provider];
          assert.ok(Number.isInteger(result.counts[provider]) &&
                    result.counts[provider] >= 0, row.id);
          assert.equal(result.counts[provider], source.model_ids.length,
                       row.id);
          assert.equal(result.provider_staleness[provider], source.stale,
                       row.id);
          assert.deepEqual(result.providers[provider].slice().sort(),
                           source.model_ids.slice().sort(), row.id);
          assert.deepEqual(Object.keys(result.models[provider]).sort(),
                           source.model_ids.slice().sort(), row.id);
          for (const rawId of source.model_ids) {
            // Complete projected metadata, copied verbatim: never reprojected
            // and never stringified/parsed away.
            assertSameValue(result.models[provider][rawId],
                            source.models[rawId],
                            `${row.id}:${provider}:${rawId}`);
          }
        }
      } else {
        const source = snapshot.providers[result.provider];
        assert.equal(result.count, result.model_ids.length, row.id);
        assert.ok(Number.isInteger(result.count) && result.count >= 0, row.id);
        assert.deepEqual(result.provider_staleness,
                         flagMap([[result.provider, source.stale]]), row.id);
        assert.deepEqual(result.model_ids.slice().sort(),
                         source.model_ids.slice().sort(), row.id);
        assert.deepEqual(Object.keys(result.models).sort(),
                         source.model_ids.slice().sort(), row.id);
        for (const rawId of source.model_ids) {
          assertSameValue(result.models[rawId], source.models[rawId],
                          `${row.id}:${rawId}`);
        }
      }
    }
    assert.deepEqual([...fixtures].sort(),
                     ["lookup.json", "ordering.json", "provider-extension.json"]);
  });

  it("keeps natural raw-ID order, per-provider metadata and zero-ID registries", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const ordering = acceptedSnapshot("ordering.json");
    const extension = acceptedSnapshot("provider-extension.json");

    // An absent provider and an explicit null are the SAME all-provider form.
    const omitted = catalog.listFreeModels(lookup, {});
    const nulled = catalog.listFreeModels(lookup, {provider: null});
    assertSameValue(omitted, nulled, "absent vs null provider");
    assert.equal(omitted.provider, undefined,
                 "the all-provider form carries no `provider` field");
    assert.equal(omitted.ok, true);

    // Natural order -- never the snapshot's frozen code-point order. The first
    // four ids are a compact literal restatement of the digit-run rule (run
    // length first, then the literal digit text), independent of the golden.
    const naturalB = catalog.listFreeModels(ordering, {}).providers[
      "synthetic-b"];
    assert.notDeepEqual(naturalB,
                        ordering.providers["synthetic-b"].model_ids,
                        "the array is re-sorted, not echoed in snapshot order");
    assert.deepEqual(naturalB.slice(0, 4), ["vendor/item1", "vendor/item2",
                                            "vendor/item02", "vendor/item10"]);
    assert.deepEqual(naturalB, queryRows("list_free_models").find(
      (row) => row.id === "list_free_ordering_all").expect.providers[
        "synthetic-b"]);

    // A raw ID shared by two providers keeps its own conflicting metadata.
    const sharedA = omitted.models["synthetic-a"]["vendor/shared"];
    const sharedB = omitted.models["synthetic-b"]["vendor/shared"];
    assert.notEqual(sharedA, sharedB, "shared ids are never aliased together");
    assert.equal(sharedA.context_length, 131072);
    assert.equal(sharedB.context_length, 32768);
    assert.deepEqual(sharedA.architecture.input_modalities, ["text"]);
    assert.deepEqual(sharedB.architecture.input_modalities,
                     ["text", "image"]);
    assert.deepEqual(sharedA.supported_parameters,
                     ["temperature", "max_tokens"]);
    assert.deepEqual(sharedB.supported_parameters, ["temperature", "top_p"]);

    // Scoped counters: distinct literal raw ids, gateways including zero-ID.
    assert.deepEqual(omitted.counts, {"synthetic-a": 3, "synthetic-b": 2,
                                      "synthetic-c": 0});
    assert.equal(omitted.total_ids, 4);
    assert.equal(omitted.n_gateways, 3);
    assert.deepEqual(omitted.provider_staleness,
                     {"synthetic-a": false, "synthetic-b": false,
                      "synthetic-c": false});
    assert.deepEqual(catalog.listFreeModels(ordering, {}).provider_staleness,
                     {"synthetic-b": true, "synthetic-a": false,
                      "synthetic-c": false});

    // A registered zero-ID provider is a SUCCESS with empty containers.
    const zero = catalog.listFreeModels(lookup, {provider: "synthetic-c"});
    assert.equal(zero.ok, true);
    assert.deepEqual(Object.keys(zero).sort(), LIST_ONE_FIELDS);
    assert.deepEqual(zero.model_ids, []);
    assert.deepEqual(zero.models, {});
    assert.equal(zero.count, 0);
    assert.deepEqual(zero.provider_staleness, {"synthetic-c": false});

    // The legitimately EMPTY registry: every container empty, no invented
    // gateway or count.
    const empty = catalog.listFreeModels(emptyRegistrySnapshot(), {});
    assert.equal(empty.ok, true);
    assert.deepEqual(Object.keys(empty).sort(), LIST_ALL_FIELDS);
    assert.deepEqual(empty.providers, {});
    assert.deepEqual(empty.models, {});
    assert.deepEqual(empty.counts, {});
    assert.deepEqual(empty.provider_staleness, {});
    assert.equal(empty.total_ids, 0);
    assert.equal(empty.n_gateways, 0);

    // Provider membership is driven by DATA alone: provider-extension.json adds
    // a gateway no package code names.
    const all = catalog.listFreeModels(extension, {});
    assert.equal(all.n_gateways, 4);
    assert.deepEqual(Object.keys(all.providers).sort(), ["synthetic-a",
      "synthetic-b", "synthetic-c", "synthetic-d"]);
    assert.deepEqual(all.counts["synthetic-d"], 1);
    assert.equal(all.total_ids, 4);
    const onlyD = catalog.listFreeModels(extension, {provider: "synthetic-d"});
    assert.equal(onlyD.provider, "synthetic-d");
    assert.deepEqual(onlyD.model_ids, ["vendor/shared"]);
    assert.deepEqual(onlyD.provider_staleness, {"synthetic-d": true});
    assert.equal(onlyD.models["vendor/shared"].context_length, 8192);
    assert.equal(onlyD.models["vendor/shared"].description,
                 "Synthetic delta endpoint with its own small context.");
  });

  it("reproduces the frozen argument boundary and its precedence", () => {
    const invalid = argumentRows("invalid_cases", "list_free_models");
    const valid = argumentRows("valid_controls", "list_free_models");
    assert.equal(invalid.length, 4, invalid.map((row) => row.id).join(","));
    assert.deepEqual(invalid.map((row) => row.id).sort(), [
      "provider_wrong_case", "provider_wrong_type",
      "unknown_arg_list_free_models", "unknown_provider_list_free_models"
    ]);
    assert.equal(valid.length, 3, valid.map((row) => row.id).join(","));

    for (const row of invalid) {
      const snapshot = acceptedSnapshot(row.fixture);
      const args = JSON.parse(JSON.stringify(row.args));
      const before = catalog.canonicalText(args);
      const result = catalog.listFreeModels(snapshot, args);
      assertSameValue(result, row.expect, row.id);
      assertArgumentEnvelope(result, row.expect.error.code,
                             row.expect.error.parameter, row.id);
      assert.equal(catalog.canonicalText(args), before,
                   `${row.id}: the argument object is never mutated`);
    }
    for (const row of valid) {
      const result = catalog.listFreeModels(acceptedSnapshot(row.fixture),
                                            JSON.parse(JSON.stringify(row.args)));
      assert.equal(result.ok, true, row.id);
    }

    const lookup = acceptedSnapshot("lookup.json");
    // A whole argument value that is not a JSON object is invalid_arguments /
    // null -- null, a list, a number, a string, a boolean and no value at all.
    for (const args of [null, [], 5, "x", false, 1.5, undefined]) {
      assertArgumentEnvelope(catalog.listFreeModels(lookup, args),
                             "invalid_arguments", null, String(args));
    }
    // A non-null provider of the wrong type is invalid_arguments/provider.
    for (const bad of [true, false, 5, 1.5, [], ["synthetic-a"], {},
                       {a: 1}]) {
      assertArgumentEnvelope(catalog.listFreeModels(lookup, {provider: bad}),
                             "invalid_arguments", "provider",
                             JSON.stringify(bad));
    }
    // Empty, padded and wrong-case keys are never repaired.
    for (const bad of ["", " synthetic-a ", "synthetic-a ", " synthetic-a",
                       "Synthetic-A", "SYNTHETIC-A", "synthetic-z", "unknown"]) {
      assertArgumentEnvelope(catalog.listFreeModels(lookup, {provider: bad}),
                             "unknown_provider", "provider", bad);
    }
    // Unknown-key precedence: ANY unknown key rejects with parameter null
    // BEFORE the provider type/membership check.
    for (const args of [
      JSON.parse('{"provider": 5, "bogus": 1}'),
      JSON.parse('{"provider": "synthetic-z", "bogus": 1}'),
      JSON.parse('{"bogus": 1}'),
      JSON.parse('{"provider": "synthetic-a", "bogus": 1}'),
      JSON.parse('{"provider": "synthetic-a", "limit": 5}'),
      JSON.parse('{"limit": 5, "offset": 0}'),
      // Prototype-looking unknown KEYS are ordinary own keys: rejected, never
      // treated as registered parameters and never reflected back.
      JSON.parse('{"__proto__": 1}'),
      JSON.parse('{"constructor": 1}'),
      JSON.parse('{"toString": 1}')
    ]) {
      assertArgumentEnvelope(catalog.listFreeModels(lookup, args),
                             "invalid_arguments", null,
                             JSON.stringify(args));
    }

    // The error envelope is a FRESH object on every call.
    const first = catalog.listFreeModels(lookup, {provider: "synthetic-z"});
    first.ok = true;
    first.error.code = "mutated";
    first.error.parameter = "provider";
    const second = catalog.listFreeModels(lookup, {provider: "synthetic-z"});
    assertArgumentEnvelope(second, "unknown_provider", "provider",
                           "fresh envelope");
    assert.notEqual(second, first);
  });

  it("returns fresh, deeply isolated payloads", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const before = catalog.canonicalText(lookup);

    const whole = catalog.listFreeModels(lookup, {});
    const again = catalog.listFreeModels(lookup, {});
    assert.equal(catalog.canonicalText(lookup), before,
                 "the snapshot is never mutated");
    assert.equal(catalog.canonicalText(whole), catalog.canonicalText(again),
                 "two calls agree");
    for (const container of ["providers", "models", "counts",
                             "provider_staleness"]) {
      assert.notEqual(whole[container], again[container], container);
      assert.notEqual(whole[container], lookup.providers[container],
                      container);
    }
    for (const provider of lookup.provider_order) {
      const source = lookup.providers[provider];
      assert.notEqual(whole.providers[provider], source.model_ids, provider);
      assert.notEqual(whole.models[provider], source.models, provider);
      for (const rawId of source.model_ids) {
        const got = whole.models[provider][rawId];
        const src = source.models[rawId];
        assert.notEqual(got, src, `${provider}:${rawId}`);
        assert.notEqual(got.architecture, src.architecture, rawId);
        assert.notEqual(got.supported_parameters, src.supported_parameters,
                        rawId);
        assert.notEqual(got.pricing, src.pricing, rawId);
        assert.notEqual(got.architecture.input_modalities,
                        src.architecture.input_modalities, rawId);
        assert.notEqual(again.models[provider][rawId], got, rawId);
      }
    }

    // Mutate the whole payload deeply; the snapshot, the earlier result and a
    // brand-new call must all stay unchanged.
    const shared = whole.models["synthetic-a"]["vendor/shared"];
    shared.name = "mutated";
    shared.architecture.input_modalities.push("vision");
    shared.supported_parameters.push("seed");
    shared.pricing.prompt = "999";
    shared.context_length = 1;
    whole.providers["synthetic-a"].push("injected");
    whole.counts["synthetic-a"] = 99;
    whole.provider_staleness["synthetic-a"] = true;
    delete whole.models["synthetic-a"]["vendor/model:free"];
    assert.equal(catalog.canonicalText(lookup), before,
                 "mutating a result never reaches the snapshot");
    const againShared = again.models["synthetic-a"]["vendor/shared"];
    assert.equal(againShared.name, "Vendor Shared");
    assert.deepEqual(againShared.architecture.input_modalities, ["text"]);
    assert.deepEqual(againShared.supported_parameters,
                     ["temperature", "max_tokens"]);
    assert.deepEqual(againShared.pricing,
                     {prompt: "0", completion: "0"});
    assert.equal(againShared.context_length, 131072);
    assert.deepEqual(again.providers["synthetic-a"],
                     ["vendor/model-renamed", "vendor/model:free",
                      "vendor/shared"]);
    const third = catalog.listFreeModels(lookup, {});
    assertSameValue(third, again, "a later call is unaffected by mutations");

    // The one-provider and registered zero-ID containers are fresh too.
    const single = catalog.listFreeModels(lookup, {provider: "synthetic-a"});
    const sourceA = lookup.providers["synthetic-a"];
    assert.notEqual(single.model_ids, sourceA.model_ids);
    assert.notEqual(single.models, sourceA.models);
    assert.notEqual(single.provider_staleness, lookup.providers);
    single.model_ids.push("injected");
    single.models["vendor/model:free"].architecture.input_modalities.push(
      "audio");
    single.provider_staleness["synthetic-a"] = true;
    assert.equal(catalog.canonicalText(lookup), before,
                 "a single-provider result is never a snapshot window");
    const zero = catalog.listFreeModels(lookup, {provider: "synthetic-c"});
    zero.model_ids.push("injected");
    zero.provider_staleness["synthetic-c"] = true;
    assert.equal(catalog.canonicalText(lookup), before);
    assert.notEqual(zero.model_ids,
                    catalog.listFreeModels(lookup, {provider: "synthetic-c"})
                      .model_ids);

    // Interleaved calls never share containers.
    const mixed = catalog.listFreeModels(lookup, {});
    catalog.listEndpoints(lookup, {});
    const mixedAgain = catalog.listFreeModels(lookup, {});
    assert.notEqual(mixed.models, mixedAgain.models);
    assert.equal(catalog.canonicalText(mixed),
                 catalog.canonicalText(mixedAgain));
  });

  it("survives prototype-looking provider and raw-ID keys", () => {
    const snapshot = lookalikeKeySnapshot();
    const frozen = catalog.canonicalText(snapshot);
    const whole = catalog.listFreeModels(snapshot, {});

    assert.deepEqual(Object.keys(whole.providers).sort(),
                     ["__proto__", "constructor"]);
    assert.deepEqual(Object.keys(whole.models).sort(),
                     ["__proto__", "constructor"]);
    assert.deepEqual(Object.keys(whole.counts).sort(),
                     ["__proto__", "constructor"]);
    // Exact own keys survive; inherited members are never mistaken for input.
    assert.equal(Object.prototype.hasOwnProperty.call(whole.providers,
                                                      "__proto__"), true);
    assert.deepEqual(whole.providers["__proto__"],
                     ["__proto__", "constructor-free", "shared-lookalike"]);
    assert.deepEqual(whole.providers["constructor"], ["toString"]);
    assert.deepEqual(whole.counts, flagMap([["__proto__", 3],
                                           ["constructor", 1]]));
    assert.equal(whole.total_ids, 4);
    assert.equal(whole.n_gateways, 2);
    assert.deepEqual(whole.provider_staleness,
                     flagMap([["__proto__", true], ["constructor", false]]));
    assert.equal(whole.tick_epoch, null,
                 "an unknown observation epoch is echoed as null");
    assert.equal(whole.snapshot_id, snapshot.snapshot_id);
    // No prototype pollution anywhere.
    assert.equal(Object.getPrototypeOf(whole.providers), Object.prototype);
    assert.equal(Object.getPrototypeOf(whole.models), Object.prototype);
    assert.equal({}.polluted, undefined);
    assert.equal(Object.prototype.polluted, undefined);
    assert.equal(typeof {}.constructor, "function",
                 "the shared prototype is untouched");
    assert.equal(whole.models["__proto__"]["__proto__"].name, "Proto");
    assert.equal(whole.models["constructor"]["toString"].name, "ToString");

    // An exact registered key is selectable, including a lookalike one.
    const selected = catalog.listFreeModels(snapshot, {provider: "__proto__"});
    assert.equal(selected.ok, true);
    assert.equal(selected.provider, "__proto__");
    assert.deepEqual(selected.model_ids,
                     ["__proto__", "constructor-free", "shared-lookalike"]);
    // `constructor` + `toString` are prototype MEMBERS of a normal object, so
    // an unregistered lookalike string is still an unknown provider.
    assertArgumentEnvelope(catalog.listFreeModels(snapshot,
                                                  {provider: "toString"}),
                           "unknown_provider", "provider", "inherited name");
    assert.equal(catalog.canonicalText(snapshot), frozen,
                 "the lookalike snapshot is never mutated");
  });

  it("publishes the same query functions to the browser namespace", () => {
    const vm = require("node:vm");
    const source = fs.readFileSync(
      path.join(__dirname, "..", "..", "web", "catalog-query.js"), "utf8");
    const sandbox = {};
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    const browserApi = sandbox.WatchdogCatalog;
    assert.equal(typeof browserApi.listFreeModels, "function");
    assert.equal(typeof browserApi.listEndpoints, "function");
    const snapshot = browserApi.loadSnapshot(readFixtureText("lookup.json"));
    const expectedAll = catalog.listFreeModels(acceptedSnapshot("lookup.json"),
                                               {});
    assert.equal(browserApi.canonicalText(browserApi.listFreeModels(snapshot,
                                                                    {})),
                 catalog.canonicalText(expectedAll),
                 "the inlined copy answers the bulk export identically");
    assert.equal(
      browserApi.canonicalText(browserApi.listEndpoints(
        snapshot, {provider: "synthetic-a"})),
      catalog.canonicalText(catalog.listEndpoints(acceptedSnapshot(
        "lookup.json"), {provider: "synthetic-a"})),
      "the inlined copy scopes endpoints identically");
  });

  it("agrees with the unchanged Python reference over the selected inputs", () => {
    const cases = queryRows("list_free_models").map((row) => ({
      label: row.id, snapshot_text: readFixtureText(row.fixture),
      args: JSON.parse(JSON.stringify(row.args))
    }));
    const lookupText = readFixtureText("lookup.json");
    for (const entry of [
      ["boundary-unknown-key", JSON.parse('{"provider": "synthetic-a", "bogus": 1}')],
      ["boundary-unknown-provider", {provider: "synthetic-z"}],
      ["boundary-wrong-type", {provider: 5}],
      ["boundary-null", {provider: null}],
      ["boundary-non-object", null],
      ["boundary-omitted", {}]
    ]) {
      cases.push({label: entry[0], snapshot_text: lookupText, args: entry[1]});
    }
    const lookalike = catalog.canonicalText(lookalikeKeySnapshot());
    cases.push({label: "lookalike-all", snapshot_text: lookalike, args: {}});
    cases.push({label: "lookalike-proto", snapshot_text: lookalike,
                args: {provider: "__proto__"}});
    cases.push({label: "empty-registry",
                snapshot_text: catalog.canonicalText(
                  emptyRegistrySnapshot()), args: {}});
    const results = cases.map((entry) => catalog.listFreeModels(
      catalog.loadSnapshot(entry.snapshot_text), entry.args));
    assertQueryParity("list_free_models", cases, results);
  });
});

describe("pure list_endpoints query", () => {
  it("reproduces every independent success golden exactly", () => {
    const rows = queryRows("list_endpoints");
    assert.equal(rows.length, 5, rows.map((row) => row.id).join(","));
    assert.deepEqual(rows.map((row) => row.id).sort(), [
      "endpoints_extension_all", "endpoints_lookup_all",
      "endpoints_lookup_provider_a", "endpoints_lookup_zero_provider_c",
      "endpoints_ordering_all"
    ]);
    for (const row of rows) {
      const snapshot = acceptedSnapshot(row.fixture);
      const snapshotText = catalog.canonicalText(snapshot);
      const args = JSON.parse(JSON.stringify(row.args));
      const argsText = catalog.canonicalText(args);
      const result = catalog.listEndpoints(snapshot, args);
      assertSameValue(result, row.expect, row.id);
      assert.equal(result.ok, true, row.id);
      // ONE exact top-level field set for both variants.
      assert.deepEqual(Object.keys(result).sort(), ENDPOINT_FIELDS, row.id);
      assert.deepEqual(Object.keys(result.counts).sort(),
                       ENDPOINT_COUNT_FIELDS, row.id);
      const selected = result.provider === null
        ? snapshot.provider_order : [result.provider];
      assert.deepEqual(Object.keys(result.gateways).sort(),
                       selected.slice().sort(), row.id);
      assert.deepEqual(Object.keys(result.provider_staleness).sort(),
                       selected.slice().sort(), row.id);
      assert.equal(result.snapshot_id, snapshot.snapshot_id, row.id);
      assert.equal(result.tick_epoch, snapshot.tick_epoch, row.id);
      for (const provider of selected) {
        const source = snapshot.providers[provider];
        const gateway = result.gateways[provider];
        assert.deepEqual(Object.keys(gateway).sort(), GATEWAY_FIELDS,
                         `${row.id}:${provider}`);
        assert.equal(gateway.chat_completions_url,
                     source.chat_completions_url, row.id);
        assert.equal(gateway.api_type, source.api_type, row.id);
        assert.deepEqual(gateway.model_ids.slice().sort(),
                         source.model_ids.slice().sort(), row.id);
        assert.deepEqual(Object.keys(gateway.models).sort(),
                         source.model_ids.slice().sort(), row.id);
        assert.equal(result.provider_staleness[provider], source.stale,
                     row.id);
        for (const rawId of source.model_ids) {
          assertSameValue(gateway.models[rawId], source.models[rawId],
                          `${row.id}:${provider}:${rawId}`);
        }
      }
      for (const field of ["gateways", "endpoints", "distinct_raw_ids",
                           "marker_group_labels", "models"]) {
        assert.ok(Number.isInteger(result.counts[field]) &&
                  result.counts[field] >= 0, `${row.id}: ${field}`);
      }
      assert.equal(catalog.canonicalText(snapshot), snapshotText,
                   `${row.id}: the snapshot is never mutated`);
      assert.equal(catalog.canonicalText(args), argsText,
                   `${row.id}: the argument object is never mutated`);
    }
  });

  it("scopes gateways, counts and the legacy alias to the selected providers", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const extension = acceptedSnapshot("provider-extension.json");

    const omitted = catalog.listEndpoints(lookup, {});
    const nulled = catalog.listEndpoints(lookup, {provider: null});
    assertSameValue(omitted, nulled, "absent vs null provider");
    assert.equal(omitted.provider, null,
                 "the all-provider form echoes a literal null");
    assert.deepEqual(Object.keys(omitted).sort(), ENDPOINT_FIELDS);
    // Labels are NOT endpoint pairs: 3 gateway labels over 5 endpoints.
    assert.deepEqual(omitted.counts, {gateways: 3, endpoints: 5,
                                      distinct_raw_ids: 4,
                                      marker_group_labels: 3, models: 3});
    assert.equal(omitted.counts.models,
                 omitted.counts.marker_group_labels,
                 "`models` is the documented legacy alias");
    assert.notEqual(omitted.counts.models, omitted.counts.endpoints);

    // Natural order, not the frozen snapshot array.
    const ordered = catalog.listEndpoints(acceptedSnapshot("ordering.json"),
                                          {});
    assert.notDeepEqual(ordered.gateways["synthetic-b"].model_ids,
                        acceptedSnapshot("ordering.json")
                          .providers["synthetic-b"].model_ids,
                        "endpoint ids are re-sorted naturally");
    assert.deepEqual(ordered.gateways["synthetic-b"].model_ids.slice(0, 4),
                     ["vendor/item1", "vendor/item2", "vendor/item02",
                      "vendor/item10"]);

    // A filtered response carries ONLY the selected provider's gateway,
    // staleness and counts -- no registry-wide total leaks in.
    const onlyA = catalog.listEndpoints(lookup, {provider: "synthetic-a"});
    assert.equal(onlyA.provider, "synthetic-a");
    assert.deepEqual(Object.keys(onlyA.gateways), ["synthetic-a"]);
    assert.deepEqual(Object.keys(onlyA.provider_staleness), ["synthetic-a"]);
    assert.deepEqual(onlyA.counts, {gateways: 1, endpoints: 3,
                                    distinct_raw_ids: 3,
                                    marker_group_labels: 3, models: 3});
    assert.equal(onlyA.gateways["synthetic-a"].chat_completions_url,
                 "https://synthetic-alpha.example/v1/chat/completions");
    assert.equal(onlyA.gateways["synthetic-a"].api_type, "openai");

    // The provider-extension control: 4 gateways, 6 endpoints, 4 distinct ids
    // but only 3 marker labels (both `model:free` and `model-free` strip to
    // the same label), and the alias agrees with the label count.
    const all = catalog.listEndpoints(extension, {});
    assert.deepEqual(all.counts, {gateways: 4, endpoints: 6,
                                  distinct_raw_ids: 4,
                                  marker_group_labels: 3, models: 3});
    assert.deepEqual(Object.keys(all.gateways).sort(),
                     ["synthetic-a", "synthetic-b", "synthetic-c",
                      "synthetic-d"]);
    assert.deepEqual(all.provider_staleness,
                     {"synthetic-d": true, "synthetic-a": false,
                      "synthetic-b": false, "synthetic-c": false});
    const onlyD = catalog.listEndpoints(extension, {provider: "synthetic-d"});
    assert.deepEqual(onlyD.counts, {gateways: 1, endpoints: 1,
                                    distinct_raw_ids: 1,
                                    marker_group_labels: 1, models: 1});
    assert.deepEqual(onlyD.gateways["synthetic-d"].model_ids,
                     ["vendor/shared"]);
    assert.equal(onlyD.gateways["synthetic-d"].models["vendor/shared"]
                   .context_length, 8192);

    // A registered zero-ID provider: gateways 1, every other count zero.
    const zero = catalog.listEndpoints(lookup, {provider: "synthetic-c"});
    assert.equal(zero.ok, true);
    assert.deepEqual(zero.counts, {gateways: 1, endpoints: 0,
                                   distinct_raw_ids: 0,
                                   marker_group_labels: 0, models: 0});
    assert.deepEqual(zero.gateways["synthetic-c"].model_ids, []);
    assert.deepEqual(zero.gateways["synthetic-c"].models, {});
    assert.deepEqual(zero.provider_staleness, {"synthetic-c": false});

    // The legitimately EMPTY registry: all counts zero, no gateway entries.
    const empty = catalog.listEndpoints(emptyRegistrySnapshot(), {});
    assert.equal(empty.ok, true);
    assert.deepEqual(Object.keys(empty).sort(), ENDPOINT_FIELDS);
    assert.equal(empty.provider, null);
    assert.deepEqual(empty.gateways, {});
    assert.deepEqual(empty.provider_staleness, {});
    assert.deepEqual(empty.counts, {gateways: 0, endpoints: 0,
                                    distinct_raw_ids: 0,
                                    marker_group_labels: 0, models: 0});
  });

  it("reproduces the frozen argument boundary and its precedence", () => {
    const invalid = argumentRows("invalid_cases", "list_endpoints");
    const valid = argumentRows("valid_controls", "list_endpoints");
    assert.equal(invalid.length, 3, invalid.map((row) => row.id).join(","));
    assert.deepEqual(invalid.map((row) => row.id).sort(), [
      "provider_empty", "unknown_arg_list_endpoints",
      "unknown_provider_list_endpoints"
    ]);
    assert.equal(valid.length, 2, valid.map((row) => row.id).join(","));

    for (const row of invalid) {
      const snapshot = acceptedSnapshot(row.fixture);
      const args = JSON.parse(JSON.stringify(row.args));
      const before = catalog.canonicalText(args);
      const result = catalog.listEndpoints(snapshot, args);
      assertSameValue(result, row.expect, row.id);
      assertArgumentEnvelope(result, row.expect.error.code,
                             row.expect.error.parameter, row.id);
      assert.equal(catalog.canonicalText(args), before,
                   `${row.id}: the argument object is never mutated`);
    }
    for (const row of valid) {
      const result = catalog.listEndpoints(acceptedSnapshot(row.fixture),
                                           JSON.parse(JSON.stringify(row.args)));
      assert.equal(result.ok, true, row.id);
    }

    const lookup = acceptedSnapshot("lookup.json");
    for (const args of [null, [], 5, "x", false, 1.5, undefined]) {
      assertArgumentEnvelope(catalog.listEndpoints(lookup, args),
                             "invalid_arguments", null, String(args));
    }
    for (const bad of [true, false, 5, 1.5, [], ["synthetic-a"], {},
                       {a: 1}]) {
      assertArgumentEnvelope(catalog.listEndpoints(lookup, {provider: bad}),
                             "invalid_arguments", "provider",
                             JSON.stringify(bad));
    }
    for (const bad of ["", " synthetic-a ", "synthetic-a ", " synthetic-a",
                       "Synthetic-A", "SYNTHETIC-A", "synthetic-z", "unknown"]) {
      assertArgumentEnvelope(catalog.listEndpoints(lookup, {provider: bad}),
                             "unknown_provider", "provider", bad);
    }
    // Unknown-key precedence, including `limit`/`offset` (NOT bulk truncation
    // controls) and prototype-looking unknown keys.
    for (const args of [
      JSON.parse('{"provider": 5, "limit": 5}'),
      JSON.parse('{"provider": "synthetic-z", "bogus": 1}'),
      JSON.parse('{"provider": null, "limit": 5}'),
      JSON.parse('{"provider": "synthetic-a", "limit": 5}'),
      JSON.parse('{"__proto__": 1}'),
      JSON.parse('{"constructor": 1}'),
      JSON.parse('{"toString": 1}')
    ]) {
      assertArgumentEnvelope(catalog.listEndpoints(lookup, args),
                             "invalid_arguments", null,
                             JSON.stringify(args));
    }
    const first = catalog.listEndpoints(lookup, {provider: "synthetic-z"});
    first.error.code = "mutated";
    const second = catalog.listEndpoints(lookup, {provider: "synthetic-z"});
    assertArgumentEnvelope(second, "unknown_provider", "provider",
                           "fresh envelope");
    assert.notEqual(second, first);
  });

  it("returns fresh, deeply isolated payloads", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const before = catalog.canonicalText(lookup);
    const whole = catalog.listEndpoints(lookup, {});
    const again = catalog.listEndpoints(lookup, {});
    assert.equal(catalog.canonicalText(lookup), before,
                 "the snapshot is never mutated");
    assert.equal(catalog.canonicalText(whole), catalog.canonicalText(again));
    for (const container of ["gateways", "counts", "provider_staleness"]) {
      assert.notEqual(whole[container], again[container], container);
      assert.notEqual(whole[container], lookup.providers[container],
                      container);
    }
    for (const provider of lookup.provider_order) {
      const source = lookup.providers[provider];
      const gateway = whole.gateways[provider];
      assert.notEqual(gateway, source, provider);
      assert.notEqual(gateway.model_ids, source.model_ids, provider);
      assert.notEqual(gateway.models, source.models, provider);
      assert.notEqual(again.gateways[provider], gateway, provider);
      for (const rawId of source.model_ids) {
        const got = gateway.models[rawId];
        const src = source.models[rawId];
        assert.notEqual(got, src, `${provider}:${rawId}`);
        assert.notEqual(got.architecture, src.architecture, rawId);
        assert.notEqual(got.supported_parameters, src.supported_parameters,
                        rawId);
        assert.notEqual(got.pricing, src.pricing, rawId);
        assert.notEqual(again.gateways[provider].models[rawId], got, rawId);
      }
    }

    const shared = whole.gateways["synthetic-a"].models["vendor/shared"];
    shared.name = "mutated";
    shared.architecture.input_modalities.push("vision");
    shared.supported_parameters.push("seed");
    shared.pricing.prompt = "999";
    whole.gateways["synthetic-a"].model_ids.push("injected");
    whole.gateways["synthetic-a"].chat_completions_url = "https://evil.example";
    whole.counts.endpoints = 99;
    whole.provider_staleness["synthetic-a"] = true;
    assert.equal(catalog.canonicalText(lookup), before,
                 "mutating a result never reaches the snapshot");
    const againShared = again.gateways["synthetic-a"].models["vendor/shared"];
    assert.equal(againShared.name, "Vendor Shared");
    assert.deepEqual(againShared.architecture.input_modalities, ["text"]);
    assert.deepEqual(againShared.supported_parameters,
                     ["temperature", "max_tokens"]);
    assert.deepEqual(againShared.pricing, {prompt: "0", completion: "0"});
    assert.deepEqual(again.gateways["synthetic-a"].model_ids,
                     ["vendor/model-renamed", "vendor/model:free",
                      "vendor/shared"]);
    assert.equal(again.gateways["synthetic-a"].chat_completions_url,
                 "https://synthetic-alpha.example/v1/chat/completions");
    const third = catalog.listEndpoints(lookup, {});
    assertSameValue(third, again, "a later call is unaffected by mutations");

    const single = catalog.listEndpoints(lookup, {provider: "synthetic-a"});
    const sourceA = lookup.providers["synthetic-a"];
    assert.notEqual(single.gateways["synthetic-a"].model_ids,
                    sourceA.model_ids);
    assert.notEqual(single.gateways["synthetic-a"].models, sourceA.models);
    single.gateways["synthetic-a"].model_ids.push("injected");
    single.gateways["synthetic-a"].models["vendor/model:free"]
      .architecture.input_modalities.push("audio");
    single.provider_staleness["synthetic-a"] = true;
    assert.equal(catalog.canonicalText(lookup), before);
    const zero = catalog.listEndpoints(lookup, {provider: "synthetic-c"});
    assert.notEqual(zero.gateways["synthetic-c"].model_ids,
                    catalog.listEndpoints(lookup, {provider: "synthetic-c"})
                      .gateways["synthetic-c"].model_ids);

    // Interleaved calls never share containers.
    const mixed = catalog.listEndpoints(lookup, {});
    catalog.listFreeModels(lookup, {});
    const mixedAgain = catalog.listEndpoints(lookup, {});
    assert.notEqual(mixed.gateways, mixedAgain.gateways);
    assert.equal(catalog.canonicalText(mixed),
                 catalog.canonicalText(mixedAgain));
  });

  it("survives prototype-looking provider and raw-ID keys", () => {
    const snapshot = lookalikeKeySnapshot();
    const frozen = catalog.canonicalText(snapshot);
    const whole = catalog.listEndpoints(snapshot, {});

    assert.deepEqual(Object.keys(whole.gateways).sort(),
                     ["__proto__", "constructor"]);
    assert.deepEqual(Object.keys(whole.provider_staleness).sort(),
                     ["__proto__", "constructor"]);
    assert.equal(Object.prototype.hasOwnProperty.call(whole.gateways,
                                                      "__proto__"), true);
    assert.equal(Object.getPrototypeOf(whole.gateways), Object.prototype);
    assert.equal(Object.getPrototypeOf(whole.counts), Object.prototype);
    assert.deepEqual(whole.gateways["__proto__"].model_ids,
                     ["__proto__", "constructor-free", "shared-lookalike"]);
    assert.deepEqual(whole.gateways["__proto__"].models["__proto__"].name,
                     "Proto");
    assert.deepEqual(whole.gateways["constructor"].model_ids, ["toString"]);
    // Labels count distinct STRINGS, never prototype member names conflated
    // with each other: `constructor-free` strips to `constructor`, which is a
    // real label here alongside the `__proto__` label.
    assert.deepEqual(whole.counts, {gateways: 2, endpoints: 4,
                                    distinct_raw_ids: 4,
                                    marker_group_labels: 4, models: 4});
    assert.deepEqual(whole.provider_staleness,
                     flagMap([["__proto__", true], ["constructor", false]]));
    assert.equal(whole.provider, null);
    assert.equal(whole.tick_epoch, null);
    assert.equal({}.polluted, undefined);
    assert.equal(Object.prototype.polluted, undefined);

    const selected = catalog.listEndpoints(snapshot, {provider: "__proto__"});
    assert.equal(selected.provider, "__proto__");
    assert.deepEqual(Object.keys(selected.gateways), ["__proto__"]);
    assert.deepEqual(selected.counts, {gateways: 1, endpoints: 3,
                                       distinct_raw_ids: 3,
                                       marker_group_labels: 3, models: 3});
    assertArgumentEnvelope(catalog.listEndpoints(snapshot,
                                                 {provider: "constructor "}),
                           "unknown_provider", "provider", "padded lookalike");
    assertArgumentEnvelope(catalog.listEndpoints(snapshot,
                                                 {provider: "valueOf"}),
                           "unknown_provider", "provider", "inherited name");
    assert.equal(catalog.canonicalText(snapshot), frozen,
                 "the lookalike snapshot is never mutated");
  });

  it("agrees with the unchanged Python reference over the selected inputs", () => {
    const cases = queryRows("list_endpoints").map((row) => ({
      label: row.id, snapshot_text: readFixtureText(row.fixture),
      args: JSON.parse(JSON.stringify(row.args))
    }));
    const lookupText = readFixtureText("lookup.json");
    for (const entry of [
      ["boundary-unknown-key", JSON.parse('{"provider": null, "limit": 5}')],
      ["boundary-unknown-provider", {provider: "synthetic-z"}],
      ["boundary-wrong-type", {provider: 5}],
      ["boundary-null", {provider: null}],
      ["boundary-omitted", {}],
      ["boundary-non-object", null],
      ["boundary-filtered", {provider: "synthetic-a"}]
    ]) {
      cases.push({label: entry[0], snapshot_text: lookupText, args: entry[1]});
    }
    const lookalike = catalog.canonicalText(lookalikeKeySnapshot());
    cases.push({label: "lookalike-all", snapshot_text: lookalike, args: {}});
    cases.push({label: "lookalike-proto", snapshot_text: lookalike,
                args: {provider: "__proto__"}});
    cases.push({label: "empty-registry",
                snapshot_text: catalog.canonicalText(
                  emptyRegistrySnapshot()), args: {}});
    const results = cases.map((entry) => catalog.listEndpoints(
      catalog.loadSnapshot(entry.snapshot_text), entry.args));
    assertQueryParity("list_endpoints", cases, results);
  });
});

// --------------------------------------------------------------------------
// pure get_model: literal membership + separate marker endpoints
// --------------------------------------------------------------------------
//
// `catalog.getModel(snapshot, args)` is the dependency-free counterpart of
// `watchdog_catalog.query.get_model`. It takes an already-VALIDATED public
// snapshot and the whole DECODED argument object, returns the frozen success
// payload or the shared argument-error envelope, never raises for a malformed
// argument and never mutates its inputs. Every expected payload below is either
// the independently authored `cases.json` / `argument-cases.json` golden or a
// hand-written literal from README's `get_model` contract table.
describe("pure get_model query", () => {
  it("reproduces every independent success golden exactly", () => {
    const rows = queryRows("get_model");
    assert.equal(rows.length, 13, rows.map((row) => row.id).join(","));
    assert.equal(new Set(rows.map((row) => row.id)).size, 13,
                 "every golden id is unique");
    assert.deepEqual(rows.map((row) => row.id).sort(), [
      "get_model_capability_known_empty_lists",
      "get_model_capability_unknown_all_null",
      "get_model_exact_id_alpha_with_marker_endpoints",
      "get_model_marker_base_no_exact",
      "get_model_metadata_known_context_inclusive_boundary",
      "get_model_metadata_unknown_context_null",
      "get_model_metadata_unknown_description_null",
      "get_model_metadata_zero_context_known_zero",
      "get_model_ordering_identical_raw_id_provider_precedence",
      "get_model_padded_query_not_trimmed",
      "get_model_shared_raw_id_two_endpoints",
      "get_model_unknown_id_empty_success",
      "get_model_unrelated_renamed_model_exact_only"
    ]);
    for (const row of rows) {
      // Per-row fixture override with the documented `lookup.json` default --
      // never a wrong-fixture fallback.
      const snapshot = acceptedSnapshot(row.fixture || "lookup.json");
      const snapshotText = catalog.canonicalText(snapshot);
      const args = JSON.parse(JSON.stringify(row.args));
      const argsText = catalog.canonicalText(args);
      const result = catalog.getModel(snapshot, args);
      assertSameValue(result, row.expect, row.id);
      assert.equal(result.ok, true, row.id);
      assert.deepEqual(Object.keys(result).sort(), GET_MODEL_FIELDS, row.id);
      // Strict scalar types and literal provenance.
      assert.equal(typeof result.query, "string", row.id);
      assert.equal(result.query, args.model_id, row.id);
      assert.equal(result.snapshot_id, snapshot.snapshot_id, row.id);
      assert.equal(result.tick_epoch, snapshot.tick_epoch, row.id);
      // exact_matches / provider_staleness carry ONE entry per provider_order
      // provider -- the registered zero-ID provider included.
      assert.deepEqual(Object.keys(result.exact_matches).sort(),
                       snapshot.provider_order.slice().sort(), row.id);
      assert.deepEqual(Object.keys(result.provider_staleness).sort(),
                       snapshot.provider_order.slice().sort(), row.id);
      for (const provider of snapshot.provider_order) {
        assert.equal(typeof result.exact_matches[provider], "boolean", row.id);
        assert.equal(result.provider_staleness[provider],
                     snapshot.providers[provider].stale, row.id);
      }
      assert.deepEqual(
        result.exact_gateways,
        snapshot.provider_order.filter(
          (provider) => result.exact_matches[provider]),
        row.id);
      for (const endpoint of result.endpoints) {
        assert.deepEqual(Object.keys(endpoint).sort(),
                         GET_MODEL_ENDPOINT_FIELDS, row.id);
        assert.equal(typeof endpoint.stale, "boolean", row.id);
        assert.equal(endpoint.stale,
                     result.provider_staleness[endpoint.provider], row.id);
        const source = snapshot.providers[endpoint.provider];
        assert.equal(endpoint.chat_completions_url,
                     source.chat_completions_url, row.id);
        assert.equal(endpoint.api_type, source.api_type, row.id);
        // Complete projected public model object of ITS OWN pair.
        assertSameValue(endpoint.metadata, source.models[endpoint.model_id],
                        `${row.id}:${endpoint.model_id}`);
      }
      assert.deepEqual(Object.keys(result.counts).sort(),
                       GET_MODEL_COUNT_FIELDS, row.id);
      for (const field of GET_MODEL_COUNT_FIELDS) {
        assert.ok(Number.isInteger(result.counts[field]) &&
                  result.counts[field] >= 0,
                  `${row.id}: ${field} is a non-negative integer`);
      }
      // Count identities -- structural restatements, the golden literal above
      // remains the correctness oracle.
      assert.equal(result.counts.exact_matches, result.exact_gateways.length,
                   row.id);
      assert.equal(result.counts.endpoints, result.endpoints.length, row.id);
      assert.equal(result.counts.distinct_raw_ids,
                   new Set(result.endpoints.map((e) => e.model_id)).size,
                   row.id);
      assert.equal(
        result.counts.marker_group_labels,
        new Set(result.endpoints.map(
          (e) => catalog.stripFreeMarker(e.model_id))).size,
        row.id);
      assert.deepEqual(result.caveats, [GET_MODEL_CAVEAT], row.id);
      assert.equal(catalog.canonicalText(snapshot), snapshotText,
                   `${row.id}: the snapshot is never mutated`);
      assert.equal(catalog.canonicalText(args), argsText,
                   `${row.id}: the argument object is never mutated`);
    }
  });

  it("keeps literal membership separate from the marker-insensitive endpoints",
     () => {
    const lookup = acceptedSnapshot("lookup.json");
    const ordering = acceptedSnapshot("ordering.json");

    // Exact membership is LITERAL; the endpoint lookup is a separate marker
    // comparison. The bare label matches two endpoints and no gateway.
    const base = catalog.getModel(lookup, {model_id: "vendor/model"});
    assert.deepEqual(base.exact_matches,
                     {"synthetic-a": false, "synthetic-b": false,
                      "synthetic-c": false});
    assert.deepEqual(base.exact_gateways, []);
    assert.deepEqual(base.endpoints.map((e) => [e.provider, e.model_id]),
                     [["synthetic-a", "vendor/model:free"],
                      ["synthetic-b", "vendor/model-free"]]);
    assert.deepEqual(base.counts, {exact_matches: 0, endpoints: 2,
                                   distinct_raw_ids: 2,
                                   marker_group_labels: 1});

    // An EXACT id is not the marker lookup: it matches only its own gateway,
    // while the marker endpoints still return both spellings.
    const exact = catalog.getModel(lookup, {model_id: "vendor/model:free"});
    assert.deepEqual(exact.exact_matches,
                     {"synthetic-a": true, "synthetic-b": false,
                      "synthetic-c": false});
    assert.deepEqual(exact.exact_gateways, ["synthetic-a"]);
    assert.deepEqual(exact.endpoints.map((e) => e.model_id),
                     ["vendor/model:free", "vendor/model-free"]);
    assert.deepEqual(exact.counts, {exact_matches: 1, endpoints: 2,
                                    distinct_raw_ids: 2,
                                    marker_group_labels: 1});
    assert.notEqual(exact.counts.endpoints, exact.counts.exact_matches);

    // The other spelling is the mirror image: exact membership never uses the
    // marker label, and the unrelated renamed id is EXACT only.
    const other = catalog.getModel(lookup, {model_id: "vendor/model-free"});
    assert.deepEqual(other.exact_gateways, ["synthetic-b"]);
    assert.deepEqual(other.endpoints.map((e) => e.model_id),
                     ["vendor/model:free", "vendor/model-free"]);
    const renamed = catalog.getModel(lookup,
                                     {model_id: "vendor/model-renamed"});
    assert.deepEqual(renamed.exact_gateways, ["synthetic-a"]);
    assert.deepEqual(renamed.endpoints.map((e) => [e.provider, e.model_id]),
                     [["synthetic-a", "vendor/model-renamed"]]);
    assert.deepEqual(renamed.counts, {exact_matches: 1, endpoints: 1,
                                      distinct_raw_ids: 1,
                                      marker_group_labels: 1});

    // One raw id on two gateways is two endpoints but ONE distinct raw id, and
    // result-scoped counts never report the registry-wide totals (the whole
    // fixture has 5 endpoint pairs, 4 distinct ids and 3 labels).
    const shared = catalog.getModel(lookup, {model_id: "vendor/shared"});
    assert.deepEqual(shared.endpoints.map((e) => [e.provider, e.model_id]),
                     [["synthetic-a", "vendor/shared"],
                      ["synthetic-b", "vendor/shared"]]);
    assert.deepEqual(shared.counts, {exact_matches: 2, endpoints: 2,
                                     distinct_raw_ids: 1,
                                     marker_group_labels: 1});

    // The padded and case-changed queries are literal: no trimming, no case
    // repair, and therefore no match at all.
    for (const query of [" vendor/shared", "vendor/shared ",
                         "VENDOR/SHARED", "Vendor/Shared"]) {
      const result = catalog.getModel(lookup, {model_id: query});
      assert.equal(result.ok, true, query);
      assert.equal(result.query, query, query);
      assert.deepEqual(result.exact_gateways, [], query);
      assert.deepEqual(result.endpoints, [], query);
      assert.deepEqual(result.counts, {exact_matches: 0, endpoints: 0,
                                       distinct_raw_ids: 0,
                                       marker_group_labels: 0}, query);
      assert.deepEqual(result.caveats, [GET_MODEL_CAVEAT], query);
    }

    // Declared display order beats alphabetic and physical provider-map order:
    // ordering.json declares b, a, c and its providers map is written a, b, c.
    const ordered = catalog.getModel(ordering, {model_id: "vendor/item2"});
    assert.deepEqual(ordered.exact_matches,
                     {"synthetic-b": true, "synthetic-a": true,
                      "synthetic-c": false});
    assert.deepEqual(ordered.exact_gateways, ["synthetic-b", "synthetic-a"]);
    assert.deepEqual(ordered.endpoints.map((e) => [e.provider, e.model_id]),
                     [["synthetic-b", "vendor/item2"],
                      ["synthetic-a", "vendor/item2"]]);
    assert.deepEqual(ordered.provider_staleness,
                     {"synthetic-b": true, "synthetic-a": false,
                      "synthetic-c": false});
    // The shared pair keeps its OWN metadata: same raw id, different gateways.
    assert.equal(ordered.endpoints[0].metadata.name, "Order B Item Two");
    assert.equal(ordered.endpoints[1].metadata.name, "Order A Item Two");
    assert.equal(ordered.endpoints[0].metadata.context_length, 131072);
    assert.equal(ordered.endpoints[1].metadata.context_length, 65536);
    assert.notEqual(ordered.endpoints[0].metadata,
                    ordered.endpoints[1].metadata);

    // A huge numeric ID run and non-ASCII ids survive exact membership and
    // arrive unchanged in the endpoint list.
    const longId = ordering.providers["synthetic-b"].model_ids.find(
      (rawId) => rawId.startsWith("vendor/long"));
    assert.ok(longId && longId.length > 60, longId);
    for (const query of [longId, "vendor/item\u0661", "vendor/\uff21",
                         "vendor/\ud83d\ude00"]) {
      const result = catalog.getModel(ordering, {model_id: query});
      assert.equal(result.ok, true, query);
      assert.deepEqual(result.exact_gateways, ["synthetic-b"], query);
      assert.deepEqual(result.endpoints.map((e) => e.model_id), [query], query);
      assert.deepEqual(result.counts, {exact_matches: 1, endpoints: 1,
                                       distinct_raw_ids: 1,
                                       marker_group_labels: 1}, query);
      assert.equal(result.snapshot_id, ordering.snapshot_id, query);
      assert.equal(result.tick_epoch, ordering.tick_epoch, query);
    }

    // A registered zero-ID provider still gets its exact_matches and staleness
    // entries, and a no-match success is a success.
    const missing = catalog.getModel(lookup,
                                     {model_id: "vendor/does-not-exist"});
    assert.equal(missing.ok, true);
    assert.deepEqual(missing.exact_matches,
                     {"synthetic-a": false, "synthetic-b": false,
                      "synthetic-c": false});
    assert.deepEqual(missing.exact_gateways, []);
    assert.deepEqual(missing.endpoints, []);
    assert.deepEqual(missing.provider_staleness,
                     {"synthetic-a": false, "synthetic-b": false,
                      "synthetic-c": false});
    assert.deepEqual(missing.counts, {exact_matches: 0, endpoints: 0,
                                      distinct_raw_ids: 0,
                                      marker_group_labels: 0});
    assert.deepEqual(missing.caveats, [GET_MODEL_CAVEAT]);

    // The legitimately EMPTY registry: every map empty, every count zero.
    const empty = catalog.getModel(emptyRegistrySnapshot(),
                                   {model_id: "vendor/shared"});
    assert.equal(empty.ok, true);
    assert.deepEqual(Object.keys(empty).sort(), GET_MODEL_FIELDS);
    assert.deepEqual(empty.exact_matches, {});
    assert.deepEqual(empty.exact_gateways, []);
    assert.deepEqual(empty.provider_staleness, {});
    assert.deepEqual(empty.endpoints, []);
    assert.deepEqual(empty.counts, {exact_matches: 0, endpoints: 0,
                                    distinct_raw_ids: 0,
                                    marker_group_labels: 0});
    assert.deepEqual(empty.caveats, [GET_MODEL_CAVEAT]);

    // Observation provenance is echoed, never derived: an unknown epoch stays
    // null and a future epoch is not "fresh" or clamped.
    const unknownTick = catalog.getModel(
      acceptedSnapshot("status-unknown-time.json"),
      {model_id: "vendor/unknown-capabilities"});
    assert.equal(unknownTick.ok, true);
    assert.equal(unknownTick.tick_epoch, null);
    assert.deepEqual(unknownTick.exact_gateways, ["synthetic-a"]);
    assert.deepEqual(unknownTick.provider_staleness,
                     {"synthetic-a": false, "synthetic-b": true});
    assert.deepEqual(Object.keys(unknownTick.exact_matches).sort(),
                     ["synthetic-a", "synthetic-b"]);
    const futureTick = catalog.getModel(futureTickSnapshot(),
                                        {model_id: "vendor/shared"});
    assert.equal(futureTick.tick_epoch, 4102444800);
    assert.equal(futureTick.snapshot_id,
                 futureTickSnapshot().snapshot_id,
                 "the same body content is the same snapshot");
  });

  it("pins the accepted marker boundaries and their natural endpoint order",
     () => {
    const snapshot = markerBoundarySnapshot();
    const frozen = catalog.canonicalText(snapshot);

    // One suffix pass then one prefix pass, with the reference's own `$`
    // semantics: a bare `free` is not a marker, an all-marker `-free` falls
    // back to itself, the prefix marker strips, and a `-freely` neighbour is
    // not a delimiter-bounded marker.
    const base = catalog.getModel(snapshot, {model_id: "vendor/z"});
    assert.deepEqual(base.exact_gateways, ["synthetic-z"]);
    assert.deepEqual(base.endpoints.map((e) => e.model_id),
                     ["free-vendor/z", "vendor/z"]);
    assert.deepEqual(base.counts, {exact_matches: 1, endpoints: 2,
                                   distinct_raw_ids: 2,
                                   marker_group_labels: 1});
    const bare = catalog.getModel(snapshot, {model_id: "free"});
    assert.deepEqual(bare.endpoints.map((e) => e.model_id), ["free"]);
    assert.deepEqual(bare.counts, {exact_matches: 1, endpoints: 1,
                                   distinct_raw_ids: 1,
                                   marker_group_labels: 1});
    const allMarker = catalog.getModel(snapshot, {model_id: "-free"});
    assert.deepEqual(allMarker.endpoints.map((e) => e.model_id), ["-free"]);

    // Python's `$` also matches before ONE final LF, so a `-free\n` id strips
    // the marker and KEEPS the LF in the label; a DOUBLE LF or a CRLF pair is
    // not a single trailing LF, so those ids keep their `free` segment. The
    // query must carry the same label, never the trimmed id.
    assert.equal(catalog.stripFreeMarker("vendor/z-free\n"), "vendor/z\n");
    assert.equal(catalog.stripFreeMarker("vendor/z-free\n\n"),
                 "vendor/z-free\n\n");
    assert.equal(catalog.stripFreeMarker("vendor/z-free\r\n"),
                 "vendor/z-free\r\n");
    const lf = catalog.getModel(snapshot, {model_id: "vendor/z\n"});
    assert.deepEqual(lf.exact_matches, {"synthetic-z": false});
    assert.deepEqual(lf.endpoints.map((e) => e.model_id),
                     ["vendor/z-free\n"]);
    assert.deepEqual(lf.counts, {exact_matches: 0, endpoints: 1,
                                 distinct_raw_ids: 1,
                                 marker_group_labels: 1});
    const doubleLf = catalog.getModel(snapshot,
                                      {model_id: "vendor/z-free\n\n"});
    assert.deepEqual(doubleLf.exact_gateways, ["synthetic-z"]);
    assert.deepEqual(doubleLf.endpoints.map((e) => e.model_id),
                     ["vendor/z-free\n\n"]);
    const bareDoubleLf = catalog.getModel(snapshot, {model_id: "vendor/z\n\n"});
    assert.deepEqual(bareDoubleLf.endpoints, []);
    const crlf = catalog.getModel(snapshot, {model_id: "vendor/z-free\r\n"});
    assert.deepEqual(crlf.exact_gateways, ["synthetic-z"]);
    assert.deepEqual(crlf.endpoints.map((e) => e.model_id),
                     ["vendor/z-free\r\n"]);
    const neighbour = catalog.getModel(snapshot, {model_id: "vendor/z-freely"});
    assert.deepEqual(neighbour.endpoints.map((e) => e.model_id),
                     ["vendor/z-freely"]);

    // Endpoint order is the accepted natural raw-ID order, NOT the order the
    // `models` map happens to be inserted in (it is reversed here).
    const mapOrder = Object.keys(snapshot.providers["synthetic-z"].models);
    assert.deepEqual(mapOrder,
                     snapshot.providers["synthetic-z"].model_ids.slice()
                       .reverse(),
                     "the models map is inserted in reverse raw-ID order");
    assert.notDeepEqual(
      base.endpoints.map((e) => e.model_id),
      mapOrder.filter((rawId) => base.endpoints.some(
        (e) => e.model_id === rawId)),
      "endpoints follow the natural raw-ID order, never map order");
    assert.equal(catalog.canonicalText(snapshot), frozen,
                 "the marker snapshot is never mutated");
  });

  it("reproduces the frozen required model_id argument boundary", () => {
    const invalid = argumentRows("invalid_cases", "get_model");
    const valid = argumentRows("valid_controls", "get_model");
    assert.equal(invalid.length, 8, invalid.map((row) => row.id).join(","));
    assert.equal(new Set(invalid.map((row) => row.id)).size, 8);
    assert.deepEqual(invalid.map((row) => row.id).sort(), [
      "args_number_not_object", "model_id_ascii_blank", "model_id_empty",
      "model_id_missing", "model_id_null", "model_id_number",
      "precedence_unknown_key_before_required", "unknown_arg_get_model"
    ]);
    assert.equal(valid.length, 2, valid.map((row) => row.id).join(","));

    for (const row of invalid) {
      const snapshot = acceptedSnapshot(row.fixture);
      const args = JSON.parse(JSON.stringify(row.args));
      const before = catalog.canonicalText(args);
      const result = catalog.getModel(snapshot, args);
      assertSameValue(result, row.expect, row.id);
      assertArgumentEnvelope(result, row.expect.error.code,
                             row.expect.error.parameter, row.id);
      assert.equal(catalog.canonicalText(args), before,
                   `${row.id}: the argument object is never mutated`);
    }
    // Acceptance-only controls: the complete oracles are the thirteen success
    // goldens above, so this pins acceptance and the retained literal query.
    for (const row of valid) {
      const result = catalog.getModel(acceptedSnapshot(row.fixture),
                                      JSON.parse(JSON.stringify(row.args)));
      assert.equal(result.ok, true, row.id);
      assert.equal(result.query, row.args.model_id, row.id);
    }

    const lookup = acceptedSnapshot("lookup.json");

    // A whole argument value that is not a JSON object: parameter is null.
    for (const args of [null, [], 5, "x", false, true, 1.5, undefined]) {
      assertArgumentEnvelope(catalog.getModel(lookup, args),
                             "invalid_arguments", null, String(args));
    }
    // A missing or wrong-typed required model_id -- never coerced.
    for (const bad of [null, true, false, 5, 1.5, [], ["vendor/shared"], {},
                       {a: 1}, new String("vendor/shared")]) {
      assertArgumentEnvelope(catalog.getModel(lookup, {model_id: bad}),
                             "invalid_arguments", "model_id",
                             JSON.stringify(String(bad)));
    }
    assertArgumentEnvelope(catalog.getModel(lookup, {model_id: undefined}),
                           "invalid_arguments", "model_id", "undefined");
    // Empty and ASCII-blank-only strings: every member of the explicit
    // U+0009..U+000D + U+0020 set, alone and combined.
    for (const blank of ["", " ", "\t", "\n", "\r", "\v", "\f",
                         " \t\n\r\v\f", "\t \n", "  ", "\f\v"]) {
      assertArgumentEnvelope(catalog.getModel(lookup, {model_id: blank}),
                             "invalid_arguments", "model_id",
                             JSON.stringify(blank));
    }
    // The ASCII blank set is never widened: non-ASCII blank-LOOKING code points
    // are literal nonblank queries -- accepted, no match, zero counts, caveat.
    for (const query of ["\u00a0", "\u2003", "\u2028", "\u2029", "\u3000",
                         "\u001c", "\u0000", "\ufeff", "\u00a0\u2003"]) {
      const result = catalog.getModel(lookup, {model_id: query});
      assert.equal(result.ok, true, JSON.stringify(query));
      assert.equal(result.query, query, JSON.stringify(query));
      assert.deepEqual(result.exact_gateways, [], JSON.stringify(query));
      assert.deepEqual(result.endpoints, [], JSON.stringify(query));
      assert.deepEqual(result.counts, {exact_matches: 0, endpoints: 0,
                                       distinct_raw_ids: 0,
                                       marker_group_labels: 0},
                       JSON.stringify(query));
      assert.deepEqual(result.caveats, [GET_MODEL_CAVEAT],
                       JSON.stringify(query));
    }
    // Unknown-key precedence: ANY unknown key rejects with parameter null
    // BEFORE the required model_id check. `provider`, `limit`, `offset` and
    // `query` are unknown here, not selection/truncation aliases.
    for (const args of [
      JSON.parse("{\"bogus\":1}"),
      JSON.parse("{\"model_id\":5,\"bogus\":1}"),
      JSON.parse("{\"model_id\":\"\",\"bogus\":1}"),
      JSON.parse("{\"provider\":\"synthetic-a\"}"),
      JSON.parse("{\"model_id\":\"vendor/shared\",\"provider\":\"synthetic-a\"}"),
      JSON.parse("{\"model_id\":\"vendor/shared\",\"limit\":5}"),
      JSON.parse("{\"model_id\":\"vendor/shared\",\"offset\":0}"),
      JSON.parse("{\"model_id\":\"vendor/shared\",\"query\":\"x\"}"),
      JSON.parse("{\"__proto__\":1}"),
      JSON.parse("{\"constructor\":1}"),
      JSON.parse("{\"toString\":1}"),
      JSON.parse("{\"model_id\":\"vendor/shared\",\"__proto__\":1}")
    ]) {
      assertArgumentEnvelope(catalog.getModel(lookup, args),
                             "invalid_arguments", null,
                             JSON.stringify(args));
    }
    // The retained identifier is never trimmed, folded or repaired.
    for (const query of [" vendor/shared ", "VENDOR/SHARED", "Vendor/Shared"]) {
      const result = catalog.getModel(lookup, {model_id: query});
      assert.equal(result.ok, true, query);
      assert.equal(result.query, query, query);
    }
    // A fresh, non-reflecting envelope on every call.
    const first = catalog.getModel(lookup, {model_id: ""});
    first.error.code = "mutated";
    first.ok = true;
    const second = catalog.getModel(lookup, {model_id: ""});
    assertArgumentEnvelope(second, "invalid_arguments", "model_id",
                           "fresh envelope");
    assert.notEqual(second, first);
    assert.notEqual(second.error, first.error);
  });

  it("returns fresh, deeply isolated payloads", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const before = catalog.canonicalText(lookup);
    const first = catalog.getModel(lookup, {model_id: "vendor/shared"});
    const second = catalog.getModel(lookup, {model_id: "vendor/shared"});
    assert.equal(catalog.canonicalText(lookup), before,
                 "the snapshot is never mutated");
    assertSameValue(first, second, "repeated call");
    for (const field of ["exact_matches", "exact_gateways",
                         "provider_staleness", "endpoints", "counts",
                         "caveats"]) {
      assert.notEqual(first[field], second[field], field);
    }
    assert.notEqual(first.endpoints[0], second.endpoints[0], "endpoint object");
    for (let index = 0; index < first.endpoints.length; index += 1) {
      const endpoint = first.endpoints[index];
      const source = lookup.providers[endpoint.provider].models[endpoint.model_id];
      const other = second.endpoints[index].metadata;
      assert.notEqual(endpoint.metadata, source, endpoint.model_id);
      assert.notEqual(endpoint.metadata, other, endpoint.model_id);
      assert.notEqual(endpoint.metadata.architecture, source.architecture,
                      endpoint.model_id);
      assert.notEqual(endpoint.metadata.supported_parameters,
                      source.supported_parameters, endpoint.model_id);
      assert.notEqual(endpoint.metadata.pricing, source.pricing,
                      endpoint.model_id);
      assert.notEqual(endpoint.metadata.architecture, other.architecture,
                      endpoint.model_id);
    }
    // Mutating the returned payload deeply leaves the snapshot, the earlier
    // independent result and a brand-new call untouched.
    const sharedA = first.endpoints[0].metadata;
    sharedA.name = "mutated";
    sharedA.architecture.input_modalities.push("vision");
    sharedA.supported_parameters.push("seed");
    sharedA.pricing.prompt = "999";
    first.exact_matches["synthetic-a"] = false;
    first.exact_gateways.push("injected");
    first.provider_staleness["synthetic-a"] = true;
    first.endpoints.push("injected");
    first.counts.endpoints = 99;
    first.caveats.push("injected");
    assert.equal(catalog.canonicalText(lookup), before,
                 "mutating a result never reaches the snapshot");
    const kept = second.endpoints[0].metadata;
    assert.equal(kept.name, "Vendor Shared");
    assert.deepEqual(kept.architecture.input_modalities, ["text"]);
    assert.deepEqual(kept.supported_parameters,
                     ["temperature", "max_tokens"]);
    assert.deepEqual(kept.pricing, {prompt: "0", completion: "0"});
    assert.deepEqual(second.exact_matches,
                     {"synthetic-a": true, "synthetic-b": true,
                      "synthetic-c": false});
    assert.equal(second.counts.endpoints, 2);
    assert.deepEqual(second.caveats, [GET_MODEL_CAVEAT]);
    const third = catalog.getModel(lookup, {model_id: "vendor/shared"});
    assertSameValue(third, second, "a later call is unaffected");

    // The marker-endpoint metadata of two gateways sharing a label is
    // independent, and a no-match success still owns fresh containers.
    const base = catalog.getModel(lookup, {model_id: "vendor/model"});
    const aMeta = base.endpoints[0].metadata;
    const bMeta = base.endpoints[1].metadata;
    assert.notEqual(aMeta, bMeta);
    aMeta.architecture.input_modalities.push("audio");
    assert.deepEqual(bMeta.architecture.input_modalities, ["text"]);
    const noMatch = catalog.getModel(lookup, {model_id: "vendor/nope"});
    noMatch.exact_matches["synthetic-a"] = true;
    noMatch.exact_gateways.push("injected");
    noMatch.endpoints.push("injected");
    noMatch.counts.endpoints = 7;
    noMatch.caveats.push("injected");
    const freshNoMatch = catalog.getModel(lookup, {model_id: "vendor/nope"});
    assert.deepEqual(freshNoMatch.exact_matches,
                     {"synthetic-a": false, "synthetic-b": false,
                      "synthetic-c": false});
    assert.deepEqual(freshNoMatch.exact_gateways, []);
    assert.deepEqual(freshNoMatch.endpoints, []);
    assert.deepEqual(freshNoMatch.caveats, [GET_MODEL_CAVEAT]);

    // A typed float pricing token keeps its `JsonFloat` wrapper: semantic
    // equality alone would not prove the accepted typed representation was
    // preserved. The returned wrapper is a fresh object, so mutating it can
    // never reach the snapshot or another result.
    const typed = typedPricingSnapshot();
    const typedFrozen = catalog.canonicalText(typed);
    const typedFirst = catalog.getModel(typed, {model_id: "vendor/shared"});
    const typedSecond = catalog.getModel(typed, {model_id: "vendor/shared"});
    const sourceWrapper =
      typed.providers["synthetic-a"].models["vendor/shared"].pricing.prompt;
    assert.ok(catalog.isFloatToken(sourceWrapper),
              "the re-sealed snapshot carries a typed float token");
    assert.ok(catalog.isFloatToken(typedFirst.endpoints[0].metadata.pricing
                                     .prompt),
              "the returned endpoint metadata keeps the float wrapper");
    assert.equal(typedFirst.endpoints[0].metadata.pricing.prompt.value, 0);
    assert.notEqual(typedFirst.endpoints[0].metadata.pricing.prompt,
                    sourceWrapper, "the wrapper is freshly allocated");
    assert.notEqual(typedFirst.endpoints[0].metadata.pricing.prompt,
                    typedSecond.endpoints[0].metadata.pricing.prompt,
                    "two results never share a wrapper");
    typedFirst.endpoints[0].metadata.pricing.prompt.value = 5;
    assert.equal(sourceWrapper.value, 0,
                 "mutating the result wrapper never reaches the snapshot");
    assert.equal(typedSecond.endpoints[0].metadata.pricing.prompt.value, 0);
    assert.equal(catalog.canonicalText(typed), typedFrozen,
                 "the typed snapshot is never mutated");

    // Interleaved calls never share containers.
    const mixed = catalog.getModel(lookup, {model_id: "vendor/shared"});
    catalog.listEndpoints(lookup, {});
    catalog.listFreeModels(lookup, {});
    const mixedAgain = catalog.getModel(lookup, {model_id: "vendor/shared"});
    assert.notEqual(mixed.endpoints, mixedAgain.endpoints);
    assert.notEqual(mixed.endpoints[0].metadata,
                    mixedAgain.endpoints[0].metadata);
    assert.equal(catalog.canonicalText(mixed),
                 catalog.canonicalText(mixedAgain));
  });

  it("survives prototype-looking provider and raw-ID keys", () => {
    const snapshot = lookalikeKeySnapshot();
    const frozen = catalog.canonicalText(snapshot);

    const shared = catalog.getModel(snapshot, {model_id: "shared-lookalike"});
    assert.deepEqual(Object.keys(shared.exact_matches).sort(),
                     ["__proto__", "constructor"]);
    assert.equal(Object.prototype.hasOwnProperty.call(shared.exact_matches,
                                                      "__proto__"), true);
    assert.equal(Object.getPrototypeOf(shared.exact_matches),
                 Object.prototype);
    assert.equal(shared.exact_matches["__proto__"], true);
    assert.equal(shared.exact_matches["constructor"], false);
    assert.deepEqual(shared.exact_gateways, ["__proto__"]);
    assert.deepEqual(shared.provider_staleness,
                     flagMap([["__proto__", true], ["constructor", false]]));
    assert.deepEqual(shared.endpoints.map((e) => [e.provider, e.model_id]),
                     [["__proto__", "shared-lookalike"]]);
    assert.equal(shared.endpoints[0].metadata.name, "Shared Lookalike");
    assert.deepEqual(shared.counts, {exact_matches: 1, endpoints: 1,
                                     distinct_raw_ids: 1,
                                     marker_group_labels: 1});

    // A prototype-looking QUERY is literal membership, never an inherited
    // member of a result map.
    const proto = catalog.getModel(snapshot, {model_id: "__proto__"});
    assert.equal(proto.exact_matches["__proto__"], true);
    assert.deepEqual(proto.endpoints.map((e) => e.model_id), ["__proto__"]);
    const inherited = catalog.getModel(snapshot, {model_id: "valueOf"});
    assert.equal(inherited.ok, true);
    assert.deepEqual(inherited.exact_gateways, []);
    assert.deepEqual(inherited.endpoints, []);

    // `constructor` is a real marker LABEL here (from `constructor-free`) but
    // not an exact id: the two lookups stay separate and no prototype member
    // is ever treated as tracked.
    const ctor = catalog.getModel(snapshot, {model_id: "constructor"});
    assert.deepEqual(ctor.exact_gateways, []);
    assert.deepEqual(ctor.endpoints.map((e) => [e.provider, e.model_id]),
                     [["__proto__", "constructor-free"]]);
    assert.deepEqual(ctor.counts, {exact_matches: 0, endpoints: 1,
                                   distinct_raw_ids: 1,
                                   marker_group_labels: 1});
    const tostring = catalog.getModel(snapshot, {model_id: "toString"});
    assert.deepEqual(tostring.exact_gateways, ["constructor"]);
    assert.deepEqual(tostring.endpoints.map((e) => e.model_id), ["toString"]);

    assert.equal({}.polluted, undefined);
    assert.equal(Object.prototype.polluted, undefined);
    // Prototype-looking ARGUMENT keys are unknown keys, rejected before the
    // required id.
    assertArgumentEnvelope(catalog.getModel(snapshot,
                                            JSON.parse("{\"__proto__\":1}")),
                           "invalid_arguments", null, "proto argument");
    assertArgumentEnvelope(
      catalog.getModel(snapshot,
                       JSON.parse("{\"model_id\":\"__proto__\"," +
                                  "\"constructor\":1}")),
      "invalid_arguments", null, "constructor argument");
    assert.equal(catalog.canonicalText(snapshot), frozen,
                 "the lookalike snapshot is never mutated");
  });

  it("agrees with the unchanged Python reference over the selected inputs",
     () => {
    const cases = queryRows("get_model").map((row) => ({
      label: row.id,
      snapshot_text: readFixtureText(row.fixture || "lookup.json"),
      args: JSON.parse(JSON.stringify(row.args))
    }));
    const lookupText = readFixtureText("lookup.json");
    for (const entry of [
      ["boundary-unknown-key",
       JSON.parse("{\"model_id\":\"vendor/shared\",\"bogus\":1}")],
      ["boundary-non-object", 5],
      ["boundary-missing", {}],
      ["boundary-empty", {model_id: ""}],
      ["boundary-ascii-blank", {model_id: " \t\n\r\v\f"}],
      ["boundary-nbsp", {model_id: "\u00a0"}],
      ["boundary-em-space", {model_id: "\u2003"}],
      ["boundary-line-separator", {model_id: "\u2028"}],
      ["boundary-nul", {model_id: "\u0000"}],
      ["boundary-padded", {model_id: " vendor/shared "}],
      ["boundary-case", {model_id: "VENDOR/SHARED"}],
      ["boundary-marker", {model_id: "vendor/model"}],
      ["boundary-exact-with-marker", {model_id: "vendor/model:free"}],
      ["boundary-no-match", {model_id: "vendor/does-not-exist"}]
    ]) {
      cases.push({label: entry[0], snapshot_text: lookupText, args: entry[1]});
    }
    cases.push({label: "empty-registry",
                snapshot_text: catalog.canonicalText(emptyRegistrySnapshot()),
                args: {model_id: "vendor/shared"}});
    cases.push({label: "null-tick",
                snapshot_text: readFixtureText("status-unknown-time.json"),
                args: {model_id: "vendor/unknown-capabilities"}});
    cases.push({label: "future-tick",
                snapshot_text: catalog.canonicalText(futureTickSnapshot()),
                args: {model_id: "vendor/shared"}});
    cases.push({label: "typed-pricing",
                snapshot_text: catalog.canonicalText(typedPricingSnapshot()),
                args: {model_id: "vendor/shared"}});
    const markerText = catalog.canonicalText(markerBoundarySnapshot());
    for (const query of ["vendor/z", "free", "-free", "vendor/z\n",
                         "vendor/z\n\n", "vendor/z-free\n\n",
                         "vendor/z-free\r\n", "vendor/z-freely"]) {
      cases.push({label: `marker-${JSON.stringify(query)}`,
                  snapshot_text: markerText, args: {model_id: query}});
    }
    const lookalikeText = catalog.canonicalText(lookalikeKeySnapshot());
    for (const query of ["__proto__", "shared-lookalike", "constructor",
                         "toString"]) {
      cases.push({label: `lookalike-${query}`, snapshot_text: lookalikeText,
                  args: {model_id: query}});
    }
    const results = cases.map((entry) => catalog.getModel(
      catalog.loadSnapshot(entry.snapshot_text), entry.args));
    assertQueryParity("get_model", cases, results);
  });
});

// --------------------------------------------------------------------------
// pure watchdog_status: fixed-clock whole-catalogue health
// --------------------------------------------------------------------------
//
// `watchdog_status` reports published-catalogue health over the WHOLE snapshot
// at a REQUIRED, separately supplied fixed clock. `args` is `{}` on every
// golden -- the clock is never a tool argument and never an ambient read -- and
// every expected payload below is either the independently authored
// `cases.json` / `argument-cases.json` row or a hand-written literal from
// README's `Success envelope — watchdog_status` and argument tables.

/** Epoch 0 at clock 0: a VALID fresh observation, never "missing". */
function zeroEpochSnapshot() {
  return resealedSnapshot("lookup.json", (body) => {
    body.tick_epoch = 0;
  });
}

/** Every tracked pair carries a KNOWN EMPTY description string. */
function describedSnapshot() {
  return resealedSnapshot("lookup.json", (body) => {
    for (const provider of body.provider_order) {
      for (const rawId of body.providers[provider].model_ids) {
        body.providers[provider].models[rawId].description = "";
      }
    }
  });
}

/** The snapshot THREE warning codes, plus one derived status code at query. */
function composedWarningSnapshot() {
  return resealedSnapshot("lookup.json", (body) => {
    body.warnings = ["tick_epoch_unknown", "metadata_missing",
                     "metadata_invalid"];
  });
}

describe("pure watchdog_status query", () => {
  it("reproduces every independent success golden exactly", () => {
    const rows = queryRows("watchdog_status");
    // Row identity is asserted so a dropped or duplicated golden cannot shrink
    // the loop and still pass.
    assert.equal(rows.length, 9, rows.map((row) => row.id).join(","));
    assert.equal(new Set(rows.map((row) => row.id)).size, 9,
                 "every golden id is unique");
    assert.deepEqual(rows.map((row) => row.id).sort(), [
      "status_capability_unknown_epoch_now_fresh",
      "status_lookup_future_epoch_not_stale",
      "status_lookup_old_tick_stale_healthy_providers",
      "status_metadata_age_at_cadence_fresh",
      "status_metadata_age_below_cadence_fresh",
      "status_metadata_age_just_over_cadence_stale",
      "status_short_cadence_age_at_cadence_fresh",
      "status_short_cadence_age_just_over_stale",
      "status_unknown_time_missing_epoch"
    ]);
    for (const row of rows) {
      // Every status case is argument-free with its own literal case clock.
      assert.deepEqual(row.args, {}, row.id);
      assert.ok(Number.isInteger(row.now) && row.now >= 0, row.id);
      // Per-row fixture override with the documented `lookup.json` default --
      // never a wrong-fixture fallback.
      const snapshot = acceptedSnapshot(row.fixture || "lookup.json");
      const snapshotText = catalog.canonicalText(snapshot);
      const args = JSON.parse(JSON.stringify(row.args));
      const result = catalog.watchdogStatus(snapshot, args, row.now);
      assertSameValue(result, row.expect, row.id);
      assert.deepEqual(Object.keys(result).sort(), STATUS_FIELDS, row.id);
      assert.deepEqual(Object.keys(result.counts).sort(), STATUS_COUNT_FIELDS,
                       row.id);
      assert.deepEqual(Object.keys(result.metadata_coverage).sort(),
                       STATUS_COVERAGE_FIELDS, row.id);
      // Strict scalar types: ok exactly true, tick_fresh a strict bool.
      assert.equal(result.ok, true, row.id);
      assert.equal(typeof result.tick_fresh, "boolean", row.id);
      assert.ok(result.last_tick_age_s === null ||
                Number.isInteger(result.last_tick_age_s), row.id);
      // Provenance echoes the consumed snapshot literally; a future epoch is
      // never repaired and the cadence is never a hardcoded 3600.
      assert.equal(result.snapshot_id, snapshot.snapshot_id, row.id);
      assert.equal(result.tick_epoch, snapshot.tick_epoch, row.id);
      assert.equal(result.cadence_s, snapshot.cadence_s, row.id);
      // Inclusive freshness: age 0 and age == cadence are fresh; a null or
      // future tick is null age and false, never clamped to a healthy zero.
      const tick = snapshot.tick_epoch;
      const cadence = snapshot.cadence_s;
      if (tick === null || tick > row.now) {
        assert.equal(result.last_tick_age_s, null, row.id);
        assert.equal(result.tick_fresh, false, row.id);
      } else {
        assert.equal(result.last_tick_age_s, row.now - tick, row.id);
        assert.equal(result.tick_fresh, row.now - tick <= cadence, row.id);
        assert.ok(result.last_tick_age_s >= 0, row.id);
      }
      // Every provider_order key, zero-ID providers included, in display order.
      assert.deepEqual(Object.keys(result.provider_staleness),
                       snapshot.provider_order, row.id);
      assert.deepEqual(Object.keys(result.provider_counts),
                       snapshot.provider_order, row.id);
      for (const provider of snapshot.provider_order) {
        const entry = snapshot.providers[provider];
        assert.equal(typeof result.provider_staleness[provider], "boolean",
                     `${row.id}:${provider}`);
        assert.equal(result.provider_staleness[provider], entry.stale,
                     `${row.id}:${provider}`);
        assert.ok(Number.isInteger(result.provider_counts[provider]) &&
                  result.provider_counts[provider] >= 0,
                  `${row.id}:${provider}`);
        assert.equal(result.provider_counts[provider],
                     entry.model_ids.length, `${row.id}:${provider}`);
      }
      // Whole-catalogue counts, structurally restated from the snapshot; the
      // literal golden above remains the correctness oracle.
      const pairs = [];
      for (const provider of snapshot.provider_order) {
        for (const rawId of snapshot.providers[provider].model_ids) {
          pairs.push(rawId);
        }
      }
      assert.deepEqual(result.counts, {
        gateways: snapshot.provider_order.length,
        endpoints: pairs.length,
        distinct_raw_ids: new Set(pairs).size,
        marker_group_labels:
          new Set(pairs.map((rawId) => catalog.stripFreeMarker(rawId))).size
      }, row.id);
      // Coverage is NON-NULL presence: a known 0 / [] / "" counts, null does
      // not, over the global tracked-pair denominator.
      const coverage = {
        endpoints: pairs.length, context_length_known: 0,
        input_modalities_known: 0, output_modalities_known: 0,
        supported_parameters_known: 0, description_known: 0
      };
      for (const provider of snapshot.provider_order) {
        const entry = snapshot.providers[provider];
        for (const rawId of entry.model_ids) {
          const model = entry.models[rawId];
          if (model.context_length !== null) {
            coverage.context_length_known += 1;
          }
          if (model.architecture.input_modalities !== null) {
            coverage.input_modalities_known += 1;
          }
          if (model.architecture.output_modalities !== null) {
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
      assert.deepEqual(result.metadata_coverage, coverage, row.id);
      assert.equal(result.metadata_coverage.endpoints,
                   result.counts.endpoints, row.id);
      for (const value of Object.values(result.metadata_coverage)) {
        assert.ok(Number.isInteger(value) && value >= 0 &&
                  value <= result.metadata_coverage.endpoints, row.id);
      }
      // The three unobservables are STRICTLY null -- never 0 or false.
      assert.equal(result.pending_alerts, null, row.id);
      assert.equal(result.site_published, null, row.id);
      assert.equal(result.site_publish_age_s, null, row.id);
      // Snapshot warnings plus derived codes, deduplicated in frozen order.
      assert.deepEqual(result.warnings,
                       STATUS_WARNING_ORDER.filter(
                         (code) => result.warnings.includes(code)), row.id);
      assert.equal(result.warnings.length,
                   new Set(result.warnings).size, row.id);
      // Inputs are never mutated.
      assert.equal(catalog.canonicalText(snapshot), snapshotText,
                   `${row.id}: the snapshot is never mutated`);
      assert.deepEqual(args, {}, `${row.id}: the arguments are never mutated`);
    }
  });

  it("moves only the clock-derived fields across the freshness boundary", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const clocks = [1759199999, 1759200000, 1759203600, 1759203601];
    const results = clocks.map((clock) => catalog.watchdogStatus(lookup, {},
                                                                 clock));
    // Provenance, provider maps, counts and coverage are clock-INVARIANT.
    for (const field of ["provider_staleness", "provider_counts",
                         "metadata_coverage", "counts", "snapshot_id",
                         "tick_epoch", "cadence_s", "pending_alerts",
                         "site_published", "site_publish_age_s"]) {
      for (const result of results) {
        assertSameValue(result[field], results[0][field], field);
      }
    }
    // Only age, freshness and warnings move -- future, exact tick, at cadence,
    // one second over.
    assert.deepEqual(results.map((result) => result.last_tick_age_s),
                     [null, 0, 3600, 3601]);
    assert.deepEqual(results.map((result) => result.tick_fresh),
                     [false, true, true, false]);
    assert.deepEqual(results.map((result) => result.warnings),
                     [["tick_epoch_future"], [], [], ["tick_stale"]]);

    // The boundary uses the SNAPSHOT cadence: at 120 s, age 120 is fresh and
    // 121 is stale -- never a hardcoded 3600.
    const short = acceptedSnapshot("status-short-cadence.json");
    assert.equal(catalog.watchdogStatus(short, {}, 1759200120).last_tick_age_s,
                 120);
    assert.equal(catalog.watchdogStatus(short, {}, 1759200120).tick_fresh, true);
    assert.deepEqual(catalog.watchdogStatus(short, {}, 1759200120).warnings,
                     ["metadata_missing"]);
    const over = catalog.watchdogStatus(short, {}, 1759200121);
    assert.equal(over.cadence_s, 120);
    assert.equal(over.last_tick_age_s, 121);
    assert.equal(over.tick_fresh, false);
    assert.deepEqual(over.warnings, ["metadata_missing", "tick_stale"]);

    // A null epoch is never a stale known observation, however late the clock.
    const unknown = catalog.watchdogStatus(
      acceptedSnapshot("status-unknown-time.json"), {}, 4102444800);
    assert.equal(unknown.last_tick_age_s, null);
    assert.equal(unknown.tick_fresh, false);
    assert.deepEqual(unknown.warnings,
                     ["tick_epoch_unknown", "metadata_missing"]);
  });

  it("returns fresh, isolated containers and never aliases an input", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const frozen = catalog.canonicalText(lookup);
    const first = catalog.watchdogStatus(lookup, {}, 1759203601);
    const second = catalog.watchdogStatus(lookup, {}, 1759203601);
    assertSameValue(first, second, "two identical calls agree");
    // Interleaved sync calls, not a worker-concurrency test: every mutable
    // outer container is freshly allocated.
    for (const field of ["provider_staleness", "provider_counts",
                         "metadata_coverage", "counts", "warnings"]) {
      assert.notEqual(first[field], second[field],
                      `${field} is separately allocated`);
    }
    first.provider_staleness["synthetic-a"] = true;
    first.provider_counts["synthetic-a"] = 99;
    first.metadata_coverage.endpoints = 99;
    first.counts.gateways = 99;
    first.warnings.push("injected");
    assert.equal(catalog.canonicalText(lookup), frozen,
                 "the snapshot is untouched");
    assert.equal(second.provider_staleness["synthetic-a"], false);
    assert.equal(second.provider_counts["synthetic-a"], 3);
    assert.equal(second.counts.gateways, 3);
    assert.deepEqual(second.warnings, ["tick_stale"]);
    assertSameValue(catalog.watchdogStatus(lookup, {}, 1759203601), second,
                    "a later call is unaffected");

    // Interleaving the three earlier tools neither sees nor supplies state.
    const statusA = catalog.watchdogStatus(lookup, {}, 1759200000);
    const models = catalog.listFreeModels(lookup, {});
    const endpoints = catalog.listEndpoints(lookup, {});
    const model = catalog.getModel(lookup, {model_id: "vendor/shared"});
    const statusB = catalog.watchdogStatus(lookup, {}, 1759200000);
    assertSameValue(statusA, statusB, "interleaved status calls agree");
    statusB.provider_counts["synthetic-b"] = 42;
    statusB.warnings.push("injected");
    assert.equal(models.n_gateways, 3);
    assert.equal(endpoints.ok, true);
    assert.equal(endpoints.counts.endpoints, 5);
    assert.equal(model.counts.endpoints, 2);
    assert.equal(catalog.canonicalText(lookup), frozen, "still untouched");

    // A fresh error envelope on every call, independent of the clock.
    const errorOne = catalog.watchdogStatus(lookup, {now: 0}, 0);
    errorOne.error.code = "mutated";
    errorOne.ok = true;
    const errorTwo = catalog.watchdogStatus(lookup, {now: 0}, 0);
    assertArgumentEnvelope(errorTwo, "invalid_arguments", null,
                           "a fresh envelope on every call");
    assert.notEqual(errorOne, errorTwo);
    assert.equal(catalog.canonicalText(lookup), frozen);
  });

  it("mirrors the accepted Python status controls literally", () => {
    // (1) epoch 0 at clock 0 is a VALID fresh observation, never "missing",
    // and adds no warning.
    const atZero = catalog.watchdogStatus(zeroEpochSnapshot(), {}, 0);
    assert.equal(atZero.tick_epoch, 0);
    assert.equal(atZero.last_tick_age_s, 0);
    assert.equal(atZero.tick_fresh, true);
    assert.deepEqual(atZero.warnings, []);

    // (2) A legitimately EMPTY registry is an all-provider control: empty maps
    // and a fully zeroed counts/coverage pair.
    const whole = catalog.watchdogStatus(emptyRegistrySnapshot(), {},
                                         1759200000);
    assert.deepEqual(Object.keys(whole).sort(), STATUS_FIELDS);
    assert.equal(whole.ok, true);
    assert.deepEqual(whole.provider_staleness, {});
    assert.deepEqual(whole.provider_counts, {});
    assert.deepEqual(whole.counts, {gateways: 0, endpoints: 0,
                                    distinct_raw_ids: 0,
                                    marker_group_labels: 0});
    assert.deepEqual(whole.metadata_coverage, {
      endpoints: 0, context_length_known: 0, input_modalities_known: 0,
      output_modalities_known: 0, supported_parameters_known: 0,
      description_known: 0
    });

    // (3) A known-EMPTY description string is known: all five pairs count.
    assert.equal(
      catalog.watchdogStatus(describedSnapshot(), {},
                             1759200000).metadata_coverage.description_known,
      5);

    // (4) The maximal FOUR-code composition in the frozen order. A full five
    // is unreachable: the two derived codes are mutually exclusive.
    const composed = composedWarningSnapshot();
    assert.deepEqual(catalog.watchdogStatus(composed, {}, 1759203601).warnings,
                     ["tick_epoch_unknown", "metadata_missing",
                      "metadata_invalid", "tick_stale"]);
    assert.deepEqual(catalog.watchdogStatus(composed, {}, 1759199999).warnings,
                     ["tick_epoch_unknown", "metadata_missing",
                      "metadata_invalid", "tick_epoch_future"]);

    // (5) Prototype-looking provider and raw-ID keys stay literal data.
    const lookalike = lookalikeKeySnapshot();
    const lookalikeText = catalog.canonicalText(lookalike);
    const proto = catalog.watchdogStatus(lookalike, {}, 1759200000);
    assert.deepEqual(Object.keys(proto.provider_staleness),
                     ["__proto__", "constructor"]);
    assert.equal(proto.provider_staleness["__proto__"], true);
    assert.equal(proto.provider_staleness["constructor"], false);
    assert.equal(proto.provider_counts["__proto__"], 3);
    assert.equal(proto.provider_counts["constructor"], 1);
    assert.deepEqual(proto.counts, {gateways: 2, endpoints: 4,
                                    distinct_raw_ids: 4,
                                    marker_group_labels: 4});
    assert.equal(proto.last_tick_age_s, null);
    assert.equal(proto.tick_fresh, false);
    assert.deepEqual(proto.warnings, ["tick_epoch_unknown"]);
    assert.equal({}.polluted, undefined);
    assert.equal(Object.prototype.polluted, undefined);
    assert.equal(catalog.canonicalText(lookalike), lookalikeText,
                 "the lookalike snapshot is never mutated");

    // (6) A zero-ID provider is still a configured gateway: count 0, stale flag
    // carried forward, and one whole-catalogue gateway.
    const capability = catalog.watchdogStatus(
      acceptedSnapshot("capability-unknown.json"), {}, 1759200000);
    assert.equal(capability.provider_counts["synthetic-b"], 0);
    assert.equal(capability.provider_staleness["synthetic-b"], true);
    assert.equal(capability.counts.gateways, 2);
    assert.equal(capability.counts.endpoints, 2);

    // (7) One raw id shared by two gateways is two endpoints and ONE distinct
    // raw id; marker labels are labels, never verified identities.
    const shared = catalog.watchdogStatus(acceptedSnapshot("lookup.json"), {},
                                          1759200000);
    assert.equal(shared.provider_counts["synthetic-a"], 3);
    assert.equal(shared.provider_counts["synthetic-b"], 2);
    assert.deepEqual(shared.counts, {gateways: 3, endpoints: 5,
                                     distinct_raw_ids: 4,
                                     marker_group_labels: 3});

    // (8) NON-NULL, not truthiness: a known 0 / [] / "" counts as known while
    // null does not, over the two short-cadence pairs.
    const short = catalog.watchdogStatus(
      acceptedSnapshot("status-short-cadence.json"), {}, 1759200000);
    assert.deepEqual(short.metadata_coverage, {
      endpoints: 2, context_length_known: 1, input_modalities_known: 1,
      output_modalities_known: 1, supported_parameters_known: 1,
      description_known: 1
    });

    // (9) No private operator state is observable through this payload.
    for (const payload of [whole, proto, shared, short, atZero]) {
      assert.equal(payload.pending_alerts, null);
      assert.equal(payload.site_published, null);
      assert.equal(payload.site_publish_age_s, null);
      assert.deepEqual(Object.keys(payload).sort(), STATUS_FIELDS);
      for (const absent of ["state_root", "source", "path", "cache", "tool",
                            "caveats", "endpoints", "query",
                            "provider_health", "pricing"]) {
        assert.equal(Object.prototype.hasOwnProperty.call(payload, absent),
                     false, `no ${absent} field`);
      }
    }
  });

  it("accepts only the empty argument object", () => {
    const invalid = argumentRows("invalid_cases", "watchdog_status");
    const valid = argumentRows("valid_controls", "watchdog_status");
    assert.equal(invalid.length, 1, "one frozen invalid row");
    assert.equal(invalid[0].id, "unknown_arg_watchdog_status");
    assert.deepEqual(invalid[0].args, {now: 1759200000});
    assert.equal(valid.length, 1, "one frozen acceptance control");
    assert.equal(valid[0].id, "valid_omitted_watchdog_status");
    assert.deepEqual(valid[0].args, {});

    const lookup = acceptedSnapshot("lookup.json");
    const frozen = catalog.canonicalText(lookup);

    for (const row of invalid) {
      // The COMPLETE literal, independently authored envelope.
      const snapshot = acceptedSnapshot(row.fixture);
      const args = JSON.parse(JSON.stringify(row.args));
      const result = catalog.watchdogStatus(snapshot, args, 1759200000);
      assertSameValue(result, row.expect, row.id);
      assertArgumentEnvelope(result, "invalid_arguments", null, row.id);
      assert.deepEqual(args, row.args,
                       "the argument object is never mutated");
    }
    for (const row of valid) {
      const result = catalog.watchdogStatus(
        acceptedSnapshot(row.fixture), JSON.parse(JSON.stringify(row.args)),
        1759200000);
      assert.equal(result.ok, true, row.id);
    }

    // A whole argument value that is not a JSON object rejects with parameter
    // null -- never a coerced array, number, string or bool.
    for (const args of [null, [], 5, "x", false, true, 1.5, undefined,
                        [["now", 1]]]) {
      assertArgumentEnvelope(catalog.watchdogStatus(lookup, args, 0),
                             "invalid_arguments", null,
                             `non-object ${JSON.stringify(args)}`);
    }

    // ANY own key is unknown, `now` included: the separately supplied clock
    // never authorizes `args.now`.
    for (const key of ["now", "provider", "limit", "offset", "query",
                       "snapshot_id", "hostile"]) {
      assertArgumentEnvelope(
        catalog.watchdogStatus(lookup, {[key]: 1759200000}, 0),
        "invalid_arguments", null, `unknown key ${key}`);
    }
    assertArgumentEnvelope(
      catalog.watchdogStatus(lookup, {now: 1759200000,
                                      provider: "synthetic-a"}, 0),
      "invalid_arguments", null, "two unknown keys");
    for (const protoArgs of [JSON.parse('{"__proto__":1}'),
                             JSON.parse('{"constructor":1}'),
                             JSON.parse('{"toString":1}')]) {
      assertArgumentEnvelope(catalog.watchdogStatus(lookup, protoArgs, 0),
                             "invalid_arguments", null,
                             "prototype-looking argument key");
    }
    assert.equal(catalog.watchdogStatus(lookup, {}, 0).ok, true);

    // The clock is not touched before the argument boundary: the throwing
    // coercion sentinel never runs because the unknown key rejects first.
    const throwingClock = {
      valueOf() {
        throw new Error("the fixed clock was read before validation");
      },
      [Symbol.toPrimitive]() {
        throw new Error("the fixed clock was read before validation");
      }
    };
    assertArgumentEnvelope(
      catalog.watchdogStatus(lookup, {now: 1759200000}, throwingClock),
      "invalid_arguments", null, "unknown key before clock use");
    assertArgumentEnvelope(catalog.watchdogStatus(lookup, null,
                                                 throwingClock),
                           "invalid_arguments", null,
                           "non-object before clock use");

    // A hostile value under an unknown key is never reflected.
    const hostile = catalog.watchdogStatus(lookup,
                                           {now: "<script>undefined"}, 0);
    assertArgumentEnvelope(hostile, "invalid_arguments", null,
                           "hostile value");
    assert.equal(JSON.stringify(hostile).includes("script"), false,
                 "no hostile value reflection");
    assert.equal(catalog.canonicalText(lookup), frozen,
                 "the snapshot is never mutated");
  });

  it("never reads an ambient wall clock", () => {
    const source = fs.readFileSync(
      path.join(REPO_ROOT, "web", "catalog-query.js"), "utf8");
    for (const forbidden of ["Date.now", "new Date", "performance.now",
                             "Math.random", "setTimeout", "setInterval",
                             "process.hrtime"]) {
      assert.equal(source.includes(forbidden), false,
                   `the pure module never references ${forbidden}`);
    }
    const lookup = acceptedSnapshot("lookup.json");
    const realNow = Date.now;
    try {
      Date.now = () => { throw new Error("ambient clock read"); };
      const atTick = catalog.watchdogStatus(lookup, {}, 1759200000);
      assert.equal(atTick.last_tick_age_s, 0);
      assert.equal(atTick.tick_fresh, true);
      assert.deepEqual(atTick.warnings, []);
      const stale = catalog.watchdogStatus(lookup, {}, 1759203601);
      assert.equal(stale.last_tick_age_s, 3601);
      assert.deepEqual(stale.warnings, ["tick_stale"]);
    } finally {
      Date.now = realNow;
    }
  });

  it("agrees with the unchanged Python reference over the selected inputs",
     () => {
    const cases = queryRows("watchdog_status").map((row) => ({
      label: row.id,
      snapshot_text: readFixtureText(row.fixture || "lookup.json"),
      args: JSON.parse(JSON.stringify(row.args)),
      now: row.now
    }));
    const lookupText = readFixtureText("lookup.json");
    const shortText = readFixtureText("status-short-cadence.json");
    for (const entry of [
      ["boundary-unknown-now", lookupText, {now: 1759200000}, 1759200000],
      ["boundary-unknown-provider", lookupText, {provider: "synthetic-a"},
       1759200000],
      ["boundary-unknown-limit", lookupText, {limit: 5}, 1759200000],
      ["boundary-non-object", lookupText, 5, 0],
      ["boundary-null-object", lookupText, null, 0],
      ["boundary-multi-key", lookupText, {now: 1, limit: 2}, 0],
      ["boundary-empty", lookupText, {}, 0],
      ["boundary-future", lookupText, {}, 1759199999],
      ["boundary-exact-tick", lookupText, {}, 1759200000],
      ["boundary-at-cadence", lookupText, {}, 1759203600],
      ["boundary-over-cadence", lookupText, {}, 1759203601],
      ["boundary-short-at", shortText, {}, 1759200120],
      ["boundary-short-over", shortText, {}, 1759200121],
      ["boundary-far-future-clock", readFixtureText("status-unknown-time.json"),
       {}, 4102444800]
    ]) {
      cases.push({label: entry[0], snapshot_text: entry[1], args: entry[2],
                  now: entry[3]});
    }
    cases.push({label: "control-empty-registry",
                snapshot_text: catalog.canonicalText(emptyRegistrySnapshot()),
                args: {}, now: 1759200000});
    cases.push({label: "control-epoch-zero",
                snapshot_text: catalog.canonicalText(zeroEpochSnapshot()),
                args: {}, now: 0});
    cases.push({label: "control-described",
                snapshot_text: catalog.canonicalText(describedSnapshot()),
                args: {}, now: 1759200000});
    cases.push({label: "control-composed-stale",
                snapshot_text: catalog.canonicalText(composedWarningSnapshot()),
                args: {}, now: 1759203601});
    cases.push({label: "control-composed-future",
                snapshot_text: catalog.canonicalText(composedWarningSnapshot()),
                args: {}, now: 1759199999});
    cases.push({label: "control-lookalike",
                snapshot_text: catalog.canonicalText(lookalikeKeySnapshot()),
                args: {}, now: 1759200000});
    const results = cases.map((entry) => catalog.watchdogStatus(
      catalog.loadSnapshot(entry.snapshot_text), entry.args, entry.now));
    assertQueryParity("watchdog_status", cases, results);
  });
});

// --------------------------------------------------------------------------
// cross-fixture integration and the dual (browser + Node) export
// --------------------------------------------------------------------------
describe("fixture integration and export shape", () => {
  const SNAPSHOT_FIELDS = ["cadence_s", "provider_order", "providers",
                           "schema_version", "snapshot_id", "tick_epoch",
                           "warnings"];

  it("accepts and re-hashes every published snapshot fixture", () => {
    const accepted = [];
    for (const name of fs.readdirSync(FIXTURE_DIR).sort()) {
      if (!name.endsWith(".json")) {
        continue;
      }
      const text = readFixtureText(name);
      const shape = JSON.parse(text);
      if (!shape || typeof shape !== "object" || Array.isArray(shape)) {
        continue;
      }
      if (Object.keys(shape).sort().join(",") !==
          SNAPSHOT_FIELDS.join(",")) {
        continue;
      }
      const decoded = catalog.loadJson(text);
      const snapshot = catalog.validateSnapshot(decoded);
      assert.equal(catalog.snapshotHash(snapshot), snapshot.snapshot_id,
                   `${name}: fixture id matches the canonical body digest`);
      assert.equal(catalog.loadSnapshot(text).snapshot_id,
                   snapshot.snapshot_id,
                   `${name}: loadSnapshot accepts the same document`);
      accepted.push(name);
    }
    assert.ok(accepted.length >= 9,
              `at least nine snapshot fixtures, saw ${accepted.join(",")}`);
    for (const required of ["basic.json", "lookup.json", "ordering.json",
                            "provider-extension.json", "search-text.json",
                            "capability-unknown.json",
                            "metadata-edge-cases.json"]) {
      assert.ok(accepted.includes(required), `${required} is a snapshot`);
    }
  });

  it("loads as a classic script that publishes one namespaced global", () => {
    const vm = require("node:vm");
    const source = fs.readFileSync(
      path.join(__dirname, "..", "..", "web", "catalog-query.js"), "utf8");
    const sandbox = {};
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    const browserApi = sandbox.WatchdogCatalog;
    assert.ok(browserApi, "the classic script publishes WatchdogCatalog");
    assert.equal(typeof browserApi.loadJson, "function");
    const text = readFixtureText("lookup.json");
    const snapshot = browserApi.loadSnapshot(text);
    assert.equal(snapshot.snapshot_id, catalog.loadSnapshot(text).snapshot_id,
                 "the in-page copy hashes identically");
    // Re-inlining the same classic script is idempotent, never a throw.
    vm.runInContext(source, sandbox);
    assert.equal(typeof sandbox.WatchdogCatalog.validateSnapshot, "function");
  });

  it("keeps the public surface deliberate", () => {
    assert.deepEqual(Object.keys(catalog).sort(), [
      "CatalogError", "JsonFloat", "MAX_JSON_DEPTH", "MAX_SAFE_INT",
      "SCHEMA_VERSION", "canonicalBytes", "canonicalText",
      "compareEndpointPairs", "compareNaturalIds", "decodeTypedJson",
      "endpointKey", "getModel", "isFloatToken", "listEndpoints",
      "listFreeModels", "loadJson", "loadSnapshot", "naturalKey",
      "normalizeCapabilities", "projectCapabilities", "projectModel",
      "snapshotHash", "stripFreeMarker", "validateSnapshot", "watchdogStatus"
    ]);
    assert.equal(catalog.MAX_SAFE_INT, 9007199254740991);
    assert.equal(catalog.MAX_JSON_DEPTH, 64);
    assert.equal(catalog.SCHEMA_VERSION, 1);
    const error = new catalog.CatalogError("invalid_json");
    assert.equal(error.code, "invalid_json");
    assert.equal(error.message, RAW_ERROR_MESSAGE);
    assert.deepEqual(error.toPayload(), {
      ok: false, error: { code: "invalid_json", message: RAW_ERROR_MESSAGE }
    });
    assert.ok(error instanceof Error);
  });
});
