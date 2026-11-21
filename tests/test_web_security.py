"""T-06: local HTTP security and lifetime exclusion, using in-process ASGI."""

import asyncio
import importlib
import json
import subprocess
import sys

import httpx
import pytest
from fastapi import Request
from starlette.exceptions import HTTPException

from agent_project.state_lock import LedgerGuard
from agent_project.web import WebSettings, create_app
from agent_project.web.instance_lock import WebInstanceBusyError, WebInstanceLock
from agent_project.web.schemas import SessionResponse, StartRunRequest
from agent_project.web.security import MAX_REQUEST_BYTES, SECURITY_HEADERS
from agent_project.web.sessions import (
    SESSION_COOKIE,
    SessionCapacityError,
    SessionStore,
)

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def secured_app(controlled_dataset):
    data_path, changes_path = controlled_dataset
    app = create_app(
        settings=WebSettings(data_path=data_path, changes_path=changes_path)
    )
    app.state.calls = []

    @app.post("/api/probe")
    async def probe(body: StartRunRequest, request: Request):
        app.state.calls.append(request.state.browser_session.session_id)
        return {"ok": True}

    @app.get("/api/probe")
    async def read_probe(request: Request):
        assert request.state.browser_session is not None
        return {"ok": True}

    return app


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
        ) as client:
            return await scenario(client)

    return asyncio.run(run())


async def bootstrap(client):
    response = await client.get("/api/session")
    assert response.status_code == 200, response.text
    return response, response.json()


def write_headers(session):
    return {"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]}


def write_body(session):
    return {
        "goal": "Review expenses.",
        "request_id": "request_1",
        "instance_id": session["instance_id"],
    }


def assert_protected(response):
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value
    assert not any(key.startswith("access-control-") for key in response.headers)


def test_bootstrap_private_cookie_stable_session_and_clean_public_dto(secured_app):
    async def scenario(client):
        response, session = await bootstrap(client)
        SessionResponse.model_validate_json(response.content)
        assert set(session) == {"csrf_token", "instance_id", "active_run_id"}
        assert session["active_run_id"] is None
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/" in cookie
        assert "Secure" not in cookie
        assert "Domain=" not in cookie
        assert client.cookies[SESSION_COOKIE] not in response.text
        assert len(session["csrf_token"]) >= 32
        assert len(session["instance_id"]) >= 32
        again, second = await bootstrap(client)
        assert second == session
        assert "set-cookie" not in again.headers
        assert_protected(response)
        assert_protected(again)

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


def test_valid_same_origin_json_is_replayed_to_strict_endpoint(secured_app):
    async def scenario(client):
        _, session = await bootstrap(client)
        response = await client.post(
            "/api/probe",
            json=write_body(session),
            headers=write_headers(session),
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}
        assert_protected(response)
        assert (await client.get("/api/probe")).status_code == 200

    exercise(secured_app, scenario)
    assert len(secured_app.state.calls) == 1


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://evil.example",
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:9000",
        "http://127.0.0.1:8765/",
        "http://127.0.0.1:8765@evil.example",
    ],
)
def test_unsafe_origin_is_exact_and_required(secured_app, origin):
    async def scenario(client):
        _, session = await bootstrap(client)
        headers = {"X-CSRF-Token": session["csrf_token"]}
        if origin is not None:
            headers["Origin"] = origin
        response = await client.post(
            "/api/probe", json=write_body(session), headers=headers
        )
        assert response.status_code == 403
        assert_protected(response)

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


@pytest.mark.parametrize(
    "host",
    [
        "",
        "localhost:8765",
        "evil.example",
        "127.0.0.1",
        "127.0.0.1:9000",
        "127.0.0.1.evil.example:8765",
        "[::1]:8765",
    ],
)
def test_host_rebinding_is_rejected_before_session_creation(secured_app, host):
    async def scenario(client):
        response = await client.get(
            "/api/session",
            headers={
                "Host": host,
                "X-Forwarded-Host": "127.0.0.1:8765",
            },
        )
        assert response.status_code == 403
        assert "set-cookie" not in response.headers
        assert_protected(response)

    exercise(secured_app, scenario)


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_missing_or_wrong_csrf_rejects_without_calling_handler(secured_app, token):
    async def scenario(client):
        _, session = await bootstrap(client)
        headers = {"Origin": ORIGIN}
        if token is not None:
            headers["X-CSRF-Token"] = token
        response = await client.post(
            "/api/probe", json=write_body(session), headers=headers
        )
        assert response.status_code == 403
        assert_protected(response)

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


def test_non_ascii_csrf_is_forbidden_not_an_internal_error(secured_app):
    async def scenario(client):
        _, session = await bootstrap(client)
        response = await client.post(
            "/api/probe",
            json=write_body(session),
            headers=[(b"origin", ORIGIN.encode()), (b"x-csrf-token", b"\xff")],
        )
        assert response.status_code == 403
        assert_protected(response)

    exercise(secured_app, scenario)


