/*
 * Node scenario harness for the static UI (no DOM, no network).
 *
 * Loads src/agent_project/web/static/app.js into a sandbox with a minimal
 * DOM stub and scripted fetch/time/storage, then runs one scenario module
 * (argv[2]) exporting `run(ui, tools)`. Assertion failures exit code 1.
 *
 * The sandbox intentionally provides no innerHTML, no eval and no layout:
 * anything the renderer builds must pass through createElement/textContent.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import process from "node:process";

const here = path.dirname(fileURLToPath(import.meta.url));
const appSource = readFileSync(
  path.join(here, "..", "src", "agent_project", "web", "static", "app.js"),
  "utf8"
);

/** Minimal DOM node: only the APIs app.js is allowed to use. */
function makeNode(tag) {
  const attributes = new Map();
  const listeners = new Map();
  let openState = false;
  const node = {
    tagName: String(tag).toUpperCase(),
    firstChild: null,
    lastChild: null,
    listeners,
    _children: [],
    get id() {
      return attributes.get("id") ?? "";
    },
    set open(v) {
      openState = Boolean(v);
    },
    get open() {
      return openState;
    },
    set className(value) {
      attributes.set("class", value);
    },
    get className() {
      return attributes.get("class") ?? "";
    },
    setAttribute(name, value) {
      attributes.set(String(name), String(value));
    },
    getAttribute(name) {
      return attributes.get(String(name)) ?? null;
    },
    removeAttribute(name) {
      attributes.delete(String(name));
    },
    addEventListener(type, handler) {
      const list = listeners.get(type) ?? [];
      list.push(handler);
      listeners.set(type, list);
    },
    dispatchEvent(type, event = {}) {
      for (const handler of listeners.get(type) ?? []) {
        handler({ type, target: node, ...event });
      }
    },
    appendChild(child) {
      child.parentNode?.removeChild(child);
      node._children.push(child);
      child.parentNode = node;
      node.firstChild = node._children[0];
      node.lastChild = child;
      return child;
    },
    removeChild(child) {
      const index = node._children.indexOf(child);
      if (index === -1) throw new Error("removeChild: not a child");
      node._children.splice(index, 1);
      child.parentNode = null;
      node.firstChild = node._children[0] ?? null;
      node.lastChild = node._children[node._children.length - 1] ?? null;
      return child;
    },
    focus() {
      node.focused = true;
    },
    click() {
      node.dispatchEvent("click");
    }
  };
  let textValue = "";
  Object.defineProperty(node, "textContent", {
    get() {
      if (node._children.length > 0) {
        return node._children.map((child) => child.textContent).join("");
      }
      return textValue;
    },
    set(value) {
      node._children = [];
      node.firstChild = null;
      node.lastChild = null;
      textValue = String(value);
    }
  });
  Object.defineProperty(node, "children", {
    get() {
      return [...node._children];
    }
  });
  return node;
}

function makeDocument(existing = {}) {
  const created = new Map();
  const built = [];
  const doc = {
    readyState: "complete",
    createElement(tag) {
      const node = makeNode(tag);
      built.push(node);
      return node;
    },
    getElementById(id) {
      if (existing[id]) return existing[id];
      const found = built.find((node) => node.getAttribute && node.getAttribute("id") === id);
      if (found) return found;
      if (!created.has(id)) created.set(id, makeNode("div"));
      const node = created.get(id);
      node.setAttribute("id", id);
      return node;
    },
    addEventListener() {}
  };
  doc._pending = created;
  return doc;
}

/** Sequence/tamper-evident fetch stub replaying scripted JSON responses. */
export function stubFetch(responses) {
  const queue = [...responses];
  const calls = [];
  const fetch = (input, init = {}) => {
    const url = String(input);
    const next = queue.shift();
    const call = {
      url,
      method: init.method ?? "GET",
      body: init.body ?? null,
      headers: init.headers ?? {}
    };
    calls.push(call);
    if (next === undefined) {
      return Promise.resolve({ ok: false, status: 999, json: async () => ({}) });
    }
    if (next.reject) {
      return Promise.reject(new Error(next.reject));
    }
    return Promise.resolve({
      ok: next.status === undefined || next.status < 400,
      status: next.status ?? 200,
      json: async () => next.body
    });
  };
  fetch.calls = calls;
  fetch.queue = queue;
  return fetch;
}

