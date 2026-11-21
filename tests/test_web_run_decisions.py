"""T-14: decision cards and the decision endpoint over real HTTP frames.

Every pending proposal is produced end to end by the trusted executor while
the model runs offline. These tests prove that the HTTP layer only records an
explicit owner decision while the backend keeps control of execution, and
that unknown, foreign, expired and repeated requests read stored state
instead of writing anything again.
"""

import asyncio
import threading
import time

import httpx
import pytest

from agent_project.web import WebSettings, create_app
from agent_project.web.approvals import ApprovalStore
from agent_project.web.runner import RunManager
from agent_project.web.runs import TERMINAL_RUN_STATUSES
from agent_project.web.sessions import SESSION_COOKIE
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
def decision_env(controlled_dataset, tmp_path, offline_model_factory):
    """An app whose runner executes the real trusted approval protocol."""
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
                # other HTTP read that needs the ledger lock.
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


async def bootstrap_session(client) -> dict:
    response = await client.get("/api/session")
    assert response.status_code == 200
    return response.json()


def write_headers(session):
    return {"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]}


async def start_owner_run(client, session, *, request_id="req_owner"):
    response = await client.post(
        "/api/runs",
        json={
            "goal": GOAL,
            "request_id": request_id,
            "instance_id": session["instance_id"],
        },
        headers=write_headers(session),
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


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


def test_pending_card_is_presentation_only_and_backend_keeps_control(decision_env):
    app, gate = decision_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        card = await wait_for_pending_card(client, run_id)
        approval_id = card["approval_id"]

        # The card shows the operation, action, transaction, reason and TTL
        # end; never the owner, fingerprint or executable arguments.
        assert card["run_id"] == run_id
        assert card["kind"] == "delete_transaction"
        assert card["status"] == "pending"
        assert card["transaction"]["transaction_id"] == "tx_pending"
        assert card["reason"]
        assert card["expires_at"]
        for forbidden in ("owner", "arguments", "fingerprint", "tool"):
            assert forbidden not in card

        snapshot = await client.get(f"/api/runs/{run_id}")
        assert snapshot.status_code == 200
        assert snapshot.json()["status"] == "awaiting_approval"

        # A card is a proposal: nothing was written yet.
        keep_count = await effective_count(client)
        assert keep_count >= 1

        flow = await client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=write_headers(session),
        )
        assert flow.status_code == 200, flow.text
        assert flow.json()["approval_id"] == approval_id
        assert flow.json()["status"] == "approved"

        # The single-use consumption is backend-controlled; with the worker
        # held at the recheck seam the write has still not happened.
        assert await effective_count(client) == keep_count

        gate.set()
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"
        assert await effective_count(client) == keep_count - 1

        missing = await client.get("/api/transactions/tx_pending")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "transaction_unavailable"

        decided_list = await client.get(f"/api/runs/{run_id}/approvals")
        assert decided_list.status_code == 200
        assert decided_list.json()["approvals"] == []

    exercise(app, scenario)


def test_foreign_and_unknown_decisions_are_identical_safe_404(decision_env):
    app, _gate = decision_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])

    async def scenario(client, session):
        owner_cookie = client.cookies.get(SESSION_COOKIE)
        run_id = await start_owner_run(client, session)
        card = await wait_for_pending_card(client, run_id)
        approval_id = card["approval_id"]
        headers = write_headers(session)

        unknown = await client.post(
            f"/api/runs/{run_id}/approvals/apr_unknown",
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert unknown.status_code == 404, unknown.text
        assert unknown.json()["error"]["code"] == "approval_unavailable"

        unknown_run = await client.post(
            "/api/runs/run_absent/approvals/apr_also_unknown",
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert unknown_run.status_code == 404
        assert unknown_run.json()["error"]["code"] == "approval_unavailable"
        assert unknown_run.text == unknown.text

        # A second anonymous visitor owns nothing and learns nothing.
        client.cookies.clear()
        other = await bootstrap_session(client)
        foreign_read = await client.get(f"/api/runs/{run_id}/approvals")
        assert foreign_read.status_code == 404, foreign_read.text
        assert foreign_read.json()["error"]["code"] == "run_unavailable"
        foreign_run_read = await client.get("/api/runs/run_absent/approvals")
        assert foreign_run_read.status_code == 404
        assert foreign_run_read.json()["error"]["code"] == "run_unavailable"
        foreign_decision = await client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "reject", "instance_id": other["instance_id"]},
            headers=write_headers(other),
        )
        assert foreign_decision.status_code == 404
        assert foreign_decision.json()["error"]["code"] == "approval_unavailable"

        # The owner's decision was never consumed by those probes. Ending the
        # scenario with an explicit owner decision keeps the executor drained.
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, owner_cookie)
        finishing = await client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "reject", "instance_id": session["instance_id"]},
            headers=write_headers(session),
        )
        assert finishing.status_code == 200
        assert finishing.json()["status"] == "rejected"
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"
        cards = await client.get(f"/api/runs/{run_id}/approvals")
        assert cards.json()["approvals"] == []

    exercise(app, scenario)


