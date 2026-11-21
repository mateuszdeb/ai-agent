"""Tests for T-07: RunStore, request deduplication, session isolation, and retention."""

import concurrent.futures
from pathlib import Path

import httpx
import pytest

from agent_project.web import WebSettings, create_app
from agent_project.web.runs import (
    InvalidRunStateTransitionError,
    RequestIdConflictError,
    RunAccessDeniedError,
    RunInProgressError,
    RunNotFoundError,
    RunStore,
    compute_request_hash,
)
from agent_project.web.schemas import (
    ObservationData,
    RunError,
    RunReceipt,
    RunSnapshot,
    ToolObservation,
)
from agent_project.web.sessions import SESSION_COOKIE


def test_compute_request_hash():
    """Request hash is deterministic and sensitive to whitespace and content."""
    h1 = compute_request_hash("Find subscriptions")
    h2 = compute_request_hash("Find subscriptions")
    h3 = compute_request_hash("Find subscriptions ")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64


def test_start_run_deduplication(controlled_clock):
    """Identical request ID and goal for the same session returns existing run."""
    store = RunStore(clock=controlled_clock)
    session_id = "sess_alpha"

    run1, created1 = store.start_run(
        owner_session_id=session_id,
        request_id="req_100",
        goal="Review spending",
    )
    assert created1 is True
    assert run1.owner_session_id == session_id
    assert run1.status == "running"
    assert run1.version == 1

    # Second call with same request_id and same goal returns the existing run
    run2, created2 = store.start_run(
        owner_session_id=session_id,
        request_id="req_100",
        goal="Review spending",
    )
    assert created2 is False
    assert run2.run_id == run1.run_id
    assert store.count() == 1
    assert store.receipt_count() == 1


def test_start_run_request_id_conflict(controlled_clock):
    """Reusing the same request ID with another goal raises conflict error."""
    store = RunStore(clock=controlled_clock)
    session_id = "sess_alpha"

    store.start_run(
        owner_session_id=session_id,
        request_id="req_100",
        goal="Review groceries",
    )

    with pytest.raises(RequestIdConflictError, match="different goal"):
        store.start_run(
            owner_session_id=session_id,
            request_id="req_100",
            goal="Review transport",
        )


def test_start_run_in_progress_conflict(controlled_clock):
    """Starting a second run while one is running raises RunInProgressError."""
    store = RunStore(clock=controlled_clock)
    session_id = "sess_alpha"

    # Start first run (active: 'running')
    run1, _ = store.start_run(
        owner_session_id=session_id,
        request_id="req_1",
        goal="First goal",
    )
    assert run1.status == "running"

    # Attempt second run while first is running
    with pytest.raises(RunInProgressError, match="already in progress"):
        store.start_run(
            owner_session_id=session_id,
            request_id="req_2",
            goal="Second goal",
        )

    # Transition to awaiting_approval; still active
    store.update_status(run1.run_id, "awaiting_approval")
    with pytest.raises(RunInProgressError, match="already in progress"):
        store.start_run(
            owner_session_id=session_id,
            request_id="req_3",
            goal="Third goal",
        )

    # Transition to completed (terminal state); second run now allowed
    store.update_status(run1.run_id, "completed")
    run2, created2 = store.start_run(
        owner_session_id=session_id,
        request_id="req_4",
        goal="Fourth goal",
    )
    assert created2 is True
    assert run2.run_id != run1.run_id


def test_session_isolation_and_cross_session_separation(controlled_clock):
    """Requests and runs across different sessions are strictly isolated."""
    store = RunStore(clock=controlled_clock)
    sess_a = "sess_alice"
    sess_b = "sess_bob"

    # Alice and Bob can use identical request_id strings independently
    run_a, _ = store.start_run(
        owner_session_id=sess_a,
        request_id="req_shared",
        goal="Alice goal",
    )
    store.update_status(run_a.run_id, "completed")

    run_b, _ = store.start_run(
        owner_session_id=sess_b,
        request_id="req_shared",
        goal="Bob goal",
    )
    assert run_a.run_id != run_b.run_id

    # Bob cannot access Alice's run
    with pytest.raises(RunAccessDeniedError):
        store.get_run(run_a.run_id, owner_session_id=sess_b)

    with pytest.raises(RunAccessDeniedError):
        store.get_snapshot(run_a.run_id, owner_session_id=sess_b)

    with pytest.raises(RunAccessDeniedError):
        store.update_status(run_a.run_id, "failed", owner_session_id=sess_b)

    # Filtering runs by session
    assert [r.run_id for r in store.list_runs(owner_session_id=sess_a)] == [
        run_a.run_id
    ]
    assert [r.run_id for r in store.list_runs(owner_session_id=sess_b)] == [
        run_b.run_id
    ]


