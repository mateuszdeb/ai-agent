"""Agent loop orchestrating model decisions, controlled tool execution, and tracing."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from agent_project.llm import LocalClient, ModelClient
from agent_project.models import (
    AssignCategoryArgs,
    ComputeBudgetBaselineArgs,
    DeleteTransactionArgs,
    FindDuplicateCandidatesArgs,
    GetTransactionDetailsArgs,
    SearchTransactionsArgs,
    UndoCategoryAssignmentArgs,
)
from agent_project.state_lock import (
    LedgerBusyError,
    LedgerCorruptionError,
    LedgerWriteError,
)
from agent_project.tools import (
    TOOL_DEFINITIONS,
    assign_category,
    compute_budget_baseline,
    delete_flagged_transaction,
    find_duplicate_candidates,
    get_transaction_details,
    search_transactions,
    undo_category_assignment,
)
from agent_project.tracing import TraceRecord, append_trace_record, make_trace_record

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCallContext:
    """Trusted loop position associated with one model tool call."""

    step: int
    tool_call_id: str


class ToolExecutor(Protocol):
    """Trusted application adapter; must enforce controller/approval policy.

    Bind application-owned paths and services in the adapter. The loop never
    falls back to the default controller after an adapter raises.
    """

    def __call__(
        self, tool_name: str, raw_args: Any, *, context: ToolCallContext
    ) -> dict[str, Any]: ...


EventCallback = Callable[[TraceRecord], None]

SYSTEM_PROMPT = """You are a Household Expense Checker Agent.
Your job is to inspect personal and household bank transactions, manage categories,
and detect unusual or duplicate charges.

You have access to three kinds of tools:
1. READ-ONLY tools:
   - `search_transactions`: search with filters (merchant, category, amount, query).
   - `get_transactions`: fetch recent transactions.
   - `get_transaction_details`: get full details for a specific transaction ID.
   - `compute_budget_baseline`: compare transaction amounts against baselines.
   - `find_duplicate_candidates`: scan for duplicate charges within 3 days.

2. REVERSIBLE WRITE tools:
   - `assign_category`: assign or update a budget category. If category is set,
     returns 'already_assigned'. Set `force_override=True` to overwrite.
   - `undo_category_assignment`: revert a previous assignment using `action_id`.

3. SENSITIVE tools:
   - `delete_flagged_transaction`: logically delete a duplicate charge.
     CRITICAL: This tool requires real user confirmation. If it returns
     'confirmation_required', do NOT claim it was deleted! Ask user to confirm.
     Your own statements do not count as user confirmation.

Follow this decision pattern:
- Observe tool results carefully.
- If a tool returns 'confirmation_required', ask user to confirm the action.
- If a tool returns 'already_assigned', respect existing category or explain override.
- If a search returns no results, refine your query or notify the user.
- Provide clear, structured final answers.

For expense reviews, scan duplicates and recent transactions, inspect suspicious
items, and compare their amounts with budget baselines before reporting.
Distinguish routine expenses from flagged anomalies and clarification questions.
Independent read-only calls may be requested together.
When confirmation is required, include the pending action ID in your question,
explain that no deletion occurred, and do not retry with self-generated approval.
"""


# ---------------------------------------------------------------------------
# Tool Registry & Application Security Guardrails
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "search_transactions": {
        "func": search_transactions,
        "model": SearchTransactionsArgs,
        "is_sensitive": False,
    },
    "get_transactions": {
        # The advertised compatibility schema includes all search filters.
        "func": search_transactions,
        "model": SearchTransactionsArgs,
        "is_sensitive": False,
    },
    "get_transaction_details": {
        "func": get_transaction_details,
        "model": GetTransactionDetailsArgs,
        "is_sensitive": False,
    },
    "compute_budget_baseline": {
        "func": compute_budget_baseline,
        "model": ComputeBudgetBaselineArgs,
        "is_sensitive": False,
    },
    "find_duplicate_candidates": {
        "func": find_duplicate_candidates,
        "model": FindDuplicateCandidatesArgs,
        "is_sensitive": False,
    },
    "assign_category": {
        "func": assign_category,
        "model": AssignCategoryArgs,
        "is_sensitive": False,
    },
    "undo_category_assignment": {
        "func": undo_category_assignment,
        "model": UndoCategoryAssignmentArgs,
        "is_sensitive": False,
    },
    "delete_flagged_transaction": {
        "func": delete_flagged_transaction,
        "model": DeleteTransactionArgs,
        "is_sensitive": True,
    },
}


def parse_tool_args(arguments: Any) -> dict[str, Any]:
    """Parse a JSON object; reject malformed JSON and non-object values."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise ValueError("Tool arguments must contain valid JSON.") from None
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    return arguments


