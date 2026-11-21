/*
 * T-18 scenario: English decision cards, three-button one-time decisions,
 * expired/stale resolution paths, and the rule that an accepted decision is
 * never displayed as a completed write before the controller result.
 */

const SESSION = { csrf_token: "csrf_18", instance_id: "inst_18", active_run_id: null };

const CARD = {
  approval_id: "apr_18",
  run_id: "run_18",
  tool_call_id: "call_1",
  kind: "delete_transaction",
  status: "pending",
  action_id: "pending_del_tx_1",
  transaction: {
    transaction_id: "tx_1",
    date: "2026-03-03",
    merchant: "Corner Coffee <script>",
    amount: -4.2,
    currency: "USD",
    category: null
  },
  reason: "Flagged for deletion by the agent run.",
  expires_at: "2026-11-08T12:00:00+00:00",
  previous_category: null,
  proposed_category: null
};

const OVERWRITE_CARD = {
  ...CARD,
  approval_id: "apr_ow",
  tool_call_id: "call_2",
  kind: "overwrite_category",
  previous_category: "Groceries",
  proposed_category: "Dining"
};

function makeController(tools, fetch) {
  const timers = tools.fakeTimers();
  const records = { run: [], cards: [] };
  const controller = tools.loadUI({ fetch }).ui.createController({
    fetch,
    storage: tools.stubStorage(),
    token: tools.stubToken(),
    timers,
    views: {
      run: (view) => records.run.push(view),
      cards: (view) => records.cards.push({ ...view, atCalls: fetch.calls.length }),
      service() {}
    }
  });
  return { controller, timers, records };
}

