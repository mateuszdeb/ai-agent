import json

import pytest

from agent_project.llm import MockClient
from agent_project.multitool_demo import run_demo, validate_demo


def valid_trace():
    return [
        {
            "event": "tool_result",
            "payload": {
                "step": 1,
                "tool_name": "search_transactions",
                "result": {"status": "success", "data": [{"id": "tx_104"}]},
            },
        },
        {
            "event": "tool_result",
            "payload": {
                "step": 2,
                "tool_name": "delete_flagged_transaction",
                "result": {
                    "status": "confirmation_required",
                    "action_id": "pending_del_tx_104",
                },
            },
        },
        {
            "event": "model_response",
            "payload": {
                "latency_ms": 10,
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "cost": 0},
            },
        },
    ]


def completed_result():
    return {"status": "completed", "final_answer": "Potwierdzasz pending_del_tx_104?"}


def test_validator_accepts_sequential_adaptation():
    assert validate_demo(valid_trace(), completed_result(), ledger_changed=False) == []


@pytest.mark.parametrize(
    "mutation", ["same_turn", "no_action_id", "state_change", "no_usage"]
)
def test_validator_rejects_invalid_evidence(mutation):
    events, result = valid_trace(), completed_result()
    if mutation == "same_turn":
        events[1]["payload"]["step"] = 1
    elif mutation == "no_action_id":
        result["final_answer"] = "Done"
    elif mutation == "no_usage":
        events[-1]["payload"]["usage"] = {}
    assert validate_demo(events, result, ledger_changed=mutation == "state_change")


def test_failed_run_preserves_diagnostic_not_success_evidence(
    controlled_dataset,
    tmp_path,
):
    data_path, _ = controlled_dataset
    target = tmp_path / "success.jsonl"

    class FailingClient:
        def complete(self, messages, tools=None):
            raise ConnectionError("SECRET_VALUE /private/user/path")

    result = run_demo(evidence_path=target, data_path=data_path, client=FailingClient())
    assert result["status"] == "blocked"
    assert result["errors"] == ["ConnectionError"]
    assert not target.exists()
    attempt = next(tmp_path.glob("success_attempt_*.jsonl"))
    content = attempt.read_text()
    assert "SECRET_VALUE" not in content
    assert "/private/user/path" not in content
    assert json.loads(content.splitlines()[-1])["event"] == "demo_failed"


def test_mock_cannot_create_canonical_live_evidence(controlled_dataset, tmp_path):
    data_path, _ = controlled_dataset
    target = tmp_path / "success.jsonl"
    result = run_demo(
        evidence_path=target,
        data_path=data_path,
        client=MockClient([{"text": "Completed", "tool_calls": []}]),
    )
    assert result["status"] == "rejected"
    assert "injected_client_is_not_live_evidence" in result["errors"]
    assert not target.exists()


def test_existing_evidence_is_not_overwritten(controlled_dataset, tmp_path):
    data_path, _ = controlled_dataset
    target = tmp_path / "success.jsonl"
    target.write_text("existing evidence")
    with pytest.raises(FileExistsError):
        run_demo(evidence_path=target, data_path=data_path, client=MockClient([]))
    assert target.read_text() == "existing evidence"