def test_replay_decision_reads_stored_result_instead_of_writing_again(decision_env):
    app, gate = decision_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        approval_id = (await wait_for_pending_card(client, run_id))["approval_id"]
        headers = write_headers(session)
        decision_url = f"/api/runs/{run_id}/approvals/{approval_id}"

        flow = await client.post(
            decision_url,
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert flow.status_code == 200
        first_receipt = flow.json()
        assert first_receipt["status"] == "approved"
        assert "result" not in first_receipt

        gate.set()
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"

        # A repeated decision reads the recorded result; it authorizes
        # nothing and executes nothing again.
        replay = await client.post(
            decision_url,
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert replay.status_code == 200
        replay_receipt = replay.json()
        assert replay_receipt["approval_id"] == approval_id
        assert replay_receipt["status"] == "completed"
        result = replay_receipt["result"]
        assert result is not None
        assert result["tool"] == "delete_flagged_transaction"
        assert result["status"] == "success"
        assert result["data"]["transaction_id"] == "tx_pending"

        flip = await client.post(
            decision_url,
            json={"decision": "reject", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert flip.status_code == 200
        assert flip.json()["status"] == "completed"
        assert flip.json()["result"] == result
        assert flip.json().pop("result") == replay_receipt["result"]

    exercise(app, scenario)


def test_reject_decision_never_writes_and_replay_is_stable(decision_env):
    app, _gate = decision_env([DELETE_TOOL_CALL, {"text": FINISH_TEXT}])

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        approval_id = (await wait_for_pending_card(client, run_id))["approval_id"]
        headers = write_headers(session)
        decision_url = f"/api/runs/{run_id}/approvals/{approval_id}"
        keep_count = await effective_count(client)

        flow = await client.post(
            decision_url,
            json={"decision": "reject", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert flow.status_code == 200
        flow_receipt = flow.json()
        assert flow_receipt["status"] == "rejected"

        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"

        # The refused action never ran: the record survives unchanged.
        assert await effective_count(client) == keep_count
        kept = await client.get("/api/transactions/tx_pending")
        assert kept.status_code == 200
        assert kept.json()["transaction_id"] == "tx_pending"

        replay = await client.post(
            decision_url,
            json={"decision": "reject", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json() == flow_receipt

        overturn = await client.post(
            decision_url,
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert overturn.status_code == 200
        assert overturn.json() == flow_receipt

        # Pending cards disappear once the decision is recorded.
        cards = await client.get(f"/api/runs/{run_id}/approvals")
        assert cards.json()["approvals"] == []

    exercise(app, scenario)


def test_ttl_expiry_closes_without_writing_and_replay_reads_expired(
    decision_env, controlled_clock
):
    app, _gate = decision_env(
        [DELETE_TOOL_CALL, {"text": FINISH_TEXT}],
        approvals=ApprovalStore(decision_ttl=60, clock=controlled_clock),
    )

    async def scenario(client, session):
        run_id = await start_owner_run(client, session)
        approval_id = (await wait_for_pending_card(client, run_id))["approval_id"]
        keep_count = await effective_count(client)

        # Nobody decides; the untouched proposal physically expires.
        controlled_clock.advance(120)
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"

        # No execution happened despite the scripted delete request.
        assert await effective_count(client) == keep_count
        kept = await client.get("/api/transactions/tx_pending")
        assert kept.status_code == 200

        pending = await client.get(f"/api/runs/{run_id}/approvals")
        assert pending.status_code == 200
        assert pending.json()["approvals"] == []

        late = await client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=write_headers(session),
        )
        assert late.status_code == 200
        body = late.json()
        assert body["approval_id"] == approval_id
        assert body["status"] == "expired"
        assert "result" not in body

    exercise(app, scenario)


def test_decision_body_accepts_only_decision_and_instance(decision_env):
    app, _gate = decision_env([{"text": "A plain read-only reply, no tools."}])

    async def scenario(client, session):
        url = "/api/runs/run_absent/approvals/apr_absent"
        headers = write_headers(session)

        # Only the decision and the instance identifier reach the handler.
        valid_shape = await client.post(
            url,
            json={"decision": "approve", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert valid_shape.status_code == 404
        assert valid_shape.json()["error"]["code"] == "approval_unavailable"
        assert app.state.approvals.count() == 0

        # Extra client fields must never smuggle an execution contract.
        smuggled = [
            {
                "decision": "approve",
                "instance_id": session["instance_id"],
                "confirmed": True,
            },
            {
                "decision": "approve",
                "instance_id": session["instance_id"],
                "force_override": True,
            },
            {
                "decision": "reject",
                "instance_id": session["instance_id"],
                "tool": "delete_flagged_transaction",
                "transaction_id": "tx_dup_a",
            },
            {
                "decision": "approve",
                "instance_id": session["instance_id"],
                "arguments": {"transaction_id": "tx_dup_a"},
            },
        ]
        for body in smuggled:
            response = await client.post(url, json=body, headers=headers)
            assert response.status_code == 422, (body, response.text)
            assert response.json()["error"]["code"] == "invalid_request"

        bad_value = await client.post(
            url,
            json={"decision": "maybe", "instance_id": session["instance_id"]},
            headers=headers,
        )
        assert bad_value.status_code == 422
        assert bad_value.json()["error"]["code"] == "invalid_request"

        missing_decision = await client.post(
            url,
            json={"instance_id": session["instance_id"]},
            headers=headers,
        )
        assert missing_decision.status_code == 422

        missing_instance = await client.post(
            url,
            json={"decision": "approve"},
            headers=headers,
        )
        assert missing_instance.status_code == 422
        assert missing_instance.json()["error"]["code"] == "invalid_request"

        stale_instance = await client.post(
            url,
            json={
                "decision": "approve",
                "instance_id": "stale" + "0" * 26,
            },
            headers=headers,
        )
        assert stale_instance.status_code == 409
        assert stale_instance.json()["error"]["code"] == "instance_changed"

        # Nothing anywhere was started or authorized.
        assert app.state.runs.count() == 0
        assert app.state.approvals.count() == 0

    exercise(app, scenario)


def test_completed_run_without_decisions_serves_empty_pending_list(decision_env):
    app, _gate = decision_env([{"text": "Nothing needed a decision this time."}])

    async def scenario(client, session):
        run_id = await start_owner_run(client, session, request_id="req_plain")
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"

        empty = await client.get(f"/api/runs/{run_id}/approvals")
        assert empty.status_code == 200
        assert empty.json() == {"approvals": []}

        still_there = await effective_count(client)
        assert still_there >= 1
        assert app.state.runner.is_busy() is False

    exercise(app, scenario)
