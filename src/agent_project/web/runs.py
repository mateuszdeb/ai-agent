"""Bounded in-memory store for agent runs, versioned snapshots, and deduplication."""

from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_project.web.schemas import (
    ApprovalStatus,
    RunError,
    RunReceipt,
    RunSnapshot,
    RunStatus,
    ToolObservation,
)

TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "step_limit_reached", "failed", "outcome_unknown"}
)
ACTIVE_RUN_STATUSES = frozenset({"running", "awaiting_approval"})

VALID_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    "running": frozenset(
        {
            "awaiting_approval",
            "completed",
            "step_limit_reached",
            "failed",
            "outcome_unknown",
        }
    ),
    "awaiting_approval": frozenset(
        {
            "running",
            "completed",
            "step_limit_reached",
            "failed",
            "outcome_unknown",
        }
    ),
    "completed": frozenset(),
    "step_limit_reached": frozenset(),
    "failed": frozenset(),
    "outcome_unknown": frozenset(),
}


class RunStoreError(Exception):
    """Base exception for run store domain errors."""


class RunInProgressError(RunStoreError):
    """Raised when an operation conflicts with an ongoing active analysis."""


class RequestIdConflictError(RunStoreError):
    """Raised when a request ID is reused with a different goal payload."""


class RunNotFoundError(RunStoreError):
    """Raised when a requested run does not exist."""


class RunAccessDeniedError(RunStoreError):
    """Raised when a session attempts to access a run owned by another session."""


class InvalidRunStateTransitionError(RunStoreError):
    """Raised when an invalid status transition is attempted on a run."""


def compute_request_hash(goal: str) -> str:
    """Compute deterministic SHA-256 hash of the goal for idempotency checks."""
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestReceipt:
    """Idempotency record binding a request key and goal hash to an accepted run."""

    owner_session_id: str
    request_id: str
    request_hash: str
    run_id: str
    created_at: datetime


@dataclass
class Run:
    """Internal server representation of an agent run; not a public DTO."""

    run_id: str
    owner_session_id: str
    goal: str
    status: RunStatus = "running"
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    result: str | None = None
    observations: list[ToolObservation] = field(default_factory=list)
    approval_outcome: ApprovalStatus | None = None
    error: RunError | None = None
    trace_path: str | Path | None = None
    completed_monotonic: float | None = None

    def to_snapshot(self) -> RunSnapshot:
        """Build public snapshot DTO without internal owner, cookie, or trace path."""
        return RunSnapshot(
            run_id=self.run_id,
            status=self.status,
            version=self.version,
            goal=self.goal,
            created_at=self.created_at,
            updated_at=self.updated_at,
            result=self.result,
            observations=list(self.observations),
            approval_outcome=self.approval_outcome,
            error=self.error,
        )

    def to_receipt(self) -> RunReceipt:
        """Build public 202 receipt acknowledgment DTO."""
        return RunReceipt(
            run_id=self.run_id,
            status=self.status,
            version=self.version,
        )