def test_state_transitions_and_versioning(controlled_clock):
    """State transitions follow allowed paths, update timestamps, and bump versions."""
    store = RunStore(clock=controlled_clock)
    run, _ = store.start_run(
        owner_session_id="sess_1",
        request_id="req_1",
        goal="Analyze categories",
    )
    assert run.version == 1
    assert run.status == "running"
    initial_updated = run.updated_at

    controlled_clock.advance(5.0)
    store.update_status(run.run_id, "awaiting_approval")
    assert run.version == 2
    assert run.status == "awaiting_approval"
    assert run.updated_at > initial_updated

    controlled_clock.advance(3.0)
    store.update_status(run.run_id, "running")
    assert run.version == 3
    assert run.status == "running"

    controlled_clock.advance(2.0)
    store.update_status(run.run_id, "completed")
    assert run.version == 4
    assert run.status == "completed"

    # Terminal run cannot transition to anything
    with pytest.raises(InvalidRunStateTransitionError):
        store.update_status(run.run_id, "running")

    # Non-existent run raises RunNotFoundError
    with pytest.raises(RunNotFoundError):
        store.update_status("nonexistent_run_id", "completed")


def test_run_mutation_helpers(controlled_clock):
    """Appending observations, setting results, and setting errors bump versions."""
    store = RunStore(clock=controlled_clock)
    run, _ = store.start_run(
        owner_session_id="sess_1",
        request_id="req_1",
        goal="Goal",
    )
    assert run.version == 1

    obs = ToolObservation(
        sequence=1,
        step=1,
        tool="search_transactions",
        status="success",
        data=ObservationData(count=1),
    )
    store.add_observation(run.run_id, obs)
    assert run.version == 2
    assert len(run.observations) == 1

    store.set_approval_outcome(
        run.run_id,
        "approved",
    )
    assert run.version == 3
    assert run.approval_outcome == "approved"

    store.set_trace_path(run.run_id, Path("/tmp/trace.jsonl"))
    # Backend bookkeeping: the trace path never bumps the public version.
    assert run.version == 3
    assert run.trace_path == "/tmp/trace.jsonl"

    store.set_result(run.run_id, "All coffee items categorized.")
    assert run.version == 4
    assert run.result == "All coffee items categorized."

    store.set_error(
        run.run_id,
        RunError(code="model_timeout", message="Model took too long to reply."),
    )
    assert run.version == 5
    assert run.error is not None
    assert run.error.code == "model_timeout"


def test_to_snapshot_and_receipt_dto(controlled_clock):
    """Run.to_snapshot() and Run.to_receipt() produce valid Pydantic models."""
    store = RunStore(clock=controlled_clock)
    run, _ = store.start_run(
        owner_session_id="sess_1",
        request_id="req_1",
        goal="Audit expenses",
    )

    snapshot = run.to_snapshot()
    assert isinstance(snapshot, RunSnapshot)
    assert snapshot.run_id == run.run_id
    assert snapshot.goal == "Audit expenses"
    assert snapshot.status == "running"
    assert snapshot.version == 1
    assert snapshot.observations == []

    receipt = run.to_receipt()
    assert isinstance(receipt, RunReceipt)
    assert receipt.run_id == run.run_id
    assert receipt.status == "running"
    assert receipt.version == 1

    stored_receipt = store.get_receipt("sess_1", "req_1")
    assert stored_receipt is not None
    assert stored_receipt.request_id == "req_1"


