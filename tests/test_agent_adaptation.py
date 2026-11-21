import json
from pathlib import Path

import pytest

from agent_project.agent import (
    TOOL_DEFINITIONS,
    TOOL_REGISTRY,
    execute_controlled_tool,
    run_expense_agent,
)
from agent_project.llm import MockClient
from agent_project.tools import load_effective_transactions


def test_execute_controlled_tool_unknown() -> None:
    res = execute_controlled_tool("malicious_tool", {"arg": 123})
    assert res["status"] == "unknown_tool"
    assert "error" in res


def test_execute_controlled_tool_invalid_args() -> None:
    # limit must be an integer <= 50, passing invalid string or extra field
    res = execute_controlled_tool(
        "search_transactions",
        {"limit": 999},  # exceeds ge=1, le=50
    )
    assert res["status"] == "invalid_arguments"
    assert "error" in res

    # Extra field forbidden
    res_extra = execute_controlled_tool(
        "search_transactions",
        {"merchant": "Biedronka", "injected_field": "exploit"},
    )
    assert res_extra["status"] == "invalid_arguments"


def test_execute_controlled_tool_model_cannot_self_confirm(tmp_path: Path) -> None:
    changes_file = tmp_path / "changes.jsonl"

    # Even if model passes confirmed=True, without app handler it is forced to False!
    res = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_104", "confirmed": True},
        changes_path=changes_file,
    )
    assert res["status"] == "confirmation_required"
    assert "pending_del_tx_104" in res["action_id"]
    assert not changes_file.exists()


def test_execute_controlled_tool_with_application_approval(tmp_path: Path) -> None:
    changes_file = tmp_path / "changes.jsonl"

    # 1. User denies
    res_denied = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_104"},
        changes_path=changes_file,
        confirmation_handler=lambda action_id, details: False,
    )
    assert res_denied["status"] == "confirmation_denied"
    assert not changes_file.exists()

    # 2. User approves
    res_approved = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_104"},
        changes_path=changes_file,
        confirmation_handler=lambda action_id, details: True,
    )
    assert res_approved["status"] == "success"
    assert changes_file.exists()


def test_agent_adaptation_scenario_a_confirmation_required(tmp_path: Path) -> None:
    """A Run That Counts: Model halts deletion on confirmation_required."""
    trace_file = tmp_path / "scenario_a_trace.jsonl"
    changes_file = tmp_path / "changes.jsonl"

    # Step 1: Model searches for Spotify transactions
    step1_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_search_spotify",
                "name": "search_transactions",
                "arguments": json.dumps({"merchant": "Spotify"}),
            }
        ],
        "latency_ms": 50,
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "cost": 0.0},
        "finish_reason": "tool_calls",
    }

    # Step 2: Model attempts to delete tx_104 after observing duplicate
    step2_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_del_104",
                "name": "delete_flagged_transaction",
                "arguments": json.dumps(
                    {"transaction_id": "tx_104", "reason": "Duplicate Spotify charge"}
                ),
            }
        ],
        "latency_ms": 60,
        "usage": {"prompt_tokens": 100, "completion_tokens": 25, "cost": 0.0},
        "finish_reason": "tool_calls",
    }

    # Step 3: Model observes confirmation_required, asks user for confirmation
    step3_response = {
        "text": (
            "Zidentyfikowałem zduplikowaną transakcję Spotify tx_104 (24.99 PLN). "
            "Usunięcie wymaga Twojego potwierdzenia (pending_del_tx_104)."
        ),
        "tool_calls": [],
        "latency_ms": 80,
        "usage": {"prompt_tokens": 150, "completion_tokens": 40, "cost": 0.0},
        "finish_reason": "stop",
    }

    mock_client = MockClient([step1_response, step2_response, step3_response])

    result = run_expense_agent(
        goal="Sprawdź i usuń duplikat Spotify",
        client=mock_client,
        trace_path=trace_file,
        changes_path=changes_file,
        max_steps=5,
    )

    assert result["status"] == "completed"
    assert result["steps"] == 3
    assert "potwierdzenia" in result["final_answer"]

    # Verify tx_104 was NOT deleted because user confirmation was required
    effective = load_effective_transactions(changes_path=changes_file)
    assert any(tx["id"] == "tx_104" for tx in effective)


