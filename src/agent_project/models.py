"""Pydantic models and schemas for domain tools and state management.

Serves as the Single Source of Truth for:
1. Tool argument validation in runtime.
2. Generating OpenAI function-calling schemas (TOOL_DEFINITIONS).
3. MCP tool schemas.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Base Tool Argument Model
# ---------------------------------------------------------------------------


class ToolArgsModel(BaseModel):
    """Base model forbidding extra fields for strict argument validation."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# 1. Read-Only Tools
# ---------------------------------------------------------------------------


class SearchTransactionsArgs(ToolArgsModel):
    """Arguments for search_transactions tool."""

    query: str | None = Field(
        default=None,
        description="Text search query in merchant or description (case-insensitive).",
    )
    merchant: str | None = Field(
        default=None,
        description="Filter by merchant name substring, e.g. 'Spotify' or 'Biedronka'.",
    )
    category: str | None = Field(
        default=None,
        description=(
            "Filter by category (or category_hint), e.g. 'Groceries', 'Utilities'."
        ),
    )
    min_amount: float | None = Field(
        default=None,
        ge=0.0,
        description="Minimum transaction amount in PLN.",
    )
    max_amount: float | None = Field(
        default=None,
        ge=0.0,
        description="Maximum transaction amount in PLN.",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of transactions to return (1-50).",
    )


class GetTransactionDetailsArgs(ToolArgsModel):
    """Arguments for get_transaction_details tool."""

    transaction_id: str = Field(
        min_length=1,
        description="Unique transaction ID to inspect, e.g. 'tx_105'.",
    )


class ComputeBudgetBaselineArgs(ToolArgsModel):
    """Arguments for compute_budget_baseline tool."""

    merchant: str | None = Field(
        default=None,
        description="Merchant name substring to compute spending statistics for.",
    )
    category_hint: str | None = Field(
        default=None,
        description="Category name to compute spending statistics for.",
    )


class FindDuplicateCandidatesArgs(ToolArgsModel):
    """Arguments for find_duplicate_candidates tool (takes no arguments)."""


# ---------------------------------------------------------------------------
# 2. Reversible Write Tools
# ---------------------------------------------------------------------------


class AssignCategoryArgs(ToolArgsModel):
    """Arguments for assign_category tool (reversible write)."""

    transaction_id: str = Field(
        min_length=1,
        description="The ID of the transaction to categorize, e.g. 'tx_101'.",
    )
    category: str = Field(
        min_length=1,
        description=("The budget category to assign (e.g. 'Groceries', 'Utilities')."),
    )
    rationale: str = Field(
        default="",
        description="Brief justification or context for assigning this category.",
    )
    force_override: bool = Field(
        default=False,
        description=(
            "If True, overwrites an existing category assignment. "
            "If False, returns 'already_assigned' when category is already set."
        ),
    )


class UndoCategoryAssignmentArgs(ToolArgsModel):
    """Arguments for undo_category_assignment tool (reverts a previous write)."""

    action_id: str = Field(
        min_length=1,
        description=(
            "The unique action_id returned by assign_category that you wish to revert."
        ),
    )
    reason: str = Field(
        default="User requested undo",
        description="Reason for reverting the category assignment.",
    )


# ---------------------------------------------------------------------------
# 3. Sensitive Tools
# ---------------------------------------------------------------------------


class DeleteTransactionArgs(ToolArgsModel):
    """Arguments for delete_flagged_transaction tool (sensitive action)."""

    transaction_id: str = Field(
        min_length=1,
        description=(
            "ID of the flagged transaction to logically delete (e.g. 'tx_104')."
        ),
    )
    reason: str = Field(
        default="",
        description="Reason for deletion, e.g. 'Duplicate charge of tx_103'.",
    )
    confirmed: bool = Field(
        default=False,
        description=(
            "Must be True to execute deletion. The Python application controller "
            "intercepts this call and requires real user confirmation before setting "
            "confirmed=True."
        ),
    )


# ---------------------------------------------------------------------------
# Structured Result Types
# ---------------------------------------------------------------------------

ToolStatus = Literal[
    "success",
    "invalid_arguments",
    "not_found",
    "already_assigned",
    "confirmation_required",
    "confirmation_denied",
    "state_conflict",
    "state_busy",
    "action_not_found",
    "already_reverted",
    "unknown_tool",
    "error",
]


class ToolResult(BaseModel):
    """Uniform structured result returned by every controlled tool execution."""

    status: ToolStatus
    message: str | None = None
    data: Any = None
    action_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary without None fields for clean JSON output."""
        return {k: v for k, v in self.model_dump().items() if v is not None}
