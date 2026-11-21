/*
 * T-17 scenario: single-flight submission, immediate acceptance state,
 * 1s polling to a terminal status, reload restore through reads only and
 * unknown-run handling. Drives UI.createController with scripted fetch,
 * manual timers and a recording run-view sink.
 */

function baseRun(id) {
  return {
    run_id: id,
    status: "running",
    version: 1
  };
}

function recordingViews(fetch) {
  const views = [];
  return {
    views,
    run(view) {
      views.push({ ...view, fetchCallsSoFar: fetch.calls.length });
    },
    service() {}
  };
}

function makeController(tools, fetch, storage) {
  const timers = tools.fakeTimers();
  const viewsSink = recordingViews(fetch);
  const controller = tools.loadUI({ fetch }).ui.createController({
    fetch,
    storage,
    token: tools.stubToken(),
    timers,
    views: viewsSink
  });
  return { controller, timers, views: viewsSink.views };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

export async function run({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage, assert, assertEqual }) {
  const session = { csrf_token: "csrf_17", instance_id: "inst_17", active_run_id: null };

  /* --- Empty goal: validation error, no backend call at all. --- */
  const identFetch = stubFetch([]);
  const ident = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, identFetch, stubStorage());
  ident.controller.resume({ ...session });
  assertEqual(ident.controller.submitGoal("   \n"), false, "blank goal must not submit");
  assertEqual(identFetch.calls.length, 0, "empty goal must reach no endpoint");
  const look = ident.views[ident.views.length - 1];
  assertEqual(look.phase, "validation", "blank goal leads to a validation view");
  assertEqual(look.errorText, "Enter a goal containing at least one non-whitespace character.", "English validation text");

  /* --- Happy path: one POST, immediate Running state, advice on 202. --- */
  const postFetch = stubFetch([
    { status: 202, body: { run_id: "run_17", status: "running", version: 1 } },
    { status: 200, body: { ...baseRun("run_17") } },
    { status: 200, body: { ...baseRun("run_17"), version: 2 } },
    {
      status: 200,
      body: {
        run_id: "run_17",
        status: "completed",
        version: 3,
        result: "Two duplicate coffee charges were found."
      }
    }
  ]);
  const storage = stubStorage();
  const happy = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, postFetch, storage);
  happy.controller.resume({ ...session });
  // Bootstrap restore: nothing stored yet -> an idle view, no run GET.
  assertEqual(
    happy.views[happy.views.length - 1].phase,
    "idle",
    "no stored run id shows the idle state"
  );
  assertEqual(happy.controller.submitGoal("Find duplicate coffee charges.\n"), true, "valid goal submits");
  assertEqual(postFetch.calls[0].method, "POST", "submission uses POST");
  assertEqual(postFetch.calls[0].url, "/api/runs", "submission targets /api/runs");
  const sentBody = JSON.parse(postFetch.calls[0].body);
  assert(sentBody.goal.trim().startsWith("Find duplicate"), "goal text forwarded as text");
  assert(sentBody.request_id.length > 0, "submission carries a single-flight request key");
  assertEqual(sentBody.instance_id, "inst_17", "submission carries the current instance id");
  assertEqual(postFetch.calls[0].headers["X-CSRF-Token"], "csrf_17", "submission carries CSRF");

  // Double-click during the in-flight POST: no second request.
  await flush();
  assertEqual(happy.controller.submitGoal("Find duplicate coffee charges."), false, "double-click is swallowed while in flight");
  assertEqual(postFetch.calls.length, 2, "still exactly one POST (then the first poll)");

  // Acceptance: status shown at once (before any further polling call).
  const acceptance = happy.views.find((view) => view.phase === "running");
  assertEqual(acceptance.runId, "run_17", "run id visible right after 202");
  assertEqual(acceptance.fetchCallsSoFar, 1, "accepted view is emitted before a second API call");
  assertEqual(acceptance.resultText, null, "no result text is invented at acceptance");

  // Polling keeps going while running...
  await happy.timers.advance(1000);
  assertEqual(postFetch.calls.length, 3, "second poll happened at 1s");
  assertEqual(postFetch.calls[2].url, "/api/runs/run_17", "polls read the run snapshot");

  // ...and stops at a terminal status with the result and run id, then the
  // list is refreshed to the effective backend state (no further polls).
  await happy.timers.advance(1000);
  await happy.timers.advance(5000);
  assertEqual(postFetch.calls.length, 5, "terminal poll, then the effective-state list refresh");
  assertEqual(postFetch.calls[3].url, "/api/runs/run_17", "terminal snapshot still polls the run");
  assertEqual(postFetch.calls[4].url, "/api/transactions", "run-end refresh reads the effective state");
  assertEqual(happy.timers.pendingCount, 0, "poll timer is cleared at the terminal status");
  const finalView = happy.views[happy.views.length - 1];
  assertEqual(finalView.phase, "terminal", "terminal view phase");
  assertEqual(finalView.statusLabel, "Completed", "Completed label");
  assertEqual(finalView.resultText, "Two duplicate coffee charges were found.", "model answer rendered as text");
  assertEqual(finalView.running, false, "run is no longer claimed as active");

  // Exactly the last run id is stored, nothing else (design §93).
  assertEqual(JSON.stringify(storage.dump()), JSON.stringify({ household_expense_run_id: "run_17" }), "only the run id is in storage");
  assert(storage.writes.every(([, value]) => value === "run_17"), "no secret, goal or CSRF token is stored");

  /* --- 409 conflict: neither success nor a fabricated run. --- */
  const conflictFetch = stubFetch([
    { status: 409, body: { error: { code: "run_in_progress", message: "Another analysis is already in progress." } } }
  ]);
  const conflict = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, conflictFetch, stubStorage());
  conflict.controller.resume({ ...session });
  await flush();
  assertEqual(conflict.controller.submitGoal("Start anyway."), true, "submission attempt is made");
  await flush();
  const conflictView = conflict.views[conflict.views.length - 1];
  assertEqual(conflictView.phase, "start_failed", "conflict maps to start_failed");
  assertEqual(conflictView.runId, null, "no run id is fabricated");
  assert(conflictView.errorText.includes("Another analysis"), "English conflict message");

  /* --- Terminal-failure snapshot: preserved observations, no auto retry. --- */
  const failFetch = stubFetch([
    { status: 202, body: { run_id: "run_fail", status: "running", version: 1 } },
    {
      status: 200,
      body: {
        run_id: "run_fail",
        status: "outcome_unknown",
        version: 8,
        observations: [
          { sequence: 1, step: 2, tool: "assign_category", status: "confirmation_denied", data: null }
        ],
        error: { code: "outcome_unknown", message: "The write outcome could not be confirmed. Check the current transactions; do not repeat the write automatically." }
      }
    }
  ]);
  const failing = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, failFetch, stubStorage());
  failing.controller.resume({ ...session });
  await flush();
  assertEqual(failing.controller.submitGoal("Categorize groceries."), true, "failure-path submission goes out");
  await flush();
  await failing.timers.advance(1000);
  await failing.timers.advance(5000);
  assertEqual(
    failFetch.calls.filter((call) => call.method === "POST").length,
    1,
    "no automatic resubmission after a failed write"
  );
  assertEqual(failFetch.calls.length, 3, "terminal snapshot then the effective-state refresh only");
  const failView = failing.views[failing.views.length - 1];
  assertEqual(failView.statusLabel, "Outcome unknown", "Outcome unknown label reaches the view");
  assertEqual(failView.observations.length, 1, "available observations are preserved");
  assertEqual(failView.observations[0].status, "confirmation_denied", "controller status stays in the view");


  /* --- Reload: restore through reads; never resend the goal. --- */
  const restoreStorage = stubStorage();
  restoreStorage.set("household_expense_run_id", "run_17");
  const reloadFetch = stubFetch([
    { status: 200, body: { csrf_token: "csrf_new", instance_id: "inst_17", active_run_id: "run_17" } },
    { status: 200, body: { ...baseRun("run_17"), version: 4 } },
    { status: 200, body: { ...baseRun("run_17"), version: 5 } },
    { status: 200, body: { ...baseRun("run_17"), version: 6 } }
  ]);
  const reloader = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, reloadFetch, restoreStorage);
  reloader.controller.resume({
    csrf_token: "csrf_new",
    instance_id: "inst_17",
    active_run_id: "run_17"
  });
  await flush();
  assertEqual(reloader.controller.submitGoal("Resubmitted goal should never run."), false, "restored running analysis blocks new submissions");
  assertEqual(reloadFetch.calls[1].url, "/api/runs/run_17", "reload reads the known run");
  assert(reloadFetch.calls.every((call) => call.method === "GET"), "reload performs no writes");
  await reloader.timers.advance(1000);
  await reloader.timers.advance(1000);
  assertEqual(reloadFetch.calls.length, 4, "restored run resumes 1s polling");

  /* --- Reload with an unknown stored run: never treated as success. --- */
  const lostFetch = stubFetch([
    { status: 404, body: { error: { code: "run_unavailable", message: "Run is not available." } } }
  ]);
  const lostStorage = stubStorage();
  lostStorage.set("household_expense_run_id", "run_history");
  const lost = makeController({ loadUI, stubFetch, stubToken, fakeTimers, stubStorage }, lostFetch, lostStorage);
  const resumedId = lost.controller.resume({
    csrf_token: "csrf_new",
    instance_id: "inst_17",
    active_run_id: null
  });
  assertEqual(resumedId, "run_history", "resume attempts the stored run id");
  await flush();
  const lostView = lost.views[lost.views.length - 1];
  assertEqual(lostView.phase, "unrecoverable", "unknown run yields the unrecoverable phase");
  assertEqual(lostView.resultText, null, "unknown run never claims a result");
  assert(lostView.errorText.includes("could not be recovered"), "English recovery hint");
  assertEqual(lostStorage.get("household_expense_run_id"), null, "unrecoverable run id is forgotten");
}