def test_agent_adaptation_scenario_b_already_assigned(tmp_path: Path) -> None:
    """A Run That Counts: Model observes already_assigned and avoids duplicate write."""
    trace_file = tmp_path / "scenario_b_trace.jsonl"
    changes_file = tmp_path / "changes.jsonl"

    # Step 1: Model tries to assign Groceries to tx_101 (which is already Groceries)
    step1_response = {
        "text": None,
        "tool_calls": [
            {
                "id": "call_assign_101",
                "name": "assign_category",
                "arguments": json.dumps(
                    {"transaction_id": "tx_101", "category": "Groceries"}
                ),
            }
        ],
        "latency_ms": 40,
        "usage": {"prompt_tokens": 40, "completion_tokens": 20, "cost": 0.0},
        "finish_reason": "tool_calls",
    }

    # Step 2: Model observes already_assigned and completes informing the user
    step2_response = {
        "text": (
            "Transakcja tx_101 posiada już kategorię Groceries. "
            "Brak konieczności zmian."
        ),
        "tool_calls": [],
        "latency_ms": 60,
        "usage": {"prompt_tokens": 90, "completion_tokens": 20, "cost": 0.0},
        "finish_reason": "stop",
    }

    mock_client = MockClient([step1_response, step2_response])

    result = run_expense_agent(
        goal="Przypisz kategorię Groceries do transakcji tx_101",
        client=mock_client,
        trace_path=trace_file,
        changes_path=changes_file,
        max_steps=5,
    )

    assert result["status"] == "completed"
    assert result["steps"] == 2
    assert "posiada już kategorię Groceries" in result["final_answer"]
    assert not changes_file.exists()


@pytest.mark.parametrize(
    "arguments",
    ["{", "", "[]", "null", '"text"', "42", "true", [], None, 42],
)
def test_controller_rejects_malformed_or_non_object_arguments(arguments) -> None:
    result = execute_controlled_tool("search_transactions", arguments)
    assert result["status"] == "invalid_arguments"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_transactions", {"limit": "10"}),
        ("search_transactions", {"limit": True}),
        ("search_transactions", {"limit": 1.0}),
        ("search_transactions", {"min_amount": "10.5"}),
        ("search_transactions", {"min_amount": -1}),
        ("search_transactions", {"limit": 0}),
        (
            "assign_category",
            {
                "transaction_id": "tx_101",
                "category": "Groceries",
                "force_override": "true",
            },
        ),
        (
            "delete_flagged_transaction",
            {"transaction_id": "tx_104", "confirmed": "true"},
        ),
        (
            "delete_flagged_transaction",
            {"transaction_id": "tx_104", "approval_token": "model_generated"},
        ),
    ],
)
def test_controller_enforces_strict_types_and_bounds(tool, arguments, tmp_path) -> None:
    ledger = tmp_path / "changes.jsonl"
    result = execute_controlled_tool(tool, arguments, changes_path=ledger)
    assert result["status"] == "invalid_arguments"
    assert not ledger.exists()


def test_schemas_and_controller_use_same_models() -> None:
    definitions = {
        item["function"]["name"]: item["function"] for item in TOOL_DEFINITIONS
    }
    assert definitions.keys() == TOOL_REGISTRY.keys()
    for name, entry in TOOL_REGISTRY.items():
        schema = entry["model"].model_json_schema()
        parameters = definitions[name]["parameters"]
        assert parameters["properties"] == schema.get("properties", {})
        assert parameters["required"] == schema.get("required", [])


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("search_transactions", {}),
        ("get_transactions", {}),
        ("get_transactions", {"merchant": "Spotify", "limit": 2}),
        ("get_transactions", {"category": "Groceries", "min_amount": 1}),
        ("get_transaction_details", {"transaction_id": "tx_101"}),
        ("compute_budget_baseline", {"merchant": "Spotify"}),
        ("find_duplicate_candidates", {}),
    ],
)
def test_read_only_controller_returns_structured_success(name, arguments, tmp_path):
    ledger = tmp_path / "changes.jsonl"
    result = execute_controlled_tool(name, arguments, changes_path=ledger)
    assert result["status"] == "success"
    assert "data" in result
    assert not ledger.exists()


