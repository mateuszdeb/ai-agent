"""Tests for T-09: one-time decision lifecycle and PendingAction store."""

import threading

import pytest

from agent_project.state_lock import LedgerGuard
from agent_project.web.approvals import (
    APPROVAL_TTL_SECONDS,
    ApprovalAccessDeniedError,
    ApprovalCapacityError,
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalStore,
    PendingAction,
    compute_state_fingerprint,
)
from agent_project.web.schemas import (
    ObservationData,
    ToolObservation,
    TransactionSummary,
)

SESSION = "sess_alpha"
OTHER_SESSION = "sess_other"
RUN = "run_alpha"
OTHER_RUN = "run_beta"
CALL_ID = "call_step2_1"


def make_transaction(
    tx_id: str = "tx_pending",
    merchant: str = "Pending Shop",
    amount: float = 120.0,
    category: str | None = None,
) -> TransactionSummary:
    return TransactionSummary(
        transaction_id=tx_id,
        date="2026-10-01",
        merchant=merchant,
        amount=amount,
        currency="PLN",
        category=category,
    )


def make_delete_args(tx_id: str = "tx_pending", reason: str = "Duplicate") -> dict:
    """Validated DeleteTransactionArgs shape; confirmed stays False here."""
    return {"transaction_id": tx_id, "reason": reason, "confirmed": False}


def register_delete(
    store: ApprovalStore,
    *,
    session: str = SESSION,
    run: str = RUN,
    tool_call_id: str = CALL_ID,
    tx_id: str = "tx_pending",
    action_id: str | None = None,
    arguments: dict | None = None,
) -> PendingAction:
    arguments = arguments if arguments is not None else make_delete_args(tx_id)
    return store.register(
        owner_session_id=session,
        run_id=run,
        tool_call_id=tool_call_id,
        tool="delete_flagged_transaction",
        kind="delete_transaction",
        arguments=arguments,
        transaction=make_transaction(tx_id),
        fingerprint="f" * 64,
        reason=str(arguments.get("reason") or ""),
        action_id=action_id,
    )


def make_observation(
    status: str = "success",
    sequence: int = 1,
) -> ToolObservation:
    return ToolObservation(
        sequence=sequence,
        step=2,
        tool="delete_flagged_transaction",
        status=status,  # type: ignore[arg-type]
        action_id="act_del_1",
        data=ObservationData(transaction_id="tx_pending", count=1),
    )


# ----------------------------------------------------------------------
# Registration, ownership and argument binding
# ----------------------------------------------------------------------


def test_register_creates_pending_with_random_approval(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)

    assert pending.status == "pending"
    assert pending.owner_session_id == SESSION
    assert pending.run_id == RUN
    assert pending.tool_call_id == CALL_ID
    assert pending.approval_id.startswith("apr_")
    assert pending.created_at == controlled_clock.now()
    assert pending.expires_monotonic == APPROVAL_TTL_SECONDS
    card = store.get_card(RUN, pending.approval_id, owner_session_id=SESSION)
    assert card.kind == "delete_transaction"
    assert card.status == "pending"
    assert card.transaction.transaction_id == "tx_pending"
    assert card.action_id is None


