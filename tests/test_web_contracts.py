"""T-03: DTOs and trusted English presentation, without HTTP or model calls."""

import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import get_args

import pytest
from pydantic import ValidationError

from agent_project.models import ToolStatus
from agent_project.web import create_app
from agent_project.web.presentation import (
    ACCEPTED_STATUS,
    APPROVAL_LABELS,
    CONFIRMATION_QUESTIONS,
    HTTP_ERRORS,
    RUN_ERRORS,
    RUN_LABELS,
    TERMINAL_RUN_STATUSES,
    TOOL_MESSAGES,
    UI_TEXT,
    approval_label,
    catalog_is_complete,
    confirmation_question,
    http_error,
    run_error,
    run_label,
    tool_message,
)
from agent_project.web.schemas import (
    ApprovalCard,
    ApprovalList,
    DecisionReceipt,
    DecisionRequest,
    ErrorResponse,
    HttpErrorDetail,
    ObservationData,
    RunError,
    RunReceipt,
    RunSnapshot,
    SessionResponse,
    StartRunRequest,
    ToolObservation,
    TransactionSummary,
)

NOW = datetime(2026, 10, 25, tzinfo=UTC)
REQUEST = {
    "goal": "Review my expenses.",
    "request_id": "req_1",
    "instance_id": "inst_1",
}
TRANSACTION = {
    "transaction_id": "tx_101",
    "date": "2026-10-25",
    "merchant": "Żabka",
    "amount": 12.5,
    "currency": "PLN",
}
OBSERVATION = {
    "sequence": 1,
    "step": 1,
    "tool": "assign_category",
    "status": "success",
}
CARD = {
    "approval_id": "approval_1",
    "run_id": "run_1",
    "tool_call_id": "call_1",
    "kind": "delete_transaction",
    "status": "pending",
    "transaction": TRANSACTION,
    "reason": "Possible duplicate.",
    "expires_at": NOW,
}
SNAPSHOT = {
    "run_id": "run_1",
    "status": "running",
    "version": 1,
    "goal": REQUEST["goal"],
    "created_at": NOW,
    "updated_at": NOW,
}
PUBLIC_CASES = [
    (StartRunRequest, REQUEST),
    (DecisionRequest, {"decision": "approve", "instance_id": "inst_1"}),
    (SessionResponse, {"csrf_token": "csrf_1", "instance_id": "inst_1"}),
    (RunReceipt, {"run_id": "run_1", "status": "running", "version": 1}),
    (TransactionSummary, TRANSACTION),
    (ObservationData, {"transaction_id": "tx_101", "count": 1}),
    (ToolObservation, OBSERVATION),
    (ApprovalCard, CARD),
    (ApprovalList, {}),
    (
        DecisionReceipt,
        {"approval_id": "approval_1", "run_id": "run_1", "status": "approved"},
    ),
    (HttpErrorDetail, {"code": "invalid_request", "message": "Invalid request."}),
    (
        ErrorResponse,
        {"error": {"code": "invalid_request", "message": "Invalid request."}},
    ),
    (RunError, {"code": "model_timeout", "message": "The model timed out."}),
    (RunSnapshot, SNAPSHOT),
]


@pytest.mark.parametrize("model,payload", PUBLIC_CASES)
def test_public_models_roundtrip_json_with_strict_types(model, payload):
    value = model.model_validate(payload)
    restored = model.model_validate_json(value.model_dump_json())
    assert restored == value


@pytest.mark.parametrize("model,payload", PUBLIC_CASES)
def test_all_public_models_reject_extra_fields(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "private_trace_path": "/private/trace.jsonl"})


@pytest.mark.parametrize(
    "goal", ["", " ", "\t\r\n", "\u2003\u00a0", "a" * 8001, 17, True, [], None]
)
def test_invalid_goals_are_rejected_in_python_and_json(goal):
    payload = {**REQUEST, "goal": goal}
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(payload)
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("goal", ["x", "  Review expenses.\n", "ą" * 8000])
def test_valid_goals_preserve_original_text_and_accept_exact_boundary(goal):
    request = StartRunRequest.model_validate_json(json.dumps({**REQUEST, "goal": goal}))
    assert request.goal == goal


@pytest.mark.parametrize("field", ["request_id", "instance_id"])
@pytest.mark.parametrize("value", ["", " ", "/private/file", 123, True, "x" * 129])
def test_request_identifiers_are_bounded_and_not_coerced(field, value):
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate({**REQUEST, field: value})


