"""Domain tools for transactions with append-only ledger and validation."""

from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Any

from agent_project.models import (
    AssignCategoryArgs,
    ComputeBudgetBaselineArgs,
    DeleteTransactionArgs,
    FindDuplicateCandidatesArgs,
    GetTransactionDetailsArgs,
    SearchTransactionsArgs,
    ToolArgsModel,
    UndoCategoryAssignmentArgs,
)
from agent_project.state_lock import (
    LedgerCorruptionError,
    LedgerGuard,
    LedgerWriteError,
)

DEFAULT_DATA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "transactions.json"
)
DEFAULT_CHANGES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "transaction_changes.jsonl"
)


def _ledger_locked[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    """Preserve signatures and lock the full read/check/write transaction."""
    call_signature = signature(function)

    @wraps(function)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        bound = call_signature.bind(*args, **kwargs)
        path = bound.arguments.get("changes_path") or DEFAULT_CHANGES_PATH
        with LedgerGuard(path):
            return function(*args, **kwargs)

    return guarded


def _validate_ledger_events(events: list[dict[str, Any]]) -> None:
    """Fail closed on malformed events, duplicate IDs and invalid undo links."""
    seen: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            raise LedgerCorruptionError("Ledger event must be an object.")
        for key in ("action_id", "transaction_id", "timestamp"):
            if not isinstance(event.get(key), str) or not event[key]:
                raise LedgerCorruptionError(
                    "Ledger event has missing or invalid metadata."
                )
        action_id = event["action_id"]
        if action_id in seen:
            raise LedgerCorruptionError("Ledger contains duplicate action IDs.")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise LedgerCorruptionError("Ledger event payload must be an object.")
        kind = event.get("action_type")
        if kind == "assign_category":
            valid = (
                isinstance(payload.get("new_category"), str)
                and bool(payload["new_category"])
                and "previous_category" in payload
                and (
                    payload["previous_category"] is None
                    or isinstance(payload["previous_category"], str)
                )
                and isinstance(payload.get("rationale"), str)
                and isinstance(payload.get("force_override"), bool)
            )
        elif kind == "undo_category":
            target = (
                seen.get(event.get("target_action_id"))
                if isinstance(event.get("target_action_id"), str)
                else None
            )
            valid = (
                target is not None
                and target["action_type"] == "assign_category"
                and target["transaction_id"] == event["transaction_id"]
                and "reverted_to" in payload
                and (
                    payload["reverted_to"] is None
                    or isinstance(payload["reverted_to"], str)
                )
                and isinstance(payload.get("reason"), str)
            )
        elif kind == "delete_transaction":
            valid = payload.get("confirmed") is True and isinstance(
                payload.get("reason"), str
            )
        else:
            valid = False
        if not valid:
            raise LedgerCorruptionError(
                "Ledger event has an invalid action or payload."
            )
        seen[action_id] = event


def _ledger_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise LedgerCorruptionError("Ledger contains duplicate JSON keys.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise LedgerCorruptionError("Ledger contains a non-finite JSON number.")


# ---------------------------------------------------------------------------
# State Management: Raw Transactions & Append-Only Changes Ledger
# ---------------------------------------------------------------------------


def load_raw_transactions(data_path: Path | None = None) -> list[dict[str, Any]]:
    """Load baseline transactions from read-only transactions.json."""
    path = data_path or DEFAULT_DATA_PATH
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@_ledger_locked
def load_changes_log(changes_path: Path | None = None) -> list[dict[str, Any]]:
    """Load append-only ledger events from transaction_changes.jsonl."""
    path = changes_path or DEFAULT_CHANGES_PATH
    if not path.exists():
        return []
    events = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    raise LedgerCorruptionError("Ledger has an incomplete final line.")
                if line.strip():
                    events.append(
                        json.loads(
                            line,
                            object_pairs_hook=_ledger_object,
                            parse_constant=_reject_json_constant,
                        )
                    )
    except (json.JSONDecodeError, UnicodeError):
        raise LedgerCorruptionError("Ledger contains unreadable events.") from None
    _validate_ledger_events(events)
    return events


@_ledger_locked
def append_change_event(
    event: dict[str, Any],
    changes_path: Path | None = None,
) -> None:
    """Append a single domain event to the append-only ledger."""
    path = changes_path or DEFAULT_CHANGES_PATH
    serialized = json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
    # Validate before starting a write; never append after a partial/corrupt event.
    _validate_ledger_events([*load_changes_log(path), json.loads(serialized)])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as handle:
            if handle.write(serialized) != len(serialized):
                raise OSError("Incomplete ledger write.")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise LedgerWriteError("Ledger write outcome is unknown.") from None


@_ledger_locked
def load_effective_transactions(
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Compute the effective state of all transactions.

    Applies the sequence of domain events from the append-only ledger
    onto the immutable baseline data/transactions.json.
    """
    raw = load_raw_transactions(data_path)
    # Deep copy to guarantee baseline transactions.json is never mutated
    tx_map = {tx["id"]: dict(tx) for tx in raw}

    events = load_changes_log(changes_path)
    assignments = {}
    for ev in events:
        action_type = ev.get("action_type")
        tx_id = ev.get("transaction_id")
        if tx_id not in tx_map or tx_map[tx_id].get("is_deleted", False):
            raise LedgerCorruptionError("Ledger references an unavailable transaction.")

        target = tx_map[tx_id]
        if action_type == "assign_category":
            if ev["payload"]["previous_category"] != target.get("category_hint") or (
                target.get("category_hint") and not ev["payload"]["force_override"]
            ):
                raise LedgerCorruptionError("Ledger category state is inconsistent.")
            target["category_hint"] = ev["payload"].get("new_category")
            target["last_action_id"] = ev.get("action_id")
            assignments[ev["action_id"]] = ev
        elif action_type == "undo_category":
            original = assignments[ev["target_action_id"]]
            if (
                target.get("last_action_id") != original["action_id"]
                or ev["payload"]["reverted_to"]
                != original["payload"]["previous_category"]
            ):
                raise LedgerCorruptionError("Ledger undo state is inconsistent.")
            target["category_hint"] = ev["payload"].get("reverted_to")
            target["last_action_id"] = ev.get("action_id")
        elif action_type == "delete_transaction":
            target["is_deleted"] = True
            target["deleted_reason"] = ev["payload"].get("reason")
            target["deleted_action_id"] = ev.get("action_id")

    return [tx for tx in tx_map.values() if not tx.get("is_deleted", False)]


# Backward compatibility alias
load_transactions_data = load_effective_transactions


# ---------------------------------------------------------------------------
# 1. Read-Only Tools
# ---------------------------------------------------------------------------


def search_transactions(
    query: str | None = None,
    merchant: str | None = None,
    category: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    limit: int = 10,
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Search and filter household bank transactions (read-only).

    Args:
        query: Optional case-insensitive text search in merchant or description.
        merchant: Optional substring filter on merchant name.
        category: Optional category filter (e.g. 'Groceries', 'Utilities').
        min_amount: Minimum transaction amount in PLN.
        max_amount: Maximum transaction amount in PLN.
        limit: Max transactions to return (1-50, default 10).
        data_path: Optional custom path to baseline JSON file.
        changes_path: Optional custom path to append-only changes ledger.
    """
    records = load_effective_transactions(data_path, changes_path)

    if query:
        q = query.lower()
        records = [
            tx
            for tx in records
            if q in tx.get("merchant", "").lower()
            or q in tx.get("description", "").lower()
        ]

    if merchant:
        m = merchant.lower()
        records = [tx for tx in records if m in tx.get("merchant", "").lower()]

    if category:
        c = category.lower()
        records = [
            tx
            for tx in records
            if (tx.get("category_hint") or "").lower() == c
            or (tx.get("category") or "").lower() == c
        ]

    if min_amount is not None:
        records = [tx for tx in records if tx.get("amount", 0.0) >= min_amount]

    if max_amount is not None:
        records = [tx for tx in records if tx.get("amount", 0.0) <= max_amount]

    clamped_limit = max(1, min(limit, 50))
    return records[:clamped_limit]


def get_transactions(
    limit: int = 10,
    merchant: str | None = None,
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve recent transactions (read-only).

    Compatibility alias delegating to search_transactions.
    """
    return search_transactions(
        merchant=merchant,
        limit=limit,
        data_path=data_path,
        changes_path=changes_path,
    )


def get_transaction_details(
    transaction_id: str,
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> dict[str, Any]:
    """Retrieve complete details for a specific transaction by its ID (read-only)."""
    records = load_effective_transactions(data_path, changes_path)
    for tx in records:
        if tx.get("id") == transaction_id:
            return tx
    return {
        "status": "not_found",
        "error": f"Transaction '{transaction_id}' not found",
    }


def compute_budget_baseline(
    merchant: str | None = None,
    category_hint: str | None = None,
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> dict[str, Any]:
    """Compute spending statistics from transactions (read-only)."""
    records = load_effective_transactions(data_path, changes_path)

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
    changes_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Find transaction pairs that are likely duplicate charges (read-only)."""
    records = load_effective_transactions(data_path, changes_path)

    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for tx in records:
        key = (tx.get("merchant", ""), tx.get("amount", 0))
        groups[key].append(tx)

    duplicates = []
    for (merch, amount), txs in groups.items():
        if len(txs) < 2:
            continue

        dated = sorted(txs, key=lambda t: t["date"])
        for i in range(len(dated) - 1):
            d1 = date.fromisoformat(dated[i]["date"])
            d2 = date.fromisoformat(dated[i + 1]["date"])
            days_diff = abs((d2 - d1).days)
            if days_diff <= 3:
                duplicates.append(
                    {
                        "merchant": merch,
                        "amount": amount,
                        "transaction_ids": [dated[i]["id"], dated[i + 1]["id"]],
                        "dates": [dated[i]["date"], dated[i + 1]["date"]],
                        "days_apart": days_diff,
                    }
                )

    return duplicates


# ---------------------------------------------------------------------------
# 2. Reversible Write Tools: assign_category & undo_category_assignment
# ---------------------------------------------------------------------------


@_ledger_locked
def assign_category(
    transaction_id: str,
    category: str,
    rationale: str = "",
    force_override: bool = False,
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> dict[str, Any]:
    """Assign or update the budget category of a transaction (reversible write).

    Validates transaction existence and prevents unintended overwrites:
    if a category is already assigned, returns status 'already_assigned'
    unless force_override=True.
    """
    records = load_effective_transactions(data_path, changes_path)
    target = next((tx for tx in records if tx.get("id") == transaction_id), None)

    if not target:
        return {
            "status": "not_found",
            "transaction_id": transaction_id,
            "message": f"Transaction '{transaction_id}' not found.",
        }

    current_cat = target.get("category_hint")
    if current_cat and not force_override:
        return {
            "status": "already_assigned",
            "transaction_id": transaction_id,
            "current_category": current_cat,
            "message": (
                f"Transaction '{transaction_id}' already has category '{current_cat}'. "
                "Set force_override=True to overwrite."
            ),
        }

    # Generate unique action ID for audit and reversible undo
    timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    action_id = f"act_cat_{timestamp_str}_{uuid.uuid4().hex[:6]}"

    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "action_id": action_id,
        "action_type": "assign_category",
        "transaction_id": transaction_id,
        "payload": {
            "previous_category": current_cat,
            "new_category": category,
            "rationale": rationale,
            "force_override": force_override,
        },
    }
    append_change_event(event, changes_path)

    return {
        "status": "success",
        "action_id": action_id,
        "transaction_id": transaction_id,
        "category": category,
        "previous_category": current_cat,
        "message": f"Assigned category '{category}' to transaction '{transaction_id}'.",
    }


@_ledger_locked
def undo_category_assignment(
    action_id: str,
    reason: str = "User requested undo",
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> dict[str, Any]:
    """Revert a previous assign_category action (reversible write undo).

    Reverts to the previous category only if the transaction state has not
    been modified by a newer action.
    """
    events = load_changes_log(changes_path)

    target_event = next(
        (
            ev
            for ev in events
            if ev.get("action_id") == action_id
            and ev.get("action_type") == "assign_category"
        ),
        None,
    )
    if not target_event:
        return {
            "status": "action_not_found",
            "action_id": action_id,
            "message": f"Action '{action_id}' not found.",
        }

    # Check if already reverted
    already_reverted = any(
        ev.get("action_type") == "undo_category"
        and ev.get("target_action_id") == action_id
        for ev in events
    )
    if already_reverted:
        return {
            "status": "already_reverted",
            "action_id": action_id,
            "message": f"Action '{action_id}' has already been reverted.",
        }

    tx_id = target_event["transaction_id"]
    assigned_category = target_event["payload"]["new_category"]

    # Verify current state matches expected value (conflict detection)
    effective_records = load_effective_transactions(data_path, changes_path)
    current_tx = next((tx for tx in effective_records if tx.get("id") == tx_id), None)
    if not current_tx:
        return {
            "status": "not_found",
            "transaction_id": tx_id,
            "message": f"Transaction '{tx_id}' no longer exists.",
        }

    if (
        current_tx.get("category_hint") != assigned_category
        or current_tx.get("last_action_id") != action_id
    ):
        return {
            "status": "state_conflict",
            "action_id": action_id,
            "transaction_id": tx_id,
            "current_category": current_tx.get("category_hint"),
            "expected_category": assigned_category,
            "message": (
                f"Cannot undo action '{action_id}': category was "
                "modified by a newer action. "
                f"Current category is '{current_tx.get('category_hint')}', "
                f"expected '{assigned_category}' from action '{action_id}'."
            ),
        }

    reverted_to = target_event["payload"]["previous_category"]
    timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    undo_action_id = f"act_undo_{timestamp_str}_{uuid.uuid4().hex[:6]}"

    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "action_id": undo_action_id,
        "action_type": "undo_category",
        "target_action_id": action_id,
        "transaction_id": tx_id,
        "payload": {
            "reverted_to": reverted_to,
            "reason": reason,
        },
    }
    append_change_event(event, changes_path)

    return {
        "status": "success",
        "action_id": undo_action_id,
        "target_action_id": action_id,
        "transaction_id": tx_id,
        "reverted_to": reverted_to,
        "message": (
            f"Reverted category for transaction '{tx_id}' back to '{reverted_to}'."
        ),
    }


# ---------------------------------------------------------------------------
# 3. Sensitive Tool: delete_flagged_transaction
# ---------------------------------------------------------------------------


@_ledger_locked
def delete_flagged_transaction(
    transaction_id: str,
    reason: str = "",
    confirmed: bool = False,
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> dict[str, Any]:
    """Logically delete a confirmed fraudulent or duplicate transaction.

    Enforces human-in-the-loop confirmation:
    If confirmed=False, halts execution and returns status 'confirmation_required'
    along with an action_id. No data modification occurs until confirmed=True.
    """
    if not confirmed:
        pending_id = f"pending_del_{transaction_id}"
        return {
            "status": "confirmation_required",
            "action_id": pending_id,
            "transaction_id": transaction_id,
            "reason": reason,
            "message": (
                f"Deletion of transaction '{transaction_id}' requires explicit "
                "user confirmation. Action halted until confirmed."
            ),
        }

    # Action confirmed: verify transaction existence
    records = load_effective_transactions(data_path, changes_path)
    target = next((tx for tx in records if tx.get("id") == transaction_id), None)
    if not target:
        return {
            "status": "not_found",
            "transaction_id": transaction_id,
            "message": f"Transaction '{transaction_id}' not found or already deleted.",
        }

    timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    action_id = f"act_del_{timestamp_str}_{uuid.uuid4().hex[:6]}"

    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "action_id": action_id,
        "action_type": "delete_transaction",
        "transaction_id": transaction_id,
        "payload": {
            "reason": reason,
            "confirmed": True,
        },
    }
    append_change_event(event, changes_path)

    return {
        "status": "success",
        "action_id": action_id,
        "transaction_id": transaction_id,
        "message": f"Transaction '{transaction_id}' has been logically deleted.",
    }


# ---------------------------------------------------------------------------
# OpenAI Tool Schemas (Single Source of Truth generated from Pydantic)
# ---------------------------------------------------------------------------


def _pydantic_to_openai_tool(
    name: str,
    description: str,
    model_cls: type[ToolArgsModel],
) -> dict[str, Any]:
    """Generate standard OpenAI function declaration from a Pydantic model."""
    schema = model_cls.model_json_schema()
    parameters = {
        "type": "object",
        "properties": schema.get("properties", {}),
        "required": schema.get("required", []),
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


TOOL_DEFINITIONS = [
    _pydantic_to_openai_tool(
        name="search_transactions",
        description=(
            "Search and filter household bank transactions (read-only). "
            "Filter by query text, merchant, category, and min/max amount."
        ),
        model_cls=SearchTransactionsArgs,
    ),
    _pydantic_to_openai_tool(
        name="get_transactions",
        description=(
            "Fetch a list of household bank transactions with optional merchant filter."
        ),
        model_cls=SearchTransactionsArgs,
    ),
    _pydantic_to_openai_tool(
        name="get_transaction_details",
        description=(
            "Get complete details for a specific transaction by its ID (read-only)."
        ),
        model_cls=GetTransactionDetailsArgs,
    ),
    _pydantic_to_openai_tool(
        name="compute_budget_baseline",
        description=(
            "Compute historical spending statistics (average, min, max, count) "
            "for a given merchant or category (read-only)."
        ),
        model_cls=ComputeBudgetBaselineArgs,
    ),
    _pydantic_to_openai_tool(
        name="find_duplicate_candidates",
        description=(
            "Scan for suspected duplicate transactions within 3 days (read-only)."
        ),
        model_cls=FindDuplicateCandidatesArgs,
    ),
    _pydantic_to_openai_tool(
        name="assign_category",
        description=(
            "Assign or update the budget category of a transaction (reversible write). "
            "Returns 'already_assigned' if category is already set unless "
            "force_override=True."
        ),
        model_cls=AssignCategoryArgs,
    ),
    _pydantic_to_openai_tool(
        name="undo_category_assignment",
        description=(
            "Revert a previous category assignment using its action_id "
            "(reversible write undo)."
        ),
        model_cls=UndoCategoryAssignmentArgs,
    ),
    _pydantic_to_openai_tool(
        name="delete_flagged_transaction",
        description=(
            "Logically delete a flagged duplicate or fraudulent transaction "
            "(sensitive action). Requires user confirmation (confirmed=True)."
        ),
        model_cls=DeleteTransactionArgs,
    ),
]
