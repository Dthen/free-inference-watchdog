"use strict";

/**
 * Node tests for the WebMCP adapter (`web/webmcp.js`) — Unit 8b.
 *
 * Scope of THIS file (and only this file plus the adapter):
 *
 *   * evaluation/require of the adapter is INERT (no DOM, island, clock,
 *     network, state or background work);
 *   * the adapter feature-detects the CURRENT native registration API
 *     (`document.modelContext.registerTool`, per
 *     https://developer.chrome.com/docs/ai/webmcp/imperative-api, last
 *     updated 2026-10-16) and degrades honestly when it is absent;
 *   * the three descriptors are derived from the embedded shared
 *     `tool-contract` resource (`watchdog_catalog/tools.json`) — never a
 *     second hard-coded schema table;
 *   * every `execute` callback forwards its argument object UNCHANGED to the
 *     real shared query engine and returns ONE complete JSON TEXT result: the
 *     pure payload plus a separate top-level `source: {mode: "browser"}`, framed
 *     by the shared typed canonical encoder (never an ordinary JSON serializer,
 *     which would collapse a `1.0` float token to `1`);
 *   * every descriptor carries `annotations: {readOnlyHint: true,
 *     untrustedContentHint: true, consequentialHint: false}`, fresh per
 *     descriptor and never a sanitizer for upstream text;
 *   * `watchdog_status` receives a wrapper-owned reference time captured at
 *     INVOCATION (not setup) through a test-injectable clock;
 *   * project schema/name budgets are PINNED here as local quality gates, not
 *     as WebMCP normative limits or runtime truncation;
 *   * a fake registrar proves ADAPTER behaviour only — never native support.
 *     Real-browser registration/execution is unit 8e's evidence; the only
 *     cross-runtime check here is CommonJS versus `vm` on ONE V8 build.
 *
 * Expected values are independent literals or frozen repository fixtures;
 * none is produced by the adapter under test. The single differential oracle
 * (`differential`/`assertPayloadParity`) compares the decoded native text
 * against DIRECT calls to the unchanged shared core, which is exactly the
 * framing contract under test.
 *
 * Run: node --test tests/js/webmcp.test.cjs
 */

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const adapter = require("../../web/webmcp.js");
const catalog = require("../../web/catalog-query.js");

const REPO_ROOT = path.join(__dirname, "..", "..");
const ADAPTER_PATH = path.join(REPO_ROOT, "web", "webmcp.js");
const ADAPTER_SOURCE = fs.readFileSync(ADAPTER_PATH, "utf8");
/** The executable half of the adapter: the documentation header excluded. */
const ADAPTER_BODY = ADAPTER_SOURCE.slice(
  ADAPTER_SOURCE.indexOf("(function (root, factory)"));
const CONTRACT_PATH = path.join(REPO_ROOT, "watchdog_catalog", "tools.json");
const CONTRACT_TEXT = fs.readFileSync(CONTRACT_PATH, "utf8");
const CONTRACT = JSON.parse(CONTRACT_TEXT);
const SNAPSHOT_TEXT = fs.readFileSync(
  path.join(__dirname, "..", "fixtures", "catalog", "lookup.json"), "utf8");
const SNAPSHOT = catalog.loadSnapshot(SNAPSHOT_TEXT);

/** The three shared tool names, as an independent literal. */
const TOOL_NAMES = [
  "get_model", "watchdog_status", "search_models"
];

/** Independent literals: the reviewed budget policy of browser contract 4.6. */
const DESCRIPTION_BUDGET = 500;
const PARAM_DESCRIPTION_BUDGET = 150;
const NAME_BUDGET = 30;

/** Independent literals: the pinned identity of the shipped resource. */
const PINNED_DESCRIPTION_LENGTHS = {
  get_model: 393,
  watchdog_status: 451,
  search_models: 494
};
const PINNED_MAX_PARAM_DESCRIPTION = 147;

/** Independent literals: the reviewed lookup fixture's timing facts. */
const FIXTURE_TICK_EPOCH = 1759200000;
const FIXTURE_CADENCE_S = 3600;

/** Independent literals: the three documented hints, exactly. */
const EXPECTED_ANNOTATION_KEYS =
  ["consequentialHint", "readOnlyHint", "untrustedContentHint"];
const EXPECTED_SOURCE = { mode: "browser" };

// ---------- fakes for the DOM / native registration boundary --------------

/**
 * A document double exposing ONLY `getElementById` plus the recorded effects.
 *
 * Any other document surface is absent on purpose: a stray `querySelector`,
 * `write`, `appendChild` or `document.body` read inside the adapter would
 * throw instead of silently succeeding, so "reads exactly the two islands and
 * modifies nothing" is observable rather than asserted by inspection.
 */
function fakeDocument(islands) {
  const reads = [];
  const document = {
    reads,
    modelContext: {},
    getElementById(id) {
      reads.push(id);
      if (!Object.prototype.hasOwnProperty.call(islands, id)) {
        return null;
      }
      return { textContent: islands[id] };
    }
  };
  return document;
}

/**
 * A registrar double. `mode` selects the observable behaviour:
 *   "ok"               — resolves, records the descriptor;
 *   "rejectAt"         — rejects the Nth call (1-based) AFTER recording it;
 *   "throwAt"          — throws synchronously on the Nth call;
 *   "thenableRejectAt" — returns an ASSIMILATED thenable rejecting on the Nth;
 *   "thenableOk"       — returns an assimilated thenable that resolves;
 *   "syncUndefined"    — returns undefined (an ordinary synchronous success);
 *   "slow"             — resolves on a later macrotask.
 * It also asserts the native method RECEIVER: `this` must be the modelContext,
 * otherwise it throws, which makes receiver preservation observable.
 */
function fakeRegistrar(mode, at) {
  const calls = [];
  const modelContext = {
    calls,
    registerTool(descriptor) {
      if (this !== modelContext) {
        throw new Error("registerTool called with the wrong receiver");
      }
      const index = calls.length + 1;
      calls.push({ index, descriptor });
      if (mode === "throwAt" && index === at) {
        throw new Error("synchronous registration failure");
      }
      if (mode === "rejectAt" && index === at) {
        return Promise.reject(new Error("registration rejected"));
      }
      if (mode === "thenableRejectAt" && index === at) {
        // Not a native Promise: `then` is the whole assimilable surface.
        return {
          then(resolve, reject) {
            reject(new Error("thenable registration rejected"));
          }
        };
      }
      if (mode === "thenableOk") {
        return { then(resolve) { resolve(); } };
      }
      if (mode === "syncUndefined") {
        return undefined;
      }
      if (mode === "slow") {
        return new Promise((resolve) => setTimeout(resolve, 5));
      }
      return Promise.resolve();
    }
  };
  return modelContext;
}

/**
 * A registrar double whose settlements are CONTROLLED by the test.
 *
 * Every call records the descriptor and returns a promise nobody settles until
 * `release()` is called; `release()` also opens the gate so later calls settle
 * as they arrive. `failAt` (1-based) rejects that one position. This is what
 * makes a genuinely PENDING native attempt observable instead of timed by a
 * sleep.
 */
function gatedRegistrar(failAt, style) {
  const calls = [];
  const gates = [];
  const fail = failAt === undefined ? 0 : failAt;
  let open = false;
  const modelContext = {
    calls,
    registerTool(descriptor) {
      if (this !== modelContext) {
        throw new Error("registerTool called with the wrong receiver");
      }
      const index = calls.length + 1;
      calls.push({ index, descriptor });
      if (style === "throw" && index === fail) {
        throw new Error("synchronous registration failure");
      }
      let settle;
      const promise = new Promise((resolve, reject) => {
        settle = () => {
          if (index === fail) {
            reject(new Error("registration rejected"));
          } else {
            resolve();
          }
        };
      });
      if (open) {
        Promise.resolve().then(settle);
      } else {
        gates.push(settle);
      }
      return promise;
    }
  };
  modelContext.release = () => {
    open = true;
    for (const settle of gates.splice(0)) {
      settle();
    }
  };
  return modelContext;
}

/** Let every queued promise job and one macrotask run. */
function settleMicrotasks() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function islandsFor(contractText, snapshotText) {
  return {
    "catalog-data": snapshotText === undefined ? SNAPSHOT_TEXT : snapshotText,
    "tool-contract": contractText === undefined ? CONTRACT_TEXT : contractText
  };
}

/** Evaluate adapter source as a classic script in an isolated vm. */
function loadClassicSource(source, sandboxGlobals) {
  const sandbox = Object.assign({}, sandboxGlobals);
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return sandbox;
}

/** Evaluate the real adapter source as a classic script in an isolated vm. */
function loadClassic(sandboxGlobals) {
  return loadClassicSource(ADAPTER_SOURCE, sandboxGlobals);
}

const CATALOG_SOURCE = fs.readFileSync(
  path.join(REPO_ROOT, "web", "catalog-query.js"), "utf8");

/**
 * Evaluate the page's TWO classic scripts in ONE vm realm, in page order.
 *
 * The real page inlines both into one document, so one realm is the faithful
 * shape; handing a host-realm engine to a sandboxed adapter would be a
 * cross-realm artifact of the harness, not of the page. `adapterSource` lets
 * the applied wrong-rule controls run a MUTATED copy here unchanged.
 */
function loadPageRealm(adapterSource) {
  const sandbox = { Date };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(CATALOG_SOURCE, sandbox);
  vm.runInContext(adapterSource === undefined ? ADAPTER_SOURCE : adapterSource,
                  sandbox);
  return sandbox;
}

/** Register the page realm's adapter against a sealed snapshot text. */
async function registerPageRealm(sandbox, snapshotText, contractText) {
  const document = fakeDocument(islandsFor(contractText, snapshotText));
  const modelContext = fakeRegistrar("ok", 0);
  document.modelContext = modelContext;
  const result = await sandbox.WatchdogWebMCP.setup({
    document,
    catalog: sandbox.WatchdogCatalog,
    clock: () => FIXTURE_TICK_EPOCH
  });
  return { result, modelContext };
}

/** Counted seams used to prove "no clock / no network" during a step. */
function countedSeams() {
  const counters = { clock: 0, fetch: 0, timers: 0 };
  return {
    counters,
    Date: Object.assign(Object.create(Date), {
      now() { counters.clock += 1; return 1761600000; }
    }),
    fetch() { counters.fetch += 1; throw new Error("no network allowed"); },
    setTimeout() { counters.timers += 1; throw new Error("no timers allowed"); }
  };
}