@pytest.mark.parametrize("decision", ["yes", "approved", "APPROVE", True, 1, None])
def test_decisions_require_explicit_literal_values(decision):
    with pytest.raises(ValidationError):
        DecisionRequest(decision=decision, instance_id="inst_1")


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_both_explicit_decisions_are_supported(decision):
    value = DecisionRequest.model_validate_json(
        json.dumps({"decision": decision, "instance_id": "inst_1"})
    )
    assert value.decision == decision


@pytest.mark.parametrize(
    "field",
    [
        "confirmed",
        "force_override",
        "tool",
        "transaction_id",
        "category",
        "goal",
        "owner",
        "fingerprint",
        "arguments",
        "csrf_token",
        "approval_id",
    ],
)
def test_decision_body_rejects_authority_and_target_substitution(field):
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(
            {
                "decision": "approve",
                "instance_id": "inst_1",
                field: True,
            }
        )


@pytest.mark.parametrize("value", [True, "1", 1.0, 0, -1])
def test_version_is_a_positive_strict_integer(value):
    with pytest.raises(ValidationError):
        RunReceipt(run_id="run_1", status="running", version=value)


@pytest.mark.parametrize("field", ["sequence", "step"])
@pytest.mark.parametrize("value", [True, "1", 1.0, 0])
def test_observation_order_is_strict_and_positive(field, value):
    with pytest.raises(ValidationError):
        ToolObservation.model_validate({**OBSERVATION, field: value})


@pytest.mark.parametrize("status", get_args(ToolStatus))
def test_all_domain_statuses_remain_observable_without_becoming_run_states(status):
    assert (
        ToolObservation.model_validate({**OBSERVATION, "status": status}).status
        == status
    )
    with pytest.raises(ValidationError):
        RunReceipt(run_id="run_1", status=status, version=1)
    assert tool_message(status) == TOOL_MESSAGES[status]


def test_unknown_tool_name_can_be_reported_without_claiming_execution():
    value = ToolObservation.model_validate(
        {
            **OBSERVATION,
            "tool": "nonexistent_tool",
            "status": "unknown_tool",
        }
    )
    assert value.tool == "nonexistent_tool"
    assert "not supported" in tool_message(value.status)


@pytest.mark.parametrize("status", ["approved", "success", 200, "200", True])
def test_run_status_does_not_accept_decision_tool_or_http_states(status):
    with pytest.raises(ValidationError):
        RunReceipt(run_id="run_1", status=status, version=1)


@pytest.mark.parametrize(
    "private_field", ["error", "message", "trace_path", "api_key", "arguments"]
)
def test_nested_observation_data_rejects_unreviewed_fields(private_field):
    with pytest.raises(ValidationError):
        ToolObservation.model_validate(
            {
                **OBSERVATION,
                "data": {private_field: "secret /Users/private/.env"},
            }
        )


@pytest.mark.parametrize("amount", [True, "12.50", float("inf"), float("nan")])
def test_amounts_are_numeric_finite_and_not_coerced(amount):
    with pytest.raises(ValidationError):
        TransactionSummary.model_validate({**TRANSACTION, "amount": amount})


def test_public_text_preserves_data_instead_of_translating_or_rendering_it():
    text = "<script>alert('test')</script> Żabka"
    transaction = TransactionSummary.model_validate({**TRANSACTION, "merchant": text})
    assert transaction.merchant == text
    assert json.loads(transaction.model_dump_json())["merchant"] == text
    assert TransactionSummary.model_validate(TRANSACTION).category is None
    assert UI_TEXT["uncategorized"] == "Uncategorized"


def test_timestamps_require_timezones_and_have_json_transport_support():
    with pytest.raises(ValidationError):
        ApprovalCard.model_validate({**CARD, "expires_at": datetime(2026, 10, 25)})
    card = ApprovalCard.model_validate(CARD)
    assert ApprovalCard.model_validate_json(card.model_dump_json()).expires_at == NOW


def test_snapshots_reject_internal_state_and_do_not_alias_observation_lists():
    first = RunSnapshot.model_validate(SNAPSHOT)
    second = RunSnapshot.model_validate(SNAPSHOT)
    assert first.observations is not second.observations
    for field in ["owner", "trace_path", "cookie", "api_key", "fingerprint"]:
        with pytest.raises(ValidationError):
            RunSnapshot.model_validate({**SNAPSHOT, field: "private"})
    with pytest.raises(ValidationError):
        RunSnapshot.model_validate({**SNAPSHOT, "goal": "   "})


