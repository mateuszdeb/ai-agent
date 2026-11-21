/*
 * Household Expense Checker — same-origin static UI.
 *
 * Catalog rule (NFR-04): transaction data and model text are untrusted, so
 * they are rendered exclusively as text nodes via `textContent`; the renderer
 * never assigns to innerHTML, outerHTML, insertAdjacentHTML or document.write,
 * and never evaluates data-derived strings. All storage keeps only the last
 * run identifier (design §93).
 */
(function () {
  "use strict";

  var root = typeof window !== "undefined" ? window : globalThis;

  /* English labels and messages mirror the backend presentation catalog. */
  var TEXT = {
    connected: "Connected to the local service.",
    serviceUnavailable: (
      "Service is unreachable. Refresh the page to try again."
    ),
    noAnalysisYet: "No analysis yet",
    runIdFallBack: "–",
    connectionLost: (
      "Connection lost. Check the run status before starting another analysis."
    ),
    obsHeading: "Observed actions",
    emptyGoal: "Enter a goal containing at least one non-whitespace character.",
    goalTooLong: "Use no more than 8000 characters.",
    uncategorized: "Uncategorized",
    categoryLabel: "Category",
    noResults: "No transactions found.",
    truncatedResults: "Showing up to {limit} results. Refine your search.",
    detailsToggle: "Details",
    detailHeading: "Details",
    description: "Description",
    previousCategory: "Previous category",
    proposedCategory: "Proposed category",
    reason: "Reason",
    expiresAt: "Expires at",
    pendingAction: "This action has not been performed.",
    reject: "Reject",
    confirmDeletion: "Confirm deletion",
    confirmOverwrite: "Confirm overwrite",
    tool: "Tool",
    actionId: "Action ID"
  };

  var MAX_GOAL_LENGTH = 8000;

  var RUN_LABELS = {
    running: "Running",
    awaiting_approval: "Waiting for your decision",
    completed: "Completed",
    step_limit_reached: "Step limit reached",
    failed: "Failed",
    outcome_unknown: "Outcome unknown",
    error: "Failed",
    start_failed: "Failed"
  };

  var OBSERVATION_TOOLS = {
    search_transactions: "Search transactions",
    assign_category: "Assign category",
    delete_flagged_transaction: "Delete flagged transaction",
    unknown_tool: "Unknown tool"
  };

  /* Public module surface; scenario tests drive exactly these functions. */
  var UI = {
    TEXT: TEXT,
    MAX_GOAL_LENGTH: MAX_GOAL_LENGTH,
    RUN_LABELS: RUN_LABELS,
    OBSERVATION_TOOLS: OBSERVATION_TOOLS
  };

  /* ------------------------------------------------------------------ *
   * Pure helpers (also exercised by the Node scenario harness).         *
   * ------------------------------------------------------------------ */

  function validateGoal(raw) {
    var goal = typeof raw === "string" ? raw : "";
    if (!/\S/.test(goal)) {
      return { ok: false, message: TEXT.emptyGoal };
    }
    if (goal.length > MAX_GOAL_LENGTH) {
      return { ok: false, message: TEXT.goalTooLong };
    }
    return { ok: true, goal: goal };
  }
  UI.validateGoal = validateGoal;

  function runStatusLabel(status) {
    return Object.prototype.hasOwnProperty.call(RUN_LABELS, status)
      ? RUN_LABELS[status]
      : "Unknown run status";
  }
  UI.runStatusLabel = runStatusLabel;

  function observationToolLabel(tool) {
    return Object.prototype.hasOwnProperty.call(OBSERVATION_TOOLS, tool)
      ? OBSERVATION_TOOLS[tool]
      : tool;
  }
  UI.observationToolLabel = observationToolLabel;

  function observationView(obs) {
    /* One English line per real controller observation; never model text. */
    var parts = [
      observationToolLabel(obs.tool),
      " — ",
      String(obs.status),
      obs.action_id ? " (action " + obs.action_id + ")" : ""
    ];
    return {
      text: parts.join(""),
      className: "status-" + obs.status,
      domId: "obs-" + String(obs.sequence)
    };
  }
  UI.observationView = observationView;

  function transactionRow(tx) {
    var category = tx.category === null || tx.category === undefined || tx.category === ""
      ? TEXT.uncategorized
      : tx.category;
    return {
      cells: [
        tx.transaction_id,
        tx.date,
        tx.merchant,
        tx.amount + " " + tx.currency,
        category
      ],
      transactionId: tx.transaction_id
    };
  }
  UI.transactionRow = transactionRow;

  function detailView(tx) {
    /* Allowlisted detail fields only; all strings render as text. */
    var lines = [];
    if (tx.description) {
      lines.push(TEXT.description + ": " + tx.description);
    }
    lines.push(
      (tx.category === null || tx.category === undefined || tx.category === "")
        ? TEXT.uncategorized
        : (TEXT.categoryLabel + ": " + tx.category)
    );
    return { lines: lines };
  }
  UI.detailView = detailView;

  /* ------------------------------------------------------------------ *
   * Transactions (T-19): the listed effective state is the source of    *
   * truth; filters produce explicit query strings and the truncation    *
   * hint never invents a total. No optimistic changes are rendered.     *
   * ------------------------------------------------------------------ */

  function transactionFiltersQuery(rawQuery, rawMerchant) {
    var params = [];
    var query = (rawQuery || "").trim();
    var merchant = (rawMerchant || "").trim();
    if (query) {
      params.push("query=" + encodeURIComponent(query));
    }
    if (merchant) {
      params.push("merchant=" + encodeURIComponent(merchant));
    }
    return params.join("&");
  }
  UI.transactionFiltersQuery = transactionFiltersQuery;

  function truncatedNote(count, limit, possiblyTruncated) {
    if (count === 0) {
      return TEXT.noResults;
    }
    return possiblyTruncated
      ? TEXT.truncatedResults.replace("{limit}", String(limit))
      : "Showing " + String(count) + (count === 1 ? " result." : " results.");
  }
  UI.truncatedNote = truncatedNote;

  /* Read-only retries (T-20): backoff starts at 1s and is capped at 10s. */
  var READ_BACKOFF_START_MS = 1000;
  var READ_BACKOFF_MAX_MS = 10000;
  UI.READ_BACKOFF_START_MS = READ_BACKOFF_START_MS;
  UI.READ_BACKOFF_MAX_MS = READ_BACKOFF_MAX_MS;

  function nextBackoffDelay(failedAttempts) {
    var attempt = Math.max(0, Number(failedAttempts) || 0);
    var delay = READ_BACKOFF_START_MS;
    var step = 0;
    while (step < attempt && delay < READ_BACKOFF_MAX_MS) {
      delay = delay * 2;
      step += 1;
    }
    return Math.min(delay, READ_BACKOFF_MAX_MS);
  }
  UI.nextBackoffDelay = nextBackoffDelay;

  /* ------------------------------------------------------------------ *
   * Renderer: builds DOM with createElement + textContent only, so     *
   * transaction data and model text can never become active content.   *
   * ------------------------------------------------------------------ */

  function h(tag, attrs, textContent) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (name) {
        var value = attrs[name];
        if (name === "class") {
          node.className = value;
        } else if (name === "text") {
          node.textContent = String(value);
        } else if (value !== null && value !== undefined) {
          node.setAttribute(name, String(value));
        }
      });
    }
    if (textContent !== undefined && textContent !== null) {
      node.textContent = String(textContent);
    }
    return node;
  }
  UI.h = h;

  function byId(id) {
    return document.getElementById(id);
  }

  function renderObservations(observations) {
    var list = byId("observations-list");
    if (list) {
      /* listChildren replaces the node children; used by tests in Node. */
      listChildren(
        list,
        (observations || []).map(function (obs) {
          var view = observationView(obs);
          var node = buildText("li", view.text, view.className, view.domId);
          return node;
        })
      );
    }
  }

  function buildText(tag, text, className, domId) {
    var node = h(tag, { id: domId !== undefined ? domId : null }, text);
    if (className) {
      node.className = className;
    }
    return node;
  }

  function listChildren(node, children) {
    /* Replacement keeps the container; safe against future innerHTML use. */
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
    children.forEach(function (child) {
      node.appendChild(child);
    });
    return node;
  }
  UI.listChildren = listChildren;

  function withChildren(node, children) {
    var hadChildren = false;
    children.forEach(function (child) {
      if (child !== null && child !== undefined) {
        node.appendChild(child);
        hadChildren = true;
      }
    });
    return hadChildren;
  }

  /* Placeholder API: recovery wires later. */
  UI.todo = {
    controllerRecovery: "T-20 wires connection recovery."
  };

  /* English decision catalog mirrors backend APPROVAL_LABELS and the
   * CONFIRMATION_QUESTIONS set (presentation.py); no global approve. */
  var APPROVAL_LABELS = {
    pending: "Waiting for your decision",
    approved: "Approval received; execution is not yet confirmed",
    rejected: "Rejected",
    expired: "Expired",
    stale: "Transaction changed; a new decision is required",
    executing: "Executing",
    completed: "Decision processed; check the tool result",
    failed: "Failed",
    outcome_unknown: "Outcome unknown"
  };
  var CONFIRMATION_QUESTIONS = {
    delete_transaction:
      "Delete this transaction? Review the details before confirming.",
    overwrite_category:
      "Overwrite this transaction's category? Review both values before confirming."
  };
  var APPROVAL_KINDS = {
    delete_transaction: "Delete this transaction",
    overwrite_category: "Overwrite this transaction"
  };
  UI.APPROVAL_LABELS = APPROVAL_LABELS;
  UI.CONFIRMATION_QUESTIONS = CONFIRMATION_QUESTIONS;
  UI.APPROVAL_KINDS = APPROVAL_KINDS;

  function approvalStatusLabel(status) {
    return Object.prototype.hasOwnProperty.call(APPROVAL_LABELS, status)
      ? APPROVAL_LABELS[status]
      : "Unknown decision status";
  }
  UI.approvalStatusLabel = approvalStatusLabel;

  function transactionFacts(tx) {
    return [
      String(tx.transaction_id),
      tx.date + " — " + tx.merchant,
      tx.amount + " " + tx.currency,
      tx.category === null || tx.category === undefined || tx.category === ""
        ? TEXT.uncategorized
        : TEXT.categoryLabel + ": " + tx.category
    ];
  }
  UI.transactionFacts = transactionFacts;

  function approvalCardView(card) {
    /* Allowlisted card fields only; no owner, fingerprint or arguments. */
    var kind = card.kind;
    var view = {
      approvalId: card.approval_id,
      kind: kind,
      status: card.status,
      statusLabel: approvalStatusLabel(card.status),
      question:
        Object.prototype.hasOwnProperty.call(CONFIRMATION_QUESTIONS, kind)
          ? CONFIRMATION_QUESTIONS[kind]
          : "Review this proposed action.",
      kindLabel: Object.prototype.hasOwnProperty.call(APPROVAL_KINDS, kind)
        ? APPROVAL_KINDS[kind]
        : "Unknown action",
      facts: transactionFacts(card.transaction),
      reason: card.reason,
      expiresLabel: "Decision window ends: " + String(card.expires_at),
      previousCategory: card.previous_category || null,
      proposedCategory: card.proposed_category || null,
      confirmLabel:
        kind === "delete_transaction"
          ? TEXT.confirmDeletion
          : TEXT.confirmOverwrite,
      rejectLabel: TEXT.reject,
      pendingActionNote: TEXT.pendingAction
    };
    return view;
  }
  UI.approvalCardView = approvalCardView;

  /* ------------------------------------------------------------------ *
   * Controller (T-17): submission with a single-flight guard, one      *
   * pending POST at a time, immediate working state on 202 and 1s      *
   * polling until a terminal status. Only the run id is stored.        *
   * ------------------------------------------------------------------ */

  var TERMINAL_RUN_STATUSES = [
    "completed",
    "step_limit_reached",
    "failed",
    "outcome_unknown"
  ];
  UI.TERMINAL_RUN_STATUSES = TERMINAL_RUN_STATUSES;

  function isTerminalRun(status) {
    return TERMINAL_RUN_STATUSES.indexOf(status) !== -1;
  }
  UI.isTerminalRun = isTerminalRun;

  var STORAGE_RUN_KEY = "household_expense_run_id";

  function emptyRunView() {
    return {
      phase: "idle",
      runId: null,
      statusLabel: TEXT.noAnalysisYet,
      running: false,
      resultText: null,
      observations: [],
      errorText: null
    };
  }
  UI.emptyRunView = emptyRunView;

  function effectiveRunText(snapshot) {
    /* The presence of `result` matters: absent means no model text yet. */
    return Object.prototype.hasOwnProperty.call(snapshot, "result")
      ? snapshot.result
      : null;
  }

  function snapshotErrorText(snapshot) {
    if (!snapshot.error) {
      return null;
    }
    return snapshot.error.message || String(snapshot.error.code);
  }

  function runViewFromSnapshot(snapshot) {
    return {
      phase: isTerminalRun(snapshot.status) ? "terminal" : snapshot.status,
      runId: snapshot.run_id,
      statusLabel: runStatusLabel(snapshot.status),
      running: snapshot.status === "running" || snapshot.status === "awaiting_approval",
      resultText: effectiveRunText(snapshot),
      observations: snapshot.observations || [],
      errorText: snapshotErrorText(snapshot)
    };
  }
  UI.runViewFromSnapshot = runViewFromSnapshot;

  function createController(deps) {
    /* deps: {fetch, storage, token, timers, views: {run, cards, service}} */
    var timers = deps.timers;
    var views = deps.views;
    var session = null;
    var activeRunId = null;
    var submitInFlight = false;
    var pollHandle = null;
    var cardsPollHandle = null;
    var knownCards = {}; /* approval_id -> latest card view */
    var cardsRunId = null; /* the run whose decisions are being polled */
    var decidedInFlight = {}; /* approval_id -> true while the POST runs */
    var disposed = false;
    /* Read-retry state (T-20): consecutive failures reset on any 200. */
    var pollAttempts = 0;
    var cardsAttempts = 0;
    var lastRunStatusLabel = null;

    function emitRun(view) {
      if (views.run) {
        views.run(view);
      }
    }

    function emitCards(view) {
      if (views.cards) {
        views.cards(view);
      }
    }

    function stopPolling() {
      if (pollHandle !== null) {
        timers.clear(pollHandle);
        pollHandle = null;
      }
    }

    function stopCardsPolling() {
      if (cardsPollHandle !== null) {
        timers.clear(cardsPollHandle);
        cardsPollHandle = null;
        cardsRunId = null;
      }
    }

    function scheduleNextCardsPoll(runId, delayMs) {
      if (disposed || cardsPollHandle !== null || runId !== activeRunId) {
        return;
      }
      cardsPollHandle = timers.set(function () {
        cardsPollHandle = null;
        pollCards(runId);
      }, delayMs);
    }

    function pollCards(runId) {
      deps
        .fetch(
          "/api/runs/" + encodeURIComponent(runId) + "/approvals",
          { method: "GET", credentials: "same-origin" }
        )
        .then(function (response) {
          return response.json().then(function (body) {
            return { ok: response.ok, status: response.status, body: body };
          });
        })
        .then(function (outcome) {
          if (disposed || runId !== activeRunId) {
            return;
          }
          if (!outcome.ok || outcome.status !== 200) {
            if (outcome.status === 404 || outcome.status === 410) {
              /* Decision context is gone: stop polling and say so. */
              stopCardsPolling();
              emitCards({
                phase: "decision_unavailable",
                runId: null,
                cards: [],
                message:
                  "The decision context is no longer available. No decision was applied."
              });
              return;
            }
            emitCards({ phase: "unavailable", runId: runId, cards: [] });
            scheduleNextCardsPoll(runId, nextBackoffDelay(cardsAttempts));
            cardsAttempts += 1;
            return;
          }
          cardsAttempts = 0;
          var listed = (outcome.body.approvals || []).map(function (card) {
            var view = approvalCardView(card);
            if (knownCards[view.approvalId]) {
              /* Preserve already-applied decisions over card churn. */
              view.status = knownCards[view.approvalId].status;
              view.statusLabel = knownCards[view.approvalId].statusLabel;
              view.buttonsLocked = knownCards[view.approvalId].buttonsLocked;
            }
            knownCards[view.approvalId] = view;
            return view;
          });
          emitCards({ phase: "cards", runId: runId, cards: listed });
          if (listed.length > 0) {
            scheduleNextCardsPoll(runId, READ_BACKOFF_START_MS);
          } else {
            stopCardsPolling();
          }
        })
        .catch(function () {
          if (!disposed && runId === activeRunId) {
            scheduleNextCardsPoll(runId, nextBackoffDelay(cardsAttempts));
            cardsAttempts += 1;
          }
        });
    }

    function startCardsPolling(runId) {
      if (cardsRunId === runId && (cardsPollHandle !== null || decidedInFlightMaybePending())) {
        return;
      }
      stopCardsPolling();
      cardsAttempts = 0;
      cardsRunId = runId;
      pollCards(runId);
    }

    function decidedInFlightMaybePending() {
      return Object.keys(decidedInFlight).length > 0;
    }

    function coordinateCards(runView) {
      if (runView.phase === "awaiting_approval" && runView.runId) {
        startCardsPolling(runView.runId);
      } else {
        stopCardsPolling();
      }
    }

    function scheduleNextPoll(runId, delayMs) {
      if (disposed || pollHandle !== null) {
        return;
      }
      pollHandle = timers.set(function () {
        pollHandle = null;
        pollRun(runId);
      }, delayMs);
    }

    function pollRun(runId) {
      deps
        .fetch("/api/runs/" + encodeURIComponent(runId), {
          method: "GET",
          credentials: "same-origin"
        })
        .then(function (response) {
          return response.json().then(function (body) {
            return { ok: response.ok, status: response.status, body: body };
          });
        })
        .then(function (outcome) {
          if (disposed || runId !== activeRunId) {
            return;
          }
          if (!outcome.ok || outcome.status !== 200) {
            if (outcome.status === 404 || outcome.status === 410) {
              /* The run is definitively gone (backend restart, lost
               * store): never guess the write result; stop reading. */
              activeRunId = null;
              stopPolling();
              stopCardsPolling();
              knownCards = {};
              deps.storage.remove(STORAGE_RUN_KEY);
              emitRun({
                phase: "unrecoverable",
                runId: runId,
                statusLabel: runStatusLabel("error"),
                running: false,
                resultText: null,
                observations: [],
                errorText:
                  "This run is no longer available. Its result is unknown; check the current transactions before starting a new analysis."
              });
              emitCards({
                phase: "decision_unavailable",
                runId: null,
                cards: [],
                message:
                  "The decision context is no longer available. No decision was applied."
              });
              return;
            }
            /* Minor/unreadable state: retry reads only, with backoff. */
            if (pollAttempts === 0) {
              emitRun({
                phase: "retrying",
                runId: runId,
                statusLabel: lastRunStatusLabel,
                running: isRunning(),
                resultText: null,
                observations: [],
                errorText: TEXT.connectionLost
              });
            }
            scheduleNextPoll(runId, nextBackoffDelay(pollAttempts));
            pollAttempts += 1;
            return;
          }
          var view = runViewFromSnapshot(outcome.body);
          pollAttempts = 0;
          lastRunStatusLabel = view.statusLabel;
          emitRun(view);
          coordinateCards(view);
          if (view.phase === "terminal") {
            activeRunId = null;
            stopPolling();
            /* The run is over: the list must show the effective backend
             * state, not optimistic changes. */
            controller.refreshTransactions();
            return;
          }
          scheduleNextPoll(runId, READ_BACKOFF_START_MS);
        })
        .catch(function () {
          /* Network loss: retry reads only, growing up to the 10s cap. */
          if (!disposed && runId === activeRunId) {
            if (pollAttempts === 0) {
              emitRun({
                phase: "retrying",
                runId: runId,
                statusLabel: lastRunStatusLabel,
                running: isRunning(),
                resultText: null,
                observations: [],
                errorText: TEXT.connectionLost
              });
            }
            scheduleNextPoll(runId, nextBackoffDelay(pollAttempts));
            pollAttempts += 1;
          }
        });
    }

    function startPolling(runId) {
      stopPolling();
      activeRunId = runId;
      pollRun(runId);
    }

    var controller = {
      get session() {
        return session;
      },
      get activeRunId() {
        return activeRunId;
      },
      resume: function (sessionPayload) {
        /* Reload path: restore only through reads, never resend the goal. */
        session = sessionPayload;
        var storedRunId = deps.storage.get(STORAGE_RUN_KEY);
        var backendRunId = sessionPayload && sessionPayload.active_run_id;
        var runId = backendRunId || storedRunId;
        if (!runId) {
          emitRun(emptyRunView());
          return null;
        }
        deps.storage.set(STORAGE_RUN_KEY, runId);
        deps
          .fetch("/api/runs/" + encodeURIComponent(runId), {
            method: "GET",
            credentials: "same-origin"
          })
          .then(function (response) {
            return response.json().then(function (body) {
              return { ok: response.ok, status: response.status, body: body };
            });
          })
          .then(function (outcome) {
            if (disposed) {
              return;
            }
            if (outcome.ok && outcome.status === 200) {
              var view = runViewFromSnapshot(outcome.body);
              emitRun(view);
              coordinateCards(view);
              if (view.phase !== "terminal") {
                startPolling(runId);
              } else {
                activeRunId = null;
              }
              return;
            }
            // Unknown or foreign run: never treat as success; forget the id.
            deps.storage.remove(STORAGE_RUN_KEY);
            emitRun({
              phase: "unrecoverable",
              runId: runId,
              statusLabel: runStatusLabel("error"),
              running: false,
              resultText: null,
              observations: [],
              errorText:
                "This run could not be recovered. Check the current transactions before starting a new analysis."
            });
          })
          .catch(function () {
            if (!disposed) {
              /* Reload read failed for an unknown reason; do not invent a
               * clean slate. The stored id stays so the user can check the
               * run status again. */
              emitRun({
                phase: "resume_unreachable",
                runId: runId || null,
                statusLabel: null,
                running: false,
                resultText: null,
                observations: [],
                errorText: TEXT.connectionLost
              });
            }
          });
        return runId;
      },
      submitGoal: function (raw) {
        var validation = validateGoal(raw);
        if (!validation.ok) {
          emitRun({
            phase: "validation",
            runId: activeRunId,
            statusLabel: null,
            running: isRunning(),
            resultText: null,
            observations: [],
            errorText: validation.message
          });
          return false;
        }
        if (submitInFlight || isRunning()) {
          /* Double-click or repeat while active: exactly one submission. */
          return false;
        }
        submitInFlight = true;
        var requestKey = deps.token ? deps.token("req") : "req_default";
        deps
          .fetch("/api/runs", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": session.csrf_token
            },
            body: JSON.stringify({
              goal: validation.goal,
              request_id: requestKey,
              instance_id: session.instance_id
            })
          })
          .then(function (response) {
            return response.json().then(function (body) {
              return { ok: response.ok, status: response.status, body: body };
            });
          })
          .then(function (outcome) {
            submitInFlight = false;
            if (disposed) {
              return;
            }
            if (outcome.ok && outcome.status === 202) {
              var runId = outcome.body.run_id;
              deps.storage.set(STORAGE_RUN_KEY, runId);
              /* Working state is shown immediately after acceptance. */
              emitRun({
                phase: "running",
                runId: runId,
                statusLabel: runStatusLabel("running"),
                running: true,
                resultText: null,
                observations: [],
                errorText: null
              });
              startPolling(runId);
              return;
            }
            var conflict = outcome.status === 409;
            emitRun({
              phase: "start_failed",
              runId: null,
              statusLabel: runStatusLabel("start_failed"),
              running: false,
              resultText: null,
              observations: [],
              errorText: conflict
                ? "Another analysis is already in progress."
                : "The analysis could not be started. Refresh the page and try again."
            });
          })
          .catch(function () {
            submitInFlight = false;
            if (disposed) {
              return;
            }
            emitRun({
              phase: "start_unreachable",
              runId: null,
              statusLabel: runStatusLabel("start_failed"),
              running: false,
              resultText: null,
              observations: [],
              errorText: TEXT.connectionLost
            });
          });
        return true;
      },
      /* One explicit decision POST per card; an accepted decision is never
       * displayed as a completed write before the controller result. */
      decide: function (approvalId, decision) {
        if (disposed || decidedInFlight[approvalId]) {
          return false;
        }
        var known = knownCards[approvalId];
        /* Only a listed, still-pending card in a live context may be acted
         * on; losing the decision context (restart, unknown run) must not
         * resend anything in the dark. */
        if (!known || known.status !== "pending" || !cardsRunId) {
          emitCards({
            phase: "decision_unavailable",
            runId: cardsRunId || null,
            cards: cardsFromKnown(),
            message:
              "This decision is no longer valid. The run status explains what happened."
          });
          return false;
        }
        decidedInFlight[approvalId] = true;
        known.buttonsLocked = true;
        deps
          .fetch(
            "/api/runs/" +
              encodeURIComponent(cardsRunId || "") +
              "/approvals/" +
              encodeURIComponent(approvalId),
            {
              method: "POST",
              credentials: "same-origin",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": session ? session.csrf_token : ""
              },
              body: JSON.stringify({
                decision: decision,
                instance_id: session ? session.instance_id : ""
              })
            }
          )
          .then(function (response) {
            return response.json().then(function (body) {
              return { ok: response.ok, status: response.status, body: body };
            });
          })
          .then(function (outcome) {
            decidedInFlight[approvalId] = false;
            if (disposed) {
              return;
            }
            if (outcome.ok && outcome.status === 200) {
              var receipt = outcome.body;
              var status = receipt.status;
              if (knownCards[approvalId]) {
                knownCards[approvalId].status = status;
                knownCards[approvalId].statusLabel = approvalStatusLabel(status);
              }
              /* approved/executing/completed all stay read-only for the UI:
               * the write result itself comes from the run observation. */
              emitCards({
                phase: "decision_accepted",
                runId: cardsRunId,
                cards: cardsFromKnown(),
                applied: {
                  approvalId: approvalId,
                  decision: decision,
                  statusLabel: approvalStatusLabel(status)
                }
              });
              return;
            }
            var message =
              outcome.status === 409
                ? "This decision is no longer valid. The run status explains what happened."
                : "This decision could not be applied. Check the run status.";
            emitCards({
              phase: "decision_unavailable",
              runId: cardsRunId,
              cards: cardsFromKnown(),
              message: message
            });
          })
          .catch(function () {
            decidedInFlight[approvalId] = false;
            if (disposed) {
              return;
            }
            emitCards({
              phase: "decision_unreachable",
              runId: cardsRunId,
              cards: cardsFromKnown(),
              message: TEXT.connectionLost
            });
          });
        return true;
      },
      dispose: function () {
        disposed = true;
        stopPolling();
        stopCardsPolling();
      }
    };

    /* Transactions read service (T-19): explicit filters, bounded list,
     * truncation hint, no optimistic changes and a backend refresh after
     * operations instead of invented state. */
    var lastFilters = { query: "", merchant: "" };

    controller.refreshTransactions = function (rawQuery, rawMerchant) {
      if (typeof rawQuery === "string" || typeof rawMerchant === "string") {
        lastFilters = {
          query: (rawQuery || "").trim(),
          merchant: (rawMerchant || "").trim()
        };
      }
      var query = transactionFiltersQuery(lastFilters.query, lastFilters.merchant);
      return deps
        .fetch("/api/transactions" + (query ? "?" + query : ""), {
          method: "GET",
          credentials: "same-origin"
        })
        .then(function (response) {
          return response.json().then(function (body) {
            return { ok: response.ok, status: response.status, body: body };
          });
        })
        .then(function (outcome) {
          if (disposed) {
            return null;
          }
          if (
            outcome.ok &&
            outcome.status === 200 &&
            outcome.body &&
            outcome.body.items
          ) {
            var rows = outcome.body.items.map(transactionRow);
            emitTransactions({
              phase: "transactions",
              rows: rows,
              note: truncatedNote(
                (outcome.body.items || []).length,
                outcome.body.limit,
                outcome.body.possibly_truncated
              ),
              filters: { ...lastFilters }
            });
            return outcome.body.items;
          }
          var message =
            outcome.status === 503
              ? "Transaction data is busy. Try again shortly."
              : "Transactions are unavailable right now. Refresh the list later.";
          emitTransactions({
            phase: "unavailable",
            rows: [],
            note: message,
            filters: { ...lastFilters }
          });
          return null;
        })
        .catch(function () {
          if (disposed) {
            return null;
          }
          emitTransactions({
            phase: "unreachable",
            rows: [],
            note: TEXT.connectionLost,
            filters: { ...lastFilters }
          });
          return null;
        });
    };

    function emitTransactions(view) {
      if (views.transactions) {
        views.transactions(view);
      }
    }

    controller.openTransaction = function (transactionId) {
      return deps
        .fetch(
          "/api/transactions/" + encodeURIComponent(transactionId),
          { method: "GET", credentials: "same-origin" }
        )
        .then(function (response) {
          return response.json().then(function (body) {
            return { ok: response.ok, status: response.status, body: body };
          });
        })
        .then(function (outcome) {
          if (disposed) {
            return null;
          }
          if (outcome.ok && outcome.status === 200 && outcome.body.transaction_id) {
            var detail = detailView(outcome.body);
            emitDetail({
              phase: "detail",
              transactionId: transactionId,
              lines: detail.lines
            });
            return outcome.body;
          }
          emitDetail({
            phase: "detail_unavailable",
            transactionId: transactionId,
            lines: ["This transaction record is not available."]
          });
          return null;
        })
        .catch(function () {
          if (disposed) {
            return null;
          }
          emitDetail({
            phase: "detail_unreachable",
            transactionId: transactionId,
            lines: [TEXT.connectionLost]
          });
          return null;
        });
    };

    function emitDetail(view) {
      if (views.detail) {
        views.detail(view);
      }
    }

    function cardsFromKnown() {
      return Object.keys(knownCards)
        .sort()
        .map(function (approvalId) {
          return knownCards[approvalId];
        });
    }

    function isRunning() {
      return activeRunId !== null;
    }

    return controller;
  }

  UI.createController = createController;

  /* ------------------------------------------------------------------ *
   * Bootstrap T-16: only GET /api/session; opening the page never      *
   * requires the model and never starts an analysis.                   *
   * ------------------------------------------------------------------ */

  function renderServiceState(text) {
    var node = byId("service-state");
    if (node) {
      node.textContent = text;
    }
    return text;
  }
  UI.renderServiceState = renderServiceState;

  /* ------------------------------------------------------------------ *
   * Renderer + binding (T-17): run facts, observations, validation and *
   * single-flight submission through the controller.                   *
   * ------------------------------------------------------------------ */

  function renderRun(view) {
    var runId = byId("run-id");
    var status = byId("run-status");
    var note = byId("run-note");
    var answer = byId("model-answer");
    if (!runId || !status || !note || !answer) {
      return view;
    }
    runId.textContent = view.runId || TEXT.runIdFallBack;
    status.textContent = view.statusLabel || TEXT.noAnalysisYet;
    status.className = view.phase
      ? "phase-" + view.phase
      : "phase-idle";
    note.hidden = !view.errorText;
    note.textContent = view.errorText || "";
    answer.textContent = view.resultText || "";
    renderObservations(view.observations);
    var runButton = byId("run-button");
    if (runButton) {
      runButton.disabled = Boolean(view.running);
    }
    return view;
  }
  UI.renderRun = renderRun;

  function renderCommandError(message) {
    var node = byId("command-error");
    if (!node) {
      return message;
    }
    node.hidden = !message;
    node.textContent = message || "";
    return message;
  }
  UI.renderCommandError = renderCommandError;

  var decideSink = null;

  /* ------------------------------------------------------------------ *
   * Decision cards (T-18): one card per approval, three buttons, and   *
   * a pending-action note; acceptance never reads as a finished write. *
   * ------------------------------------------------------------------ */

  function cardNodes(view, onDecide) {
    var article = h("article", { class: "decision-card" });
    article.appendChild(
      h("h3", { class: "card-kind" }, view.kindLabel)
    );
    article.appendChild(h("p", { class: "card-status" }, view.statusLabel));
    article.appendChild(h("p", { class: "card-question" }, view.question));
    var facts = h("ul", { class: "card-facts" });
    view.facts.forEach(function (fact) {
      facts.appendChild(h("li", { text: fact }));
    });
    article.appendChild(facts);
    if (view.previousCategory) {
      article.appendChild(
        h(
          "p",
          { class: "card-previous" },
          TEXT.previousCategory + ": " + view.previousCategory
        )
      );
    }
    if (view.proposedCategory) {
      article.appendChild(
        h(
          "p",
          { class: "card-proposed" },
          TEXT.proposedCategory + ": " + view.proposedCategory
        )
      );
    }
    article.appendChild(h("p", { class: "card-reason" }, TEXT.reason + ": " + view.reason));
    article.appendChild(h("p", { class: "card-expiry" }, view.expiresLabel));
    article.appendChild(h("p", { class: "card-note" }, view.pendingActionNote));
    var actions = h("div", { class: "card-actions" });
    var confirmButton = h(
      "button",
      { type: "button", class: "card-confirm" },
      view.confirmLabel
    );
    confirmButton.addEventListener("click", function () {
      confirmButton.disabled = true;
      onDecide(view.approvalId, "approve");
    });
    var rejectButton = h(
      "button",
      { type: "button", class: "card-reject" },
      view.rejectLabel
    );
    rejectButton.addEventListener("click", function () {
      rejectButton.disabled = true;
      onDecide(view.approvalId, "reject");
    });
    if (view.buttonsLocked) {
      confirmButton.disabled = true;
      rejectButton.disabled = true;
    }
    actions.appendChild(confirmButton);
    actions.appendChild(rejectButton);
    article.appendChild(actions);
    return article;
  }

  function renderCards(cardsView) {
    var container = byId("decision-cards");
    if (!container) {
      return cardsView;
    }
    var note = byId("decision-note");
    if (note) {
      var message = cardsView.message || "";
      note.hidden = !message;
      note.textContent = message;
    }
    var pieces = [];
    (cardsView.cards || []).forEach(function (card) {
      pieces.push(cardNodes(card, decideSink || function () {}));
    });
    listChildren(container, pieces);
    return cardsView;
  }
  UI.renderCards = renderCards;

  function bindDecide(fn) {
    decideSink = fn;
  }
  UI.bindDecide = bindDecide;

  /* ------------------------------------------------------------------ *
   * Transactions (T-19): effective state table, filters, lazy native   *
   * <details> per row (keyboard accessible) and truncation hint.       *
   * ------------------------------------------------------------------ */

  function detailNode(transactionId, onOpen) {
    var fold = h("details", { class: "tx-details" });
    var summary = h("summary", null, TEXT.detailsToggle);
    var body = h(
      "div",
      { class: "tx-detail", id: "detail-for-" + transactionId },
      "Details load when opened."
    );
    fold.appendChild(summary);
    fold.appendChild(body);
    fold.addEventListener("toggle", function () {
      if (fold.open) {
        onOpen(transactionId, body);
      }
    });
    return fold;
  }

  function renderTransactions(view) {
    var body = byId("transactions-body");
    if (!body) {
      return view;
    }
    var note = byId("transactions-note");
    var empty = byId("transactions-empty");
    if (note) {
      note.textContent = view.note || "";
    }
    var rows = [];
    (view.rows || []).forEach(function (row) {
      var tr = h("tr", { class: "tx-row" });
      row.cells.forEach(function (cell) {
        tr.appendChild(h("td", { text: cell }));
      });
      var detailsCell = h("td");
      detailsCell.appendChild(
        detailNode(row.transactionId, UI.detailSink || function () {})
      );
      tr.appendChild(detailsCell);
      rows.push(tr);
    });
    listChildren(body, rows);
    if (empty) {
      empty.hidden = rows.length > 0 || (view.phase && view.phase !== "transactions");
    }
    return view;
  }
  UI.renderTransactions = renderTransactions;

  function renderDetail(view) {
    var cell = byId("detail-for-" + view.transactionId);
    if (cell) {
      listChildren(
        cell,
        view.lines.map(function (line) {
          return h("p", { text: line });
        })
      );
    }
    return view;
  }
  UI.renderDetail = renderDetail;

  function bindDetail(fn) {
    UI.detailSink = fn;
  }
  UI.bindDetail = bindDetail;

  function makeBrowserDeps() {
    /* fetch is a native method that requires the window receiver; a bare
     * reference called as `deps.fetch(...)` becomes an Illegal invocation
     * in Chromium, so the browser dependency is bound to the root once.
     * The DOM storage uses getItem/setItem/removeItem, while the controller
     * speaks the small get/set/remove contract, so the browser side adapts. */
    return {
      fetch: (typeof fetch === "function" ? fetch : root.fetch).bind(root),
      storage: {
        get: function (name) {
          return sessionStorage.getItem(name);
        },
        set: function (name, value) {
          sessionStorage.setItem(name, String(value));
        },
        remove: function (name) {
          sessionStorage.removeItem(name);
        }
      },
      timers: root.__UI_TIMERS__ || {
        set: function (handler, delay) {
          return root.setTimeout(handler, delay);
        },
        clear: function (handle) {
          root.clearTimeout(handle);
        }
      },
      token: function (label) {
        var bytes = new Uint8Array(9);
        crypto.getRandomValues(bytes);
        var digits = Array.prototype.map
          .call(bytes, function (item) {
            return item.toString(36).padStart(2, "0");
          })
          .join("");
        return label + "_" + digits;
      },
      views: {
        run: function (view) {
          renderRun(view);
          if (view.phase === "validation") {
            renderCommandError(view.errorText);
          } else {
            renderCommandError(null);
          }
        },
        cards: renderCards,
        service: renderServiceState,
        transactions: renderTransactions
      }
    };
  }

  function attach() {
    var deps = makeBrowserDeps();
    var controller = createController(deps);
    bindDecide(controller.decide.bind(controller));
    bindDetail(controller.openTransaction.bind(controller));
    var session = bootstrap();
    UI.__controller__ = controller;
    session.then(function (sessionPayload) {
      if (sessionPayload) {
        controller.resume(sessionPayload);
        /* Effective backend state right away; opening the screen reads. */
        controller.refreshTransactions();
      } else {
        controller.dispose();
      }
    });
    var runButton = byId("run-button");
    var goalInput = byId("goal-input");
    if (runButton && goalInput) {
      runButton.addEventListener("click", function () {
        controller.submitGoal(goalInput.value);
      });
    }
    var filters = byId("transaction-filters");
    var queryInput = byId("filter-query");
    var merchantInput = byId("filter-merchant");
    if (filters && queryInput && merchantInput) {
      filters.addEventListener("submit", function (event) {
        if (event && event.preventDefault) {
          event.preventDefault();
        }
        controller.refreshTransactions(queryInput.value, merchantInput.value);
      });
    }
    return controller;
  }
  UI.attach = attach;

  function fetchSession() {
    return fetch("/api/session", { method: "GET", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("session_failed");
        }
        return response.json();
      })
      .then(function (session) {
        renderServiceState(TEXT.connected);
        return session;
      })
      .catch(function () {
        renderServiceState(TEXT.serviceUnavailable);
        return null;
      });
  }
  UI.fetchSession = fetchSession;

  function bootstrap() {
    return fetchSession();
  }
  UI.bootstrap = bootstrap;

  function start() {
    attach();
  }
  UI.start = start;

  root.UI = UI;
  if (typeof document !== "undefined" && root.__UI_MANUAL__ !== true) {
    /* Defer wiring until the DOM is parsed (script is loaded with defer). */
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  }
  root.__UI_MANUAL__ = true;
})();