// ---------- the ONE native-exit decoder -----------------------------------

/**
 * Decode one native text result at the boundary this unit owns.
 *
 * The assertions inside this helper ARE the framing contract: the callback
 * returns JSON TEXT (never a bare object), that text decodes with the shared
 * strict loader, and removing ONLY the top-level `source` leaves the pure
 * query payload. Everything the payload contains is left untouched — no field
 * renamed, no array shortened, no count re-derived.
 */
function decodeNative(output) {
  assert.equal(typeof output, "string",
               "a native tool result must be JSON text, not a raw object");
  assert.ok(output.length > 0, "the text result is not empty");
  const parsed = catalog.loadJson(output);
  assert.ok(parsed !== null && typeof parsed === "object" && !Array.isArray(parsed),
            "the text decodes to one JSON object");
  const source = parsed.source;
  delete parsed.source;
  return { payload: parsed, source, text: output };
}

/** Decode a callback's text result and return just the pure payload. */
function nativePayload(descriptor, args) {
  return decodeNative(descriptor.execute(args)).payload;
}

/** The exact-source assertion, reusable by the applied wrong-rule control. */
function assertExactSource(source) {
  assert.deepEqual(source, EXPECTED_SOURCE,
                   "the embedded source is exactly {mode: \"browser\"}");
  assert.deepEqual(Object.keys(source), ["mode"],
                   "no URL, path, cache or retrieval field is invented");
}

/** The typed-canonical parity assertion, reusable by wrong-rule controls. */
function assertPayloadParity(actual, expected, label) {
  assert.deepEqual(actual, expected,
                   `${label}: decoded text equals the direct core payload`);
  assert.equal(catalog.canonicalText(actual), catalog.canonicalText(expected),
               `${label}: identical typed canonical text`);
}

/** The exact-annotation assertion, reusable by the wrong-rule control. */
function assertAnnotationTriplet(annotations) {
  assert.ok(annotations !== null && typeof annotations === "object",
            "annotations are present");
  assert.deepEqual(Object.keys(annotations).sort(), EXPECTED_ANNOTATION_KEYS,
                   "exactly the three documented hints are advertised");
  assert.equal(annotations.readOnlyHint, true, "readOnlyHint is exactly true");
  assert.equal(annotations.untrustedContentHint, true,
               "untrustedContentHint is exactly true");
  assert.equal(annotations.consequentialHint, false,
               "consequentialHint is exactly false");
}

// ---------- independent drift detectors -----------------------------------

/**
 * Compare registered descriptors against the shipped resource.
 *
 * Returns a list of human-readable findings, empty when the adapter derives
 * every name/description/schema from the contract verbatim. Used positively
 * (real adapter → no findings) and as the bounded applied wrong-rule control
 * (a mutated adapter copy must produce findings).
 */
/** JSON-shape view of a value, so values from another vm realm compare. */
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function driftFindings(descriptors, contract) {
  const findings = [];
  const byName = new Map(contract.map((entry) => [entry.name, entry]));
  if (descriptors.length !== contract.length) {
    findings.push(`descriptor count ${descriptors.length} != ${contract.length}`);
  }
  const seen = new Set();
  for (const descriptor of descriptors) {
    const expected = byName.get(descriptor.name);
    if (!expected) {
      findings.push(`unknown registered tool ${descriptor.name}`);
      continue;
    }
    if (seen.has(descriptor.name)) {
      findings.push(`duplicate registered tool ${descriptor.name}`);
    }
    seen.add(descriptor.name);
    if (descriptor.description !== expected.description) {
      findings.push(`description drift for ${descriptor.name}`);
    }
    try {
      assert.deepEqual(plain(descriptor.inputSchema), expected.inputSchema);
    } catch (error) {
      findings.push(`inputSchema drift for ${descriptor.name}`);
    }
    if (typeof descriptor.execute !== "function") {
      findings.push(`execute is not callable for ${descriptor.name}`);
    }
  }
  for (const entry of contract) {
    if (!seen.has(entry.name)) {
      findings.push(`tool ${entry.name} was never registered`);
    }
  }
  return findings;
}

/** Every (tool, parameter) description/name length fact of one contract. */
function budgetRows(contract) {
  const rows = [];
  for (const entry of contract) {
    rows.push({
      kind: "tool-name", tool: entry.name, name: entry.name,
      length: entry.name.length
    });
    rows.push({
      kind: "tool-description", tool: entry.name, name: entry.name,
      length: entry.description.length
    });
    for (const [name, schema] of Object.entries(entry.inputSchema.properties)) {
      rows.push({
        kind: "parameter-name", tool: entry.name, name, length: name.length
      });
      rows.push({
        kind: "parameter-description", tool: entry.name, name,
        length: schema.description.length
      });
    }
  }
  return rows;
}

const BUDGET_LIMITS = {
  "tool-name": NAME_BUDGET,
  "tool-description": DESCRIPTION_BUDGET,
  "parameter-name": NAME_BUDGET,
  "parameter-description": PARAM_DESCRIPTION_BUDGET
};

/** Rows that exceed the project's local budget (empty when compliant). */
function overBudget(contract) {
  return budgetRows(contract).filter(
    (row) => row.length > BUDGET_LIMITS[row.kind]);
}

// ---------- sealed synthetic catalogue builders ---------------------------

/** Seal a snapshot with the shared canonical hash and return its text. */
function seal(snapshot) {
  snapshot.snapshot_id = catalog.snapshotHash(snapshot);
  return { text: JSON.stringify(snapshot), snapshot };
}

/**
 * Build a sealed synthetic snapshot with `count` models on one gateway.
 *
 * The seal uses the shared canonical hash, so the synthetic catalogue is a
 * genuine input for `loadSnapshot` rather than a fixture-shaped hand wave.
 * `padding` controls the per-model description size, which is what makes the
 * small/large output-size evidence meaningful; `descriptionOf` overrides it per
 * model (used for the hostile-text framing test).
 */
function syntheticSnapshot(count, padding, descriptionOf) {
  const models = {};
  const modelIds = [];
  for (let index = 0; index < count; index += 1) {
    const rawId = `synthetic-model-${String(index).padStart(3, "0")}`;
    modelIds.push(rawId);
    models[rawId] = {
      architecture: {
        input_modalities: ["text"], output_modalities: ["text"]
      },
      context_length: 4096 + index,
      description: descriptionOf !== undefined ? descriptionOf(index)
        : padding === 0 ? null : "d".repeat(padding),
      id: rawId,
      name: `Synthetic Model ${index}`,
      supported_parameters: ["temperature"]
    };
  }
  return seal({
    cadence_s: FIXTURE_CADENCE_S,
    provider_order: ["synthetic-bulk"],
    providers: {
      "synthetic-bulk": {
        api_type: "openai",
        chat_completions_url:
          "https://synthetic-bulk.example/v1/chat/completions",
        display_name: "Synthetic Bulk",
        model_ids: modelIds,
        models,
        stale: false
      }
    },
    schema_version: 2,
    tick_epoch: FIXTURE_TICK_EPOCH,
    warnings: [],
    snapshot_id: ""
  });
}

/**
 * A sealed snapshot whose ordinary string values look hostile: an HTML/script
 * break-out attempt, a Unicode line separator, quotes and backslashes, and
 * prototype-looking identifiers. Used to prove framing treats upstream text as
 * DATA — preserved byte for byte, never executed, never repaired.
 */
const HOSTILE_ID = "vendor/__proto__-free";
const HOSTILE_NAME = "constructor";
const HOSTILE_DESCRIPTION =
  "</script><img src=x onerror=alert(1)>\u2028\"quoted\" \\ __proto__ $& \u{1F600}";

function hostileSnapshot() {
  const model = {
    architecture: {
      input_modalities: ["text"], output_modalities: ["text"]
    },
    context_length: 8192,
    description: HOSTILE_DESCRIPTION,
    id: HOSTILE_ID,
    name: HOSTILE_NAME,
    supported_parameters: ["temperature"]
  };
  return seal({
    cadence_s: FIXTURE_CADENCE_S,
    provider_order: ["synthetic-hostile"],
    providers: {
      "synthetic-hostile": {
        api_type: "openai",
        chat_completions_url:
          "https://synthetic-hostile.example/v1/chat/completions",
        display_name: "Synthetic Hostile",
        model_ids: [HOSTILE_ID],
        models: { [HOSTILE_ID]: model },
        stale: false
      }
    },
    schema_version: 2,
    tick_epoch: FIXTURE_TICK_EPOCH,
    warnings: [],
    snapshot_id: ""
  });
}

/** Register against a sealed snapshot text and return the descriptors. */
async function registerWith(snapshotText, contractText, clock, implementation) {
  const document = fakeDocument(islandsFor(contractText, snapshotText));
  const modelContext = fakeRegistrar("ok", 0);
  document.modelContext = modelContext;
  const api = implementation === undefined ? adapter : implementation;
  const result = await api.setup({
    document, catalog,
    clock: clock === undefined ? () => FIXTURE_TICK_EPOCH : clock
  });
  return { result, modelContext, document };
}

/** The registered descriptor with the given name, or a loud failure. */
function descriptorFor(modelContext, name) {
  const call = modelContext.calls.find((entry) => entry.descriptor.name === name);
  assert.ok(call, `tool ${name} was registered`);
  return call.descriptor;
}

/**
 * The REAL shared engine, wrapped in a call counter.
 *
 * Observation only: every member is the accepted implementation, delegated to
 * unchanged, so a count of `loadSnapshot`/`loadJson` invocations says whether a
 * duplicate setup re-parsed the islands without substituting a fake engine.
 */
function countedCatalog() {
  const counts = { loadJson: 0, loadSnapshot: 0 };
  const engine = Object.assign({}, catalog, {
    loadJson(text) {
      counts.loadJson += 1;
      return catalog.loadJson(text);
    },
    loadSnapshot(text) {
      counts.loadSnapshot += 1;
      return catalog.loadSnapshot(text);
    }
  });
  return { catalog: engine, counts };
}

/**
 * Observe ONE sequential duplicate setup against a given adapter implementation.
 *
 * The second call changes the catalogue island and the clock and passes an
 * engine with no seams at all: a document that already had a registration
 * attempt must ignore every one of those. Values are JSON-normalised so the
 * same helper runs a host-realm and a page-realm (mutant) adapter.
 */