export async function run({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage, assert, assertEqual }) {
  const flush = () => new Promise((resolve) => setImmediate(resolve));

  /* --- Pure card view: allowlisted fields, English labels, no HTML. --- */
  const load = loadUI();
  const card = load.ui.approvalCardView(CARD);
  assertEqual(card.kindLabel, "Delete this transaction", "kind label");
  assertEqual(
    card.question,
    "Delete this transaction? Review the details before confirming.",
    "delete question from catalog"
  );
  assertEqual(card.confirmLabel, "Confirm deletion", "delete confirm label");
  assertEqual(card.rejectLabel, "Reject", "reject label");
  assertEqual(card.statusLabel, "Waiting for your decision", "pending status label");
  assert(card.facts.includes("-4.2 USD"), "amount keeps its currency");
  assert(card.facts.some((fact) => fact.includes("Uncategorized")), "missing category is Uncategorized");
  assertEqual(card.pendingActionNote, "This action has not been performed.", "pending-action note present");
  assert(!JSON.stringify(card).includes("fingerprint"), "fingerprint never leaks to cards");

  const overwrite = load.ui.approvalCardView(OVERWRITE_CARD);
  assertEqual(overwrite.confirmLabel, "Confirm overwrite", "overwrite confirm label");
  assertEqual(
    overwrite.question,
    "Overwrite this transaction's category? Review both values before confirming.",
    "overwrite question"
  );
  assertEqual(overwrite.previousCategory, "Groceries", "previous category surfaced");
  assertEqual(overwrite.proposedCategory, "Dining", "proposed category surfaced");

  const unknownKind = load.ui.approvalCardView({
    ...CARD,
    approval_id: "apr_x",
    kind: "rocket_launch"
  });
  assertEqual(unknownKind.confirmLabel, "Confirm overwrite", "unknown kind falls back safely");
  assert(unknownKind.question.startsWith("Review this proposed action."), "unknown kind uses a safe question");

  /* --- Flow: awaiting approval -> card poll -> approve decision. --- */
  const fetch = stubFetch([
    { status: 202, body: { run_id: "run_18", status: "running", version: 1 } },
    { status: 200, body: { run_id: "run_18", status: "awaiting_approval", version: 2 } },
    { status: 200, body: { approvals: [CARD, OVERWRITE_CARD] } },
    { status: 200, body: { approval_id: "apr_18", run_id: "run_18", status: "approved", result: null } },
    { status: 200, body: { run_id: "run_18", status: "completed", version: 9, result: "Deletion was carried out." } }
  ]);
  const flow = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, fetch);
  flow.controller.resume({ ...SESSION, active_run_id: null });
  await flush();
  assertEqual(flow.controller.submitGoal("Delete the flagged coffee charge."), true, "run goes out");
  await flush();
  // Snapshot switched to awaiting_approval -> card polling starts at once.
  const cardViews = flow.records.cards;
  assertEqual(cardViews[0].phase, "cards", "card list arrives");
  assertEqual(cardViews[0].cards.length, 2, "both decisions are listed, not batch approved");

  // The confirmation POST carries the strict payload.
  assertEqual(flow.controller.decide("apr_18", "approve"), true, "decision accepted for sending");
  assertEqual(fetch.calls[3].method, "POST", "decision uses POST");
  assert(
    fetch.calls[3].url.endsWith("/api/runs/run_18/approvals/apr_18"),
    "decision targets exactly one approval"
  );
  const sent = JSON.parse(fetch.calls[3].body);
  assertEqual(sent.decision, "approve", "approve decision value");
  assertEqual(sent.instance_id, "inst_18", "decision carries the instance id");
  assertEqual(Object.keys(sent).length, 2, "decision payload is strict: no extra fields");

  // Double-click: the second decide on a card in flight is swallowed.
  assertEqual(flow.controller.decide("apr_18", "approve"), false, "double-click does not resend a decision");
  assertEqual(
    fetch.calls.filter((call) => call.method === "POST" && call.url.includes("/approvals")).length,
    1,
    "exactly one decision POST"
  );

  await flush();
  const accepted = cardViews.find((view) => view.phase === "decision_accepted");
  assert(accepted, "an accepted-decision view is emitted");
  assertEqual(accepted.applied.statusLabel, "Approval received; execution is not yet confirmed", "acceptance is not a completed write");
  assert(accepted.applied.statusLabel.indexOf("execution is not yet confirmed") !== -1, "explicitly non-confirmed wording");
  // While awaiting the controller result nothing claims success.
  assert(!cardViews.some((view) => (view.applied || {}).statusLabel === "Completed" && !accepted), "no premature completion claim");

  // Run completes afterwards; card list stops and result text flows in.
  // Run and card polling both stay alive while awaiting approval.
  assertEqual(flow.timers.pendingCount, 2, "run and cards polling remain while awaiting approval");
  await flow.timers.advance(1000);
  await flush();
  const lastRun = flow.records.run[flow.records.run.length - 1];
  assertEqual(lastRun.phase, "terminal", "run moved to terminal");
  assertEqual(lastRun.resultText, "Deletion was carried out.", "the result shows only after the snapshot says so");

  /* --- Decision window expired: backend echoes the stored safe state. --- */
  const expiredFetch = stubFetch([
    { status: 202, body: { run_id: "run_exp", status: "running", version: 1 } },
    { status: 200, body: { run_id: "run_exp", status: "awaiting_approval", version: 2 } },
    { status: 200, body: { approvals: [{ ...CARD, status: "expired" }] } },
    { status: 200, body: { approval_id: "apr_18", run_id: "run_exp", status: "expired", result: null } },
    { status: 200, body: { run_id: "run_exp", status: "running", version: 4 } }
  ]);
  const expired = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, expiredFetch);
  expired.controller.resume({ ...SESSION, active_run_id: null });
  await flush();
  assertEqual(expired.controller.submitGoal("Delete the flagged coffee charge."), true, "expired-path submission");
  await flush();
  assert(expiredFetch.calls.some((call) => call.url.endsWith("/approvals")), "approvals polling happened");
  // A resolved card is rendered, but acting on it is a client-side no-op.
  const shownCard = expired
    .records
    .cards
    .flatMap((entry) => entry.cards || [])
    .find((card) => card.approvalId === "apr_18");
  assert(shownCard, "the expired card is still surfaced");
  assertEqual(shownCard.statusLabel, "Expired", "expired status wording renders");
  assertEqual(expired.controller.decide("apr_never", "approve"), false, "unknown approval decides nothing");
  assertEqual(expired.controller.decide("apr_18", "approve"), false, "an expired card cannot be acted on");
  assertEqual(
    expiredFetch.calls.filter((call) => call.method === "POST" && call.url.includes("/approvals")).length,
    0,
    "no decision request is sent for an unlisted card"
  );

  /* --- Late but listed decision path: backend echoes expired safely. --- */
  const lateFetch = stubFetch([
    { status: 202, body: { run_id: "run_late", status: "running", version: 1 } },
    { status: 200, body: { run_id: "run_late", status: "awaiting_approval", version: 2 } },
    { status: 200, body: { approvals: [{ ...CARD }] } },
    { status: 200, body: { approval_id: "apr_18", run_id: "run_late", status: "expired", result: null } },
    { status: 200, body: { run_id: "run_late", status: "running", version: 4 } }
  ]);
  const late = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, lateFetch);
  late.controller.resume({ ...SESSION, active_run_id: null });
  await flush();
  assertEqual(late.controller.submitGoal("Delete the flagged coffee charge."), true, "late-path submission");
  await flush();
  assertEqual(late.controller.decide("apr_18", "approve"), true, "listed pending card can be decided");
  await flush();
  const lateAccepted = late
    .records
    .cards
    .find((entry) => entry.phase === "decision_accepted");
  assert(lateAccepted, "late decision reaches the acceptance-free status read");
  assertEqual(lateAccepted.applied.statusLabel, "Expired", "backend echoes expired, not success");
  const lateRuns = late.records.run;
  const lateSnapshot = lateRuns[lateRuns.length - 1];
  assert(
    ["running", "awaiting_approval", "terminal", "idle"].includes(lateSnapshot.phase),
    "run continues safely"
  );
  /* --- Wrong-session decision: safe identical behavior, no success. --- */
  const foreignFetch = stubFetch([
    { status: 202, body: { run_id: "run_f", status: "running", version: 1 } },
    { status: 200, body: { run_id: "run_f", status: "awaiting_approval", version: 2 } },
    { status: 200, body: { approvals: [CARD] } },
    { status: 404, body: { error: { code: "approval_unavailable", message: "The requested resource is unavailable." } } }
  ]);
  const foreign = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, foreignFetch);
  foreign.controller.resume({ ...SESSION, active_run_id: null });
  await flush();
  assertEqual(foreign.controller.submitGoal("Try a foreign decision."), true, "foreign-path submission");
  await flush();
  const decided = foreign.controller.decide("apr_18", "approve");
  assertEqual(decided, true, "decision attempt is sent (backend decides)");
  await flush();
  const unavailable = foreign.records.cards.find((view) => view.phase === "decision_unavailable");
  assert(unavailable, "a 404 decision yields the decision_unavailable view");
  assert(
    unavailable.message.includes("no longer valid") || unavailable.message.includes("could not be applied"),
    "English safe message on a failed decision"
  );
  assert(!JSON.stringify(unavailable).includes("fingerprint"), "no fingerprint leak on decisions");
}