/** Deterministic crypto-quality token generator for request ids. */
export function stubToken() {
  let counter = 0;
  return (label = "req") => {
    counter += 1;
    return `${label}_${String(counter).padStart(3, "0")}`;
  };
}

/** Manual timers: advance() fires due callbacks in schedule order. */
export function fakeTimers() {
  let now = 0;
  let seq = 0;
  const pending = new Map();
  const timers = {
    now: () => now,
    set(handler, delay = 0) {
      seq += 1;
      pending.set(seq, { at: now + delay, handler });
      return seq;
    },
    clear(handle) {
      pending.delete(handle);
    },
    async advance(ms) {
      const target = now + ms;
      // Fire due timers repeatedly in due order (stable seq order ties).
      for (;;) {
        const due = [...pending.entries()]
          .filter(([, timer]) => timer.at <= target)
          .sort((a, b) => a[1].at - b[1].at || a[0] - b[0]);
        if (due.length === 0) break;
        const [handle, timer] = due[0];
        pending.delete(handle);
        now = timer.at;
        await timer.handler();
        // Let every promise chain spawned by the handler settle before
        // recomputing due timers; setImmediate drains all microtasks.
        await new Promise((resolve) => setImmediate(resolve));
      }
      now = target;
    },
    get pendingCount() {
      return pending.size;
    }
  };
  return timers;
}

/** sessionStorage stub with call recording. */
export function stubStorage() {
  const data = new Map();
  const reads = [];
  const writes = [];
  return {
    get(key) {
      reads.push(key);
      return data.has(key) ? data.get(key) : null;
    },
    set(key, value) {
      data.set(key, String(value));
      writes.push([key, String(value)]);
    },
    remove(key) {
      data.delete(key);
    },
    reads,
    writes,
    dump: () => Object.fromEntries(data)
  };
}

/** Evaluate app.js exactly like a browser would (single script, strict). */
export function loadUI({ doc, fetch } = {}) {
  const documentInstance = doc ?? makeDocument();
  const sessionStorageInstance = stubStorage();
  const sandboxWindow = { __UI_MANUAL__: true };
  const factory = new Function(
    "window",
    "document",
    "sessionStorage",
    "crypto",
    "fetch",
    "location",
    "console",
    appSource
  );
  const uiResult = factory(
    sandboxWindow,
    documentInstance,
    sessionStorageInstance,
    { getRandomValues: () => {} },
    fetch ?? (() => Promise.resolve({ ok: false, status: 999, json: async () => ({}) })),
    { href: "http://127.0.0.1:8765/" },
    { error: () => {} }
  );
  const ui = uiResult ?? sandboxWindow.UI;
  if (!ui) throw new Error("app.js did not expose window.UI");
  return { ui, document: documentInstance, storage: sessionStorageInstance };
}

/** Tiny assertion helpers shared by scenarios. */
export function assert(condition, message) {
  if (!condition) {
    throw new Error(`Assertion failed: ${message}`);
  }
}

export function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(
      `Assertion failed: ${message} (actual ${JSON.stringify(actual)}, expected ${JSON.stringify(expected)})`
    );
  }
}

/* Run scenario: node tests/web_ui_harness.mjs tests/web_ui/<name>.mjs */
const scenarioPath = process.argv[2];
if (!scenarioPath) {
  console.error("Usage: node tests/web_ui_harness.mjs <scenario.mjs>");
  process.exit(2);
}
try {
  const { run } = await import(path.resolve(scenarioPath));
  await run({
    loadUI,
    stubFetch,
    stubToken,
    fakeTimers,
    stubStorage,
    makeDocument,
    assert,
    assertEqual
  });
} catch (error) {
  console.error(error?.stack ?? String(error));
  process.exit(1);
}
console.log(`Scenario passed: ${path.basename(scenarioPath)}`);