async function duplicateObservation(api, engine, duplicateEngine) {
  const islands = islandsFor();
  const document = fakeDocument(islands);
  const modelContext = fakeRegistrar("ok", 0);
  document.modelContext = modelContext;
  const first = await api.setup({
    document, catalog: engine, clock: () => FIXTURE_TICK_EPOCH
  });
  islands["catalog-data"] = syntheticSnapshot(3, 0).text;
  const duplicate = await api.setup({
    document,
    // By default a core object with NO seams at all: an owned document must not
    // consult it. The mutant control passes a WORKING engine instead, so a
    // removed guard surfaces as a real second registration rather than as a
    // pre-registration `core_unavailable` refusal.
    catalog: duplicateEngine === undefined ? {} : duplicateEngine,
    clock: () => FIXTURE_TICK_EPOCH + 60
  });
  return {
    calls: modelContext.calls.length,
    reads: document.reads.length,
    first: plain(first),
    duplicate: plain(duplicate)
  };
}

/** The document-lifetime ownership assertion, shared with the mutant control. */
function assertOwnedOnce(observation) {
  assert.equal(observation.calls, 3, "exactly one native call per tool");
  assert.equal(observation.reads, 2, "one snapshot load and one contract parse");
  assert.equal(observation.first.status, "registered");
  assert.equal(observation.duplicate.status, "registered",
               "the duplicate reports the owned outcome");
  assert.deepEqual(observation.duplicate.registered, TOOL_NAMES,
                   "the duplicate names the same three registered tools");
  assert.deepEqual(observation.duplicate, observation.first,
                   "the duplicate reports the same outcome values");
}

/** Observe one synchronous native throw at position 2 (a partial attempt). */
async function failureObservation(api, engine) {
  const document = fakeDocument(islandsFor());
  const modelContext = fakeRegistrar("throwAt", 2);
  document.modelContext = modelContext;
  const outcome = await api.setup({
    document, catalog: engine, clock: () => FIXTURE_TICK_EPOCH
  });
  return {
    outcome: plain(outcome),
    attempted: modelContext.calls.map((entry) => entry.descriptor.name)
  };
}

/** The honest-partial-failure assertion, shared with the mutant control. */
function assertHonestFailure(observation) {
  assert.equal(observation.outcome.status, "registration_failed");
  assert.equal(observation.outcome.reason, "native_registration_threw");
  assert.deepEqual(observation.outcome.registered,
                   ["get_model"],
                   "the confirmed prefix is reported exactly");
  assert.equal(observation.outcome.failed, "watchdog_status",
               "the actual failed tool name is reported");
  assert.deepEqual(observation.attempted, TOOL_NAMES.slice(0, 2),
                   "no tool is attempted after the failure");
}

// ==========================================================================

describe("unit 8b — inert evaluation and honest absence", () => {
  it("publishes only its namespaced export and touches nothing at load", () => {
    const seams = countedSeams();
    const sandbox = loadClassic({
      Date: seams.Date, fetch: seams.fetch, setTimeout: seams.setTimeout,
      document: { getElementById() { throw new Error("DOM touched at load"); } }
    });
    assert.deepEqual(Object.keys(sandbox.WatchdogWebMCP).sort(),
                     ["TOOL_NAMES", "setup"]);
    assert.deepEqual(seams.counters, { clock: 0, fetch: 0, timers: 0 });
    // Re-evaluating the same classic script is idempotent, never a throw.
    vm.runInContext(ADAPTER_SOURCE, sandbox);
    assert.equal(typeof sandbox.WatchdogWebMCP.setup, "function");
  });

  it("is inert when required from CommonJS, even with a hostile document", () => {
    const before = Object.getOwnPropertyDescriptor(globalThis, "document");
    let touched = 0;
    Object.defineProperty(globalThis, "document", {
      configurable: true,
      get() { touched += 1; throw new Error("document read at require time"); }
    });
    try {
      delete require.cache[require.resolve("../../web/webmcp.js")];
      const fresh = require("../../web/webmcp.js");
      assert.deepEqual(Object.keys(fresh).sort(), ["TOOL_NAMES", "setup"]);
      assert.equal(touched, 0);
    } finally {
      if (before === undefined) {
        delete globalThis.document;
      } else {
        Object.defineProperty(globalThis, "document", before);
      }
      delete require.cache[require.resolve("../../web/webmcp.js")];
      require("../../web/webmcp.js");
    }
    assert.equal(touched, 0);
  });

  it("exports the three shared tool names as an immutable published set", () => {
    assert.deepEqual(adapter.TOOL_NAMES, TOOL_NAMES);
    assert.ok(Object.isFrozen(adapter.TOOL_NAMES));
    assert.throws(() => { adapter.TOOL_NAMES.push("extra_tool"); },
                  "the published name set cannot be extended");
    assert.deepEqual(adapter.TOOL_NAMES, TOOL_NAMES);
  });

  it("reports unsupported without reading islands, the clock or the DOM", async () => {
    const document = fakeDocument(islandsFor());
    delete document.modelContext;
    const seams = countedSeams();
    const result = await adapter.setup({
      document, catalog, clock: () => { seams.counters.clock += 1; return 0; }
    });
    assert.deepEqual(result, {
      ok: false,
      status: "unsupported",
      reason: "native_api_absent",
      registered: [],
      failed: null,
      code: null
    });
    assert.deepEqual(document.reads, [], "no island was parsed");
    assert.equal(seams.counters.clock, 0, "no clock read on the absent path");
    assert.equal(seams.counters.fetch, 0);
    assert.equal(document.modelContext, undefined);
    assert.deepEqual(Object.keys(document).sort(),
                     ["getElementById", "reads"]);
  });

  it("treats a non-callable native API as absent, never as an error", async () => {
    for (const value of [null, "registerTool", 42, {}]) {
      const document = fakeDocument(islandsFor());
      document.modelContext = value === null ? null : { registerTool: value };
      if (value === null) {
        document.modelContext = null;
      }
      const result = await adapter.setup({ document, catalog });
      assert.equal(result.status, "unsupported", `for API ${String(value)}`);
      assert.equal(result.reason, "native_api_absent");
      assert.deepEqual(document.reads, []);
    }
  });

  it("degrades honestly when no document seam exists at all", async () => {
    const result = await adapter.setup({ document: undefined, catalog });
    assert.equal(result.status, "unsupported");
    assert.equal(result.reason, "native_api_absent");
    const nothing = await adapter.setup();
    assert.equal(nothing.status, "unsupported");
  });

  it("fails before registration when a shared core seam is missing", async () => {
    const document = fakeDocument(islandsFor());
    document.modelContext = fakeRegistrar("ok", 0);
    const result = await adapter.setup({ document, catalog: {} });
    assert.equal(result.ok, false);
    assert.equal(result.status, "invalid");
    assert.equal(result.reason, "core_unavailable");
    assert.deepEqual(document.modelContext.calls, []);
    assert.deepEqual(document.reads, [], "no island parsed without a core");

    // The typed canonical encoder is a REQUIRED seam: without it the adapter
    // could only mis-frame a payload, so setup refuses instead of substituting.
    const noEncoder = Object.assign({}, catalog);
    delete noEncoder.canonicalText;
    const second = fakeDocument(islandsFor());
    second.modelContext = fakeRegistrar("ok", 0);
    const encodedAbsent = await adapter.setup({ document: second, catalog: noEncoder });
    assert.equal(encodedAbsent.status, "invalid");
    assert.equal(encodedAbsent.reason, "core_unavailable");
    assert.deepEqual(second.modelContext.calls, []);
  });

  it("contains no network primitive and no out-of-scope lifecycle surface", () => {
    for (const needle of ["fetch(", "XMLHttpRequest", "import(", "require(",
                          "sendBeacon", "WebSocket", "polyfill",
                          "navigator.modelContext"]) {
      assert.equal(ADAPTER_BODY.includes(needle), false,
                   `adapter code must not reference ${needle}`);
    }
    assert.ok(ADAPTER_BODY.length > 0, "the executable half was found");
    // 8c owns document-lifetime ownership (implemented here); a public
    // unregister seam stays out of scope, and 8d owns inlining/auto-setup.
    for (const needle of ["unregisterTool", "exposedTo",
                          "AbortController", "addEventListener",
                          "registerTool.call(native.modelContext, descriptor,"]) {
      assert.equal(ADAPTER_BODY.includes(needle), false,
                   `8b must not pre-empt a later unit's surface: ${needle}`);
    }
    // The native boundary is text framed by the SHARED encoder, never an
    // ordinary JSON serializer that would lose float-token identity.
    assert.equal(ADAPTER_BODY.includes("JSON.stringify"), false,
                 "framing must not use an ordinary JSON serializer");
    assert.ok(ADAPTER_BODY.includes("catalog.canonicalText(attachSource(payload))"),
              "the shared typed canonical encoder frames every result");
    assert.ok(ADAPTER_BODY.includes("annotations: browserAnnotations()"),
              "every descriptor carries the fresh annotation set");
    assert.ok(ADAPTER_SOURCE.includes("modelContext.registerTool"),
              "the current native member is the detected seam");
    assert.ok(ADAPTER_SOURCE.includes("registerTool.call(native.modelContext"),
              "the native receiver is preserved explicitly");
  });
});

