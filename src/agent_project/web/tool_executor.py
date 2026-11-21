"""WebToolExecutor: bind the existing controller to one-time approvals (T-10,
T-11).

The executor is trusted application code placed between the agent loop and
the domain controller. For the sensitive delete path it always probes the
controller without any consent, registers a server-side PendingAction under
the ledger lock, waits for the decision outside that lock, re-checks the
data fingerprint, consumes the approval exactly once and re-runs the one
saved validated call through a single-use trusted hook. A model-level
``confirmed=True`` never carries authorization; only an approved, fresh,
single-use decision enables one write.

For the web path, a proposed category overwrite follows the same pattern:
the executor validates with the exact common model, probes the controller
without the forced intent and turns only a real overwrite of a different
existing category into an one-time ``overwrite_category`` decision. The
default CLI/MCP tool contract is untouched.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError

from agent_project.agent import (
    ToolCallContext,
    execute_controlled_tool,
    parse_tool_args,
)
from agent_project.models import AssignCategoryArgs, ToolStatus
from agent_project.state_lock import (
    LedgerBusyError,
    LedgerCorruptionError,
    LedgerGuard,
    LedgerWriteError,
)
from agent_project.tools import load_effective_transactions
from agent_project.web.approvals import (
    ApprovalStore,
    PendingAction,
    compute_state_fingerprint,
)
from agent_project.web.schemas import (
    DecisionReceipt,
    ObservationData,
    ToolObservation,
    TransactionSummary,
)

_LOGGER = logging.getLogger(__name__)

_DELETE_TOOL = "delete_flagged_transaction"
_ASSIGN_TOOL = "assign_category"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def transaction_summary_from_record(record: Mapping[str, Any]) -> TransactionSummary:
    """Build the allowlisted card data from one effective transaction record."""
    return TransactionSummary(
        transaction_id=str(record["id"]),
        date=str(record.get("date") or ""),
        merchant=str(record.get("merchant") or ""),
        amount=float(record.get("amount") or 0.0),
        currency=str(record.get("currency") or "PLN"),
        category=(record.get("category_hint") or None) or None,
    )


def observation_from_result(
    result: Mapping[str, Any], tool: str = _DELETE_TOOL
) -> ToolObservation:
    """Safe, allowlisted observation derived from a controller result dict.

    Only identifier-shaped action ids and transaction ids pass through; no
    message text, error strings or raw arguments are carried. An unknown
    status never reaches the public vocabulary.
    """
    allowed_statuses = get_args(ToolStatus)
    raw_status = result.get("status")
    status = raw_status if raw_status in allowed_statuses else "error"
    action_id_raw = result.get("action_id")
    action_id = (
        str(action_id_raw)
        if isinstance(action_id_raw, str) and IDENTIFIER_RE.match(action_id_raw)
        else None
    )
    tx_id_raw = result.get("transaction_id")
    tx_id = (
        str(tx_id_raw)
        if isinstance(tx_id_raw, str) and IDENTIFIER_RE.match(tx_id_raw)
        else None
    )
    category_value = result.get("category")
    category = category_value if category_value not in (None, "") else None
    return ToolObservation(
        sequence=1,
        step=1,
        tool=tool,
        status=status,
        action_id=action_id,
        data=ObservationData(transaction_id=tx_id, category=category),
    )


class WebToolExecutor:
    """Trusted adapter enforcing the one-time approval policy (AC-15..AC-23).

    Responsibility split:
      * non-sensitive tools other than category writes pass straight through
        to the existing controller;
      * deletion is a two-phase server flow: consent-free proposal, bounded
        waiting, then a single authorized re-execution;
      * a proposed overwrite of an existing category is likewise an one-time
        server-side decision; a normal assignment and every non-overwrite
        outcome keep the plain controller contract (T-11);
      * a decision recorded on :class:`ApprovalStore` closes as
        ``rejected``, ``expired`` or ``stale`` without any write.
    """

    def __init__(
        self,
        *,
        approvals: ApprovalStore,
        data_path: Path,
        changes_path: Path,
        run_id: str,
        owner_session_id: str,
        decision_wait_seconds: float | None = None,
        on_awaiting_approval: Callable[[], None] | None = None,
        on_resumed: Callable[[], None] | None = None,
        on_registered: Callable[[PendingAction], None] | None = None,
        on_resolution: Callable[[DecisionReceipt], None] | None = None,
        before_recheck: Callable[[], None] | None = None,
    ) -> None:
        self._approvals = approvals
        self._data_path = Path(data_path)
        self._changes_path = Path(changes_path)
        self._run_id = run_id
        self._owner_session_id = owner_session_id
        self._decision_wait_seconds = decision_wait_seconds
        self._on_awaiting_approval = on_awaiting_approval
        self._on_resumed = on_resumed
        self._on_registered = on_registered
        self._on_resolution = on_resolution
        self._before_recheck = before_recheck

    def __call__(
        self,
        tool_name: str,
        raw_args: Any,
        *,
        context: ToolCallContext,
    ) -> dict[str, Any]:
        """One controller-bound tool call executed under the approval policy."""
        if tool_name == _DELETE_TOOL:
            return self._handle_delete(raw_args, context)
        if tool_name == _ASSIGN_TOOL:
            return self._handle_assign(raw_args, context)
        # Everything else keeps the existing controller contract.
        return execute_controlled_tool(
            tool_name,
            raw_args,
            data_path=self._data_path,
            changes_path=self._changes_path,
        )

    # ------------------------------------------------------------------
    # Phase 1: consent-free proposal through the controller
    # ------------------------------------------------------------------

    def _handle_delete(
        self,
        raw_args: Any,
        context: ToolCallContext,
    ) -> dict[str, Any]:
        proposal = execute_controlled_tool(
            _DELETE_TOOL,
            raw_args,
            data_path=self._data_path,
            changes_path=self._changes_path,
        )
        # No consent involved yet: the controller forced confirmed=False and,
        # for sensitive tools, already verified the transaction exists.
        if proposal.get("status") != "confirmation_required":
            return proposal

        pending_or_result = self._run_registration(
            lambda: self._register_pending(proposal, context),
            str(proposal.get("transaction_id") or ""),
        )
        if isinstance(pending_or_result, PendingAction):
            return self._await_and_execute(pending_or_result)
        return pending_or_result

    def _register_pending(
        self,
        proposal: Mapping[str, Any],
        context: ToolCallContext,
    ) -> PendingAction:
        """Bind one proposal under the shared ledger lock; nobody else sees it."""
        with LedgerGuard(self._changes_path):
            records = load_effective_transactions(self._data_path, self._changes_path)
            transaction_id = str(proposal.get("transaction_id") or "")
            record = next(
                (item for item in records if item.get("id") == transaction_id),
                None,
            )
            if record is None:
                return_key = {
                    "status": "not_found",
                    "transaction_id": transaction_id,
                    "message": "Transaction not found or already deleted.",
                }
                raise _RegistrationAbort(return_key)
            arguments = {
                "transaction_id": transaction_id,
                "reason": str(proposal.get("reason") or ""),
                # The stored proposal itself never carries consent.
                "confirmed": False,
            }
            return self._approvals.register(
                owner_session_id=self._owner_session_id,
                run_id=self._run_id,
                tool_call_id=context.tool_call_id,
                tool=_DELETE_TOOL,
                kind="delete_transaction",
                arguments=arguments,
                transaction=transaction_summary_from_record(record),
                fingerprint=compute_state_fingerprint(
                    self._data_path, self._changes_path, record
                ),
                reason=str(proposal.get("reason") or ""),
                action_id=str(proposal.get("action_id") or ""),
            )

    def _run_registration(
        self, register: Callable[[], PendingAction], transaction_id: str
    ) -> PendingAction | dict[str, Any]:
        """Bind one proposal under the ledger; translate persistence errors."""
        try:
            return register()
        except LedgerBusyError:
            return {
                "status": "state_busy",
                "transaction_id": transaction_id,
                "message": ("Transaction state is busy. No operation was started."),
            }
        except LedgerCorruptionError:
            return {
                "status": "state_conflict",
                "transaction_id": transaction_id,
                "message": ("Transaction state is inconsistent. Review current data."),
            }
        except LedgerWriteError:
            return {
                "status": "error",
                "error_code": "outcome_unknown",
                "message": (
                    "Ledger write outcome is unknown. Do not retry automatically."
                ),
            }
        except _RegistrationAbort as abort:
            return dict(abort.result)

    def _await_and_execute(self, pending: PendingAction) -> dict[str, Any]:
        """Wait outside the ledger lock, then honor the decision once."""
        self._publish(self._on_registered, pending)
        self._publish(self._on_awaiting_approval)

        receipt = self._approvals.wait_for_resolution(
            self._run_id,
            pending.approval_id,
            owner_session_id=self._owner_session_id,
            timeout=self._decision_wait_seconds,
        )
        self._publish(self._on_resolution, receipt)
        self._publish(self._on_resumed)

        if receipt.status == "approved":
            return self._execute_approved(pending)
        return rejection_observation(
            pending,
            message=("Confirmation was denied. The proposed action was not performed.")
            if receipt.status == "rejected"
            else (
                "The decision window expired. The proposed action was not performed."
            ),
        )

    # ------------------------------------------------------------------
    # Web policy: category writes bound to one-time overwrite decisions
    # ------------------------------------------------------------------

    def _handle_assign(
        self,
        raw_args: Any,
        context: ToolCallContext,
    ) -> dict[str, Any]:
        """Route assign_category through the overwrite approval policy.

        The executor validates with the exact common model, probes the
        controller without the forced intent and keeps the plain controller
        contract when no decision belongs to the outcome: an assignment with
        no existing category, an identical category, a non-forced proposal
        and every error result. Only a proposed overwrite of a different
        existing category becomes an one-time server-side decision whose
        approval covers exactly the shown old and new values (AC-22).
        """
        try:
            clean_raw = parse_tool_args(raw_args)
        except ValueError:
            return execute_controlled_tool(
                _ASSIGN_TOOL,
                raw_args,
                data_path=self._data_path,
                changes_path=self._changes_path,
            )
        try:
            parsed = AssignCategoryArgs.model_validate(clean_raw, strict=True)
        except (ValidationError, ValueError):
            return execute_controlled_tool(
                _ASSIGN_TOOL,
                raw_args,
                data_path=self._data_path,
                changes_path=self._changes_path,
            )

        # The probing attempt never carries the model's forced intent.
        proposal = parsed.model_dump()
        proposal["force_override"] = False
        probe = execute_controlled_tool(
            _ASSIGN_TOOL,
            proposal,
            data_path=self._data_path,
            changes_path=self._changes_path,
        )
        if probe.get("status") != "already_assigned":
            return probe
        if probe.get("current_category") == parsed.category:
            return probe
        if not parsed.force_override:
            return probe

        outcome = self._run_registration(
            lambda: self._register_overwrite(parsed.model_dump(), context),
            str(parsed.transaction_id),
        )
        if isinstance(outcome, dict):
            return outcome
        return self._await_and_execute(outcome)

    def _register_overwrite(
        self,
        proposal: Mapping[str, Any],
        context: ToolCallContext,
    ) -> PendingAction:
        """Bind one overwrite proposal under the shared ledger lock.

        The registration re-checks the observed category under the lock, so
        the card and the later execution both rely on one consistent state
        snapshot: the shown old value is the record read together with the
        approval fingerprint.
        """
        with LedgerGuard(self._changes_path):
            records = load_effective_transactions(self._data_path, self._changes_path)
            transaction_id = str(proposal["transaction_id"])
            record = next(
                (item for item in records if item.get("id") == transaction_id),
                None,
            )
            if record is None:
                raise _RegistrationAbort(
                    {
                        "status": "not_found",
                        "transaction_id": transaction_id,
                        "message": "Transaction not found or already deleted.",
                    }
                )
            current = record.get("category_hint")
            if not current or current == proposal["category"]:
                raise _RegistrationAbort(
                    {
                        "status": "already_assigned",
                        "transaction_id": transaction_id,
                        "current_category": current,
                        "message": (
                            f"Transaction '{transaction_id}' already has "
                            f"category '{current}'."
                        ),
                    }
                )
            arguments = {
                "transaction_id": transaction_id,
                "category": str(proposal["category"]),
                "rationale": str(proposal.get("rationale") or ""),
                "previous_category": str(current),
                # The stored proposal is itself the forced write being approved.
                "force_override": True,
            }
            return self._approvals.register(
                owner_session_id=self._owner_session_id,
                run_id=self._run_id,
                tool_call_id=context.tool_call_id,
                tool=_ASSIGN_TOOL,
                kind="overwrite_category",
                arguments=arguments,
                transaction=transaction_summary_from_record(record),
                fingerprint=compute_state_fingerprint(
                    self._data_path, self._changes_path, record
                ),
                reason=str(proposal.get("rationale") or ""),
            )

    # ------------------------------------------------------------------
    # Phase 2: single authorized execution after an explicit decision
    # ------------------------------------------------------------------

    def _execute_approved(self, pending: PendingAction) -> dict[str, Any]:
        """Re-check freshness, consume the decision once, run the saved call."""
        # Test seam, fired before locking: a decision may carry simultaneous
        # data changes that must become observable before the re-check.
        self._publish(self._before_recheck)
        try:
            with LedgerGuard(self._changes_path):
                records = load_effective_transactions(
                    self._data_path, self._changes_path
                )
                record = next(
                    (
                        item
                        for item in records
                        if item.get("id") == pending.arguments["transaction_id"]
                    ),
                    None,
                )
                fingerprint = compute_state_fingerprint(
                    self._data_path, self._changes_path, record
                )
                if fingerprint != pending.fingerprint:
                    self._mark_stale_safely(pending)
                    return {
                        "status": "state_conflict",
                        "action_id": pending.action_id,
                        "transaction_id": pending.arguments["transaction_id"],
                        "message": (
                            "Transaction state changed since the proposal. "
                            "A new decision is required."
                        ),
                    }
                action, consumed = self._approvals.begin_execution(
                    self._run_id,
                    pending.approval_id,
                    owner_session_id=self._owner_session_id,
                )
                if not consumed:
                    replay = self._replay_result(action)
                    self._record(pending, replay)
                    return replay
                arguments = dict(pending.arguments)
                if pending.kind == "overwrite_category":
                    # The recorded old value is card metadata; the saved
                    # tool call itself stays a valid controller argument.
                    arguments.pop("previous_category", None)
                result = execute_controlled_tool(
                    pending.tool,
                    arguments,
                    data_path=self._data_path,
                    changes_path=self._changes_path,
                    confirmation_handler=(
                        # Only the sensitive path has a self-confirmation
                        # hook; an approved overwrite is authorized by the
                        # consumed decision and the re-checked arguments.
                        self._one_time_hook(pending)
                        if pending.kind == "delete_transaction"
                        else None
                    ),
                )
                self._record(pending, result)
                return result
        except LedgerBusyError:
            return {
                "status": "state_busy",
                "action_id": pending.action_id,
                "transaction_id": pending.arguments["transaction_id"],
                "message": "Transaction state is busy. No operation was started.",
            }
        except LedgerCorruptionError:
            observation = {
                "status": "state_conflict",
                "action_id": pending.action_id,
                "transaction_id": pending.arguments.get("transaction_id"),
                "message": ("Transaction state is inconsistent. Review current data."),
            }
            self._record(pending, observation)
            return observation

    def _one_time_hook(self, pending: PendingAction) -> Callable[[str, dict], bool]:
        """Return literal True exactly once, only for the bound saved call."""
        state = {"consumed": False}

        def handler(action_id: str, clean_args: Mapping[str, Any]) -> bool:
            if state["consumed"]:
                return False
            if action_id != pending.action_id:
                return False
            if dict(clean_args) != dict(pending.arguments):
                return False
            state["consumed"] = True
            return True

        return handler

    # ------------------------------------------------------------------
    # Result recording and small helpers
    # ------------------------------------------------------------------

    def _record(self, pending: PendingAction, result: Mapping[str, Any]) -> None:
        """Best-effort close of the proposal with the controller outcome."""
        observation = observation_from_result(result, tool=pending.tool)
        try:
            if result.get("error_code") == "outcome_unknown":
                self._approvals.record_outcome_unknown(
                    self._run_id,
                    pending.approval_id,
                    owner_session_id=self._owner_session_id,
                )
            self._approvals.record_result(
                self._run_id,
                pending.approval_id,
                owner_session_id=self._owner_session_id,
                result=observation,
            )
        except Exception:
            _LOGGER.debug("Could not record outcome on pending decision.")

    def _mark_stale_safely(self, pending: PendingAction) -> None:
        try:
            self._approvals.mark_stale(
                self._run_id,
                pending.approval_id,
                owner_session_id=self._owner_session_id,
            )
        except Exception:
            _LOGGER.debug("Could not mark a pending decision stale.")

    def _replay_result(self, action: PendingAction) -> dict[str, Any]:
        """Observed-only result for an already consumed decision (AC-19)."""
        status = action.result.status if action.result is not None else "error"
        return {
            "status": status,
            "action_id": action.action_id,
            "transaction_id": action.arguments.get("transaction_id"),
            "message": ("This decision was already consumed; use the recorded result."),
        }

    def _publish(self, hook: Callable[..., None] | None, *args: Any) -> None:
        if hook is None:
            return
        try:
            hook(*args)
        except Exception:
            # Progress publication must never alter or retry execution.
            _LOGGER.warning("Execution progress hook failed; not retried.")


class _RegistrationAbort(Exception):
    """Internal signal carrying a safe result instead of a pending action."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(str(result))
        self.result = result


def rejection_observation(pending: PendingAction, *, message: str) -> dict[str, Any]:
    """Structured denial result: consent closed without any write."""
    return {
        "status": "confirmation_denied",
        "action_id": pending.action_id,
        "transaction_id": pending.arguments.get("transaction_id"),
        "message": message,
    }
