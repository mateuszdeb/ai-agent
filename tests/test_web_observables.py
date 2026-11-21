"""T-15: safe run summaries, catalog errors, correlated failures and traces.

Runs execute on real frames with the trusted executor while the model stays
offline. These tests prove that the model text never stands in for a tool
result, that failures expose only catalog codes plus a correlation id, that
the run trace is a dedicated web_<run_id>.jsonl without transport context,
and that available observations survive terminal failures.
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from agent_project.state_lock import LedgerWriteError
from agent_project.web import WebSettings, create_app
from agent_project.web.presentation import run_label, tool_message
from agent_project.web.runner import RunManager, trace_run_event
from agent_project.web.runs import TERMINAL_RUN_STATUSES
from agent_project.web.sessions import SESSION_COOKIE
from agent_project.web.tool_executor import WebToolExecutor

ORIGIN = "http://127.0.0.1:8765"
GOAL = "Review the duplicate charge and delete it after my approval."
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


@pytest.fixture
def observables_env(controlled_dataset, tmp_path, offline_model_factory):
    """An offline app on isolated dataset and trace directories."""
    data_path, changes_path = controlled_dataset
    traces_dir = tmp_path / "traces"
    apps: list = []

    def build(responses, *, client=None):
        app = create_app(
            settings=WebSettings(data_path=data_path, changes_path=changes_path)
        )
        app.state.runner.shutdown(wait=False)
        app.state.runner = RunManager(
            runs=app.state.runs,
            data_path=data_path,
            changes_path=changes_path,
            client=client if client is not None else offline_model_factory(responses),
            traces_dir=traces_dir,
            tool_executor_factory=_factory_for(app, data_path, changes_path),
        )
        apps.append(app)
        return app

    yield build
    for app in apps:
        app.state.runner.shutdown(wait=False)


def _factory_for(app, data_path, changes_path):
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
            data_path=data_path,
            changes_path=changes_path,
            run_id=kwargs["run_id"],
            owner_session_id=kwargs["owner_session_id"],
            on_awaiting_approval=kwargs["on_awaiting_approval"],
            on_resumed=kwargs["on_resumed"],
            on_registered=on_registered,
            on_resolution=on_resolution,
        )

    return factory


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            response = await client.get("/api/session")
            assert response.status_code == 200
            return await scenario(client, response.json(), app)

    return asyncio.run(run())


async def start_owner_run(client, session, goal=GOAL, request_id="req_obs"):
    response = await client.post(
        "/api/runs",
        json={
            "goal": goal,
            "request_id": request_id,
            "instance_id": session["instance_id"],
        },
        headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]},
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


async def wait_for_terminal(runs, run_id: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = runs.get_run(run_id)
        if run is not None and run.status in TERMINAL_RUN_STATUSES:
            return run
        await asyncio.sleep(0.05)
    pytest.fail("The run never reached a terminal state.")


async def wait_for_pending_card(client, run_id: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(f"/api/runs/{run_id}/approvals")
        approvals = response.json().get("approvals", [])
        if approvals:
            return approvals[0]["approval_id"]
        await asyncio.sleep(0.05)
    pytest.fail("No pending decision card was ever published.")


def read_trace(trace_path) -> tuple[str, list[dict]]:
    raw = Path(trace_path).read_text(encoding="utf-8")
    return raw, [json.loads(line) for line in raw.splitlines() if line.strip()]


def test_web_trace_is_dedicated_per_run_and_named_by_run_id(observables_env):
    app = observables_env([{"text": "Plain analysis, no tools needed."}])

    async def scenario(client, session, app):
        first = await start_owner_run(client, session, request_id="req_one")
        await wait_for_terminal(app.state.runs, first)
        run = app.state.runs.get_run(first)
        assert run.trace_path is not None
        path = run.trace_path
        assert path.endswith(f"web_{first}.jsonl")
        trace, _events = read_trace(path)
        assert "agent_start" in trace
        assert "agent_completed" in trace

        # A second run gets its own dedicated trace file.
        second = await start_owner_run(client, session, request_id="req_two")
        await wait_for_terminal(app.state.runs, second)
        second_path = app.state.runs.get_run(second).trace_path
        assert second_path.endswith(f"web_{second}.jsonl")
        assert second_path != path

    exercise(app, scenario)


def test_trace_records_decision_lifecycle_without_transport_context(observables_env):
    app = observables_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])

    async def scenario(client, session, app):
        owner_cookie = client.cookies.get(SESSION_COOKIE)
        run_id = await start_owner_run(client, session)
        approval_id = await wait_for_pending_card(client, run_id)
        flow = await client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]},
        )
        assert flow.status_code == 200
        await wait_for_terminal(app.state.runs, run_id)

        run = app.state.runs.get_run(run_id)
        raw, events = read_trace(run.trace_path)
        names = {event["event"] for event in events}
        # Registered and resolved decisions are observable in the trace.
        assert "approval_raised" in names
        assert "approval_resolved" in names
        raised = next(e for e in events if e["event"] == "approval_raised")
        assert raised["payload"]["approval_id"] == approval_id
        assert raised["payload"]["kind"] == "delete_transaction"
        # No transport or session context ever enters the trace.
        assert "cookie" not in raw.lower()
        assert "csrf" not in raw.lower()
        assert owner_cookie not in raw
        assert session["csrf_token"] not in raw
        assert "openrouter" not in raw.lower()

    exercise(app, scenario)


def test_model_claim_never_proves_execution_status(observables_env):
    app = observables_env(
        [
            DELETE_TOOL_CALL,
            {"text": "I deleted the transaction; everything is done."},
        ]
    )

    async def scenario(client, session, app):
        run_id = await start_owner_run(client, session)
        approval_id = await wait_for_pending_card(client, run_id)
        flow = await client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "reject", "instance_id": session["instance_id"]},
            headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]},
        )
        assert flow.status_code == 200
        await wait_for_terminal(app.state.runs, run_id)

        snapshot = (await client.get(f"/api/runs/{run_id}")).json()
        assert snapshot["status"] == "completed"
        # The model text is stored as text only; the execution truth lives in
        # the controller observation, which reports the denied confirmation.
        assert "deleted" in snapshot["result"].lower()
        denied = [
            o
            for o in snapshot["observations"]
            if o["tool"] == "delete_flagged_transaction"
        ]
        assert denied
        assert denied[-1]["status"] == "confirmation_denied"
        # The catalog label matches the controller status, in English.
        assert tool_message("confirmation_denied").startswith("Confirmation")
        assert run_label("completed").startswith("Completed")
        kept = await client.get("/api/transactions/tx_pending")
        assert kept.status_code == 200

    exercise(app, scenario)


def test_uncertain_write_yields_outcome_unknown_with_correlation_id(observables_env):
    class AmbiguousLedgerClient:
        model = "mock"
        provider = "local"

        def complete(self, messages, tools=None):
            if len(messages) <= 2:
                return DELETE_TOOL_CALL
            raise LedgerWriteError("Simulated ambiguous ledger sync.")

    app = observables_env(None, client=AmbiguousLedgerClient())

    async def scenario(client, session, app):
        run_id = await start_owner_run(client, session)
        approval_id = await wait_for_pending_card(client, run_id)
        flow = await client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]},
        )
        assert flow.status_code == 200
        await wait_for_terminal(app.state.runs, run_id)

        snapshot = (await client.get(f"/api/runs/{run_id}")).json()
        assert snapshot["status"] == "outcome_unknown"
        error = snapshot["error"]
        assert error["code"] == "outcome_unknown"
        assert error["message"].startswith("The write outcome could not be confirmed.")
        correlation_id = error["correlation_id"]
        assert correlation_id and correlation_id.startswith("corr_")
        # Raw simulated exception text (which mirrors real infra strings) is
        # never exposed; the observations captured before the failure remain.
        assert "Simulated ambiguous" not in json.dumps(snapshot)
        assert snapshot["observations"], "Available results must be preserved."

    exercise(app, scenario)


def test_internal_failure_is_catalog_code_with_correlation_and_no_raw_text(
    observables_env,
):
    class FailingClient:
        model = "mock"
        provider = "local"

        def complete(self, messages, tools=None):
            raise RuntimeError("secret /Users/private/.env sk-live-key-abcdef")

    app = observables_env(None, client=FailingClient())

    async def scenario(client, session, app):
        run_id = await start_owner_run(client, session)
        await wait_for_terminal(app.state.runs, run_id)
        snapshot = (await client.get(f"/api/runs/{run_id}")).json()
        assert snapshot["status"] == "failed"
        error = snapshot["error"]
        assert error["code"] == "internal_error"
        assert error["message"].startswith("The analysis failed.")
        assert error["correlation_id"]
        raw = json.dumps(snapshot)
        assert "sk-live" not in raw
        assert "/Users/private" not in raw
        assert "secret" not in raw

    exercise(app, scenario)


def test_step_limit_ends_with_preserved_observations(observables_env):
    search_call = {
        "text": "",
        "tool_calls": [
            {
                "id": "call_search",
                "name": "search_transactions",
                "arguments": {"query": "stream", "limit": 5},
            }
        ],
    }
    app = observables_env([dict(search_call) for _ in range(16)])

    async def scenario(client, session, app):
        run_id = await start_owner_run(client, session)
        await wait_for_terminal(app.state.runs, run_id)
        snapshot = (await client.get(f"/api/runs/{run_id}")).json()
        assert snapshot["status"] == "step_limit_reached"
        # No result text substitutes for the analysis stop; observations live.
        assert "result" not in snapshot
        assert len(snapshot["observations"]) >= 10
        assert all(o["tool"] == "search_transactions" for o in snapshot["observations"])
        assert "error" not in snapshot

    exercise(app, scenario)


FINISH_TEXT = "Requested work is finished after your decision."
