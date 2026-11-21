"""Dedicated single-worker executor running the agent loop off the HTTP thread."""

from __future__ import annotations

import logging
import re
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, get_args

from agent_project.agent import ToolExecutor, run_expense_agent
from agent_project.llm import LocalClient, ModelClient
from agent_project.models import ToolStatus
from agent_project.state_lock import LedgerCorruptionError, LedgerWriteError
from agent_project.tracing import TraceRecord, append_trace_record, make_trace_record
from agent_project.web.presentation import run_error
from agent_project.web.runs import Run, RunInProgressError, RunStore
from agent_project.web.schemas import (
    ObservationData,
    RunError,
    RunErrorCode,
    ToolObservation,
    TransactionSummary,
)

_LOGGER = logging.getLogger(__name__)

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def build_tool_observation(sequence: int, record: TraceRecord) -> ToolObservation:
    """Safely build a public ToolObservation from an internal TraceRecord."""
    payload = record.get("payload") if isinstance(record, dict) else {}
    data_dict = payload if isinstance(payload, dict) else {}
    step_val = data_dict.get("step")
    step = int(step_val) if isinstance(step_val, (int, float)) and step_val >= 1 else 1
    tool_name = str(data_dict.get("tool_name") or "unknown_tool")

    result_dict = data_dict.get("result")
    if not isinstance(result_dict, dict):
        result_dict = {}

    status_raw = result_dict.get("status")
    status: ToolStatus = status_raw if status_raw in get_args(ToolStatus) else "error"

    action_id_raw = result_dict.get("action_id")
    action_id: str | None = None
    if isinstance(action_id_raw, str) and IDENTIFIER_RE.match(action_id_raw):
        action_id = action_id_raw

    tx_summary: TransactionSummary | None = None
    tx_raw = result_dict.get("transaction")
    if isinstance(tx_raw, dict):
        try:
            tx_id = str(tx_raw.get("id") or tx_raw.get("transaction_id") or "")
            date_val = str(tx_raw.get("date") or "")
            merchant_val = str(tx_raw.get("merchant") or "")
            amount_val = float(tx_raw.get("amount", 0.0))
            currency_val = str(tx_raw.get("currency") or "PLN")
            cat_val = tx_raw.get("category_hint") or tx_raw.get("category")
            category_str = str(cat_val) if cat_val and str(cat_val).strip() else None

            if (
                IDENTIFIER_RE.match(tx_id)
                and date_val.strip()
                and merchant_val.strip()
                and re.fullmatch(r"^[A-Z]{3}$", currency_val)
            ):
                tx_summary = TransactionSummary(
                    transaction_id=tx_id,
                    date=date_val,
                    merchant=merchant_val,
                    amount=amount_val,
                    currency=currency_val,
                    category=category_str,
                )
        except Exception:
            tx_summary = None

    count_val = result_dict.get("count")
    if not isinstance(count_val, int) or count_val < 0:
        # Fall back: count items in a "data" list (e.g. search_transactions)
        data_list = result_dict.get("data")
        count_val = len(data_list) if isinstance(data_list, list) else None

    cat_val = result_dict.get("category")
    category = str(cat_val) if cat_val and str(cat_val).strip() else None

    prev_cat_val = result_dict.get("previous_category")
    prev_category = (
        str(prev_cat_val) if prev_cat_val and str(prev_cat_val).strip() else None
    )

    tx_id_raw = result_dict.get("transaction_id")
    tx_id: str | None = None
    if isinstance(tx_id_raw, str) and IDENTIFIER_RE.match(tx_id_raw):
        tx_id = tx_id_raw
    elif tx_summary is not None:
        tx_id = tx_summary.transaction_id

    obs_data: ObservationData | None = None
    try:
        obs_data = ObservationData(
            transaction_id=tx_id,
            category=category,
            previous_category=prev_category,
            count=count_val,
            transaction=tx_summary,
        )
    except Exception:
        obs_data = None

    return ToolObservation(
        sequence=sequence,
        step=step,
        tool=tool_name,
        status=status,
        action_id=action_id,
        data=obs_data,
    )


def trace_run_event(
    runs: RunStore, run_id: str, event: str, payload: dict[str, Any]
) -> None:
    """Best-effort append of one allowlisted event to the run's trace file.

    Callers must only pass catalog-safe fields (identifiers, kinds, statuses);
    no owner session id, cookie, CSRF token, raw path or secret is accepted
    here, so the trace never mirrors transport context.
    """
    run = runs.get_run(run_id)
    path = run.trace_path if run is not None else None
    if not path:
        return
    try:
        append_trace_record(Path(path), make_trace_record(event, dict(payload)))
    except Exception:
        # Tracing must never alter or retry the executed work.
        _LOGGER.debug("Could not append %s to the trace of run %s.", event, run_id)


