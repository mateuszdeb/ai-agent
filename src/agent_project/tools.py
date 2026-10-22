"""Read-only tools for inspecting household transactions."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_DATA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "transactions.json"
)


def load_transactions_data(data_path: Path | None = None) -> list[dict[str, Any]]:
    """Load transaction records from the JSON dataset."""
    path = data_path or DEFAULT_DATA_PATH
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_transactions(
    limit: int = 10,
    merchant: str | None = None,
    data_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve recent transactions with optional merchant filtering.

    Args:
        limit: Maximum number of transactions to return (max 50).
        merchant: Optional case-insensitive substring filter on merchant name.
        data_path: Optional custom path to transactions JSON file.
    """
    records = load_transactions_data(data_path)
    if merchant:
        records = [
            tx for tx in records if merchant.lower() in tx.get("merchant", "").lower()
        ]
    return records[: min(limit, 50)]


def get_transaction_details(
    transaction_id: str,
    data_path: Path | None = None,
) -> dict[str, Any]:
    """Retrieve detailed information about a single transaction by its ID.

    Args:
        transaction_id: The unique identifier of the transaction (e.g. 'tx_105').
        data_path: Optional custom path to transactions JSON file.
    """
    records = load_transactions_data(data_path)
    for tx in records:
        if tx.get("id") == transaction_id:
            return tx
    return {"error": f"Transaction '{transaction_id}' not found"}


def compute_budget_baseline(
    merchant: str | None = None,
    category_hint: str | None = None,
    data_path: Path | None = None,
) -> dict[str, Any]:
    """Compute spending statistics (avg, min, max, count) from historical transactions.

    Use this to detect anomalies: compare a suspicious transaction's amount
    against the historical baseline for the same merchant or category.

    Args:
        merchant: Filter baseline to a specific merchant name
            (case-insensitive substring).
        category_hint: Filter baseline to a specific category hint
            (e.g. 'Groceries').
        data_path: Optional custom path to transactions JSON file.
    """
    records = load_transactions_data(data_path)

    if merchant:
        records = [
            tx for tx in records if merchant.lower() in tx.get("merchant", "").lower()
        ]
    if category_hint:
        records = [
            tx
            for tx in records
            if (tx.get("category_hint") or "").lower() == category_hint.lower()
        ]

    if not records:
        return {
            "merchant": merchant,
            "category_hint": category_hint,
            "count": 0,
            "message": "No historical data found for the given filters.",
        }

    amounts = [tx["amount"] for tx in records]
    avg = sum(amounts) / len(amounts)
    return {
        "merchant": merchant,
        "category_hint": category_hint,
        "count": len(amounts),
        "avg_amount": round(avg, 2),
        "min_amount": min(amounts),
        "max_amount": max(amounts),
        "transactions": [tx["id"] for tx in records],
    }


def find_duplicate_candidates(
    data_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Find transaction pairs that are likely duplicates.

    Criteria: same merchant, same amount, within 3 days of each other.

    Returns a list of suspected duplicate groups for the agent to investigate.

    Args:
        data_path: Optional custom path to transactions JSON file.
    """
    records = load_transactions_data(data_path)

    # Group by (merchant, amount)
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for tx in records:
        key = (tx.get("merchant", ""), tx.get("amount", 0))
        groups[key].append(tx)

    duplicates = []
    for (merch, amount), txs in groups.items():
        if len(txs) < 2:
            continue
        # Check if any two share dates within 3 days
        from datetime import date

        dated = sorted(txs, key=lambda t: t["date"])
        for i in range(len(dated) - 1):
            d1 = date.fromisoformat(dated[i]["date"])
            d2 = date.fromisoformat(dated[i + 1]["date"])
            if abs((d2 - d1).days) <= 3:
                duplicates.append(
                    {
                        "merchant": merch,
                        "amount": amount,
                        "transaction_ids": [dated[i]["id"], dated[i + 1]["id"]],
                        "dates": [dated[i]["date"], dated[i + 1]["date"]],
                        "days_apart": abs((d2 - d1).days),
                    }
                )

    return duplicates


# Tool schemas in standard OpenAI format for the model
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_transactions",
            "description": (
                "Fetch a list of household bank transactions. "
                "Optionally filter by merchant name substring. "
                "Use this first to get an overview of recent spending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max transactions to return (1-50).",
                        "default": 10,
                    },
                    "merchant": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring to filter by merchant name, "
                            "e.g. 'Spotify' or 'Biedronka'."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_transaction_details",
            "description": (
                "Get complete details for a specific transaction by its ID. "
                "Use this to inspect a suspicious transaction more closely."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {
                        "type": "string",
                        "description": "The transaction ID to inspect, e.g. 'tx_105'.",
                    },
                },
                "required": ["transaction_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_budget_baseline",
            "description": (
                "Compute historical spending statistics (average, min, max, count) "
                "for a given merchant or category. "
                "Use this to check whether a transaction amount is unusually "
                "high or low compared to past spending patterns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "merchant": {
                        "type": "string",
                        "description": (
                            "Case-insensitive merchant name substring to compute "
                            "baseline for, e.g. 'PGE' or 'Biedronka'."
                        ),
                    },
                    "category_hint": {
                        "type": "string",
                        "description": (
                            "Category to compute baseline for, e.g. 'Groceries', "
                            "'Utilities', 'Transport', 'Healthcare', 'Subscriptions'."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_duplicate_candidates",
            "description": (
                "Scan all transactions for likely duplicate charges: "
                "same merchant, same amount, within 3 days of each other. "
                "Use this early in the analysis to identify potential double-billings."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]
