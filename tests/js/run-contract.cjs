#!/usr/bin/env node
"use strict";

/**
 * Permanent Python/Node contract harness CLI for `web/catalog-query.js`.
 *
 * Built-in modules only (no runtime package). It is a TRANSPORT and DISPATCH
 * layer: every operation below calls the real exported implementation from
 * `../../web/catalog-query.js` and never re-implements validation, selection,
 * ordering, encoding or pagination. A `require()`/`import()` of this file is
 * inert; only command execution owns stdin/stdout. That is enforced at the very
 * bottom of the file by an explicit entry-point guard (`require.main ===
 * module`), so loading the module neither reads stdin, nor writes a protocol
 * document, nor changes the process exit status.
 *
 * Raw stdin is bounded WHILE it is read, not after it has been accumulated: it
 * is consumed in fixed `STDIN_CHUNK_BYTES` reads and the first byte past
 * `MAX_STDIN_BYTES` rejects the document immediately (one bounded detection
 * read of allowance, never an unbounded read-to-EOF). The accumulated buffer
 * therefore never exceeds the bound, and the bytes are decoded once at the end
 * so a multi-byte UTF-8 sequence split across two reads stays intact.
 *
 * Protocol v1 -- one deterministic JSON document in, one out.
 *
 *   stdin : {"protocol": 1, "requests": [<request>, ...]}
 *   stdout: {"protocol": 1, "responses": [<response>, ...]}
 *
 * Exactly ONE JSON document is written to stdout and nothing else (diagnostics
 * go to stderr). Responses are emitted in request order, echo their request
 * `id` verbatim, and the response count always equals the request count, so a
 * caller can and must check ids/cardinality instead of silently accepting a
 * truncated list.
 *
 *   <request>  = {"id": <string>, "op": <op>, ...op fields}
 *   <response> = {"id": <string>, "op": <op>, "status": <status>, ...}
 *
 * `status` is one of:
 *   "ok"       the operation ran; `canonical` holds the typed canonical text of
 *              the primary value. A returned ERROR ENVELOPE (the frozen
 *              argument-error payload of a query) is a normal `ok` value.
 *   "rejected" the published raw/structural boundary rejected the document;
 *              `canonical` holds the typed canonical text of the frozen
 *              `{ok:false,error:{code,message}}` payload and `code` the code.
 *
 * An implementation exception, a malformed request or a setup fault is NOT
 * translated into a happy response: the process writes a diagnostic to stderr
 * and exits non-zero so a broken harness can never look like a passing one.
 *
 * Typed values. Comparison happens on the accepted typed canonical text
 * (`canonicalText`, the counterpart of Python `canonical_bytes`), which keeps
 * the integer-versus-float distinction, so no value is round-tripped through a
 * lossy `JSON.stringify`/`JSON.parse` pair.
 *
 * Argument documents are carried as RAW TEXT in `args_text` and decoded with
 * `decodeTypedJson` -- the accepted non-published typed decoder -- because a
 * harness control may legitimately contain an unsafe numeric argument value
 * that the PUBLIC raw-document path is required to reject. Published catalogue
 * documents keep the strict `loadJson`/`loadSnapshot` path.
 *
 * Operations
 *   hello            {}                    runner/module identity + export names
 *   load_json        {"text"}              loadJson: canonical value | envelope
 *   load_snapshot    {"text"}              loadSnapshot: canonical + snapshotHash
 *   validate_value   {"value_text"}        structural layer on a native value
 *   canonical_vector {"text","scope"?}     decodeTypedJson -> canonical + sha256
 *   tool             {"tool", "fixture"|"snapshot_text", "args_text", "now"?}
 *                                          dispatch a real query export
 *   ordering         {"ids"}               compareNaturalIds + naturalKey
 *   marker_strip     {"ids"}               stripFreeMarker per id
 *   normalization    {"raw_text"}          projectCapabilities + normalizeCapabilities
 */

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const catalog = require("../../web/catalog-query.js");

const PROTOCOL = 1;
const MAX_STDIN_BYTES = 32 * 1024 * 1024;
//: Fixed read size for raw stdin. It is also the overflow-detection allowance:
//: the bound is noticed within one chunk of the limit, never after EOF.
const STDIN_CHUNK_BYTES = 64 * 1024;
//: A stdin descriptor that libuv made non-blocking (see nodejs/node#42826) can
//: answer `readSync` with EAGAIN instead of data; retry that for a bounded
//: wall-clock budget rather than spinning or giving up immediately.
const EAGAIN_MAX_WAIT_MS = 60000;
const FIXTURES_DIR = path.join(__dirname, "..", "fixtures", "catalog");
const MODULE_PATH = path.join(__dirname, "..", "..", "web", "catalog-query.js");

