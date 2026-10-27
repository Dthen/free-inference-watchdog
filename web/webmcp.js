/*!
 * free-inference-watchdog — WebMCP progressive-enhancement adapter.
 *
 * ============================================================================
 * THIS FILE IS AN INERT CLASSIC SCRIPT. Evaluating or requiring it performs NO
 * registration, NO DOM or island read, NO clock read, NO fetch, NO document
 * mutation and NO background work. It only publishes one small namespaced
 * export on the global object: `WatchdogWebMCP`.
 * ============================================================================
 *
 * EXPLICIT SETUP ENTRY POINT — the ONLY thing that does anything:
 *
 *     await WatchdogWebMCP.setup(options) -> Promise<SetupResult>
 *
 * `options` (all optional, every one an explicit seam; nothing is discovered
 * implicitly beyond the two browser defaults named below):
 *
 *   * `document` — the page document. Default: `globalThis.document`. Used for
 *     exactly two things: the native registration surface
 *     `document.modelContext.registerTool`, and `getElementById` reads of the
 *     two inert JSON islands `#catalog-data` / `#tool-contract`.
 *   * `catalog` — the shared query engine, i.e. the accepted
 *     `web/catalog-query.js` export (`globalThis.WatchdogCatalog` by default).
 *     It supplies the strict raw loader, the snapshot seal check and the five
 *     real query functions. The adapter never re-implements any of them.
 *   * `clock` — the wrapper-owned reference-time source for `watchdog_status`,
 *     returning epoch seconds. Default: whole-second wall time
 *     (`Math.floor(Date.now() / 1000)`). It is read at INVOCATION of that one
 *     tool, never at setup, and it is NOT a public tool argument or schema
 *     field.
 *
 * CURRENT NATIVE API (verified against the official documentation on
 * 2026-10-27):
 *   https://developer.chrome.com/docs/ai/webmcp                (2026-10-07)
 *   https://developer.chrome.com/docs/ai/webmcp/imperative-api (2026-10-16)
 * The documented imperative surface is `document.modelContext.registerTool`,
 * awaited, taking a descriptor `{name, description, inputSchema, execute}` and
 * optional registration options. This adapter feature-detects exactly that
 * callable. The older third-party `navigator.modelContext` shape and any
 * polyfill are deliberately NOT targeted: the whole API-churn surface lives in
 * `_detect()` below, so a future rename is a one-line change here.
 *
 * SETUP RESULT (small, stable, deliberately not a lifecycle framework):
 *
 *   { ok, status, reason, registered, failed, code }
 *
 *   status: "registered"        — every shared tool was registered
 *           "unsupported"       — no callable native API; honest no-op
 *           "invalid"           — island/core/contract fault BEFORE registering
 *           "registration_failed" — the native API refused one tool
 *   reason: a stable code, or null. The reason vocabulary is
 *           "native_api_absent", "core_unavailable",
 *           "catalog_island_missing", "catalog_island_invalid",
 *           "contract_island_missing", "contract_island_invalid",
 *           "contract_not_array", "contract_tool_count",
 *           "contract_invalid_entry", "contract_unknown_tool",
 *           "contract_duplicate_tool", "contract_invalid_description",
 *           "contract_invalid_schema",
 *           "native_registration_threw", "native_registration_rejected".
 *   registered: the tool names actually registered, in registration order.
 *   failed: the tool name that failed, or null.
 *   code: the frozen `CatalogError.code` behind an "invalid" outcome, or null.
 *
 * A whole-tool-unit abort is intentional: a partial registration is reported
 * honestly rather than being retried or repaired, and later tools are not
 * attempted after a failure. Lifecycle/retry/duplicate coverage belongs to the
 * next unit; annotations, `AbortSignal` registration options, page wiring and
 * native RESULT WRAPPING are also out of scope here — `execute` returns the
 * complete raw shared-core payload (including its structured invalid-argument
 * envelope) unchanged.
 *
 * ARGUMENT BOUNDARY: WebMCP may invoke a tool with no argument object at all.
 * `undefined` is therefore forwarded as `{}` — the documented empty-arguments
 * case — while every OTHER value (explicit `null`, arrays, unknown own keys,
 * out-of-range numbers) is forwarded to the shared engine UNCHANGED, so the
 * engine's own success/error policy decides the outcome. Nothing is
 * destructured, coerced, filtered or defaulted here.
 *
 * DESCRIPTOR PROVENANCE: names, descriptions and complete nested input schemas
 * are taken from the embedded `tool-contract` island (the shipped
 * `watchdog_catalog/tools.json` resource) — never from a second hard-coded
 * schema table in this file. Each descriptor gets a defensive deep copy of its
 * schema, so a native layer that mutates what it was given cannot corrupt the
 * captured contract or a later registration.
 */

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.WatchdogWebMCP = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---------- the published shared contract identity ----------------------
  //
  // The five tool names are the ONE piece of fixed knowledge this adapter
  // carries: they bind the shared resource to the five real query functions.
  // Every schema detail still comes from the embedded contract.

  var TOOL_NAMES = Object.freeze([
    "list_free_models",
    "get_model",
    "list_endpoints",
    "watchdog_status",
    "search_models"
  ]);

  /** Shared-contract tool name -> the accepted core function name. */
  var CORE_FUNCTIONS = Object.freeze({
    list_free_models: "listFreeModels",
    get_model: "getModel",
    list_endpoints: "listEndpoints",
    watchdog_status: "watchdogStatus",
    search_models: "searchModels"
  });

  /** The exactly-two inert islands this adapter is allowed to read. */
  var CATALOG_ISLAND_ID = "catalog-data";
  var CONTRACT_ISLAND_ID = "tool-contract";

  /** The descriptor fields the adapter builds; nothing else is invented. */
  var ENTRY_FIELDS = ["name", "description", "inputSchema"];

  /** Frozen clock-failure text, mirroring the Python `_status_epoch` seam. */
  var STATUS_CLOCK_ERROR =
    "watchdog_status requires a usable wall-clock reading.";

  // ---------- tiny structural helpers -------------------------------------

  function isPlainObject(value) {
    if (value === null || typeof value !== "object") {
      return false;
    }
    if (Object.prototype.toString.call(value) !== "[object Object]") {
      return false;
    }
    var prototype = Object.getPrototypeOf(value);
    if (prototype === null) {
      return true;
    }
    // Realm-safe: an ordinary object literal created in ANOTHER realm (a vm
    // context, a same-page frame) carries a FOREIGN Object.prototype, so the
    // prototype reference itself cannot be compared with this realm's one.
    // Its own `constructor` is still the built-in Object either way.
    return Object.prototype.hasOwnProperty.call(prototype, "constructor") &&
           typeof prototype.constructor === "function" &&
           prototype.constructor.name === "Object";
  }

  function hasExactFields(value, fields) {
    if (!isPlainObject(value)) {
      return false;
    }
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

  function isNonEmptyString(value) {
    return typeof value === "string" && value.length > 0;
  }

  /**
   * A recursive defensive copy of a JSON-shaped value.
   *
   * Plain objects and arrays are copied; every other value (string, number,
   * boolean, null, and the shared engine's immutable typed-float wrapper) is
   * passed through by value. This is what lets the native layer own whatever
   * it does with its descriptor without touching the captured contract.
   */
  function copyJson(value) {
    var index;
    if (Array.isArray(value)) {
      var array = new Array(value.length);
      for (index = 0; index < value.length; index += 1) {
        array[index] = copyJson(value[index]);
      }
      return array;
    }
    if (isPlainObject(value)) {
      var object = {};
      var keys = Object.keys(value);
      for (index = 0; index < keys.length; index += 1) {
        object[keys[index]] = copyJson(value[keys[index]]);
      }
      return object;
    }
    return value;
  }

  // ---------- setup result ------------------------------------------------

  function result(status, reason, registered, failed, code) {
    return {
      ok: status === "registered",
      status: status,
      reason: reason === undefined ? null : reason,
      registered: registered === undefined ? [] : registered.slice(),
      failed: failed === undefined ? null : failed,
      code: code === undefined ? null : code
    };
  }

  // ---------- native API detection (the whole churn surface) --------------

  /**
   * The CURRENT documented native registration seam, or null.
   *
   * Kept deliberately tiny and total: a missing/hostile document or a
   * non-callable member degrades to null (honest unsupported) instead of
   * throwing into ordinary page code.
   */
  function detect(document) {
    if (document === null || document === undefined) {
      return null;
    }
    var modelContext;
    try {
      modelContext = document.modelContext;
    } catch (error) {
      return null;
    }
    if (modelContext === null || modelContext === undefined) {
      return null;
    }
    var registerTool;
    try {
      registerTool = modelContext.registerTool;
    } catch (error) {
      return null;
    }
    if (typeof registerTool !== "function") {
      return null;
    }
    return { modelContext: modelContext, registerTool: registerTool };
  }

  // ---------- inert island acquisition ------------------------------------

  /** Read one inert JSON island's text, or null when it is unusable. */
  function readIsland(document, id) {
    var node;
    try {
      node = document.getElementById(id);
    } catch (error) {
      return null;
    }
    if (node === null || node === undefined) {
      return null;
    }
    var text;
    try {
      text = node.textContent;
    } catch (error) {
      return null;
    }
    return typeof text === "string" ? text : null;
  }

  /**
   * Shared-engine availability: the five real query functions must exist.
   *
   * The adapter has no query of its own, so a missing engine is a setup fault,
   * not something to substitute for.
   */
  function coreAvailable(catalog) {
    if (catalog === null || catalog === undefined) {
      return false;
    }
    for (var index = 0; index < TOOL_NAMES.length; index += 1) {
      var name = CORE_FUNCTIONS[TOOL_NAMES[index]];
      if (typeof catalog[name] !== "function") {
        return false;
      }
    }
    return true;
  }

  // ---------- shared contract validation ---------------------------------

  /**
   * Validate the parsed `tool-contract` resource structurally.
   *
   * Returns null when the contract is exactly the five known tools with
   * unique names, usable descriptions and object-typed input schemas whose
   * required/declared properties agree and whose `additionalProperties` is a
   * boolean. Any fault aborts setup BEFORE registration: nothing is repaired,
   * reordered, defaulted or partially registered. Every nested schema keyword
   * (constraints, defaults, patterns, items, nested objects) is preserved
   * as-is — this function never rewrites a schema value.
   */
  function validateContract(contract) {
    if (!Array.isArray(contract)) {
      return "contract_not_array";
    }
    if (contract.length !== TOOL_NAMES.length) {
      return "contract_tool_count";
    }
    var seen = {};
    for (var index = 0; index < contract.length; index += 1) {
      var entry = contract[index];
      if (!hasExactFields(entry, ENTRY_FIELDS)) {
        return "contract_invalid_entry";
      }
      if (!isNonEmptyString(entry.name)) {
        return "contract_invalid_entry";
      }
      if (TOOL_NAMES.indexOf(entry.name) === -1) {
        return "contract_unknown_tool";
      }
      if (Object.prototype.hasOwnProperty.call(seen, entry.name)) {
        return "contract_duplicate_tool";
      }
      seen[entry.name] = true;
      if (!isNonEmptyString(entry.description)) {
        return "contract_invalid_description";
      }
      var schema = entry.inputSchema;
      if (!isPlainObject(schema) || schema.type !== "object") {
        return "contract_invalid_schema";
      }
      if (!isPlainObject(schema.properties)) {
        return "contract_invalid_schema";
      }
      if (!Array.isArray(schema.required)) {
        return "contract_invalid_schema";
      }
      if (typeof schema.additionalProperties !== "boolean") {
        return "contract_invalid_schema";
      }
      var required = schema.required;
      for (var item = 0; item < required.length; item += 1) {
        if (typeof required[item] !== "string" ||
            required[item].length === 0 ||
            !Object.prototype.hasOwnProperty.call(schema.properties,
                                                 required[item])) {
          return "contract_invalid_schema";
        }
      }
      var propertyNames = Object.keys(schema.properties);
      for (var position = 0; position < propertyNames.length; position += 1) {
        if (!isPlainObject(schema.properties[propertyNames[position]])) {
          return "contract_invalid_schema";
        }
      }
    }
    return null;
  }

  // ---------- the dispatch seam ------------------------------------------

  /**
   * One validated wall-clock reading as whole epoch seconds.
   *
   * Mirrors the accepted server seam: a native finite number inside the
   * JSON-safe ceiling, truncated toward zero; anything else is ONE fixed
   * non-reflective error. `bool` does not exist as a distinct type here, and
   * a non-number (including a numeric string) is rejected rather than coerced.
   */
  function referenceEpoch(value) {
    if (typeof value !== "number" || !isFinite(value) || value < 0 ||
        value > Number.MAX_SAFE_INTEGER) {
      throw new Error(STATUS_CLOCK_ERROR);
    }
    return Math.trunc(value);
  }

  /**
   * Build the `execute` callback for one shared tool.
   *
   * `undefined` means "the native layer supplied no argument object" and is
   * forwarded as `{}`; every other value is forwarded UNCHANGED. The return
   * value is the complete raw shared-core payload — result wrapping belongs to
   * the unit that owns the native response boundary.
   */
  function makeExecute(coreFunction, snapshot, capturesClock, clock) {
    return function execute(args) {
      var forwarded = args === undefined ? {} : args;
      if (!capturesClock) {
        return coreFunction(snapshot, forwarded);
      }
      return coreFunction(snapshot, forwarded, referenceEpoch(clock()));
    };
  }

  /** One descriptor, with a defensive schema copy and no invented fields. */
  function makeDescriptor(entry, snapshot, catalog, clock) {
    var coreFunction = catalog[CORE_FUNCTIONS[entry.name]];
    return {
      name: entry.name,
      description: entry.description,
      inputSchema: copyJson(entry.inputSchema),
      execute: makeExecute(coreFunction, snapshot,
                           entry.name === "watchdog_status", clock)
    };
  }

  // ---------- the only entry point ---------------------------------------

  function globalObject() {
    return typeof globalThis !== "undefined" ? globalThis : undefined;
  }

  function defaultClock() {
    return Math.floor(Date.now() / 1000);
  }

  /**
   * Register the five shared tools against the loaded page snapshot.
   *
   * Resolves — never rejects — with the small result described in the file
   * header. Failures are values: ordinary page code is never asked to catch.
   * The snapshot is validated and seal-checked ONCE here; every later tool
   * call queries that captured snapshot without refreshing, reloading or
   * re-sealing it, and without reading provider state.
   */
  async function setup(options) {
    var opts = options === null || options === undefined ? {} : options;
    var host = globalObject();

    var document = opts.document !== undefined
      ? opts.document
      : (host === undefined ? undefined : host.document);
    var catalog = opts.catalog !== undefined
      ? opts.catalog
      : (host === undefined ? undefined : host.WatchdogCatalog);
    var clock = opts.clock !== undefined ? opts.clock : defaultClock;

    // 1. Feature-detect the current native API. Absent -> honest no-op, with
    //    no island read, no clock read and no document mutation.
    var native = detect(document);
    if (native === null) {
      return result("unsupported", "native_api_absent");
    }

    // 2. The shared engine must be present: this adapter owns no query.
    if (!coreAvailable(catalog)) {
      return result("invalid", "core_unavailable");
    }

    // 3. Read exactly the two inert islands through textContent.
    var catalogText = readIsland(document, CATALOG_ISLAND_ID);
    if (catalogText === null) {
      return result("invalid", "catalog_island_missing");
    }
    var contractText = readIsland(document, CONTRACT_ISLAND_ID);
    if (contractText === null) {
      return result("invalid", "contract_island_missing");
    }

    // 4. Decode with the SHARED strict loader (no permissive JSON.parse: float
    //    tokens and duplicate members stay observable) and validate/seal the
    //    snapshot exactly once, here.
    var snapshot;
    try {
      snapshot = catalog.loadSnapshot(catalogText);
    } catch (error) {
      return result("invalid", "catalog_island_invalid", [],
                    null, error && typeof error.code === "string"
                      ? error.code : null);
    }

    var contract;
    try {
      contract = catalog.loadJson(contractText);
    } catch (error) {
      return result("invalid", "contract_island_invalid", [],
                    null, error && typeof error.code === "string"
                      ? error.code : null);
    }

    var reason = validateContract(contract);
    if (reason !== null) {
      return result("invalid", reason);
    }

    // 5. Register every descriptor, in shared-contract order, awaiting each
    //    native call and preserving its receiver. A synchronous throw and a
    //    rejected promise are reported distinctly and stop the sequence.
    var descriptors = [];
    for (var index = 0; index < contract.length; index += 1) {
      descriptors.push(makeDescriptor(contract[index], snapshot, catalog,
                                      clock));
    }

    var registered = [];
    for (var position = 0; position < descriptors.length; position += 1) {
      var descriptor = descriptors[position];
      var pending;
      try {
        pending = native.registerTool.call(native.modelContext, descriptor);
      } catch (error) {
        return result("registration_failed", "native_registration_threw",
                      registered, descriptor.name);
      }
      try {
        await pending;
      } catch (error) {
        return result("registration_failed", "native_registration_rejected",
                      registered, descriptor.name);
      }
      registered.push(descriptor.name);
    }

    return result("registered", null, registered);
  }

  return {
    TOOL_NAMES: TOOL_NAMES,
    setup: setup
  };
});
