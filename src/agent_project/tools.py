"""Read-only tools for inspecting household transactions."""

from __future__ import annotations

import json
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
    status_filter: str | None = None,
    data_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve recent transactions with optional status filtering.

    Args:
        limit: Maximum number of transactions to return (max 50).
        status_filter: Optional filter by status ('normal', 'flagged_duplicate',
            'flagged_high_amount', 'flagged_unrecognized').
        data_path: Optional custom path to transactions JSON file.
    """
    records = load_transactions_data(data_path)
    if status_filter:
        records = [tx for tx in records if tx.get("status") == status_filter]
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


# Tool schemas in standard OpenAI format for the model
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_transactions",
            "description": "Fetch a list of household bank transactions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max transactions to return (1-20).",
                        "default": 10,
                    },
                    "status_filter": {
                        "type": "string",
                        "description": (
                            "Filter by status: 'normal', 'flagged_duplicate', "
                            "'flagged_high_amount', 'flagged_unrecognized'"
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
                "Get complete details and notes for a specific transaction by its ID."
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
]