@pytest.mark.parametrize("header", ["host", "origin", "x-csrf-token", "cookie"])
def test_ambiguous_security_headers_fail_closed(secured_app, header):
    async def scenario(client):
        _, session = await bootstrap(client)
        values = {
            "host": "127.0.0.1:8765",
            "origin": ORIGIN,
            "x-csrf-token": session["csrf_token"],
            "cookie": f"{SESSION_COOKIE}={client.cookies[SESSION_COOKIE]}",
        }
        headers = [(key, value) for key, value in values.items()]
        headers.append((header, values[header]))
        response = await client.post(
            "/api/probe", json=write_body(session), headers=headers
        )
        assert response.status_code == 403

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "application/merge-patch+json",
    ],
)
def test_writes_require_json_content_type(secured_app, content_type):
    async def scenario(client):
        _, session = await bootstrap(client)
        headers = write_headers(session)
        if content_type is not None:
            headers["Content-Type"] = content_type
        response = await client.post(
            "/api/probe",
            content=json.dumps(write_body(session)),
            headers=headers,
        )
        assert response.status_code == 422
        assert_protected(response)

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


@pytest.mark.parametrize(
    "body",
    [
        "{",
        "null",
        "[]",
        '{"instance_id":true}',
        '{"goal":NaN}',
        '{"instance_id":"a","instance_id":"b"}',
    ],
)
def test_invalid_json_is_not_forwarded_or_echoed(secured_app, body):
    async def scenario(client):
        _, session = await bootstrap(client)
        response = await client.post(
            "/api/probe",
            content=body,
            headers={**write_headers(session), "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


def test_current_instance_is_required_before_execution(secured_app):
    async def scenario(client):
        _, session = await bootstrap(client)
        response = await client.post(
            "/api/probe",
            json={**write_body(session), "instance_id": "previous_instance"},
            headers=write_headers(session),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "instance_changed"

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


def test_unauthenticated_requests_and_another_sessions_csrf_cannot_execute(secured_app):
    async def scenario(client):
        response, session_a = await bootstrap(client)
        cookie_a = client.cookies[SESSION_COOKIE]
        client.cookies.clear()
        unauthenticated = await client.post(
            "/api/probe",
            json=write_body(session_a),
            headers=write_headers(session_a),
        )
        assert unauthenticated.status_code == 403
        assert (await client.get("/api/probe")).status_code == 403
        _, session_b = await bootstrap(client)
        assert session_a["csrf_token"] != session_b["csrf_token"]
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, cookie_a)
        response = await client.post(
            "/api/probe",
            json=write_body(session_a),
            headers=write_headers(session_b),
        )
        assert response.status_code == 403

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_origin_bootstrap_cannot_create_session(secured_app, headers):
    async def scenario(client):
        response = await client.get("/api/session", headers=headers)
        assert response.status_code == 403
        assert "set-cookie" not in response.headers
        assert_protected(response)

    exercise(secured_app, scenario)


def test_cross_origin_preflight_has_no_cors_permissions(secured_app):
    async def scenario(client):
        response = await client.options(
            "/api/probe",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 403
        assert_protected(response)

    exercise(secured_app, scenario)


def test_body_limit_and_validation_errors_hide_request_inputs(secured_app):
    async def scenario(client):
        _, session = await bootstrap(client)
        for goal in ["PRIVATE_INPUT" * 700, "x" * (MAX_REQUEST_BYTES + 1)]:
            response = await client.post(
                "/api/probe",
                json={**write_body(session), "goal": goal},
                headers=write_headers(session),
            )
            assert response.status_code == 422
            assert "PRIVATE_INPUT" not in response.text
            assert "input" not in response.json()["error"]
            assert_protected(response)

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


@pytest.mark.parametrize("exception_kind", ["runtime", "http"])
def test_exceptions_are_safe_json_with_security_headers(secured_app, exception_kind):
    @secured_app.get("/failure")
    async def fail():
        if exception_kind == "http":
            raise HTTPException(500, detail="SECRET /Users/private/.env")
        raise RuntimeError("SECRET /Users/private/.env")

    async def scenario(client):
        response = await client.get("/failure")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "SECRET" not in response.text
        assert "/Users" not in response.text
        assert_protected(response)

    exercise(secured_app, scenario)


def test_static_root_rejects_traversal_dotfiles_and_external_symlinks(
    controlled_dataset, tmp_path, monkeypatch
):
    root = tmp_path / "ui"
    root.mkdir()
    (root / "app.css").write_text("body { color: black; }")
    (root / ".hidden").write_text("PRIVATE_MARKER")
    outside = tmp_path / ".env"
    outside.write_text("PRIVATE_MARKER")
    (root / "leak.txt").symlink_to(outside)
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    app_module = importlib.import_module("agent_project.web.app")
    monkeypatch.setattr(app_module, "STATIC_ROOT", root)
    data_path, changes_path = controlled_dataset
    app = create_app(
        settings=WebSettings(data_path=data_path, changes_path=changes_path)
    )

    async def scenario(client):
        public = await client.get("/static/app.css")
        assert public.status_code == 200
        assert_protected(public)
        for path in [
            "/.env",
            "/data/transactions.json",
            "/traces/agent.jsonl",
            "/static/.hidden",
            "/static/.gitkeep",
            "/static/../.env",
            "/static/%2e%2e/.env",
            "/static/%252e%252e/.env",
            "/static/leak.txt",
            "/static/outside/.env",
            "/src/agent_project/agent.py",
        ]:
            response = await client.get(path)
            assert response.status_code == 404, path
            assert "PRIVATE_MARKER" not in response.text
            assert_protected(response)

    exercise(app, scenario)


def test_session_store_is_bounded_expiring_and_resists_fixation():
    now = [0.0]
    store = SessionStore(ttl=10, capacity=1, clock=lambda: now[0])
    session, created = store.bootstrap("attacker_chosen_id")
    assert created
    assert session.session_id != "attacker_chosen_id"
    assert session.session_id != session.csrf_token
    with pytest.raises(SessionCapacityError):
        store.bootstrap(None)
    assert store.bootstrap(session.session_id) == (session, False)
    now[0] = 10
    assert store.get(session.session_id) is None
    replacement, created = store.bootstrap(session.session_id)
    assert created
    assert replacement.session_id != session.session_id
    assert replacement.csrf_token != session.csrf_token


def test_full_session_store_returns_safe_service_unavailable(secured_app):
    secured_app.state.sessions = SessionStore(capacity=1)
    secured_app.state.sessions.bootstrap(None)

    async def scenario(client):
        response = await client.get("/api/session")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "state_busy"
        assert "set-cookie" not in response.headers
        assert_protected(response)

    exercise(secured_app, scenario)


@pytest.mark.parametrize(
    "limits",
    [
        {"ttl": 0},
        {"ttl": float("nan")},
        {"ttl": float("inf")},
        {"ttl": True},
        {"capacity": 0},
        {"capacity": True},
        {"capacity": 1.5},
    ],
)
def test_session_store_rejects_invalid_limits(limits):
    with pytest.raises(ValueError):
        SessionStore(**limits)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_unsafe_method_enforces_origin_before_routing(secured_app, method):
    async def scenario(client):
        _, session = await bootstrap(client)
        response = await client.request(
            method,
            "/api/probe",
            json=write_body(session),
            headers={**write_headers(session), "Origin": "null"},
        )
        assert response.status_code == 403
        assert_protected(response)

    exercise(secured_app, scenario)
    assert secured_app.state.calls == []


def test_utf8_json_at_goal_boundary_and_invalid_encoding(secured_app):
    async def scenario(client):
        _, session = await bootstrap(client)
        valid = await client.post(
            "/api/probe",
            json={**write_body(session), "goal": "ą" * 8000},
            headers={
                **write_headers(session),
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        assert valid.status_code == 200
        escaped = json.dumps(
            {**write_body(session), "goal": "\U00010348" * 8000},
            ensure_ascii=True,
        )
        assert len(escaped) > 64 * 1024
        escaped_response = await client.post(
            "/api/probe",
            content=escaped,
            headers={**write_headers(session), "Content-Type": "application/json"},
        )
        assert escaped_response.status_code == 200
        invalid = await client.post(
            "/api/probe",
            content=b"\xff",
            headers={**write_headers(session), "Content-Type": "application/json"},
        )
        assert invalid.status_code == 422

    exercise(secured_app, scenario)
    assert len(secured_app.state.calls) == 2


def test_web_instance_lock_is_stable_separate_and_exclusive(controlled_dataset):
    _, ledger = controlled_dataset
    first = WebInstanceLock(ledger)
    with first, LedgerGuard(ledger, timeout=0):
        inode = first.path.stat().st_ino
        with pytest.raises(WebInstanceBusyError), WebInstanceLock(ledger):
            pass
        with WebInstanceLock(ledger.with_name("other.jsonl")):
            pass
    with WebInstanceLock(ledger):
        assert first.path.stat().st_ino == inode


def test_web_instance_lock_rejects_a_second_process(controlled_dataset):
    _, ledger = controlled_dataset
    script = """
import sys
from pathlib import Path
from agent_project.web.instance_lock import WebInstanceLock, WebInstanceBusyError
try:
    with WebInstanceLock(Path(sys.argv[1])):
        raise AssertionError("Second instance acquired the dataset")
except WebInstanceBusyError:
    print("blocked")
"""
    with WebInstanceLock(ledger):
        result = subprocess.run(
            [sys.executable, "-c", script, str(ledger)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "blocked"


def test_lifespan_exclusion_restart_nonce_and_session_invalidation(secured_app):
    second = create_app(settings=secured_app.state.settings)

    async def scenario(client):
        async with secured_app.router.lifespan_context(secured_app):
            _, before = await bootstrap(client)
            with pytest.raises(WebInstanceBusyError):
                async with second.router.lifespan_context(second):
                    pass
        async with secured_app.router.lifespan_context(secured_app):
            _, after = await bootstrap(client)
            assert before["instance_id"] != after["instance_id"]
            assert before["csrf_token"] != after["csrf_token"]
            response = await client.post(
                "/api/probe",
                json=write_body(before),
                headers=write_headers(after),
            )
            assert response.status_code == 409
        async with second.router.lifespan_context(second):
            pass

    exercise(secured_app, scenario)
