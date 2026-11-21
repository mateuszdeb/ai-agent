"""T-23: one automated browser test over the real local application.

A single headless Chromium visit proves the static UI loads from the real
locally served app on an isolated loopback port while the backend model
client stays fully stubbed: no live model, no external service, no working
ledger write. The environment fixture lives in ``browser_env.py``.
"""

import pytest
from browser_env import BrowserEnv
from playwright.sync_api import sync_playwright


@pytest.fixture
def browser_app(controlled_dataset, tmp_path):
    """Serve the real app on one isolated loopback port with offline data."""
    data_path, changes_path = controlled_dataset
    env = BrowserEnv(
        data_path=data_path,
        changes_path=changes_path,
        tmp_root=tmp_path,
        responses=[{"text": "Analysis stays offline in this test.", "tool_calls": []}],
    )
    try:
        yield env
    finally:
        env.close()
        # The teardown itself proves the clean process shutdown.
        assert not env.thread.is_alive()


def test_ui_opens_in_headless_chromium_without_a_live_model(browser_app):
    env = browser_app
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            response = page.goto(env.url, wait_until="domcontentloaded")
            assert response is not None and response.status == 200

            # The real screen title and heading render as static text.
            assert page.title() == "Household Expense Checker"
            assert page.locator("h1").first.inner_text() == "Household Expense Checker"

            # The visit must never reach a model: the client stays silent and
            # the visit starts no analysis run.
            assert env.client.calls == []
            assert env.app.state.runs.count() == 0
        finally:
            browser.close()

    # The environment keeps no analysis work behind the screen.
    assert env.app.state.runner.is_busy() is False