describe("unit 8b — registration binds the embedded shared definitions", () => {
  it("registers exactly the three shared descriptors in contract order", async () => {
    const { result, modelContext } = await registerWith();
    assert.deepEqual(result, {
      ok: true,
      status: "registered",
      reason: null,
      registered: TOOL_NAMES,
      failed: null,
      code: null
    });
    assert.deepEqual(modelContext.calls.map((entry) => entry.descriptor.name),
                     CONTRACT.map((entry) => entry.name));
    assert.deepEqual(driftFindings(
      modelContext.calls.map((entry) => entry.descriptor), CONTRACT), []);
    for (const { descriptor } of modelContext.calls) {
      assert.deepEqual(Object.keys(descriptor).sort(),
                       ["annotations", "description", "execute", "inputSchema",
                        "name"],
                       "8b registers annotations plus the reviewed fields only");
    }
  });

  it("derives every name, description and nested schema from the island", async () => {
    const { modelContext } = await registerWith();
    for (const entry of CONTRACT) {
      const descriptor = descriptorFor(modelContext, entry.name);
      assert.equal(descriptor.name, entry.name);
      assert.equal(descriptor.description, entry.description);
      assert.deepEqual(descriptor.inputSchema, entry.inputSchema);
    }
    // Spot-check the preserved constraint surface, never a repaired schema.
    const search = descriptorFor(modelContext, "search_models").inputSchema;
    assert.equal(search.additionalProperties, false);
    assert.deepEqual(search.required, []);
    assert.equal(search.properties.limit.maximum, 200);
    assert.equal(search.properties.limit.default, 5);
    assert.equal(search.properties.offset.maximum, 9007199254740991);
    assert.deepEqual(search.properties.min_context_length.type,
                     ["integer", "null"]);
    assert.deepEqual(search.properties.input_modalities.items,
                     { type: "string", minLength: 1 });
    const model = descriptorFor(modelContext, "get_model").inputSchema;
    assert.deepEqual(model.required, ["provider", "model_id"]);
    assert.equal(model.properties.provider.type, "string");
    assert.equal(model.properties.model_id.pattern,
                 "[^\\u0009-\\u000D\\u0020]");
    assert.deepEqual(descriptorFor(modelContext, "watchdog_status")
                       .inputSchema.properties, {});
  });

  it("keeps the shipped resource inside the pinned project budgets", () => {
    const rows = budgetRows(CONTRACT);
    assert.deepEqual(
      Object.fromEntries(TOOL_NAMES.map((name) => [
        name, CONTRACT.find((entry) => entry.name === name).description.length
      ])), PINNED_DESCRIPTION_LENGTHS, "pinned description identity");
    const paramDescriptions = rows.filter(
      (row) => row.kind === "parameter-description");
    assert.equal(Math.max(...paramDescriptions.map((row) => row.length)),
                 PINNED_MAX_PARAM_DESCRIPTION);
    assert.deepEqual(overBudget(CONTRACT), [],
                     "every tool/parameter name and description is in budget");
    assert.ok(rows.length > 20, "the lint actually walked the resource");
    // The budgets are project gates, not normative WebMCP limits.
    assert.equal(DESCRIPTION_BUDGET, 500);
    assert.equal(PARAM_DESCRIPTION_BUDGET, 150);
    assert.equal(NAME_BUDGET, 30);
  });

  it("the lint rejects an over-budget contract instead of truncating it", () => {
    const mutated = JSON.parse(CONTRACT_TEXT);
    mutated[0].description = "x".repeat(DESCRIPTION_BUDGET + 1);
    mutated[2].inputSchema.properties.query.description =
      "y".repeat(PARAM_DESCRIPTION_BUDGET + 1);
    const findings = overBudget(mutated).map((row) => row.kind + ":" + row.tool);
    assert.deepEqual(findings, [
      "tool-description:get_model", "parameter-description:search_models"
    ]);
  });

  it("awaits asynchronous native registration before reporting success", async () => {
    const document = fakeDocument(islandsFor());
    const modelContext = fakeRegistrar("slow", 0);
    document.modelContext = modelContext;
    let settled = false;
    const pending = adapter.setup({ document, catalog }).then((result) => {
      settled = true;
      return result;
    });
    assert.equal(settled, false, "setup does not report success synchronously");
    assert.ok(modelContext.calls.length < 3,
              "the registration sequence is not finished synchronously");
    const result = await pending;
    assert.equal(result.status, "registered");
    assert.equal(modelContext.calls.length, 3);
  });

  it("preserves the native method receiver", async () => {
    // `fakeRegistrar` throws when `this` is not the modelContext object, so a
    // detached call would surface as registration_failed instead.
    const { result } = await registerWith();
    assert.equal(result.status, "registered");
    assert.deepEqual(result.registered, TOOL_NAMES);
  });

  it("hands the registrar defensive copies, never the captured contract", async () => {
    const document = fakeDocument(islandsFor());
    const modelContext = fakeRegistrar("ok", 0);
    document.modelContext = modelContext;
    const first = await adapter.setup({ document, catalog });
    assert.equal(first.status, "registered");
    // Mutate everything the native layer was handed.
    for (const { descriptor } of modelContext.calls) {
      descriptor.description = "mutated";
      descriptor.inputSchema.type = "string";
      descriptor.annotations.readOnlyHint = false;
      delete descriptor.annotations.untrustedContentHint;
      for (const value of Object.values(descriptor.inputSchema.properties)) {
        delete value.description;
      }
    }
    const secondDocument = fakeDocument(islandsFor());
    secondDocument.modelContext = fakeRegistrar("ok", 0);
    const second = await adapter.setup({
      document: secondDocument, catalog,
      clock: () => FIXTURE_TICK_EPOCH
    });
    assert.equal(second.status, "registered");
    for (const { descriptor } of secondDocument.modelContext.calls) {
      assert.deepEqual(plain(descriptor.inputSchema),
                       CONTRACT.find((entry) => entry.name === descriptor.name)
                         .inputSchema);
      assertAnnotationTriplet(descriptor.annotations);
    }
    const fresh = await registerWith();
    assert.deepEqual(driftFindings(
      fresh.modelContext.calls.map((entry) => entry.descriptor), CONTRACT), [],
      "a native mutation cannot leak into a later registration");
  });

  it("uses the same implementation from the classic-script export", async () => {
    let fetchCalls = 0;
    const sandbox = loadPageRealm();
    Object.defineProperty(sandbox, "fetchCalls", {
      get() { return fetchCalls; }
    });
    // The page realm's own engine, plus a fetch trap: dispatch must be local.
    sandbox.fetch = () => {
      fetchCalls += 1;
      throw new Error("no network allowed");
    };
    const classic = sandbox.WatchdogWebMCP;
    assert.deepEqual([...classic.TOOL_NAMES], TOOL_NAMES);
    const classicContext = await registerPageRealm(sandbox);
    assert.equal(classicContext.result.status, "registered");
    assert.deepEqual(driftFindings(
      classicContext.modelContext.calls.map((entry) => entry.descriptor),
      CONTRACT), [], "the classic copy derives the same definitions");
    const commonContext = await registerWith();
    assert.deepEqual(
      plain(classicContext.modelContext.calls.map(
        (entry) => [entry.descriptor.name, entry.descriptor.description,
                    entry.descriptor.inputSchema,
                    plain(entry.descriptor.annotations)])),
      plain(commonContext.modelContext.calls.map(
        (entry) => [entry.descriptor.name, entry.descriptor.description,
                    entry.descriptor.inputSchema, entry.descriptor.annotations])));
    // Same behaviour through the dispatch seam, not just the same shape.
    const classicSearch = descriptorFor(classicContext.modelContext, "search_models")
      .execute({ limit: 2 });
    const commonSearch = descriptorFor(commonContext.modelContext, "search_models")
      .execute({ limit: 2 });
    assert.equal(typeof classicSearch, "string");
    assert.equal(classicSearch, commonSearch,
                 "both scripts frame the identical text");
    assert.equal(sandbox.fetchCalls, 0, "dispatch performs no network call");
  });
});

