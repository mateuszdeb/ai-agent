import json
from pathlib import Path

import pytest

from agent_project.agent import execute_controlled_tool
from agent_project.tools import (
    assign_category,
    delete_flagged_transaction,
    load_effective_transactions,
    load_raw_transactions,
    search_transactions,
    undo_category_assignment,
)


@pytest.mark.parametrize("category", ["Groceries", "Travel"])
def test_existing_category_cannot_be_overwritten_without_force(
    category,
    controlled_dataset,
):
    data_path, changes_path = controlled_dataset
    result = execute_controlled_tool(
        "assign_category",
        {"transaction_id": "tx_categorized", "category": category},
        data_path=data_path,
        changes_path=changes_path,
    )
    assert result["status"] == "already_assigned"
    assert result["current_category"] == "Groceries"
    assert not changes_path.exists()


@pytest.mark.parametrize(
    "newer_categories",
    [
        ["Travel"],
        ["Electronics"],
        ["Travel", "Electronics"],
    ],
)
def test_undo_detects_newer_actions_even_when_category_matches(
    newer_categories,
    controlled_dataset,
):
    data_path, changes_path = controlled_dataset
    first = assign_category(
        "tx_pending", "Electronics", data_path=data_path, changes_path=changes_path
    )
    for category in newer_categories:
        newer = assign_category(
            "tx_pending",
            category,
            force_override=True,
            data_path=data_path,
            changes_path=changes_path,
        )
        assert newer["status"] == "success"
    snapshot = changes_path.read_bytes()
    result = undo_category_assignment(
        first["action_id"],
        data_path=data_path,
        changes_path=changes_path,
    )
    assert result["status"] == "state_conflict"
    assert changes_path.read_bytes() == snapshot


def test_undo_restores_category_with_unrelated_newer_event(controlled_dataset):
    data_path, changes_path = controlled_dataset
    first = assign_category(
        "tx_pending",
        "Electronics",
        data_path=data_path,
        changes_path=changes_path,
    )
    assign_category(
        "tx_categorized",
        "Other",
        force_override=True,
        data_path=data_path,
        changes_path=changes_path,
    )
    before_undo = changes_path.read_bytes()
    result = execute_controlled_tool(
        "undo_category_assignment",
        {"action_id": first["action_id"]},
        data_path=data_path,
        changes_path=changes_path,
    )
    assert result["status"] == "success"
    assert changes_path.read_bytes().startswith(before_undo)
    by_id = {
        tx["id"]: tx for tx in load_effective_transactions(data_path, changes_path)
    }
    assert by_id["tx_pending"]["category_hint"] is None
    assert by_id["tx_categorized"]["category_hint"] == "Other"
    snapshot = changes_path.read_bytes()
    repeated = execute_controlled_tool(
        "undo_category_assignment",
        {"action_id": first["action_id"]},
        data_path=data_path,
        changes_path=changes_path,
    )
    assert repeated["status"] == "already_reverted"
    assert changes_path.read_bytes() == snapshot


def test_unknown_undo_action_does_not_create_ledger(controlled_dataset):
    data_path, changes_path = controlled_dataset
    result = execute_controlled_tool(
        "undo_category_assignment",
        {"action_id": "missing"},
        data_path=data_path,
        changes_path=changes_path,
    )
    assert result["status"] == "action_not_found"
    assert not changes_path.exists()


@pytest.mark.parametrize(
    ("approval", "status"),
    [
        (None, "confirmation_required"),
        (False, "confirmation_denied"),
        (True, "success"),
    ],
)
def test_sensitive_action_requires_real_application_decision(
    approval,
    status,
    controlled_dataset,
):
    data_path, changes_path = controlled_dataset
    assign_category(
        "tx_pending",
        "Other",
        data_path=data_path,
        changes_path=changes_path,
    )
    previous_events = changes_path.read_bytes()
    decisions = []

    def confirm(action_id, arguments):
        decisions.append((action_id, arguments))
        return approval

    result = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_dup_b", "confirmed": True, "reason": "Duplicate"},
        data_path=data_path,
        changes_path=changes_path,
        confirmation_handler=confirm if approval is not None else None,
    )
    assert result["status"] == status
    remaining = {
        tx["id"] for tx in load_effective_transactions(data_path, changes_path)
    }
    assert ("tx_dup_b" in remaining) == (approval is not True)
    if approval is True:
        assert changes_path.read_bytes().startswith(previous_events)
        events = [json.loads(line) for line in changes_path.read_text().splitlines()]
        assert len(events) == 2
        assert events[-1]["action_type"] == "delete_transaction"
        assert events[-1]["action_id"] == result["action_id"]
        assert events[-1]["payload"]["reason"] == "Duplicate"
    else:
        assert changes_path.read_bytes() == previous_events
    if approval is not None:
        assert decisions == [
            (
                "pending_del_tx_dup_b",
                {
                    "transaction_id": "tx_dup_b",
                    "reason": "Duplicate",
                    "confirmed": False,
                },
            )
        ]


def test_transactions_json_remains_unmutated(tmp_path: Path) -> None:
    """Baseline transactions.json must NEVER be modified by any tool."""
    base_file = tmp_path / "transactions.json"
    changes_file = tmp_path / "changes.jsonl"

    original_content = (
        Path(__file__).resolve().parent.parent / "data" / "transactions.json"
    ).read_text(encoding="utf-8")
    base_file.write_text(original_content, encoding="utf-8")

    # Run mutations
    assign_category(
        transaction_id="tx_101",
        category="NewCategory",
        data_path=base_file,
        changes_path=changes_file,
    )
    delete_flagged_transaction(
        transaction_id="tx_102",
        confirmed=True,
        data_path=base_file,
        changes_path=changes_file,
    )

    # Baseline file content MUST remain 100% identical
    assert base_file.read_text(encoding="utf-8") == original_content


