"""T-22: concurrency, idempotency and write failures over real HTTP frames.

Two processes and threads, state changes between proposal and execution,
lost decision acknowledgments and a restart after the write are all forced
through the live trusted protocol while the model stays offline. Every
handoff is synchronized with a threading gate or barrier and every wait is a
bounded deadline poll: no other sleeping appears anywhere in these tests.
"""

import asyncio
import threading
import time

import httpx
import pytest

from agent_project import tools
from agent_project.agent import execute_controlled_tool
from agent_project.web import WebSettings, create_app
from agent_project.web.runner import RunManager
from agent_project.web.runs import TERMINAL_RUN_STATUSES
from agent_project.web.tool_executor import WebToolExecutor

ORIGIN = "http://127.0.0.1:8765"
GOAL = "Delete the duplicate charge, but ask me before you touch the data."
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
FINISH_TEXT = "Requested work is finished after your decision."


@pytest.fixture
def concurrency_env(controlled_dataset, tmp_path, offline_model_factory):
    """An app whose runner executes the real trusted approval protocol.

    ``gate`` blocks the worker at the recheck seam after a decision, so a
    test can place the concurrent change or failure deterministically.
    """
    data_path, changes_path = controlled_dataset
    apps: list = []

    def build(responses, *, approvals=None):
        app = create_app(
            settings=WebSettings(data_path=data_path, changes_path=changes_path)
        )
        app.state.runner.shutdown(wait=False)
        if approvals is not None:
            app.state.approvals = approvals

        gate = threading.Event()

        def factory(**kwargs):
            return WebToolExecutor(
                approvals=app.state.approvals,
                data_path=data_path,
                changes_path=changes_path,
                run_id=kwargs["run_id"],
                owner_session_id=kwargs["owner_session_id"],
                on_awaiting_approval=kwargs["on_awaiting_approval"],
                on_resumed=kwargs["on_resumed"],
                # Fired before locking: holding the gate must not block any
                # other HTTP read or domain write that needs the ledger.
                before_recheck=lambda: gate.wait(15.0),
            )

        app.state.runner = RunManager(
            runs=app.state.runs,
            data_path=data_path,
            changes_path=changes_path,
            client=offline_model_factory(responses),
            traces_dir=tmp_path / "traces",
            tool_executor_factory=factory,
        )
        apps.append(app)
        return app, gate

    yield build
    for app in apps:
        app.state.runner.shutdown(wait=False)


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            response = await client.get("/api/session")
            assert response.status_code == 200
            return await scenario(client, response.json())

    return asyncio.run(run())