describe("unit 8b — native text result, embedded source and payload parity", () => {
  it("returns one JSON text result for every shared tool with exact parity", async () => {
    const now = FIXTURE_TICK_EPOCH + 90;
    const { modelContext } = await registerWith(undefined, undefined, () => now);
    const cases = [
      ["get_model", "getModel",
       { provider: "synthetic-a", model_id: "vendor/shared" }],
      ["search_models", "searchModels", { query: "synthetic", limit: 2 }]
    ];
    for (const [tool, coreName, args] of cases) {
      const descriptor = descriptorFor(modelContext, tool);
      const decoded = decodeNative(descriptor.execute(Object.assign({}, args)));
      assertExactSource(decoded.source);
      assert.equal(decoded.payload.ok, true, `${tool} returns a success`);
      assertPayloadParity(decoded.payload,
                          catalog[coreName](SNAPSHOT, Object.assign({}, args)),
                          tool);
      assert.notEqual(decoded.payload, args,
                      `${tool} returns a fresh payload`);
    }
    const status = decodeNative(
      descriptorFor(modelContext, "watchdog_status").execute());
    assertExactSource(status.source);
    assertPayloadParity(status.payload, catalog.watchdogStatus(SNAPSHOT, {}, now),
                        "watchdog_status");
    assert.equal(status.payload.last_tick_age_s, 90);
  });

  it("keeps the frozen literal expectations for status and search", async () => {
    const now = FIXTURE_TICK_EPOCH + 120;
    const { modelContext } = await registerWith(undefined, undefined, () => now);

    const status = nativePayload(descriptorFor(modelContext, "watchdog_status"), {});
    assert.equal(status.ok, true);
    assert.equal(status.tick_epoch, FIXTURE_TICK_EPOCH);
    assert.equal(status.cadence_s, FIXTURE_CADENCE_S);
    assert.equal(status.last_tick_age_s, 120);
    assert.equal(status.tick_fresh, true);
    assert.deepEqual(status.warnings, []);
    assert.equal(status.snapshot_id, SNAPSHOT.snapshot_id);
    assert.deepEqual(status.counts, {
      gateways: 3, endpoints: 5, distinct_raw_ids: 4, marker_group_labels: 3
    });

    const search = nativePayload(descriptorFor(modelContext, "search_models"),
                                 { query: "synthetic", limit: 2, offset: 0 });
    assert.equal(search.ok, true);
    assert.equal(search.total_matches, 2);
    assert.equal(search.next_offset, null);
    assert.deepEqual(Object.keys(search).sort(), [
      "endpoints", "next_offset", "ok", "provider_staleness", "snapshot_id",
      "tick_epoch", "total_matches"]);
    assert.deepEqual(search.endpoints.map((row) => row.provider + "/" + row.model_id),
                     ["synthetic-a/vendor/shared", "synthetic-b/vendor/shared"]);
    assert.equal("description" in search.endpoints[0], false);

  });

  it("attaches the source to invalid arguments without promoting ok:false", async () => {
    const { modelContext } = await registerWith();
    const search = descriptorFor(modelContext, "search_models");
    const expected = catalog.searchModels(SNAPSHOT, null);
    const decoded = decodeNative(search.execute(null));
    assertExactSource(decoded.source);
    assertPayloadParity(decoded.payload, expected, "invalid arguments");
    assert.equal(decoded.payload.ok, false, "an argument error stays an error");
    assert.deepEqual(decoded.payload.error,
                     { code: "invalid_arguments",
                       message: "Invalid tool arguments.", parameter: null });

    // An error the engine itself classifies keeps its own code and parameter.
    const unknownProvider = decodeNative(
      search.execute({ provider: "not-a-gateway" }));
    assertExactSource(unknownProvider.source);
    assert.equal(unknownProvider.payload.ok, false);
    assert.equal(unknownProvider.payload.error.code, "unknown_provider");
    assert.equal(unknownProvider.payload.error.parameter, "provider");

    // Unknown keys on the no-argument tool are rejected, never ignored.
    const status = decodeNative(
      descriptorFor(modelContext, "watchdog_status").execute({ anything: true }));
    assertExactSource(status.source);
    assert.equal(status.payload.error.code, "invalid_arguments");

    // The source of one call is a fresh object, not shared state.
    const first = decodeNative(search.execute(null)).source;
    const second = decodeNative(search.execute(null)).source;
    assert.deepEqual(first, second);
    assert.notEqual(first, second, "each invocation gets its own source object");
  });

  it("keeps omitted arguments distinct from explicit null", async () => {
    const { modelContext } = await registerWith();
    const search = descriptorFor(modelContext, "search_models");
    // Omitted argument object: the documented native invocation boundary.
    const omitted = decodeNative(search.execute());
    assertExactSource(omitted.source);
    assert.equal(omitted.payload.ok, true);
    assert.equal("limit" in omitted.payload, false);
    assertPayloadParity(omitted.payload, catalog.searchModels(SNAPSHOT, {}),
                        "omitted arguments");
    // Explicit null is forwarded unchanged and stays its own rejection.
    assert.equal(decodeNative(search.execute(null)).payload.ok, false);
    assert.equal(
      decodeNative(descriptorFor(modelContext, "get_model").execute(null))
        .payload.error.code, "invalid_arguments");
  });

  it("forwards arrays, unknown keys and capability lists unchanged", async () => {
    const { modelContext } = await registerWith();
    const search = descriptorFor(modelContext, "search_models");
    assertPayloadParity(decodeNative(search.execute({ limit: 1, unknown: 1 })).payload,
                        catalog.searchModels(SNAPSHOT, { limit: 1, unknown: 1 }),
                        "unknown own key");
    assertPayloadParity(decodeNative(search.execute([1, 2])).payload,
                        catalog.searchModels(SNAPSHOT, [1, 2]), "array argument");
    const capabilities = { input_modalities: [], supported_parameters: [] };
    assertPayloadParity(decodeNative(search.execute(capabilities)).payload,
                        catalog.searchModels(SNAPSHOT, capabilities),
                        "empty capability lists");
  });

  it("captures the status clock at invocation, once, never at setup", async () => {
    let reads = 0;
    let now = FIXTURE_TICK_EPOCH + 10;
    const { modelContext } = await registerWith(undefined, undefined,
                                               () => { reads += 1; return now; });
    assert.equal(reads, 0, "setup reads no clock");

    const status = descriptorFor(modelContext, "watchdog_status");
    const first = nativePayload(status, {});
    assert.equal(reads, 1);
    assert.equal(first.last_tick_age_s, 10);
    now = FIXTURE_TICK_EPOCH + FIXTURE_CADENCE_S + 1;
    const second = nativePayload(status, {});
    assert.equal(reads, 2);
    assert.equal(second.last_tick_age_s, FIXTURE_CADENCE_S + 1);
    assert.equal(second.tick_fresh, false);
    assert.ok(second.warnings.includes("tick_stale"));
    // The other two callbacks never touch the clock.
    descriptorFor(modelContext, "get_model").execute(
      { provider: "synthetic-a", model_id: "x" });
    descriptorFor(modelContext, "search_models").execute({ limit: 1 });
    assert.equal(reads, 2, "only watchdog_status reads the clock");
    // The clock is not a public tool argument or schema field.
    for (const entry of CONTRACT) {
      assert.equal(Object.prototype.hasOwnProperty.call(
        entry.inputSchema.properties, "now"), false);
    }
  });

  it("truncates a fractional clock to whole epoch seconds like the server", async () => {
    const { modelContext } = await registerWith(
      undefined, undefined, () => FIXTURE_TICK_EPOCH + 30.9);
    const status = nativePayload(descriptorFor(modelContext, "watchdog_status"), {});
    assert.equal(status.last_tick_age_s, 30);
    assert.equal(Number.isInteger(status.last_tick_age_s), true);
  });

  it("raises one fixed error for an unusable clock reading, before framing", async () => {
    for (const bad of [NaN, Infinity, -1, "123", null, true]) {
      const { modelContext } = await registerWith(
        undefined, undefined, () => bad);
      const status = descriptorFor(modelContext, "watchdog_status");
      assert.throws(() => status.execute({}),
                    /usable wall-clock reading/,
                    `clock value ${String(bad)} is rejected`);
    }
  });

  it("decodes and seals the snapshot once, then queries that capture only", async () => {
    const QUERY_NAMES = ["getModel", "watchdogStatus", "searchModels"];
    const counts = { loadSnapshot: 0, loadJson: 0, queries: 0, encoded: 0 };
    const spy = {};
    for (const name of Object.keys(catalog)) {
      spy[name] = typeof catalog[name] === "function"
        ? function () {
          if (name === "loadSnapshot" || name === "loadJson") {
            counts[name] += 1;
          } else if (name === "canonicalText") {
            counts.encoded += 1;
          } else if (QUERY_NAMES.includes(name)) {
            counts.queries += 1;
          }
          return catalog[name].apply(catalog, arguments);
        }
        : catalog[name];
    }
    const islands = islandsFor();
    const document = fakeDocument(islands);
    const modelContext = fakeRegistrar("ok", 0);
    document.modelContext = modelContext;
    await adapter.setup({
      document, catalog: spy, clock: () => FIXTURE_TICK_EPOCH
    });
    assert.equal(counts.loadSnapshot, 1, "snapshot validated exactly once");
    assert.equal(counts.loadJson, 1, "contract decoded exactly once");
    const queriesAfterSetup = counts.queries;

    const search = descriptorFor(modelContext, "search_models");
    const first = nativePayload(search, { limit: 1 });
    // Replacing the page islands after setup cannot affect a later call: the
    // adapter queries the captured snapshot, it never re-reads or reseals.
    islands["catalog-data"] = "\"not a snapshot\"";
    islands["tool-contract"] = "[]";
    const second = nativePayload(search, { limit: 1 });
    assert.deepEqual(first, second);
    assert.equal(counts.loadSnapshot, 1, "no reseal per call");
    assert.equal(counts.loadJson, 1, "no island re-decode per call");
    assert.equal(counts.queries, queriesAfterSetup + 2,
                 "each call is exactly one shared query");
    assert.equal(counts.encoded, 2, "each call frames exactly one text result");
    assert.equal(second.snapshot_id, SNAPSHOT.snapshot_id);
  });

  it("never mutates the argument object and returns isolated results", async () => {
    const { modelContext } = await registerWith();
    const args = { query: "synthetic", limit: 3 };
    const snapshotBefore = JSON.stringify(SNAPSHOT);
    nativePayload(descriptorFor(modelContext, "search_models"), args);
    assert.deepEqual(args, { query: "synthetic", limit: 3 });
    assert.equal(JSON.stringify(SNAPSHOT), snapshotBefore,
                 "querying never rewrites the captured snapshot");
    const first = nativePayload(descriptorFor(modelContext, "search_models"), args);
    const second = nativePayload(descriptorFor(modelContext, "search_models"), args);
    assert.deepEqual(first, second, "repeat calls are identical");
    assert.notEqual(first, second, "repeat calls are fresh objects");
    const returnedRows = second.endpoints.length;
    assert.equal(returnedRows, 2, "the fixture has two text matches");
    first.endpoints.push({ injected: true });
    assert.equal(second.endpoints.length, returnedRows,
                 "mutating one decoded result cannot leak");
  });

  it("keeps hostile strings intact through the text frame", async () => {
    // Annotations are advisory metadata: they neither rewrite upstream text nor
    // authorize rendering it. The detail description travels as inert JSON.
    const hostile = hostileSnapshot();
    const hostileContext = await registerWith(hostile.text, CONTRACT_TEXT);
    const hostileDecoded = decodeNative(
      descriptorFor(hostileContext.modelContext, "get_model")
        .execute({ provider: "synthetic-hostile", model_id: HOSTILE_ID }));
    assertExactSource(hostileDecoded.source);
    const endpoint = hostileDecoded.payload.endpoint;
    assert.equal(Object.getPrototypeOf(endpoint), Object.prototype,
                 "decoding does not adopt a hostile prototype");
    assert.equal(endpoint.model_id, HOSTILE_ID,
                 "the prototype-looking model id is an ordinary value");
    assert.equal(endpoint.description, HOSTILE_DESCRIPTION,
                 "the hostile description is preserved exactly");
    assert.equal(endpoint.name, HOSTILE_NAME);
    // The text is ONE line of pure ASCII: quotes, line separators, astral
    // characters and control characters are escaped, so upstream text can
    // never break out of the JSON string context.
    assert.equal(hostileDecoded.text.includes("\n"), false,
                 "no raw newline escapes the JSON string");
    assert.equal(hostileDecoded.text.includes("\u2028"), false,
                 "no raw line separator survives");
    assert.ok(hostileDecoded.text.includes("\\u2028"), "it is escaped instead");
    assert.ok(hostileDecoded.text.includes("\\\"quoted\\\""),
              "embedded quotes are escaped, so the string cannot be closed");
    assert.ok(/^[\x20-\x7e]*$/.test(hostileDecoded.text),
              "the canonical text is pure printable ASCII");
  });
});

describe("unit 8b — annotations on every descriptor", () => {
  it("annotates all three descriptors with the three exact hints", async () => {
    const { modelContext } = await registerWith();
    assert.equal(modelContext.calls.length, 3);
    for (const { descriptor } of modelContext.calls) {
      assertAnnotationTriplet(descriptor.annotations);
    }
  });

  it("gives every descriptor and every setup its own fresh annotations", async () => {
    const first = await registerWith();
    const descriptors = first.modelContext.calls.map((entry) => entry.descriptor);
    assert.equal(new Set(descriptors.map((d) => d.annotations)).size, 3,
                 "three distinct annotation objects in one registration");
    // A native layer that mutates what it was handed cannot corrupt the rest.
    descriptors[0].annotations.readOnlyHint = false;
    delete descriptors[1].annotations.consequentialHint;
    assertAnnotationTriplet(descriptors[2].annotations);
    const second = await registerWith();
    for (const { descriptor } of second.modelContext.calls) {
      assertAnnotationTriplet(descriptor.annotations);
    }
  });

  it("does not treat annotations as sanitization or as an HTML sink", async () => {
    // Annotations are advisory metadata: they neither rewrite upstream text nor
    // authorize rendering it. The adapter has no DOM/HTML write path at all.
    // These needles are ABSENCE checks over the adapter body — the strings are
    // never executed here; finding one would mean the adapter contains a sink.
    for (const needle of ["innerHTML", "outerHTML", "insertAdjacentHTML",
                          "document.write", "eval(", "new Function"]) {
      assert.equal(ADAPTER_BODY.includes(needle), false,
                   `the adapter must not reference ${needle}`);
    }
    const hostile = hostileSnapshot();
    const hostileContext = await registerWith(hostile.text, CONTRACT_TEXT);
    const text = descriptorFor(hostileContext.modelContext, "get_model")
      .execute({ provider: "synthetic-hostile", model_id: HOSTILE_ID });
    const decoded = decodeNative(text);
    assert.equal(decoded.payload.endpoint.description,
                 HOSTILE_DESCRIPTION,
                 "the untrusted description is returned as DATA, unrepaired");
    assert.equal(decoded.text.includes("<img src=x onerror=alert(1)>"), true,
                 "the payload text is inert JSON data, not markup");
  });
});

