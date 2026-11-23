"use strict";

/**
 * Node tests for the dependency-free catalogue validation/projection
 * counterpart (`web/catalog-query.js`).
 *
 * The shared raw-JSON, canonical, structural, projection, ordering, marker,
 * argument and shared-tool corpora are owned by `tests/test_contract_parity.py`,
 * which executes every row against independent literal expectations in BOTH
 * engines. This file keeps the native host behaviour (prototype hazards, typed
 * float tokens, browser export), the hand-authored boundary literals, and the
 * live Python-reference drift probes. Expectations are NEVER produced by the
 * implementation under test, and no validator, comparator or canonical encoder
 * is reimplemented here.
 *
 * Run: node --test tests/js/catalog-query.test.cjs
 */

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

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
  "from watchdog_catalog.query import (strip_free_marker, natural_key)",
  "payload = json.load(sys.stdin)",
  "print(json.dumps({",
  '    "markers": [strip_free_marker(x) for x in payload["ids"]],',
  '    "keys": [[list(t) for t in natural_key(x)] for x in payload["ids"]],',
  '    "order": sorted(payload["ids"], key=natural_key)',
  "}))"
].join("\n");

function referencePython() {
  // An explicit override is authoritative; otherwise the normal PATH Python is
  // used. The old hard-coded host interpreter path is deliberately gone so this
  // suite stays installable and portable.
  const override = process.env.CATALOG_PYTHON;
  return override && override.length > 0 ? override : "python3";
}

function runPythonReference(payload) {
  const { execFileSync } = require("node:child_process");
  const env = Object.assign({}, process.env);
  delete env.PYTHONPATH;
  delete env.PYTHONHOME;
  env.PYTHONDONTWRITEBYTECODE = "1";
  const stdout = execFileSync(referencePython(), ["-c", REFERENCE_SCRIPT], {
    cwd: REPO_ROOT, env, input: JSON.stringify(payload), encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024, timeout: 120000
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

// --------------------------------------------------------------------------
// raw JSON acceptance/rejection boundary
// --------------------------------------------------------------------------
describe("raw JSON acceptance boundary", () => {
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
      '{"schema_version":2,"providers":{"__proto__":{"polluted":true}}}');
    assert.equal(snapshotShaped.providers.polluted, undefined);
    assert.deepEqual(Object.keys(snapshotShaped.providers), ["__proto__"]);
  });
});

// --------------------------------------------------------------------------
// typed canonical encoding
// --------------------------------------------------------------------------
describe("typed canonical encoding", () => {
  it("sorts keys by Unicode code point, not UTF-16 code unit", () => {
    const value = catalog.loadJson(
      '{"\\uD83D\\uDE80":"astral","2":"two","\\uFF21":"bmp","10":"ten","\\uE000":"pua"}');
    assert.equal(
      catalog.canonicalText(value),
      '{"10":"ten","2":"two","\\ue000":"pua","\\uff21":"bmp","\\ud83d\\ude80":"astral"}',
      "code-point order places the astral key last");
  });

  it("escapes to ASCII with the frozen short escapes", () => {
    const text = catalog.canonicalText(catalog.loadJson(
      '{"s":"caf\\u00e9","a":"[\\u007f]"}'));
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
    `{"cadence_s":3600,"provider_order":["synthetic-a","synthetic-b"],"providers":{"synthetic-a":{"api_type":"openai","chat_completions_url":"https://synthetic-alpha.example/v1/chat/completions","display_name":"Synthetic Alpha","model_ids":["alpha-1:free","alpha-2-free"],"models":{"alpha-1:free":{"architecture":{"input_modalities":["text"],"output_modalities":["text"]},"context_length":32768,"description":"Synthetic model one for the contract freeze.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","id":"alpha-1:free","name":"Alpha One","supported_parameters":["temperature","max_tokens"]},"alpha-2-free":{"architecture":{"input_modalities":["text"],"output_modalities":["text"]},"context_length":8192,"description":null,"id":"alpha-2-free","name":"Alpha Two","supported_parameters":["temperature"]}},"stale":false},"synthetic-b":{"api_type":"openai","chat_completions_url":"https://synthetic-beta.example/v1/chat/completions","display_name":"Synthetic Beta","model_ids":[],"models":{},"stale":false}},"schema_version":2,"tick_epoch":1759200000,"warnings":[]}`;
  const PYTHON_SEALED_ID =
    "60c5718e5e84e4f0283dfd7d5f693632e9f4e3e868ae69c9f8df6225812475cb";
  // A retained known-wrong comparison value. Its historical origin is
  // unverified; the independently sealed positive control above supplies the
  // oracle for the current body.
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
      schema_version: 2,
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
    assertRejects(() => catalog.loadSnapshot("{\"schema_version\": 2,"),
                  "invalid_json", RAW_ERROR_MESSAGE, "truncated document");
  });

  it("reserves unsupported_schema_version for a strict integer other than 2", () => {
    const unsupported = mutate({
      id: "probe-version-3", base_fixture: "lookup.json", reseal: true,
      changes: [{ op: "set", path: ["schema_version"], value: 3 }]
    });
    assertRejects(() => catalog.validateSnapshot(unsupported),
                  "unsupported_schema_version", UNSUPPORTED_VERSION_MESSAGE,
                  "schema_version 3");
    for (const wrongType of [true, "2", null, catalog.loadJson("2.0")]) {
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
      schema_version: 2, tick_epoch: 0, cadence_s: 3600,
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
    assert.deepEqual(emptyNormalized.input_modalities, []);
    assert.deepEqual(emptyNormalized.output_modalities, []);
    assert.deepEqual(Object.keys(emptyNormalized).sort(), [
      "context_length", "input_modalities", "output_modalities",
      "supported_parameters"]);

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
      private_field: "never serialized",
      _token: "never serialized"
    });
    assert.deepEqual(Object.keys(model).sort(),
                     ["architecture", "context_length", "description", "id",
                      "name", "supported_parameters"]);
    assert.deepEqual(warnings, []);
    assertSameValue(model, {
      id: "vendor/model:free",
      name: "Vendor Model",
      description: "A description.",
      context_length: 65536,
      architecture: { input_modalities: ["text"],
                      output_modalities: ["text"] },
      supported_parameters: ["temperature"]
    }, "complete projection");
  });

  it("applies the name and description degradation rules", () => {
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
      supported_parameters: ["temperature"]
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
                      "name", "supported_parameters"]);
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
// ASCII digits), with case-sensitive Unicode CODE POINT text. The frozen
// `ordering-cases.json` and `cases.json` marker tables are executed by
// `tests/test_contract_parity.py`; every expectation below is a hand-authored
// literal. `array.sort` is always called with the PRODUCTION comparator, so no
// second comparator is implemented in the tests.
describe("ordering and free-marker helpers", () => {
  const orderingCases = readFixtureDocument("ordering-cases.json");
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

  it("reproduces the literal ordering controls forward and reversed",
     () => {
       const corpus = new Set();
       for (const [supplied, expected] of NATURAL_KEY_CONTROLS) {
         const before = JSON.stringify(supplied);
         assert.deepEqual(sortedBy(supplied, catalog.compareNaturalIds),
                          expected, JSON.stringify(supplied));
         assert.equal(JSON.stringify(supplied), before,
                      `${JSON.stringify(supplied)}: no mutation`);
         assert.deepEqual(
           sortedBy(supplied.slice().reverse(), catalog.compareNaturalIds),
           expected, `${JSON.stringify(supplied)}: reverse-supplied`);
         for (const id of supplied) {
           corpus.add(id);
         }
         for (const id of expected) {
           corpus.add(id);
         }
       }
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
    const result = runPythonReference({ ids: uniqueIds });
    assert.deepEqual(
      uniqueIds.map((rawId) => catalog.stripFreeMarker(rawId)),
      result.markers, "marker outputs agree with Python");
    assert.deepEqual(uniqueIds.map((rawId) => catalog.naturalKey(rawId)),
                     result.keys, "natural keys agree with Python");
    assert.deepEqual(sortedBy(uniqueIds, catalog.compareNaturalIds),
                     result.order, "natural order agrees with Python");
  });
});

// --------------------------------------------------------------------------
// shared query-case tables
// --------------------------------------------------------------------------
//
// The literal `cases.json` success rows are driven through the pure queries by
// the unchanged Python/Node contract-parity owner; the property owners below
// reuse this table plus a small Python comparison as a DRIFT probe against the
// unchanged reference helper -- agreement alone is never taken as proof.

const QUERY_CASES_DOCUMENT = readFixtureDocument("cases.json");
const QUERY_CASES = QUERY_CASES_DOCUMENT.cases;
const QUERY_FIXTURE = QUERY_CASES_DOCUMENT.fixture;

// The frozen `watchdog_status` top-level field set, copied from README's
// `Success envelope — watchdog_status` table.
const STATUS_FIELDS = ["cadence_s", "counts", "last_tick_age_s",
                       "metadata_coverage", "ok", "pending_alerts",
                       "provider_counts", "provider_staleness",
                       "site_publish_age_s", "site_published", "snapshot_id",
                       "tick_epoch", "tick_fresh", "warnings"];
const ARGUMENT_MESSAGES = {
  invalid_arguments: "Invalid tool arguments.",
  unknown_provider: "Unknown provider."
};

function queryRows(tool) {
  return QUERY_CASES.filter((row) => row.tool === tool);
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
    supported_parameters: null
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
    schema_version: 2,
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
  "",
  "def _stage_pairs(snapshot, args):",
  "    pairs = query_module._search_text_provider_pairs(",
  "        snapshot, query=args['query'], provider=args['provider'])",
  "    pairs = query_module._search_min_context_pairs(",
  "        snapshot, pairs, min_context_length=args['min_context_length'])",
  "    pairs = query_module._search_capability_pairs(",
  "        snapshot, pairs, field='input_modalities',",
  "        requested=args['input_modalities'])",
  "    pairs = query_module._search_capability_pairs(",
  "        snapshot, pairs, field='supported_parameters',",
  "        requested=args['supported_parameters'])",
  "    return [[provider, raw_id] for provider, raw_id in pairs]",
  "",
  "def _stage_filter(snapshot, args):",
  "    pairs = [tuple(pair) for pair in args['pairs']]",
  "    return {",
  "        'context': [list(pair) for pair in",
  "                    query_module._search_min_context_pairs(",
  "                        snapshot, pairs,",
  "                        min_context_length=args['min_context_length'])],",
  "        'input': [list(pair) for pair in",
  "                  query_module._search_capability_pairs(",
  "                      snapshot, pairs, field='input_modalities',",
  "                      requested=args['input_modalities'])],",
  "        'parameters': [list(pair) for pair in",
  "                       query_module._search_capability_pairs(",
  "                           snapshot, pairs, field='supported_parameters',",
  "                           requested=args['supported_parameters'])],",
  "    }",
  "",
  "def _load_error(snapshot_text):",
  "    try:",
  "        load_snapshot(snapshot_text)",
  "    except Exception as error:",
  "        return {'code': getattr(error, 'code', type(error).__name__)}",
  "    return {'code': None}",
  "",
  "payload = json.load(sys.stdin)",
  "handlers = {'get_model': query_module.get_model,",
  "            'watchdog_status': query_module.watchdog_status,",
  "            'search_stage_pairs': _stage_pairs,",
  "            'search_stage_filter': _stage_filter}",
  "results = []",
  "for case in payload['cases']:",
  "    if case['tool'] == 'load_error':",
  "        results.append(json.dumps(_load_error(case['snapshot_text']),",
  "                                  sort_keys=True, ensure_ascii=True,",
  "                                  separators=(',', ':'), allow_nan=False))",
  "        continue",
  "    snapshot = load_snapshot(case['snapshot_text'])",
  "    handler = handlers[case['tool']]",
  "    extra = {} if case.get('now') is None else {'now': case['now']}",
  "    results.append(json.dumps(handler(snapshot, case['args'], **extra),",
  "                              sort_keys=True, ensure_ascii=True,",
  "                              separators=(',', ':'), allow_nan=False))",
  "print(json.dumps({'origin': query_module.__file__, 'results': results}))"
].join("\n");

// Private origin guard for the live reference probe. All four `assertQueryParity`
// call sites go through it, so it is the single place that decides whether the
// imported Python module really is this repository's copy.
//
// Both sides are resolved with `fs.realpathSync` first, so a symlink cannot
// smuggle a foreign module in under an in-root name. Containment is then decided
// with `path.relative` instead of a raw string prefix: a prefix test accepts any
// sibling whose name merely begins with the root's name (`<root>-old`,
// `<root>.bak`), which is the sibling-prefix path-traversal class (CWE-22).
// `path.relative` yields "" for the root itself, a leading `..` segment for
// anything above it, and a plain descendant path otherwise, so the check follows
// real path boundaries and the platform `path.sep`. The `path.isAbsolute` clause
// covers the Windows different-drive result, which is returned absolute rather
// than as a `..` path.
function assertReferenceModuleOrigin(originPath, repoRoot) {
  const resolvedOrigin = fs.realpathSync(originPath);
  const resolvedRoot = fs.realpathSync(repoRoot);
  const relative = path.relative(resolvedRoot, resolvedOrigin);
  const inside = relative !== "" && relative !== ".." &&
    !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative);
  assert.ok(inside,
    `the reference module is this repository's copy: ${originPath} ` +
    `(resolved ${resolvedOrigin}, root ${resolvedRoot})`);
  return resolvedOrigin;
}

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
  assertReferenceModuleOrigin(parsed.origin, REPO_ROOT);
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

