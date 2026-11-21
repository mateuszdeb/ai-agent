/*
 * T-20 scenario: connection loss and state recovery. Start and decision
 * failures never guess success, lost reads retry with a growing delay
 * capped at 10s, a restarted backend (unknown run) stops polling with an
 * honest outcome-unknown message, and decisions without context never
 * send a rewrite in the dark.
 */

const SESSION = { csrf_token: "csrf_20", instance_id: "inst_20", active_run_id: null };

function recordingTimers(tools) {
  const timers = tools.fakeTimers();
  const delays = [];
  const rawSet = timers.set;
  timers.set = (handler, delay) => {
    delays.push(delay);
    return rawSet(handler, delay);
  };
  timers.delays = delays;
  return timers;
}

function makeController(tools, fetch, storage) {
  const timers = recordingTimers(tools);
  const records = { run: [], cards: [] };
  const controller = tools.loadUI({ fetch }).ui.createController({
    fetch,
    storage: storage ?? tools.stubStorage(),
    token: tools.stubToken(),
    timers,
    views: {
      run: (view) => records.run.push(view),
      cards: (view) => records.cards.push(view),
      service() {}
    }
  });
  return { controller, timers, records };
}

export async function run({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage, assert, assertEqual }) {
  const flush = () => new Promise((resolve) => setImmediate(resolve));

  /* --- Pure backoff: 1s, 2s, 4s, 8s, then a 10s cap forever. --- */
  const pure = loadUI().ui;
  assertEqual(pure.nextBackoffDelay(0), 1000, "first retry after 1s");
  assertEqual(pure.nextBackoffDelay(1), 2000, "second retry after 2s");
  assertEqual(pure.nextBackoffDelay(2), 4000, "third retry after 4s");
  assertEqual(pure.nextBackoffDelay(3), 8000, "fourth retry after 8s");
  assertEqual(pure.nextBackoffDelay(4), 10000, "fifth retry at the 10s cap");
  assertEqual(pure.nextBackoffDelay(40), 10000, "the cap holds for any attempt count");
  assertEqual(pure.nextBackoffDelay(-5), 1000, "nonsense attempts fall back to the start");

  /* --- Start response lost: no fabricated success, retry allowed. --- */
  const startLost = stubFetch([{ reject: "network down" }]);
  const startCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, startLost);
  startCase.controller.resume({ ...SESSION });
  await flush();
  assertEqual(startCase.controller.submitGoal("Review my expenses."), true, "first submission goes out");
  await flush();
  const lostView = startCase.records.run[startCase.records.run.length - 1];
  assertEqual(lostView.phase, "start_unreachable", "start failure phase");
  assertEqual(lostView.runId, null, "no run id is fabricated");
  assertEqual(lostView.resultText, null, "no result is invented");
  assert(lostView.errorText.indexOf("Connection lost") !== -1, "English connection message");
  assertEqual(startLost.calls.length, 1, "no automatic resubmission");
  assertEqual(startCase.controller.submitGoal("Review my expenses."), true, "an explicit retry is the user's choice");

  /* --- Decision response lost: acceptance is not claimed; retry allowed. --- */
  const decideLost = stubFetch([
    { status: 202, body: { run_id: "run_20", status: "running", version: 1 } },
    { status: 200, body: { run_id: "run_20", status: "awaiting_approval", version: 2 } },
    {
      status: 200,
      body: {
        approvals: [
          {
            approval_id: "apr_20",
            run_id: "run_20",
            tool_call_id: "call_1",
            kind: "overwrite_category",
            status: "pending",
            action_id: "pending_over_1",
            transaction: {
              transaction_id: "tx_1",
              date: "2026-03-02",
              merchant: "Corner Coffee",
              amount: -4.2,
              currency: "USD",
              category: "Groceries"
            },
            reason: "Flagged overwrite.",
            expires_at: "2026-11-08T12:00:00+00:00",
            previous_category: "Groceries",
            proposed_category: "Dining"
          }
        ]
      }
    },
    { reject: "network down" }
  ]);
  const decideCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, decideLost);
  decideCase.controller.resume({ ...SESSION });
  await flush();
  assertEqual(decideCase.controller.submitGoal("Fix the coffee category."), true, "run goes out");
  await flush();
  assertEqual(decideCase.controller.decide("apr_20", "approve"), true, "decision attempt goes out");
  await flush();
  const unreachable = decideCase.records.cards.find((view) => view.phase === "decision_unreachable");
  assert(unreachable, "a network-lost decision shows the unreachable view");
  assert(unreachable.message.indexOf("Connection lost") !== -1, "English connection message on a lost decision");
  assert(!decideCase.records.cards.some((view) => (view.applied || {}).statusLabel), "no premature acceptance claim");
  assertEqual(decideCase.controller.decide("apr_20", "approve"), true, "a retry decision is the user's choice");

  /* --- Reload in mid-run with a flapping network: backoff and reset. --- */
  const restoreStorage = stubStorage();
  restoreStorage.set("household_expense_run_id", "run_20");
  const flapFetch = stubFetch([
    { status: 200, body: { run_id: "run_20", status: "running", version: 3 } },
    { reject: "net 1" },
    { reject: "net 2" },
    { reject: "net 3" },
    { reject: "net 4" },
    { reject: "net 5" },
    { status: 200, body: { run_id: "run_20", status: "running", version: 4 } },
    { status: 200, body: { run_id: "run_20", status: "running", version: 5 } }
  ]);
  const flapCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, flapFetch, restoreStorage);
  flapCase.controller.resume({ csrf_token: "csrf_new", instance_id: "inst_20", active_run_id: "run_20" });
  await flush();
  assert(flapFetch.calls.every((call) => call.method === "GET"), "recovery uses reads only");
  assertEqual(flapCase.controller.submitGoal("Never while running."), false, "running analysis blocks new submissions");
  assertEqual(flapCase.controller.activeRunId, "run_20", "the known run id stays active");
  // The restore read succeeded; the immediate poll fails five times, then
  // reads succeed again. Drive the clock until the retry pattern completes.
  assertEqual(flapCase.timers.delays[0], 1000, "first retry is scheduled at 1s");
  assertEqual(
    flapCase.records.run.some((view) => view.phase === "retrying"),
    true,
    "a retrying view informs the user once"
  );
  const retryView = flapCase.records.run.find((view) => view.phase === "retrying");
  assertEqual(retryView.resultText, null, "retrying claims no result");
  assert(retryView.errorText.indexOf("Connection lost") !== -1, "English retry note");
  for (let tick = 0; tick < 40 && flapCase.timers.delays.length < 7; tick += 1) {
    await flapCase.timers.advance(10000);
    await flush();
  }
  assertEqual(
    JSON.stringify(flapCase.timers.delays.slice(0, 5)),
    JSON.stringify([1000, 2000, 4000, 8000, 10000]),
    "read retries grow to the 10s cap"
  );
  // A successful read resets the interval to the plain 1s rhythm.
  assertEqual(flapCase.timers.delays[5], 1000, "after recovery the first poll resets to 1s");
  assertEqual(flapCase.timers.delays[6], 1000, "the 1s rhythm holds while reads succeed");
  assert(
    flapFetch.calls.some((call) => call.url === "/api/runs/run_20"),
    "polling continued after the retries"
  );

  /* --- Reload with a again-unreachable backend: reads fail honestly. --- */
  const deadResume = stubFetch([{ reject: "backend down" }]);
  const deadStorage = stubStorage();
  deadStorage.set("household_expense_run_id", "run_20");
  const deadCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, deadResume, deadStorage);
  const storedId = deadCase.controller.resume({
    csrf_token: "csrf_new",
    instance_id: "inst_20",
    active_run_id: null
  });
  assertEqual(storedId, "run_20", "resume still checks the stored id");
  await flush();
  const deadView = deadCase.records.run[deadCase.records.run.length - 1];
  assertEqual(deadView.phase, "resume_unreachable", "failed resume phase");
  assertEqual(deadView.resultText, null, "failed resume claims no result");
  assert(deadView.errorText.indexOf("Connection lost") !== -1, "English message on failed resume");
  assertEqual(deadStorage.get("household_expense_run_id"), "run_20", "the run id stays for a later status check");

  /* --- Backend restart during the run: run gone -> outcome unknown. --- */
  const restartFetch = stubFetch([
    { status: 202, body: { run_id: "run_restart", status: "running", version: 1 } },
    { status: 200, body: { run_id: "run_restart", status: "awaiting_approval", version: 2 } },
    { status: 200, body: { approvals: [APPROVAL] } },
    { status: 404, body: { error: { code: "run_unavailable", message: "Run is not available." } } }
  ]);
  const restartCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, restartFetch);
  restartCase.controller.resume({ ...SESSION });
  await flush();
  assertEqual(restartCase.controller.submitGoal("Delete the flagged charge."), true, "run goes out");
  await flush();
  assert(restartCase.records.cards.some((view) => view.phase === "cards"), "a card was listed before the restart");
  await restartCase.timers.advance(1000);
  await flush();
  const restartRun = restartCase.records.run[restartCase.records.run.length - 1];
  assertEqual(restartRun.phase, "unrecoverable", "an unknown run ends the polling");
  assertEqual(restartRun.resultText, null, "the lost run claims no result");
  assert(restartRun.errorText.indexOf("result is unknown") !== -1, "the message says the outcome is unknown");
  assertEqual(restartCase.controller.activeRunId, null, "polling stopped");
  assertEqual(restartCase.timers.pendingCount, 0, "no timers keep reading a dead run");
  const contextGone = restartCase.records.cards.find((view) => view.phase === "decision_unavailable");
  assert(contextGone, "the losing card context is announced");
  assert(contextGone.message.indexOf("No decision was applied") !== -1, "English context-lost message");
  assertEqual(restartCase.controller.decide("apr_20", "approve"), false, "a stale card cannot be decided");
  assertEqual(
    restartFetch.calls.filter((call) => call.method === "POST" && call.url.includes("/approvals")).length,
    0,
    "no decision rewrite is sent after the restart"
  );

  /* --- Cards polling also backs off on busy reads, without lying. --- */
  const busyFetch = stubFetch([
    { status: 202, body: { run_id: "run_busy", status: "running", version: 1 } },
    { status: 200, body: { run_id: "run_busy", status: "awaiting_approval", version: 2 } },
    { status: 503, body: { error: { code: "state_busy", message: "Transaction state is busy." } } }
  ]);
  const busyCase = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, busyFetch);
  busyCase.controller.resume({ ...SESSION });
  await flush();
  assertEqual(busyCase.controller.submitGoal("Review busy state."), true, "run goes out");
  await flush();
  assert(busyCase.records.cards.some((view) => view.phase === "unavailable"), "a busy approvals read shows unavailability");
  assert(busyCase.records.cards.every((view) => !(view.applied || {}).statusLabel), "a busy read claims no applied decision");
  assert(busyCase.timers.pendingCount >= 1, "cards polling schedules a retry after a busy read");
}

const APPROVAL = {
  approval_id: "apr_20",
  run_id: "run_restart",
  tool_call_id: "call_1",
  kind: "delete_transaction",
  status: "pending",
  action_id: "pending_del_20",
  transaction: {
    transaction_id: "tx_1",
    date: "2026-03-02",
    merchant: "Corner Coffee",
    amount: -4.2,
    currency: "USD",
    category: null
  },
  reason: "Flagged for deletion.",
  expires_at: "2026-11-08T12:00:00+00:00",
  previous_category: null,
  proposed_category: null
};
