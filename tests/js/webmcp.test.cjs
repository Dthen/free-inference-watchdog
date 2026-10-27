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
 *   * the five descriptors are derived from the embedded shared
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

/** The five shared tool names, as an independent literal. */
const TOOL_NAMES = [
  "list_free_models", "get_model", "list_endpoints", "watchdog_status",
  "search_models"
];

/** Independent literals: the reviewed budget policy of browser contract 4.6. */
const DESCRIPTION_BUDGET = 500;
const PARAM_DESCRIPTION_BUDGET = 150;
const NAME_BUDGET = 30;

/** Independent literals: the pinned identity of the shipped resource. */
const PINNED_DESCRIPTION_LENGTHS = {
  list_free_models: 482,
  get_model: 388,
  list_endpoints: 478,
  watchdog_status: 451,
  search_models: 497
};
const PINNED_MAX_PARAM_DESCRIPTION = 147;

/** Independent literals: the reviewed lookup fixture's timing facts. */
const FIXTURE_TICK_EPOCH = 1759200000;
const FIXTURE_CADENCE_S = 3600;

/** Independent literals: the three documented hints, exactly. */
const EXPECTED_ANNOTATION_KEYS =
  ["consequentialHint", "readOnlyHint", "untrustedContentHint"];
const EXPECTED_SOURCE = { mode: "browser" };

