"""Tests for T-10: WebToolExecutor binding deletion to one-time approvals."""

import json
import threading
import uuid
from datetime import UTC, datetime

import pytest

from agent_project.agent import ToolCallContext
from agent_project.state_lock import LedgerGuard
from agent_project.tools import append_change_event
from agent_project.web.approvals import (
    ApprovalNotFoundError,
    ApprovalStore,
)
from agent_project.web.runner import RunManager
from agent_project.web.runs import RunStore
from agent_project.web.tool_executor import WebToolExecutor

SESSION = "sess_alpha"
RUN = "run_alpha"


def make_executor(
    approvals: ApprovalStore,
    data_path,
    changes_path,
    *,
    run_id=RUN,
    decision_wait_seconds=None,
    on_registered=None,
    before_recheck=None,
) -> WebToolExecutor:
    return WebToolExecutor(
        approvals=approvals,
        data_path=data_path,
        changes_path=changes_path,
        run_id=run_id,
        owner_session_id=SESSION,
        decision_wait_seconds=decision_wait_seconds,
        on_registered=on_registered,
        before_recheck=before_recheck,
    )


def delete_call(executor: WebToolExecutor, tx_id="tx_pending", **extra) -> dict:
    arguments = {"transaction_id": tx_id, "reason": "Duplicate charge"}
    arguments.update(extra)
    return executor(
        "delete_flagged_transaction",
        arguments,
        context=ToolCallContext(step=1, tool_call_id="call_1"),
    )


def ledger_events(changes_path) -> list[dict]:
    if not changes_path.exists():
        return []
    return [
        json.loads(line)
        for line in changes_path.read_text().splitlines()
        if line.strip()
    ]


def delete_event_count(changes_path) -> int:
    return sum(
        1
        for event in ledger_events(changes_path)
        if event.get("action_type") == "delete_transaction"
    )


class AutoDecider:
    """Deterministic background decision-maker bound to registration."""

    def __init__(self, store: ApprovalStore, clock=None) -> None:
        self.store = store
        self.clock = clock
        self.registered = threading.Event()
        self.approval_id: str | None = None
        self._thread: threading.Thread | None = None

    def on_registered(self, pending) -> None:
        self.approval_id = pending.approval_id
        self.registered.set()

    def decide(self, decision: str, *, wait_for_expiry: bool = False) -> None:
        def worker() -> None:
            assert self.registered.wait(2.0), "Proposal was never registered."
            if wait_for_expiry:
                self.clock.advance(10 * 60 + 1)
            self.store.decide(
                RUN, self.approval_id, owner_session_id=SESSION, decision=decision
            )

        self._thread = threading.Thread(target=worker)
        self._thread.start()

    def join(self, timeout: float = 5.0) -> None:
        assert self._thread is not None
        self._thread.join(timeout)
        assert not self._thread.is_alive()


# ----------------------------------------------------------------------
# Executor level: proposal, decision, single authorized write
# ----------------------------------------------------------------------


def test_approve_executes_exactly_one_write(controlled_dataset, controlled_clock):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("approve")
    result = delete_call(executor)
    decider.join()

    assert result["status"] == "success"
    assert result["transaction_id"] == "tx_pending"
    assert delete_event_count(changes_path) == 1

    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert action.status == "completed"
    assert action.result is not None
    assert action.result.status == "success"
    assert action.result.data.transaction_id == "tx_pending"
    assert dict(action.arguments) == {
        "transaction_id": "tx_pending",
        "reason": "Duplicate charge",
        "confirmed": False,
    }
    card = store.get_card(RUN, decider.approval_id, owner_session_id=SESSION)
    assert card.status == "completed"
    assert card.kind == "delete_transaction"


def test_reject_denies_without_write(controlled_dataset, controlled_clock):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("reject")
    result = delete_call(executor)
    decider.join()

    assert result["status"] == "confirmation_denied"
    assert result["transaction_id"] == "tx_pending"
    assert ledger_events(changes_path) == []
    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert action.status == "rejected"
    assert action.result is None


def test_expiry_denies_without_write(controlled_dataset, controlled_clock):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store, clock=controlled_clock)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("approve", wait_for_expiry=True)
    result = delete_call(executor)
    decider.join()

    assert result["status"] == "confirmation_denied"
    assert "expired" in result["message"]
    assert ledger_events(changes_path) == []
    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert action.status == "expired"


def test_model_confirmed_true_grants_no_privilege(controlled_dataset, controlled_clock):
    """A model-supplied confirmed=True executes nothing without a decision."""
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store, clock=controlled_clock)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("approve", wait_for_expiry=True)
    result = delete_call(executor, confirmed=True)
    decider.join()

    assert result["status"] == "confirmation_denied"
    assert ledger_events(changes_path) == []
    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert dict(action.arguments)["confirmed"] is False


def test_changed_data_between_decision_and_execution_is_stale(
    controlled_dataset, controlled_clock
):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store)

    touch_done = threading.Event()

    def decide_then_touch() -> None:
        assert decider.registered.wait(2.0)
        store.decide(
            RUN, decider.approval_id, owner_session_id=SESSION, decision="approve"
        )
        append_change_event(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "action_id": "act_assign_test01",
                "action_type": "assign_category",
                "transaction_id": "tx_pending",
                "payload": {
                    "previous_category": None,
                    "new_category": "Food",
                    "rationale": (
                        "Ledger touched by the test between decision and execution."
                    ),
                    "force_override": False,
                },
            },
            changes_path,
        )
        touch_done.set()

    toucher = threading.Thread(target=decide_then_touch)
    toucher.start()
    executor = make_executor(
        store,
        data_path,
        changes_path,
        on_registered=decider.on_registered,
        before_recheck=lambda: touch_done.wait(5.0),  # deterministic ordering
    )
    result = delete_call(executor)
    toucher.join(5.0)

    assert result["status"] == "state_conflict"
    assert delete_event_count(changes_path) == 0
    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert action.status == "stale"
    assert action.result is None
    _, consumed = store.begin_execution(
        RUN, decider.approval_id, owner_session_id=SESSION
    )
    assert consumed is False


