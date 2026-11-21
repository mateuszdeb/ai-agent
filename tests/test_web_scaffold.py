"""T-02: configuration and in-process app checks, never a listening server."""

import asyncio
import socket
import subprocess
import sys

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from agent_project.llm import OpenRouterClient
from agent_project.tools import DEFAULT_CHANGES_PATH, DEFAULT_DATA_PATH
from agent_project.web import WebSettings, create_app
from agent_project.web.__main__ import main


def get_in_process(app: FastAPI, path: str) -> httpx.Response:
    """Exercise ASGI directly without a listening socket or deprecated client."""

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"http://127.0.0.1:{app.state.settings.port}",
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


def test_default_settings_are_local_and_match_existing_data_paths():
    settings = WebSettings()
    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.workers == 1
    assert settings.reload is False
    assert settings.data_path == DEFAULT_DATA_PATH
    assert settings.changes_path == DEFAULT_CHANGES_PATH


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "localhost", "::1", "127.0.0.2", "example.com", None]
)
def test_noncanonical_hosts_are_rejected(host):
    with pytest.raises(ValidationError):
        WebSettings(host=host)


@pytest.mark.parametrize("port", [0, -1, 65536, True, "8765"])
def test_invalid_ports_are_rejected_without_coercion(port):
    with pytest.raises(ValidationError):
        WebSettings(port=port)


@pytest.mark.parametrize("workers", [0, 2, True, "1"])
def test_only_one_strict_integer_worker_is_allowed(workers):
    with pytest.raises(ValidationError):
        WebSettings(workers=workers)


@pytest.mark.parametrize("reload", [True, 1, "false"])
def test_reload_and_coerced_boolean_settings_are_rejected(reload):
    with pytest.raises(ValidationError):
        WebSettings(reload=reload)


def test_settings_are_frozen_and_reject_unknown_fields():
    settings = WebSettings()
    with pytest.raises(ValidationError):
        settings.port = 9000
    with pytest.raises(ValidationError):
        WebSettings(allow_remote=True)


def test_factory_uses_trusted_paths_without_creating_them(tmp_path):
    root = tmp_path / "not_created"
    settings = WebSettings(
        data_path=root / "baseline.json",
        changes_path=root / "ledger.jsonl",
        port=9000,
    )
    app = create_app(settings=settings)
    assert isinstance(app, FastAPI)
    assert app.state.settings is settings
    assert not root.exists()
    # The static screen is served through FileResponse, file paths only.
    screen = get_in_process(app, "/")
    assert screen.status_code == 200
    assert screen.headers["content-type"].startswith("text/html")
    assert not root.exists()


def test_factory_creates_independent_apps_and_validates_its_input():
    first, second = create_app(), create_app()
    assert first is not second
    assert first.state.settings is not second.state.settings
    with pytest.raises(TypeError, match="WebSettings"):
        create_app(settings={"host": "0.0.0.0"})


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_scaffold_has_no_public_contracts_or_documentation_routes(path):
    assert get_in_process(create_app(), path).status_code == 404


def test_root_route_is_the_application_screen_only():
    """`/` serves the static UI; API contracts stay under /api prefix."""
    response = get_in_process(create_app(), "/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_factory_does_not_construct_a_live_model_or_bind_a_socket(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Factory must not create a model or listen on a socket")

    monkeypatch.setattr(OpenRouterClient, "__init__", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    app = create_app()
    assert isinstance(app, FastAPI)
    assert {route.path for route in app.routes} == {
        "/api/session",
        "/api/runs",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/approvals",
        "/api/runs/{run_id}/approvals/{approval_id}",
        "/api/transactions",
        "/api/transactions/{transaction_id}",
        "/static",
        "/",
    }


def test_fresh_import_and_factory_do_not_connect_or_bind(tmp_path):
    script = """
import socket
def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected network activity")
socket.socket.connect = forbidden
socket.socket.bind = forbidden
from agent_project.web import create_app
assert {route.path for route in create_app().routes} == {
    "/api/session",
    "/api/runs",
    "/api/runs/{run_id}",
    "/api/runs/{run_id}/approvals",
    "/api/runs/{run_id}/approvals/{approval_id}",
    "/api/transactions",
    "/api/transactions/{transaction_id}",
    "/static",
    "/",
}
print("Factory is offline")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Factory is offline"


def test_module_help_works_outside_repository_and_does_not_start_server(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "agent_project.web", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "127.0.0.1" in result.stdout
    assert "8765" in result.stdout
    assert "CSRF" in result.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "args",
    [
        ["--host", "0.0.0.0"],
        ["--port", "0"],
        ["--port", "65536"],
        ["--workers", "2"],
        ["--reload"],
    ],
)
def test_cli_rejects_unsupported_configuration(args, capsys):
    with pytest.raises(SystemExit) as stopped:
        main(args)
    assert stopped.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_starts_only_loopback_single_worker_without_proxy_trust(monkeypatch):
    started = []

    def forbidden(*args, **kwargs):
        raise AssertionError("Test must not really bind a socket")

    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: started.append((app, kwargs))
    )
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    main([])
    app, settings = started[0]
    assert {route.path for route in app.routes} == {
        "/api/session",
        "/api/runs",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/approvals",
        "/api/runs/{run_id}/approvals/{approval_id}",
        "/api/transactions",
        "/api/transactions/{transaction_id}",
        "/static",
        "/",
    }
    assert settings == {
        "host": "127.0.0.1",
        "port": 8765,
        "workers": 1,
        "reload": False,
        "proxy_headers": False,
        "access_log": False,
        "server_header": False,
        "lifespan": "on",
    }
