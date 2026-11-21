"""T-01: verify shared fixtures before implementing any web functionality."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agent_project.agent import execute_controlled_tool, run_expense_agent
from agent_project.tools import load_effective_transactions


def test_controlled_clock_starts_at_fixed_utc_time(controlled_clock):
    assert controlled_clock.now() == datetime(2026, 10, 25, tzinfo=UTC)
    assert controlled_clock.monotonic() == 0.0


def test_controlled_clock_advances_both_time_sources(controlled_clock):
    start = controlled_clock.now()
    controlled_clock.advance(599)
    controlled_clock.advance(1.5)
    assert controlled_clock.now() == start + timedelta(seconds=600.5)
    assert controlled_clock.monotonic() == 600.5


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf"), float("-inf")])
def test_clock_rejects_invalid_advance_without_changing_time(controlled_clock, seconds):
    start = controlled_clock.now()
    with pytest.raises(ValueError, match="finite and non-negative"):
        controlled_clock.advance(seconds)
    assert controlled_clock.now() == start
    assert controlled_clock.monotonic() == 0.0


def test_dataset_is_fixed_and_ledger_starts_absent(controlled_dataset, tmp_path):
    data_path, changes_path = controlled_dataset
    records = json.loads(data_path.read_text(encoding="utf-8"))
    assert data_path.parent == changes_path.parent == tmp_path
    assert len(records) == 5
    assert {record["id"] for record in records} == {
        "tx_categorized",
        "tx_pending",
        "tx_dup_a",
        "tx_dup_b",
        "tx_fuel",
    }
    assert not changes_path.exists()


def test_temporary_ledger_does_not_change_fixture_baseline(controlled_dataset):
    data_path, changes_path = controlled_dataset
    original_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
    result = execute_controlled_tool(
        "assign_category",
        {"transaction_id": "tx_pending", "category": "Other"},
        data_path=data_path,
        changes_path=changes_path,
    )
    assert result["status"] == "success"
    assert changes_path.exists()
    assert len(changes_path.read_text().splitlines()) == 1
    assert hashlib.sha256(data_path.read_bytes()).hexdigest() == original_hash
    records = {
        tx["id"]: tx for tx in load_effective_transactions(data_path, changes_path)
    }
    assert records["tx_pending"]["category_hint"] == "Other"


def test_offline_client_records_snapshots_not_mutable_references(
    offline_model_factory,
):
    client = offline_model_factory([{"text": "Ready", "tool_calls": []}])
    messages = [{"role": "user", "content": "Review expenses"}]
    tools = [{"type": "function", "function": {"name": "search_transactions"}}]
    assert client.complete(messages, tools)["text"] == "Ready"
    messages[0]["content"] = "Changed after call"
    tools[0]["function"]["name"] = "changed"
    assert client.calls[0]["messages"][0]["content"] == "Review expenses"
    assert client.calls[0]["tools"][0]["function"]["name"] == "search_transactions"
    assert client.model == client.provider == "mock"


def test_offline_clients_have_independent_response_queues(offline_model_factory):
    scripted = [{"text": "Ready", "tool_calls": []}]
    first = offline_model_factory(scripted)
    second = offline_model_factory(scripted)
    scripted[0]["text"] = "Changed by caller"
    first.complete([])["text"] = "Changed by consumer"
    assert second.complete([])["text"] == "Ready"
    with pytest.raises(RuntimeError, match="queue is empty"):
        first.complete([])


def test_offline_client_does_not_send_http_requests(
    offline_model_factory,
    monkeypatch,
):
    def unexpected_http(*args, **kwargs):
        raise AssertionError("Fixture attempted an HTTP request")

    async def unexpected_async_http(*args, **kwargs):
        raise AssertionError("Fixture attempted an asynchronous HTTP request")

    monkeypatch.setattr(httpx.Client, "send", unexpected_http)
    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected_async_http)
    client = offline_model_factory([{"text": "Offline", "tool_calls": []}])
    assert client.complete([])["text"] == "Offline"


def test_agent_accepts_offline_client_and_isolated_dataset(
    offline_model_factory,
    controlled_dataset,
    tmp_path,
):
    data_path, changes_path = controlled_dataset
    trace_path = tmp_path / "fixture_agent.jsonl"
    client = offline_model_factory(
        [
            {
                "tool_calls": [
                    {
                        "id": "fixture_search",
                        "name": "search_transactions",
                        "arguments": '{"merchant":"Stream"}',
                    }
                ]
            },
            {"text": "Two subscription transactions found.", "tool_calls": []},
        ]
    )
    result = run_expense_agent(
        "Review subscription transactions",
        client=client,
        data_path=data_path,
        changes_path=changes_path,
        trace_path=trace_path,
    )
    assert result["status"] == "completed"
    assert result["steps"] == 2
    assert len(client.calls) == 2
    tool_message = next(
        message for message in client.calls[1]["messages"] if message["role"] == "tool"
    )
    observed = json.loads(tool_message["content"])
    assert observed["status"] == "success"
    assert len(observed["data"]) == 2
    assert not changes_path.exists()