/** Independent literal: the ~1.5K per-output RECOMMENDATION (never a cap). */
const RECOMMENDED_CHARACTER_BUDGET = 1500;

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
 *   "ok"        — resolves, records the descriptor;
 *   "rejectAt"  — rejects the Nth call (1-based) AFTER recording it;
 *   "throwAt"   — throws synchronously on the Nth call;
 *   "slow"      — resolves on a later macrotask.
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
      if (mode === "slow") {
        return new Promise((resolve) => setTimeout(resolve, 5));
      }
      return Promise.resolve();
    }
  };
  return modelContext;
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
               `${label}: identical typed canonical text (float tokens intact)`);
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
      pricing: { completion: "0", prompt: "0" },
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
    schema_version: 1,
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
    pricing: { completion: "0", prompt: "0" },
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
    schema_version: 1,
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

  it("exports the five shared tool names as an immutable published set", () => {
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
    // 8c owns unregistration/lifecycle and cross-origin exposure, 8d owns
    // inlining/auto-setup: 8b must not pre-empt them.
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
  it("registers exactly the five shared descriptors in contract order", async () => {
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
    assert.equal(search.properties.limit.default, 50);
    assert.equal(search.properties.offset.maximum, 9007199254740991);
    assert.deepEqual(search.properties.min_context_length.type,
                     ["integer", "null"]);
    assert.deepEqual(search.properties.input_modalities.items,
                     { type: "string", minLength: 1 });
    const model = descriptorFor(modelContext, "get_model").inputSchema;
    assert.deepEqual(model.required, ["model_id"]);
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
    mutated[1].inputSchema.properties.model_id.description =
      "y".repeat(PARAM_DESCRIPTION_BUDGET + 1);
    const findings = overBudget(mutated).map((row) => row.kind + ":" + row.tool);
    assert.deepEqual(findings, [
      "tool-description:list_free_models", "parameter-description:get_model"
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
    assert.ok(modelContext.calls.length < 5,
              "the registration sequence is not finished synchronously");
    const result = await pending;
    assert.equal(result.status, "registered");
    assert.equal(modelContext.calls.length, 5);
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
  it("returns one JSON text result for all five tools with exact parity", async () => {
    const now = FIXTURE_TICK_EPOCH + 90;
    const { modelContext } = await registerWith(undefined, undefined, () => now);
    const cases = [
      ["list_free_models", "listFreeModels", {}],
      ["get_model", "getModel", { model_id: "vendor/shared" }],
      ["list_endpoints", "listEndpoints", {}],
      ["search_models", "searchModels", { query: "synthetic", limit: 2 }]
    ];
    for (const [tool, coreName, args] of cases) {
      const descriptor = descriptorFor(modelContext, tool);
      const decoded = decodeNative(descriptor.execute(Object.assign({}, args)));
      assertExactSource(decoded.source);
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

    const endpoints = nativePayload(descriptorFor(modelContext, "list_endpoints"), {});
    assert.deepEqual(endpoints.counts, {
      gateways: 3, endpoints: 5, distinct_raw_ids: 4,
      marker_group_labels: 3, models: 3
    });
    assert.deepEqual(Object.keys(endpoints.gateways), SNAPSHOT.provider_order);
    assert.equal(endpoints.gateways["synthetic-a"].model_ids.length, 3);
  });

  it("keeps the frozen literal expectations for status, search and bulk", async () => {
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
    assert.equal(search.limit, 2);
    assert.equal(search.offset, 0);
    assert.equal(search.returned_count, 2);
    assert.equal(search.total_matches, 2);
    assert.equal(search.next_offset, null);
    assert.deepEqual(search.endpoints.map((row) => row.provider + "/" + row.model_id),
                     ["synthetic-a/vendor/shared", "synthetic-b/vendor/shared"]);
    assert.equal(search.counts.endpoints, 2);
    assert.deepEqual(search.filters,
                     { provider: null, min_context_length: null,
                       input_modalities: null, supported_parameters: null });

    const bulk = nativePayload(descriptorFor(modelContext, "list_free_models"), {});
    assert.equal(bulk.ok, true);
    assert.deepEqual(Object.keys(bulk.providers), SNAPSHOT.provider_order);
    assert.deepEqual(bulk.providers["synthetic-a"],
                     ["vendor/model-renamed", "vendor/model:free",
                      "vendor/shared"]);
    assert.deepEqual(bulk.providers["synthetic-c"], []);
    assert.equal(bulk.total_ids, 4, "distinct raw ids across gateways");
    assert.equal(bulk.n_gateways, 3);
    assert.deepEqual(bulk.counts,
                     { "synthetic-a": 3, "synthetic-b": 2, "synthetic-c": 0 });
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
    assert.equal(omitted.payload.limit, 50);
    assert.equal(omitted.payload.offset, 0);
    assert.equal(omitted.payload.ok, true);
    assertPayloadParity(omitted.payload, catalog.searchModels(SNAPSHOT, {}),
                        "omitted arguments");
    // Explicit null is forwarded unchanged and stays its own rejection.
    assert.equal(decodeNative(search.execute(null)).payload.ok, false);
    assert.equal(
      decodeNative(descriptorFor(modelContext, "list_free_models").execute(null))
        .payload.error.code, "invalid_arguments");
  });

  it("forwards arrays, unknown keys and capability lists unchanged", async () => {
    const { modelContext } = await registerWith();
    const search = descriptorFor(modelContext, "search_models");
    const bulk = descriptorFor(modelContext, "list_free_models");
    assertPayloadParity(decodeNative(search.execute({ limit: 1, unknown: 1 })).payload,
                        catalog.searchModels(SNAPSHOT, { limit: 1, unknown: 1 }),
                        "unknown own key");
    assertPayloadParity(decodeNative(search.execute([1, 2])).payload,
                        catalog.searchModels(SNAPSHOT, [1, 2]), "array argument");
    assertPayloadParity(decodeNative(bulk.execute({ provider: null })).payload,
                        catalog.listFreeModels(SNAPSHOT, { provider: null }),
                        "explicit null provider");
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
    // The other four callbacks never touch the clock.
    descriptorFor(modelContext, "list_free_models").execute({});
    descriptorFor(modelContext, "get_model").execute({ model_id: "x" });
    descriptorFor(modelContext, "list_endpoints").execute({});
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
    const QUERY_NAMES = ["listFreeModels", "getModel", "listEndpoints",
                         "watchdogStatus", "searchModels"];
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

  it("keeps float tokens and hostile strings intact through the text frame", async () => {
    // 1. A float TOKEN in an ordinary payload field: the shared engine copies
    //    the request's `min_context_length` into `filters`, so the typed value
    //    survives the frame. An ordinary serializer would spell `1.0` as `1`.
    const { modelContext } = await registerWith();
    const search = descriptorFor(modelContext, "search_models");
    const floatArgs = { min_context_length: new catalog.JsonFloat(1.0) };
    const floatText = search.execute(floatArgs).toString();
    assert.ok(floatText.includes("\"min_context_length\":1.0"),
              "the float token is spelled 1.0, not collapsed to 1");
    const floatDecoded = decodeNative(floatText);
    assertExactSource(floatDecoded.source);
    assert.equal(catalog.isFloatToken(floatDecoded.payload.filters.min_context_length),
                 true, "the decoded token is still a typed float");
    assertPayloadParity(floatDecoded.payload,
                        catalog.searchModels(SNAPSHOT, floatArgs),
                        "float-token payload");
    // The defect the encoder avoids, stated as a control.
    assert.equal(
      JSON.stringify(catalog.searchModels(SNAPSHOT, floatArgs).filters)
        .includes("\"min_context_length\":1.0"), false,
      "an ordinary serializer would lose the float token");

    // 2. Hostile-looking ordinary STRINGS stay data, byte for byte.
    const hostile = hostileSnapshot();
    const hostileContext = await registerWith(hostile.text, CONTRACT_TEXT);
    const hostileDecoded = decodeNative(
      descriptorFor(hostileContext.modelContext, "list_free_models").execute({}));
    assertExactSource(hostileDecoded.source);
    const models = hostileDecoded.payload.models["synthetic-hostile"];
    assert.equal(Object.getPrototypeOf(models), Object.prototype,
                 "decoding does not adopt a hostile prototype");
    assert.equal(Object.prototype.hasOwnProperty.call(models, HOSTILE_ID), true,
                 "the prototype-looking model id is an ordinary own key");
    assert.equal(models[HOSTILE_ID].description, HOSTILE_DESCRIPTION,
                 "the hostile description is preserved exactly");
    assert.equal(models[HOSTILE_ID].name, HOSTILE_NAME);
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
  it("annotates all five descriptors with the three exact hints", async () => {
    const { modelContext } = await registerWith();
    assert.equal(modelContext.calls.length, 5);
    for (const { descriptor } of modelContext.calls) {
      assertAnnotationTriplet(descriptor.annotations);
    }
  });

  it("gives every descriptor and every setup its own fresh annotations", async () => {
    const first = await registerWith();
    const descriptors = first.modelContext.calls.map((entry) => entry.descriptor);
    assert.equal(new Set(descriptors.map((d) => d.annotations)).size, 5,
                 "five distinct annotation objects in one registration");
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
      .execute({ model_id: HOSTILE_ID });
    const decoded = decodeNative(text);
    assert.equal(decoded.payload.endpoints[0].metadata.description,
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
      [JSON.stringify(base.slice(0, 4)), "contract_tool_count"],
      [JSON.stringify(base.concat([base[0]])), "contract_tool_count"],
      [JSON.stringify(base.map((entry) => entry.name === "get_model"
        ? Object.assign({}, entry, { name: "get_model_extra" }) : entry)),
       "contract_unknown_tool"],
      [JSON.stringify(base.map((entry, index) => index === 1
        ? Object.assign({}, entry, { name: "list_free_models" }) : entry)),
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
      [JSON.stringify(base.map((entry) => entry.name === "list_endpoints"
        ? Object.assign({}, entry, {
          inputSchema: Object.assign({}, entry.inputSchema,
                                     { required: ["not_a_property"] })
        }) : entry)), "contract_invalid_schema"],
      [JSON.stringify(base.map((entry) => entry.name === "list_endpoints"
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

  it("reports a rejected registration without claiming all five", async () => {
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
      registered: ["list_free_models", "get_model"],
      failed: "list_endpoints",
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
    assert.equal(result.failed, "list_free_models");
    assert.equal(modelContext.calls.length, 1);
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

describe("unit 8b — output-size evidence under the 4.6 policy", () => {
  it("preserves complete bulk payloads above the 1.5K recommendation", async () => {
    const small = syntheticSnapshot(2, 0);
    const large = syntheticSnapshot(120, 200);
    const measurements = {};

    for (const [label, synthetic] of [["small", small], ["large", large]]) {
      const { modelContext } = await registerWith(synthetic.text, CONTRACT_TEXT);
      // The same callbacks through the REAL classic-script export, in the page
      // realm. This is ONE V8 build (CommonJS vs vm), not two native runtimes.
      const sandbox = loadPageRealm();
      const classicContext = await registerPageRealm(sandbox, synthetic.text,
                                                     CONTRACT_TEXT);
      assert.equal(classicContext.result.status, "registered");

      for (const tool of ["list_free_models", "list_endpoints", "search_models"]) {
        const args = tool === "search_models" ? { limit: 5 } : {};
        const text = descriptorFor(modelContext, tool).execute(Object.assign({}, args));
        const decoded = decodeNative(text);
        assertExactSource(decoded.source);
        measurements[`${label}:${tool}`] = text.length;
        assert.ok(text.length > 0);
        assert.equal(decoded.payload.ok, true);

        // Completeness against the synthetic input, never a shortened array.
        const expectedModels =
          synthetic.snapshot.providers["synthetic-bulk"].model_ids.length;
        if (tool === "search_models") {
          assert.equal(decoded.payload.endpoints.length,
                       Math.min(5, expectedModels));
          assert.equal(decoded.payload.returned_count,
                       decoded.payload.endpoints.length);
          assert.equal(decoded.payload.total_matches, expectedModels);
          assert.equal(decoded.payload.next_offset,
                       expectedModels > 5 ? 5 : null,
                       "pagination is the engine's, not a truncation artifact");
        } else if (tool === "list_free_models") {
          const ids = decoded.payload.providers["synthetic-bulk"];
          assert.deepEqual(ids,
                           synthetic.snapshot.providers["synthetic-bulk"].model_ids,
                           `${tool}: every raw id survives in order`);
          assert.equal(ids.length, expectedModels);
          assert.equal(decoded.payload.total_ids, expectedModels);
        } else {
          const gateway = decoded.payload.gateways["synthetic-bulk"];
          assert.deepEqual(gateway.model_ids,
                           synthetic.snapshot.providers["synthetic-bulk"].model_ids,
                           "no endpoint was silently dropped");
          assert.equal(Object.keys(gateway.models).length, expectedModels);
          if (label === "large") {
            assert.equal(
              gateway.models["synthetic-model-000"].description.length, 200,
              "full model metadata is preserved, never shortened");
          }
        }
        // The classic copy frames byte-identical text for the same input.
        const classicText = descriptorFor(classicContext.modelContext, tool)
          .execute(Object.assign({}, args));
        assert.equal(classicText.length, text.length);
        assert.equal(classicText, text);
      }
    }

    const bulkLarge = measurements["large:list_endpoints"];
    const searchLarge = measurements["large:search_models"];
    const bulkSmall = measurements["small:list_free_models"];
    assert.ok(bulkLarge > RECOMMENDED_CHARACTER_BUDGET,
              "the bulk export exceeds the ~1.5K recommendation and stays whole");
    assert.ok(bulkSmall > 0 && bulkSmall < bulkLarge);
    assert.ok(bulkLarge > searchLarge * 10,
              "the bounded search page is far smaller than the bulk export");
    // Routine lookups are directed to search_models with a small limit.
    assert.deepEqual(nativePayload(
      descriptorFor((await registerWith(large.text, CONTRACT_TEXT)).modelContext,
                    "search_models"), { limit: 1 }).returned_count, 1);
    // One durable, human-readable evidence line per run. These are NODE
    // measurements of the wrapped text; real-browser numbers are unit 8e's.
    process.stdout.write("# unit8b size evidence " +
      JSON.stringify(Object.assign({ budget: RECOMMENDED_CHARACTER_BUDGET,
                                     runtime: "node-vm+commonjs" },
                                   measurements)) + "\n");
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
      "return catalog.canonicalText(attachSource(payload)).slice(0, 1500);");
    const truncatingContext = await registerMutant(truncatingMutant);
    assert.throws(
      () => decodeNative(
        descriptorFor(truncatingContext, "list_endpoints").execute({})),
      /the text decodes to one JSON object|Unexpected|JSON/,
      "the completeness assertion rejects the truncated text");

    // Mutant D — one semantic field dropped while framing.
    const droppingMutant = ADAPTER_SOURCE.replace(
      "attached[keys[index]] = payload[keys[index]];",
      "if (keys[index] !== \"counts\") { attached[keys[index]] = payload[keys[index]]; }");
    const droppingContext = await registerMutant(droppingMutant);
    assert.throws(
      () => assertPayloadParity(
        nativePayload(descriptorFor(droppingContext, "list_free_models"), {}),
        catalog.listFreeModels(SNAPSHOT, {}), "counts dropped"),
      /counts dropped/, "the parity assertion rejects dropped metadata");

    // Mutant E — an ordinary serializer instead of the typed encoder.
    const serializerMutant = ADAPTER_SOURCE.replace(
      "return catalog.canonicalText(attachSource(payload));",
      "return JSON.stringify(attachSource(payload));");
    const serializerContext = await registerMutant(serializerMutant);
    const collapsed = descriptorFor(serializerContext, "search_models")
      .execute({ min_context_length: new catalog.JsonFloat(1.0) });
    assert.equal(collapsed.includes("\"min_context_length\":1.0"), false,
                 "the float-token assertion rejects the lossy serializer");
    assert.throws(
      () => assertPayloadParity(
        decodeNative(collapsed).payload,
        catalog.searchModels(SNAPSHOT,
                             { min_context_length: new catalog.JsonFloat(1.0) }),
        "float token collapsed"),
      /float token collapsed/,
      "typed-canonical parity rejects the collapsed float token");

    // Restored control: the unchanged adapter is still green.
    const restored = await registerWith();
    for (const { descriptor } of restored.modelContext.calls) {
      assertAnnotationTriplet(descriptor.annotations);
    }
  });
});