def test_double_decision_never_repeats_write(controlled_dataset, controlled_clock):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("approve")
    first = delete_call(executor)
    decider.join()
    assert first["status"] == "success"
    assert delete_event_count(changes_path) == 1

    replay = store.decide(
        RUN, decider.approval_id, owner_session_id=SESSION, decision="approve"
    )
    assert replay.status == "completed"
    assert replay.result is not None and replay.result.status == "success"
    assert (
        store.decide(
            RUN, decider.approval_id, owner_session_id=SESSION, decision="reject"
        ).status
        == "completed"
    )
    assert delete_event_count(changes_path) == 1


def test_read_tools_pass_through_without_registration(
    controlled_dataset, controlled_clock
):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    executor = make_executor(store, data_path, changes_path)

    result = executor(
        "get_transactions",
        {"limit": 3},
        context=ToolCallContext(step=1, tool_call_id="call_1"),
    )
    assert result["status"] == "success"
    assert store.count() == 0
    assert ledger_events(changes_path) == []


def test_proposal_for_missing_transaction_never_registers(
    controlled_dataset, controlled_clock
):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    executor = make_executor(store, data_path, changes_path)

    result = delete_call(executor, tx_id="tx_missing")
    assert result["status"] == "not_found"
    assert store.count() == 0
    assert ledger_events(changes_path) == []


def test_busy_ledger_yields_state_busy_without_proposal(
    controlled_dataset, controlled_clock
):
    """With the flock held elsewhere, the consent-free probe fails busy."""
    data_path, changes_path = controlled_dataset

    release = threading.Event()
    holding = threading.Event()

    def hold_guard() -> None:
        with LedgerGuard(changes_path):
            holding.set()
            assert release.wait(10.0)

    holder = threading.Thread(target=hold_guard)
    holder.start()
    assert holding.wait(2.0)

    store = ApprovalStore(clock=controlled_clock)
    executor = make_executor(store, data_path, changes_path)
    result = delete_call(executor)
    release.set()
    holder.join(5.0)

    assert result["status"] == "state_busy"
    assert store.count() == 0


def test_wait_parameters_are_validated(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    with pytest.raises(ValueError):
        store.wait_for_resolution(
            RUN, "apr_none", owner_session_id=SESSION, timeout=-1.0
        )
    with pytest.raises(ApprovalNotFoundError):
        store.wait_for_resolution(
            RUN, "apr_none", owner_session_id=SESSION, timeout=0.0
        )


# ----------------------------------------------------------------------
# Full scenario: agent loop + RunManager + test client, no live model.
# ----------------------------------------------------------------------


def test_full_run_with_client_from_proposal_to_single_write(
    controlled_dataset, controlled_clock, tmp_path, offline_model_factory
):
    data_path, changes_path = controlled_dataset
    approvals = ApprovalStore(clock=controlled_clock)
    runs = RunStore(clock=controlled_clock)

    decider = AutoDecider(approvals)
    awaiting_seen = threading.Event()
    resumed_seen = threading.Event()

    def factory(**kwargs) -> WebToolExecutor:
        awaiting_hook = kwargs["on_awaiting_approval"]
        resumed_hook = kwargs["on_resumed"]

        def on_awaiting() -> None:
            awaiting_hook()
            awaiting_seen.set()

        def on_resumed() -> None:
            resumed_hook()
            resumed_seen.set()

        return WebToolExecutor(
            approvals=approvals,
            data_path=data_path,
            changes_path=changes_path,
            run_id=kwargs["run_id"],
            owner_session_id=kwargs["owner_session_id"],
            on_awaiting_approval=on_awaiting,
            on_resumed=on_resumed,
            on_registered=decider.on_registered,
        )

    manager = RunManager(
        runs,
        data_path=data_path,
        changes_path=changes_path,
        client=offline_model_factory(
            [
                {
                    "text": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "delete_flagged_transaction",
                            "arguments": {
                                "transaction_id": "tx_pending",
                                "reason": "Duplicate charge",
                            },
                        }
                    ],
                },
                {"text": "The flagged transaction was deleted after approval."},
            ]
        ),
        traces_dir=tmp_path / "traces",
        tool_executor_factory=factory,
    )

    run, created = manager.submit_run(
        owner_session_id=SESSION,
        request_id=f"req_{uuid.uuid4().hex[:8]}",
        goal="Delete the duplicate transaction after my approval.",
    )
    assert created is True

    def decision_worker() -> None:
        assert decider.registered.wait(5.0), "Proposal was never registered."
        assert awaiting_seen.wait(5.0), "Run never entered awaiting_approval."
        approvals.decide(
            run.run_id,
            decider.approval_id,
            owner_session_id=SESSION,
            decision="approve",
        )

    worker = threading.Thread(target=decision_worker)
    worker.start()
    finished = manager.wait_for_active_run(timeout=15.0)
    worker.join(10.0)
    try:
        assert finished is True
        assert resumed_seen.wait(5.0)

        snapshot = runs.get_snapshot(run.run_id)
        assert snapshot is not None
        assert snapshot.status == "completed"
        assert "deleted" in snapshot.result

        assert delete_event_count(changes_path) == 1

        action = approvals.get_internal(
            run.run_id, decider.approval_id, owner_session_id=SESSION
        )
        assert action.status == "completed"
        assert action.result is not None and action.result.status == "success"
    finally:
        manager.shutdown(wait=False)
