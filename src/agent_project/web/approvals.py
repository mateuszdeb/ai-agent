"""One-time decision lifecycle: server-side PendingAction store (T-09).

A consent decision binds one random ``approval_id`` to exactly one validated
tool call, one run, one owner session and one data fingerprint. Execution
parameters are read only from the stored PendingAction; the domain
``pending_del_<id>`` action identifier is presentation data and cannot
authorize a write on its own.

The store is intentionally in-memory: a restart drops proposals and their
context, so no old consent can ever replay a write. It is thread-safe and
never acquires the ledger lock itself; waiting for a user decision must keep
the ledger available to other readers and writers. State fingerprinting is a
pure helper: the calling executor combines it with ``LedgerGuard`` in its own
critical section when checking data freshness.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_args

from agent_project.web.schemas import (
    ApprovalCard,
    ApprovalKind,
    ApprovalStatus,
    DecisionReceipt,
    ToolObservation,
    TransactionSummary,
)

APPROVAL_TTL_SECONDS = 10 * 60.0
"""A single proposal may wait at most 10 minutes for an explicit decision."""

TERMINAL_ACTION_STATUSES: frozenset[ApprovalStatus] = frozenset(
    {
        "rejected",
        "expired",
        "stale",
        "completed",
        "failed",
        "outcome_unknown",
    }
)

VALID_ACTION_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    "pending": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"executing", "stale"}),
    "executing": frozenset({"completed", "failed", "outcome_unknown"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "stale": frozenset(),
    "completed": frozenset(),
    "failed": frozenset(),
    "outcome_unknown": frozenset(),
}


FINGERPRINT_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
"""Expected shape of a SHA-256 data fingerprint built by this module."""


class ApprovalStoreError(Exception):
    """Base exception for pending decision lifecycle errors."""


class ApprovalNotFoundError(ApprovalStoreError):
    """The proposal does not exist or belongs to another run."""


class ApprovalAccessDeniedError(ApprovalStoreError):
    """A session attempted to touch an action owned by another session."""


class ApprovalConflictError(ApprovalStoreError):
    """The request contradicts the lifecycle, e.g. execution without consent."""


class ApprovalCapacityError(ApprovalStoreError):
    """At-capacity storage has no finished proposal to retire."""


def _digest_or_empty(path: Path) -> str:
    """Hash file content; a missing file hashes as empty content.

    A dataset may legitimately have no ledger yet. The identity of "no file"
    and "empty file" is the same fingerprint state, and any later event
    changes the digest, so freshness checks remain sound.
    """
    try:
        content = Path(path).read_bytes()
    except FileNotFoundError:
        content = b""
    return hashlib.sha256(content).hexdigest()


def compute_state_fingerprint(
    data_path: Path,
    changes_path: Path,
    transaction: Mapping[str, Any] | None = None,
) -> str:
    """Hash the base dataset, the ledger and one transaction into a fingerprint.

    Caller contract: run this inside the caller's ledger critical section
    (``LedgerGuard``) so all three inputs describe one consistent state. The
    helper never locks, never repairs and never parses file contents; raw
    persistence failures propagate to the caller. Storage bytes are hashed as
    read, so any change to the dataset, the ledger ordering or the effective
    transaction fields produces a different fingerprint.
    """
    data_digest = _digest_or_empty(Path(data_path))
    ledger_digest = _digest_or_empty(Path(changes_path))
    transaction_canonical = json.dumps(
        dict(transaction) if transaction is not None else None,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload = json.dumps(
        {
            "data": data_digest,
            "ledger": ledger_digest,
            "transaction": transaction_canonical,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class PendingAction:
    """Internal server-side proposal; never handed to a client as a DTO.

    ``arguments`` is a frozen, validated copy: mutating the caller's mapping
    after registration cannot silently redirect a later execution. The known
    ``approval_id`` is a fresh random identifier; the controller domain
    ``action_id`` (e.g. ``pending_del_tx_104``) rides along for display only.
    """

    approval_id: str
    owner_session_id: str
    run_id: str
    tool_call_id: str
    tool: str
    kind: ApprovalKind
    arguments: Mapping[str, Any]
    transaction: TransactionSummary
    fingerprint: str
    reason: str = ""
    action_id: str | None = None
    status: ApprovalStatus = "pending"
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_monotonic: float = 0.0
    result: ToolObservation | None = None
    finished_monotonic: float | None = None

    def to_card(self) -> ApprovalCard:
        """Public presentation card: no owner, arguments or fingerprint."""
        return ApprovalCard(
            approval_id=self.approval_id,
            run_id=self.run_id,
            tool_call_id=self.tool_call_id,
            kind=self.kind,
            status=self.status,
            action_id=self.action_id,
            transaction=self.transaction,
            reason=self.reason,
            expires_at=self.expires_at,
            previous_category=self._category_field("previous_category"),
            proposed_category=self._category_field("category"),
        )

    def to_receipt(self) -> DecisionReceipt:
        """Public acknowledgment of one decided or closed proposal."""
        return DecisionReceipt(
            approval_id=self.approval_id,
            run_id=self.run_id,
            status=self.status,
            result=self.result,
        )

    def _category_field(self, key: str) -> str | None:
        if self.kind != "overwrite_category":
            return None
        value = self.arguments.get(key)
        return str(value) if value not in (None, "") else None


class ApprovalStore:
    """Bounded, thread-safe lifecycle owner for one-time decisions.

    Guarantees:
      * a decision is expressible once; later decisions observe the stored
        state instead of flipping it or triggering any execution,
      * TTL expiry physically closes untouched pending proposals during reads
        (injectable monotonic + UTC clock gives deterministic tests),
      * ``begin_execution`` is the single-use consumption point: ``approved``
        becomes ``executing`` exactly once per proposal,
      * data changed between proposal and consumption closes as ``stale``,
      * no method ever acquires the shared ledger lock.
    """

    def __init__(
        self,
        *,
        decision_ttl: float = APPROVAL_TTL_SECONDS,
        max_actions: int = 256,
        clock: Any = None,
        time_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(decision_ttl, bool)
            or not isinstance(decision_ttl, (int, float))
            or not math.isfinite(decision_ttl)
            or decision_ttl <= 0
            or isinstance(max_actions, bool)
            or not isinstance(max_actions, int)
            or max_actions < 1
        ):
            raise ValueError("ApprovalStore limits must be positive numbers.")

        self._decision_ttl = float(decision_ttl)
        self._max_actions = max_actions

        if clock is not None and hasattr(clock, "monotonic") and hasattr(clock, "now"):
            self._monotonic_clock = clock.monotonic
            self._time_provider = clock.now
        else:
            self._monotonic_clock = clock if callable(clock) else time.monotonic
            self._time_provider = time_provider or (lambda: datetime.now(UTC))

        self._mutex = threading.RLock()
        self._resolution = threading.Condition(self._mutex)
        self._actions: dict[str, PendingAction] = {}

    # ------------------------------------------------------------------
    # Registration and reads
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        owner_session_id: str,
        run_id: str,
        tool_call_id: str,
        tool: str,
        kind: ApprovalKind,
        arguments: Mapping[str, Any],
        transaction: TransactionSummary,
        fingerprint: str,
        reason: str = "",
        action_id: str | None = None,
    ) -> PendingAction:
        """Store one validated proposal bound to run, owner and fingerprint.

        The domain ``action_id`` is accepted only as presentation metadata and
        never as a decision key: lookups use the random ``approval_id``.
        """
        owner_session_id = _require_text(owner_session_id, "owner_session_id")
        run_id = _require_text(run_id, "run_id")
        tool_call_id = _require_text(tool_call_id, "tool_call_id")
        tool = _require_text(tool, "tool")
        kind = _validate_kind(kind)
        if not isinstance(fingerprint, str) or not FINGERPRINT_HEX_RE.match(
            fingerprint
        ):
            raise ValueError(
                "fingerprint must be the 64-hex SHA-256 computed by this module."
            )
        reason = str(reason or "")
        action_id = (
            _require_text(action_id, "action_id") if action_id is not None else None
        )
        if not isinstance(arguments, Mapping):
            raise ValueError("Validated arguments must be provided as a mapping.")
        if not isinstance(transaction, TransactionSummary):
            raise ValueError("Transaction must be a validated TransactionSummary.")

        with self._mutex:
            self._retire_unlocked()
            now_monotonic = self._monotonic_clock()
            now_datetime = self._time_provider()
            approval_id = f"apr_{secrets.token_urlsafe(12)}"
            pending = PendingAction(
                approval_id=approval_id,
                owner_session_id=owner_session_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool=tool,
                kind=kind,
                arguments=MappingProxyType(dict(arguments)),
                transaction=transaction,
                fingerprint=fingerprint,
                reason=reason,
                action_id=action_id,
                status="pending",
                version=1,
                created_at=now_datetime,
                expires_at=now_datetime + timedelta(seconds=self._decision_ttl),
                expires_monotonic=now_monotonic + self._decision_ttl,
            )
            self._actions[approval_id] = pending
            return pending

    def get_card(
        self,
        run_id: str,
        approval_id: str,
        *,
        owner_session_id: str,
    ) -> ApprovalCard:
        """Return one presentation card, refreshing pending TTL expiry."""
        with self._mutex:
            action = self._get_locked(run_id, approval_id, owner_session_id)
            self._refresh_expiry_unlocked(action)
            return action.to_card()

    def list_cards(
        self,
        run_id: str,
        *,
        owner_session_id: str,
        pending_only: bool = True,
    ) -> list[ApprovalCard]:
        """Return cards for one run owned by one session, expired ones visible."""
        cards: list[ApprovalCard] = []
        with self._mutex:
            for action in self._actions.values():
                if action.run_id != run_id:
                    continue
                if action.owner_session_id != owner_session_id:
                    raise ApprovalAccessDeniedError(
                        "Another session's pending decision is not readable."
                    )
                self._refresh_expiry_unlocked(action)
                if pending_only and action.status != "pending":
                    continue
                cards.append(action.to_card())
        cards.sort(key=lambda card: card.expires_at)
        return cards

    # ------------------------------------------------------------------
    # Decisions and execution lifecycle
    # ------------------------------------------------------------------

    def decide(
        self,
        run_id: str,
        approval_id: str,
        *,
        owner_session_id: str,
        decision: str,
    ) -> DecisionReceipt:
        """Record one approve/reject; replays never flip the state again.

        An already-decided, expired or otherwise closed proposal returns its
        stored state and result without any side effect. The HTTP layer maps
        those receipt statuses onto its own response vocabulary.
        """
        decision = _require_text(decision, "decision")
        if decision not in ("approve", "reject"):
            raise ValueError("Decision must be 'approve' or 'reject'.")

        with self._mutex:
            action = self._get_locked(run_id, approval_id, owner_session_id)
            self._refresh_expiry_unlocked(action)
            if action.status == "pending":
                action.status = "approved" if decision == "approve" else "rejected"
                action.version += 1
                self._notify_resolution_locked(action)
                return action.to_receipt()
            # Idempotent replay: a repeated or conflicting decision observes
            # the recorded state. It authorizes nothing and executes nothing.
            return action.to_receipt()

    def wait_for_resolution(
        self,
        run_id: str,
        approval_id: str,
        *,
        owner_session_id: str,
        timeout: float | None = None,
        slice_seconds: float = 0.05,
    ) -> DecisionReceipt:
        """Block until the proposal is decided, expired or otherwise closed.

        This is the worker's synchronization object between the HTTP decision
        and the write: waiting happens here and never while holding a ledger
        lock. ``timeout`` is an optional additional deadline measured with the
        store's monotonic clock; a proposal always also closes at its own TTL.
        ``slice_seconds`` bounds each real-time wait slice so a virtual clock
        that later advances is still observed; notifications wake immediately.
        """
        if timeout is not None and not _is_nonnegative_number(timeout):
            raise ValueError("Wait timeout must be finite and non-negative.")
        if not _is_nonnegative_number(slice_seconds) or slice_seconds <= 0:
            raise ValueError("Wait slice must be a positive finite number.")
        slice_seconds_f = min(float(slice_seconds), 0.5)

        with self._mutex:
            action = self._get_locked(run_id, approval_id, owner_session_id)
            deadline = (
                None if timeout is None else self._monotonic_clock() + float(timeout)
            )
            while action.status == "pending":
                wait_until = action.expires_monotonic
                if deadline is not None and deadline < wait_until:
                    wait_until = deadline
                remaining = wait_until - self._monotonic_clock()
                if remaining <= 0:
                    self._refresh_expiry_unlocked(action)
                    break
                self._resolution.wait(min(remaining, slice_seconds_f))
                self._refresh_expiry_unlocked(action)
            return action.to_receipt()

    def begin_execution(
        self,
        run_id: str,
        approval_id: str,
        *,
        owner_session_id: str,
    ) -> tuple[PendingAction, bool]:
        """Consume an approved decision once, moving it to ``executing``.

        Returns ``(action, consumed)``. Only a pending ``approved`` state can
        be consumed; a second call observes ``executing`` or the stored result
        instead of re-authorizing the write. A proposal already closed as
        expired, stale, rejected, completed, failed or outcome unknown is
        returned untouched with ``consumed=False``; a still pending proposal
        without a recorded approval raises ``ApprovalConflictError``.
        """
        with self._mutex:
            action = self._get_locked(run_id, approval_id, owner_session_id)
            self._refresh_expiry_unlocked(action)
            if action.status == "approved":
                action.status = "executing"
                action.version += 1
                return action, True
            if action.status != "pending":
                return action, False
            raise ApprovalConflictError(
                "Only an approved single-use decision can start execution."
            )

    def mark_stale(
        self,
        run_id: str,
        approval_id: str,
        *,
        owner_session_id: str,
    ) -> DecisionReceipt:
        """Invalidate a decided proposal whose data fingerprint changed.

        The proposal becomes terminal: a new proposal and a new explicit
        decision are required before any execution.
        """
        with self._mutex:
            action = self._get_locked(run_id, approval_id, owner_session_id)
            if action.status != "approved":
                raise ApprovalConflictError(
                    "Only a not-yet-executed approved decision becomes stale."
                )
            action.status = "stale"
            action.version += 1
            action.finished_monotonic = self._monotonic_clock()
            return action.to_receipt()

    def record_result(
        self,
        run_id: str,
        approval_id: str,
        *,
        owner_session_id: str,
        result: ToolObservation,
    ) -> DecisionReceipt:
        """Close an executing proposal with the controller's real outcome.

        ``success`` maps to ``completed``; every other controller observation
        closes the proposal as ``failed``. An ``outcome_unknown`` proposal can
        still adopt a late result without changing its status: the UI must not
        be told a write succeeded when the durable outcome is unknown.
        """
        if not isinstance(result, ToolObservation):
            raise ValueError("Result must be a validated ToolObservation.")

        with self._mutex:
            action = self._get_locked(run_id, approval_id, owner_session_id)
            if action.status == "outcome_unknown" and action.result is None:
                action.result = result
                action.version += 1
                return action.to_receipt()
            if action.status != "executing":
                raise ApprovalConflictError(
                    "Only an executing proposal accepts a controller result."
                )
            action.status = "completed" if result.status == "success" else "failed"
            action.result = result
            action.version += 1
            action.finished_monotonic = self._monotonic_clock()
            return action.to_receipt()

    def record_outcome_unknown(
        self,
        run_id: str,
        approval_id: str,
        *,
        owner_session_id: str,
    ) -> DecisionReceipt:
        """Mark a write whose durable effect could not be confirmed.

        An uncertain write is never retried automatically; see
        ``record_result`` for late-arriving evidence.
        """
        with self._mutex:
            action = self._get_locked(run_id, approval_id, owner_session_id)
            if action.status != "executing":
                raise ApprovalConflictError(
                    "Only an executing proposal can become outcome unknown."
                )
            action.status = "outcome_unknown"
            action.version += 1
            action.finished_monotonic = self._monotonic_clock()
            return action.to_receipt()

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Total number of stored proposals, including closed ones."""
        with self._mutex:
            return len(self._actions)

    def pending_count(self) -> int:
        """Number of proposals still waiting for a decision."""
        with self._mutex:
            return sum(1 for a in self._actions.values() if a.status == "pending")

    def get_internal(
        self,
        run_id: str,
        approval_id: str,
        *,
        owner_session_id: str,
    ) -> PendingAction:
        """Read validated arguments and fingerprint (trusted executor only).

        This never touches the public DTO layer. Consumers must treat the
        returned arguments as immutable and re-check any freshness assumption
        against a fingerprint under the ledger lock themselves.
        """
        with self._mutex:
            return self._get_locked(run_id, approval_id, owner_session_id)

    # ------------------------------------------------------------------
    # Internal mechanics
    # ------------------------------------------------------------------

    def _get_locked(
        self,
        run_id: str,
        approval_id: str,
        owner_session_id: str,
    ) -> PendingAction:
        """Lookup with run binding and ownership checks; caller holds the mutex."""
        action = self._actions.get(approval_id)
        if action is None or action.run_id != run_id:
            raise ApprovalNotFoundError("This decision is unknown for the given run.")
        if action.owner_session_id != owner_session_id:
            raise ApprovalAccessDeniedError("This decision belongs to another session.")
        return action

    def _refresh_expiry_unlocked(self, action: PendingAction) -> None:
        """Physically expire an untouched pending proposal past its TTL."""
        if action.status != "pending":
            return
        if self._monotonic_clock() >= action.expires_monotonic:
            action.status = "expired"
            action.version += 1
            action.finished_monotonic = self._monotonic_clock()
            self._notify_resolution_locked(action)

    def _notify_resolution_locked(self, action: PendingAction) -> None:
        """Wake resolution waiters once a proposal is no longer pending."""
        if action.status == "pending":
            return
        with self._resolution:
            self._resolution.notify_all()

    def _retire_unlocked(self) -> None:
        """Keep the store bounded; never evict a proposal still in use."""
        if len(self._actions) < self._max_actions:
            return
        for action in sorted(
            self._actions.values(),
            key=lambda a: (
                a.finished_monotonic if a.finished_monotonic is not None else -1.0,
                a.approval_id,
            ),
        ):
            if action.status in TERMINAL_ACTION_STATUSES:
                del self._actions[action.approval_id]
                if len(self._actions) < self._max_actions:
                    return
        raise ApprovalCapacityError(
            "Too many pending decisions; no finished proposal can be retired."
        )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value


def _is_nonnegative_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _validate_kind(kind: object) -> ApprovalKind:
    allowed = get_args(ApprovalKind)
    if kind not in allowed:
        raise ValueError(f"Approval kind must be one of {allowed}.")
    return kind
