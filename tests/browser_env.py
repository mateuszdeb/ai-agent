"""Standalone environment for the browser test suite (T-23).

This module is intentionally not named ``test_*`` so pytest never collects
it. It starts one real ``create_app`` on an isolated loopback port served by
uvicorn in a daemon thread, with an offline scripted model client behind the
backend and disposable temporary data. A test controls the expected model
answers through ``responses`` and closes everything explicitly: no process,
socket, or working ledger entry survives.
"""

from __future__ import annotations

import socket
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import uvicorn

from agent_project.llm import MockClient
from agent_project.web import WebSettings, create_app
from agent_project.web.runner import RunManager, trace_run_event
from agent_project.web.tool_executor import WebToolExecutor


class ScriptedOfflineClient(MockClient):
    """Offline scripted client whose complete calls are recorded for asserts."""

    model = "mock"
    provider = "mock"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(deepcopy(responses))
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(deepcopy({"messages": messages, "tools": tools}))
        return deepcopy(super().complete(messages, tools))


def free_loopback_port() -> int:
    """Pick one unused 127.0.0.1 port; the OS releases it before the server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _executor_factory_for(app):
    """Mirror the app's own decision-executor wiring (lifecycle traces)."""

    def factory(**kwargs):
        def on_registered(pending):
            # The lifecycle trace keeps only catalog-safe identifiers.
            trace_run_event(
                app.state.runs,
                kwargs["run_id"],
                "approval_raised",
                {
                    "approval_id": pending.approval_id,
                    "kind": pending.kind,
                    "tool": pending.tool,
                    "action_id": pending.action_id,
                    "transaction_id": pending.transaction.transaction_id,
                },
            )

        def on_resolution(receipt):
            trace_run_event(
                app.state.runs,
                kwargs["run_id"],
                "approval_resolved",
                {"approval_id": receipt.approval_id, "status": receipt.status},
            )

        return WebToolExecutor(
            approvals=app.state.approvals,
            data_path=app.state.settings.data_path,
            changes_path=app.state.settings.changes_path,
            run_id=kwargs["run_id"],
            owner_session_id=kwargs["owner_session_id"],
            on_awaiting_approval=kwargs["on_awaiting_approval"],
            on_resumed=kwargs["on_resumed"],
            on_registered=on_registered,
            on_resolution=on_resolution,
        )

    return factory


class BrowserEnv:
    """One locally served application fixture for headless browser tests."""

    def __init__(
        self,
        *,
        data_path: Path,
        changes_path: Path,
        tmp_root: Path,
        responses: list[dict[str, Any]] | None = None,
        client: MockClient | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.client = (
            client if client is not None else ScriptedOfflineClient(responses or [])
        )
        self.port = free_loopback_port()
        self.app = create_app(
            settings=WebSettings(
                data_path=data_path, changes_path=changes_path, port=self.port
            )
        )
        uv_config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(uv_config)
        self.thread = threading.Thread(
            target=self.server.run, name="browser-test-server", daemon=True
        )
        self.thread.start()
        self._wait_ready()
        # Lifespan startup runs reset_runtime, which rebuilds a default runner
        # with the local model client. The offline test runner is therefore
        # installed only after startup, wired to the live stores and to the
        # app's own decision-executor wiring.
        self.app.state.runner.shutdown(wait=False)
        runner_kwargs: dict[str, Any] = {
            "runs": self.app.state.runs,
            "data_path": self.app.state.settings.data_path,
            "changes_path": self.app.state.settings.changes_path,
            "client": self.client,
            "traces_dir": tmp_root / "traces",
            "tool_executor_factory": _executor_factory_for(self.app),
        }
        if max_steps is not None:
            runner_kwargs["max_steps"] = max_steps
        self.app.state.runner = RunManager(**runner_kwargs)
        self.url = f"http://127.0.0.1:{self.port}"

    @property
    def data_path(self) -> Path:
        return self.app.state.settings.data_path

    @property
    def changes_path(self) -> Path:
        return self.app.state.settings.changes_path

    def _wait_ready(self, timeout: float = 15.0) -> None:
        """Poll the uvicorn startup flag with a deadline, no random sleeps."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.server.started:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("The local server did not start in time.")

    def stop_serving(self) -> None:
        """Stop the HTTP server while keeping the app state introspectable."""
        self.server.should_exit = True
        self.thread.join(timeout=15.0)

    def close(self) -> None:
        """Stop the server and the worker pool; safe on a half-open state."""
        self.stop_serving()
        self.app.state.runner.shutdown(wait=False)

    def __enter__(self) -> BrowserEnv:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