def test_acceptance_is_not_a_claim_that_a_write_succeeded():
    receipt = DecisionReceipt(
        approval_id="approval_1", run_id="run_1", status="approved"
    )
    assert receipt.result is None
    assert "not yet confirmed" in approval_label(receipt.status)
    assert ACCEPTED_STATUS == 202
    assert "completed" in TERMINAL_RUN_STATUSES
    assert "outcome_unknown" in TERMINAL_RUN_STATUSES
    assert not {"running", "awaiting_approval"} & TERMINAL_RUN_STATUSES


@pytest.mark.parametrize("code", HTTP_ERRORS)
def test_safe_http_error_catalog_and_envelope(code):
    status, response = http_error(code, correlation_id="error_1")
    assert isinstance(status, HTTPStatus)
    assert status == HTTP_ERRORS[code][0]
    assert response.error.message == HTTP_ERRORS[code][1]
    assert response.error.code == code
    assert response.error.correlation_id == "error_1"
    assert set(json.loads(response.model_dump_json())) == {"error"}
    assert "traceback" not in response.model_dump_json().lower()


@pytest.mark.parametrize(
    "code,expected",
    [
        ("invalid_request", 422),
        ("forbidden", 403),
        ("run_unavailable", 404),
        ("run_in_progress", 409),
        ("request_id_conflict", 409),
        ("approval_conflict", 409),
        ("state_busy", 503),
        ("internal_error", 500),
    ],
)
def test_transport_status_codes_follow_design(code, expected):
    assert http_error(code)[0] == expected


@pytest.mark.parametrize("code", RUN_ERRORS)
def test_run_failures_use_fixed_messages_not_model_exception_text(code):
    error = run_error(code, correlation_id="error_2")
    assert error.code == code
    assert error.message == RUN_ERRORS[code]
    snapshot = RunSnapshot.model_validate(
        {**SNAPSHOT, "status": "failed", "error": error}
    )
    assert snapshot.error == error
    assert code not in HTTP_ERRORS or code == "internal_error"


def test_error_factories_cannot_accept_raw_exception_or_validation_input():
    secret = "SECRET_KEY /Users/private/.env"
    with pytest.raises(TypeError):
        http_error("internal_error", message=secret)
    with pytest.raises(TypeError):
        run_error("internal_error", exception=RuntimeError(secret))
    with pytest.raises(ValidationError):
        http_error("internal_error", correlation_id=secret)
    assert secret not in http_error("invalid_request")[1].model_dump_json()
    assert secret not in run_error("internal_error").model_dump_json()


def test_catalog_is_complete_english_and_read_only():
    assert catalog_is_complete()
    assert run_label("awaiting_approval") == "Waiting for your decision"
    assert UI_TEXT["confirm_deletion"] == "Confirm deletion"
    assert UI_TEXT["confirm_overwrite"] == "Confirm overwrite"
    assert "not performed" in tool_message("confirmation_denied")
    assert "not been performed" in tool_message("confirmation_required")
    assert "already has a category" in tool_message("already_assigned")
    assert "write" not in tool_message("success").lower()
    for catalog in [
        RUN_LABELS,
        APPROVAL_LABELS,
        TOOL_MESSAGES,
        UI_TEXT,
        CONFIRMATION_QUESTIONS,
        RUN_ERRORS,
    ]:
        assert all(isinstance(text, str) and text.strip() for text in catalog.values())
        with pytest.raises(TypeError):
            catalog["injected"] = "Replace trusted text"
    assert "Delete this transaction?" in confirmation_question("delete_transaction")
    assert "Review both values" in confirmation_question("overwrite_category")
    with pytest.raises(KeyError):
        confirmation_question("unknown_action")


def test_unknown_status_fallbacks_do_not_echo_untrusted_details():
    secret = "secret /Users/private/.env"
    assert secret not in tool_message(secret)
    assert secret not in run_label(secret)
    assert secret not in approval_label(secret)


def test_business_contracts_register_only_summary_endpoints():
    """No generic tool execution route exists; runs remain summary-only."""
    routes = {route.path for route in create_app().routes}
    assert routes == {
        "/api/session",
        "/api/runs",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/approvals",
        "/api/runs/{run_id}/approvals/{approval_id}",
        "/api/transactions",
        "/api/transactions/{transaction_id}",
        "/static",
        "/",
    }