@pytest.mark.parametrize("approval", [False, None, "false", "true", 1, {"ok": True}])
def test_only_literal_application_true_authorizes_deletion(approval, tmp_path):
    ledger = tmp_path / "changes.jsonl"
    result = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_104", "confirmed": True},
        changes_path=ledger,
        confirmation_handler=lambda action_id, details: approval,
    )
    assert result["status"] == "confirmation_denied"
    assert result["action_id"] == "pending_del_tx_104"
    assert not ledger.exists()


def test_confirmation_hook_cannot_change_approved_target(tmp_path):
    ledger = tmp_path / "changes.jsonl"

    def approve(action_id, details):
        assert action_id == "pending_del_tx_104"
        assert details["confirmed"] is False
        details["transaction_id"] = "tx_103"
        return True

    result = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_104", "confirmed": True},
        changes_path=ledger,
        confirmation_handler=approve,
    )
    assert result["status"] == "success"
    remaining = {tx["id"] for tx in load_effective_transactions(changes_path=ledger)}
    assert "tx_104" not in remaining
    assert "tx_103" in remaining


def test_confirmation_hook_exception_fails_closed(tmp_path):
    ledger = tmp_path / "changes.jsonl"

    def broken_hook(action_id, details):
        raise RuntimeError("confirmation UI unavailable")

    result = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_104"},
        changes_path=ledger,
        confirmation_handler=broken_hook,
    )
    assert result["status"] == "error"
    assert not ledger.exists()


def test_missing_transaction_does_not_request_confirmation(tmp_path):
    ledger = tmp_path / "changes.jsonl"
    approvals = []
    result = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_missing"},
        changes_path=ledger,
        confirmation_handler=lambda *args: approvals.append(args),
    )
    assert result["status"] == "not_found"
    assert approvals == []
    assert not ledger.exists()


def test_controller_write_and_undo_preserve_source_data(tmp_path):
    ledger = tmp_path / "changes.jsonl"
    source = Path(__file__).resolve().parents[1] / "data" / "transactions.json"
    original_bytes = source.read_bytes()
    before = load_effective_transactions(changes_path=ledger)
    assigned = execute_controlled_tool(
        "assign_category",
        {"transaction_id": "tx_101", "category": "Other", "force_override": True},
        changes_path=ledger,
    )
    assert assigned["status"] == "success"
    assert load_effective_transactions(changes_path=ledger) != before
    reverted = execute_controlled_tool(
        "undo_category_assignment",
        {"action_id": assigned["action_id"]},
        changes_path=ledger,
    )
    assert reverted["status"] == "success"
    # Undo restores domain fields but retains its new audit action identifier.
    restored = load_effective_transactions(changes_path=ledger)
    assert [
        {key: value for key, value in tx.items() if key != "last_action_id"}
        for tx in restored
    ] == before
    assert source.read_bytes() == original_bytes


