"""T-04: actual OS locks and domain transactions on isolated ledgers."""

import fcntl
import inspect
import json
import os
import select
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_project import state_lock, tools
from agent_project.agent import execute_controlled_tool
from agent_project.models import ToolResult
from agent_project.state_lock import (
    LedgerBusyError,
    LedgerCorruptionError,
    LedgerGuard,
    LedgerWriteError,
)


def test_same_instance_and_separate_instances_are_reentrant(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    guard = LedgerGuard(path, timeout=0)
    real_flock = fcntl.flock
    operations = []

    def record(fd, operation):
        operations.append(operation)
        return real_flock(fd, operation)

    monkeypatch.setattr(state_lock.fcntl, "flock", record)
    with guard:
        with guard, LedgerGuard(path, timeout=0):
            assert guard.lock_path.exists()
    assert operations == [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN]
    inode = guard.lock_path.stat().st_ino
    with guard:
        pass
    assert guard.lock_path.stat().st_ino == inode
    assert not path.exists()


@pytest.mark.parametrize("timeout", [-1, True, "1", float("nan"), float("inf")])
def test_invalid_timeouts_fail_before_touching_disk(tmp_path, timeout):
    with pytest.raises(ValueError):
        LedgerGuard(tmp_path / "ledger", timeout=timeout)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("alias_kind", ["dotdot", "symlink"])
def test_alias_paths_share_thread_lock_and_failed_acquire_can_be_retried(
    tmp_path, alias_kind
):
    folder = tmp_path / "sub"
    folder.mkdir()
    alias = folder / ".." / "ledger"
    real = tmp_path / "ledger"
    if alias_kind == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(real)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with LedgerGuard(real):
            future = executor.submit(_attempt_lock, alias, 0.03)
            assert future.result(timeout=3) == "busy"
        assert executor.submit(_attempt_lock, alias, 0).result(timeout=3) == "acquired"


def _attempt_lock(path, timeout):
    try:
        with LedgerGuard(path, timeout=timeout):
            return "acquired"
    except LedgerBusyError:
        return "busy"


def test_different_ledgers_do_not_block_each_other(tmp_path):
    with ThreadPoolExecutor(max_workers=1) as executor:
        with LedgerGuard(tmp_path / "one"):
            result = executor.submit(_attempt_lock, tmp_path / "two", 0)
            assert result.result(timeout=3) == "acquired"


def test_nested_exception_keeps_outer_lock_then_releases_it(tmp_path):
    path = tmp_path / "ledger"
    with ThreadPoolExecutor(max_workers=1) as executor:
        with LedgerGuard(path):
            with pytest.raises(RuntimeError), LedgerGuard(path):
                raise RuntimeError("Stop inner operation")
            assert executor.submit(_attempt_lock, path, 0).result(timeout=3) == "busy"
        assert executor.submit(_attempt_lock, path, 0).result(timeout=3) == "acquired"


def test_open_failure_does_not_leave_thread_lock_acquired(tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    real_open = state_lock.os.open

    def fail(*args, **kwargs):
        raise PermissionError("No permission")

    monkeypatch.setattr(state_lock.os, "open", fail)
    with pytest.raises(PermissionError), LedgerGuard(path):
        pass
    monkeypatch.setattr(state_lock.os, "open", real_open)
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(_attempt_lock, path, 0).result(timeout=3) == "acquired"


def test_process_contention_respects_timeout_and_preserves_lock_file(tmp_path):
    path = tmp_path / "ledger"
    script = """
import sys, time
from pathlib import Path
from agent_project.state_lock import LedgerGuard, LedgerBusyError
started = time.monotonic()
try:
    with LedgerGuard(Path(sys.argv[1]), timeout=0.05):
        raise AssertionError("Lock was acquired while another process owned it")
except LedgerBusyError:
    assert time.monotonic() - started < 2
    print("busy")
"""
    with LedgerGuard(path) as guard:
        inode = guard.lock_path.stat().st_ino
        result = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "busy"
    with LedgerGuard(path):
        assert guard.lock_path.stat().st_ino == inode


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork required")
def test_fork_does_not_inherit_ownership_or_unlock_parent(tmp_path):
    script = """
import os, sys
from pathlib import Path
from agent_project.state_lock import LedgerGuard, LedgerBusyError
guard = LedgerGuard(Path(sys.argv[1]), timeout=0.05)
with guard:
    child = os.fork()
    if child == 0:
        try:
            with guard:
                os._exit(2)
        except LedgerBusyError:
            os._exit(0)
        except BaseException:
            os._exit(3)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
with guard:
    pass
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "ledger")],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _operation(mode, data_path, changes_path, action_id=None):
    paths = {"data_path": data_path, "changes_path": changes_path}
    if mode == "assign":
        return tools.assign_category("tx_pending", "Reviewed", **paths)
    if mode == "undo":
        return tools.undo_category_assignment(action_id, **paths)
    return tools.delete_flagged_transaction("tx_dup_a", confirmed=True, **paths)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("assign", ["already_assigned", "success"]),
        ("undo", ["already_reverted", "success"]),
        ("delete", ["not_found", "success"]),
    ],
)
def test_thread_mutations_validate_and_write_in_one_critical_section(
    controlled_dataset, monkeypatch, mode, expected
):
    data_path, changes_path = controlled_dataset
    action_id = None
    if mode == "undo":
        action_id = tools.assign_category(
            "tx_pending", "Reviewed", data_path=data_path, changes_path=changes_path
        )["action_id"]
    before = len(tools.load_changes_log(changes_path))
    real_read = tools.load_effective_transactions

    def slow_read(*args, **kwargs):
        records = real_read(*args, **kwargs)
        time.sleep(0.03)  # Make a missing outer read/check/write lock observable.
        return records

    monkeypatch.setattr(tools, "load_effective_transactions", slow_read)
    start = threading.Barrier(2)

    def perform():
        start.wait(timeout=3)
        return _operation(mode, data_path, changes_path, action_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(perform) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert sorted(result["status"] for result in results) == expected
    assert len(tools.load_changes_log(changes_path)) == before + 1


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("assign", ["already_assigned", "success"]),
        ("undo", ["already_reverted", "success"]),
        ("delete", ["not_found", "success"]),
    ],
)
def test_two_process_mutations_cannot_both_validate_stale_state(
    controlled_dataset, mode, expected
):
    data_path, changes_path = controlled_dataset
    action_id = ""
    if mode == "undo":
        action_id = tools.assign_category(
            "tx_pending", "Reviewed", data_path=data_path, changes_path=changes_path
        )["action_id"]
    before = len(tools.load_changes_log(changes_path))
    script = """
import json, sys, time
from pathlib import Path
from agent_project import tools
data, changes, mode, action = sys.argv[1:]
paths = dict(data_path=Path(data), changes_path=Path(changes))
original = tools.load_effective_transactions
def slow(*args, **kwargs):
    records = original(*args, **kwargs)
    time.sleep(0.1)
    return records
tools.load_effective_transactions = slow
print("ready", flush=True)
if mode == "assign":
    result = tools.assign_category("tx_pending", "Reviewed", **paths)
elif mode == "undo":
    result = tools.undo_category_assignment(action, **paths)
else:
    result = tools.delete_flagged_transaction("tx_dup_a", confirmed=True, **paths)
print(json.dumps(result), flush=True)
"""
    processes = []
    try:
        with LedgerGuard(changes_path):
            for _ in range(2):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(data_path),
                        str(changes_path),
                        mode,
                        action_id,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                processes.append(process)
            for process in processes:
                ready, _, _ = select.select([process.stdout], [], [], 5)
                assert ready, "Child process failed to initialize"
                assert process.stdout.readline().strip() == "ready"
        results = []
        for process in processes:
            output, error = process.communicate(timeout=8)
            assert process.returncode == 0, error
            results.append(json.loads(output))
        assert sorted(result["status"] for result in results) == expected
        assert len(tools.load_changes_log(changes_path)) == before + 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_reentrant_external_guard_covers_domain_and_effective_reads(controlled_dataset):
    data_path, changes_path = controlled_dataset
    with LedgerGuard(changes_path, timeout=0):
        result = tools.assign_category(
            "tx_pending", "Reviewed", data_path=data_path, changes_path=changes_path
        )
        assert result["status"] == "success"
        state = tools.get_transaction_details(
            "tx_pending", data_path=data_path, changes_path=changes_path
        )
        assert state["category_hint"] == "Reviewed"
        assert state["last_action_id"] == result["action_id"]
        assert len(tools.load_changes_log(changes_path)) == 1


def test_read_waits_until_a_partially_written_event_is_complete(
    controlled_dataset, monkeypatch
):
    data_path, changes_path = controlled_dataset
    half_written = threading.Event()
    finish = threading.Event()

    def paused_append(event, path):
        text = json.dumps(event) + "\n"
        middle = len(text) // 2
        with path.open("w") as handle:
            handle.write(text[:middle])
            handle.flush()
            half_written.set()
            assert finish.wait(timeout=3)
            handle.write(text[middle:])

    monkeypatch.setattr(tools, "append_change_event", paused_append)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            tools.assign_category,
            "tx_pending",
            "Reviewed",
            data_path=data_path,
            changes_path=changes_path,
        )
        try:
            assert half_written.wait(timeout=3)
            reader = executor.submit(
                tools.get_transaction_details,
                "tx_pending",
                data_path=data_path,
                changes_path=changes_path,
            )
            time.sleep(0.03)
            assert not reader.done()
        finally:
            finish.set()
        assert writer.result(timeout=3)["status"] == "success"
        assert reader.result(timeout=3)["category_hint"] == "Reviewed"


@pytest.mark.parametrize(
    "name",
    [
        "load_changes_log",
        "append_change_event",
        "load_effective_transactions",
        "assign_category",
        "undo_category_assignment",
        "delete_flagged_transaction",
    ],
)
def test_guarded_tools_keep_their_original_signatures(name):
    function = getattr(tools, name)
    assert inspect.signature(function) == inspect.signature(function.__wrapped__)


def test_controller_returns_typed_state_busy_without_mutation(
    controlled_dataset, monkeypatch
):
    data_path, changes_path = controlled_dataset
    monkeypatch.setattr(state_lock, "DEFAULT_LOCK_TIMEOUT", 0.03)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with LedgerGuard(changes_path):
            future = executor.submit(
                execute_controlled_tool,
                "assign_category",
                {"transaction_id": "tx_pending", "category": "Reviewed"},
                data_path,
                changes_path,
            )
            result = future.result(timeout=3)
    assert ToolResult.model_validate(result).status == "state_busy"
    assert not changes_path.exists()


def test_controller_never_holds_ledger_lock_while_asking_for_confirmation(
    controlled_dataset,
):
    data_path, changes_path = controlled_dataset

    def reject_after_checking_other_thread(*args):
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert (
                executor.submit(_attempt_lock, changes_path, 0).result(timeout=3)
                == "acquired"
            )
        return False

    result = execute_controlled_tool(
        "delete_flagged_transaction",
        {"transaction_id": "tx_dup_a"},
        data_path,
        changes_path,
        reject_after_checking_other_thread,
    )
    assert result["status"] == "confirmation_denied"
    assert not changes_path.exists()


@pytest.mark.parametrize(
    "content",
    [
        b'{"action_id":',
        b"{}\n",
        b"[]\n",
        b"\xff\n",
        b'{"action_type":"unknown"}\n',
    ],
)
def test_corrupt_ledgers_are_not_skipped_repaired_or_appended(
    controlled_dataset, content
):
    data_path, changes_path = controlled_dataset
    changes_path.write_bytes(content)
    with pytest.raises(LedgerCorruptionError):
        tools.load_effective_transactions(data_path, changes_path)
    result = execute_controlled_tool(
        "assign_category",
        {"transaction_id": "tx_pending", "category": "Reviewed"},
        data_path,
        changes_path,
    )
    assert result["status"] == "state_conflict"
    assert changes_path.read_bytes() == content


@pytest.mark.parametrize(
    "mutation",
    [
        "no_newline",
        "duplicate_id",
        "wrong_previous",
        "unknown_transaction",
        "missing_payload",
        "wrong_payload",
        "unknown_action",
        "duplicate_json_key",
        "non_finite",
    ],
)
def test_incomplete_or_inconsistent_existing_events_fail_closed(
    controlled_dataset, mutation
):
    data_path, changes_path = controlled_dataset
    tools.assign_category(
        "tx_pending", "Reviewed", data_path=data_path, changes_path=changes_path
    )
    event = json.loads(changes_path.read_text())
    if mutation == "no_newline":
        content = json.dumps(event)
    elif mutation == "duplicate_id":
        content = (json.dumps(event) + "\n") * 2
    elif mutation == "duplicate_json_key":
        content = json.dumps(event).replace("{", '{"action_id":"shadowed",', 1) + "\n"
    else:
        if mutation == "wrong_previous":
            event["payload"]["previous_category"] = "Not the current category"
        elif mutation == "unknown_transaction":
            event["transaction_id"] = "missing"
        elif mutation == "missing_payload":
            event.pop("payload")
        elif mutation == "wrong_payload":
            event["payload"] = []
        elif mutation == "unknown_action":
            event["action_type"] = "unknown_action"
        elif mutation == "non_finite":
            event["unexpected_number"] = float("nan")
        content = json.dumps(event) + "\n"
    changes_path.write_text(content)
    with pytest.raises(LedgerCorruptionError):
        tools.assign_category(
            "tx_pending",
            "Other",
            force_override=True,
            data_path=data_path,
            changes_path=changes_path,
        )
    assert changes_path.read_text() == content


def test_invalid_append_is_rejected_before_a_write(controlled_dataset):
    _, changes_path = controlled_dataset
    with pytest.raises(LedgerCorruptionError):
        tools.append_change_event({}, changes_path)
    assert not changes_path.exists()


def test_fsync_failure_reports_unknown_outcome_not_success_or_automatic_retry(
    controlled_dataset, monkeypatch
):
    data_path, changes_path = controlled_dataset

    def fail(fd):
        raise OSError("Simulated durability failure")

    monkeypatch.setattr(tools.os, "fsync", fail)
    result = execute_controlled_tool(
        "assign_category",
        {"transaction_id": "tx_pending", "category": "Reviewed"},
        data_path,
        changes_path,
    )
    assert result["status"] == "error"
    assert result["error_code"] == "outcome_unknown"
    assert len(tools.load_changes_log(changes_path)) == 1
    with pytest.raises(LedgerWriteError):
        tools.assign_category(
            "tx_fuel",
            "Reviewed",
            force_override=True,
            data_path=data_path,
            changes_path=changes_path,
        )