def test_retention_ttl_eviction(controlled_clock):
    """Terminal runs older than 24 hours are evicted; active runs are never evicted."""
    store = RunStore(clock=controlled_clock, history_ttl=86400.0)
    session_id = "sess_1"

    # Run 1: completed
    r1, _ = store.start_run(session_id, "req_1", "Goal 1")
    store.update_status(r1.run_id, "completed")

    # Run 2: still running (active)
    r2, _ = store.start_run(session_id, "req_2", "Goal 2")

    assert store.count() == 2
    assert store.receipt_count() == 2

    # Advance 23 hours: r1 is 23h old, not expired
    controlled_clock.advance(23 * 3600)
    store.cleanup()
    assert store.count() == 2
    assert store.get_run(r1.run_id) is not None

    # Advance 2 more hours (total 25 hours): r1 is expired (> 24h)
    controlled_clock.advance(2 * 3600)
    store.cleanup()

    # r1 should be evicted; its receipt should also be evicted
    assert store.get_run(r1.run_id) is None
    assert store.get_run(r2.run_id) is not None
    assert store.count() == 1
    assert store.receipt_count() == 1

    # Active run r2 is NOT evicted even though it is 25h old
    assert store.get_run(r2.run_id).status == "running"


def test_retention_capacity_limit_evicts_oldest_terminal(controlled_clock):
    """Store caps terminal runs at max_terminal_runs (100) and evicts the oldest."""
    store = RunStore(clock=controlled_clock, max_terminal_runs=5)
    session_id = "sess_1"

    terminal_ids = []
    for i in range(7):
        controlled_clock.advance(10.0)
        r, _ = store.start_run(session_id, f"req_{i}", f"Goal {i}")
        controlled_clock.advance(1.0)
        store.update_status(r.run_id, "completed")
        terminal_ids.append(r.run_id)

    # We created 7 terminal runs with capacity 5.
    # The oldest 2 should be evicted (terminal_ids[0], terminal_ids[1]).
    assert store.terminal_count() == 5
    assert store.count() == 5
    assert store.receipt_count() == 5

    assert store.get_run(terminal_ids[0]) is None
    assert store.get_run(terminal_ids[1]) is None
    for r_id in terminal_ids[2:]:
        assert store.get_run(r_id) is not None


def test_concurrent_start_run_deduplication(controlled_clock):
    """Concurrent threads starting the same run deduplicate cleanly via mutex."""
    store = RunStore(clock=controlled_clock)
    session_id = "sess_thread"
    request_id = "req_concurrent"
    goal = "Thread safe goal"

    def attempt_start():
        return store.start_run(
            owner_session_id=session_id,
            request_id=request_id,
            goal=goal,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(attempt_start) for _ in range(8)]
        results = [f.result() for f in futures]

    runs = [r for r, _ in results]
    created_flags = [created for _, created in results]

    # Exactly one thread created the run; all 8 got the same run_id
    assert created_flags.count(True) == 1
    assert created_flags.count(False) == 7
    first_run_id = runs[0].run_id
    assert all(r.run_id == first_run_id for r in runs)
    assert store.count() == 1


def test_web_app_session_and_restart_integration(controlled_dataset):
    """FastAPI integration: active_run_id in /api/session, reset on restart."""
    import asyncio

    data_path, changes_path = controlled_dataset
    app = create_app(
        settings=WebSettings(data_path=data_path, changes_path=changes_path)
    )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
        ) as client:
            # 1. Bootstrap session
            resp1 = await client.get("/api/session")
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert data1["active_run_id"] is None
            session_cookie = resp1.cookies[SESSION_COOKIE]
            assert data1["instance_id"]

            # 2. Simulate start_run directly in store for this session
            run, created = app.state.runs.start_run(
                owner_session_id=session_cookie,
                request_id="req_web_1",
                goal="Audit web expenses",
            )
            assert created is True

            # 3. GET /api/session now returns the active run ID
            resp2 = await client.get("/api/session")
            assert resp2.status_code == 200
            assert resp2.json()["active_run_id"] == run.run_id

            # 4. Another session has active_run_id = None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8765",
            ) as client_bob:
                resp_bob = await client_bob.get("/api/session")
                assert resp_bob.json()["active_run_id"] is None

            # 5. Simulate app restart (reset runtime / rotate instance_id)
            # This reflects in-memory state loss upon server restart
            app.state.instance_id = "new_instance_id_after_restart"
            app.state.runs = RunStore()

            resp_after = await client.get("/api/session")
            assert resp_after.status_code == 200
            data_after = resp_after.json()
            assert data_after["instance_id"] == "new_instance_id_after_restart"
            assert data_after["active_run_id"] is None
            assert app.state.runs.count() == 0

    asyncio.run(scenario())