def execute_controlled_tool(
    tool_name: str,
    raw_args: Any,
    data_path: Path | None = None,
    changes_path: Path | None = None,
    confirmation_handler: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Deterministic tool controller enforcing permissions and validation.

    Guarantees:
    1. Tool is in whitelist of allowed tools (unknown_tool rejection).
    2. Arguments match Pydantic schema with strict type validation (invalid_arguments).
    3. Sensitive tools cannot be self-confirmed by the model: requires verified
       application-level confirmation via confirmation_handler.
    4. Propagates structured results (success, already_assigned, not_found, etc.).
    """
    # 1. Whitelist check
    if tool_name not in TOOL_REGISTRY:
        return {
            "status": "unknown_tool",
            "error": f"Unknown tool: '{tool_name}'",
        }

    entry = TOOL_REGISTRY[tool_name]
    model_cls = entry["model"]
    func = entry["func"]
    is_sensitive = entry["is_sensitive"]

    # 2-3. Reject malformed input and coercion, using the advertised models.
    try:
        parsed_args = parse_tool_args(raw_args)
        validated_model = model_cls.model_validate(parsed_args, strict=True)
    except (ValidationError, ValueError) as exc:
        return {
            "status": "invalid_arguments",
            "error": f"Invalid arguments for tool '{tool_name}': {exc}",
        }

    clean_args = validated_model.model_dump()

    # 4-5. Guardrails and execution fail closed, including hook failures.
    try:
        if is_sensitive:
            # Check state before asking a human to approve a nonexistent action.
            current = get_transaction_details(
                clean_args["transaction_id"],
                data_path=data_path,
                changes_path=changes_path,
            )
            if current.get("status") == "not_found":
                return current

            # Never expose a model-supplied approval to the trusted hook.
            clean_args["confirmed"] = False
            pending = func(**clean_args, data_path=data_path, changes_path=changes_path)
            if confirmation_handler is None:
                return pending
            action_id = pending["action_id"]
            # Pass a copy so the hook cannot change which action is executed.
            user_approved = confirmation_handler(action_id, clean_args.copy())
            if user_approved is not True:
                return {
                    "status": "confirmation_denied",
                    "action_id": action_id,
                    "transaction_id": clean_args.get("transaction_id"),
                    "message": "Application did not explicitly approve this action.",
                }
            clean_args["confirmed"] = True

        result = func(
            **clean_args,
            data_path=data_path,
            changes_path=changes_path,
        )
        if isinstance(result, list) or (
            isinstance(result, dict) and "status" not in result
        ):
            return {"status": "success", "data": result}
        return result
    except LedgerBusyError:
        return {"status": "state_busy", "error": "Transaction state is busy."}
    except LedgerCorruptionError:
        return {
            "status": "state_conflict",
            "error": "Ledger is invalid. Review the current state before continuing.",
        }
    except LedgerWriteError:
        return {
            "status": "error",
            "error_code": "outcome_unknown",
            "error": "Ledger write outcome is unknown. Do not automatically retry.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Tool execution failed: {exc}",
        }


# ---------------------------------------------------------------------------
# Agent Loop Orchestrator
# ---------------------------------------------------------------------------


def run_expense_agent(
    goal: str,
    client: ModelClient | None = None,
    trace_path: Path | None = None,
    max_steps: int = 12,
    data_path: Path | None = None,
    changes_path: Path | None = None,
    confirmation_handler: Callable[[str, dict[str, Any]], bool] | None = None,
    *,
    tool_executor: ToolExecutor | None = None,
    on_event: EventCallback | None = None,
    response_language: str | None = None,
) -> dict[str, Any]:
    """Execute one bounded loop, with optional trusted application adapters.

    Existing positional arguments and the default controller remain unchanged.
    Observers receive isolated internal trace records, not public HTTP DTOs;
    sanitize them in the web presentation layer. Observer exceptions are logged
    without exception text and do not interrupt or retry execution. Trace I/O
    failures and executor exceptions still propagate, without fallback/retry.
    A language tag sets a per-run preference, not a translation guarantee.
    """
    if response_language is not None and (
        not isinstance(response_language, str)
        or len(response_language) > 35
        or re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", response_language)
        is None
    ):
        raise ValueError("Response language must be a language tag such as 'en'.")
    system_prompt = SYSTEM_PROMPT
    if response_language is not None:
        system_prompt += (
            f"\nResponse language preference: {response_language}. "
            "Respond in this language by default, but honor an explicit user "
            "request for another language. Do not translate merchant names or "
            "transaction identifiers."
        )
    model_client = client or LocalClient()
    if trace_path is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        trace_path = Path("traces") / f"agent_run_{timestamp}.jsonl"

    def publish(event: str, payload: dict[str, Any]) -> None:
        record = make_trace_record(event, payload)
        append_trace_record(trace_path, record)
        if on_event is not None:
            try:
                on_event(deepcopy(record))
            except Exception:
                _LOGGER.warning(
                    "Agent event callback failed; execution was not retried."
                )

    publish(
        "agent_start",
        {
            "goal": goal,
            "max_steps": max_steps,
            "model": getattr(model_client, "model", "mock"),
            "provider": getattr(model_client, "provider", "local"),
        },
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal},
    ]

    final_answer: str | None = None

    for step in range(1, max_steps + 1):
        response = model_client.complete(messages, tools=TOOL_DEFINITIONS)
        text = response.get("text")
        tool_calls = response.get("tool_calls") or []

        # Ensure cost is always present in usage (0.0 for local/free models)
        usage = response.get("usage") or {}
        if "cost" not in usage:
            usage["cost"] = 0.0

        publish(
            "model_response",
            {
                "step": step,
                "model": response.get("model")
                or getattr(model_client, "model", "mock"),
                "provider": response.get("provider")
                or getattr(model_client, "provider", "local"),
                "text": text,
                "tool_calls": tool_calls,
                "latency_ms": response.get("latency_ms"),
                "usage": usage,
                "finish_reason": response.get("finish_reason"),
            },
        )

        if not tool_calls:
            # Model finished reasoning and returned text output
            if text:
                final_answer = text
                publish(
                    "agent_completed",
                    {"step": step, "final_answer": final_answer},
                )
                return {
                    "status": "completed",
                    "final_answer": final_answer,
                    "steps": step,
                    "trace_path": str(trace_path),
                }
            # Model gave empty output without tool calls
            publish(
                "agent_error",
                {"step": step, "error": "Empty model reply without tool calls"},
            )
            return {
                "status": "error",
                "error": "Empty model reply without tool calls",
                "steps": step,
                "trace_path": str(trace_path),
            }

        # Format assistant tool call message
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": text or "",
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call["arguments"]
                        if isinstance(call["arguments"], str)
                        else json.dumps(call["arguments"]),
                    },
                }
                for call in tool_calls
            ],
        }
        messages.append(assistant_msg)

        # Execute tool calls through deterministic Python controller
        for call in tool_calls:
            tool_name = call["name"]
            tool_id = call["id"]
            raw_args = call.get("arguments", {})

            if tool_executor is None:
                result = execute_controlled_tool(
                    tool_name=tool_name,
                    raw_args=raw_args,
                    data_path=data_path,
                    changes_path=changes_path,
                    confirmation_handler=confirmation_handler,
                )
            else:
                result = tool_executor(
                    tool_name,
                    deepcopy(raw_args),
                    context=ToolCallContext(step=step, tool_call_id=tool_id),
                )

            publish(
                "tool_result",
                {
                    "step": step,
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    # Preserve malformed input without reparsing or crashing.
                    "arguments": raw_args,
                    "result": result,
                },
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": tool_name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    publish(
        "agent_step_limit_reached",
        {"max_steps": max_steps},
    )
    return {
        "status": "step_limit_reached",
        "steps": max_steps,
        "trace_path": str(trace_path),
    }


if __name__ == "__main__":
    import sys

    print("Running Household Expense Checker Agent...")
    default_goal = (
        "Przejrzyj ostatnie transakcje domowe, wykryj anomalie i duplikaty "
        "samodzielnie analizując kwoty i merchantów, a następnie wskaż "
        "transakcje normalne oraz te podejrzane/wymagające doprecyzowania."
    )
    user_goal = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else default_goal

    print(f"Cel: {user_goal}\n")
    res = run_expense_agent(goal=user_goal)
    print("\n--- Wynik agenta ---")
    print(f"Status: {res['status']}")
    print(f"Kroki: {res['steps']}")
    print(f"Ślad zapisano do: {res['trace_path']}")
    if "final_answer" in res:
        print("\nOdpowiedź końcowa:")
        print(res["final_answer"])
