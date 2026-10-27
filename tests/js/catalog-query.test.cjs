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
      "SCHEMA_VERSION", "canonicalBytes", "canonicalText", "decodeTypedJson",
      "isFloatToken", "loadJson", "loadSnapshot", "normalizeCapabilities",
      "projectCapabilities", "projectModel", "snapshotHash", "validateSnapshot"
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
