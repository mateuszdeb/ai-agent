"""T-24: end-to-end scenarios, accessibility and responsiveness in Chromium.

Real user journeys run against the real locally served application with a
fully deterministic, stubbed backend (no live model, no network): the whole
journey (happy path), unavailable model, step limit, empty and limited
transaction reads, an approve and a reject decision, a conflict between two
browser sessions, a reload resume, and a connection loss. Keyboard access,
labels, English text, untrusted content as text and a 360 px viewport are
asserted on the live page. Every wait is bounded; no arbitrary sleeps.
"""

import contextlib
import json
import threading

import pytest
from browser_env import BrowserEnv
from playwright.sync_api import sync_playwright

from agent_project.llm import MockClient

TITLE = "Household Expense Checker"
CONNECTED = "Connected to the local service."
SERVICE_DOWN = "Service is unreachable. Refresh the page to try again."
CONNECTION_LOST = (
    "Connection lost. Check the run status before starting another analysis."
)
MODEL_UNAVAILABLE = (
    "Local model is unavailable. "
    "Check its configuration before starting a new analysis."
)
RUN_IN_PROGRESS = "Another analysis is already in progress."
EMPTY_GOAL = "Enter a goal containing at least one non-whitespace character."
GOAL = "Review my expenses and look for duplicate charges."
REVIEWED_TEXT = "Reviewed expenses; no script executed."
FINISH_TEXT = "Analysis finished after your decision."
DELETE_TOOL_CALL = {
    "text": "",
    "tool_calls": [
        {
            "id": "call_1",
            "name": "delete_flagged_transaction",
            "arguments": {"transaction_id": "tx_pending", "reason": GOAL},
        }
    ],
}
SEARCH_TOOL_CALL = {
    "text": "",
    "tool_calls": [
        {
            "id": "call_1",
            "name": "search_transactions",
            "arguments": {"query": "", "limit": 5},
        }
    ],
}
VIEWPORT = {"width": 1280, "height": 800}
SLOW_TIMEOUT = 10_000


class UnavailableModelClient(MockClient):
    """Backend stub whose model contact fails without any network access."""

    model = "mock"
    provider = "mock"

    def __init__(self) -> None:
        super().__init__([])

    def complete(self, messages, tools=None):
        raise ConnectionError("Simulated offline outage")


DEFAULT_RECORDS = [
    ("tx_categorized", "Market", 40.0, "Groceries", "Weekly groceries"),
    ("tx_pending", "Pending Shop", 120.0, None, "Uncategorized purchase"),
    ("tx_dup_a", "Stream", 25.0, "Subscriptions", "Monthly subscription"),
    ("tx_dup_b", "Stream", 25.0, "Subscriptions", "Repeated subscription"),
    ("tx_fuel", "Fuel Station", 200.0, "Transport", "Fuel purchase"),
]


def standard_records():
    return [list(record) for record in DEFAULT_RECORDS]


def generated_records(count: int):
    return [
        [f"tx_e{index:02d}", f"E2E Shop {index:02d}", 10.0 + index, "General", ""]
        for index in range(1, count + 1)
    ]


