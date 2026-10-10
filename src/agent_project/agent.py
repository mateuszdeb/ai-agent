"""Agent loop orchestrating model decisions, tool calls, and trace recording."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_project.llm import LocalClient, ModelClient
from agent_project.tools import (
    TOOL_DEFINITIONS,
    get_transaction_details,
    get_transactions,
)
from agent_project.tracing import append_trace

SYSTEM_PROMPT = """You are a Household Expense Checker Agent.
Your job is to inspect personal and household transactions, detect unusual
expenses, price spikes, or potential duplicates, and highlight items requiring
user clarification.

Guidelines:
- Use the available read-only tools: `get_transactions` and `get_transaction_details`.
- When independent tool calls are helpful, you may request them together.
- Analyze transaction amounts, merchants, and notes.
- In your final response, provide a structured summary:
  1. Routine categorized expenses (normal spending).
  2. Flagged unusual/anomalous expenses (with clear reasons and clarification
     questions for the household user).
"""


def parse_tool_args(arguments: Any) -> dict[str, Any]:
    """Parse tool arguments safely from a JSON string or dict."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {}
    return {}


def run_expense_agent(
    goal: str,
    client: ModelClient | None = None,
    trace_path: Path | None = None,
    max_steps: int = 12,
    data_path: Path | None = None,
) -> dict[str, Any]:
    """Execute the agent loop for the specified goal.

    Loop pattern: goal -> choose tool -> run tool -> return result -> decide/stop.
    """
    model_client = client or LocalClient()
    if trace_path is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        trace_path = Path("traces") / f"agent_run_{timestamp}.jsonl"

    append_trace(
        trace_path,
        "agent_start",
        {
            "goal": goal,
            "max_steps": max_steps,
            "model": getattr(model_client, "model", "mock"),
        },
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]

    tool_dispatcher = {
        "get_transactions": lambda **kwargs: get_transactions(
            data_path=data_path, **kwargs
        ),
        "get_transaction_details": lambda **kwargs: get_transaction_details(
            data_path=data_path, **kwargs
        ),
    }

    final_answer: str | None = None

    for step in range(1, max_steps + 1):
        response = model_client.complete(messages, tools=TOOL_DEFINITIONS)
        text = response.get("text")
        tool_calls = response.get("tool_calls") or []

        append_trace(
            trace_path,
            "model_response",
            {
                "step": step,
                "text": text,
                "tool_calls": tool_calls,
                "latency_ms": response.get("latency_ms"),
                "usage": response.get("usage", {}),
                "finish_reason": response.get("finish_reason"),
            },
        )

        if not tool_calls:
            # Model finished reasoning and returned text output
            if text:
                final_answer = text
                append_trace(
                    trace_path,
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
            append_trace(
                trace_path,
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

        # Execute tool calls
        for call in tool_calls:
            tool_name = call["name"]
            tool_id = call["id"]
            raw_args = call.get("arguments", {})
            parsed_args = parse_tool_args(raw_args)

            if tool_name in tool_dispatcher:
                try:
                    result = tool_dispatcher[tool_name](**parsed_args)
                except Exception as exc:
                    result = {"error": f"Tool execution failed: {exc}"}
            else:
                result = {"error": f"Unknown tool: '{tool_name}'"}

            append_trace(
                trace_path,
                "tool_result",
                {
                    "step": step,
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "arguments": parsed_args,
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

    append_trace(
        trace_path,
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
        "Przejrzyj ostatnie transakcje domowe, wskaż transakcje normalne "
        "oraz te podejrzane/wymagające doprecyzowania."
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
