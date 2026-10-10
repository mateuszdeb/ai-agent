from agent_project.tools import (
    TOOL_DEFINITIONS,
    get_transaction_details,
    get_transactions,
)


def test_tool_definitions_structure() -> None:
    assert len(TOOL_DEFINITIONS) == 2
    names = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}
    assert "get_transactions" in names
    assert "get_transaction_details" in names


def test_get_transactions_returns_data() -> None:
    txs = get_transactions(limit=5)
    assert len(txs) > 0
    assert "id" in txs[0]
    assert "amount" in txs[0]
    assert "merchant" in txs[0]


def test_get_transactions_status_filter() -> None:
    duplicates = get_transactions(status_filter="flagged_duplicate")
    assert len(duplicates) == 1
    assert duplicates[0]["merchant"] == "Spotify Polska"


def test_get_transaction_details_found() -> None:
    tx = get_transaction_details("tx_105")
    assert tx["merchant"] == "Carrefour Hipermarket"
    assert tx["amount"] == 1850.00


def test_get_transaction_details_not_found() -> None:
    res = get_transaction_details("non_existent_tx")
    assert "error" in res