def test_agent_adapts_to_observation_not_fixed_response_script(tmp_path):
    """Choose the next call only after inspecting actual tool observations."""
    ledger = tmp_path / "changes.jsonl"
    trace = tmp_path / "adaptive.jsonl"

    class ObservationDrivenClient:
        def complete(self, messages, tools=None):
            observations = [
                json.loads(m["content"]) for m in messages if m["role"] == "tool"
            ]
            if not observations:
                name, arguments = "search_transactions", {"merchant": "Shell"}
            elif len(observations) == 1:
                assert observations[-1] == {"status": "success", "data": []}
                name, arguments = "search_transactions", {"category": "Transport"}
            elif len(observations) == 2:
                found = observations[-1]["data"]
                assert found
                name = "delete_flagged_transaction"
                arguments = {"transaction_id": found[0]["id"], "reason": "Review"}
            else:
                pending = observations[-1]
                assert pending["status"] == "confirmation_required"
                return {
                    "text": f"Potwierdź {pending['action_id']}. Nic nie usunięto.",
                    "tool_calls": [],
                }
            return {
                "tool_calls": [
                    {
                        "id": f"call_{len(observations)}",
                        "name": name,
                        "arguments": json.dumps(arguments),
                    }
                ]
            }

    result = run_expense_agent(
        "Znajdź płatność za paliwo i poproś o zgodę na usunięcie",
        client=ObservationDrivenClient(),
        trace_path=trace,
        changes_path=ledger,
        max_steps=4,
    )
    assert result["status"] == "completed"
    assert "Nic nie usunięto" in result["final_answer"]
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    outcomes = [
        event["payload"]["result"]
        for event in events
        if event["event"] == "tool_result"
    ]
    assert len(outcomes) == 3
    assert outcomes[0]["data"] == []
    assert outcomes[1]["data"]
    assert outcomes[2]["status"] == "confirmation_required"
    assert not ledger.exists()


def test_malformed_tool_json_is_returned_to_model_and_traced(tmp_path):
    trace = tmp_path / "malformed.jsonl"

    class CorrectingClient:
        def complete(self, messages, tools=None):
            if messages[-1]["role"] == "tool":
                assert (
                    json.loads(messages[-1]["content"])["status"] == "invalid_arguments"
                )
                return {"text": "Niepoprawne argumenty; operacja nie została wykonana."}
            return {
                "tool_calls": [
                    {"id": "bad", "name": "search_transactions", "arguments": "{"}
                ]
            }

    result = run_expense_agent(
        "Sprawdź transakcje",
        client=CorrectingClient(),
        trace_path=trace,
        changes_path=tmp_path / "changes.jsonl",
    )
    assert result["status"] == "completed"
    entries = [json.loads(line) for line in trace.read_text().splitlines()]
    tool_event = next(entry for entry in entries if entry["event"] == "tool_result")
    assert tool_event["payload"]["arguments"] == "{"
    assert tool_event["payload"]["result"]["status"] == "invalid_arguments"


def test_scenario_a_uses_observed_duplicate_then_requests_confirmation(
    controlled_dataset,
    tmp_path,
):
    """A model mock must inspect actual results before choosing its next call."""
    data_path, changes_path = controlled_dataset
    trace_path = tmp_path / "scenario_a.jsonl"

    class DuplicateReviewClient:
        def complete(self, messages, tools=None):
            observations = [
                json.loads(message["content"])
                for message in messages
                if message["role"] == "tool"
            ]
            if not observations:
                name, arguments = "find_duplicate_candidates", {}
            elif len(observations) == 1:
                duplicate = observations[-1]["data"][0]
                assert duplicate["merchant"] == "Stream"
                name, arguments = (
                    "delete_flagged_transaction",
                    {
                        "transaction_id": duplicate["transaction_ids"][1],
                        "reason": "Duplicate subscription",
                        "confirmed": True,
                    },
                )
            else:
                pending = observations[-1]
                assert pending["status"] == "confirmation_required"
                return {
                    "text": (
                        f"Czy potwierdzasz {pending['action_id']}? Nic nie usunięto."
                    ),
                    "tool_calls": [],
                }
            return {
                "tool_calls": [
                    {
                        "id": f"step_{len(observations)}",
                        "name": name,
                        "arguments": json.dumps(arguments),
                    }
                ]
            }

    result = run_expense_agent(
        "Sprawdź duplikaty i poproś o zgodę na usunięcie",
        client=DuplicateReviewClient(),
        data_path=data_path,
        changes_path=changes_path,
        trace_path=trace_path,
    )
    assert result["status"] == "completed"
    assert result["steps"] == 3
    assert "pending_del_tx_dup_b" in result["final_answer"]
    assert not changes_path.exists()
    observations = [
        event["payload"]["result"]
        for event in map(json.loads, trace_path.read_text().splitlines())
        if event["event"] == "tool_result"
    ]
    assert [item["status"] for item in observations] == [
        "success",
        "confirmation_required",
    ]
    assert len(load_effective_transactions(data_path, changes_path)) == 5


