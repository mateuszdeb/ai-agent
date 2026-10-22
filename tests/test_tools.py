
import pytest

from agent_project.tools import (
    TOOL_DEFINITIONS,
    compute_budget_baseline,
    find_duplicate_candidates,
    get_transaction_details,
    get_transactions,
)

# ---------------------------------------------------------------------------
# get_transactions
# ---------------------------------------------------------------------------


def test_tool_definitions_count() -> None:
    assert len(TOOL_DEFINITIONS) == 4
    names = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}
    assert names == {
        "get_transactions",
        "get_transaction_details",
        "compute_budget_baseline",
        "find_duplicate_candidates",
    }


def test_get_transactions_returns_data() -> None:
    txs = get_transactions(limit=5)
    assert len(txs) > 0
    assert "id" in txs[0]
    assert "amount" in txs[0]
    assert "merchant" in txs[0]


def test_get_transactions_no_pre_labels() -> None:
    """Transactions must NOT carry pre-labeled anomaly fields."""
    txs = get_transactions(limit=50)
    for tx in txs:
        assert "status" not in tx, f"tx {tx['id']} still has a 'status' label"
        assert "notes" not in tx, f"tx {tx['id']} still has a 'notes' label"


def test_get_transactions_merchant_filter() -> None:
    spotify = get_transactions(merchant="Spotify")
    assert len(spotify) == 2
    assert all("Spotify" in tx["merchant"] for tx in spotify)


def test_get_transactions_merchant_filter_case_insensitive() -> None:
    results = get_transactions(merchant="biedronka")
    assert len(results) > 0


def test_get_transactions_merchant_filter_no_match() -> None:
    results = get_transactions(merchant="NonExistentMerchant123")
    assert results == []


# ---------------------------------------------------------------------------
# get_transaction_details
# ---------------------------------------------------------------------------


def test_get_transaction_details_found() -> None:
    tx = get_transaction_details("tx_105")
    assert tx["merchant"] == "Carrefour Hipermarket"
    assert tx["amount"] == 1850.00


def test_get_transaction_details_not_found() -> None:
    res = get_transaction_details("non_existent_tx")
    assert "error" in res


# ---------------------------------------------------------------------------
# compute_budget_baseline
# ---------------------------------------------------------------------------


def test_compute_budget_baseline_by_merchant() -> None:
    result = compute_budget_baseline(merchant="Biedronka")
    assert result["count"] >= 2, "Should find multiple Biedronka transactions"
    assert "avg_amount" in result
    assert result["min_amount"] <= result["avg_amount"] <= result["max_amount"]


def test_compute_budget_baseline_by_category() -> None:
    result = compute_budget_baseline(category_hint="Utilities")
    assert result["count"] >= 2
    assert result["avg_amount"] > 0


def test_compute_budget_baseline_detects_spike() -> None:
    """PGE tx_110 (541.20 PLN) should be well above the average (~284 PLN baseline)."""
    baseline = compute_budget_baseline(merchant="PGE")
    # With tx_102 (284.10) and tx_110 (541.20) both present, avg > 284
    assert baseline["count"] == 2
    assert baseline["max_amount"] == pytest.approx(541.20)
    # The spike is detectable: max is ~90% higher than min
    ratio = baseline["max_amount"] / baseline["min_amount"]
    assert ratio > 1.5, "Expected a detectable spike in PGE billing"


def test_compute_budget_baseline_no_data() -> None:
    result = compute_budget_baseline(merchant="NonExistentXYZ")
    assert result["count"] == 0
    assert "message" in result


# ---------------------------------------------------------------------------
# find_duplicate_candidates
# ---------------------------------------------------------------------------


def test_find_duplicate_candidates_finds_spotify() -> None:
    dupes = find_duplicate_candidates()
    assert len(dupes) >= 1
    merchants = [d["merchant"] for d in dupes]
    assert any("Spotify" in m for m in merchants)


def test_find_duplicate_candidates_structure() -> None:
    dupes = find_duplicate_candidates()
    for d in dupes:
        assert "merchant" in d
        assert "amount" in d
        assert "transaction_ids" in d
        assert len(d["transaction_ids"]) >= 2
        assert "days_apart" in d
        assert d["days_apart"] <= 3
