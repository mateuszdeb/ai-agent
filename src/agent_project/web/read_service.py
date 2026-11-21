"""Model-free read adapter for the web path (T-13).

The service reuses the existing deterministic controller for every read, so
the HTTP responses always describe the effective transaction state and keep
the existing search semantics. It never contacts a model, never writes and
never returns raw records: only allowlisted summary cards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_project.agent import execute_controlled_tool
from agent_project.web.schemas import TransactionDetails

DEFAULT_READ_LIMIT = 10
MAX_READ_LIMIT = 50
READ_QUERY_ALLOWLIST = frozenset({"query", "merchant", "limit"})


def parse_read_query(values: list[tuple[str, str]]) -> dict[str, str]:
    """Build the explicit read filter bag from raw HTTP query pairs.

    HTTP query strings are text by transport. Duplicates and unknown names
    fail closed instead of being silently ignored, and the ``limit`` value
    stays a raw string for the caller's explicit integer conversion; JSON
    argument validation elsewhere remains untouched and strict.
    """
    seen: dict[str, str] = {}
    for key, value in values:
        if key not in READ_QUERY_ALLOWLIST:
            raise ValueError(f"Unknown query parameter: {key!r}")
        if key in seen:
            raise ValueError(f"Duplicate query parameter: {key!r}")
        seen[key] = value
    return seen


def parse_limit(raw: str | None) -> int:
    """Convert the numeric query text into an integer for shared validation.

    A missing ``limit`` uses the shared default of the controller schema; an
    unparsable or out-of-bounds value reaches the controller unchanged in
    meaning and is rejected there like any other tool argument.
    """
    if raw is None:
        return DEFAULT_READ_LIMIT
    return int(raw)


class TransactionReadService:
    """Read-only web adapter bound to application-owned dataset paths."""

    def __init__(self, *, data_path: Path, changes_path: Path) -> None:
        self._data_path = Path(data_path)
        self._changes_path = Path(changes_path)

    def search(
        self,
        *,
        query: str | None = None,
        merchant: str | None = None,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> tuple[list[TransactionDetails], bool]:
        """Effective-state search cards plus the possibly_truncated flag.

        The flag is True exactly when the result count reaches the serving
        limit, so the UI can announce a bounded list without inventing an
        unknown total.
        """
        result = execute_controlled_tool(
            "search_transactions",
            {"query": query, "merchant": merchant, "limit": limit},
            data_path=self._data_path,
            changes_path=self._changes_path,
        )
        records = _unwrap_success(result)
        items = [transaction_card_from_record(record) for record in records]
        return items, len(items) == int(limit)

    def details(self, transaction_id: str) -> TransactionDetails | None:
        """One effective-state details card, or None for an unknown record."""
        result = execute_controlled_tool(
            "get_transaction_details",
            {"transaction_id": transaction_id},
            data_path=self._data_path,
            changes_path=self._changes_path,
        )
        if result.get("status") == "not_found":
            return None
        record = _unwrap_success(result)
        return transaction_card_from_record(record)


def _unwrap_success(result: dict[str, Any]) -> list[Any] | dict[str, Any]:
    """Unwrap one controller result or translate its failure status.

    ``invalid_arguments`` is a client error against the same strict schema;
    state errors stay catalog-known internal responses. Nothing is clamped.
    """
    status = result.get("status")
    if status == "success" and "data" in result:
        return result["data"]
    code = (
        "invalid_request"
        if status == "invalid_arguments"
        else "state_busy"
        if status == "state_busy"
        else "internal_error"
    )
    from agent_project.web.presentation import http_error

    raise ReadUnavailableError(code, http_error(code))


class ReadUnavailableError(Exception):
    """A read could not be served; carries the catalog response parts."""

    def __init__(self, code: str, response_parts: tuple[int, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.status, self.error = response_parts


def transaction_card_from_record(record: Any) -> TransactionDetails:
    """Build one allowlisted card from one controller record."""
    from agent_project.web.tool_executor import transaction_summary_from_record

    card = transaction_summary_from_record(record)
    description = record.get("description") if isinstance(record, dict) else None
    return TransactionDetails(
        transaction_id=card.transaction_id,
        date=card.date,
        merchant=card.merchant,
        amount=card.amount,
        currency=card.currency,
        category=card.category,
        description=(
            str(description) if isinstance(description, str) and description else None
        ),
    )
