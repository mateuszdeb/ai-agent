import json

from agent_project.agent import run_expense_agent
from agent_project.llm import MockClient


def test_agent_multi_step_investigation(tmp_path) -> None:
    """Agent should call multiple tools before producing a final answer."""
    trace_file = tmp_path / "test_trace.jsonl"

    # Step 1: Model calls find_duplicate_candidates AND get_transactions together
    step1_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_dupes",
                "name": "find_duplicate_candidates",
                "arguments": "{}",
            },
            {
                "id": "call_tx_1",
                "name": "get_transactions",
                "arguments": json.dumps({"limit": 20}),
            },
        ],
        "latency_ms": 80,
        "usage": {"prompt_tokens": 15, "completion_tokens": 20, "cost": 0.0},
        "finish_reason": "tool_calls",
    }

    # Step 2: Model inspects a suspicious transaction (Carrefour)
    step2_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_detail_105",
                "name": "get_transaction_details",
                "arguments": json.dumps({"transaction_id": "tx_105"}),
            },
        ],
        "latency_ms": 60,
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "cost": 0.0},
        "finish_reason": "tool_calls",
    }

    # Step 3: Model checks baseline for Carrefour spending
    step3_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_baseline_pge",
                "name": "compute_budget_baseline",
                "arguments": json.dumps({"merchant": "PGE"}),
            },
        ],
        "latency_ms": 55,
        "usage": {"prompt_tokens": 40, "completion_tokens": 8, "cost": 0.0},
        "finish_reason": "tool_calls",
    }

    # Step 4: Model provides final structured answer
    step4_response = {
        "text": (
            "## Raport wydatków domowych\n\n"
            "### Rutynowe: Biedronka (142.50 PLN), Apteka DOZ (68.40 PLN)\n\n"
            "### Wymagające uwagi:\n"
            "- Spotify Polska: potencjalny duplikat (tx_103 i tx_104)\n"
            "- Carrefour Hipermarket: 1850 PLN — powyżej normy\n"
            "- PAYPAL *XYZ_CLOUD_SRV: nieznany odbiorca\n"
            "- PGE Dystrybucja tx_110: 541.20 PLN vs średnia 412.65 PLN"
        ),
        "tool_calls": [],
        "latency_ms": 200,
        "usage": {"prompt_tokens": 60, "completion_tokens": 80, "cost": 0.0},
        "finish_reason": "stop",
    }

    mock_client = MockClient(
        [step1_response, step2_response, step3_response, step4_response]
    )

    result = run_expense_agent(
        goal="Sprawdź transakcje domowe i wykryj anomalie",
        client=mock_client,
        trace_path=trace_file,
        max_steps=10,
    )

    assert result["status"] == "completed"
    assert result["steps"] == 4, "Agent should take 4 steps (multi-step investigation)"

    # Verify trace contents
    raw_lines = trace_file.read_text(encoding="utf-8").splitlines()
    lines = [json.loads(line) for line in raw_lines]
    events = [entry["event"] for entry in lines]

    assert "agent_start" in events
    assert "agent_completed" in events
    assert events.count("model_response") == 4
    assert events.count("tool_result") == 4  # dupes + txs + detail + baseline

    # Verify provider, model, latency, and cost are always logged
    start_event = next(e for e in lines if e["event"] == "agent_start")
    assert "provider" in start_event["payload"]
    assert "model" in start_event["payload"]

    model_responses = [e for e in lines if e["event"] == "model_response"]
    for resp in model_responses:
        assert "cost" in resp["payload"]["usage"], "cost must be present in every step"
        assert "provider" in resp["payload"], "provider must be present in every step"
        assert "model" in resp["payload"], "model must be present in every step"
        assert resp["payload"]["latency_ms"] is not None


def test_agent_cost_always_logged(tmp_path) -> None:
    """cost field must be present even when the LLM client does not return it."""
    trace_file = tmp_path / "cost_trace.jsonl"

    response_no_cost = {
        "text": "Done.",
        "tool_calls": [],
        "latency_ms": 30,
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},  # no cost key
        "finish_reason": "stop",
    }

    mock_client = MockClient([response_no_cost])
    run_expense_agent(
        goal="Quick check",
        client=mock_client,
        trace_path=trace_file,
        max_steps=5,
    )

    lines = [json.loads(line) for line in trace_file.read_text().splitlines()]
    model_steps = [e for e in lines if e["event"] == "model_response"]
    assert model_steps[0]["payload"]["usage"]["cost"] == 0.0


def test_agent_loop_step_limit_reached(tmp_path) -> None:
    trace_file = tmp_path / "test_limit_trace.jsonl"

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
        "usage": {"cost": 0.0},
        "finish_reason": "tool_calls",
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


def test_agent_unknown_tool_returns_error(tmp_path) -> None:
    trace_file = tmp_path / "test_unknown_tool.jsonl"

    bad_tool_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_bad",
                "name": "nonexistent_tool",
                "arguments": "{}",
            }
        ],
        "latency_ms": 10,
        "usage": {"cost": 0.0},
        "finish_reason": "tool_calls",
    }
    final_response = {
        "text": "I encountered an error with the tool.",
        "tool_calls": [],
        "latency_ms": 10,
        "usage": {"cost": 0.0},
        "finish_reason": "stop",
    }

    mock_client = MockClient([bad_tool_response, final_response])
    run_expense_agent(
        goal="Use a bad tool",
        client=mock_client,
        trace_path=trace_file,
        max_steps=5,
    )

    lines = [json.loads(line) for line in trace_file.read_text().splitlines()]
    tool_results = [e for e in lines if e["event"] == "tool_result"]
    assert any("error" in tr["payload"]["result"] for tr in tool_results)