async def start_owner_run(client, session, *, request_id="req_owner"):
    response = await client.post(
        "/api/runs",
        json={
            "goal": GOAL,
            "request_id": request_id,
            "instance_id": session["instance_id"],
        },
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": session["csrf_token"],
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def decision_headers(session):
    return {"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]}


def decision_body(session, decision="approve"):
    return {"decision": decision, "instance_id": session["instance_id"]}


async def wait_for_pending_card(client, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(f"/api/runs/{run_id}/approvals")
        assert response.status_code == 200, response.text
        approvals = response.json()["approvals"]
        if approvals:
            return approvals[0]
        await asyncio.sleep(0.05)
    pytest.fail("No pending decision card was ever published.")


async def wait_for_terminal(runs, run_id: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = runs.get_run(run_id)
        if run is not None and run.status in TERMINAL_RUN_STATUSES:
            return run
        await asyncio.sleep(0.05)
    pytest.fail("The run never reached a terminal state.")


async def effective_count(client) -> int:
    response = await client.get("/api/transactions?limit=50")
    assert response.status_code == 200
    return len(response.json()["items"])


def ledger_lines(app) -> int:
    path = app.state.settings.changes_path
    if not path.exists():
        return 0
    return len(path.read_text().splitlines())


async def approve_once(client, run_id: str, approval_id: str, session) -> dict:
    response = await client.post(
        f"/api/runs/{run_id}/approvals/{approval_id}",
        json=decision_body(session),
        headers=decision_headers(session),
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1) Racing decisions on one card: one writer, no flip, no write before it


def test_racing_identical_decisions_record_once_and_keep_one_write(concurrency_env):
    app, gate = concurrency_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        approval_id = (await wait_for_pending_card(client, run_id))["approval_id"]
        keep = await effective_count(client)
        url = f"/api/runs/{run_id}/approvals/{approval_id}"

        # Two identical decisions hit the endpoint at once. The store
        # serializes them: one records the approval, the other must read
        # the recorded state without flipping it.
        first, second = await asyncio.gather(
            client.post(
                url, json=decision_body(session), headers=decision_headers(session)
            ),
            client.post(
                url, json=decision_body(session), headers=decision_headers(session)
            ),
        )
        assert first.status_code == second.status_code == 200, (
            first.text,
            second.text,
        )
        assert first.json()["approval_id"] == approval_id
        assert second.json()["approval_id"] == approval_id
        assert first.json()["status"] == second.json()["status"] == "approved"
        assert "result" not in first.json() and "result" not in second.json()

        # The single-use consumption happens only at the backend seam; with
        # the gate still closed no one has executed the write yet.
        assert await effective_count(client) == keep
        assert ledger_lines(app) == 0

        gate.set()
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"
        assert await effective_count(client) == keep - 1
        assert ledger_lines(app) == 1

        # One of the racing confirmations lost its response on the wire? A
        # resent body stays a read of the recorded state, never a second
        # execution.
        retry = await approve_once(client, run_id, approval_id, session)
        assert retry["approval_id"] == approval_id
        assert retry["status"] == "completed"
        assert retry["result"]["tool"] == "delete_flagged_transaction"
        assert retry["result"]["data"]["transaction_id"] == "tx_pending"
        assert ledger_lines(app) == 1

        cards = await client.get(f"/api/runs/{run_id}/approvals")
        assert cards.status_code == 200
        assert cards.json()["approvals"] == []

    exercise(app, scenario)


def test_lost_decision_response_defines_replay_since_save(concurrency_env):
    app, gate = concurrency_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        approval_id = (await wait_for_pending_card(client, run_id))["approval_id"]
        keep = await effective_count(client)

        flow = await approve_once(client, run_id, approval_id, session)
        assert flow["status"] == "approved"

        gate.set()
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"

        # A confirmation whose response never arrived was still recorded, so
        # the retry reads the stored outcome and never writes again.
        replay = await approve_once(client, run_id, approval_id, session)
        assert replay["approval_id"] == approval_id
        assert replay["status"] == "completed"
        assert replay["result"]["status"] == "success"
        assert await effective_count(client) == keep - 1

        # A conflicting decision body observes the stored record, not a flip.
        assert (
            await approve_once(client, run_id, approval_id, session)  # identical body
        ) == replay
        reject_flip = await client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json=decision_body(session, "reject"),
            headers=decision_headers(session),
        )
        assert reject_flip.status_code == 200
        assert reject_flip.json() == replay

        # The ledger saw exactly one append for the whole affair.
        assert ledger_lines(app) == 1
        assert app.state.runs.count() == 1

    exercise(app, scenario)


# ---------------------------------------------------------------------------
# 2) Data changed between the decision and the execution: stale, no write


def test_data_change_between_decision_and_execution_is_stale(concurrency_env):
    app, gate = concurrency_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])
    data_path = app.state.settings.data_path
    changes_path = app.state.settings.changes_path

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        approval_id = (await wait_for_pending_card(client, run_id))["approval_id"]
        keep = await effective_count(client)

        flow = await approve_once(client, run_id, approval_id, session)
        assert flow["status"] == "approved"

        # While the worker is parked at the recheck seam, an owner-independent
        # domain write lands (on a different transaction). The proposal's
        # fingerprint no longer matches the world.
        mutation = execute_controlled_tool(
            "assign_category",
            {
                "transaction_id": "tx_categorized",
                "category": "Reviewed",
                "force_override": True,
            },
            data_path,
            changes_path,
        )
        assert mutation["status"] == "success", mutation

        gate.set()
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"

        # The stale approval authorized nothing: the delete never ran, and
        # the winner of the change is the concurrent write.
        assert await effective_count(client) == keep
        kept = await client.get("/api/transactions/tx_pending")
        assert kept.status_code == 200, kept.text
        mutated = await client.get("/api/transactions/tx_categorized")
        assert mutated.status_code == 200
        assert mutated.json()["category"] == "Reviewed"

        replay = await approve_once(client, run_id, approval_id, session)
        assert replay["approval_id"] == approval_id
        assert replay["status"] == "stale"
        assert "result" not in replay
        assert ledger_lines(app) == 1

    exercise(app, scenario)


# ---------------------------------------------------------------------------
# 3) Waiting for a decision blocks nothing: concurrent writes and reads


def test_waiting_for_a_decision_blocks_neither_reads_nor_other_writes(
    concurrency_env,
):
    app, _gate = concurrency_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])
    data_path = app.state.settings.data_path
    changes_path = app.state.settings.changes_path

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        card = await wait_for_pending_card(client, run_id)
        assert card["status"] == "pending"
        keep = await effective_count(client)

        # The worker waits for the decision without holding the ledger lock,
        # so a concurrent controlled write must go through immediately.
        mutation = await asyncio.to_thread(
            execute_controlled_tool,
            "assign_category",
            {
                "transaction_id": "tx_dup_a",
                "category": "Reviewed",
                "force_override": True,
            },
            data_path,
            changes_path,
        )
        assert mutation["status"] == "success", mutation

        # And the HTTP reads must stay live while the decision is pending.
        listing = await client.get("/api/transactions?limit=50")
        assert listing.status_code == 200
        snapshot = await client.get(f"/api/runs/{run_id}")
        assert snapshot.status_code == 200
        assert snapshot.json()["status"] == "awaiting_approval"

        # The owner refuses: the delete never runs, but the concurrent write
        # survived the whole time.
        rejected = await client.post(
            f"/api/runs/{run_id}/approvals/{card['approval_id']}",
            json=decision_body(session, "reject"),
            headers=decision_headers(session),
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"

        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"

        assert await effective_count(client) == keep
        kept = await client.get("/api/transactions/tx_pending")
        assert kept.status_code == 200
        assert kept.json()["transaction_id"] == "tx_pending"
        mutated = await client.get("/api/transactions/tx_dup_a")
        assert mutated.status_code == 200
        assert mutated.json()["category"] == "Reviewed"
        assert ledger_lines(app) == 1

        cards = await client.get(f"/api/runs/{run_id}/approvals")
        assert cards.status_code == 200
        assert cards.json()["approvals"] == []
        assert app.state.runs.active_count() == 0

    exercise(app, scenario)


# ---------------------------------------------------------------------------
# 4) Restart after the write: new instance, old runs hidden, state kept


def test_restart_after_write_needs_new_instance_and_hides_old_runs(
    concurrency_env,
):
    first_app, gate = concurrency_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])
    saved: dict[str, str] = {}

    async def first_scenario(client, session):
        run_id = await start_owner_run(client, session)
        approval_id = (await wait_for_pending_card(client, run_id))["approval_id"]

        flow = await approve_once(client, run_id, approval_id, session)
        assert flow["status"] == "approved"

        gate.set()
        finished = await wait_for_terminal(first_app.state.runs, run_id)
        assert finished.status == "completed"
        assert await effective_count(client) == 4
        assert ledger_lines(first_app) == 1

        saved["instance"] = session["instance_id"]
        saved["run"] = run_id

    exercise(first_app, first_scenario)

    second_app, _gate = concurrency_env([{"text": "Fresh analysis finished."}])

    async def second_scenario(client, session):
        assert session["instance_id"] != saved["instance"]

        stale = await client.post(
            "/api/runs",
            json={
                "goal": "Review expenses.",
                "request_id": "req_restart",
                "instance_id": saved["instance"],
            },
            headers=decision_headers(session),
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "instance_changed"

        lost = await client.get(f"/api/runs/{saved['run']}")
        assert lost.status_code == 404, lost.text
        assert lost.json()["error"]["code"] == "run_unavailable"
        assert second_app.state.runs.count() == 0

        # The effective state survived the restart: the deleted transaction
        # is still gone.
        listing = await client.get("/api/transactions?limit=50")
        assert listing.status_code == 200
        assert len(listing.json()["items"]) == 4
        missing = await client.get("/api/transactions/tx_pending")
        assert missing.status_code == 404

    exercise(second_app, second_scenario)


# ---------------------------------------------------------------------------
# 5) A write failure after the append: outcome unknown, never auto-retried


def test_fsync_failure_reports_unknown_outcome_without_automatic_retry(
    concurrency_env, monkeypatch
):
    app, gate = concurrency_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])

    def fail(fd):
        raise OSError("Simulated durability failure")

    monkeypatch.setattr(tools.os, "fsync", fail)

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        approval_id = (await wait_for_pending_card(client, run_id))["approval_id"]
        keep = await effective_count(client)

        flow = await approve_once(client, run_id, approval_id, session)
        assert flow["status"] == "approved"

        gate.set()
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"

        # The append landed before the durability failure; the UI must learn
        # the honest unknown outcome, not a retry.
        assert ledger_lines(app) == 1
        assert await effective_count(client) == keep - 1

        replay = await approve_once(client, run_id, approval_id, session)
        assert replay["approval_id"] == approval_id
        assert replay["status"] == "outcome_unknown"
        # The stored observation is the error view of the one attempted write.
        assert replay["result"]["tool"] == "delete_flagged_transaction"
        assert replay["result"]["status"] == "error"

        # Nothing was ever repeated: one run, one append, no further work.
        assert ledger_lines(app) == 1
        assert app.state.runs.count() == 1
        cards = await client.get(f"/api/runs/{run_id}/approvals")
        assert cards.json()["approvals"] == []

    exercise(app, scenario)
