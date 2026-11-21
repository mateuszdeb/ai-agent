"""T-16: static screen, safe bootstrap and CSP-compatible same-origin assets.

The screen is served from the same origin with the app's security headers.
Opening it never requires the model, never starts an analysis and never
creates a run. Untrusted content must be renderable only as text, so the
source contract is asserted here (labels, no CDNs, no inline handlers,
textContent-only renderer) while behavior scenarios live in the Node harness
(tests/web_ui/screen.mjs).
"""

import subprocess
from pathlib import Path

import httpx
import pytest

from agent_project.web import WebSettings, create_app

ORIGIN = "http://127.0.0.1:8765"

REQUIRED_LABELS = [
    "Household Expense Checker",
    "What would you like to review?",
    "Run analysis",
    "Analysis status",
    "Pending decision",
    "Result",
    "Transactions",
]
REQUIRED_AREA_IDS = [
    "service-state",
    "goal-input",
    "run-button",
    "run-id",
    "run-status",
    "decision-cards",
    "model-answer",
    "observations-list",
    "transaction-filters",
    "transactions-body",
]


@pytest.fixture
def screen_env(tmp_path, controlled_dataset):
    """Offline app with isolated dataset; screen files are repo assets."""
    data_path, changes_path = controlled_dataset
    app = create_app(
        settings=WebSettings(data_path=data_path, changes_path=changes_path)
    )
    return app


def _fetch(app, path):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=ORIGIN, follow_redirects=False
        ) as client:
            return await client.get(path)

    import asyncio

    return asyncio.run(scenario())


def test_root_serves_static_screen_with_security_headers(screen_env):
    response = _fetch(screen_env, "/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The middleware applies the same strict security headers to the screen.
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    for label in REQUIRED_LABELS:
        assert label in response.text
    for area_id in REQUIRED_AREA_IDS:
        assert f'id="{area_id}"' in response.text


def test_screen_links_only_same_origin_assets(screen_env):
    response = _fetch(screen_env, "/")
    assert 'href="/static/app.css"' in response.text
    assert 'src="/static/app.js"' in response.text
    # No inline active content anywhere in the screen markup.
    body = response.text.split("<body", 1)[1]
    import re

    assert not re.search(r"\son[a-z]+\s*=", body)
    assert "<style" not in body
    assert not re.search(r"<script[^>]*>[^<]", body)


def test_static_assets_are_served(screen_env):
    css = _fetch(screen_env, "/static/app.css")
    js = _fetch(screen_env, "/static/app.js")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert ":focus-visible" in css.text
    assert "@media" in css.text
    assert "overflow-x: auto" in css.text
    assert js.status_code == 200
    assert js.headers["content-type"].startswith(
        ("text/javascript", "application/javascript")
    )
    # Renderer safety contract: no HTML assignment and no dynamic evaluation.
    for pattern in (".innerHTML =", ".outerHTML =", "eval(", "new Function("):
        assert pattern not in js.text


def test_screen_open_does_not_start_analysis_or_need_model(screen_env):
    async def scenario(client):
        bootstrap = await client.get("/api/session")
        assert bootstrap.status_code == 200
        screen = await client.get("/")
        assert screen.status_code == 200

    import asyncio

    async def full():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=screen_env), base_url=ORIGIN
        ) as client:
            await scenario(client)

    asyncio.run(full())
    # GET / is a read-only screen: no run exists in the store afterwards.
    assert screen_env.state.runs.get_active_run() is None
    assert (
        screen_env.state.runs.list_runs() in ({}, None)
        or len(getattr(screen_env.state.runs, "_runs", {})) == 0
    )


def test_unknown_static_paths_stay_hidden(screen_env):
    blocked = _fetch(screen_env, "/static/.gitkeep")
    assert blocked.status_code == 404
    missing = _fetch(screen_env, "/static/does-not-exist.css")
    assert missing.status_code == 404


NODE_SCENARIOS = ["screen", "flow", "decisions", "transactions", "recovery"]


@pytest.mark.parametrize("scenario", NODE_SCENARIOS)
def test_ui_node_scenarios(scenario):
    """Run the Node behavior scenario for the UI contract."""
    root = Path(__file__).parent
    scenario_path = root / "web_ui" / f"{scenario}.mjs"
    harness = root / "web_ui_harness.mjs"
    result = subprocess.run(
        ["node", str(harness), str(scenario_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
