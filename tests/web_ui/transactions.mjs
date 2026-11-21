/*
 * T-19 scenario: the transaction list reads the effective backend state,
 * filters map to explicit query strings, truncation stays a hint, details
 * open lazily per row, failures show English notes and the empty set is
 * rendered without inventing rows.
 */

const SESSION = { csrf_token: "csrf_19", instance_id: "inst_19", active_run_id: null };

function makeController(tools, fetch, doc, withRenderer = false) {
  const timers = tools.fakeTimers();
  const loaded = tools.loadUI(doc ? { doc, fetch } : { fetch });
  const records = { transactions: [], detail: [], run: [] };
  const recorded = {
    run: [],
    cards: [],
    transactions: [],
    detail: []
  };
  const passthrough = {
    run: (view) => recorded.run.push(view),
    cards: (view) => recorded.cards.push(view),
    transactions: (view) => recorded.transactions.push(view),
    detail: (view) => recorded.detail.push(view)
  };
  const views = withRenderer
    ? {
        run: (view) => {
          passthrough.run(view);
          loaded.ui.renderRun(view);
        },
        cards: (view) => {
          passthrough.cards(view);
          loaded.ui.renderCards(view);
        },
        transactions: (view) => {
          passthrough.transactions(view);
          loaded.ui.renderTransactions(view);
        },
        detail: (view) => {
          passthrough.detail(view);
          loaded.ui.renderDetail(view);
        },
        service() {}
      }
    : { ...passthrough, service() {} };
  const controller = loaded.ui.createController({ fetch, storage: tools.stubStorage(), token: tools.stubToken(), timers, views });
  return { controller, timers, records: recorded, ui: loaded.ui };
}