const TOOL_EXPORTS = {
  search_models: "searchModels",
  get_model: "getModel",
  list_free_models: "listFreeModels",
  list_endpoints: "listEndpoints",
  watchdog_status: "watchdogStatus"
};

function sha256Hex(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function canonical(value) {
  return catalog.canonicalText(value);
}

function fixturePath(name) {
  if (typeof name !== "string" || name.length === 0) {
    throw new Error("a fixture request needs a nonempty fixture name");
  }
  const resolved = path.resolve(FIXTURES_DIR, name);
  const relative = path.relative(FIXTURES_DIR, resolved);
  if (relative === "" || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("fixture name escapes the fixture directory: " + name);
  }
  return resolved;
}

function snapshotFor(request) {
  if (typeof request.snapshot_text === "string") {
    return catalog.loadSnapshot(request.snapshot_text);
  }
  if (typeof request.fixture === "string") {
    return catalog.loadSnapshot(
      fs.readFileSync(fixturePath(request.fixture), "utf8"));
  }
  throw new Error("a tool request needs `fixture` or `snapshot_text`");
}

function rejection(error) {
  if (!(error instanceof catalog.CatalogError)) {
    throw error;
  }
  return {status: "rejected", code: error.code,
          canonical: canonical(error.toPayload())};
}

const OPS = {
  hello: function () {
    return {
      status: "ok",
      protocol: PROTOCOL,
      node: process.version,
      cwd: process.cwd(),
      module: fs.realpathSync(MODULE_PATH),
      module_sha256: sha256Hex(fs.readFileSync(MODULE_PATH)),
      fixture_dir: fs.realpathSync(FIXTURES_DIR),
      exports: Object.keys(catalog).sort(),
      max_safe_int: catalog.MAX_SAFE_INT
    };
  },

  load_json: function (request) {
    try {
      return {status: "ok", canonical: canonical(catalog.loadJson(request.text))};
    } catch (error) {
      return rejection(error);
    }
  },

  load_snapshot: function (request) {
    try {
      const snapshot = catalog.loadSnapshot(request.text);
      return {
        status: "ok",
        canonical: canonical(snapshot),
        hash: catalog.snapshotHash(snapshot),
        snapshot_id: snapshot.snapshot_id
      };
    } catch (error) {
      return rejection(error);
    }
  },

  validate_value: function (request) {
    // The STRUCTURAL layer on a native value: the typed non-published decoder
    // lets a rule-table control carry an unsafe numeric value that the public
    // raw path is required to reject.
    const value = catalog.decodeTypedJson(request.value_text);
    try {
      const snapshot = catalog.validateSnapshot(value);
      return {
        status: "ok",
        canonical: canonical(snapshot),
        hash: catalog.snapshotHash(snapshot),
        snapshot_id: snapshot.snapshot_id
      };
    } catch (error) {
      return rejection(error);
    }
  },

  canonical_vector: function (request) {
    const value = catalog.decodeTypedJson(request.text);
    const text = canonical(value);
    if (request.scope === "snapshot") {
      // The fixture's `snapshot` vectors claim the canonical BODY (the root
      // `snapshot_id` excluded), so the accepted body digest is reported too.
      const body = {};
      for (const key of Object.keys(value)) {
        if (key !== "snapshot_id") {
          Object.defineProperty(body, key, {
            value: value[key], enumerable: true, writable: true,
            configurable: true
          });
        }
      }
      return {
        status: "ok",
        canonical: text,
        body_canonical: canonical(body),
        sha256: catalog.snapshotHash(value)
      };
    }
    return {
      status: "ok",
      canonical: text,
      sha256: sha256Hex(Buffer.from(text, "utf8"))
    };
  },

  tool: function (request) {
    const name = TOOL_EXPORTS[request.tool];
    if (!name) {
      throw new Error("unknown tool: " + String(request.tool));
    }
    if (typeof catalog[name] !== "function") {
      throw new Error("the shared implementation does not export " + name);
    }
    let snapshot;
    try {
      snapshot = snapshotFor(request);
    } catch (error) {
      // A rejected PUBLISHED document is a contract outcome, not a fault: the
      // same frozen envelope the load ops report.
      return rejection(error);
    }
    const args = catalog.decodeTypedJson(
      request.args_text === undefined ? "{}" : request.args_text);
    let result;
    if (request.tool === "watchdog_status") {
      if (!Number.isInteger(request.now)) {
        throw new Error("watchdog_status needs an explicit fixed integer `now`");
      }
      result = catalog[name](snapshot, args, request.now);
    } else {
      result = catalog[name](snapshot, args);
    }
    return {status: "ok", canonical: canonical(result)};
  },

  ordering: function (request) {
    if (!Array.isArray(request.ids)) {
      throw new Error("ordering needs an `ids` array");
    }
    const sorted = request.ids.slice().sort(catalog.compareNaturalIds);
    return {
      status: "ok",
      canonical: canonical({
        order: sorted,
        keys: request.ids.map((rawId) => catalog.naturalKey(rawId))
      })
    };
  },

  marker_strip: function (request) {
    if (!Array.isArray(request.ids)) {
      throw new Error("marker_strip needs an `ids` array");
    }
    return {
      status: "ok",
      canonical: canonical(request.ids.map(
        (rawId) => catalog.stripFreeMarker(rawId)))
    };
  },

  normalization: function (request) {
    if (typeof request.raw_text !== "string") {
      throw new Error("normalization needs `raw_text`");
    }
    const raw = catalog.decodeTypedJson(request.raw_text);
    const pair = catalog.projectCapabilities(raw);
    return {
      status: "ok",
      canonical: canonical({
        projected: pair[0],
        warnings: pair[1],
        normalized: catalog.normalizeCapabilities(pair[0])
      })
    };
  }
};

function sleepMs(milliseconds) {
  // A bounded blocking wait that owns no timer handle: the main thread parks on
  // a private zeroed cell for at most `milliseconds`.
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
}

function readStdin() {
  // Incremental, bounded raw reads. `fs.readFileSync(0)` would materialize the
  // WHOLE stream before any bound could be applied, which is not enforcement
  // while consuming: a 4 GiB document would be allocated first.
  const chunks = [];
  const buffer = Buffer.allocUnsafe(STDIN_CHUNK_BYTES);
  let total = 0;
  let reads = 0;
  let largest = 0;
  let waitedMs = 0;
  for (;;) {
    let bytesRead;
    try {
      bytesRead = fs.readSync(0, buffer, 0, buffer.length, null);
    } catch (error) {
      if (error && (error.code === "EAGAIN" || error.code === "EWOULDBLOCK")) {
        if (waitedMs >= EAGAIN_MAX_WAIT_MS) {
          throw new Error("stdin stayed unreadable (EAGAIN) for " +
                          waitedMs + " ms");
        }
        sleepMs(5);
        waitedMs += 5;
        continue;
      }
      if (error && error.code === "EOF") {
        break;                       // older Node raises EOF at end of input
      }
      throw error;
    }
    if (bytesRead === 0) {
      break;                         // EOF
    }
    reads += 1;
    if (bytesRead > largest) {
      largest = bytesRead;
    }
    total += bytesRead;
    if (total > MAX_STDIN_BYTES) {
      // Reject the FIRST byte past the bound: no wait for EOF, no assembled
      // string and no JSON.parse for an oversized document.
      throw new Error(
        "stdin exceeds the " + MAX_STDIN_BYTES + " byte bound: rejected at " +
        total + " bytes after " + reads + " bounded reads of at most " +
        buffer.length + " bytes (largest read " + largest + ")");
    }
    chunks.push(Buffer.from(buffer.subarray(0, bytesRead)));
  }
  // Decode ONCE over the concatenated bytes, so a multi-byte UTF-8 sequence
  // that straddles a read boundary is still decoded correctly. `total` never
  // exceeds MAX_STDIN_BYTES, so this assembly is bounded by the same limit.
  return Buffer.concat(chunks, total).toString("utf8");
}

function main() {
  const document = JSON.parse(readStdin());
  if (!document || document.protocol !== PROTOCOL ||
      !Array.isArray(document.requests)) {
    throw new Error("stdin must be {\"protocol\":1,\"requests\":[...]}");
  }
  const responses = document.requests.map((request) => {
    if (!request || typeof request.id !== "string") {
      throw new Error("every request needs a string `id`");
    }
    const op = OPS[request.op];
    if (!op) {
      throw new Error("unknown op: " + String(request.op));
    }
    const response = op(request);
    return Object.assign({id: request.id, op: request.op}, response);
  });
  process.stdout.write(JSON.stringify(
    {protocol: PROTOCOL, responses: responses}));
}

function runCli() {
  try {
    main();
  } catch (error) {
    process.stderr.write("run-contract.cjs: " + (error && error.stack
      ? error.stack : String(error)) + "\n");
    process.exitCode = 1;
  }
}

// The entry-point guard: a plain `require()`/dynamic `import()` of this file
// returns here and stays inert (Node modules docs, "Accessing the main module";
// for a non-CommonJS entry point `require.main` is `undefined`, so the guard is
// false there too). Only `node run-contract.cjs` owns stdin/stdout and the exit
// status.
if (require.main === module) {
  runCli();
}
