"""English presentation catalog and safe error factories.

No raw exceptions or controller message/error strings are accepted here.
Text remains unescaped plain text; a later UI must render it with textContent.
Catalog values describe observations without claiming every success is a write.
"""

from http import HTTPStatus
from types import MappingProxyType
from typing import get_args

from agent_project.models import ToolStatus
from agent_project.web.schemas import (
    ApprovalKind,
    ApprovalStatus,
    ErrorResponse,
    HttpErrorCode,
    HttpErrorDetail,
    RunError,
    RunErrorCode,
    RunStatus,
)

RUN_LABELS = MappingProxyType(
    {
        "running": "Running",
        "awaiting_approval": "Waiting for your decision",
        "completed": "Completed",
        "step_limit_reached": "Step limit reached",
        "failed": "Failed",
        "outcome_unknown": "Outcome unknown",
    }
)
APPROVAL_LABELS = MappingProxyType(
    {
        "pending": "Waiting for your decision",
        "approved": "Approval received; execution is not yet confirmed",
        "rejected": "Rejected",
        "expired": "Expired",
        "stale": "Transaction changed; a new decision is required",
        "executing": "Executing",
        "completed": "Decision processed; check the tool result",
        "failed": "Failed",
        "outcome_unknown": "Outcome unknown",
    }
)
TOOL_MESSAGES = MappingProxyType(
    {
        "success": "The tool completed successfully. Check its result for details.",
        "invalid_arguments": "The tool arguments are invalid. No change was confirmed.",
        "not_found": "The transaction was not found.",
        "already_assigned": "This transaction already has a category.",
        "confirmation_required": (
            "Your confirmation is required. The proposed action has not been performed."
        ),
        "confirmation_denied": (
            "Confirmation was denied. The proposed action was not performed."
        ),
        "state_conflict": "The transaction state has changed. Review the current data.",
        "state_busy": "The transaction state is busy. No operation was started.",
        "action_not_found": "The requested action was not found.",
        "already_reverted": "This action has already been reverted.",
        "unknown_tool": "The requested tool is not supported.",
        "error": (
            "The tool encountered an error. "
            "Review the current state before trying again."
        ),
    }
)
UI_TEXT = MappingProxyType(
    {
        "title": "Household Expense Checker",
        "goal_label": "What would you like to review?",
        "goal_placeholder": "Review my expenses and look for duplicate charges.",
        "run_analysis": "Run analysis",
        "transactions": "Transactions",
        "search": "Search",
        "merchant": "Merchant",
        "amount": "Amount",
        "currency": "Currency",
        "category": "Category",
        "date": "Date",
        "transaction_id": "Transaction ID",
        "run_id": "Run ID",
        "tool": "Tool",
        "status": "Status",
        "action_id": "Action ID",
        "result": "Result",
        "reason": "Reason",
        "expires_at": "Expires at",
        "previous_category": "Previous category",
        "proposed_category": "Proposed category",
        "uncategorized": "Uncategorized",
        "no_results": "No transactions found.",
        "truncated_results": "Showing up to {limit} results. Refine your search.",
        "reject": "Reject",
        "confirm_deletion": "Confirm deletion",
        "confirm_overwrite": "Confirm overwrite",
        "pending_action": "This action has not been performed.",
        "empty_goal": "Enter a goal containing at least one non-whitespace character.",
        "goal_too_long": "Use no more than 8000 characters.",
        "connection_lost": (
            "Connection lost. Check the run status before starting another analysis."
        ),
        "outcome_unknown": (
            "The write outcome could not be confirmed. Check the transactions; "
            "do not resend the action automatically."
        ),
        "step_limit_reached": (
            "The step limit was reached. Available results are preserved."
        ),
    }
)
CONFIRMATION_QUESTIONS = MappingProxyType(
    {
        "delete_transaction": (
            "Delete this transaction? Review the details before confirming."
        ),
        "overwrite_category": (
            "Overwrite this transaction's category? "
            "Review both values before confirming."
        ),
    }
)

