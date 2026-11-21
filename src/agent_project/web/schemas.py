"""Strict public DTOs, not session storage or executable authorization.

Internal owners, cookies, fingerprints, tool arguments and trace paths must never
be added to these public models. HTTP status, run state and domain tool status
are intentionally separate vocabularies. JSON uses strict types; HTTP query
parsing belongs to the future read adapter, not to these request models.
"""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from agent_project.models import ToolStatus

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Goal = Annotated[str, StringConstraints(min_length=1, max_length=8000, pattern=r"\S")]
NonBlankText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
RunStatus = Literal[
    "running",
    "awaiting_approval",
    "completed",
    "step_limit_reached",
    "failed",
    "outcome_unknown",
]
ApprovalKind = Literal["delete_transaction", "overwrite_category"]
ApprovalStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "expired",
    "stale",
    "executing",
    "completed",
    "failed",
    "outcome_unknown",
]
ToolName = Literal[
    "search_transactions",
    "get_transaction_details",
    "compute_budget_baseline",
    "find_duplicate_candidates",
    "assign_category",
    "undo_category_assignment",
    "delete_flagged_transaction",
]
HttpErrorCode = Literal[
    "resource_unavailable",
    "method_not_allowed",
    "invalid_request",
    "forbidden",
    "run_unavailable",
    "transaction_unavailable",
    "approval_unavailable",
    "run_in_progress",
    "request_id_conflict",
    "instance_changed",
    "approval_conflict",
    "state_busy",
    "internal_error",
]
RunErrorCode = Literal[
    "model_unavailable",
    "model_timeout",
    "data_unavailable",
    "outcome_unknown",
    "internal_error",
]


class PublicModel(BaseModel):
    """Reject coercion and unexpected fields, including nested DTO fields."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )


class StartRunRequest(PublicModel):
    goal: Goal
    request_id: Identifier
    instance_id: Identifier

    @field_validator("goal")
    @classmethod
    def require_nonblank_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "Enter a goal containing at least one non-whitespace character."
            )
        return value


class DecisionRequest(PublicModel):
    decision: Literal["approve", "reject"]
    instance_id: Identifier


class SessionResponse(PublicModel):
    """Bootstrap only; cookie/session owner is not a public identifier."""

    csrf_token: Identifier
    instance_id: Identifier
    active_run_id: Identifier | None = None


class RunReceipt(PublicModel):
    """202 means accepted, not that an analysis or write has succeeded."""

    run_id: Identifier
    status: RunStatus
    version: int = Field(ge=1)


class TransactionSummary(PublicModel):
    """Explicit allowlist for transaction information shown on decision cards."""

    transaction_id: Identifier
    date: NonBlankText
    merchant: NonBlankText
    amount: float
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    category: NonBlankText | None = None


class TransactionDetails(TransactionSummary):
    """One effective-state record card; no raw controller messages."""

    description: NonBlankText | None = None


class TransactionList(PublicModel):
    """Bounded effective-state list; a truncation hint, never a fake total."""

    items: list[TransactionDetails] = Field(default_factory=list)
    limit: int = Field(ge=1, le=50)
    possibly_truncated: bool


class ObservationData(PublicModel):
    """No arbitrary dictionaries, raw exceptions, paths or tool arguments."""

    transaction_id: Identifier | None = None
    category: NonBlankText | None = None
    previous_category: NonBlankText | None = None
    count: int | None = Field(default=None, ge=0)
    transaction: TransactionSummary | None = None


class ToolObservation(PublicModel):
    sequence: int = Field(ge=1)
    step: int = Field(ge=1)
    tool: NonBlankText
    status: ToolStatus
    action_id: Identifier | None = None
    data: ObservationData | None = None

    # Tool name is text rather than ToolName: unknown_tool must remain observable.
    # A status/message is not inferred from a model's declaration of success.


class ApprovalCard(PublicModel):
    """Presentation only: no owner, fingerprint or executable arguments."""

    approval_id: Identifier
    run_id: Identifier
    tool_call_id: Identifier
    kind: ApprovalKind
    status: ApprovalStatus
    action_id: Identifier | None = None
    transaction: TransactionSummary
    reason: str
    expires_at: AwareDatetime
    previous_category: NonBlankText | None = None
    proposed_category: NonBlankText | None = None


class ApprovalList(PublicModel):
    approvals: list[ApprovalCard] = Field(default_factory=list)


class DecisionReceipt(PublicModel):
    """Acknowledging approval is distinct from a verified controller result."""

    approval_id: Identifier
    run_id: Identifier
    status: ApprovalStatus
    result: ToolObservation | None = None


class HttpErrorDetail(PublicModel):
    code: HttpErrorCode
    message: NonBlankText
    correlation_id: Identifier | None = None


class ErrorResponse(PublicModel):
    error: HttpErrorDetail


class RunError(PublicModel):
    code: RunErrorCode
    message: NonBlankText
    correlation_id: Identifier | None = None


class RunSnapshot(PublicModel):
    run_id: Identifier
    status: RunStatus
    version: int = Field(ge=1)
    goal: Goal
    created_at: AwareDatetime
    updated_at: AwareDatetime
    result: str | None = None
    observations: list[ToolObservation] = Field(default_factory=list)
    approval_outcome: ApprovalStatus | None = None
    error: RunError | None = None

    # Do not expose the internal owner, trace path, model configuration or cookie.
