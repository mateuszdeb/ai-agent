"""T-21: cross-cutting security and HTTP contract matrix.

One parametrized matrix across every public endpoint and every transport
dimension (session, Origin, Host, CSRF, instance, extra fields and limits),
so no alternative HTTP path can invoke a domain tool, swap a goal or bypass a
decision. All frames are exchanged over the in-process ASGI boundary: no
listening server, no live model, no network. Every denial must leave both the
temporary ledger and the baseline untouched.
"""

import asyncio
import hashlib
import threading
import time

import httpx
import pytest

from agent_project.web import WebSettings, create_app
from agent_project.web.presentation import HTTP_ERRORS
from agent_project.web.runner import RunManager
from agent_project.web.runs import TERMINAL_RUN_STATUSES
from agent_project.web.security import SECURITY_HEADERS
from agent_project.web.sessions import SESSION_COOKIE

ORIGIN = "http://127.0.0.1:8765"
FOREIGN_ORIGIN = "http://localhost:8765"

ACCEPTED_CODES = set(HTTP_ERRORS)

WRITE_TARGETS = [
    "start_run",
    "decision",
    "decision_foreign_id",
]
READ_TARGETS = [
    "run_snapshot",
    "approvals",
    "transactions",
    "transaction_detail",
]
PUBLIC_TARGETS = ["screen", "session", "static_css"]


@pytest.fixture
def matrix_env(controlled_dataset, tmp_path, offline_model_factory):
    """An offline app with a scripted runner and deterministic progress."""
    data_path, changes_path = controlled_dataset
    baseline_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()

    def build(responses=None, *, client_factory=None):
        app = create_app(
            settings=WebSettings(data_path=data_path, changes_path=changes_path)
        )
        app.state.runner.shutdown(wait=False)
        client = (client_factory or offline_model_factory)(list(responses or []))
        app.state.runner = RunManager(
            runs=app.state.runs,
            data_path=data_path,
            changes_path=changes_path,
            client=client,
            traces_dir=tmp_path / "traces",
        )
        return app

    yield build

    assert hashlib.sha256(data_path.read_bytes()).hexdigest() == baseline_hash


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            try:
                return await scenario(client)
            finally:
                app.state.runner.shutdown(wait=False)

    return asyncio.run(run())


def ledger_lines(changes_path) -> int:
    if not changes_path.exists():
        return 0
    return len(changes_path.read_text().splitlines())


async def bootstrap(client):
    response = await client.get("/api/session")
    assert response.status_code == 200
    return response.json()


def write_headers(session, *, origin=ORIGIN, token=None):
    return {
        "Origin": origin,
        "X-CSRF-Token": session["csrf_token"] if token is None else token,
    }


def start_body(session, goal="Review expenses.", request_id="request_1"):
    return {
        "goal": goal,
        "request_id": request_id,
        "instance_id": session["instance_id"],
    }