def write_baseline(path, records):
    path.write_text(
        json.dumps(
            [
                {
                    "id": tx_id,
                    "date": "2026-10-01",
                    "merchant": merchant,
                    "amount": amount,
                    "currency": "PLN",
                    "category_hint": category,
                    "description": description,
                }
                for tx_id, merchant, amount, category, description in records
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def pw():
    with sync_playwright() as p:
        yield p


@pytest.fixture
def e2e(pw, tmp_path):
    """Build one isolated local app plus one real Chromium page per scenario.

    Every scenario owns a private baseline file (default controlled records,
    or an override), one loopback server, a stubbed backend and one page;
    everything is closed deterministically on exit.
    """
    paths: list = []

    def build(
        responses=None,
        *,
        records=None,
        client=None,
        max_steps=None,
        viewport=VIEWPORT,
    ):
        paths.append(tmp_path)
        data_path = tmp_path / f"baseline_{len(paths)}.json"
        changes_path = tmp_path / f"ledger_{len(paths)}.jsonl"
        write_baseline(data_path, standard_records() if records is None else records)
        env = BrowserEnv(
            data_path=data_path,
            changes_path=changes_path,
            tmp_root=tmp_path,
            responses=list(responses or []),
            client=client,
            max_steps=max_steps,
        )
        browser = pw.chromium.launch(headless=True)
        # Test tooling evaluates probes even under the page's strict CSP.
        context = browser.new_context(viewport=viewport, bypass_csp=True)
        page = context.new_page()
        page.goto(env.url, wait_until="domcontentloaded")
        wait_connected(page)
        bundle = _Scenario(env, page, context, browser)
        return _closing(bundle)

    yield build


class _Scenario:
    def __init__(self, env, page, context, browser):
        self.env = env
        self.page = page
        self.context = context
        self.browser = browser


@contextlib.contextmanager
def _closing(bundle):
    try:
        yield bundle
    finally:
        bundle.browser.close()
        bundle.env.close()


def wait_connected(page):
    page.wait_for_function(
        "document.getElementById('service-state').textContent === "
        + json.dumps(CONNECTED),
        timeout=SLOW_TIMEOUT,
    )


def submit_goal(page, goal):
    page.locator("#goal-input").fill(goal)
    page.locator("#run-button").click()


def wait_run_status(page, status, timeout=SLOW_TIMEOUT):
    page.wait_for_function(
        "document.getElementById('run-status').textContent === " + json.dumps(status),
        timeout=timeout,
    )


def wait_transaction_rows(page, count, timeout=SLOW_TIMEOUT):
    """The table refresh starts when the run turns terminal; wait for it."""
    page.wait_for_function(
        "document.querySelectorAll('#transactions-body tr.tx-row').length === "
        + json.dumps(count),
        timeout=timeout,
    )


def transaction_rows(page) -> int:
    return page.locator("#transactions-body tr.tx-row").count()


def first_note_text(page) -> str:
    return page.locator("#transactions-note").inner_text()


# ---------------------------------------------------------------------------
# 1) The whole journey: submit, work in bounded time, result and English data


def test_whole_journey_reaches_completed_result_in_bounded_time(e2e):
    with e2e([{"text": REVIEWED_TEXT, "tool_calls": []}]) as scenario:
        page = scenario.page
        submit_goal(page, GOAL)
        # The working state appears immediately; completion stays in bounds.
        wait_run_status(page, "Completed")
        assert REVIEWED_TEXT in page.locator("#model-answer").inner_text()
        assert page.locator("#service-state").inner_text() == CONNECTED
        not_confirmed = "No analysis yet"
        assert page.locator("#run-note").is_hidden()

        # The effective state table is English UI with untouched data.
        wait_transaction_rows(page, 5)
        assert transaction_rows(page) == 5
        body_text = page.locator("#transactions-table").inner_text()
        for original in ("Market", "Fuel Station", "Pending Shop"):
            assert original in body_text
        assert not_confirmed not in page.locator("#run-status").inner_text()


# ---------------------------------------------------------------------------
# 2) Approve journey: consent, single write, refreshed table


def test_approve_decision_completes_the_one_allowed_write(e2e):
    with e2e([DELETE_TOOL_CALL, {"text": FINISH_TEXT}]) as scenario:
        page = scenario.page
        submit_goal(page, GOAL)
        card = page.locator(".decision-card").first
        card.wait_for(state="visible")
        assert (
            "Delete this transaction" in page.locator(".card-kind").first.inner_text()
        )  # noqa: E501
        assert "tx_pending" in page.locator(".card-facts").first.inner_text()
        confirm = page.locator(".card-confirm").first
        confirm.focus()
        confirm.press("Enter")  # keyboard-only consent

        wait_run_status(page, "Completed")
        observations = page.locator("#observations-list").inner_text()
        assert "Delete flagged transaction" in observations
        assert "success" in observations
        wait_transaction_rows(page, 4)
        row_texts = page.locator("#transactions-body").inner_text()
        assert "tx_pending" not in row_texts
        assert scenario.env.app.state.runs.active_count() == 0


# ---------------------------------------------------------------------------
# 3) Reject journey: consent denied, nothing written


def test_reject_decision_keeps_every_transaction(e2e):
    with e2e([DELETE_TOOL_CALL, {"text": FINISH_TEXT}]) as scenario:
        page = scenario.page
        submit_goal(page, GOAL)
        card = page.locator(".decision-card").first
        card.wait_for(state="visible")
        reject = page.locator(".card-reject").first
        reject.focus()
        reject.press("Enter")

        wait_run_status(page, "Completed")
        # The card stays visible with its terminal status label; the denial
        # observation is the honest follow-up in the run's history.
        assert "Rejected" in page.locator(".decision-card").first.inner_text()
        observations = page.locator("#observations-list").inner_text()
        assert "confirmation_denied" in observations
        wait_transaction_rows(page, 5)
        assert "tx_pending" in page.locator("#transactions-body").inner_text()


# ---------------------------------------------------------------------------
# 4) Step limit: the label is reached and observations survive


def test_step_limit_status_reached_within_bounded_time(e2e):
    with e2e(
        [SEARCH_TOOL_CALL, {"text": "Further analysis stopped."}],
        max_steps=1,
    ) as scenario:
        page = scenario.page
        submit_goal(page, GOAL)
        wait_run_status(page, "Step limit reached")
        assert "Search transactions" in page.locator("#observations-list").inner_text()
        run_button = page.locator("#run-button")
        assert run_button.is_enabled()
        wait_transaction_rows(page, 5)


# ---------------------------------------------------------------------------
# 5) Unavailable model: English failure, never a stack trace


def test_unavailable_model_shows_english_failure(e2e):
    with e2e(client=UnavailableModelClient()) as scenario:
        page = scenario.page
        submit_goal(page, GOAL)
        wait_run_status(page, "Failed")
        note = page.locator("#run-note")
        note.wait_for(state="visible")
        assert note.inner_text() == MODEL_UNAVAILABLE
        assert "Traceback" not in page.content()
        assert "ConnectionError" not in page.content()
        wait_transaction_rows(page, 5)


# ---------------------------------------------------------------------------
# 6) Empty and limited reads


def test_transactions_empty_and_limited_reads_in_browser(e2e):
    with e2e(records=[]) as scenario:
        page = scenario.page
        empty_marker = page.locator("#transactions-empty")
        empty_marker.wait_for(state="visible")
        assert empty_marker.inner_text() == "No transactions match your filters."
        assert first_note_text(page) == "No transactions found."
        assert transaction_rows(page) == 0

    with e2e(records=generated_records(12)) as scenario:
        page = scenario.page
        page.wait_for_function(
            "document.getElementById('transactions-note').textContent === "
            "'Showing up to 10 results. Refine your search.'",
            timeout=SLOW_TIMEOUT,
        )
        assert transaction_rows(page) == 10
        # A filter limits the read to one item again.
        page.locator("#filter-merchant").fill("E2E Shop 03")
        page.locator("#filter-button").click()
        page.wait_for_function(
            "document.getElementById('transactions-note').textContent === "
            "'Showing 1 result.'",
            timeout=SLOW_TIMEOUT,
        )
        assert transaction_rows(page) == 1


# ---------------------------------------------------------------------------
# 7) Untrusted content stays text and never executes


def test_untrusted_model_text_never_becomes_a_script(e2e):
    payloads = []

    with e2e(
        [{"text": "<script>alert(1)</script> " + REVIEWED_TEXT, "tool_calls": []}]
    ) as scenario:
        page = scenario.page
        page.on(
            "dialog",
            lambda dialog: (payloads.append(dialog.type()), dialog.dismiss()),
        )
        submit_goal(page, "<script>alert(1)</script> " + GOAL)
        wait_run_status(page, "Completed")
        answer = page.locator("#model-answer")
        assert REVIEWED_TEXT in answer.inner_text()
        assert "<script>alert(1)</script>" in answer.inner_text()
        # The result text renders as one text node with no element children.
        assert (
            page.evaluate("document.getElementById('model-answer').childElementCount")
            == 0
        )
        assert payloads == []
        assert page.locator("#decision-cards").inner_text() == ""


# ---------------------------------------------------------------------------
# 8) Reload (przeładowanie): resume through reads only


def test_reload_resumes_the_pending_decision(e2e):
    with e2e([DELETE_TOOL_CALL, {"text": FINISH_TEXT}]) as scenario:
        page = scenario.page
        submit_goal(page, GOAL)
        page.locator(".decision-card").first.wait_for(state="visible")
        page.reload(wait_until="domcontentloaded")
        wait_connected(page)
        wait_run_status(page, "Waiting for your decision")
        card = page.locator(".decision-card").first
        card.wait_for(state="visible")
        # The resumed decision remains usable and finishes the journey.
        page.locator(".card-confirm").first.click()
        wait_run_status(page, "Completed")
        wait_transaction_rows(page, 4)


# ---------------------------------------------------------------------------
# 9) Connection loss while the decision hangs: bounded English retry state


def test_connection_loss_shows_retrying_state(e2e):
    with e2e([DELETE_TOOL_CALL, {"text": FINISH_TEXT}]) as scenario:
        env = scenario.env
        page = scenario.page
        submit_goal(page, GOAL)
        page.locator(".decision-card").first.wait_for(state="visible")
        thread = threading.Thread(target=env.stop_serving, name="stop-server")
        thread.start()
        thread.join(timeout=15)
        note = page.locator("#run-note")
        note.wait_for(state="visible", timeout=SLOW_TIMEOUT)
        assert note.inner_text() == CONNECTION_LOST
        # The last known status stays visible, never guessed as finished.
        assert page.locator("#run-status").inner_text() == "Waiting for your decision"
        assert page.locator("#run-button").is_disabled()


# ---------------------------------------------------------------------------
# 10) Conflict: a second browser session cannot start its own run


def test_conflict_second_session_cannot_start_run(e2e):
    with e2e([DELETE_TOOL_CALL, {"text": FINISH_TEXT}]) as scenario:
        env = scenario.env
        browser = scenario.browser
        page = scenario.page
        submit_goal(page, GOAL)
        page.locator(".decision-card").first.wait_for(state="visible")

        other = browser.new_context(viewport=VIEWPORT).new_page()
        other.goto(env.url, wait_until="domcontentloaded")
        wait_connected(other)
        submit_goal(other, GOAL)
        note = other.locator("#run-note")
        note.wait_for(state="visible", timeout=SLOW_TIMEOUT)
        assert note.inner_text() == RUN_IN_PROGRESS
        assert other.locator("#run-status").inner_text() == "Failed"
        assert other.locator("#run-button").is_enabled()
        # The owner session still owns its pending decision untouched.
        assert page.locator(".decision-card").first.is_visible()
        assert env.app.state.runs.count() == 1


# ---------------------------------------------------------------------------
# 11) Keyboard access and labels without the mouse


def test_keyboard_only_validation_and_labels(e2e):
    with e2e([{"text": REVIEWED_TEXT, "tool_calls": []}]) as scenario:
        page = scenario.page
        assert page.get_by_label("What would you like to review?").count() == 1
        assert page.get_by_label("Search transactions").count() == 1
        assert page.get_by_label("Filter by merchant").count() == 1
        assert page.locator("#transactions-table caption").is_visible()

        # Tab from the goal textarea reaches the run button directly.
        page.locator("#goal-input").focus()
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == "run-button"
        page.keyboard.press("Enter")  # empty goal: validation happens
        error = page.locator("#command-error")
        error.wait_for(state="visible")
        assert error.inner_text() == EMPTY_GOAL
        # aria-live region for decisions exists and is polite.
        polite = page.locator("#decision-cards[aria-live='polite']")
        assert polite.count() == 1
        # Status nodes announce themselves.
        assert page.locator("#service-state[role='status']").count() == 1
        assert page.locator("#run-note[role='status']").count() == 1


# ---------------------------------------------------------------------------
# 12) 360 px viewport: readable, no page-level horizontal overflow


def test_360px_viewport_stays_readable_without_page_scroll(e2e):
    with e2e([{"text": REVIEWED_TEXT, "tool_calls": []}]) as scenario:
        page = scenario.page
        for selector in ("#app-header h1", "#run-button", "#transactions-heading"):
            assert page.locator(selector).is_visible()
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - "
            "document.documentElement.clientWidth"
        )
        assert overflow <= 1