describe("live reference origin guard", () => {
  it("accepts only resolved paths that name the repository root or its descendants", () => {
    // Synthetic trees live under this run's owned TMPDIR, never beside the real
    // project, and are removed before the test returns.
    const base = fs.mkdtempSync(path.join(os.tmpdir(), "unit7e-origin-"));
    try {
      const repoRoot = path.join(base, "repo");
      const moduleDir = path.join(repoRoot, "watchdog_catalog");
      fs.mkdirSync(moduleDir, {recursive: true});
      const modulePath = path.join(moduleDir, "query.py");
      fs.writeFileSync(modulePath, "# synthetic in-root reference module\n");
      // A real sibling directory whose name starts with the root's name -- the
      // exact shape a raw `startsWith` comparison cannot distinguish.
      const siblingDir = path.join(base, "repo-old", "watchdog_catalog");
      fs.mkdirSync(siblingDir, {recursive: true});
      const siblingPath = path.join(siblingDir, "query.py");
      fs.writeFileSync(siblingPath, "# synthetic same-prefix sibling module\n");
      // In-root symlink whose resolved target escapes the root.
      const escapingLink = path.join(moduleDir, "escaped.py");
      fs.symlinkSync(siblingPath, escapingLink);

      // Positive control through the real guard: an in-root module is accepted
      // and reported by its resolved path.
      assert.equal(assertReferenceModuleOrigin(modulePath, repoRoot),
                   fs.realpathSync(modulePath),
                   "in-root module accepted by its resolved path");
      // Second positive control: the actual repository module this guard serves
      // (the four assertQueryParity call sites depend on it).
      assert.equal(assertReferenceModuleOrigin(
                     path.join(REPO_ROOT, "watchdog_catalog", "query.py"),
                     REPO_ROOT),
                   fs.realpathSync(
                     path.join(REPO_ROOT, "watchdog_catalog", "query.py")),
                   "the repository's own reference module is accepted");
      // Premise, not the guard: the sibling really does share the raw prefix, so
      // the rejections below isolate boundary semantics rather than spelling.
      assert.ok(fs.realpathSync(siblingPath).startsWith(fs.realpathSync(repoRoot)),
                "premise: the same-prefix sibling shares the resolved raw prefix");
      // The guard must reject the prefix-sharing sibling and the resolved escape.
      for (const rejected of [siblingPath, escapingLink]) {
        assert.throws(() => assertReferenceModuleOrigin(rejected, repoRoot),
                      {name: "AssertionError",
                       message: /the reference module is this repository's copy/},
                      `${rejected} is not this repository's copy`);
      }
    } finally {
      fs.rmSync(base, {recursive: true, force: true});
    }
  });
});

// --------------------------------------------------------------------------
// pure get_model: exact provider + literal raw id
// --------------------------------------------------------------------------
//
// `catalog.getModel(snapshot, args)` is the dependency-free counterpart of
// `watchdog_catalog.query.get_model`. It takes an already-VALIDATED public
// snapshot and the whole DECODED argument object, returns the flat success
// payload or the shared error envelope, never raises for a malformed argument
// and never mutates its inputs. The literal success rows are driven by the
// unchanged contract-parity owner; the owners here pin the exact pair identity,
// the flat details row, the not_found envelope, the argument boundary and
// output freshness.
describe("pure get_model query", () => {
  it("returns the exact pair's flat details and provider-scoped staleness", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const before = catalog.canonicalText(lookup);

    const result = catalog.getModel(lookup, {
      provider: "synthetic-a", model_id: "vendor/model:free"});
    assert.deepEqual(Object.keys(result).sort(), [
      "endpoint", "ok", "provider_staleness", "snapshot_id", "tick_epoch"]);
    assert.equal(result.ok, true);
    assert.equal(result.snapshot_id, lookup.snapshot_id);
    assert.equal(result.tick_epoch, lookup.tick_epoch);
    assert.deepEqual(result.provider_staleness, {"synthetic-a": false});
    assert.deepEqual(result.endpoint, {
      provider: "synthetic-a",
      model_id: "vendor/model:free",
      name: "Vendor Model",
      description: "Oracle case 1 alpha endpoint: sufficient context, image input.",
      context_length: 131072,
      input_modalities: ["text", "image"],
      output_modalities: ["text"],
      supported_parameters: ["temperature", "max_tokens"],
      chat_completions_url:
        "https://synthetic-alpha.example/v1/chat/completions",
      api_type: "openai"
    });
    assert.equal(catalog.canonicalText(lookup), before);

    // A raw id tracked by two providers is TWO endpoint identities; each call
    // returns that pair's OWN metadata and its own staleness scope.
    const alpha = catalog.getModel(lookup, {
      provider: "synthetic-a", model_id: "vendor/shared"});
    const beta = catalog.getModel(lookup, {
      provider: "synthetic-b", model_id: "vendor/shared"});
    assert.equal(alpha.endpoint.context_length, 131072);
    assert.deepEqual(alpha.endpoint.input_modalities, ["text"]);
    assert.deepEqual(alpha.endpoint.supported_parameters,
                     ["temperature", "max_tokens"]);
    assert.equal(beta.endpoint.context_length, 32768);
    assert.deepEqual(beta.endpoint.input_modalities, ["text", "image"]);
    assert.deepEqual(beta.endpoint.supported_parameters,
                     ["temperature", "top_p"]);
    assert.deepEqual(alpha.provider_staleness, {"synthetic-a": false});
    assert.deepEqual(beta.provider_staleness, {"synthetic-b": false});
  });

  it("keeps ids literal and returns the fixed not_found envelope", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const notFound = {
      ok: false,
      error: {code: "not_found", message: "Model not found."}
    };
    for (const modelId of ["vendor/model", "vendor/model-free", "VENDOR/SHARED",
                           " vendor/shared", "vendor/shared ",
                           "vendor/does-not-exist", "\u00a0", "\u2003"]) {
      const result = catalog.getModel(lookup, {
        provider: "synthetic-a", model_id: modelId});
      assert.deepEqual(result, notFound, JSON.stringify(modelId));
      assert.deepEqual(Object.keys(result.error).sort(), ["code", "message"]);
    }
    // A registered zero-ID provider is a KNOWN provider: every id is absent.
    assert.deepEqual(catalog.getModel(lookup, {
      provider: "synthetic-c", model_id: "vendor/shared"}), notFound);
  });

  it("pins the provider-first argument boundary", () => {
    const lookup = acceptedSnapshot("lookup.json");
    for (const args of [null, [], 5, "x", false, 1.0, "{}", undefined]) {
      assertArgumentEnvelope(catalog.getModel(lookup, args),
                             "invalid_arguments", null, `object ${args}`);
    }
    for (const args of [{bogus: 1},
                        {provider: "synthetic-a", bogus: 1},
                        {provider: "synthetic-a", model_id: "vendor/shared",
                         limit: 5},
                        {provider: "synthetic-z", bogus: 1}]) {
      assertArgumentEnvelope(catalog.getModel(lookup, args),
                             "invalid_arguments", null, JSON.stringify(args));
    }
    // provider: missing or wrong-typed is invalid_arguments(provider).
    assertArgumentEnvelope(catalog.getModel(lookup, {model_id: "vendor/shared"}),
                           "invalid_arguments", "provider", "missing");
    for (const bad of [null, true, false, 5, 1.5, [], ["synthetic-a"], {}]) {
      assertArgumentEnvelope(catalog.getModel(lookup, {
        provider: bad, model_id: "vendor/shared"}),
        "invalid_arguments", "provider", String(bad));
    }
    for (const bad of ["", " synthetic-a ", "synthetic-a ", " synthetic-a",
                       "Synthetic-A", "SYNTHETIC-A", "synthetic-z", "unknown"]) {
      assertArgumentEnvelope(catalog.getModel(lookup, {
        provider: bad, model_id: "vendor/shared"}),
        "unknown_provider", "provider", JSON.stringify(bad));
    }
    // Provider membership precedes the model_id check.
    assertArgumentEnvelope(catalog.getModel(lookup, {provider: "synthetic-z"}),
                           "unknown_provider", "provider", "unknown first");
    assertArgumentEnvelope(catalog.getModel(lookup, {
      provider: "synthetic-z", model_id: ""}),
      "unknown_provider", "provider", "unknown before blank");

    // model_id: missing or wrong-typed is invalid_arguments(model_id).
    assertArgumentEnvelope(catalog.getModel(lookup, {provider: "synthetic-a"}),
                           "invalid_arguments", "model_id", "missing");
    for (const bad of [null, true, false, 5, 1.5, [], ["vendor/shared"], {},
                       {a: 1}]) {
      assertArgumentEnvelope(catalog.getModel(lookup, {
        provider: "synthetic-a", model_id: bad}),
        "invalid_arguments", "model_id", String(bad));
    }
    for (const blank of ["", " ", "\t", "\n", "\r", "\u000b", "\f",
                         " \t\n\r\u000b\f", "\t \n", "  "]) {
      assertArgumentEnvelope(catalog.getModel(lookup, {
        provider: "synthetic-a", model_id: blank}),
        "invalid_arguments", "model_id", JSON.stringify(blank));
    }
  });

  it("returns fresh containers and never aliases the snapshot", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const before = catalog.canonicalText(lookup);
    const args = {provider: "synthetic-a", model_id: "vendor/shared"};
    const first = catalog.getModel(lookup, args);
    const second = catalog.getModel(lookup, args);
    assert.deepEqual(first, second);
    assert.notEqual(first.endpoint, second.endpoint);
    assert.notEqual(first.endpoint.input_modalities,
                    second.endpoint.input_modalities);
    assert.notEqual(first.provider_staleness, second.provider_staleness);

    first.endpoint.input_modalities.push("vision");
    first.endpoint.supported_parameters.push("seed");
    first.provider_staleness["synthetic-a"] = true;
    assert.equal(catalog.canonicalText(lookup), before);
    assert.deepEqual(args, {provider: "synthetic-a",
                            model_id: "vendor/shared"});
    assert.deepEqual(second.endpoint.input_modalities, ["text"]);
    assert.deepEqual(second.endpoint.supported_parameters,
                     ["temperature", "max_tokens"]);
    assert.deepEqual(second.provider_staleness, {"synthetic-a": false});

    const firstError = catalog.getModel(lookup, {
      provider: "synthetic-a", model_id: ""});
    firstError.error.code = "mutated";
    firstError.ok = true;
    const secondError = catalog.getModel(lookup, {
      provider: "synthetic-a", model_id: ""});
    assertArgumentEnvelope(secondError, "invalid_arguments", "model_id",
                           "fresh error");
    assert.notEqual(secondError, firstError);

    // A real empty registry has no known provider at all.
    assertArgumentEnvelope(
      catalog.getModel(emptyRegistrySnapshot(),
                       {provider: "synthetic-a", model_id: "vendor/shared"}),
      "unknown_provider", "provider", "empty registry");
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
       JSON.parse("{\"provider\":\"synthetic-a\",\"bogus\":1}")],
      ["boundary-non-object", 5],
      ["boundary-missing-provider", {}],
      ["boundary-missing-id", {provider: "synthetic-a"}],
      ["boundary-empty-id", {provider: "synthetic-a", model_id: ""}],
      ["boundary-ascii-blank",
       {provider: "synthetic-a", model_id: " \t\n\r\u000b\f"}],
      ["boundary-nbsp", {provider: "synthetic-a", model_id: "\u00a0"}],
      ["boundary-em-space", {provider: "synthetic-a", model_id: "\u2003"}],
      ["boundary-nul", {provider: "synthetic-a", model_id: "\u0000"}],
      ["boundary-padded", {provider: "synthetic-a",
                           model_id: " vendor/shared "}],
      ["boundary-case", {provider: "synthetic-a",
                         model_id: "VENDOR/SHARED"}],
      ["boundary-marker", {provider: "synthetic-a", model_id: "vendor/model"}],
      ["boundary-unknown-provider",
       {provider: "synthetic-z", model_id: "vendor/shared"}],
      ["boundary-no-match", {provider: "synthetic-a",
                             model_id: "vendor/does-not-exist"}]
    ]) {
      cases.push({label: entry[0], snapshot_text: lookupText, args: entry[1]});
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
// case -- the clock is never a tool argument and never an ambient read. The
// `cases.json` / `argument-cases.json` rows are executed by
// `tests/test_contract_parity.py`; every expected payload below is a
// hand-written literal from README's `Success envelope — watchdog_status` and
// argument tables.

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

    // Interleaving the other tools neither sees nor supplies state.
    const statusA = catalog.watchdogStatus(lookup, {}, 1759200000);
    const model = catalog.getModel(lookup, {
      provider: "synthetic-a", model_id: "vendor/shared"});
    const statusB = catalog.watchdogStatus(lookup, {}, 1759200000);
    assertSameValue(statusA, statusB, "interleaved status calls agree");
    statusB.provider_counts["synthetic-b"] = 42;
    statusB.warnings.push("injected");
    assert.equal(model.endpoint.model_id, "vendor/shared");
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
                            "provider_health"]) {
        assert.equal(Object.prototype.hasOwnProperty.call(payload, absent),
                     false, `no ${absent} field`);
      }
    }
  });

  it("accepts only the empty argument object", () => {
    const lookup = acceptedSnapshot("lookup.json");
    const frozen = catalog.canonicalText(lookup);

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
// pure metadata-search pipeline stages (internal, unpaginated)
// --------------------------------------------------------------------------
//
// `search_models`' INTERNAL pipeline is four pure stages: candidate selection
// by literal text and exact provider, then INCLUSIVE minimum context, then
// input-modality AND, then supported-parameter AND. Each filter stage narrows an
// ALREADY selected ordered list of `[provider, model_id]` identities; none of
// them re-derives a global selection, sorts, deduplicates or paginates. Public
// argument decoding, success/error envelopes and pagination belong to a later
// unit, so every expectation below is a literal identity array taken from the
// INDEPENDENTLY authored `cases.json` goldens, a hand-authored boundary row, or
// a live comparison against the unchanged Python reference -- agreement between
// the two runtimes alone is never treated as proof of correctness.

const SEARCH_STAGE_KEYS = new Set(["query", "provider", "min_context_length",
                                   "input_modalities", "supported_parameters"]);

/** The five stage arguments, with every omitted member resolved to `null`. */
function stageArguments(args) {
  const stage = {query: null, provider: null, min_context_length: null,
                 input_modalities: null, supported_parameters: null};
  for (const key of Object.keys(args || {})) {
    if (SEARCH_STAGE_KEYS.has(key)) {
      stage[key] = args[key];
    }
  }
  return stage;
}

/** The `[provider, model_id]` identity of one selected pair, as a fresh array. */
function pairIdentity(pair) {
  return [pair[0], pair[1]];
}

function searchRows() {
  return QUERY_CASES.filter((row) => row.tool === "search_models");
}

/**
 * The frozen `search_models` rows usable as a COMPLETE stage oracle: their
 * arguments are confined to the five stage keys and their whole match set fits
 * the default page. A paginated or page-capped row stays a later public-envelope
 * oracle and must never silently widen this set.
 */
function completeStageRows() {
  return searchRows().filter((row) =>
    Object.keys(row.args).every((key) => SEARCH_STAGE_KEYS.has(key)) &&
    row.expect.next_offset === null &&
    row.expect.total_matches === row.expect.endpoints.length);
}

function rowSnapshot(row) {
  return acceptedSnapshot(row.fixture === undefined ? QUERY_FIXTURE
                                                   : row.fixture);
}

/** Compose the four internal stages exactly as the accepted Python pipeline. */
function composeSearchStages(snapshot, args) {
  const stage = stageArguments(args);
  let pairs = catalog.searchTextProviderPairs(
    snapshot, {query: stage.query, provider: stage.provider});
  pairs = catalog.searchMinContextPairs(snapshot, pairs,
                                        stage.min_context_length);
  pairs = catalog.searchInputModalityPairs(snapshot, pairs,
                                            stage.input_modalities);
  return catalog.searchSupportedParameterPairs(snapshot, pairs,
                                               stage.supported_parameters);
}

// -- synthetic, fully-validated search registries ---------------------------
//
// These are CONSISTENCY controls: they are re-sealed with the canonical digest
// of the implementation under test, so they prove the stages work over input the
// REAL validator accepts, never that the digest itself is independent. The
// committed literals and the live Python comparison stay the independent checks.

/** Literal Unicode code-point order, as the frozen provider shape requires. */
function codePointCompare(left, right) {
  const a = Array.from(left);
  const b = Array.from(right);
  const shared = Math.min(a.length, b.length);
  for (let index = 0; index < shared; index += 1) {
    const x = a[index].codePointAt(0);
    const y = b[index].codePointAt(0);
    if (x !== y) {
      return x < y ? -1 : 1;
    }
  }
  return a.length === b.length ? 0 : (a.length < b.length ? -1 : 1);
}

/** One complete projected public model object (the accepted model shape). */
function searchModel(rawId, name, options) {
  const opts = options || {};
  const pick = (key, fallback) => (key in opts ? opts[key] : fallback);
  return {
    id: rawId,
    name: name,
    description: pick("description", null),
    context_length: pick("context_length", null),
    architecture: {
      input_modalities: pick("input_modalities", null),
      output_modalities: pick("output_modalities", null)
    },
    supported_parameters: pick("supported_parameters", null)
  };
}

/** One provider entry with its raw-ID array in the required literal order. */
function searchProvider(models, displayName, url, options) {
  const opts = options || {};
  const ids = Object.keys(models).sort(codePointCompare);
  const ordered = {};
  for (const rawId of ids) {
    withOwnKey(ordered, rawId, models[rawId]);
  }
  return {
    display_name: displayName,
    chat_completions_url: url,
    api_type: opts.api_type === undefined ? "openai" : opts.api_type,
    model_ids: ids,
    models: ordered,
    stale: opts.stale === undefined ? false : opts.stale
  };
}

/** A resealed synthetic snapshot the REAL validator accepts. */
function searchSnapshot(providerOrder, providers) {
  const ordered = {};
  for (const key of Object.keys(providers)) {
    withOwnKey(ordered, key, providers[key]);
  }
  return resealSnapshot({
    schema_version: 2,
    snapshot_id: "0".repeat(64),
    tick_epoch: 1759200000,
    cadence_s: 3600,
    provider_order: providerOrder.slice(),
    providers: ordered,
    warnings: []
  });
}

/**
 * The text/provider boundary registry: provider order, provider-map insertion
 * order and literal raw-ID order are three DIFFERENT orders, so the pinned
 * sequences can only come from `provider_order` precedence plus natural order.
 */
const SEARCH_ZETA_ORDER = [
  ["zeta", "m-1"], ["zeta", "m-1a"], ["zeta", "m-2"], ["zeta", "m-9"],
  ["zeta", "m-01"], ["zeta", "m-10"],
  ["zeta", "m-" + "0".repeat(29) + "1"],
  ["zeta", "m-\u00b2"], ["zeta", "m-\u0661"]
];
const SEARCH_ALPHA_ORDER = [
  ["Alpha", "ascii-case"], ["Alpha", "caps-only"], ["Alpha", "fusion-left"],
  ["Alpha", "idtoken-model"], ["Alpha", "literal-regex"],
  ["Alpha", "name-target"], ["Alpha", "shared-id"], ["Alpha", "unicode-upper"],
  ["Alpha", "vendor/tool:free"]
];
const SEARCH_ALPHA_LOWER_ORDER = [
  ["alpha", "shared-id"], ["alpha", "unicode-lower"], ["alpha", "vendor/tool"]
];
const SEARCH_BULK_PAIRS = Array.from({length: 55}, (_, index) => [
  "bulk", "bulk-" + String(index).padStart(2, "0")
]);
const SEARCH_ALL_PAIRS = SEARCH_ZETA_ORDER.concat(
  SEARCH_ALPHA_ORDER, SEARCH_ALPHA_LOWER_ORDER, SEARCH_BULK_PAIRS);

function searchBoundarySnapshot() {
  const zetaModels = {};
  const zetaRows = [
    ["m-1", "Order A"], ["m-01", "Order B"], ["m-1a", "Order C"],
    ["m-2", "Order D"], ["m-9", "Order E"], ["m-10", "Order F"],
    ["m-" + "0".repeat(29) + "1", "Order G"], ["m-\u00b2", "Order H"],
    ["m-\u0661", "Order I"]
  ];
  for (const [rawId, name] of zetaRows) {
    withOwnKey(zetaModels, rawId, searchModel(rawId, name));
  }
  const upper = {
    "ascii-case": searchModel("ascii-case", "MiXeD Name"),
    "caps-only": searchModel("caps-only", "Caps Only", {
      description: "plain synthetic prose", context_length: 7,
      input_modalities: ["ImageToken"], output_modalities: ["OutToken"],
      supported_parameters: ["ParamToken"]
    }),
    "fusion-left": searchModel("fusion-left", "Alpha", {
      description: "Beta prose body."
    }),
    "idtoken-model": searchModel("idtoken-model", "Alpha Display One", {
      description: "descword one."
    }),
    "literal-regex": searchModel("literal-regex", "paren (test)", {
      description: "literal [ab].* marker"
    }),
    "name-target": searchModel("name-target", "NameToken Display", {
      description: "Alpha prose two."
    }),
    "shared-id": searchModel("shared-id", "Borrow Target"),
    "unicode-upper": searchModel("unicode-upper", "\u00c9CHO"),
    "vendor/tool:free": searchModel("vendor/tool:free", "Tool Free Display")
  };
  const lower = {
    "shared-id": searchModel("shared-id", "Other Label", {
      description: "Sibling prose."
    }),
    "unicode-lower": searchModel("unicode-lower", "\u00e9cho"),
    "vendor/tool": searchModel("vendor/tool", "Tool Sibling")
  };
  const bulk = {};
  for (let index = 0; index < 55; index += 1) {
    const rawId = "bulk-" + String(index).padStart(2, "0");
    withOwnKey(bulk, rawId, searchModel(rawId,
      "Bulk row " + String(index).padStart(2, "0"), {
        description: "Bulk synthetic row."
      }));
  }
  // Insertion order here is deliberately unrelated to provider_order.
  const providers = {
    empty: searchProvider({}, "Empty Gateway",
                          "https://empty.example/v1/chat/completions"),
    bulk: searchProvider(bulk, "Bulk Gateway",
                         "https://bulk.example/v1/chat/completions"),
    alpha: searchProvider(lower, "Lower Alpha",
                          "https://alpha.example/v1/chat/completions"),
    zeta: searchProvider(zetaModels, "Zeta Order",
                         "https://zeta.example/v1/chat/completions"),
    Alpha: searchProvider(upper, "Upper Alpha",
                          "https://urltoken.example/v1/chat/completions",
                          {api_type: "apitoken"})
  };
  return searchSnapshot(["zeta", "Alpha", "alpha", "bulk", "empty"], providers);
}

//: Compact (query, provider, expected ordered identities) boundary rows.
const SEARCH_TEXT_BOUNDARY_ROWS = [
  // Omitted or empty query applies NO text constraint; whitespace is literal.
  [null, null, SEARCH_ALL_PAIRS],
  ["", null, SEARCH_ALL_PAIRS],
  ["   ", null, []],
  ["   ", "zeta", []],
  // Per-pair text only: a shared raw ID never borrows a sibling's name or
  // description, and provider scope ANDs with the text predicate.
  ["borrow", null, [["Alpha", "shared-id"]]],
  ["borrow", "Alpha", [["Alpha", "shared-id"]]],
  ["borrow", "alpha", []],
  ["other label", null, [["alpha", "shared-id"]]],
  ["shared-id", null, [["Alpha", "shared-id"], ["alpha", "shared-id"]]],
  // Each individual field matches on its own: raw ID, name, description.
  ["idtoken", null, [["Alpha", "idtoken-model"]]],
  ["nametoken", null, [["Alpha", "name-target"]]],
  ["descword", null, [["Alpha", "idtoken-model"]]],
  // A substring spanning a field boundary does not match: "Alpha" (name) and
  // "Beta prose body." (description) are never concatenated.
  ["Alpha Beta", null, []],
  // ASCII-only fold: ASCII letters fold, non-ASCII case pairs stay literal.
  ["mixed name", null, [["Alpha", "ascii-case"]]],
  ["MIXED NAME", null, [["Alpha", "ascii-case"]]],
  ["\u00c9CHO", null, [["Alpha", "unicode-upper"]]],
  ["\u00e9cho", null, [["alpha", "unicode-lower"]]],
  ["\u00e9cho", "Alpha", []],
  // Literal regex-looking and invalid-regex queries; literal parentheses.
  ["[ab].*", null, [["Alpha", "literal-regex"]]],
  ["(", null, [["Alpha", "literal-regex"]]],
  // Never trimmed and never marker-normalized.
  ["Caps Only", null, [["Alpha", "caps-only"]]],
  ["Caps Only ", null, []],
  ["vendor/tool", null, [["Alpha", "vendor/tool:free"],
                          ["alpha", "vendor/tool"]]],
  ["vendor/tool", "alpha", [["alpha", "vendor/tool"]]],
  ["vendor/tool:free", null, [["Alpha", "vendor/tool:free"]]],
  // A null description is NOT a text field: neither Python's `str(None)`
  // spelling nor the JSON spelling may match a null.
  ["none", null, []],
  ["null", null, []],
  // Capability arrays, wiring URL, provider keys and object key names
  // are never searched.
  ["ImageToken", null, []],
  ["ParamToken", null, []],
  ["urltoken", null, []],
  ["apitoken", null, []],
  ["https", null, []],
  ["zeta", null, []],
  ["context_length", null, []],
  ["architecture", null, []],
  ["supported_parameters", null, []],
  ["display_name", null, []],
  // Exact provider scoping, including differently-cased VALID keys and a
  // registered zero-ID provider.
  [null, "zeta", SEARCH_ZETA_ORDER],
  [null, "Alpha", SEARCH_ALPHA_ORDER],
  [null, "alpha", SEARCH_ALPHA_LOWER_ORDER],
  [null, "empty", []],
  ["bulk", null, SEARCH_BULK_PAIRS]
];

/**
 * The astral text registry: ONE provider whose three rows put a valid astral
 * character in a different field each -- the raw id, the name and a non-null
 * description -- using two DIFFERENT astral characters, so a lone-surrogate
 * query cannot pass by matching one chosen emoji.
 *
 * Every expectation is the accepted Python rule: `query` is a literal,
 * untrimmed, ASCII-only-folded CODE-POINT substring of the pair's own raw id,
 * name or non-null description. A valid astral character is ONE code point but
 * TWO UTF-16 code units, so a query holding only its high or low half is not a
 * substring of it -- while the whole character is.
 */
const SEARCH_ASTRAL_ROWS = [
  // Inactive text: unchanged provider order and natural raw-ID order.
  ["empty", "", [["greek", "desc-target"], ["greek", "name-target"],
                 ["greek", "scalar-\u{1F31F}"]]],
  // The whole astral character matches the raw ID row AND the name row.
  ["astral", "\u{1F31F}", [["greek", "name-target"],
                           ["greek", "scalar-\u{1F31F}"]]],
  // Scoped to one field by the surrounding literal text.
  ["astral-raw", "scalar-\u{1F31F}", [["greek", "scalar-\u{1F31F}"]]],
  ["astral-name", "\u{1F31F} n", [["greek", "name-target"]]],
  // The ASCII-only fold still applies to an astral-bearing query and field.
  ["astral-upper-prefix", "SCALAR-", [["greek", "scalar-\u{1F31F}"]]],
  ["astral-upper", "SCALAR-\u{1F31F}", [["greek", "scalar-\u{1F31F}"]]],
  // A DIFFERENT astral character, matched through the description.
  ["clef", "\u{1D11E}", [["greek", "desc-target"]]],
  ["clef-prose", "\u{1D11E} p", [["greek", "desc-target"]]],
  // ... whose own halves are not substrings of it either: a general rule, not
  // one blacklisted emoji or two specific code units.
  ["clef-high-half", "\uD834", []],
  ["clef-low-half", "\uDD1E", []],
  // The reported regression: each isolated half of U+1F31F is NOT a substring
  // of the valid astral character, exactly like Python's code-point `in`.
  ["high-half", "\uD83C", []],
  ["low-half", "\uDF1F", []],
  // Near misses: a half plus an ordinary character stays literal.
  ["high-half-plus-x", "\uD83Cx", []],
  ["low-half-prefix", "scalar-\uDF1F", []],
  ["low-half-plus-name", "\uDF1F n", []]
];

/**
 * A resealed, VALIDATED snapshot carrying two different astral characters in
 * three different fields of ONE provider. `searchSnapshot` re-seals through the
 * accepted validator and hash, and the live Python dispatcher re-loads the same
 * canonical text with the accepted `load_snapshot` before calling the reference
 * stage, so BOTH runtimes accept this snapshot independently.
 */
function searchAstralSnapshot() {
  const astral = "\u{1F31F}";
  const clef = "\u{1D11E}";
  const models = {};
  withOwnKey(models, "scalar-" + astral,
             searchModel("scalar-" + astral, "Scalar One"));
  withOwnKey(models, "name-target",
             searchModel("name-target", "Scalar " + astral + " Name",
                         {description: "Plain prose."}));
  withOwnKey(models, "desc-target",
             searchModel("desc-target", "Desc Target",
                         {description: "Clef " + clef + " prose."}));
  return searchSnapshot(["greek"], {
    greek: searchProvider(models, "Greek Gateway",
                          "https://greek.example/v1/chat/completions")
  });
}

/**
 * The context/modality/parameter boundary registry: known 0, known high values,
 * unknown (null) metadata, known-empty arrays, per-provider differing metadata
 * behind a SHARED raw ID and a 210-endpoint bulk provider.
 */
function searchCapabilitySnapshot() {
  const gate = {
    both: searchModel("both", "Both Modalities", {
      context_length: 65536, input_modalities: ["text", "image"],
      supported_parameters: ["temperature", "top_p"]
    }),
    "text-only": searchModel("text-only", "Text Only", {
      context_length: 32768, input_modalities: ["text"],
      supported_parameters: ["temperature"]
    }),
    "empty-known": searchModel("empty-known", "Empty Known", {
      context_length: 0, input_modalities: [],
      supported_parameters: []
    }),
    "unknown-array": searchModel("unknown-array", "Unknown Array", {
      context_length: null, input_modalities: null,
      supported_parameters: null
    }),
    "cased-dupes": searchModel("cased-dupes", "Cased Dupes", {
      context_length: 1, input_modalities: ["TEXT", "Image", "text", "image"],
      supported_parameters: ["Top_P", "top_p"]
    }),
    padded: searchModel("padded", "Padded", {
      context_length: 65535, input_modalities: [" text "],
      supported_parameters: [" temperature "]
    }),
    "unicode-upper": searchModel("unicode-upper", "Unicode Upper", {
      context_length: 65537, input_modalities: ["\u00c9"],
      supported_parameters: ["\u00c9"]
    }),
    "output-image": searchModel("output-image", "Output Image", {
      context_length: 65536, input_modalities: ["text"],
      output_modalities: ["image"], supported_parameters: ["temperature"]
    }),
    "name-image": searchModel("name-image", "Image Name", {
      description: "image prose", context_length: 65536,
      input_modalities: ["text"], supported_parameters: ["temperature"]
    }),
    "param-image": searchModel("param-image", "Param Image", {
      context_length: 65536, input_modalities: ["text"],
      supported_parameters: ["image_size"]
    }),

    "raw-id-image": searchModel("raw-id-image", "Raw Id Image", {
      context_length: 65536, input_modalities: ["text"],
      supported_parameters: ["temperature"]
    }),
    "shared-id": searchModel("shared-id", "Shared A", {
      context_length: 65536, input_modalities: ["text", "image"],
      supported_parameters: ["temperature"]
    }),
    "max-context": searchModel("max-context", "Max Context", {
      context_length: catalog.MAX_SAFE_INT, input_modalities: ["text"],
      supported_parameters: ["temperature"]
    })
  };
  const other = {
    "shared-id": searchModel("shared-id", "Shared B", {
      context_length: 32768, input_modalities: ["text"],
      supported_parameters: ["top_p"]
    })
  };
  const bulk = {};
  for (let index = 0; index < 210; index += 1) {
    const rawId = "bulk-" + String(index).padStart(3, "0");
    withOwnKey(bulk, rawId, searchModel(rawId,
      "Bulk " + String(index).padStart(3, "0"), {
        context_length: 65536, input_modalities: ["text"],
        supported_parameters: ["temperature"]
      }));
  }
  return searchSnapshot(["gate", "other", "bulk", "zero-provd"], {
    gate: searchProvider(gate, "Gate",
                         "https://gate.example/v1/chat/completions"),
    other: searchProvider(other, "Other",
                          "https://other.example/v1/chat/completions"),
    bulk: searchProvider(bulk, "Bulk",
                         "https://bulk.example/v1/chat/completions"),
    "zero-provd": searchProvider({}, "Zero",
                                 "https://zero.example/v1/chat/completions")
  });
}

/**
 * A validated registry whose keys LOOK like prototype members, whose shared raw
 * ID carries deliberately different metadata per provider, and which mixes a
 * 60-digit run, non-ASCII digits, an astral id, known-empty and unknown arrays.
 */
function adversarialSearchSnapshot() {
  const proto = {};
  withOwnKey(proto, "__proto__", searchModel("__proto__", "Proto Model", {
    context_length: 0, input_modalities: ["text"],
    supported_parameters: ["temperature"]
  }));
  withOwnKey(proto, "constructor", searchModel("constructor",
                                               "Constructor Model", {
    description: "proto prose", input_modalities: [], supported_parameters: []
  }));
  withOwnKey(proto, "dup-id", searchModel("dup-id", "Proto Dup", {
    context_length: 65536, input_modalities: ["text", "image"],
    supported_parameters: ["temperature", "top_p"]
  }));
  withOwnKey(proto, "num-" + "0".repeat(59) + "9",
             searchModel("num-" + "0".repeat(59) + "9", "Long Run", {
               context_length: 65536, input_modalities: ["text"]
             }));
  withOwnKey(proto, "num-9", searchModel("num-9", "Short Run", {
    context_length: 65536, input_modalities: ["text"]
  }));
  withOwnKey(proto, "\ud83c\udf1f-model",
             searchModel("\ud83c\udf1f-model", "Astral Model", {
               context_length: 1, input_modalities: ["text"],
               supported_parameters: ["temperature"]
             }));
  const sibling = {};
  withOwnKey(sibling, "dup-id", searchModel("dup-id", "Sibling Dup", {
    context_length: 32768, input_modalities: ["text"],
    supported_parameters: ["top_p"]
  }));
  withOwnKey(sibling, "toString", searchModel("toString", "ToString Model", {
    context_length: null, input_modalities: null, supported_parameters: null
  }));
  withOwnKey(sibling, "\u00c9-model", searchModel("\u00c9-model", "\u00c9CHO", {
    context_length: 0, input_modalities: ["image"],
    supported_parameters: ["\u00c9"]
  }));
  withOwnKey(sibling, "\u00e9-model", searchModel("\u00e9-model", "\u00e9cho", {
    context_length: 0, input_modalities: ["image"],
    supported_parameters: ["\u00e9"]
  }));
  const providers = {};
  withOwnKey(providers, "__proto__", searchProvider(
    proto, "Proto Gateway", "https://proto.example/v1/chat/completions"));
  withOwnKey(providers, "toString", searchProvider(
    sibling, "ToString Gateway", "https://to-string.example/v1/chat/completions",
    {api_type: "anthropic", stale: true}));
  return searchSnapshot(["__proto__", "toString"], providers);
}

/** The exact document a wrong-schema loader refusal must reject. */
function wrongSchemaSnapshotText() {
  const body = typedClone(acceptedSnapshot("lookup.json"));
  body.schema_version = 3;
  body.snapshot_id = catalog.snapshotHash(body);
  return catalog.canonicalText(body);
}

// -- stage parity against the unchanged Python reference --------------------

/** One `search_stage_pairs` (composition) parity case. */
function stagePairCase(label, snapshot, args) {
  return {label: label, tool: "search_stage_pairs",
          snapshot_text: catalog.canonicalText(snapshot),
          args: stageArguments(args), now: null};
}

/** One `search_stage_filter` (supplied-selection) parity case. */
function stageFilterCase(label, snapshot, pairs, args) {
  const stage = stageArguments(args);
  return {label: label, tool: "search_stage_filter",
          snapshot_text: catalog.canonicalText(snapshot),
          args: {pairs: pairs.map(pairIdentity),
                 min_context_length: stage.min_context_length,
                 input_modalities: stage.input_modalities,
                 supported_parameters: stage.supported_parameters},
          now: null};
}

/** The JavaScript half of one stage parity case, in the reference's shape. */
function runJavaScriptStageCase(entry) {
  const snapshot = catalog.loadSnapshot(entry.snapshot_text);
  if (entry.tool === "search_stage_pairs") {
    return composeSearchStages(snapshot, entry.args).map(pairIdentity);
  }
  const supplied = entry.args.pairs.map(pairIdentity);
  return {
    context: catalog.searchMinContextPairs(
      snapshot, supplied, entry.args.min_context_length).map(pairIdentity),
    input: catalog.searchInputModalityPairs(
      snapshot, supplied, entry.args.input_modalities).map(pairIdentity),
    parameters: catalog.searchSupportedParameterPairs(
      snapshot, supplied, entry.args.supported_parameters).map(pairIdentity)
  };
}

function assertStageParity(cases) {
  const pythonResults = runQueryReference(cases.map((entry) => ({
    tool: entry.tool, snapshot_text: entry.snapshot_text,
    args: entry.args, now: null
  })));
  for (let index = 0; index < cases.length; index += 1) {
    assert.equal(catalog.canonicalText(runJavaScriptStageCase(cases[index])),
                 pythonResults[index],
                 `${cases[index].label}: typed canonical payload matches Python`);
  }
}

describe("pure metadata-search stage helpers", () => {
  it("pins literal ASCII-only text and exact provider scope boundaries", () => {
    const snapshot = searchBoundarySnapshot();
    // The pin is meaningful: neither provider-map insertion order nor literal
    // raw-ID order coincides with the frozen endpoint order.
    assert.notDeepEqual(snapshot.provider_order, Object.keys(snapshot.providers),
                        "provider order is not insertion order");
    assert.notDeepEqual(snapshot.provider_order, snapshot.provider_order.slice()
                                                  .sort(), "not alphabetic");
    assert.notDeepEqual(snapshot.providers.zeta.model_ids.slice().sort(),
                        SEARCH_ZETA_ORDER.map((pair) => pair[1]),
                        "not literal raw-ID order");
    const before = catalog.canonicalText(snapshot);
    for (const [query, provider, expected] of SEARCH_TEXT_BOUNDARY_ROWS) {
      const result = catalog.searchTextProviderPairs(
        snapshot, {query: query, provider: provider});
      assert.deepEqual(result.map(pairIdentity), expected,
                       `query=${JSON.stringify(query)} provider=${provider}`);
    }
    // Omitted members behave exactly like explicit nulls.
    assert.deepEqual(catalog.searchTextProviderPairs(snapshot).map(pairIdentity),
                     SEARCH_ALL_PAIRS);
    assert.deepEqual(catalog.searchTextProviderPairs(snapshot, {}).map(pairIdentity),
                     SEARCH_ALL_PAIRS);

    // More than the public default page (5) is returned WHOLE.
    const bulk = catalog.searchTextProviderPairs(snapshot, {query: "bulk"});
    assert.equal(bulk.length, 55);
    assert.deepEqual(bulk[0], ["bulk", "bulk-00"]);
    assert.deepEqual(bulk[54], ["bulk", "bulk-54"]);
    // A zero-ID registered provider and an empty registry both select nothing.
    assert.deepEqual(catalog.searchTextProviderPairs(
      searchSnapshot(["solo"], {
        solo: searchProvider({}, "Solo",
                             "https://solo.example/v1/chat/completions")
      })), []);
    assert.deepEqual(catalog.searchTextProviderPairs(snapshot,
                                                     {provider: "empty"}), []);
    assert.equal(catalog.canonicalText(snapshot), before,
                 "boundary controls never mutate the snapshot");
  });

  it("matches astral text by code point, never by an isolated surrogate half", () => {
    const snapshot = searchAstralSnapshot();
    // The shape of the class under test: each astral character is ONE code
    // point but TWO UTF-16 code units, which is exactly where a code-unit
    // search and Python's code-point `in` can disagree.
    assert.equal("\u{1F31F}".length, 2, "U+1F31F is two code units");
    assert.equal("\u{1D11E}".length, 2, "U+1D11E is two code units");
    const before = catalog.canonicalText(snapshot);
    for (const [label, query, expected] of SEARCH_ASTRAL_ROWS) {
      const result = catalog.searchTextProviderPairs(snapshot, {query: query});
      assert.deepEqual(result.map(pairIdentity), expected,
                       `${label}: query=${JSON.stringify(query)}`);
      for (const pair of result) {
        // Complete ordered literals above are compared as a whole; these
        // checks only pin the fresh two-string pair shape.
        assert.equal(Array.isArray(pair) && pair.length, 2,
                     `${label}: fresh two-string pairs`);
        assert.equal(typeof pair[0], "string", label);
        assert.equal(typeof pair[1], "string", label);
      }
    }
    // The reported regression in its shortest form: a query holding ONLY one
    // half of a valid astral character selects nothing, and the whole
    // character still selects its pair.
    assert.deepEqual(
      catalog.searchTextProviderPairs(snapshot, {query: "\uD83C"}), [],
      "an isolated high surrogate is not a substring of U+1F31F");
    assert.deepEqual(
      catalog.searchTextProviderPairs(snapshot, {query: "\uDF1F"}), [],
      "an isolated low surrogate is not a substring of U+1F31F");
    assert.deepEqual(
      catalog.searchTextProviderPairs(snapshot, {query: "\u{1F31F}"})
        .map(pairIdentity),
      [["greek", "name-target"], ["greek", "scalar-\u{1F31F}"]],
      "the whole astral character still matches");
    assert.equal(catalog.canonicalText(snapshot), before,
                 "surrogate-half controls never mutate the snapshot");
  });

  it("keeps context inclusive, unknown distinct from zero and selection order", () => {
    const snapshot = searchCapabilitySnapshot();
    const gate = (rawId) => ["gate", rawId];
    const supplied = [gate("both"), gate("unknown-array"), gate("empty-known"),
                      gate("cased-dupes"), gate("padded"),
                      gate("unicode-upper"), gate("max-context")];
    const rows = [
      // A null threshold is inactive: every supplied pair, freshly.
      [null, supplied],
      // Zero is ACTIVE: a known 0 passes, an unknown null fails.
      [0, [gate("both"), gate("empty-known"), gate("cased-dupes"),
           gate("padded"), gate("unicode-upper"), gate("max-context")]],
      // The inclusive boundary at equality, then one either side.
      [65535, [gate("both"), gate("padded"), gate("unicode-upper"),
               gate("max-context")]],
      [65536, [gate("both"), gate("unicode-upper"), gate("max-context")]],
      [65537, [gate("unicode-upper"), gate("max-context")]],
      [catalog.MAX_SAFE_INT, [gate("max-context")]],
      [catalog.MAX_SAFE_INT + 1, []]
    ];
    for (const [threshold, expected] of rows) {
      assert.deepEqual(catalog.searchMinContextPairs(
        snapshot, supplied, threshold).map(pairIdentity), expected,
        `threshold=${threshold}`);
    }
    // Omitted threshold equals an explicit null.
    assert.deepEqual(catalog.searchMinContextPairs(snapshot, supplied)
                       .map(pairIdentity), supplied);

    // Order and multiplicity of the SUPPLIED selection are preserved exactly:
    // never re-derived, never sorted, never deduplicated.
    const reversed = [gate("text-only"), gate("both"), gate("text-only"),
                      ["other", "shared-id"], gate("both")];
    assert.deepEqual(catalog.searchMinContextPairs(snapshot, reversed, 0)
                       .map(pairIdentity), reversed);
    // A shared raw ID is never resolved against another provider's metadata.
    assert.deepEqual(catalog.searchMinContextPairs(
      snapshot, [["other", "shared-id"]], 65536).map(pairIdentity), []);
    assert.deepEqual(catalog.searchMinContextPairs(
      snapshot, [gate("shared-id")], 65536).map(pairIdentity),
      [gate("shared-id")]);
    // An empty selection stays empty on both branches.
    assert.deepEqual(catalog.searchMinContextPairs(snapshot, []), []);
    assert.deepEqual(catalog.searchMinContextPairs(snapshot, [], 0), []);
  });

  it("ANDs every requested input modality on the same endpoint", () => {
    const snapshot = searchCapabilitySnapshot();
    const gate = (rawId) => ["gate", rawId];
    const rows = [
      // None and [] are inactive, retaining unknown and known-empty arrays.
      [[gate("both"), gate("unknown-array"), gate("empty-known")], null,
       [gate("both"), gate("unknown-array"), gate("empty-known")]],
      [[gate("both"), gate("unknown-array"), gate("empty-known")], [],
       [gate("both"), gate("unknown-array"), gate("empty-known")]],
      // A positive request rejects BOTH the unknown and the known-empty array.
      [[gate("both"), gate("unknown-array"), gate("empty-known")], ["text"],
       [gate("both")]],
      // AND, not OR: every requested token on the SAME pair.
      [[gate("both"), gate("text-only")], ["text"],
       [gate("both"), gate("text-only")]],
      [[gate("both"), gate("text-only")], ["text", "image"], [gate("both")]],
      [[gate("both"), gate("text-only")], ["image", "text"], [gate("both")]],
      // Duplicates are idempotent, never a multiplicity constraint.
      [[gate("both")], ["text", "text", "TEXT"], [gate("both")]],
      // ASCII-insensitive on BOTH sides; first-occurrence dupes harmless.
      [[gate("cased-dupes")], ["image", "TEXT"], [gate("cased-dupes")]],
      // Whitespace stays literal: a padded token matches only an identically
      // padded reported token.
      [[gate("padded")], ["text"], []],
      [[gate("padded")], [" text "], [gate("padded")]],
      [[gate("padded")], [" text"], []],
      [[gate("padded")], ["text "], []],
      // A non-ASCII case pair stays distinct (never Unicode-folded).
      [[gate("unicode-upper")], ["\u00e9"], []],
      [[gate("unicode-upper")], ["\u00c9"], [gate("unicode-upper")]],
      // Output modalities are not substitutes for input modalities.
      [[gate("output-image")], ["image"], []],
      [[gate("output-image")], ["text"], [gate("output-image")]],
      // No capability is inferred from name, description, raw id, provider key
      // or supported_parameters.
      [[gate("name-image")], ["image"], []],
      [[gate("param-image")], ["image"], []],
      [[gate("raw-id-image")], ["raw-id-image"], []],
      [[gate("name-image")], ["image name"], []],
      // Exact tokens, never substrings or wildcards.
      [[gate("both")], ["imag"], []],
      [[gate("both")], ["tex"], []],
      // A supplied strict subset is never expanded; empty input stays empty.
      [[gate("both")], ["text"], [gate("both")]],
      [[], ["text"], []],
      // Incoming order is preserved: never sorted, never deduplicated.
      [[gate("text-only"), gate("both"), gate("cased-dupes")], ["text"],
       [gate("text-only"), gate("both"), gate("cased-dupes")]],
      // A shared raw id with disjoint per-provider capabilities: no union and
      // no borrowing, and the incoming order still decides the output order.
      [[gate("shared-id"), ["other", "shared-id"]], ["image"],
       [gate("shared-id")]],
      [[gate("shared-id"), ["other", "shared-id"]], ["text"],
       [gate("shared-id"), ["other", "shared-id"]]],
      [[["other", "shared-id"], gate("shared-id")], ["image"],
       [gate("shared-id")]],
      [[["other", "shared-id"], gate("shared-id")], ["text"],
       [["other", "shared-id"], gate("shared-id")]],
      [[["other", "shared-id"]], ["image"], []]
    ];
    for (const [pairs, request, expected] of rows) {
      const result = catalog.searchInputModalityPairs(snapshot, pairs, request);
      assert.deepEqual(result.map(pairIdentity), expected,
                       `${JSON.stringify(pairs)} request=${JSON.stringify(request)}`);
    }
    // Same-endpoint composition: a high-context TEXT-only endpoint and a
    // low-context IMAGE endpoint select NEITHER for `context >= 65536 AND
    // image`, even though each predicate alone selects one of them.
    const conflict = searchSnapshot(["ctx"], {
      ctx: searchProvider({
        "high-text": searchModel("high-text", "High Text", {
          context_length: 65536, input_modalities: ["text"]
        }),
        "low-image": searchModel("low-image", "Low Image", {
          context_length: 32768, input_modalities: ["text", "image"]
        })
      }, "Ctx", "https://ctx.example/v1/chat/completions")
    });
    assert.deepEqual(catalog.searchInputModalityPairs(
      conflict,
      catalog.searchMinContextPairs(
        conflict, catalog.searchTextProviderPairs(conflict), 65536),
      ["image"]).map(pairIdentity), []);
    assert.deepEqual(catalog.searchInputModalityPairs(
      conflict, catalog.searchTextProviderPairs(conflict), ["image"])
        .map(pairIdentity), [["ctx", "low-image"]]);
    assert.deepEqual(catalog.searchMinContextPairs(
      conflict, catalog.searchTextProviderPairs(conflict), 65536)
        .map(pairIdentity), [["ctx", "high-text"]]);
  });

  it("ANDs every requested supported parameter on the same endpoint", () => {
    const snapshot = searchCapabilitySnapshot();
    const gate = (rawId) => ["gate", rawId];
    const rows = [
      // None and [] are inactive, retaining unknown and known-empty arrays.
      [[gate("both"), gate("unknown-array"), gate("empty-known")], null,
       [gate("both"), gate("unknown-array"), gate("empty-known")]],
      [[gate("both"), gate("unknown-array"), gate("empty-known")], [],
       [gate("both"), gate("unknown-array"), gate("empty-known")]],
      [[gate("both"), gate("unknown-array"), gate("empty-known")],
       ["temperature"], [gate("both")]],
      // ALL on one pair, in either request order, duplicates idempotent.
      [[gate("both"), gate("text-only")], ["temperature"],
       [gate("both"), gate("text-only")]],
      [[gate("both"), gate("text-only")], ["temperature", "top_p"],
       [gate("both")]],
      [[gate("both")], ["top_p", "temperature"], [gate("both")]],
      [[gate("both")], ["TEMPERATURE", "temperature"], [gate("both")]],
      // ASCII-insensitive both sides; first-occurrence dupes harmless.
      [[gate("cased-dupes")], ["TOP_P", "top_p"], [gate("cased-dupes")]],
      // Whitespace and non-ASCII case stay literal.
      [[gate("padded")], ["temperature"], []],
      [[gate("padded")], [" temperature "], [gate("padded")]],
      [[gate("unicode-upper")], ["\u00e9"], []],
      [[gate("unicode-upper")], ["\u00c9"], [gate("unicode-upper")]],
      // No substring and no inference from any other field.
      [[gate("both")], ["temp"], []],
      [[gate("param-image")], ["image"], []],
      [[gate("param-image")], ["image_size"], [gate("param-image")]],
      [[gate("both")], ["image"], []],
      [[gate("output-image")], ["image"], []],
      [[gate("price-image")], ["0"], []],
      // No cross-provider borrowing and no union to satisfy a conjunction: the
      // shared raw id carries temperature on one provider and top_p on the
      // other, so the conjunction selects NEITHER.
      [[gate("shared-id"), ["other", "shared-id"]],
       ["temperature", "top_p"], []],
      [[gate("shared-id")], ["temperature"], [gate("shared-id")]],
      [[["other", "shared-id"]], ["temperature"], []],
      [[["other", "shared-id"]], ["top_p"], [["other", "shared-id"]]],
      [[gate("shared-id"), ["other", "shared-id"]], ["top_p"],
       [["other", "shared-id"]]],
      // Order and multiplicity of the supplied selection are preserved.
      [[gate("text-only"), gate("both"), gate("text-only")], ["temperature"],
       [gate("text-only"), gate("both"), gate("text-only")]],
      [[], ["temperature"], []]
    ];
    for (const [pairs, request, expected] of rows) {
      assert.deepEqual(
        catalog.searchSupportedParameterPairs(snapshot, pairs, request)
          .map(pairIdentity), expected,
        `${JSON.stringify(pairs)} request=${JSON.stringify(request)}`);
    }
  });

  it("keeps prototype-looking keys, shared ids, digit runs and Unicode honest",
     () => {
    const snapshot = adversarialSearchSnapshot();
    const before = catalog.canonicalText(snapshot);
    const longId = "num-" + "0".repeat(59) + "9";
    // Natural order within a provider, never literal or UTF-16 unit order.
    assert.deepEqual(
      catalog.searchTextProviderPairs(snapshot, {provider: "__proto__"})
        .map(pairIdentity),
      [["__proto__", "__proto__"], ["__proto__", "constructor"],
       ["__proto__", "dup-id"], ["__proto__", "num-9"], ["__proto__", longId],
       ["__proto__", "\ud83c\udf1f-model"]]);
    // A prototype-looking provider key selects exactly that provider.
    assert.deepEqual(
      catalog.searchTextProviderPairs(snapshot, {provider: "toString"})
        .map(pairIdentity),
      [["toString", "dup-id"], ["toString", "toString"],
       ["toString", "\u00c9-model"], ["toString", "\u00e9-model"]]);
    // Both providers are selected by default, in provider_order precedence.
    assert.equal(catalog.searchTextProviderPairs(snapshot).length, 10);
    // A prototype-looking raw ID is an ordinary identity, and object key names
    // are still never searched.
    assert.deepEqual(
      catalog.searchTextProviderPairs(snapshot, {query: "__proto__"})
        .map(pairIdentity), [["__proto__", "__proto__"]]);
    assert.deepEqual(
      catalog.searchTextProviderPairs(snapshot, {query: "constructor"})
        .map(pairIdentity), [["__proto__", "constructor"]]);
    assert.deepEqual(
      catalog.searchTextProviderPairs(snapshot, {query: "toString"})
        .map(pairIdentity), [["toString", "toString"]],
      "the matching raw id and display name are the SAME pair");
    // A 60-digit run is never parsed as a float: the 59-zero id sorts after
    // the short one and both survive as literal ids.
    assert.deepEqual(
      catalog.searchMinContextPairs(snapshot,
        catalog.searchTextProviderPairs(snapshot, {query: "num-"}), 65536)
        .map(pairIdentity),
      [["__proto__", "num-9"], ["__proto__", longId]]);
    // The shared raw id keeps its OWN per-provider metadata.
    assert.deepEqual(
      catalog.searchInputModalityPairs(snapshot,
        [["__proto__", "dup-id"], ["toString", "dup-id"]], ["image"])
        .map(pairIdentity), [["__proto__", "dup-id"]]);
    assert.deepEqual(
      catalog.searchSupportedParameterPairs(snapshot,
        [["__proto__", "dup-id"], ["toString", "dup-id"]], ["top_p"])
        .map(pairIdentity), [["__proto__", "dup-id"], ["toString", "dup-id"]]);
    assert.deepEqual(
      catalog.searchSupportedParameterPairs(snapshot,
        [["__proto__", "dup-id"], ["toString", "dup-id"]],
        ["temperature", "top_p"]).map(pairIdentity),
      [["__proto__", "dup-id"]]);
    // Non-ASCII case pairs stay distinct in capability tokens as well.
    assert.deepEqual(
      catalog.searchSupportedParameterPairs(snapshot,
        [["toString", "\u00c9-model"], ["toString", "\u00e9-model"]],
        ["\u00c9"]).map(pairIdentity), [["toString", "\u00c9-model"]]);
    // Unicode digits are TEXT, not digit runs, and survive as literal ids.
    assert.equal(
      catalog.searchTextProviderPairs(snapshot, {query: "m-\u0661"}).length, 0);
    assert.equal(catalog.canonicalText(snapshot), before,
                 "adversarial controls never mutate the snapshot");
  });

  it("isolates inputs, results and requests across interleaved calls", () => {
    // This is interleaving, not a worker-concurrency test: repeated and
    // overlapping calls in one thread must not share any mutable state.
    const snapshot = searchCapabilitySnapshot();
    const before = catalog.canonicalText(snapshot);
    const request = ["TEXT", "image"];
    const requestBefore = request.slice();
    const supplied = [["gate", "both"], ["gate", "text-only"]];
    const suppliedBefore = supplied.map(pairIdentity);

    const first = catalog.searchInputModalityPairs(snapshot, supplied, request);
    assert.deepEqual(first.map(pairIdentity), [["gate", "both"]]);
    first[0][0] = "hijacked";
    first[0][1] = "hijacked";
    first.push(["gate", "injected"]);
    first.length = 0;

    const contextResult = catalog.searchMinContextPairs(snapshot, supplied, 0);
    const parameterResult = catalog.searchSupportedParameterPairs(
      snapshot, supplied, ["temperature", "top_p"]);
    const textResult = catalog.searchTextProviderPairs(snapshot,
                                                       {query: "modalities"});

    assert.deepEqual(catalog.searchInputModalityPairs(snapshot, supplied,
                                                      request)
                       .map(pairIdentity), [["gate", "both"]],
                     "a later call is unaffected by mutating an earlier result");
    assert.deepEqual(catalog.searchMinContextPairs(snapshot, supplied, 0)
                       .map(pairIdentity), suppliedBefore);
    assert.deepEqual(catalog.searchSupportedParameterPairs(
      snapshot, supplied, ["temperature", "top_p"]).map(pairIdentity),
      [["gate", "both"]]);
    assert.deepEqual(request, requestBefore, "the request list is untouched");
    assert.deepEqual(supplied, suppliedBefore, "the selection is untouched");
    assert.equal(contextResult.length, 2);
    assert.equal(parameterResult.length, 1);
    assert.ok(textResult.length >= 1);
    assert.equal(catalog.canonicalText(snapshot), before,
                 "no call can corrupt the snapshot");

    // Every branch returns a NEW outer array of NEW pair arrays.
    const inactiveA = catalog.searchMinContextPairs(snapshot, supplied, null);
    const inactiveB = catalog.searchMinContextPairs(snapshot, supplied, null);
    assert.notEqual(inactiveA, inactiveB);
    assert.notEqual(inactiveA[0], inactiveB[0]);
    inactiveA[0][1] = "mutated";
    assert.deepEqual(inactiveB.map(pairIdentity), suppliedBefore);
    const modalityInactive = catalog.searchInputModalityPairs(snapshot,
                                                              supplied, []);
    assert.notEqual(modalityInactive, supplied);
    assert.notEqual(modalityInactive[0], supplied[0]);
    const parameterInactive = catalog.searchSupportedParameterPairs(
      snapshot, supplied, null);
    assert.notEqual(parameterInactive, supplied);
    assert.equal(catalog.canonicalText(snapshot), before);
  });

  it("stays complete and unpaginated well past two hundred pairs", () => {
    const snapshot = searchCapabilitySnapshot();
    const bulk = catalog.searchTextProviderPairs(snapshot, {provider: "bulk"});
    assert.equal(bulk.length, 210, "every bulk endpoint is selected");
    const composed = composeSearchStages(snapshot, {
      provider: "bulk", min_context_length: 65536, input_modalities: ["text"],
      supported_parameters: ["temperature"]
    });
    assert.equal(composed.length, 210, "no stage slices the complete set");
    assert.deepEqual(composed[0], ["bulk", "bulk-000"]);
    assert.deepEqual(composed[209], ["bulk", "bulk-209"]);
    // A >200 selection survives a later filter stage with multiplicity intact.
    const repeated = composed.concat(composed.slice(0, 5));
    assert.equal(catalog.searchSupportedParameterPairs(
      snapshot, repeated, ["temperature"]).length, 215);
  });

  it("refuses a wrong-schema document before any stage can see it", () => {
    const text = wrongSchemaSnapshotText();
    assert.throws(() => catalog.loadSnapshot(text),
                  {name: "CatalogError",
                   message: UNSUPPORTED_VERSION_MESSAGE},
                  "the accepted loader refuses schema_version 3");
    assert.deepEqual(runQueryReference([{tool: "load_error",
                                        snapshot_text: text, args: {},
                                        now: null}]),
                     [catalog.canonicalText({code: "unsupported_schema_version"})],
                     "Python refuses the same document with the same code");
  });

  it("agrees with the unchanged Python reference on every stage", () => {
    const cases = [];
    for (const row of completeStageRows()) {
      cases.push(stagePairCase(row.id, rowSnapshot(row), row.args));
    }
    const boundary = searchBoundarySnapshot();
    for (const [label, args] of [
      ["boundary-none", {}],
      ["boundary-shared-id", {query: "shared-id"}],
      ["boundary-upper", {query: "MIXED NAME"}],
      ["boundary-unicode", {query: "\u00c9CHO"}],
      ["boundary-whitespace", {query: "   "}],
      ["boundary-raw", {query: "bulk"}],
      ["boundary-provider", {provider: "alpha"}],
      ["boundary-provider-query", {query: "vendor/tool", provider: "Alpha"}],
      ["boundary-context", {min_context_length: 7}],
      ["boundary-context-zero", {min_context_length: 0}],
      ["boundary-modality", {input_modalities: ["imagetoken"]}],
      ["boundary-parameter", {supported_parameters: ["paramtoken"]}]
    ]) {
      cases.push(stagePairCase(label, boundary, args));
    }
    const capability = searchCapabilitySnapshot();
    for (const [label, args] of [
      ["capability-context-65536", {min_context_length: 65536}],
      ["capability-context-max", {min_context_length: catalog.MAX_SAFE_INT}],
      ["capability-modality-and", {input_modalities: ["text", "IMAGE"]}],
      ["capability-parameter-and", {supported_parameters: ["temperature",
                                                          "TOP_P"]}],
      ["capability-all", {min_context_length: 65536,
                          input_modalities: ["text"],
                          supported_parameters: ["temperature"]}],
      ["capability-zero", {min_context_length: 0,
                           input_modalities: [], supported_parameters: []}]
    ]) {
      cases.push(stagePairCase(label, capability, args));
    }
    const adversarial = adversarialSearchSnapshot();
    for (const [label, args] of [
      ["adversarial-none", {}],
      ["adversarial-proto", {provider: "__proto__"}],
      ["adversarial-proto-query", {query: "__proto__", provider: "__proto__"}],
      ["adversarial-toString", {provider: "toString"}],
      ["adversarial-unicode", {query: "\u00c9CHO"}],
      ["adversarial-digit-run", {query: "num-"}],
      ["adversarial-context-zero", {min_context_length: 0}],
      ["adversarial-modality", {input_modalities: ["image"]}],
      ["adversarial-parameter", {supported_parameters: ["top_p"]}]
    ]) {
      cases.push(stagePairCase(label, adversarial, args));
    }
    cases.push(stagePairCase("future-epoch",
                             resealedSnapshot("lookup.json", (body) => {
                               body.tick_epoch = 4102444800;
                             }), {query: "shared"}));
    cases.push(stagePairCase("zero-epoch-and-zero-context",
                             resealedSnapshot("lookup.json", (body) => {
                               body.tick_epoch = 0;
                               body.warnings = [];
                             }), {min_context_length: 0}));
    cases.push(stagePairCase("empty-registry",
                             searchSnapshot([], {}), {}));

    // Supplied-selection cases: reversed subsets, repeats and single pairs, so
    // order and multiplicity are pinned against the reference too.
    const reversed = [["gate", "text-only"], ["gate", "both"],
                      ["gate", "text-only"], ["other", "shared-id"],
                      ["gate", "both"]];
    for (const [label, pairs, args] of [
      ["filter-inactive", reversed, {}],
      ["filter-context-zero", reversed, {min_context_length: 0}],
      ["filter-context-high", reversed, {min_context_length: 65536}],
      ["filter-modality", reversed, {input_modalities: ["text"]}],
      ["filter-modality-image", reversed, {input_modalities: ["IMAGE"]}],
      ["filter-parameter", reversed, {supported_parameters: ["top_p"]}],
      ["filter-all", reversed,
       {min_context_length: 65536, input_modalities: ["text"],
        supported_parameters: ["temperature"]}],
      ["filter-single", [["other", "shared-id"]],
       {min_context_length: 65536, input_modalities: ["image"],
        supported_parameters: ["temperature"]}],
      ["filter-empty", [], {min_context_length: 0,
                            input_modalities: ["text"]}]
    ]) {
      cases.push(stageFilterCase(label, capability, pairs, args));
    }

    assert.equal(cases.length, 22 + 12 + 6 + 9 + 3 + 9,
                 "the parity case set is complete");
    assertStageParity(cases);
  });

  it("agrees with the unchanged Python reference on astral and surrogate halves", () => {
    const snapshot = searchAstralSnapshot();
    const cases = SEARCH_ASTRAL_ROWS.map(([label, query]) =>
      stagePairCase("astral-" + label, snapshot, {query: query}));
    assert.equal(cases.length, SEARCH_ASTRAL_ROWS.length,
                 "the astral parity set is complete");
    // The live dispatcher re-loads this exact canonical text with the accepted
    // Python `load_snapshot`, resolves the module through the accepted
    // in-repository origin guard, and calls the real
    // `_search_text_provider_pairs` on the unchanged floor interpreter -- so a
    // Python rejection of this snapshot, a foreign module or a surviving
    // code-unit predicate all fail here rather than being masked.
    assertStageParity(cases);
  });
});

// --------------------------------------------------------------------------
// cross-fixture integration and the dual (browser + Node) export
// --------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// pure search_models query: pagination, argument errors, complete payload
// ---------------------------------------------------------------------------
describe("pure search_models query", () => {
  /** The frozen argument-error envelope with its literal message. */
  function searchArgumentError(code, parameter) {
    const messages = {invalid_arguments: "Invalid tool arguments.",
                      unknown_provider: "Unknown provider."};
    return {ok: false, error: {code: code, message: messages[code],
                               parameter: parameter}};
  }

  /** Typed argument decoding from a JSON literal (float tokens preserved). */
  function typedArgs(text) {
    return catalog.decodeTypedJson(text);
  }

  /** One provider whose `model_ids` is the required natural-order array. */
  function sealedProvider(models, displayName, url, options) {
    const opts = options || {};
    const ids = Object.keys(models).sort(catalog.compareNaturalIds);
    const ordered = {};
    for (const rawId of Object.keys(models)) {
      withOwnKey(ordered, rawId, models[rawId]);
    }
    return {display_name: displayName, chat_completions_url: url,
            api_type: "openai", model_ids: ids, models: ordered,
            stale: opts.stale === true};
  }

  /** A resealed synthetic snapshot the REAL validator accepts. */
  function sealedSearchSnapshot(providerOrder, providers, options) {
    const opts = options || {};
    const ordered = {};
    for (const key of Object.keys(providers)) {
      withOwnKey(ordered, key, providers[key]);
    }
    return resealSnapshot({
      schema_version: 2,
      snapshot_id: "0".repeat(64),
      tick_epoch: opts.tick_epoch === undefined ? 1759200000
                                                : opts.tick_epoch,
      cadence_s: opts.cadence_s === undefined ? 3600 : opts.cadence_s,
      provider_order: providerOrder.slice(),
      providers: ordered,
      warnings: opts.warnings === undefined ? [] : opts.warnings.slice()
    });
  }

  function identities(page) {
    return page.endpoints.map((row) => [row.provider, row.model_id]);
  }

  it("keeps the frozen error precedence independent of key order", () => {
    const snapshot = acceptedSnapshot("lookup.json");
    const call = (text) => catalog.searchModels(snapshot, typedArgs(text));

    // Signature order, not insertion order: query wins over offset.
    assert.deepEqual(call('{"offset":null,"query":[]}'),
                     searchArgumentError("invalid_arguments", "query"));
    // ... provider wins over min_context_length ...
    assert.deepEqual(call('{"min_context_length":-1,"provider":"synthetic-z"}'),
                     searchArgumentError("unknown_provider", "provider"));
    // ... and min_context_length wins over the capability arrays.
    assert.deepEqual(call(
      '{"input_modalities":"text","min_context_length":true}'),
      searchArgumentError("invalid_arguments", "min_context_length"));
    // ANY unknown own key wins before every known-signature error.
    assert.deepEqual(call('{"query":[],"bogus":1}'),
                     searchArgumentError("invalid_arguments", null));
    // Prototype-looking OWN keys are unknown names, never inherited lookups.
    for (const name of ["__proto__", "constructor", "toString",
                        "hasOwnProperty"]) {
      const args = typedArgs(`{"${name}": 1}`);
      assert.ok(Object.prototype.hasOwnProperty.call(args, name), name);
      assert.deepEqual(catalog.searchModels(snapshot, args),
                       searchArgumentError("invalid_arguments", null), name);
    }
    // An own `constructor` member is an unknown key, so it is rejected rather
    // than inherited from the prototype chain.
    const protoQuery = typedArgs('{"query":"x","provider":null}');
    withOwnKey(protoQuery, "constructor", 1);
    assert.deepEqual(catalog.searchModels(snapshot, protoQuery),
                     searchArgumentError("invalid_arguments", null));
    // A non-object argument is the same null-parameter envelope.
    for (const value of [[], 5, "x", null, true]) {
      assert.deepEqual(catalog.searchModels(snapshot, value),
                       searchArgumentError("invalid_arguments", null));
    }
  });

  function bulkSnapshot() {
    const omega = {};
    for (let index = 0; index < 120; index += 1) {
      const rawId = "om-" + String(index).padStart(3, "0");
      withOwnKey(omega, rawId, searchModel(rawId, "Omega row " + rawId, {
        description: "Bulk population row for the omega gateway.",
        context_length: index >= 115 ? 65536 : 8192,
        input_modalities: ["text"], output_modalities: ["text"],
        supported_parameters: ["temperature"]
      }));
    }
    const alpha = {};
    for (let index = 0; index < 95; index += 1) {
      const rawId = "al-" + String(index).padStart(3, "0");
      withOwnKey(alpha, rawId, searchModel(rawId, "Alpha row " + rawId, {
        description: "Bulk population row for the alpha gateway.",
        context_length: 8192, input_modalities: ["text"],
        output_modalities: ["text"], supported_parameters: ["temperature"]
      }));
    }
    // A provider-map insertion order different from `provider_order`.
    const providers = {};
    withOwnKey(providers, "zero", sealedProvider(
      {}, "Zero", "https://zero.example/v1/chat/completions"));
    withOwnKey(providers, "alpha", sealedProvider(
      alpha, "Alpha", "https://alpha.example/v1/chat/completions"));
    withOwnKey(providers, "omega", sealedProvider(
      omega, "Omega", "https://omega.example/v1/chat/completions"));
    return sealedSearchSnapshot(["omega", "alpha", "zero"], providers);
  }

  it("paginates 215 pairs with one global sort-then-slice", () => {
    const snapshot = bulkSnapshot();
    assert.equal(snapshot.providers.omega.model_ids.length, 120);
    assert.equal(snapshot.providers.alpha.model_ids.length, 95);
    assert.notDeepEqual(snapshot.provider_order,
                        Object.keys(snapshot.providers));
    assert.notDeepEqual(snapshot.provider_order,
                        snapshot.provider_order.slice().sort());
    const before = catalog.canonicalText(snapshot);
    const omegaIds = snapshot.providers.omega.model_ids;
    const alphaIds = snapshot.providers.alpha.model_ids;

    const defaultPage = catalog.searchModels(snapshot, {});
    assert.deepEqual(Object.keys(defaultPage).sort(),
                     ["endpoints", "next_offset", "ok", "provider_staleness",
                      "snapshot_id", "tick_epoch", "total_matches"].sort(),
                     "default page field set");
    assert.equal(defaultPage.ok, true);
    assert.equal(defaultPage.total_matches, 215);
    assert.equal(defaultPage.endpoints.length, 5);
    assert.equal(defaultPage.next_offset, 5);
    assert.deepEqual(identities(defaultPage),
                     omegaIds.slice(0, 5).map((id) => ["omega", id]));
    assert.deepEqual(defaultPage.provider_staleness,
                     {omega: false, alpha: false, zero: false});

    const maxPage = catalog.searchModels(snapshot, {limit: 200});
    assert.equal(maxPage.endpoints.length, 200);
    assert.equal(maxPage.next_offset, 200);
    assert.equal(maxPage.endpoints[119].model_id, "om-119");
    assert.equal(maxPage.endpoints[120].provider, "alpha");
    assert.equal(maxPage.endpoints[120].model_id, "al-000");
    const tail = catalog.searchModels(snapshot, {limit: 200, offset: 200});
    assert.equal(tail.endpoints.length, 15);
    assert.equal(tail.next_offset, null);
    assert.deepEqual(tail.endpoints.map((row) => row.model_id),
                     alphaIds.slice(80));
    assert.equal(tail.total_matches, 215);

    const cross = catalog.searchModels(snapshot, {limit: 2, offset: 119});
    assert.deepEqual(identities(cross), [["omega", "om-119"],
                                         ["alpha", "al-000"]]);
    assert.equal(cross.next_offset, 121);

    const exactEnd = catalog.searchModels(snapshot, {limit: 2, offset: 213});
    assert.equal(exactEnd.endpoints.length, 2);
    assert.equal(exactEnd.next_offset, null);
    const continuing = catalog.searchModels(snapshot, {limit: 2, offset: 211});
    assert.equal(continuing.next_offset, 213);

    for (const offset of [215, 999999, catalog.MAX_SAFE_INT]) {
      const page = catalog.searchModels(snapshot, {limit: 2, offset: offset});
      assert.deepEqual(page.endpoints, []);
      assert.equal(page.next_offset, null);
      assert.equal(page.total_matches, 215);
      assert.deepEqual(page.provider_staleness,
                       {omega: false, alpha: false, zero: false});
    }

    // Filter BEFORE the single slice: no qualifying row is in the first
    // unfiltered window, and the filtered page is the later match window.
    const filtered = catalog.searchModels(snapshot,
                                          {min_context_length: 65536,
                                           limit: 3});
    assert.deepEqual(filtered.endpoints.map((row) => row.model_id),
                     ["om-115", "om-116", "om-117"]);
    assert.equal(filtered.total_matches, 5);
    assert.equal(filtered.next_offset, 3);

    // A zero-ID provider and a genuinely empty registry both succeed.
    const zeroPage = catalog.searchModels(snapshot, {provider: "zero"});
    assert.deepEqual(zeroPage.endpoints, []);
    assert.equal(zeroPage.total_matches, 0);
    assert.deepEqual(zeroPage.provider_staleness, {zero: false});
    const empty = catalog.searchModels(
      sealedSearchSnapshot([], {}, {}), {});
    assert.equal(empty.ok, true);
    assert.deepEqual(empty.provider_staleness, {});
    assert.deepEqual(empty.endpoints, []);
    assert.equal(empty.total_matches, 0);
    assert.equal(empty.next_offset, null);
    assert.equal(catalog.canonicalText(snapshot), before);
  });

  it("scopes staleness to the selected provider", () => {
    const markerId = "acme/chat:free";
    function markerModel(rawId) {
      return searchModel(rawId, "Marker " + rawId, {
        input_modalities: ["text"], supported_parameters: ["temperature"]
      });
    }
    const marker = {};
    withOwnKey(marker, markerId, markerModel(markerId));
    withOwnKey(marker, "acme/other", markerModel("acme/other"));
    const second = {};
    withOwnKey(second, "acme/chat_free", markerModel("acme/chat_free"));
    const third = {};
    withOwnKey(third, markerId, markerModel(markerId));
    const snapshot = sealedSearchSnapshot(["p1", "p2", "p3"], {
      p1: sealedProvider(marker, "P1", "https://p1.example/v1/chat/completions",
                         {stale: true}),
      p2: sealedProvider(second, "P2",
                         "https://p2.example/v1/chat/completions"),
      p3: sealedProvider(third, "P3", "https://p3.example/v1/chat/completions",
                         {stale: true})
    });
    const page = catalog.searchModels(snapshot, {query: "acme"});
    assert.deepEqual(identities(page), [["p1", markerId],
                                        ["p1", "acme/other"],
                                        ["p2", "acme/chat_free"],
                                        ["p3", markerId]]);
    assert.equal(page.total_matches, 4);
    assert.deepEqual(page.provider_staleness,
                     {p1: true, p2: false, p3: true});

    // A single-provider page keeps only its own scope; the row itself has no
    // stale flag or wiring field.
    const p3 = catalog.searchModels(snapshot, {provider: "p3"});
    assert.deepEqual(identities(p3), [["p3", markerId]]);
    assert.deepEqual(Object.keys(p3.endpoints[0]).sort(), [
      "context_length", "input_modalities", "model_id", "name",
      "output_modalities", "provider", "supported_parameters"]);
    assert.deepEqual(p3.provider_staleness, {p3: true});

    // An exhausted page keeps every provider's staleness and stays a success.
    const exhausted = catalog.searchModels(snapshot, {offset: 99});
    assert.deepEqual(exhausted.endpoints, []);
    assert.equal(exhausted.total_matches, 4);
    assert.equal(exhausted.next_offset, null);
    assert.deepEqual(exhausted.provider_staleness,
                     {p1: true, p2: false, p3: true});
  });

  it("never borrows capabilities across providers with the same raw id", () => {
    const conflictId = "vendor/conflict";
    const a = {};
    withOwnKey(a, conflictId, searchModel(conflictId, "Conflict A", {
      context_length: 65536, input_modalities: ["text"],
      output_modalities: ["text"], supported_parameters: ["temperature"]
    }));
    const b = {};
    withOwnKey(b, conflictId, searchModel(conflictId, "Conflict B", {
      context_length: 8192, input_modalities: ["text", "image"],
      output_modalities: ["text"], supported_parameters: ["temperature"]
    }));
    const snapshot = sealedSearchSnapshot(["gw-a", "gw-b"], {
      "gw-a": sealedProvider(a, "Gate A",
                             "https://gw-a.example/v1/chat/completions"),
      "gw-b": sealedProvider(b, "Gate B",
                             "https://gw-b.example/v1/chat/completions")
    });
    const before = catalog.canonicalText(snapshot);
    const neither = catalog.searchModels(snapshot, {
      min_context_length: 65536, input_modalities: ["image"]});
    assert.deepEqual(neither.endpoints, []);
    assert.equal(neither.total_matches, 0);
    assert.equal(neither.next_offset, null);
    assert.deepEqual(identities(catalog.searchModels(snapshot, {
      min_context_length: 65536, input_modalities: ["text"],
      supported_parameters: ["temperature"]})), [["gw-a", conflictId]]);
    assert.deepEqual(identities(catalog.searchModels(snapshot, {
      min_context_length: 8192, input_modalities: ["IMAGE"],
      supported_parameters: ["TEMPERATURE"]})), [["gw-b", conflictId]]);

    // Capability AND is on the SAME pair: no union satisfies a conjunction.
    const splitId = "vendor/split";
    const left = {};
    withOwnKey(left, splitId, searchModel(splitId, "Split A", {
      context_length: 65536, input_modalities: ["text"],
      supported_parameters: ["temperature"]
    }));
    const right = {};
    withOwnKey(right, splitId, searchModel(splitId, "Split B", {
      context_length: 65536, input_modalities: ["text"],
      supported_parameters: ["max_tokens"]
    }));
    const split = sealedSearchSnapshot(["gw-a", "gw-b"], {
      "gw-a": sealedProvider(left, "Gate A",
                             "https://gw-a.example/v1/chat/completions"),
      "gw-b": sealedProvider(right, "Gate B",
                             "https://gw-b.example/v1/chat/completions")
    });
    assert.deepEqual(catalog.searchModels(split, {
      supported_parameters: ["temperature", "max_tokens"]}).endpoints, []);
    assert.deepEqual(identities(catalog.searchModels(split, {
      supported_parameters: ["temperature"]})), [["gw-a", splitId]]);
    assert.deepEqual(identities(catalog.searchModels(split, {
      supported_parameters: ["max_tokens"]})), [["gw-b", splitId]]);
    assert.equal(catalog.canonicalText(snapshot), before);
  });

  it("projects compact normalized rows without any metadata copy", () => {
    const cased = searchModel("vendor/cased", "Cased", {
      description: "Prose for the cased row.", context_length: 100,
      input_modalities: ["TEXT", "Image", "text"], output_modalities: [],
      supported_parameters: ["Temperature", "TEMPERATURE"]
    });
    const unknown = searchModel("vendor/unknown", "Unknown");
    const models = {};
    withOwnKey(models, "vendor/cased", cased);
    withOwnKey(models, "vendor/unknown", unknown);
    const snapshot = sealedSearchSnapshot(["norm"], {
      norm: sealedProvider(models, "Norm",
                           "https://norm.example/v1/chat/completions")
    });
    const before = catalog.canonicalText(snapshot);
    const args = typedArgs(
      '{"input_modalities":["TEXT","text"],' +
      '"supported_parameters":["temperature"],"limit":1.0}');
    const result = catalog.searchModels(snapshot, args);
    assert.deepEqual(Object.keys(result).sort(), [
      "endpoints", "next_offset", "ok", "provider_staleness", "snapshot_id",
      "tick_epoch", "total_matches"]);
    assert.deepEqual(identities(result), [["norm", "vendor/cased"]]);
    const row = result.endpoints[0];
    assert.deepEqual(Object.keys(row).sort(), [
      "context_length", "input_modalities", "model_id", "name",
      "output_modalities", "provider", "supported_parameters"]);
    assert.equal(row.name, "Cased");
    assert.deepEqual(row.input_modalities, ["text", "image"]);
    assert.deepEqual(row.output_modalities, []);
    assert.deepEqual(row.supported_parameters, ["temperature"]);
    assert.equal("description" in row, false);
    assert.equal("metadata" in row, false);
    assert.equal("unknown_fields" in row, false);

    const unknownRow = catalog.searchModels(
      snapshot, {query: "unknown"}).endpoints[0];
    assert.equal(unknownRow.context_length, null);
    assert.equal(unknownRow.input_modalities, null);
    assert.equal(unknownRow.output_modalities, null);
    assert.equal(unknownRow.supported_parameters, null);
    assert.equal("unknown_fields" in unknownRow, false);

    // Deep isolation: mutating any returned category never reaches a later
    // call, the argument object or the snapshot.
    const saved = catalog.canonicalText(result);
    result.provider_staleness.norm = true;
    row.input_modalities.push("injected");
    row.supported_parameters.length = 0;
    result.endpoints.length = 0;
    assert.equal(catalog.canonicalText(catalog.searchModels(snapshot, args)),
                 saved);
    assert.equal(catalog.canonicalText(snapshot), before);
    assert.equal(args.input_modalities.length, 2);
  });

  it("accepts integral float pagination inputs without an echo", () => {
    const priced = searchModel("vendor/priced", "Priced", {
      context_length: 0, input_modalities: ["text"],
      output_modalities: ["text"], supported_parameters: ["temperature"]
    });
    const models = {};
    withOwnKey(models, "vendor/priced", priced);
    const body = sealedSearchSnapshot(["floats"], {
      floats: sealedProvider(models, "Floats",
                             "https://floats.example/v1/chat/completions")
    });
    const text = catalog.canonicalText(body);
    const snapshot = catalog.loadSnapshot(text);
    assert.equal(catalog.snapshotHash(snapshot), snapshot.snapshot_id);

    const typed = catalog.searchModels(snapshot, typedArgs(
      '{"min_context_length":0.0,"limit":1.0,"offset":0.0}'));
    const strict = catalog.searchModels(
      snapshot, {min_context_length: 0, limit: 1, offset: 0});
    assert.deepEqual(typed, strict);
    assert.equal(typed.total_matches, 1);
    assert.equal(typed.endpoints.length, 1);
    assert.equal("filters" in typed, false);
  });

  it("handles prototype-looking keys, unicode and lone surrogates", () => {
    // A registry whose provider and model keys are prototype member names.
    const protoModels = {};
    withOwnKey(protoModels, "__proto__", searchModel("__proto__", "Literal",
                                                     {context_length: 0}));
    withOwnKey(protoModels, "constructor", searchModel("constructor",
                                                       "Constructor"));
    withOwnKey(protoModels, "hasOwnProperty",
              searchModel("hasOwnProperty", "Own"));
    const plainModels = {};
    withOwnKey(plainModels, "vendor/plain", searchModel("vendor/plain",
                                                        "Plain"));
    const stringModels = {};
    withOwnKey(stringModels, "vendor/tostring",
              searchModel("vendor/tostring", "Stringly"));
    const snapshot = sealedSearchSnapshot(
      ["__proto__", "constructor", "toString", "empty"], (() => {
        // A `{"__proto__": ...}` object LITERAL would set the prototype; the
        // registry needs an ordinary OWN member, exactly as JSON.parse makes it.
        const providers = {};
        withOwnKey(providers, "__proto__", sealedProvider(
          protoModels, "Literal proto",
          "https://proto.example/v1/chat/completions", {stale: true}));
        withOwnKey(providers, "constructor", sealedProvider(
          plainModels, "Constructor",
          "https://constructor.example/v1/chat/completions"));
        withOwnKey(providers, "toString", sealedProvider(
          stringModels, "ToString",
          "https://tostring.example/v1/chat/completions"));
        withOwnKey(providers, "empty", sealedProvider(
          {}, "Empty", "https://empty.example/v1/chat/completions"));
        return providers;
      })());
    const page = catalog.searchModels(snapshot, {limit: 200});
    assert.deepEqual(identities(page), [["__proto__", "__proto__"],
                                        ["__proto__", "constructor"],
                                        ["__proto__", "hasOwnProperty"],
                                        ["constructor", "vendor/plain"],
                                        ["toString", "vendor/tostring"]]);
    assert.deepEqual(page.provider_staleness,
                     flagMap([["__proto__", true], ["constructor", false],
                              ["toString", false], ["empty", false]]));
    assert.deepEqual(identities(catalog.searchModels(snapshot,
                                                     {query: "__proto__"})),
                     [["__proto__", "__proto__"]]);
    assert.deepEqual(identities(catalog.searchModels(
      snapshot, {provider: "constructor"})), [["constructor", "vendor/plain"]]);

    // Unicode natural order and ASCII-only folding.
    const unicodeIds = ["vendor/\u00e9", "vendor/\u00c9",
                        "vendor/e\u0301", "vendor/\ud83d\ude00",
                        "vendor/\ud83d\ude01", "vendor/\uffff",
                        "vendor/long" + "9".repeat(64),
                        "vendor/long" + "8".repeat(64)];
    const unicodeModels = {};
    for (const rawId of unicodeIds.slice().reverse()) {
      withOwnKey(unicodeModels, rawId, searchModel(rawId, "Row " + rawId,
                                                   {context_length: 1000}));
    }
    const unicode = sealedSearchSnapshot(["unicode"], {
      unicode: sealedProvider(unicodeModels, "Unicode",
                              "https://unicode.example/v1/chat/completions")
    });
    const expectedOrder = unicode.providers.unicode.model_ids;
    const unicodePage = catalog.searchModels(unicode, {limit: 200});
    assert.deepEqual(unicodePage.endpoints.map((row) => row.model_id),
                     expectedOrder);
    // Independently authored natural order: text tokens compare by code point
    // ("e"+U+0301 < "long" < "É" < "é" < U+FFFF < astral), and the two
    // 64-digit runs keep their ORIGINAL length with a lexical 8 < 9 tie-break
    // (a float or BigInt parse would collide them).
    assert.deepEqual(expectedOrder, [
      "vendor/e\u0301",
      "vendor/long" + "8".repeat(64), "vendor/long" + "9".repeat(64),
      "vendor/\u00c9", "vendor/\u00e9", "vendor/\uffff",
      "vendor/\ud83d\ude00", "vendor/\ud83d\ude01"
    ]);
    assert.equal(expectedOrder[0], "vendor/e\u0301");
    assert.equal(expectedOrder[2], "vendor/long" + "9".repeat(64));
    assert.deepEqual(identities(catalog.searchModels(unicode, {
      query: "\u00c9"})), [["unicode", "vendor/\u00c9"]]);
    assert.deepEqual(identities(catalog.searchModels(unicode, {
      query: "\u00e9"})), [["unicode", "vendor/\u00e9"]]);
    // A bare combining acute is a literal code point of the NFD id, while the
    // circumflex near-miss appears nowhere.
    assert.deepEqual(identities(catalog.searchModels(unicode, {
      query: "\u0301"})), [["unicode", "vendor/e\u0301"]]);
    assert.equal(catalog.searchModels(unicode, {query: "\u0302"}).total_matches,
                 0);

    // Astral pairs are single code points: each half alone never matches, and
    // an unpaired surrogate query matches only that same literal code unit.
    const astral = "vendor/\ud83d\ude00";
    assert.equal(catalog.searchModels(unicode, {
      query: "\ud83d\ude00"}).total_matches, 1);
    assert.equal(catalog.searchModels(unicode, {query: "\ud83d"}).total_matches,
                 0);
    assert.equal(catalog.searchModels(unicode, {query: "\ude00"}).total_matches,
                 0);
    const surrogateModels = {};
    withOwnKey(surrogateModels, "vendor/surrogate", searchModel(
      "vendor/surrogate", "Lone \ud800 surrogate", {
        description: "Field with an unpaired \udfff unit",
        context_length: 4096
      }));
    const surrogate = sealedSearchSnapshot(["surrogates"], {
      surrogates: sealedProvider(surrogateModels, "Surrogates",
                                 "https://surrogate.example/v1/chat/completions")
    });
    const lone = catalog.searchModels(surrogate, {query: "\ud800"});
    assert.equal(lone.total_matches, 1);
    assert.equal(lone.endpoints[0].model_id, "vendor/surrogate");
    assert.equal(catalog.searchModels(surrogate,
                                      {query: "\udfff"}).total_matches, 1);
    assert.equal(catalog.searchModels(surrogate, {query: "\ud83d"}).total_matches,
                 0);
  });

  it("rejects duplicate raw ids and duplicate JSON members identically", () => {
    const models = {};
    withOwnKey(models, "vendor/x", searchModel("vendor/x", "X"));
    const duplicate = {
      schema_version: 2, snapshot_id: "0".repeat(64),
      tick_epoch: 1759200000, cadence_s: 3600,
      provider_order: ["dup"],
      providers: {dup: {display_name: "Dup",
                        chat_completions_url:
                          "https://dup.example/v1/chat/completions",
                        api_type: "openai",
                        model_ids: ["vendor/x", "vendor/x"],
                        models: models, stale: false}},
      warnings: []
    };
    assert.throws(() => catalog.validateSnapshot(duplicate),
                  (error) => error instanceof catalog.CatalogError &&
                             error.code === "invalid_snapshot");
    const duplicateMembers =
      '{"schema_version":2,"schema_version":2,"snapshot_id":"' +
      "0".repeat(64) + '","tick_epoch":1759200000,"cadence_s":3600,' +
      '"provider_order":[],"providers":{},"warnings":[]}';
    assert.throws(() => catalog.loadJson(duplicateMembers),
                  (error) => error instanceof catalog.CatalogError &&
                             error.code === "invalid_json");
  });

  it("is the same implementation in the inlined classic browser script", () => {
    const vm = require("node:vm");
    const source = fs.readFileSync(
      path.join(__dirname, "..", "..", "web", "catalog-query.js"), "utf8");
    const sandbox = {};
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    const browserApi = sandbox.WatchdogCatalog;
    assert.equal(typeof browserApi.searchModels, "function");
    assert.equal(browserApi.searchModels.length, 2);
    const lookup = readFixtureText("lookup.json");
    const nodeSnapshot = catalog.loadSnapshot(lookup);
    const browserSnapshot = browserApi.loadSnapshot(lookup);
    const args = {query: "shared", min_context_length: 65536, limit: 2};
    assert.equal(
      browserApi.canonicalText(browserApi.searchModels(browserSnapshot, args)),
      catalog.canonicalText(catalog.searchModels(nodeSnapshot, args)));
    // `searchModels` closes over the same module instance in the page copy.
    const pageResult = browserApi.searchModels(browserSnapshot, {});
    assert.equal(pageResult.ok, true);
    assert.equal(pageResult.total_matches, 5);
  });
});

describe("fixture integration and export shape", () => {
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
      "compareNaturalIds", "decodeTypedJson", "getModel", "isFloatToken",
      "loadJson", "loadSnapshot",
      "naturalKey",
      "normalizeCapabilities", "projectCapabilities", "projectModel",
      "searchInputModalityPairs", "searchMinContextPairs", "searchModels",
      "searchSupportedParameterPairs", "searchTextProviderPairs",
      "snapshotHash", "stripFreeMarker", "validateSnapshot", "watchdogStatus"
    ]);
    assert.equal(catalog.MAX_SAFE_INT, 9007199254740991);
    assert.equal(catalog.MAX_JSON_DEPTH, 64);
    assert.equal(catalog.SCHEMA_VERSION, 2);
    const error = new catalog.CatalogError("invalid_json");
    assert.equal(error.code, "invalid_json");
    assert.equal(error.message, RAW_ERROR_MESSAGE);
    assert.deepEqual(error.toPayload(), {
      ok: false, error: { code: "invalid_json", message: RAW_ERROR_MESSAGE }
    });
    assert.ok(error instanceof Error);
  });
});
