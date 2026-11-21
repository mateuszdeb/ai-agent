/*
 * T-16 scenario: static screen shape, safe bootstrap, untrusted content as
 * text only, keyboard-relevant attributes and the field contract shared
 * between index.html, app.css and app.js.
 */
import {
  readFileSync
} from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.join(here, "..", "..", "src", "agent_project", "web", "static");
const html = readFileSync(path.join(staticDir, "index.html"), "utf8");
const css = readFileSync(path.join(staticDir, "app.css"), "utf8");
const js = readFileSync(path.join(staticDir, "app.js"), "utf8");

export async function run({ loadUI, stubFetch, assert, assertEqual }) {
  /* --- Static source guarantees (no CDN, no inline active content). --- */
  assert(!/https?:\/\//.test(html), "index.html must not reference external URLs");
  assert(!/https?:\/\/(?!127\.0\.0\.1)/.test(css), "app.css must not reference external URLs");
  assert(!/https?:\/\/(?!127\.0\.0\.1)/.test(js), "app.js must not reference external URLs");
  assert(!/\son[a-z]+\s*=/.test(html), "index.html must not use inline event handlers");
  assert(!/<style[ >]/.test(html), "index.html must not carry inline styles");
  assert(!/<script[^>]*>[^<]/.test(html), "index.html must not carry an inline script");
  assert(
    !/\.innerHTML\s*=|\.outerHTML\s*=|\.insertAdjacentHTML\s*\(|document\.write\s*\(/.test(js),
    "app.js must never assign or stream HTML"
  );
  assert(!/\beval\s*\(|new Function\s*\(/.test(js), "app.js must never evaluate dynamic code");

  /* --- Keyboard accessibility and narrow viewport commitments. --- */
  assert(/:focus-visible\s*{/.test(css), "app.css must define a visible focus style");
  assert(/@media[^{]*max-width/.test(css), "app.css must include a narrow-view media query");
  assert(/overflow-x:\s*auto/.test(css), "the transactions table must scroll horizontally");
  assert(/<label for="goal-input">/.test(html), "the goal field needs a programmatic label");
  assert(/role="status"/.test(html), "service/notes areas must be announced via role=status");
  assert(/role="alert"/.test(html), "validation errors must be announced via role=alert");
  assert(/<th scope="col">/.test(html), "table headers need explicit scope");

  /* --- Renderer safety: untrusted data never becomes markup. --- */
  const payload = '<img src=x onerror=alert(1)><script>alert("pwned")</script>';
  const { ui, document } = loadUI();
  const box = document.createElement("div");
  ui.listChildren(box, [
    ui.h("span", { class: "merchant" }, payload),
    ui.h("b", null, payload)
  ]);
  assertEqual(box.children.length, 2, "renderer must build exactly two nodes");
  assertEqual(box.children[0].tagName, "SPAN", "unexpected node type");
  assertEqual(box.children[0].textContent, payload, "untrusted text must survive verbatim as text");
  assertEqual(box.children[0].children.length, 0, "untrusted text must never be parsed into nodes");
  assertEqual(box.children[1].children.length, 0, "untrusted text must never be parsed into nodes");

  /* --- Pure helpers: validation, statuses, rows, notes. --- */
  assertEqual(ui.validateGoal("").ok, false, "empty goal must fail validation");
  assertEqual(ui.validateGoal("   \n\t").ok, false, "blank goal must fail validation");
  assertEqual(ui.validateGoal("Review duplicates").ok, true, "plain goal must pass");
  assertEqual(ui.validateGoal("x".repeat(8001)).ok, false, "over-long goal must fail");
  assertEqual(ui.runStatusLabel("running"), "Running", "status label contract");
  assertEqual(ui.runStatusLabel("awaiting_approval"), "Waiting for your decision", "status label");
  assertEqual(ui.runStatusLabel("mystery"), "Unknown run status", "unknown status stays text");

  const row = ui.transactionRow({
    transaction_id: "tx_1",
    date: "2026-03-02",
    merchant: "& <script>",
    amount: -12.5,
    currency: "USD",
    category: null
  });
  assertEqual(row.cells[2], "& <script>", "merchant stays raw text");
  assertEqual(row.cells[3], "-12.5 USD", "amount carries the currency");
  assertEqual(row.cells[4], "Uncategorized", "missing category shows Uncategorized");

  assertEqual(ui.truncatedNote(0, 50, false), "No transactions found.", "empty note");
  assertEqual(ui.truncatedNote(3, 50, false), "Showing 3 results.", "counted note");
  assertEqual(ui.truncatedNote(1, 50, false), "Showing 1 result.", "singular note");
  assertEqual(
    ui.truncatedNote(50, 50, true),
    "Showing up to 50 results. Refine your search.",
    "truncation hint never invents a total"
  );

  /* --- Bootstrap: only /api/session; success and failure paths. --- */
  const okFetch = stubFetch([
    { status: 200, body: { csrf_token: "csrf_x", instance_id: "inst_x", active_run_id: null } }
  ]);
  const okApp = loadUI({ fetch: okFetch });
  const session = await okApp.ui.fetchSession();
  assertEqual(okFetch.calls.length, 1, "bootstrap performs exactly one API call");
  assertEqual(okFetch.calls[0].url, "/api/session", "bootstrap must hit only the session endpoint");
  assertEqual(okFetch.calls[0].method, "GET", "bootstrap must not mutate anything");
  assertEqual(session.csrf_token, "csrf_x", "session payload is consumed, not echoed");
  assertEqual(
    okApp.document.getElementById("service-state").textContent,
    "Connected to the local service.",
    "service state text after bootstrap"
  );

  const failFetch = stubFetch([{ status: 503, body: {} }]);
  const failApp = loadUI({ fetch: failFetch });
  await failApp.ui.fetchSession();
  assertEqual(
    failApp.document.getElementById("service-state").textContent,
    "Service is unreachable. Refresh the page to try again.",
    "failure path shows an English recovery hint"
  );

  const crashFetch = stubFetch([{ reject: "network down" }]);
  const crashApp = loadUI({ fetch: crashFetch });
  await crashApp.ui.fetchSession();
  assertEqual(
    crashApp.document.getElementById("service-state").textContent,
    "Service is unreachable. Refresh the page to try again.",
    "network failure also lands on the safe recovery text"
  );
}
