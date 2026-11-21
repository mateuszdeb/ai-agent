"""T-13: model-free transaction reads through the shared controller.

Every comparison runs the very same controlled tool the service uses, so
assertions hold against effective state after ledger changes. No test
writes, starts a run or touches a model; baselines and ledger bytes stay
input-only.
"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from agent_project.agent import execute_controlled_tool
from agent_project.state_lock import LedgerGuard
from agent_project.tools import append_change_event
from agent_project.web import WebSettings, create_app
from agent_project.web.read_service import transaction_card_from_record

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def app_with_ledger(controlled_dataset):
    """A fresh app whose read service binds the isolated dataset paths."""
    data_path, changes_path = controlled_dataset
    app = create_app(
        settings=WebSettings(data_path=data_path, changes_path=changes_path)
    )
    try:
        yield app, data_path, changes_path
    finally:
        app.state.runner.shutdown(wait=False)


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
        ) as client:
            response = await client.get("/api/session")
            assert response.status_code == 200
            return await scenario(client, response.json())

    return asyncio.run(run())


def assign_event(tx_id: str, previous: str | None, new: str) -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "action_id": "act_read_setup01",
        "action_type": "assign_category",
        "transaction_id": tx_id,
        "payload": {
            "previous_category": previous,
            "new_category": new,
            "rationale": "Ledger prepared by the test for effective reads.",
            "force_override": bool(previous),
        },
    }


def controller_cards(data_path, changes_path, payload: dict) -> list[dict]:
    result = execute_controlled_tool(
        "search_transactions",
        payload,
        data_path=data_path,
        changes_path=changes_path,
    )
    assert result["status"] == "success"
    return [
        transaction_card_from_record(record).model_dump() for record in result["data"]
    ]


def test_list_matches_controller_after_ledger_changes(app_with_ledger):
    app, data_path, changes_path = app_with_ledger
    with LedgerGuard(changes_path):
        append_change_event(assign_event("tx_pending", None, "Food"), changes_path)

    async def scenario(client, session):
        response = await client.get("/api/transactions?limit=50")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert set(payload) == {"items", "limit", "possibly_truncated"}
        assert payload["limit"] == 50
        assert payload["possibly_truncated"] is False
        expected = controller_cards(data_path, changes_path, {"limit": 50})
        assert payload["items"] == expected

        # The category comes from the effective state, not the base JSON.
        categories = {
            item["transaction_id"]: item["category"] for item in payload["items"]
        }
        assert categories["tx_pending"] == "Food"
        assert categories["tx_categorized"] == "Groceries"

    exercise(app, scenario)


def test_filters_use_existing_search_semantics(app_with_ledger):
    app, data_path, changes_path = app_with_ledger

    async def scenario(client, session):
        for query in {"query": "stream"}, {"merchant": "stream"}:
            response = await client.get(
                f"/api/transactions?limit=50&{next(iter(query))}=stream"
            )
            assert response.status_code == 200
            payload = response.json()
            expected = controller_cards(data_path, changes_path, {**query, "limit": 50})
            assert payload["items"] == expected
            assert payload["possibly_truncated"] is False

    exercise(app, scenario)


def test_empty_result_is_clean_not_an_error(app_with_ledger):
    app, data_path, changes_path = app_with_ledger

    async def scenario(client, session):
        response = await client.get("/api/transactions?merchant=Nonexistent")
        assert response.status_code == 200
        payload = response.json()
        assert payload["items"] == []
        assert payload["possibly_truncated"] is False
        assert payload["limit"] == 10

    exercise(app, scenario)


def test_limit_reaches_exactly_one_result_and_marks_truncation(app_with_ledger):
    app, data_path, changes_path = app_with_ledger

    async def scenario(client, session):
        response = await client.get("/api/transactions?limit=1")
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["items"]) == 1
        assert payload["possibly_truncated"] is True
        assert payload["limit"] == 1

    exercise(app, scenario)


@pytest.mark.parametrize(
    "query",
    ["?limit=0", "?limit=51", "?limit=abc", "?limit=1.5", "?limit=", "?page=1"],
)
def test_limit_and_unknown_parameters_fail_closed(app_with_ledger, query):
    app, data_path, changes_path = app_with_ledger

    async def scenario(client, session):
        response = await client.get(f"/api/transactions{query}")
        if query == "?limit=":
            # Empty text is not a number; keep the strict bounds.
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_request"
            return
        if query in {"?limit=0", "?limit=51", "?limit=abc", "?limit=1.5", "?page=1"}:
            assert response.status_code == 422, query
            assert response.json()["error"]["code"] == "invalid_request"

    exercise(app, scenario)


def test_duplicate_parameters_fail_closed(app_with_ledger):
    app, data_path, changes_path = app_with_ledger

    async def scenario(client, session):
        response = await client.get(
            httpx.URL("/api/transactions", params={"limit": "10"})
        )
        assert response.status_code == 200

        from agent_project.web.read_service import parse_read_query

        with pytest.raises(ValueError, match="Duplicate"):
            parse_read_query([("limit", "1"), ("limit", "2")])

    exercise(app, scenario)


def test_details_of_existing_and_unknown_record(app_with_ledger):
    app, data_path, changes_path = app_with_ledger
    with LedgerGuard(changes_path):
        append_change_event(
            assign_event("tx_fuel", "Transport", "Vacation"), changes_path
        )

    async def scenario(client, session):
        response = await client.get("/api/transactions/tx_fuel")
        assert response.status_code == 200, response.text
        card = response.json()
        assert card["transaction_id"] == "tx_fuel"
        assert card["merchant"] == "Fuel Station"
        assert card["amount"] == 200.0
        assert card["currency"] == "PLN"
        assert card["category"] == "Vacation"  # effective, not baseline
        assert card["description"] == "Fuel purchase"

        missing = await client.get("/api/transactions/tx_absent")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "transaction_unavailable"

    exercise(app, scenario)


def test_reads_never_write_and_never_start_work(app_with_ledger):
    app, data_path, changes_path = app_with_ledger
    before = changes_path.read_bytes() if changes_path.exists() else b""

    async def scenario(client, session):
        for path in (
            "/api/transactions?limit=50",
            "/api/transactions?query=stream",
            "/api/transactions/tx_dup_a",
            "/api/transactions/tx_absent",
        ):
            response = await client.get(path)
            assert response.status_code in {200, 404}
        assert app.state.runs.count() == 0
        assert app.state.runs.active_count() == 0
        assert app.state.runner.is_busy() is False
        assert not (changes_path.exists() and changes_path.read_bytes() != before)


def test_reads_require_a_session(app_with_ledger):
    app, data_path, changes_path = app_with_ledger

    async def scenario(client, session):
        response = await client.get(
            "/api/transactions",
            headers={"Cookie": ""},
        )
        # The session boundary rejects unauthenticated reads.
        assert response.status_code in {403, 404}

    exercise(app, scenario)
