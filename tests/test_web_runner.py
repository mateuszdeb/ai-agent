"""Tests for T-08: RunManager – single-worker pool, error mapping, non-blocking HTTP."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from agent_project.state_lock import LedgerWriteError
from agent_project.tracing import TraceRecord
from agent_project.web import WebSettings, create_app
from agent_project.web.runner import RunManager, build_tool_observation
from agent_project.web.runs import RunInProgressError, RunStore
from agent_project.web.schemas import ToolObservation
from agent_project.web.sessions import SESSION_COOKIE


class BlockingMockClient:
    """Mock client that blocks until an event is set, simulating a slow model."""

    def __init__(self, answer: str = "Analysis complete.") -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.answer = answer

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.entered.set()
        self.release.wait(timeout=5.0)
        return {
            "text": self.answer,
            "tool_calls": [],
            "latency_ms": 50,
            "usage": {"cost": 0.0},
        }


class SteppingMockClient:
    """Mock client that returns tool calls up to step budget."""

    def __init__(self) -> None:
        self.call_count = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.call_count += 1
        return {
            "text": None,
            "tool_calls": [
                {
                    "id": f"call_{self.call_count}",
                    "name": "search_transactions",
                    "arguments": {"query": "Coffee"},
                }
            ],
            "latency_ms": 10,
            "usage": {"cost": 0.0},
        }


class ErrorMockClient:
    """Mock client that raises an exception during generation."""

    def __init__(self, exception: Exception) -> None:
        self.exception = exception

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raise self.exception


def test_build_tool_observation_success():
    """Verify build_tool_observation converts trace data into safe public DTO."""
    record: TraceRecord = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": "tool_result",
        "payload": {
            "step": 2,
            "tool_name": "assign_category",
            "result": {
                "status": "success",
                "action_id": "act_123",
                "transaction_id": "tx_456",
                "category": "Groceries",
                "previous_category": "Uncategorized",
                "count": 1,
            },
        },
    }
    obs = build_tool_observation(1, record)
    assert isinstance(obs, ToolObservation)
    assert obs.sequence == 1
    assert obs.step == 2
    assert obs.tool == "assign_category"
    assert obs.status == "success"
    assert obs.action_id == "act_123"
    assert obs.data is not None
    assert obs.data.transaction_id == "tx_456"
    assert obs.data.category == "Groceries"
    assert obs.data.previous_category == "Uncategorized"
    assert obs.data.count == 1


def test_build_tool_observation_with_transaction():
    """Verify build_tool_observation handles full transaction details."""
    record: TraceRecord = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": "tool_result",
        "payload": {
            "step": 1,
            "tool_name": "get_transaction_details",
            "result": {
                "status": "success",
                "transaction": {
                    "id": "tx_789",
                    "date": "2026-10-01",
                    "merchant": "Coffee Shop",
                    "amount": 15.50,
                    "currency": "PLN",
                    "category": "Dining",
                },
            },
        },
    }
    obs = build_tool_observation(1, record)
    assert obs.status == "success"
    assert obs.data is not None
    assert obs.data.transaction is not None
    assert obs.data.transaction.transaction_id == "tx_789"
    assert obs.data.transaction.merchant == "Coffee Shop"
    assert obs.data.transaction.amount == 15.50


def test_single_worker_slot_and_no_hidden_queue(tmp_path, controlled_clock):
    """At most one analysis runs; concurrent attempts fail immediately with no queue."""
    runs = RunStore(clock=controlled_clock)
    slow_client = BlockingMockClient(answer="Done.")
    runner = RunManager(
        runs=runs,
        client=slow_client,
        traces_dir=tmp_path / "traces",
    )

    try:
        # Start first run (session A)
        run1, created1 = runner.submit_run(
            owner_session_id="sess_a",
            request_id="req_1",
            goal="First slow analysis",
        )
        assert created1 is True
        assert run1.status == "running"

        # Wait until worker enters the model call
        assert slow_client.entered.wait(timeout=2.0)
        assert runner.is_busy() is True

        # Second run from Session B while A is running: immediately rejected!
        with pytest.raises(RunInProgressError, match="already in progress"):
            runner.submit_run(
                owner_session_id="sess_b",
                request_id="req_2",
                goal="Second analysis while busy",
            )

        # Second run from Session A (different request_id): also rejected!
        with pytest.raises(RunInProgressError, match="already in progress"):
            runner.submit_run(
                owner_session_id="sess_a",
                request_id="req_3",
                goal="Another goal from session A",
            )

        # Idempotent replay: same session, same request_id → returns existing run
        run_replay, created_replay = runner.submit_run(
            owner_session_id="sess_a",
            request_id="req_1",
            goal="First slow analysis",
        )
        assert created_replay is False
        assert run_replay.run_id == run1.run_id

        # Release first run
        slow_client.release.set()
        assert runner.wait_for_active_run(timeout=2.0) is True

        # Verify first run completed successfully
        updated1 = runs.get_run(run1.run_id)
        assert updated1.status == "completed"
        assert updated1.result == "Done."
        assert runner.is_busy() is False

        # Now that slot is freed, Session B can start
        fast_client = BlockingMockClient(answer="Session B finished.")
        runner._client = fast_client
        run2, created2 = runner.submit_run(
            owner_session_id="sess_b",
            request_id="req_2",
            goal="Second analysis after release",
        )
        assert created2 is True
        fast_client.release.set()
        assert runner.wait_for_active_run(timeout=2.0) is True

        updated2 = runs.get_run(run2.run_id)
        assert updated2.status == "completed"
        assert updated2.result == "Session B finished."

    finally:
        slow_client.release.set()
        runner.shutdown(wait=False)


def test_http_reads_not_blocked_during_slow_run(controlled_dataset, tmp_path):
    """HTTP GET /api/session responds immediately while the agent worker is busy."""
    data_path, changes_path = controlled_dataset
    app = create_app(
        settings=WebSettings(data_path=data_path, changes_path=changes_path)
    )

    slow_client = BlockingMockClient(answer="Completed analysis.")
    app.state.runner._client = slow_client
    # The run trace belongs with this test's isolated dataset.
    app.state.runner._traces_dir = tmp_path / "traces"

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
        ) as client:
            # 1. Bootstrap session
            resp1 = await client.get("/api/session")
            assert resp1.status_code == 200
            session_cookie = resp1.cookies[SESSION_COOKIE]

            # 2. Start slow run in worker
            run, created = app.state.runner.submit_run(
                owner_session_id=session_cookie,
                request_id="req_slow",
                goal="Review slow expenses",
            )
            assert created is True
            assert slow_client.entered.wait(timeout=2.0) is True

            # 3. HTTP GET /api/session responds immediately during execution (< 0.2s)
            start_t = time.perf_counter()
            resp2 = await client.get("/api/session")
            elapsed = time.perf_counter() - start_t
            assert elapsed < 0.2
            assert resp2.status_code == 200
            assert resp2.json()["active_run_id"] == run.run_id

            # 4. Release model worker
            slow_client.release.set()
            assert app.state.runner.wait_for_active_run(timeout=2.0) is True

            # 5. Verify run completed
            resp3 = await client.get("/api/session")
            assert resp3.status_code == 200
            assert resp3.json()["active_run_id"] is None

    try:
        asyncio.run(scenario())
    finally:
        slow_client.release.set()
        app.state.runner.shutdown(wait=False)


def test_tool_observations_and_completion(tmp_path, controlled_dataset):
    """Tool executions in worker publish ToolObservation records to RunStore."""
    data_path, changes_path = controlled_dataset
    runs = RunStore()

    class SingleToolMockClient:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools=None):
            self.step += 1
            if self.step == 1:
                return {
                    "text": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "search_transactions",
                            "arguments": {"query": "Market"},
                        }
                    ],
                    "latency_ms": 10,
                    "usage": {"cost": 0.0},
                }
            return {
                "text": "Found market transactions.",
                "tool_calls": [],
                "latency_ms": 10,
                "usage": {"cost": 0.0},
            }

    runner = RunManager(
        runs=runs,
        client=SingleToolMockClient(),
        data_path=data_path,
        changes_path=changes_path,
        traces_dir=tmp_path / "traces",
    )

    try:
        run, _ = runner.submit_run(
            owner_session_id="sess_1",
            request_id="req_tool",
            goal="Find market items",
        )
        assert runner.wait_for_active_run(timeout=2.0) is True

        completed_run = runs.get_run(run.run_id)
        assert completed_run.status == "completed"
        assert completed_run.result == "Found market transactions."
        assert len(completed_run.observations) == 1
        obs = completed_run.observations[0]
        assert obs.tool == "search_transactions"
        assert obs.status == "success"
        assert obs.sequence == 1
        assert obs.step == 1
        assert obs.data is not None
        assert obs.data.count is not None
        assert obs.data.count >= 1
    finally:
        runner.shutdown(wait=False)


def test_step_limit_reached(tmp_path, controlled_dataset):
    """Exhausting max_steps halts execution with step_limit_reached status."""
    data_path, changes_path = controlled_dataset
    runs = RunStore()
    stepping_client = SteppingMockClient()
    runner = RunManager(
        runs=runs,
        client=stepping_client,
        data_path=data_path,
        changes_path=changes_path,
        max_steps=2,
        traces_dir=tmp_path / "traces",
    )

    try:
        run, _ = runner.submit_run(
            owner_session_id="sess_1",
            request_id="req_step_limit",
            goal="Loop forever",
        )
        assert runner.wait_for_active_run(timeout=2.0) is True

        finished_run = runs.get_run(run.run_id)
        assert finished_run.status == "step_limit_reached"
        assert len(finished_run.observations) == 2
        assert runner.is_busy() is False
    finally:
        runner.shutdown(wait=False)


@pytest.mark.parametrize(
    ("exception", "expected_code", "expected_status"),
    [
        (TimeoutError("Model timed out"), "model_timeout", "failed"),
        (ConnectionError("Connection refused"), "model_unavailable", "failed"),
        (
            LedgerWriteError("Disk failed during fsync"),
            "outcome_unknown",
            "outcome_unknown",
        ),
        (RuntimeError("Unexpected fault"), "internal_error", "failed"),
    ],
)
def test_worker_exception_mapping(tmp_path, exception, expected_code, expected_status):
    """Worker exceptions are mapped to terminal status and safe error codes."""
    runs = RunStore()
    error_client = ErrorMockClient(exception)
    runner = RunManager(
        runs=runs,
        client=error_client,
        traces_dir=tmp_path / "traces",
    )

    try:
        run, _ = runner.submit_run(
            owner_session_id="sess_1",
            request_id="req_err",
            goal="Test error mapping",
        )
        assert runner.wait_for_active_run(timeout=2.0) is True

        finished_run = runs.get_run(run.run_id)
        assert finished_run.status == expected_status
        assert finished_run.error is not None
        assert finished_run.error.code == expected_code
        # Active slot is freed
        assert runner.is_busy() is False
    finally:
        runner.shutdown(wait=False)