def test_identical_proposals_get_distinct_random_approval_ids(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    first = register_delete(store)
    second = register_delete(
        store, tool_call_id="call_step3_1", action_id="pending_del_tx_pending"
    )
    assert first.approval_id != second.approval_id


def test_arguments_are_frozen_against_swapping(controlled_clock):
    """Post-registration argument mutation cannot redirect the execution."""
    store = ApprovalStore(clock=controlled_clock)
    arguments = make_delete_args("tx_pending")
    pending = register_delete(
        store, arguments=arguments, action_id="pending_del_tx_pending"
    )

    arguments["transaction_id"] = "tx_fuel"
    arguments["confirmed"] = True

    stored = store.get_internal(RUN, pending.approval_id, owner_session_id=SESSION)
    assert dict(stored.arguments) == make_delete_args("tx_pending")
    with pytest.raises(TypeError):
        stored.arguments["transaction_id"] = "tx_fuel"


def test_register_validates_inputs(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    base = dict(
        owner_session_id=SESSION,
        run_id=RUN,
        tool_call_id=CALL_ID,
        tool="delete_flagged_transaction",
        kind="delete_transaction",
        arguments=make_delete_args(),
        transaction=make_transaction(),
        fingerprint="f" * 64,
    )
    with pytest.raises(ValueError):
        store.register(**{**base, "owner_session_id": "  "})
    with pytest.raises(ValueError):
        store.register(**{**base, "kind": "unknown_kind"})
    with pytest.raises(ValueError):
        store.register(**{**base, "transaction": {"transaction_id": "tx_pending"}})
    with pytest.raises(ValueError):
        store.register(**{**base, "fingerprint": "short"})


def test_store_limits_must_be_positive(controlled_clock):
    with pytest.raises(ValueError):
        ApprovalStore(decision_ttl=0, clock=controlled_clock)
    with pytest.raises(ValueError):
        ApprovalStore(decision_ttl=float("inf"), clock=controlled_clock)
    with pytest.raises(ValueError):
        ApprovalStore(max_actions=0, clock=controlled_clock)


# ----------------------------------------------------------------------
# Presentation cards
# ----------------------------------------------------------------------


def test_card_hides_owner_arguments_and_fingerprint(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    card = store.get_card(RUN, pending.approval_id, owner_session_id=SESSION)

    dumped = card.model_dump(exclude_none=True)
    assert "owner_session_id" not in dumped
    assert "arguments" not in dumped
    assert "fingerprint" not in dumped
    assert "result" not in dumped
    assert dumped["kind"] == "delete_transaction"


def test_overwrite_card_shows_old_and_new_category(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = store.register(
        owner_session_id=SESSION,
        run_id=RUN,
        tool_call_id=CALL_ID,
        tool="assign_category",
        kind="overwrite_category",
        arguments={
            "transaction_id": "tx_categorized",
            "category": "Utilities",
            "previous_category": "Groceries",
            "force_override": True,
        },
        transaction=make_transaction("tx_categorized", category="Groceries"),
        fingerprint="e" * 64,
    )
    card = store.get_card(RUN, pending.approval_id, owner_session_id=SESSION)
    assert card.previous_category == "Groceries"
    assert card.proposed_category == "Utilities"


def test_list_cards_and_pending_filtering(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    assert [c.approval_id for c in store.list_cards(RUN, owner_session_id=SESSION)] == (
        [pending.approval_id]
    )

    store.decide(RUN, pending.approval_id, owner_session_id=SESSION, decision="reject")
    assert store.list_cards(RUN, owner_session_id=SESSION) == []
    closed = store.list_cards(RUN, owner_session_id=SESSION, pending_only=False)
    assert [card.status for card in closed] == ["rejected"]


# ----------------------------------------------------------------------
# Decisions: one-time consent, replay and conflicts
# ----------------------------------------------------------------------


def test_approve_and_reject_decisions(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    approve = register_delete(store, tool_call_id="call_1")
    receipt = store.decide(
        RUN, approve.approval_id, owner_session_id=SESSION, decision="approve"
    )
    assert receipt.status == "approved"
    assert receipt.result is None
    assert receipt.approval_id == approve.approval_id

    reject = register_delete(store, tool_call_id="call_2")
    receipt = store.decide(
        RUN, reject.approval_id, owner_session_id=SESSION, decision="reject"
    )
    assert receipt.status == "rejected"

    with pytest.raises(ValueError):
        store.decide(
            RUN, reject.approval_id, owner_session_id=SESSION, decision="close"
        )


def test_double_decision_returns_recorded_state_without_flip(controlled_clock):
    """A repeated or reversed decision never re-authorizes or flips state."""
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    first = store.decide(
        RUN, pending.approval_id, owner_session_id=SESSION, decision="approve"
    )
    replay = store.decide(
        RUN, pending.approval_id, owner_session_id=SESSION, decision="approve"
    )
    assert replay.status == first.status == "approved"

    conflict = store.decide(
        RUN, pending.approval_id, owner_session_id=SESSION, decision="reject"
    )
    assert conflict.status == "approved"


def test_foreign_session_cannot_decide_or_read(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    with pytest.raises(ApprovalAccessDeniedError):
        store.decide(
            RUN, pending.approval_id, owner_session_id=OTHER_SESSION, decision="approve"
        )
    with pytest.raises(ApprovalAccessDeniedError):
        store.get_card(RUN, pending.approval_id, owner_session_id=OTHER_SESSION)
    with pytest.raises(ApprovalAccessDeniedError):
        store.list_cards(RUN, owner_session_id=OTHER_SESSION)


def test_decision_is_bound_to_its_run(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    with pytest.raises(ApprovalNotFoundError):
        store.decide(
            OTHER_RUN, pending.approval_id, owner_session_id=SESSION, decision="approve"
        )
    with pytest.raises(ApprovalNotFoundError):
        store.get_card(OTHER_RUN, pending.approval_id, owner_session_id=SESSION)
    with pytest.raises(ApprovalNotFoundError):
        store.decide(RUN, "apr_missing", owner_session_id=SESSION, decision="approve")


def test_domain_action_id_is_not_a_decision_key(controlled_clock):
    """Knowing `pending_del_<tx>` alone cannot locate or authorize anything."""
    store = ApprovalStore(clock=controlled_clock)
    register_delete(store, action_id="pending_del_tx_pending")

    with pytest.raises(ApprovalNotFoundError):
        store.decide(
            RUN, "pending_del_tx_pending", owner_session_id=SESSION, decision="approve"
        )

    pending = next(
        action
        for action in store._actions.values()
        if action.action_id == "pending_del_tx_pending"
    )
    assert pending.approval_id.startswith("apr_")


# ----------------------------------------------------------------------
# TTL expiry with the controlled clock
# ----------------------------------------------------------------------


def test_expiry_after_ten_minutes_of_waiting(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    card = store.get_card(RUN, pending.approval_id, owner_session_id=SESSION)
    assert card.status == "pending"

    controlled_clock.advance(APPROVAL_TTL_SECONDS - 1)
    assert (
        store.get_card(RUN, pending.approval_id, owner_session_id=SESSION).status
        == "pending"
    )

    controlled_clock.advance(1)
    card = store.get_card(RUN, pending.approval_id, owner_session_id=SESSION)
    assert card.status == "expired"

    # A late decision observes the expired state and never approves it.
    receipt = store.decide(
        RUN, pending.approval_id, owner_session_id=SESSION, decision="approve"
    )
    assert receipt.status == "expired"
    assert store.get_card(
        RUN, pending.approval_id, owner_session_id=SESSION
    ).status == ("expired")


def test_rejected_proposal_does_not_expire(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    store.decide(RUN, pending.approval_id, owner_session_id=SESSION, decision="reject")
    controlled_clock.advance(APPROVAL_TTL_SECONDS * 5)
    card = store.get_card(RUN, pending.approval_id, owner_session_id=SESSION)
    assert card.status == "rejected"


# ----------------------------------------------------------------------
# Execution consumption: approval is spent exactly once
# ----------------------------------------------------------------------


def test_begin_execution_consumes_approval_once(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    store.decide(RUN, pending.approval_id, owner_session_id=SESSION, decision="approve")

    action, consumed = store.begin_execution(
        RUN, pending.approval_id, owner_session_id=SESSION
    )
    assert consumed is True
    assert action.status == "executing"
    assert dict(action.arguments) == make_delete_args()

    # Replay of a consumed decision observes executing status only.
    again, consumed_again = store.begin_execution(
        RUN, pending.approval_id, owner_session_id=SESSION
    )
    assert consumed_again is False
    assert again.status == "executing"


def test_execution_requires_prior_consent(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    with pytest.raises(ApprovalConflictError):
        store.begin_execution(RUN, pending.approval_id, owner_session_id=SESSION)


def test_rejected_proposal_never_enters_execution(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    store.decide(RUN, pending.approval_id, owner_session_id=SESSION, decision="reject")
    again, consumed = store.begin_execution(
        RUN, pending.approval_id, owner_session_id=SESSION
    )
    assert consumed is False
    assert again.status == "rejected"


def test_stale_data_invalidates_bound_decision(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    store.decide(RUN, pending.approval_id, owner_session_id=SESSION, decision="approve")
    store.mark_stale(RUN, pending.approval_id, owner_session_id=SESSION)

    again, consumed = store.begin_execution(
        RUN, pending.approval_id, owner_session_id=SESSION
    )
    assert consumed is False
    assert again.status == "stale"

    with pytest.raises(ApprovalConflictError):
        store.mark_stale(RUN, pending.approval_id, owner_session_id=SESSION)


def test_result_closes_proposal_by_controller_outcome(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    ok = register_delete(store, tool_call_id="call_1")
    store.decide(RUN, ok.approval_id, owner_session_id=SESSION, decision="approve")
    store.begin_execution(RUN, ok.approval_id, owner_session_id=SESSION)
    receipt = store.record_result(
        RUN, ok.approval_id, owner_session_id=SESSION, result=make_observation()
    )
    assert receipt.status == "completed"
    assert receipt.result is not None and receipt.result.status == "success"

    conflict = register_delete(store, tool_call_id="call_2")
    store.decide(
        RUN, conflict.approval_id, owner_session_id=SESSION, decision="approve"
    )
    store.begin_execution(RUN, conflict.approval_id, owner_session_id=SESSION)
    receipt = store.record_result(
        RUN,
        conflict.approval_id,
        owner_session_id=SESSION,
        result=make_observation(status="not_found"),
    )
    assert receipt.status == "failed"

    done = register_delete(store, tool_call_id="call_3")
    with pytest.raises(ApprovalConflictError):
        store.record_result(
            RUN, done.approval_id, owner_session_id=SESSION, result=make_observation()
        )


def test_outcome_unknown_waits_and_adopts_a_late_result(controlled_clock):
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    store.decide(RUN, pending.approval_id, owner_session_id=SESSION, decision="approve")
    store.begin_execution(RUN, pending.approval_id, owner_session_id=SESSION)

    receipt = store.record_outcome_unknown(
        RUN, pending.approval_id, owner_session_id=SESSION
    )
    assert receipt.status == "outcome_unknown"
    assert receipt.result is None

    # A late controller observation may complete the record, but never
    # promotes an unknown outcome into a claimed success.
    late = store.record_result(
        RUN, pending.approval_id, owner_session_id=SESSION, result=make_observation()
    )
    assert late.status == "outcome_unknown"
    assert late.result is not None and late.result.status == "success"


# ----------------------------------------------------------------------
# Waiting must never hold the shared ledger lock
# ----------------------------------------------------------------------


def test_lifecycle_ops_run_while_ledger_is_locked(controlled_dataset):
    """Registering, deciding and consuming work with a held LedgerGuard."""
    data_path, changes_path = controlled_dataset
    clock = type("Clock", (), {})()
    store = ApprovalStore(clock=clock)
    pending = register_delete(store, run=RUN, session=SESSION, tx_id="tx_pending")

    with LedgerGuard(changes_path):
        # All store operations complete immediately inside this block; the
        # store never touches the ledger lock while a decision is open.
        store.register(
            owner_session_id=SESSION,
            run_id=RUN,
            tool_call_id="call_probe",
            tool="delete_flagged_transaction",
            kind="delete_transaction",
            arguments=make_delete_args("tx_fuel"),
            transaction=make_transaction("tx_fuel"),
            fingerprint="a" * 64,
        )
        store.list_cards(RUN, owner_session_id=SESSION)
        store.decide(
            RUN, pending.approval_id, owner_session_id=SESSION, decision="approve"
        )
        action, consumed = store.begin_execution(
            RUN, pending.approval_id, owner_session_id=SESSION
        )
        assert consumed is True
        store.record_result(
            RUN,
            pending.approval_id,
            owner_session_id=SESSION,
            result=make_observation(),
        )
        assert store.pending_count() == 1

    with LedgerGuard(changes_path, timeout=0):
        pass  # the guard was never held by the store


# ----------------------------------------------------------------------
# Capacity bounds and cross-thread consistency
# ----------------------------------------------------------------------


def test_capacity_full_blocks_registration(controlled_clock):
    """At capacity, a failure-safe error wins over a hidden queue or eviction."""
    store = ApprovalStore(max_actions=2, clock=controlled_clock)
    register_delete(store, tool_call_id="call_1")
    register_delete(store, tool_call_id="call_2")
    with pytest.raises(ApprovalCapacityError):
        register_delete(store, tool_call_id="call_3")


def test_capacity_retires_finished_proposals_first(controlled_clock):
    """Closed proposals free slots; live ones are never evicted."""
    store = ApprovalStore(max_actions=2, clock=controlled_clock)
    first = register_delete(store, tool_call_id="call_1")
    register_delete(store, tool_call_id="call_2")
    store.decide(RUN, first.approval_id, owner_session_id=SESSION, decision="reject")

    third = register_delete(store, tool_call_id="call_3")
    assert store.count() == 2
    assert store.pending_count() == 2
    assert third.status == "pending"


def test_concurrent_decisions_execute_consent_only_once(controlled_clock):
    """Exactly one thread's begin_execution consumes the approval."""
    store = ApprovalStore(clock=controlled_clock)
    pending = register_delete(store)
    store.decide(RUN, pending.approval_id, owner_session_id=SESSION, decision="approve")

    consumed_flags: list[bool] = []
    statuses: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        action, consumed = store.begin_execution(
            RUN, pending.approval_id, owner_session_id=SESSION
        )
        consumed_flags.append(consumed)
        statuses.append(action.status)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert consumed_flags.count(True) == 1
    assert set(statuses) == {"executing"}
    assert store.pending_count() == 0


# ----------------------------------------------------------------------
# State fingerprint
# ----------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_data_sensitive(controlled_dataset):
    data_path, changes_path = controlled_dataset
    if not changes_path.exists():
        changes_path.write_text("")
    transaction = {
        "id": "tx_categorized",
        "date": "2026-10-01",
        "merchant": "Market",
        "amount": 40.0,
        "currency": "PLN",
        "category_hint": "Groceries",
    }

    baseline = compute_state_fingerprint(data_path, changes_path, transaction)
    assert baseline == compute_state_fingerprint(data_path, changes_path, transaction)
    assert len(baseline) == 64

    changed_tx = dict(transaction, category_hint=None)
    assert compute_state_fingerprint(data_path, changes_path, changed_tx) != baseline
    assert compute_state_fingerprint(data_path, changes_path, None) != baseline

    # A ledger write invalidates every previously bound fingerprint.
    ledger_text = changes_path.read_text()
    changes_path.write_text(ledger_text + '{"marker": "new event"}\n')
    assert compute_state_fingerprint(data_path, changes_path, transaction) != baseline