class RunStore:
    """Bounded, thread-safe in-memory store for agent runs and deduplication.

    Retains at most 100 finished runs and 24 hours of history; active runs are
    never evicted. Supports injectable clocks for deterministic testing.
    """

    def __init__(
        self,
        *,
        capacity: int = 100,
        max_terminal_runs: int | None = None,
        history_ttl: float = 24 * 60 * 60,
        clock: Any = None,
        time_provider: Callable[[], datetime] | None = None,
    ) -> None:
        effective_capacity = (
            max_terminal_runs if max_terminal_runs is not None else capacity
        )
        if (
            isinstance(history_ttl, bool)
            or not isinstance(history_ttl, (int, float))
            or not math.isfinite(history_ttl)
            or history_ttl <= 0
            or isinstance(effective_capacity, bool)
            or not isinstance(effective_capacity, int)
            or effective_capacity < 1
        ):
            raise ValueError("RunStore limits must be positive numbers.")

        self._capacity = effective_capacity
        self._history_ttl = history_ttl

        if clock is not None and hasattr(clock, "monotonic") and hasattr(clock, "now"):
            self._monotonic_clock = clock.monotonic
            self._time_provider = clock.now
        else:
            self._monotonic_clock = clock if callable(clock) else time.monotonic
            if time_provider is not None:
                self._time_provider = time_provider
            else:
                self._time_provider = lambda: datetime.now(UTC)

        self._mutex = threading.RLock()
        self._runs: dict[str, Run] = {}
        self._receipts: dict[tuple[str, str], RequestReceipt] = {}

    def _cleanup_unlocked(self, now_monotonic: float) -> None:
        """Evict expired terminal runs and enforce maximum capacity.

        Active runs are never evicted under any circumstances.
        When a run is evicted, its corresponding request receipt is also purged.
        """
        # 1. Evict terminal runs older than history_ttl
        expired_run_ids: set[str] = set()
        for run in self._runs.values():
            if run.status in TERMINAL_RUN_STATUSES:
                if (
                    run.completed_monotonic is not None
                    and (now_monotonic - run.completed_monotonic) >= self._history_ttl
                ):
                    expired_run_ids.add(run.run_id)

        for run_id in expired_run_ids:
            del self._runs[run_id]

        # 2. Enforce maximum capacity on remaining terminal runs
        terminal_runs = [
            r for r in self._runs.values() if r.status in TERMINAL_RUN_STATUSES
        ]
        if len(terminal_runs) > self._capacity:
            # Sort by completed_monotonic (oldest first); fallback to updated_at
            terminal_runs.sort(
                key=lambda r: (
                    r.completed_monotonic
                    if r.completed_monotonic is not None
                    else float("inf"),
                    r.updated_at,
                )
            )
            excess_count = len(terminal_runs) - self._capacity
            for r in terminal_runs[:excess_count]:
                expired_run_ids.add(r.run_id)
                del self._runs[r.run_id]

        # 3. Clean up receipts whose runs are no longer stored
        if expired_run_ids:
            purged_keys = [
                k
                for k, receipt in self._receipts.items()
                if receipt.run_id in expired_run_ids
            ]
            for k in purged_keys:
                del self._receipts[k]

    def cleanup(self) -> None:
        """Explicitly run retention and capacity cleanup."""
        with self._mutex:
            self._cleanup_unlocked(self._monotonic_clock())

    def create_run(
        self,
        owner_session_id: str,
        request_id: str,
        goal: str,
        *,
        trace_path: str | Path | None = None,
    ) -> tuple[Run, bool]:
        """Atomically accept a start request or return an existing deduplicated run.

        Returns (run, created) where created is True for a new run, or False
        for an idempotent deduplication replay.

        Raises:
            RequestIdConflictError: if the request ID was used with a different goal.
            RunInProgressError: if another analysis is active and this is not a replay.
            ValueError: if any required argument is blank.
        """
        if not owner_session_id or not owner_session_id.strip():
            raise ValueError("owner_session_id must be non-empty.")
        if not request_id or not request_id.strip():
            raise ValueError("request_id must be non-empty.")
        if not goal or not goal.strip():
            raise ValueError("goal must be non-empty.")

        with self._mutex:
            now_m = self._monotonic_clock()
            self._cleanup_unlocked(now_m)

            request_hash = compute_request_hash(goal)
            receipt_key = (owner_session_id, request_id)
            existing_receipt = self._receipts.get(receipt_key)

            if existing_receipt is not None:
                if existing_receipt.request_hash == request_hash:
                    existing_run = self._runs.get(existing_receipt.run_id)
                    if existing_run is not None:
                        return existing_run, False
                else:
                    raise RequestIdConflictError(
                        "This request ID was already used for a different goal."
                    )

            # Check if any run is currently active
            for r in self._runs.values():
                if r.status in ACTIVE_RUN_STATUSES:
                    raise RunInProgressError("Another analysis is already in progress.")

            # Create new run
            now_dt = self._time_provider()
            run_id = f"run_{secrets.token_urlsafe(16)}"
            run = Run(
                run_id=run_id,
                owner_session_id=owner_session_id,
                goal=goal,
                status="running",
                version=1,
                created_at=now_dt,
                updated_at=now_dt,
                trace_path=trace_path,
            )
            receipt = RequestReceipt(
                owner_session_id=owner_session_id,
                request_id=request_id,
                request_hash=request_hash,
                run_id=run_id,
                created_at=now_dt,
            )

            self._runs[run_id] = run
            self._receipts[receipt_key] = receipt
            return run, True

    start_run = create_run

    def _get_run_unlocked(
        self,
        run_id: str,
        *,
        owner_session_id: str | None = None,
    ) -> Run | None:
        """Internal helper to retrieve a run without locking."""
        run = self._runs.get(run_id)
        if run is None:
            return None
        if owner_session_id is not None and run.owner_session_id != owner_session_id:
            raise RunAccessDeniedError(
                "This run does not belong to the requesting session."
            )
        return run

    def get_run(
        self,
        run_id: str,
        *,
        owner_session_id: str | None = None,
    ) -> Run | None:
        """Retrieve a run by ID, enforcing session ownership if specified."""
        with self._mutex:
            return self._get_run_unlocked(run_id, owner_session_id=owner_session_id)

    def get_snapshot(
        self,
        run_id: str,
        *,
        owner_session_id: str | None = None,
    ) -> RunSnapshot | None:
        """Get public snapshot of a run, enforcing session ownership."""
        run = self.get_run(run_id, owner_session_id=owner_session_id)
        return run.to_snapshot() if run is not None else None

    def get_active_run(
        self,
        *,
        owner_session_id: str | None = None,
    ) -> Run | None:
        """Return the current active run (if any), optionally matching session owner."""
        with self._mutex:
            for r in self._runs.values():
                if r.status in ACTIVE_RUN_STATUSES:
                    if (
                        owner_session_id is None
                        or r.owner_session_id == owner_session_id
                    ):
                        return r
            return None

    def get_active_run_id(
        self,
        *,
        owner_session_id: str | None = None,
    ) -> str | None:
        """Return ID of current active run, optionally filtered by session owner."""
        run = self.get_active_run(owner_session_id=owner_session_id)
        return run.run_id if run is not None else None

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        owner_session_id: str | None = None,
    ) -> Run:
        """Update run status following strict state transition rules."""
        with self._mutex:
            run = self._get_run_unlocked(run_id, owner_session_id=owner_session_id)
            if run is None:
                raise RunNotFoundError(f"Run '{run_id}' not found.")

            allowed = VALID_TRANSITIONS.get(run.status, frozenset())
            if status not in allowed:
                raise InvalidRunStateTransitionError(
                    f"Cannot transition run '{run_id}' "
                    f"from '{run.status}' to '{status}'."
                )

            run.status = status
            run.version += 1
            run.updated_at = self._time_provider()
            if status in TERMINAL_RUN_STATUSES:
                run.completed_monotonic = self._monotonic_clock()
                self._cleanup_unlocked(run.completed_monotonic)
            return run

    def add_observation(
        self,
        run_id: str,
        observation: ToolObservation,
        *,
        owner_session_id: str | None = None,
    ) -> Run:
        """Append an observation to the run and increment its version."""
        with self._mutex:
            run = self._get_run_unlocked(run_id, owner_session_id=owner_session_id)
            if run is None:
                raise RunNotFoundError(f"Run '{run_id}' not found.")

            run.observations.append(observation)
            run.version += 1
            run.updated_at = self._time_provider()
            return run

    def set_result(
        self,
        run_id: str,
        result: str,
        *,
        owner_session_id: str | None = None,
    ) -> Run:
        """Set the final result of the run and increment version."""
        with self._mutex:
            run = self._get_run_unlocked(run_id, owner_session_id=owner_session_id)
            if run is None:
                raise RunNotFoundError(f"Run '{run_id}' not found.")

            run.result = result
            run.version += 1
            run.updated_at = self._time_provider()
            return run

    def set_error(
        self,
        run_id: str,
        error: RunError,
        *,
        owner_session_id: str | None = None,
    ) -> Run:
        """Set error detail on the run and increment version."""
        with self._mutex:
            run = self._get_run_unlocked(run_id, owner_session_id=owner_session_id)
            if run is None:
                raise RunNotFoundError(f"Run '{run_id}' not found.")

            run.error = error
            run.version += 1
            run.updated_at = self._time_provider()
            return run

    def set_approval_outcome(
        self,
        run_id: str,
        outcome: ApprovalStatus,
        *,
        owner_session_id: str | None = None,
    ) -> Run:
        """Record decision outcome on the run and increment version."""
        with self._mutex:
            run = self._get_run_unlocked(run_id, owner_session_id=owner_session_id)
            if run is None:
                raise RunNotFoundError(f"Run '{run_id}' not found.")

            run.approval_outcome = outcome
            run.version += 1
            run.updated_at = self._time_provider()
            return run

    def set_trace_path(
        self,
        run_id: str,
        trace_path: str | Path | None,
        *,
        owner_session_id: str | None = None,
    ) -> Run:
        """Pin the internal trace path without changing the public version.

        The trace file is backend bookkeeping, not client-visible run state,
        so this never bumps the snapshot version the UI polls.
        """
        with self._mutex:
            run = self._get_run_unlocked(run_id, owner_session_id=owner_session_id)
            if run is None:
                raise RunNotFoundError(f"Run '{run_id}' not found.")

            run.trace_path = str(trace_path) if trace_path is not None else None
            return run

    def get_receipt(
        self,
        owner_session_id: str,
        request_id: str,
    ) -> RequestReceipt | None:
        """Look up idempotency receipt for a session and request key."""
        with self._mutex:
            return self._receipts.get((owner_session_id, request_id))

    def list_runs(
        self,
        *,
        owner_session_id: str | None = None,
    ) -> list[Run]:
        """List stored runs, optionally filtered by owner session."""
        with self._mutex:
            if owner_session_id is not None:
                return [
                    r
                    for r in self._runs.values()
                    if r.owner_session_id == owner_session_id
                ]
            return list(self._runs.values())

    def count(self) -> int:
        """Return total number of active and terminal runs in store."""
        with self._mutex:
            return len(self._runs)

    def terminal_count(self) -> int:
        """Return count of terminal runs in store."""
        with self._mutex:
            return sum(
                1 for r in self._runs.values() if r.status in TERMINAL_RUN_STATUSES
            )

    def active_count(self) -> int:
        """Return count of active runs in store."""
        with self._mutex:
            return sum(
                1 for r in self._runs.values() if r.status in ACTIVE_RUN_STATUSES
            )

    def receipt_count(self) -> int:
        """Return total count of idempotency receipts currently tracked."""
        with self._mutex:
            return len(self._receipts)