describe("unit 8b — graceful island and registration faults", () => {
  it("fails before registration when an island is missing", async () => {
    const cases = [
      [{ "tool-contract": CONTRACT_TEXT }, "catalog_island_missing"],
      [{ "catalog-data": SNAPSHOT_TEXT }, "contract_island_missing"]
    ];
    for (const [islands, reason] of cases) {
      const document = fakeDocument(islands);
      const modelContext = fakeRegistrar("ok", 0);
      document.modelContext = modelContext;
      const result = await adapter.setup({ document, catalog });
      assert.equal(result.ok, false, reason);
      assert.equal(result.status, "invalid");
      assert.equal(result.reason, reason);
      assert.deepEqual(modelContext.calls, [], "nothing was registered");
    }
    // A document that throws on lookup is also a graceful failure.
    const hostile = {
      modelContext: fakeRegistrar("ok", 0),
      getElementById() { throw new Error("lookup exploded"); }
    };
    const result = await adapter.setup({ document: hostile, catalog });
    assert.equal(result.reason, "catalog_island_missing");
    assert.deepEqual(hostile.modelContext.calls, []);
  });

  it("reads a throwing or non-string textContent as a missing island", async () => {
    const nodes = [
      { get textContent() { throw new Error("text exploded"); } },
      { textContent: 42 },
      { get textContent() { return null; } }
    ];
    for (const node of nodes) {
      const document = {
        modelContext: fakeRegistrar("ok", 0),
        getElementById() { return node; }
      };
      const result = await adapter.setup({ document, catalog });
      assert.equal(result.status, "invalid");
      assert.equal(result.reason, "catalog_island_missing");
      assert.deepEqual(result.registered, []);
      assert.deepEqual(document.modelContext.calls, [],
                       "an unusable island text registers nothing");
    }
  });

  it("fails before registration on a malformed or unsealed catalogue", async () => {
    const malformed = [
      [SNAPSHOT_TEXT.replace(/\}\s*$/, ",}"), "invalid_json"],
      [SNAPSHOT_TEXT.replace(/(\"cadence_s\"\s*:\s*3600)/,
                            "$1, \"cadence_s\": 3600"), "invalid_json"],
      [SNAPSHOT_TEXT.replace(/(\"snapshot_id\"\s*:\s*\")/,
                            (match) => match + "0"), "invalid_snapshot"],
      ["\"just a string\"", "invalid_snapshot"]
    ];
    for (const [text, code] of malformed) {
      assert.notEqual(text, SNAPSHOT_TEXT, `${code}: variant really changed`);
      const document = fakeDocument(islandsFor(CONTRACT_TEXT, text));
      const modelContext = fakeRegistrar("ok", 0);
      document.modelContext = modelContext;
      const result = await adapter.setup({ document, catalog });
      assert.equal(result.status, "invalid");
      assert.equal(result.reason, "catalog_island_invalid");
      assert.equal(result.code, code, `for snapshot variant ${code}`);
      assert.deepEqual(modelContext.calls, []);
    }
  });

  it("rejects missing, duplicate, unknown and malformed tool entries", async () => {
    const base = JSON.parse(CONTRACT_TEXT);
    const variants = [
      [JSON.stringify({ tools: base }), "contract_not_array"],
      [JSON.stringify(base.slice(0, 2)), "contract_tool_count"],
      [JSON.stringify(base.concat([base[0]])), "contract_tool_count"],
      [JSON.stringify(base.map((entry) => entry.name === "get_model"
        ? Object.assign({}, entry, { name: "get_model_extra" }) : entry)),
       "contract_unknown_tool"],
      [JSON.stringify(base.map((entry, index) => index === 1
        ? Object.assign({}, entry, { name: "get_model" }) : entry)),
       "contract_duplicate_tool"],
      [JSON.stringify(base.map((entry) => entry.name === "search_models"
        ? Object.assign({}, entry, { description: "" }) : entry)),
       "contract_invalid_description"],
      [JSON.stringify(base.map((entry) => entry.name === "watchdog_status"
        ? Object.assign({}, entry, { inputSchema: { type: "array" } }) : entry)),
       "contract_invalid_schema"],
      [JSON.stringify(base.map((entry) => entry.name === "get_model"
        ? Object.assign({}, entry, {
          inputSchema: Object.assign({}, entry.inputSchema,
                                     { additionalProperties: "no" })
        }) : entry)), "contract_invalid_schema"],
      [JSON.stringify(base.map((entry) => entry.name === "get_model"
        ? Object.assign({}, entry, {
          inputSchema: Object.assign({}, entry.inputSchema,
                                     { required: ["not_a_property"] })
        }) : entry)), "contract_invalid_schema"],
      [JSON.stringify(base.map((entry) => entry.name === "search_models"
        ? { name: entry.name, description: entry.description,
          inputSchema: entry.inputSchema, extra: true } : entry)),
       "contract_invalid_entry"]
    ];
    for (const [text, reason] of variants) {
      const document = fakeDocument(islandsFor(text));
      const modelContext = fakeRegistrar("ok", 0);
      document.modelContext = modelContext;
      const result = await adapter.setup({ document, catalog });
      assert.equal(result.ok, false, reason);
      assert.equal(result.status, "invalid");
      assert.equal(result.reason, reason);
      assert.deepEqual(result.registered, []);
      assert.deepEqual(modelContext.calls, [],
                       `${reason}: no partial registration`);
      assert.deepEqual(document.reads.sort(),
                       ["catalog-data", "tool-contract"]);
    }
  });

  it("rejects a malformed or duplicate-member contract island", async () => {
    const duplicateMember = CONTRACT_TEXT.replace(
      /("name"\s*:\s*"get_model")/, '$1, "name": "get_model_extra"');
    assert.notEqual(duplicateMember, CONTRACT_TEXT,
                    "the duplicate-member variant really applied");
    const cases = [
      // Raw JSON that never reaches the structural validator at all.
      ['{"tools": [', "invalid_json"],
      ["", "invalid_json"],
      // A duplicate member is rejected by the SHARED strict loader, not by a
      // permissive JSON.parse that would silently keep the last value.
      [duplicateMember, "invalid_json"]
    ];
    // The permissive parser would have ACCEPTED the duplicate-member text,
    // which is exactly why the shared loader is the acceptance path here.
    assert.equal(JSON.parse(duplicateMember)[0].name, "get_model_extra");
    assert.throws(() => catalog.loadJson(duplicateMember), /invalid|JSON/i);
    for (const [text, code] of cases) {
      const document = fakeDocument(islandsFor(text));
      const modelContext = fakeRegistrar("ok", 0);
      document.modelContext = modelContext;
      const result = await adapter.setup({ document, catalog });
      assert.equal(result.ok, false);
      assert.equal(result.status, "invalid");
      assert.equal(result.reason, "contract_island_invalid");
      assert.equal(result.code, code);
      assert.deepEqual(result.registered, []);
      assert.deepEqual(modelContext.calls, [],
                       "a malformed contract registers nothing");
    }
  });

  it("reports a rejected registration without claiming all three", async () => {
    const document = fakeDocument(islandsFor());
    const modelContext = fakeRegistrar("rejectAt", 3);
    document.modelContext = modelContext;
    const result = await adapter.setup({
      document, catalog, clock: () => FIXTURE_TICK_EPOCH
    });
    assert.deepEqual(result, {
      ok: false,
      status: "registration_failed",
      reason: "native_registration_rejected",
      registered: ["get_model", "watchdog_status"],
      failed: "search_models",
      code: null
    });
    assert.equal(modelContext.calls.length, 3, "later tools were not attempted");
    assert.deepEqual(Object.keys(document).sort(),
                     ["getElementById", "modelContext", "reads"]);
  });

  it("reports a synchronous registration throw the same honest way", async () => {
    const document = fakeDocument(islandsFor());
    const modelContext = fakeRegistrar("throwAt", 1);
    document.modelContext = modelContext;
    const result = await adapter.setup({ document, catalog });
    assert.equal(result.ok, false);
    assert.equal(result.status, "registration_failed");
    assert.equal(result.reason, "native_registration_threw");
    assert.deepEqual(result.registered, []);
    assert.equal(result.failed, "get_model");
    assert.equal(modelContext.calls.length, 1);
  });

  it("reports every failing position and style with its exact prefix", async () => {
    // Position 1, the middle tool and the LAST tool, each failed by a
    // synchronous throw, a rejected Promise and a rejected thenable.
    const styles = [
      ["throwAt", "native_registration_threw"],
      ["rejectAt", "native_registration_rejected"],
      ["thenableRejectAt", "native_registration_rejected"]
    ];
    const rows = [];
    for (const position of [1, 2, 3]) {
      for (const [mode, reason] of styles) {
        const label = `${mode}@${position}`;
        const document = fakeDocument(islandsFor());
        const modelContext = fakeRegistrar(mode, position);
        document.modelContext = modelContext;
        const first = await adapter.setup({
          document, catalog, clock: () => FIXTURE_TICK_EPOCH
        });
        rows.push({ position, mode, reason, outcome: plain(first) });
        assert.equal(first.ok, false, label);
        assert.equal(first.status, "registration_failed", label);
        assert.equal(first.reason, reason, label);
        assert.equal(first.code, null, label);
        assert.deepEqual(first.registered, TOOL_NAMES.slice(0, position - 1),
                         `${label}: exactly the confirmed prefix`);
        assert.equal(first.failed, TOOL_NAMES[position - 1],
                     `${label}: the actual failed tool name`);
        assert.deepEqual(modelContext.calls.map((entry) => entry.descriptor.name),
                         TOOL_NAMES.slice(0, position),
                         `${label}: attempted order stops at the failure`);
        // A repeat AFTER the failure is answered from the cached outcome: no
        // retry, no duplicate native call, no upgraded success.
        const repeat = await adapter.setup({ document, catalog });
        assert.deepEqual(repeat, first, `${label}: the repeat is the same outcome`);
        assert.notEqual(repeat.registered, first.registered,
                        `${label}: the repeat gets its own result container`);
        assert.equal(modelContext.calls.length, position,
                     `${label}: the failure was not retried`);
        // Already-confirmed descriptors keep working, with native text.
        if (position > 1) {
          const confirmed = decodeNative(
            descriptorFor(modelContext, "get_model")
              .execute({ provider: "synthetic-a", model_id: "vendor/shared" }))
            .payload;
          assert.equal(confirmed.ok, true,
                       `${label}: the confirmed prefix answers successfully`);
          assertPayloadParity(
            confirmed,
            catalog.getModel(SNAPSHOT,
                             { provider: "synthetic-a",
                               model_id: "vendor/shared" }),
            `${label}: the confirmed prefix stays usable`);
        }
      }
    }
    assert.equal(rows.length, 9, "three positions times three failure styles");
    process.stdout.write("# unit8c failure matrix " + JSON.stringify(rows) + "\n");
  });

  it("answers a duplicate honestly while the failing attempt is pending", async () => {
    const seen = [];
    const onUnhandled = (reason) => { seen.push(reason); };
    process.on("unhandledRejection", onUnhandled);
    try {
      const document = fakeDocument(islandsFor());
      const modelContext = gatedRegistrar(3);
      document.modelContext = modelContext;
      const first = adapter.setup({
        document, catalog, clock: () => FIXTURE_TICK_EPOCH
      });
      await settleMicrotasks();
      assert.equal(modelContext.calls.length, 1,
                     "the attempt starts at the first tool and waits");
      // The duplicate arrives while tool 1 is still unresolved.
      const duplicate = adapter.setup({
        document, catalog, clock: () => FIXTURE_TICK_EPOCH + 30
      });
      await settleMicrotasks();
      assert.equal(modelContext.calls.length, 1,
                     "the duplicate started no second attempt");
      modelContext.release();
      const [owned, repeated] = await Promise.all([first, duplicate]);
      assert.equal(owned.status, "registration_failed");
      assert.equal(owned.reason, "native_registration_rejected");
      assert.deepEqual(owned.registered, ["get_model", "watchdog_status"]);
      assert.equal(owned.failed, "search_models");
      assert.deepEqual(repeated, owned,
                       "the pending duplicate reports the same honest outcome");
      assert.notEqual(repeated.registered, owned.registered,
                      "the pending duplicate gets its own result container");
      assert.equal(modelContext.calls.length, 3,
                     "the sequence stopped at the failing tool");
      const after = await adapter.setup({ document, catalog });
      assert.deepEqual(after, owned, "no retry after a failed attempt");
      assert.equal(modelContext.calls.length, 3);
      await settleMicrotasks();
      assert.deepEqual(seen, [], "no unhandled rejection was left behind");
    } finally {
      process.removeListener("unhandledRejection", onUnhandled);
    }
  });

  it("treats a synchronous or assimilated-thenable success as registered", async () => {
    for (const mode of ["syncUndefined", "thenableOk"]) {
      const document = fakeDocument(islandsFor());
      const modelContext = fakeRegistrar(mode, 0);
      document.modelContext = modelContext;
      const result = await adapter.setup({
        document, catalog, clock: () => FIXTURE_TICK_EPOCH
      });
      assert.equal(result.ok, true, mode);
      assert.equal(result.status, "registered", mode);
      assert.equal(result.reason, null, mode);
      assert.deepEqual(result.registered, TOOL_NAMES, mode);
      assert.equal(result.failed, null, mode);
      assert.equal(modelContext.calls.length, 3, mode);
    }
  });

  it("never leaves an unhandled rejection behind", async () => {
    const seen = [];
    const onUnhandled = (reason) => { seen.push(reason); };
    process.on("unhandledRejection", onUnhandled);
    try {
      const document = fakeDocument(islandsFor());
      document.modelContext = fakeRegistrar("rejectAt", 2);
      const result = await adapter.setup({ document, catalog });
      assert.equal(result.reason, "native_registration_rejected");
      await new Promise((resolve) => setTimeout(resolve, 20));
      assert.deepEqual(seen, [], "setup consumed the native rejection");
    } finally {
      process.removeListener("unhandledRejection", onUnhandled);
    }
  });
});

describe("unit 8c — document-lifetime ownership", () => {
  it("registers once per document and replays the outcome to a duplicate", async () => {
    const counted = countedCatalog();
    const islands = islandsFor();
    const document = fakeDocument(islands);
    const modelContext = fakeRegistrar("ok", 0);
    document.modelContext = modelContext;
    const clockCalls = [];
    const clock = () => { clockCalls.push(1); return FIXTURE_TICK_EPOCH; };

    const first = await adapter.setup({
      document, catalog: counted.catalog, clock
    });
    assert.deepEqual(first, {
      ok: true, status: "registered", reason: null, registered: TOOL_NAMES,
      failed: null, code: null
    });
    assert.equal(modelContext.calls.length, 3);
    assert.deepEqual(document.reads, ["catalog-data", "tool-contract"]);
    assert.equal(counted.counts.loadSnapshot, 1);
    assert.equal(counted.counts.loadJson, 1);
    assert.equal(clockCalls.length, 0, "setup never reads the clock");

    const capture = descriptorFor(modelContext, "search_models").execute({ limit: 2 });
    const before = catalog.searchModels(SNAPSHOT, { limit: 2 });

    // A duplicate on the SAME document: changed island, changed clock and an
    // engine with no seams at all. None of it may be consulted.
    islands["catalog-data"] = syntheticSnapshot(40, 40).text;
    const duplicate = await adapter.setup({
      document, catalog: {}, clock: () => FIXTURE_TICK_EPOCH + 3600
    });
    assert.deepEqual(duplicate, first, "the same honest outcome values");
    assert.notEqual(duplicate, first, "a fresh result container");
    assert.notEqual(duplicate.registered, first.registered,
                    "a fresh registered array");
    assert.equal(modelContext.calls.length, 3, "no second native registration");
    assert.equal(document.reads.length, 2, "the duplicate re-parsed no island");
    assert.equal(counted.counts.loadSnapshot, 1, "one snapshot load in total");
    assert.equal(counted.counts.loadJson, 1, "one contract parse in total");
    assert.equal(clockCalls.length, 0, "no clock read on a duplicate either");

    // The FIRST snapshot, core binding and callbacks are still the live ones.
    assertPayloadParity(decodeNative(capture).payload, before,
                        "post-duplicate callback");
    const postDuplicate = decodeNative(
      descriptorFor(modelContext, "get_model")
        .execute({ provider: "synthetic-a", model_id: "vendor/shared" }))
      .payload;
    assert.equal(postDuplicate.ok, true,
                 "post-duplicate callback answers successfully");
    assertPayloadParity(
      postDuplicate,
      catalog.getModel(SNAPSHOT,
                       { provider: "synthetic-a", model_id: "vendor/shared" }),
      "post-duplicate callback");
    assert.equal(descriptorFor(modelContext, "search_models").execute({ limit: 2 }),
                 capture, "the original callback frames the same text");
    // `watchdog_status` keeps the FIRST captured clock seam and reads it at
    // INVOCATION: the duplicate's own clock is never consulted.
    const statusPayload = decodeNative(
      descriptorFor(modelContext, "watchdog_status").execute({})).payload;
    assert.equal(clockCalls.length, 1, "the first clock seam is the one read");
    assert.equal(statusPayload.last_tick_age_s, 0,
                 "the invocation clock is the first setup's reading");

    // Result containers stay isolated per caller.
    duplicate.registered.push("poison_tool");
    duplicate.registered.length = 1;
    const third = await adapter.setup({ document, catalog });
    assert.deepEqual(third.registered, TOOL_NAMES,
                     "caller mutation cannot poison a later result");
    assert.equal(modelContext.calls.length, 3);
    assert.deepEqual(Object.keys(document).sort(),
                     ["getElementById", "modelContext", "reads"],
                     "the document gained no adapter state");
  });

  it("holds ONE in-flight attempt for concurrent duplicate setups", async () => {
    const document = fakeDocument(islandsFor());
    const modelContext = gatedRegistrar(0);
    document.modelContext = modelContext;
    const first = adapter.setup({
      document, catalog, clock: () => FIXTURE_TICK_EPOCH
    });
    const duplicate = adapter.setup({
      document, catalog, clock: () => FIXTURE_TICK_EPOCH + 30
    });
    let settled = 0;
    first.then(() => { settled += 1; });
    duplicate.then(() => { settled += 1; });
    await settleMicrotasks();
    assert.equal(modelContext.calls.length, 1,
                   "only the first call reached the native registrar");
    assert.equal(settled, 0, "the single attempt is genuinely pending");
    modelContext.release();
    const [owned, repeated] = await Promise.all([first, duplicate]);
    assert.equal(owned.status, "registered");
    assert.deepEqual(owned.registered, TOOL_NAMES);
    assert.deepEqual(repeated, owned, "the concurrent duplicate sees the outcome");
    assert.notEqual(repeated, owned, "fresh result containers");
    assert.notEqual(repeated.registered, owned.registered,
                    "fresh registered arrays");
    assert.equal(settled, 2, "both callers resolved");
    assert.equal(modelContext.calls.length, 3, "one native call per tool");
    assert.deepEqual(modelContext.calls.map((entry) => entry.descriptor.name),
                     TOOL_NAMES, "shared-contract order, no repeats");
    assert.equal(document.reads.length, 2, "one snapshot load and contract parse");
  });

  it("absorbs a synchronous re-entrant setup from inside registerTool", async () => {
    const document = fakeDocument(islandsFor());
    const calls = [];
    let nested = null;
    const modelContext = {
      calls,
      registerTool(descriptor) {
        if (this !== modelContext) {
          throw new Error("registerTool called with the wrong receiver");
        }
        calls.push({ index: calls.length + 1, descriptor });
        if (calls.length === 1) {
          // Captured, never awaited here: awaiting would deadlock the outer
          // sequence this very callback belongs to.
          nested = adapter.setup({
            document, catalog, clock: () => FIXTURE_TICK_EPOCH + 7
          });
        }
        return Promise.resolve();
      }
    };
    document.modelContext = modelContext;
    const outer = await adapter.setup({
      document, catalog, clock: () => FIXTURE_TICK_EPOCH
    });
    assert.equal(outer.status, "registered");
    assert.ok(nested !== null, "the re-entrant call captured a promise");
    const inner = await nested;
    assert.deepEqual(inner, outer, "the re-entrant call reports the same outcome");
    assert.notEqual(inner.registered, outer.registered,
                    "fresh registered array for the re-entrant caller");
    assert.equal(calls.length, 3, "the re-entrant call registered nothing");
    assert.deepEqual(calls.map((entry) => entry.descriptor.name), TOOL_NAMES);
    assert.equal(document.reads.length, 2);
  });

  it("keeps different documents independent", async () => {
    const firstDocument = fakeDocument(islandsFor());
    const firstRegistrar = fakeRegistrar("ok", 0);
    firstDocument.modelContext = firstRegistrar;
    const secondDocument = fakeDocument(islandsFor());
    const secondRegistrar = fakeRegistrar("ok", 0);
    secondDocument.modelContext = secondRegistrar;
    const [first, second] = await Promise.all([
      adapter.setup({ document: firstDocument, catalog,
                      clock: () => FIXTURE_TICK_EPOCH }),
      adapter.setup({ document: secondDocument, catalog,
                      clock: () => FIXTURE_TICK_EPOCH })
    ]);
    assert.equal(first.status, "registered");
    assert.equal(second.status, "registered");
    assert.equal(firstRegistrar.calls.length, 3);
    assert.equal(secondRegistrar.calls.length, 3);
    assert.equal(firstDocument.reads.length, 2);
    assert.equal(secondDocument.reads.length, 2);
    // ... and a third document is still a fresh registration.
    const third = await registerWith();
    assert.equal(third.result.status, "registered");
    assert.equal(third.modelContext.calls.length, 3);
  });

  it("does not claim ownership before a registration attempt starts", async () => {
    // (a) No native API: honest no-op, no islands read, no ownership.
    const islands = islandsFor();
    const document = fakeDocument(islands);
    const absent = await adapter.setup({ document, catalog });
    assert.equal(absent.status, "unsupported");
    assert.equal(absent.reason, "native_api_absent");
    assert.deepEqual(document.reads, [], "no island parsed without an API");
    // The API appearing later is a NEW opportunity, not a cached refusal.
    const modelContext = fakeRegistrar("ok", 0);
    document.modelContext = modelContext;
    const repaired = await adapter.setup({
      document, catalog, clock: () => FIXTURE_TICK_EPOCH
    });
    assert.equal(repaired.status, "registered");
    assert.equal(modelContext.calls.length, 3);

    // (b) A malformed island fault, then a correction on the same document.
    const brokenIslands = { "catalog-data": "{ not json", "tool-contract": CONTRACT_TEXT };
    const brokenDocument = fakeDocument(brokenIslands);
    const brokenRegistrar = fakeRegistrar("ok", 0);
    brokenDocument.modelContext = brokenRegistrar;
    const invalid = await adapter.setup({ document: brokenDocument, catalog });
    assert.equal(invalid.status, "invalid");
    assert.equal(invalid.reason, "catalog_island_invalid");
    assert.deepEqual(brokenRegistrar.calls, []);
    brokenIslands["catalog-data"] = SNAPSHOT_TEXT;
    const fixed = await adapter.setup({
      document: brokenDocument, catalog, clock: () => FIXTURE_TICK_EPOCH
    });
    assert.equal(fixed.status, "registered");
    assert.equal(brokenRegistrar.calls.length, 3);

    // (c) A missing shared engine, then a working one, on the same document.
    const coreIslands = islandsFor();
    const coreDocument = fakeDocument(coreIslands);
    const coreRegistrar = fakeRegistrar("ok", 0);
    coreDocument.modelContext = coreRegistrar;
    const noCore = await adapter.setup({ document: coreDocument, catalog: {} });
    assert.equal(noCore.status, "invalid");
    assert.equal(noCore.reason, "core_unavailable");
    assert.deepEqual(coreDocument.reads, [], "no island parsed without a core");
    assert.deepEqual(coreRegistrar.calls, []);
    const withCore = await adapter.setup({
      document: coreDocument, catalog, clock: () => FIXTURE_TICK_EPOCH
    });
    assert.equal(withCore.status, "registered");
    assert.equal(coreRegistrar.calls.length, 3);
  });
});

describe("unit 8b — applied wrong-rule controls", () => {
  /** Register a MUTATED adapter copy in its own page realm. */
  async function registerMutant(source) {
    assert.notEqual(source, ADAPTER_SOURCE, "the mutation really applied");
    const sandbox = loadPageRealm(source);
    const { result, modelContext } = await registerPageRealm(sandbox);
    assert.equal(result.status, "registered",
                 "the mutant still SETS UP: the failure is the assertion below");
    return modelContext;
  }

  it("rejects an altered hint, an altered source and a lossy frame", async () => {
    // Control: the unchanged adapter passes every assertion below.
    const control = await registerWith();
    assertAnnotationTriplet(
      descriptorFor(control.modelContext, "search_models").annotations);
    assertExactSource(decodeNative(
      descriptorFor(control.modelContext, "search_models").execute({ limit: 1 }))
      .source);

    // Mutant A — one safety hint weakened (code anchor, not the doc header).
    const hintMutant = ADAPTER_SOURCE.replace(
      "      readOnlyHint: true,\n      untrustedContentHint: true,",
      "      readOnlyHint: true,\n      untrustedContentHint: false,");
    const hintContext = await registerMutant(hintMutant);
    assert.throws(
      () => assertAnnotationTriplet(
        descriptorFor(hintContext, "search_models").annotations),
      /untrustedContentHint/, "the annotation assertion rejects the wrong hint");

    // Mutant B — an invented provenance field.
    const sourceMutant = ADAPTER_SOURCE.replace(
      "return { mode: \"browser\" };",
      "return { mode: \"browser\", url: \"https://models.dthen.xyz/\" };");
    const sourceContext = await registerMutant(sourceMutant);
    assert.throws(
      () => assertExactSource(decodeNative(
        descriptorFor(sourceContext, "search_models").execute({ limit: 1 }))
        .source),
      /embedded source is exactly/,
      "the source assertion rejects the invented field");

    // Mutant C — the frame truncated to the ~1.5K recommendation.
    const truncatingMutant = ADAPTER_SOURCE.replace(
      "return catalog.canonicalText(attachSource(payload));",
      "return catalog.canonicalText(attachSource(payload)).slice(0, 100);");
    const truncatingContext = await registerMutant(truncatingMutant);
    assert.throws(
      () => decodeNative(
        descriptorFor(truncatingContext, "search_models").execute({ limit: 2 })),
      /the text decodes to one JSON object|Unexpected|JSON/,
      "the completeness assertion rejects the truncated text");

    // Mutant D — one semantic field dropped while framing.
    const droppingMutant = ADAPTER_SOURCE.replace(
      "attached[keys[index]] = payload[keys[index]];",
      "if (keys[index] !== \"endpoints\") { attached[keys[index]] = payload[keys[index]]; }");
    const droppingContext = await registerMutant(droppingMutant);
    assert.throws(
      () => assertPayloadParity(
        nativePayload(descriptorFor(droppingContext, "search_models"), {}),
        catalog.searchModels(SNAPSHOT, {}), "endpoints dropped"),
      /endpoints dropped/, "the parity assertion rejects dropped metadata");

    // Restored control: the unchanged adapter is still green.
    const restored = await registerWith();
    for (const { descriptor } of restored.modelContext.calls) {
      assertAnnotationTriplet(descriptor.annotations);
    }
  });
});

describe("unit 8c — applied lifecycle wrong-rule controls", () => {
  it("rejects a removed ownership guard and a failure-as-success rule", async () => {
    // Control: the unchanged adapter satisfies both lifecycle assertions.
    assertOwnedOnce(await duplicateObservation(adapter, catalog));
    assertHonestFailure(await failureObservation(adapter, catalog));

    // Mutant G — the document-lifetime guard removed: the record is still
    // written, but every duplicate lookup misses and registers again.
    const guardMutant = ADAPTER_SOURCE.replace(
      "    var owned = isDocumentKey(document) ? OWNERSHIP.get(document) : undefined;\n" +
      "    if (owned !== undefined) {\n" +
      "      return copyResult(await owned);\n" +
      "    }",
      "    var owned = undefined;");
    assert.notEqual(guardMutant, ADAPTER_SOURCE,
                    "the ownership-guard mutation applied");
    const guardSandbox = loadPageRealm(guardMutant);
    const guardObservation = await duplicateObservation(
      guardSandbox.WatchdogWebMCP, guardSandbox.WatchdogCatalog,
      guardSandbox.WatchdogCatalog);
    // The mutant really produced a second, duplicate native registration.
    assert.equal(guardObservation.calls, 6,
                 "the guard-less copy registered every tool twice");
    assert.throws(() => assertOwnedOnce(guardObservation),
                  /exactly one native call per tool/,
                  "the ownership assertion rejects the removed guard");

    // Mutant H — after a synchronous throw the sequence CONTINUES and the
    // outcome claims all three tools were registered.
    const continueMutant = ADAPTER_SOURCE.replace(
      "      } catch (error) {\n" +
      "        return result(\"registration_failed\", \"native_registration_threw\",\n" +
      "                      registered, descriptor.name);\n" +
      "      }",
      "      } catch (error) {\n" +
      "        registered.push(descriptor.name);\n" +
      "        continue;\n" +
      "      }");
    assert.notEqual(continueMutant, ADAPTER_SOURCE,
                    "the failure-continuation mutation applied");
    const continueSandbox = loadPageRealm(continueMutant);
    const continueObservation = await failureObservation(
      continueSandbox.WatchdogWebMCP, continueSandbox.WatchdogCatalog);
    assert.equal(continueObservation.outcome.status, "registered",
                 "the mutant really claimed success after the throw");
    assert.deepEqual(continueObservation.attempted, TOOL_NAMES,
                     "the mutant really attempted every tool");
    assert.throws(() => assertHonestFailure(continueObservation),
                  /registration_failed/,
                  "the honest-failure assertion rejects the continuation");

    // Restored control: the unchanged adapter passes the same assertions.
    assertOwnedOnce(await duplicateObservation(adapter, catalog));
    assertHonestFailure(await failureObservation(adapter, catalog));
  });
});