def unique_trace_path(traces_dir: Path, run_id: str) -> Path:
    """Destination ``web_<run_id>.jsonl``; never overwrites an existing file."""
    candidate = traces_dir / f"web_{run_id}.jsonl"
    if not candidate.exists():
        return candidate
    while True:
        candidate = traces_dir / f"web_{run_id}_{secrets.token_hex(3)}.jsonl"
        if not candidate.exists():
            return candidate


def recorded_run_error(
    run_id: str, code: RunErrorCode, *, exc: Exception | None = None
) -> RunError:
    """Catalog error stamped with one correlation id, logged server-side only.

    The raw exception text stays out of both the log message content that
    clients can reach and the public DTO; the id lets the UI quote a failure
    back to the operator without exposing stacks.
    """
    correlation_id = f"corr_{secrets.token_urlsafe(8)}"
    if exc is not None:
        _LOGGER.warning(
            "Run %s failed; correlation_id=%s", run_id, correlation_id, exc_info=exc
        )
    else:
        _LOGGER.warning("Run %s failed; correlation_id=%s", run_id, correlation_id)
    return run_error(code, correlation_id=correlation_id)


class RunManager:
    """Manages asynchronous agent execution in a single worker thread.

    Enforces that at most one analysis is active across all sessions.
    Never places a second run into a hidden queue: any concurrent start
    attempt while busy is immediately rejected with RunInProgressError.
    """

    def __init__(
        self,
        runs: RunStore,
        *,
        data_path: Path | None = None,
        changes_path: Path | None = None,
        client: ModelClient | None = None,
        client_factory: Callable[[], ModelClient] | None = None,
        tool_executor: ToolExecutor | None = None,
        tool_executor_factory: Callable[..., ToolExecutor] | None = None,
        traces_dir: Path | None = None,
        max_steps: int = 12,
        response_language: str = "en",
    ) -> None:
        self._runs = runs
        self._data_path = data_path
        self._changes_path = changes_path
        self._client = client
        self._client_factory = client_factory
        self._tool_executor = tool_executor
        self._tool_executor_factory = tool_executor_factory
        self._traces_dir = traces_dir or Path("traces")
        self._max_steps = max_steps
        self._response_language = response_language

        self._lock = threading.Lock()
        self._active_run_id: str | None = None
        self._active_future: Future[None] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="agent_worker",
        )

    def is_busy(self) -> bool:
        """Return True if an analysis is currently executing or awaiting."""
        with self._lock:
            if self._active_run_id is not None:
                return True
        return self._runs.get_active_run() is not None

    def get_active_run_id(self) -> str | None:
        """Return ID of currently active run, if any."""
        with self._lock:
            if self._active_run_id is not None:
                return self._active_run_id
        return self._runs.get_active_run_id()

    def submit_run(
        self,
        owner_session_id: str,
        request_id: str,
        goal: str,
    ) -> tuple[Run, bool]:
        """Atomically accept a run for background execution or return existing replay.

        Returns (run, created) where created is True for a newly started job,
        or False if this is an idempotent replay of an existing run.

        Raises:
            RunInProgressError: if another analysis is active and this is not a replay.
            RequestIdConflictError: if request_id was reused with a different goal.
            ValueError: if any required argument is blank.
        """
        with self._lock:
            # Check for existing idempotency receipt
            existing_receipt = self._runs.get_receipt(owner_session_id, request_id)
            if existing_receipt is not None:
                # Idempotent replay handled by RunStore deduplication
                run, created = self._runs.start_run(
                    owner_session_id=owner_session_id,
                    request_id=request_id,
                    goal=goal,
                )
                return run, False

            # If another run is active in the worker or store, reject immediately
            if (
                self._active_run_id is not None
                or self._runs.get_active_run() is not None
            ):
                raise RunInProgressError("Another analysis is already in progress.")

            # Create the run first so the trace file name carries the stable
            # run id (web_<run_id>.jsonl), never overwriting an existing file.
            run, created = self._runs.start_run(
                owner_session_id=owner_session_id,
                request_id=request_id,
                goal=goal,
            )
            trace_path = unique_trace_path(self._traces_dir, run.run_id)
            try:
                self._runs.set_trace_path(run.run_id, trace_path)
            except Exception:
                _LOGGER.debug("Could not pin the trace path of run %s.", run.run_id)

            self._active_run_id = run.run_id

            # Submit to single worker thread without hidden queueing
            self._active_future = self._executor.submit(
                self._execute_run,
                run_id=run.run_id,
                goal=goal,
                trace_path=trace_path,
            )
            return run, True

    def _resolve_client(self) -> ModelClient:
        """Resolve ModelClient instance for the worker run."""
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            return self._client_factory()
        return LocalClient()

    def _build_run_executor(
        self, run_id: str, owner_session_id: str
    ) -> ToolExecutor | None:
        """Build the per-run executor and wire run-state publication.

        The factory receives ``run_id``, ``owner_session_id`` and the two
        progress hooks reflecting this run's awaiting/resumed transitions in
        the RunStore. Returns ``None`` when no factory is configured.
        """
        if self._tool_executor_factory is None:
            return None

        def on_awaiting_approval() -> None:
            try:
                self._runs.update_status(run_id, "awaiting_approval")
            except Exception:
                _LOGGER.debug("Could not record awaiting_approval on run %s", run_id)

        def on_resumed() -> None:
            try:
                run = self._runs.get_run(run_id)
                if run is not None and run.status == "awaiting_approval":
                    self._runs.update_status(run_id, "running")
            except Exception:
                _LOGGER.debug("Could not record resume on run %s", run_id)

        return self._tool_executor_factory(
            run_id=run_id,
            owner_session_id=owner_session_id,
            on_awaiting_approval=on_awaiting_approval,
            on_resumed=on_resumed,
        )

    def _execute_run(
        self,
        run_id: str,
        goal: str,
        trace_path: Path,
    ) -> None:
        """Execute agent loop in worker thread, publishing progress to RunStore."""
        sequence = 0

        def on_event(record: TraceRecord) -> None:
            nonlocal sequence
            if record.get("event") == "tool_result":
                sequence += 1
                try:
                    obs = build_tool_observation(sequence, record)
                    self._runs.add_observation(run_id, obs)
                except Exception:
                    _LOGGER.warning("Failed to record tool observation on run.")

        try:
            client = self._resolve_client()
            run = self._runs.get_run(run_id)
            owner_session_id = run.owner_session_id if run is not None else ""
            result = run_expense_agent(
                goal=goal,
                client=client,
                trace_path=trace_path,
                max_steps=self._max_steps,
                data_path=self._data_path,
                changes_path=self._changes_path,
                tool_executor=self._build_run_executor(run_id, owner_session_id)
                or self._tool_executor,
                on_event=on_event,
                response_language=self._response_language,
            )

            status = result.get("status")
            if status == "completed":
                final_answer = result.get("final_answer") or ""
                self._runs.set_result(run_id, final_answer)
                self._runs.update_status(run_id, "completed")
            elif status == "step_limit_reached":
                self._runs.update_status(run_id, "step_limit_reached")
            elif status == "error":
                self._runs.set_error(
                    run_id, recorded_run_error(run_id, "internal_error")
                )
                self._runs.update_status(run_id, "failed")
            else:
                self._runs.set_error(
                    run_id, recorded_run_error(run_id, "internal_error")
                )
                self._runs.update_status(run_id, "failed")

        except Exception as exc:
            self._handle_exception(run_id, exc)
        finally:
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None
                    self._active_future = None

    def _handle_exception(self, run_id: str, exc: Exception) -> None:
        """Translate worker exceptions into safe error codes and terminal status."""
        exc_name = type(exc).__name__
        exc_str = str(exc).lower()

        if (
            isinstance(exc, TimeoutError)
            or "timeout" in exc_name.lower()
            or "timeout" in exc_str
        ):
            err = recorded_run_error(run_id, "model_timeout", exc=exc)
            target_status = "failed"
        elif (
            isinstance(exc, ConnectionError)
            or "connection" in exc_name.lower()
            or "connection" in exc_str
            or isinstance(exc, ValueError)
        ):
            err = recorded_run_error(run_id, "model_unavailable", exc=exc)
            target_status = "failed"
        elif isinstance(exc, LedgerWriteError):
            err = recorded_run_error(run_id, "outcome_unknown", exc=exc)
            target_status = "outcome_unknown"
        elif isinstance(exc, (LedgerCorruptionError, FileNotFoundError)):
            err = recorded_run_error(run_id, "data_unavailable", exc=exc)
            target_status = "failed"
        else:
            err = recorded_run_error(run_id, "internal_error", exc=exc)
            target_status = "failed"

        try:
            self._runs.set_error(run_id, err)
        except Exception:
            _LOGGER.debug("Could not record error detail on run %s", run_id)

        try:
            self._runs.update_status(run_id, target_status)
        except Exception:
            _LOGGER.debug(
                "Run %s already in terminal status; ignored %s", run_id, target_status
            )

    def wait_for_active_run(self, timeout: float | None = None) -> bool:
        """Wait for the currently executing background future to complete."""
        future: Future[None] | None = None
        with self._lock:
            future = self._active_future
        if future is None:
            return True
        try:
            future.result(timeout=timeout)
            return True
        except Exception:
            return False

    def shutdown(self, *, wait: bool = False) -> None:
        """Shut down the background worker pool."""
        self._executor.shutdown(wait=wait, cancel_futures=True)
