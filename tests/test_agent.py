import json

from agent_project.agent import run_expense_agent
from agent_project.llm import MockClient


def test_agent_loop_with_tool_call(tmp_path) -> None:
    trace_file = tmp_path / "test_trace.jsonl"

    # Step 1: Model requests get_transactions
    step1_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_tx_1",
                "name": "get_transactions",
                "arguments": json.dumps({"limit": 2}),
            }
        ],
        "latency_ms": 50,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0},
    }

    # Step 2: Model provides final answer
    step2_response = {
        "text": "Found 2 transactions: Biedronka and PGE Dystrybucja. Both are normal.",
        "tool_calls": [],
        "latency_ms": 60,
        "usage": {"prompt_tokens": 20, "completion_tokens": 15, "cost": 0.0},
    }

    mock_client = MockClient([step1_response, step2_response])

    result = run_expense_agent(
        goal="Check my recent transactions",
        client=mock_client,
        trace_path=trace_file,
        max_steps=5,
    )

    assert result["status"] == "completed"
    assert result["steps"] == 2
    assert "Biedronka" in result["final_answer"]

    # Verify trace was written
    assert trace_file.exists()
    raw_lines = trace_file.read_text(encoding="utf-8").splitlines()
    lines = [json.loads(line) for line in raw_lines]
    events = [entry["event"] for entry in lines]
    assert "agent_start" in events
    assert "model_response" in events
    assert "tool_result" in events
    assert "agent_completed" in events


def test_agent_loop_step_limit_reached(tmp_path) -> None:
    trace_file = tmp_path / "test_limit_trace.jsonl"

    # Repeated tool call
    repetitive_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_loop",
                "name": "get_transactions",
                "arguments": "{}",
            }
        ],
        "latency_ms": 20,
        "usage": {},
    }

    mock_client = MockClient([repetitive_response, repetitive_response])

    result = run_expense_agent(
        goal="Keep querying",
        client=mock_client,
        trace_path=trace_file,
        max_steps=2,
    )

    assert result["status"] == "step_limit_reached"
    assert result["steps"] == 2