def test_scenario_b_already_assigned_changes_next_tool_and_target(
    controlled_dataset,
    tmp_path,
):
    data_path, changes_path = controlled_dataset
    trace_path = tmp_path / "scenario_b.jsonl"

    class CategorizationClient:
        def complete(self, messages, tools=None):
            observations = [
                json.loads(message["content"])
                for message in messages
                if message["role"] == "tool"
            ]
            if not observations:
                name, arguments = (
                    "assign_category",
                    {
                        "transaction_id": "tx_categorized",
                        "category": "Travel",
                    },
                )
            elif len(observations) == 1:
                assert observations[-1]["status"] == "already_assigned"
                assert observations[-1]["current_category"] == "Groceries"
                # Do not retry with force_override: choose a different target.
                name, arguments = "search_transactions", {"query": "Pending"}
            elif len(observations) == 2:
                target = observations[-1]["data"][0]
                assert target["category_hint"] is None
                name, arguments = (
                    "assign_category",
                    {
                        "transaction_id": target["id"],
                        "category": "Travel",
                    },
                )
            else:
                assert observations[-1]["status"] == "success"
                return {
                    "text": (
                        "Pozostawiono Groceries bez zmian. Skategoryzowano tx_pending."
                    ),
                    "tool_calls": [],
                }
            return {
                "tool_calls": [
                    {
                        "id": f"step_{len(observations)}",
                        "name": name,
                        "arguments": json.dumps(arguments),
                    }
                ]
            }

    result = run_expense_agent(
        "Skategoryzuj zakupy bez nadpisywania istniejących kategorii",
        client=CategorizationClient(),
        data_path=data_path,
        changes_path=changes_path,
        trace_path=trace_path,
    )
    assert result["status"] == "completed"
    assert result["steps"] == 4
    effective = {
        item["id"]: item
        for item in load_effective_transactions(data_path, changes_path)
    }
    assert effective["tx_categorized"]["category_hint"] == "Groceries"
    assert effective["tx_pending"]["category_hint"] == "Travel"
    events = list(map(json.loads, changes_path.read_text().splitlines()))
    assert len(events) == 1
    assert events[0]["transaction_id"] == "tx_pending"
    assert events[0]["payload"]["force_override"] is False
    statuses = [
        event["payload"]["result"]["status"]
        for event in map(json.loads, trace_path.read_text().splitlines())
        if event["event"] == "tool_result"
    ]
    assert statuses == ["already_assigned", "success", "success"]


def test_model_cannot_self_approve_by_repeated_calls(controlled_dataset, tmp_path):
    data_path, changes_path = controlled_dataset

    class SelfApprovingClient:
        def complete(self, messages, tools=None):
            if messages[-1]["role"] == "tool":
                assert (
                    json.loads(messages[-1]["content"])["status"]
                    == "confirmation_required"
                )
            return {
                "tool_calls": [
                    {
                        "id": f"retry_{len(messages)}",
                        "name": "delete_flagged_transaction",
                        "arguments": {"transaction_id": "tx_dup_b", "confirmed": True},
                    }
                ]
            }

    result = run_expense_agent(
        "Usuń duplikat",
        client=SelfApprovingClient(),
        data_path=data_path,
        changes_path=changes_path,
        trace_path=tmp_path / "retries.jsonl",
        max_steps=3,
    )
    assert result["status"] == "step_limit_reached"
    assert not changes_path.exists()
    assert len(load_effective_transactions(data_path, changes_path)) == 5
