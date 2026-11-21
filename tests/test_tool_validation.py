import pytest
from pydantic import ValidationError

from agent_project.agent import execute_controlled_tool
from agent_project.models import (
    AssignCategoryArgs,
    ComputeBudgetBaselineArgs,
    DeleteTransactionArgs,
    SearchTransactionsArgs,
    ToolResult,
    UndoCategoryAssignmentArgs,
)


def test_search_transactions_args_valid() -> None:
    args = SearchTransactionsArgs(
        query="Biedronka",
        merchant="Biedronka",
        min_amount=10.0,
        limit=20,
    )
    assert args.merchant == "Biedronka"
    assert args.min_amount == 10.0
    assert args.limit == 20


def test_search_transactions_args_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        SearchTransactionsArgs.model_validate(
            {"merchant": "Biedronka", "unknown_field": "injected"}
        )


def test_search_transactions_args_validates_limits() -> None:
    with pytest.raises(ValidationError):
        SearchTransactionsArgs(limit=100)  # max 50

    with pytest.raises(ValidationError):
        SearchTransactionsArgs(min_amount=-5.0)  # ge 0.0


def test_assign_category_args_validation() -> None:
    valid = AssignCategoryArgs(
        transaction_id="tx_101",
        category="Groceries",
        rationale="Supermarket",
        force_override=True,
    )
    assert valid.transaction_id == "tx_101"
    assert valid.force_override is True

    # Empty transaction_id not allowed
    with pytest.raises(ValidationError):
        AssignCategoryArgs(transaction_id="", category="Groceries")


def test_undo_category_assignment_args_validation() -> None:
    valid = UndoCategoryAssignmentArgs(action_id="act_123")
    assert valid.action_id == "act_123"
    assert valid.reason == "User requested undo"

    with pytest.raises(ValidationError):
        UndoCategoryAssignmentArgs(action_id="")


def test_delete_transaction_args_validation() -> None:
    valid = DeleteTransactionArgs(
        transaction_id="tx_104",
        reason="Duplicate of tx_103",
        confirmed=False,
    )
    assert valid.transaction_id == "tx_104"
    assert valid.confirmed is False

    with pytest.raises(ValidationError):
        DeleteTransactionArgs.model_validate(
            {"transaction_id": "tx_104", "extra_token": "hack"}
        )


def test_compute_budget_baseline_args() -> None:
    args = ComputeBudgetBaselineArgs(merchant="PGE", category_hint="Utilities")
    assert args.merchant == "PGE"


def test_tool_result_serialization() -> None:
    res = ToolResult(
        status="success",
        message="Action completed",
        action_id="act_999",
        data={"tx_id": "tx_101"},
    )
    d = res.to_dict()
    assert d["status"] == "success"
    assert d["action_id"] == "act_999"
    assert "error" not in d  # None fields omitted


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": -1},
        {"limit": 51},
        {"limit": "duzo"},
        {"limit": True},
        {"min_amount": -0.01},
        {"max_amount": -0.01},
        {"max_amount": "100"},
        {"merchant": 123},
        {"query": []},
        {"category": {}},
    ],
)
def test_controller_rejects_bad_search_arguments_without_side_effects(
    arguments,
    controlled_dataset,
):
    data_path, changes_path = controlled_dataset
    result = execute_controlled_tool(
        "search_transactions", arguments, data_path=data_path, changes_path=changes_path
    )
    assert result["status"] == "invalid_arguments"
    assert not changes_path.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 1},
        {"limit": 50},
        {"min_amount": 0},
        {"max_amount": 0},
    ],
)
def test_controller_accepts_search_boundary_values(arguments, controlled_dataset):
    data_path, changes_path = controlled_dataset
    result = execute_controlled_tool(
        "search_transactions", arguments, data_path=data_path, changes_path=changes_path
    )
    assert result["status"] == "success"
    assert not changes_path.exists()


@pytest.mark.parametrize(
    "name", ["unknown_tool", "os.system", "", "Search_Transactions"]
)
def test_controller_rejects_unknown_tool_names(name, controlled_dataset):
    data_path, changes_path = controlled_dataset
    result = execute_controlled_tool(
        name, {}, data_path=data_path, changes_path=changes_path
    )
    assert result["status"] == "unknown_tool"
    assert not changes_path.exists()