def test_search_transactions_filters(tmp_path: Path) -> None:
    res_merchant = search_transactions(merchant="Biedronka")
    assert len(res_merchant) >= 2
    assert all("Biedronka" in tx["merchant"] for tx in res_merchant)

    res_amount = search_transactions(min_amount=200.0, max_amount=300.0)
    assert len(res_amount) > 0
    assert all(200.0 <= tx["amount"] <= 300.0 for tx in res_amount)

    res_query = search_transactions(query="spotify")
    assert len(res_query) >= 2

    res_category = search_transactions(category="Groceries")
    assert len(res_category) >= 2


def test_assign_category_not_found(tmp_path: Path) -> None:
    res = assign_category(
        transaction_id="tx_nonexistent_999",
        category="Groceries",
        changes_path=tmp_path / "changes.jsonl",
    )
    assert res["status"] == "not_found"


def test_assign_category_already_assigned(tmp_path: Path) -> None:
    changes_file = tmp_path / "changes.jsonl"
    # tx_101 already has category_hint = "Groceries"
    res = assign_category(
        transaction_id="tx_101",
        category="Groceries",
        force_override=False,
        changes_path=changes_file,
    )
    assert res["status"] == "already_assigned"
    assert res["current_category"] == "Groceries"
    assert not changes_file.exists()


def test_assign_category_success_and_force_override(tmp_path: Path) -> None:
    changes_file = tmp_path / "changes.jsonl"

    # Assign category to tx_105 (which initially has null category)
    res = assign_category(
        transaction_id="tx_105",
        category="Electronics",
        rationale="TV purchase",
        changes_path=changes_file,
    )
    assert res["status"] == "success"
    assert res["category"] == "Electronics"
    action_id = res["action_id"]

    # Verify effective state
    effective = load_effective_transactions(changes_path=changes_file)
    tx_105 = next(tx for tx in effective if tx["id"] == "tx_105")
    assert tx_105["category_hint"] == "Electronics"

    # Override without force fails
    res_fail = assign_category(
        transaction_id="tx_105",
        category="Electronics",
        force_override=False,
        changes_path=changes_file,
    )
    assert res_fail["status"] == "already_assigned"

    # Override with force succeeds
    res_override = assign_category(
        transaction_id="tx_105",
        category="Home Equipment",
        force_override=True,
        changes_path=changes_file,
    )
    assert res_override["status"] == "success"
    assert res_override["category"] == "Home Equipment"
    assert res_override["action_id"] != action_id


def test_undo_category_assignment(tmp_path: Path) -> None:
    changes_file = tmp_path / "changes.jsonl"

    # Assign category
    assign_res = assign_category(
        transaction_id="tx_105",
        category="Electronics",
        changes_path=changes_file,
    )
    action_id = assign_res["action_id"]

    # Undo
    undo_res = undo_category_assignment(
        action_id=action_id,
        changes_path=changes_file,
    )
    assert undo_res["status"] == "success"
    assert undo_res["reverted_to"] is None

    # Verify state reverted
    effective = load_effective_transactions(changes_path=changes_file)
    tx_105 = next(tx for tx in effective if tx["id"] == "tx_105")
    assert tx_105.get("category_hint") is None

    # Undoing already reverted action returns already_reverted
    undo_again = undo_category_assignment(
        action_id=action_id,
        changes_path=changes_file,
    )
    assert undo_again["status"] == "already_reverted"


def test_undo_category_state_conflict(tmp_path: Path) -> None:
    changes_file = tmp_path / "changes.jsonl"

    # Action 1: assign Electronics
    res1 = assign_category(
        transaction_id="tx_105",
        category="Electronics",
        changes_path=changes_file,
    )
    action_1 = res1["action_id"]

    # Action 2: overwrite with Appliances
    assign_category(
        transaction_id="tx_105",
        category="Appliances",
        force_override=True,
        changes_path=changes_file,
    )

    # Trying to undo Action 1 now triggers a state conflict!
    undo_conflict = undo_category_assignment(
        action_id=action_1,
        changes_path=changes_file,
    )
    assert undo_conflict["status"] == "state_conflict"


def test_delete_flagged_transaction_confirmation_required(tmp_path: Path) -> None:
    changes_file = tmp_path / "changes.jsonl"

    # Sensitive action without confirmation halts
    res = delete_flagged_transaction(
        transaction_id="tx_104",
        reason="Duplicate Spotify charge",
        confirmed=False,
        changes_path=changes_file,
    )
    assert res["status"] == "confirmation_required"
    assert "pending_del_tx_104" in res["action_id"]
    # No write was performed
    assert not changes_file.exists()


def test_delete_flagged_transaction_confirmed_success(tmp_path: Path) -> None:
    changes_file = tmp_path / "changes.jsonl"

    # Confirmed sensitive action executes
    res = delete_flagged_transaction(
        transaction_id="tx_104",
        reason="Duplicate Spotify charge",
        confirmed=True,
        changes_path=changes_file,
    )
    assert res["status"] == "success"

    # Effective transactions no longer includes tx_104
    effective = load_effective_transactions(changes_path=changes_file)
    assert not any(tx["id"] == "tx_104" for tx in effective)

    # Raw transactions still contains tx_104
    raw = load_raw_transactions()
    assert any(tx["id"] == "tx_104" for tx in raw)

    # Deleting again returns not_found
    res_del_again = delete_flagged_transaction(
        transaction_id="tx_104",
        confirmed=True,
        changes_path=changes_file,
    )
    assert res_del_again["status"] == "not_found"