# Only transport failures belong here. Tool statuses are NOT HTTP status codes.
HTTP_ERRORS = MappingProxyType(
    {
        "resource_unavailable": (
            HTTPStatus.NOT_FOUND,
            "The requested resource is unavailable.",
        ),
        "method_not_allowed": (
            HTTPStatus.METHOD_NOT_ALLOWED,
            "This HTTP method is not allowed.",
        ),
        "invalid_request": (
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "The request is invalid. Check the supplied fields.",
        ),
        "forbidden": (HTTPStatus.FORBIDDEN, "This request is not authorized."),
        "run_unavailable": (
            HTTPStatus.NOT_FOUND,
            "This run is unavailable. Check the current transactions; "
            "do not resend it automatically.",
        ),
        "transaction_unavailable": (
            HTTPStatus.NOT_FOUND,
            "The transaction was not found.",
        ),
        "approval_unavailable": (HTTPStatus.NOT_FOUND, "This decision is unavailable."),
        "run_in_progress": (
            HTTPStatus.CONFLICT,
            "Another analysis is already in progress.",
        ),
        "request_id_conflict": (
            HTTPStatus.CONFLICT,
            "This request ID was already used for a different goal.",
        ),
        "instance_changed": (
            HTTPStatus.CONFLICT,
            "The server has restarted. Refresh the page and check the current state.",
        ),
        "approval_conflict": (
            HTTPStatus.CONFLICT,
            "This decision is no longer valid. Review the current state.",
        ),
        "state_busy": (
            HTTPStatus.SERVICE_UNAVAILABLE,
            "The transaction state is busy. Check its status before trying again.",
        ),
        "internal_error": (
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "An internal error occurred. Check the current state before trying again.",
        ),
    }
)
RUN_ERRORS = MappingProxyType(
    {
        "model_unavailable": (
            "Local model is unavailable. "
            "Check its configuration before starting a new analysis."
        ),
        "model_timeout": (
            "The local model timed out. Check the run before starting a new analysis."
        ),
        "data_unavailable": (
            "Transaction data is unavailable. "
            "Check the data before starting a new analysis."
        ),
        "outcome_unknown": (
            "The write outcome could not be confirmed. Check the current transactions; "
            "do not repeat the write automatically."
        ),
        "internal_error": (
            "The analysis failed. "
            "Check the current state before starting a new analysis."
        ),
    }
)
ACCEPTED_STATUS = HTTPStatus.ACCEPTED
TERMINAL_RUN_STATUSES = frozenset(
    {
        "completed",
        "step_limit_reached",
        "failed",
        "outcome_unknown",
    }
)


def run_label(status: RunStatus) -> str:
    return RUN_LABELS.get(status, "Unknown run status")


def approval_label(status: ApprovalStatus) -> str:
    return APPROVAL_LABELS.get(status, "Unknown decision status")


def tool_message(status: ToolStatus) -> str:
    return TOOL_MESSAGES.get(status, "Unknown tool result. Check the current state.")


def confirmation_question(kind: ApprovalKind) -> str:
    # Unknown action kinds must not result in a generic confirmation prompt.
    return CONFIRMATION_QUESTIONS[kind]


def http_error(
    code: HttpErrorCode, *, correlation_id: str | None = None
) -> tuple[HTTPStatus, ErrorResponse]:
    """Build an allowlisted public error, never using exception text or inputs."""
    status, message = HTTP_ERRORS[code]
    return status, ErrorResponse(
        error=HttpErrorDetail(code=code, message=message, correlation_id=correlation_id)
    )


def run_error(code: RunErrorCode, *, correlation_id: str | None = None) -> RunError:
    """A failed model call updates a run, not the HTTP status of its snapshot."""
    return RunError(code=code, message=RUN_ERRORS[code], correlation_id=correlation_id)


def catalog_is_complete() -> bool:
    """Explicit check useful for tests without import-time side effects."""
    return (
        set(RUN_LABELS) == set(get_args(RunStatus))
        and set(APPROVAL_LABELS) == set(get_args(ApprovalStatus))
        and set(TOOL_MESSAGES) == set(get_args(ToolStatus))
        and set(HTTP_ERRORS) == set(get_args(HttpErrorCode))
        and set(RUN_ERRORS) == set(get_args(RunErrorCode))
        and set(CONFIRMATION_QUESTIONS) == set(get_args(ApprovalKind))
    )