export async function run({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage, makeDocument, assert, assertEqual }) {
  const flush = () => new Promise((resolve) => setImmediate(resolve));

  /* --- Filters -> explicit query string (allowlisted keys only). --- */
  const ui = loadUI().ui;
  assertEqual(ui.transactionFiltersQuery("tea leak", "Corner Coffee"), "query=tea%20leak&merchant=Corner%20Coffee", "both filters encoded");
  assertEqual(ui.transactionFiltersQuery("   ", null), "", "blank filters yield an empty query");
  assertEqual(ui.transactionFiltersQuery("a?b&c"), "query=a%3Fb%26c", "special characters stay data");
  assertEqual(ui.transactionFiltersQuery(null, "\tCoffee "), "merchant=Coffee", "merchant trimmed and alone");

  /* --- Empty result set: honest empty message, no fabricated rows. --- */
  const emptyFetch = stubFetch([
    { status: 200, body: { items: [], limit: 50, possibly_truncated: false } }
  ]);
  const emptyCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, emptyFetch);
  await emptyCase.controller.refreshTransactions();
  await flush();
  const emptyView = emptyCase.records.transactions[0];
  assertEqual(emptyView.phase, "transactions", "empty list phase");
  assertEqual(emptyView.rows.length, 0, "no rows are invented");
  assertEqual(emptyView.note, "No transactions found.", "English empty note");

  /* --- Bounded list: truncation hint mentions the limit, never a total. --- */
  const rowsIsNull = (limit) =>
    Array.from({ length: limit }, (_, index) => ({
      transaction_id: "tx_" + String(index).padStart(2, "0"),
      date: "2026-03-0" + (index + 1),
      merchant: "Merchant " + index,
      amount: -10 - index,
      currency: "USD",
      category: index % 2 ? "Groceries" : null
    }));
  const truncFetch = stubFetch([
    { status: 200, body: { items: rowsIsNull(50), limit: 50, possibly_truncated: true } }
  ]);
  const truncCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, truncFetch);
  await truncCase.controller.refreshTransactions("stream");
  await flush();
  assertEqual(truncFetch.calls[0].url, "/api/transactions?query=stream", "refresh carries the current filter");
  const truncView = truncCase.records.transactions[0];
  assertEqual(truncView.rows.length, 50, "the bounded list renders up to the limit");
  assertEqual(truncView.note, "Showing up to 50 results. Refine your search.", "truncated hint reuses the backend limit");
  const mixedRow = truncView.rows[1].cells;
  assertEqual(mixedRow[4], "Groceries", "category stays untranslated");
  assertEqual(truncView.rows[0].cells[4], "Uncategorized", "missing category renders Uncategorized");
  assertEqual(truncView.rows[3].cells[3], "-13 USD", "amount keeps the currency");

  /* --- Merchant-only filter goes through reads; data stays effective. --- */
  const merchantFetch = stubFetch([
    { status: 200, body: { items: [rowsIsNull(1)[0]], limit: 50, possibly_truncated: false } }
  ]);
  const merchantCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, merchantFetch);
  await merchantCase.controller.refreshTransactions(null, "Merchant 0");
  await flush();
  assertEqual(merchantFetch.calls[0].url, "/api/transactions?merchant=Merchant%200", "merchant filter is an explicit query parameter");
  assertEqual(merchantCase.records.transactions[0].note, "Showing 1 result.", "untruncated counted note");

  /* --- Lazy details: only an opened row fetches its record. --- */
  const detailFetch = stubFetch([
    {
      status: 200,
      body: {
        transaction_id: "tx_03",
        date: "2026-03-04",
        merchant: "Corner Coffee <script>",
        amount: -13,
        currency: "USD",
        category: "Groceries",
        description: 'Loyalty tea <img src=x onerror="alert(1)">'
      }
    }
  ]);
  const detailCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, detailFetch);
  await detailCase.controller.openTransaction("tx_03");
  await flush();
  assertEqual(detailFetch.calls[0].url, "/api/transactions/tx_03", "detail reads the exact record");
  const detailView = detailCase.records.detail[0];
  assertEqual(detailView.phase, "detail", "detail phase");
  assertEqual(detailView.lines[0], "Description: Loyalty tea <img src=x onerror=\"alert(1)\">", "untrusted description stays text");
  assertEqual(detailView.lines[1], "Category: Groceries", "categorized record keeps its label");

  const missingFetch = stubFetch([
    { status: 404, body: { error: { code: "transaction_unavailable", message: "The requested resource is unavailable." } } }
  ]);
  const missingCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, missingFetch);
  await missingCase.controller.openTransaction("tx_nope");
  await flush();
  const missingView = missingCase.records.detail[0];
  assertEqual(missingView.phase, "detail_unavailable", "missing record phase");
  assertEqual(missingView.lines[0], "This transaction record is not available.", "English safe detail message");

  /* --- Read service busy: English note, no fabricated rows. --- */
  const busyFetch = stubFetch([
    { status: 503, body: { error: { code: "state_busy", message: "Transaction state is busy." } } }
  ]);
  const busyCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, busyFetch);
  await busyCase.controller.refreshTransactions();
  await flush();
  const busyView = busyCase.records.transactions[0];
  assertEqual(busyView.phase, "unavailable", "busy phase");
  assertEqual(busyView.note, "Transaction data is busy. Try again shortly.", "English busy note");
  assertEqual(busyView.rows.length, 0, "busy never fabricates rows");

  /* --- Renderer: rows driven by data, details keyboard-accessible. --- */
  const tools = { loadUI, stubFetch, stubToken, fakeTimers, stubStorage };
  const doc = makeDocument();
  const renderFetch = stubFetch([
    { status: 200, body: { items: rowsIsNull(2), limit: 50, possibly_truncated: false } },
    {
      status: 200,
      body: {
        transaction_id: "tx_00",
        date: "2026-03-01",
        merchant: "Merchant 0",
        amount: -10,
        currency: "USD",
        category: null,
        description: 'Loyalty tea <img src=x onerror="alert(1)">'
      }
    },
    { status: 404, body: { error: { code: "transaction_unavailable", message: "The requested resource is unavailable." } } }
  ]);
  const renderCase = makeController(tools, renderFetch, doc, true);
  renderCase.ui.bindDetail(renderCase.controller.openTransaction.bind(renderCase.controller));
  await renderCase.controller.refreshTransactions();
  await flush();
  const tableBody = doc.getElementById("transactions-body");
  assertEqual(tableBody.children.length, 2, "renderer builds rows from data");
  const firstRow = tableBody.children[0];
  assertEqual(firstRow.tagName, "TR", "row element");
  for (let i = 0; i < 6; i += 1) {
    assertEqual(firstRow.children[i].tagName, "TD", "cells only");
  }
  const fold = firstRow.children[5].children[0];
  assertEqual(fold.tagName, "DETAILS", "details element per row");
  assertEqual(fold.children[0].tagName, "SUMMARY", "summary keeps native toggle semantics");
  assertEqual(fold.children[0].textContent, "Details", "English details toggle");
  for (let i = 0; i < 6; i += 1) {
    assertEqual(tableBody.children[1].children[i].tagName, "TD", "second row cells only");
  }

  /* --- Toggling a detail fetches only that record; untrusted text stays. --- */
  fold.open = true;
  fold.dispatchEvent("toggle");
  await flush();
  assert(
    renderFetch.calls.some((call) => call.url === "/api/transactions/tx_00"),
    "toggling the first row read its record"
  );
  const body00 = doc.getElementById("detail-for-tx_00");
  assert(
    body00.textContent.includes("Description: Loyalty tea <img src=x"),
    "untrusted description appears only as text"
  );
  assert(body00.textContent.includes("Uncategorized"), "missing category note");
  // Second row stays shut; opening it hits a missing record.
  const secondFold = tableBody.children[1].children[5].children[0];
  secondFold.open = true;
  secondFold.dispatchEvent("toggle");
  await flush();
  assert(
    renderFetch.calls.some((call) => call.url === "/api/transactions/tx_01"),
    "second row toggling reads only that record"
  );
  const body01 = doc.getElementById("detail-for-tx_01");
  assert(body01.textContent.includes("not available"), "English safe missing text in the node");
  const missingDetail = renderCase.records.detail.find((view) => view.transactionId === "tx_01");
  assert(missingDetail, "missing record produced a detail view");
  assertEqual(missingDetail.phase, "detail_unavailable", "missing detail phase");
}