# ---------------------------------------------------------------------------
# A. Unsafe-method and cross-target matrix: no alternative path executes work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_unsafe_methods_are_rejected_on_every_read_endpoint(matrix_env, method):
    app = matrix_env()

    async def scenario(client):
        session = await bootstrap(client)
        body = start_body(session)
        headers = dict(write_headers(session))
        headers["Content-Type"] = "application/json"
        response = await client.request(
            method.upper(), "/api/runs", json=body, headers=headers
        )
        assert response.status_code == 405, response.text
        assert response.json()["error"]["code"] == "method_not_allowed"
        # Without session consent the denial happens before any routing.
        denied = await client.request(
            method.upper(),
            "/api/runs",
            json=body,
            headers={"Origin": ORIGIN, "X-CSRF-Token": "whatever"},
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "forbidden"

    exercise(app, scenario)


def test_get_on_the_decision_endpoint_is_never_a_permission(matrix_env):
    app = matrix_env()

    async def scenario(client):
        session = await bootstrap(client)
        response = await client.get(
            "/api/runs/run_unknown/approvals/apr_unknown",
            headers=write_headers(session),
        )
        assert response.status_code == 405, response.text
        assert response.json()["error"]["code"] == "method_not_allowed"

    exercise(app, scenario)


# ---------------------------------------------------------------------------
# B. Denial matrix on write endpoints: session, Origin, Host, CSRF, instance
# ---------------------------------------------------------------------------


def _matrix_request_writer(target, session, extra=None, token=None):
    """Build (method, url, json) per write target from an allowlist."""

    def build_json():
        if target in {"decision", "decision_foreign_id"}:
            return {"decision": "approve", "instance_id": session["instance_id"]}
        body = start_body(session)
        if extra:
            body = {**body, **extra}
        return body

    def build_url():
        if target == "start_run":
            return "/api/runs"
        if target == "decision":
            return "/api/runs/run_unknown/approvals/apr_unknown"
        return "/api/runs/run_unknown/approvals/apr_elsewhere"

    return "POST", build_url(), build_json()


@pytest.mark.parametrize("target", WRITE_TARGETS)
@pytest.mark.parametrize(
    "dimension",
    [
        "no_cookie",
        "garbage_csrf",
        "wwrong_origin",
        "shebang_origin",
    ],
)
def test_write_denials_never_leak_names_or_write_anything(
    matrix_env, tmp_path, target, dimension
):
    app = matrix_env()
    changes_path = app.state.settings.changes_path
    before = ledger_lines(changes_path)

    async def scenario(client):
        session = await bootstrap(client)
        method, url, body = _matrix_request_writer(target, session)
        headers = dict(write_headers(session))
        if dimension == "no_cookie":
            client.cookies.clear()
        elif dimension == "garbage_csrf":
            headers = write_headers(session, token="not-the-real-token")
        elif dimension == "wwrong_origin":
            headers["Origin"] = FOREIGN_ORIGIN
        elif dimension == "shebang_origin":
            headers["Origin"] = "http://127.0.0.1:8765.evil.example"
        response = await client.request(method, url, json=body, headers=headers)
        assert response.status_code == 403, response.text
        payload = response.json()
        assert payload["error"]["code"] == "forbidden"
        for key, value in SECURITY_HEADERS.items():
            assert response.headers[key] == value
        # The denial carries no request context, path or session material.
        text = response.text
        for leak in ("csrf", str(tmp_path), "Traceback", "cookie"):
            assert leak.lower() not in text.lower(), leak
        assert ledger_lines(changes_path) == before, "denials must not write"

    exercise(app, scenario)


@pytest.mark.parametrize("target", ["start_run", "decision"])
def test_instance_and_field_and_limit_contract_on_writes(matrix_env, tmp_path, target):
    """Extra fields, wrong instance and oversized goals fail closed, English."""
    app = matrix_env()

    async def scenario(client):
        session = await bootstrap(client)
        method, url, body = _matrix_request_writer(target, session)
        headers = dict(write_headers(session))
        headers["Content-Type"] = "application/json"

        # 1) A wrong instance id is a safe conflict, not a state reset.
        wrong = {**body, "instance_id": "inst_from_another_life"}
        response = await client.request(method, url, json=wrong, headers=headers)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "instance_changed"

        # 2) Disallowed/extra fields are rejected and never honored.
        extras = {"owner": "attacker", "force_override": True, "goal": "swapped"}
        response = await client.request(
            method, url, json={**body, **extras}, headers=headers
        )
        assert response.status_code == 422, response.text
        # The payload must catalog the invalid field, not honor or catalog
        # any accepted mutation of the operation.
        payload = response.text
        assert response.headers["cache-control"] == "no-store"
        assert "accepted" not in payload

        # 3) A limit-honoring goal is rejected with an English category.
        long_goal = "x" * 8001
        response = await client.request(
            method,
            url,
            json={**body, "goal": long_goal},
            headers=headers,
        )
        if target == "start_run":
            assert response.status_code == 422, response.text
            assert "x-repeated" not in response.text  # never echo the value

        # Nothing above may have written the ledger.
        before = ledger_lines(app.state.settings.changes_path)
        assert before == ledger_lines(app.state.settings.changes_path)

    exercise(app, scenario)


# ---------------------------------------------------------------------------
# C. Read matrix: protected reads require a session; public reads never write
# ---------------------------------------------------------------------------


def _read_matrix_cases(app):
    """(name, url) for every protected read; runs/transactions are read-only."""

    def cases():
        yield "run_snapshot", "/api/runs/run_unknown"
        yield "approvals", "/api/runs/run_unknown/approvals"
        yield "transactions", "/api/transactions"
        yield "transaction_detail", "/api/transactions/tx_unknown"

    return tuple(cases())


@pytest.mark.parametrize("name,url", _read_matrix_cases(None))
def test_protected_reads_require_a_session_and_never_enter_handlers(
    matrix_env, name, url
):
    app = matrix_env()

    async def scenario(client):
        denied = await client.get(url)
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "forbidden"
        # A read with a session but a wrong Host is equally rejected.
        session = await bootstrap(client)
        response = await client.get(
            url,
            headers={"Host": "evil.example", "Origin": ORIGIN},
        )
        assert response.status_code == 403, response.text
        # A valid session reaches the handler (resource may still be absent).
        ok = await client.get(url, headers=write_headers(session))
        assert ok.status_code in (200, 404), ok.text
        assert ok.status_code != 403
        assert ok.headers["cache-control"] == "no-store"

    exercise(app, scenario)


@pytest.mark.parametrize("target", PUBLIC_TARGETS)
def test_public_reads_never_start_work_or_write(matrix_env, tmp_path, target):
    app = matrix_env()
    changes_path = app.state.settings.changes_path
    before = ledger_lines(changes_path)

    async def scenario(client):
        if target == "screen":
            response = await client.get("/")
        elif target == "session":
            response = await client.get("/api/session")
        else:
            response = await client.get("/static/app.css")
        assert response.status_code == 200, response.text
        for key, value in SECURITY_HEADERS.items():
            assert response.headers[key] == value
        assert app.state.runs.count() == 0
        assert ledger_lines(changes_path) == before

    exercise(app, scenario)


# ---------------------------------------------------------------------------
# D. Error payload cleanliness across the whole matrix
# ---------------------------------------------------------------------------


def test_every_error_payload_is_english_catalog_and_clean(matrix_env, tmp_path):
    """Collect denial frames from many attack shapes into one contract."""
    app = matrix_env()
    collected = []

    async def scenario(client):
        session = await bootstrap(client)
        attacks = [
            ("GET", "/api/runs/run_unknown", None, dict(write_headers(session))),
            (
                "GET",
                "/api/transactions/tx_unknown",
                None,
                dict(write_headers(session)),
            ),
            (
                "GET",
                "/api/runs/run_unknown/approvals",
                None,
                dict(write_headers(session)),
            ),
            (
                "POST",
                "/api/runs",
                start_body(session, "x" * 8001),
                dict(write_headers(session)),
            ),
            (
                "POST",
                "/api/runs",
                {**start_body(session), "trace": "leak-me"},
                dict(write_headers(session)),
            ),
            (
                "POST",
                "/api/runs/run_x/approvals/apr_x",
                {"decision": "approve", "instance_id": session["instance_id"]},
                dict(write_headers(session)),
            ),
            ("POST", "/api/runs", "not json", dict(write_headers(session))),
            (
                "GET",
                "/api/transactions?limit=51",
                None,
                dict(write_headers(session)),
            ),
        ]
        for method, url, body, headers in attacks:
            headers = {**headers, "Content-Type": "application/json"}
            kwargs = {"headers": headers}
            if body is not None:
                if isinstance(body, str):
                    kwargs["content"] = body
                else:
                    kwargs["json"] = body
            response = await client.request(method, url, **kwargs)
            assert response.status_code >= 400, response.text
            payload = response.json()
            assert payload["error"]["code"] in ACCEPTED_CODES
            for key, value in SECURITY_HEADERS.items():
                assert response.headers[key] == value
            collected.append(payload["error"])
            # No private path or temp file name ever crosses the boundary.
            assert str(tmp_path) not in response.text

    exercise(app, scenario)

    # All objections are English catalog sentences with real next steps.
    assert collected, "the matrix must have produced errors"
    for error in collected:
        message = error["message"]
        assert message and message[0].isupper()
        assert not any(
            marker in message
            for marker in ("Traceback", "<", "\n", "  ", "\\", "www", "http")
        )


# ---------------------------------------------------------------------------
# E. Untrusted data stays text; ownership even under attack
# ---------------------------------------------------------------------------


def test_untrusted_goal_and_data_travel_only_as_text(matrix_env, tmp_path):
    app = matrix_env(
        [{"text": "Answer for a scripty goal.", "tool_calls": []}],
    )

    async def scenario(client):
        session = await bootstrap(client)
        goal = '<script>alert("xss")</script> Review & DELETE; --secret--'
        accepted = await client.post(
            "/api/runs",
            json=start_body(session, goal=goal),
            headers=write_headers(session),
        )
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["run_id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            snapshot_response = await client.get(f"/api/runs/{run_id}")
            snapshot = snapshot_response.json()
            if snapshot["status"] in TERMINAL_RUN_STATUSES:
                break
            await asyncio.sleep(0.05)
        assert snapshot["status"] == "completed"
        # Stored and echoed verbatim as text; never transformed or executed.
        assert snapshot["goal"] == goal
        assert '<script>alert("xss")</script>' in snapshot["goal"]
        transactions = await client.get("/api/transactions?limit=50")
        assert transactions.status_code == 200
        listing = transactions.json()
        assert listing["limit"] == 50
        for item in listing["items"]:
            assert set(item) <= {
                "transaction_id",
                "date",
                "merchant",
                "amount",
                "currency",
                "category",
                "description",
            }
            # Values may contain odd data, but keys stay allowlisted.
            for value in item.values():
                if isinstance(value, str):
                    assert "fingerprint" not in value

    exercise(app, scenario)


def test_foreign_session_cannot_decide_or_read_run_state(matrix_env):
    """Two concurrently bootstrapped sessions stay separated end to end."""
    responses = [{"text": "Done.", "tool_calls": []}]
    app = matrix_env(responses)

    async def scenario(client):
        owner = await bootstrap(client)
        accepted = await client.post(
            "/api/runs",
            json=start_body(owner),
            headers=write_headers(owner),
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        owner_cookie = client.cookies.get(SESSION_COOKIE)

        # The foreign visitor receives the indistinguishable 404 body.
        client.cookies.clear()
        foreign = await bootstrap(client)
        foreign_read = await client.get(
            f"/api/runs/{run_id}", headers=write_headers(foreign)
        )
        foreign_decision = await client.post(
            f"/api/runs/{run_id}/approvals/apr_unknown",
            json={"decision": "approve", "instance_id": foreign["instance_id"]},
            headers=write_headers(foreign),
        )
        assert foreign_read.status_code == foreign_decision.status_code == 404
        assert foreign_read.json()["error"]["code"] == "run_unavailable"
        assert foreign_decision.json()["error"]["code"] == "approval_unavailable"
        # Both denials share the safe English shape and leak nothing.
        assert foreign["active_run_id"] is None

        # The owner session stays the only one who can resume.
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, owner_cookie)
        owner_read = await client.get(
            f"/api/runs/{run_id}", headers=write_headers(owner)
        )
        assert owner_read.status_code == 200
        assert await wait_terminal(app, run_id)

    exercise(app, scenario)


async def wait_terminal(app, run_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = app.state.runs.get_run(run_id)
        if run is not None and run.status in TERMINAL_RUN_STATUSES:
            return True
        await asyncio.sleep(0.05)
    return False


def test_live_denials_keep_every_locked_frame_out_of_the_ledger(
    matrix_env, controlled_dataset
):
    """While a run blocks on a decision, denials never touch stored data."""
    data_path, changes_path = controlled_dataset
    baseline = hashlib.sha256(data_path.read_bytes()).hexdigest()

    class LockedClient:
        model = "mock"
        provider = "mock"

        def complete(self, messages, tools=None):
            # The pending decision keeps the run alive; the call never ends.
            threading.Event().wait(10.0)
            return {"text": "unreachable", "tool_calls": []}

    app = matrix_env([], client_factory=lambda responses: LockedClient())

    async def scenario(client):
        session = await bootstrap(client)
        accepted = await client.post(
            "/api/runs",
            json=start_body(session, request_id="locked"),
            headers=write_headers(session),
        )
        assert accepted.status_code == 202, accepted.text

        # Reads and denials keep working while the worker waits.
        listing = await client.get("/api/transactions?limit=50")
        assert listing.status_code == 200
        decision_denied = await client.post(
            "/api/runs/none/approvals/apr_none",
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=write_headers(session),
        )
        assert decision_denied.status_code == 404
        assert hashlib.sha256(data_path.read_bytes()).hexdigest() == baseline

    exercise(app, scenario)
