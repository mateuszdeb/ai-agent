"""Tests for T-11: unapproved category forcing is blocked in the web path.

The web executor validates with the exact common model, probes the
controller without the forced intent and turns only a proposed overwrite of
a different existing category into a one-time ``overwrite_category``
decision. The default CLI/MCP tool contract is untouched.
"""

import json
import threading
import uuid
from datetime import UTC, datetime

from agent_project.agent import ToolCallContext
from agent_project.tools import append_change_event, load_effective_transactions
from agent_project.web.approvals import ApprovalStore
from agent_project.web.runner import RunManager
from agent_project.web.runs import RunStore
from agent_project.web.tool_executor import WebToolExecutor

SESSION = "sess_alpha"
RUN = "run_alpha"

FORCE_ARGS = {"transaction_id": "tx_categorized", "category": "Food"}


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


def assign_call(
    executor: WebToolExecutor,
    tx_id: str = "tx_categorized",
    category: str = "Food",
    **extra,
) -> dict:
    arguments = {"transaction_id": tx_id, "category": category}
    arguments.update(extra)
    return executor(
        "assign_category",
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


def assign_events(changes_path) -> list[dict]:
    return [
        event
        for event in ledger_events(changes_path)
        if event.get("action_type") == "assign_category"
    ]


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
# Probe outcomes that keep the plain controller contract
# ----------------------------------------------------------------------


def test_no_category_assigns_directly_without_decision(
    controlled_dataset, controlled_clock
):
    """A normal assignment on an uncategorized transaction needs no decision."""
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    executor = make_executor(store, data_path, changes_path)

    result = assign_call(executor, tx_id="tx_pending", category="Food")

    assert result["status"] == "success"
    assert result["previous_category"] is None
    events = assign_events(changes_path)
    assert len(events) == 1
    assert events[0]["payload"]["force_override"] is False
    assert events[0]["payload"]["new_category"] == "Food"
    assert store.count() == 0


def test_same_category_returns_already_assigned_without_write(
    controlled_dataset, controlled_clock
):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    executor = make_executor(store, data_path, changes_path)

    result = assign_call(executor, category="Groceries")

    assert result["status"] == "already_assigned"
    assert result["current_category"] == "Groceries"
    assert assign_events(changes_path) == []
    assert store.count() == 0


def test_different_category_without_force_proposal_returns_already_assigned(
    controlled_dataset, controlled_clock
):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    executor = make_executor(store, data_path, changes_path)

    result = assign_call(executor)

    assert result["status"] == "already_assigned"
    assert result["current_category"] == "Groceries"
    assert assign_events(changes_path) == []
    assert store.count() == 0


def test_invalid_forced_arguments_stay_rejected_without_registration(
    controlled_dataset, controlled_clock
):
    """The common model rejects coercion; nothing is executed or stored."""
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    executor = make_executor(store, data_path, changes_path)

    result = assign_call(executor, force_override="yes")

    assert result["status"] == "invalid_arguments"
    assert assign_events(changes_path) == []
    assert store.count() == 0


def test_missing_transaction_never_reaches_a_decision(
    controlled_dataset, controlled_clock
):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    executor = make_executor(store, data_path, changes_path)

    result = assign_call(executor, tx_id="tx_missing", force_override=True)

    assert result["status"] == "not_found"
    assert assign_events(changes_path) == []
    assert store.count() == 0


# ----------------------------------------------------------------------
# Overwrite proposal: one-time decision, exactly one forced write
# ----------------------------------------------------------------------


def test_forced_overwrite_proposal_awaits_decision_and_executes_once(
    controlled_dataset, controlled_clock
):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("approve")
    result = assign_call(executor, force_override=True)
    decider.join()

    assert result["status"] == "success"
    assert result["previous_category"] == "Groceries"
    events = assign_events(changes_path)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["force_override"] is True
    assert payload["previous_category"] == "Groceries"
    assert payload["new_category"] == "Food"

    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert action.status == "completed"
    assert action.tool == "assign_category"
    assert dict(action.arguments) == {
        "transaction_id": "tx_categorized",
        "category": "Food",
        "rationale": "",
        "previous_category": "Groceries",
        "force_override": True,
    }
    card = store.get_card(RUN, decider.approval_id, owner_session_id=SESSION)
    assert card.kind == "overwrite_category"
    assert card.previous_category == "Groceries"
    assert card.proposed_category == "Food"


def test_rejection_denies_overwrite_without_write(controlled_dataset, controlled_clock):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("reject")
    result = assign_call(executor, force_override=True)
    decider.join()

    assert result["status"] == "confirmation_denied"
    assert result["transaction_id"] == "tx_categorized"
    assert assign_events(changes_path) == []
    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert action.status == "rejected"
    assert action.result is None


def test_expiry_denies_overwrite(controlled_dataset, controlled_clock):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store, clock=controlled_clock)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("approve", wait_for_expiry=True)
    result = assign_call(executor, force_override=True)
    decider.join()

    assert result["status"] == "confirmation_denied"
    assert "expired" in result["message"]
    assert assign_events(changes_path) == []
    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert action.status == "expired"


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
                "action_id": "act_assign_touch01",
                "action_type": "assign_category",
                "transaction_id": "tx_categorized",
                "payload": {
                    "previous_category": "Groceries",
                    "new_category": "Utilities",
                    "rationale": "Ledger touched by the test between decision"
                    " and execution.",
                    "force_override": True,
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
    result = assign_call(executor, force_override=True)
    toucher.join(5.0)

    assert result["status"] == "state_conflict"
    # Only the test's own touch event exists; the approved overwrite never ran.
    assert len(assign_events(changes_path)) == 1
    records = {
        tx["id"]: tx for tx in load_effective_transactions(data_path, changes_path)
    }
    assert records["tx_categorized"]["category_hint"] == "Utilities"
    action = store.get_internal(RUN, decider.approval_id, owner_session_id=SESSION)
    assert action.status == "stale"
    assert action.result is None
    _, consumed = store.begin_execution(
        RUN, decider.approval_id, owner_session_id=SESSION
    )
    assert consumed is False


def test_double_decision_observes_recorded_result_without_repeat(
    controlled_dataset, controlled_clock
):
    data_path, changes_path = controlled_dataset
    store = ApprovalStore(clock=controlled_clock)
    decider = AutoDecider(store)
    executor = make_executor(
        store, data_path, changes_path, on_registered=decider.on_registered
    )

    decider.decide("approve")
    first = assign_call(executor, force_override=True)
    decider.join()
    assert first["status"] == "success"
    assert len(assign_events(changes_path)) == 1

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
    _, consumed = store.begin_execution(
        RUN, decider.approval_id, owner_session_id=SESSION
    )
    assert consumed is False
    assert len(assign_events(changes_path)) == 1


# ----------------------------------------------------------------------
# Full scenario: agent loop + RunManager + test client, no live model
# ----------------------------------------------------------------------


def test_full_run_with_client_blocks_forcing_until_approval(
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
                            "name": "assign_category",
                            "arguments": {
                                "transaction_id": "tx_categorized",
                                "category": "Food",
                                "rationale": "Better fitting category.",
                                "force_override": True,
                            },
                        }
                    ],
                },
                {"text": "The category was overwritten after approval."},
            ]
        ),
        traces_dir=tmp_path / "traces",
        tool_executor_factory=factory,
    )

    run, created = manager.submit_run(
        owner_session_id=SESSION,
        request_id=f"req_{uuid.uuid4().hex[:8]}",
        goal="Reassign the category after my explicit approval.",
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
        assert "overwritten" in snapshot.result

        events = assign_events(changes_path)
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["force_override"] is True
        assert payload["previous_category"] == "Groceries"
        assert payload["new_category"] == "Food"

        action = approvals.get_internal(
            run.run_id, decider.approval_id, owner_session_id=SESSION
        )
        assert action.status == "completed"
        assert action.result is not None and action.result.status == "success"
    finally:
        manager.shutdown(wait=False)
