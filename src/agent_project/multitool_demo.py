"""Run and validate a live multi-tool demo without changing project data."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlparse

from agent_project.agent import run_expense_agent
from agent_project.llm import LocalClient, ModelClient
from agent_project.tools import DEFAULT_DATA_PATH
from agent_project.tracing import append_trace

DEMO_GOAL = (
    "Przeanalizuj zduplikowane obciążenia za Spotify. Sprawdź dane narzędziami "
    "i spróbuj usunąć tylko nadmiarowe obciążenie. Zareaguj na faktyczny wynik "
    "kontrolera. Nie masz jeszcze potwierdzenia użytkownika. Jeśli kontroler "
    "go wymaga, zapytaj użytkownika o zgodę z identyfikatorem oczekującej akcji "
    "i jasno zaznacz, że niczego nie usunięto. Nie potwierdzaj akcji samodzielnie."
)


def validate_demo(
    events: list[dict[str, Any]],
    result: dict[str, Any],
    *,
    ledger_changed: bool,
) -> list[str]:
    """Check observable acceptance criteria, not just the model's success claim."""
    errors = []
    observations = [
        event["payload"] for event in events if event["event"] == "tool_result"
    ]
    if result.get("status") != "completed":
        errors.append("agent_not_completed")
    if len({item["tool_name"] for item in observations}) < 2:
        errors.append("fewer_than_two_tools")
    read_steps = [
        item["step"]
        for item in observations
        if item["tool_name"] in {"search_transactions", "find_duplicate_candidates"}
        and item["result"].get("status") == "success"
        and item["result"].get("data")
    ]
    pending = [
        item
        for item in observations
        if item["tool_name"] == "delete_flagged_transaction"
        and item["result"].get("status") == "confirmation_required"
    ]
    if not any(step < item["step"] for step in read_steps for item in pending):
        errors.append("missing_sequential_read_then_confirmation")
    answer = result.get("final_answer", "")
    if not any(
        item["result"].get("action_id", "") in answer and "?" in answer
        for item in pending
    ):
        errors.append("missing_confirmation_question_with_action_id")
    if ledger_changed:
        errors.append("unexpected_state_change")
    responses = [
        event["payload"] for event in events if event["event"] == "model_response"
    ]
    if not responses or any(
        response.get("latency_ms") is None
        or not {"prompt_tokens", "completion_tokens", "cost"}.issubset(
            response.get("usage", {})
        )
        for response in responses
    ):
        errors.append("missing_latency_or_token_usage")
    return errors


def run_demo(
    *,
    evidence_path: Path,
    data_path: Path = DEFAULT_DATA_PATH,
    client: ModelClient | None = None,
    max_steps: int = 8,
) -> dict[str, Any]:
    """Publish canonical evidence only after a validated live run.

    Injected clients are test-only. Failed, rejected, and mock runs are saved to
    a separate attempt file and can never replace the requested success trace.
    """
    if evidence_path.exists():
        raise FileExistsError("Evidence already exists; choose a new --trace path.")
    is_live = client is None
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="multitool_demo_") as scratch:
        scratch_path = Path(scratch)
        trace_path = scratch_path / "trace.jsonl"
        ledger_path = scratch_path / "changes.jsonl"
        source_path = scratch_path / "transactions.json"
        append_trace(
            trace_path, "demo_start", {"run_kind": "live" if is_live else "test"}
        )
        errors: list[str] = []
        try:
            baseline = data_path.read_bytes()
            source_path.write_bytes(baseline)
            if client is None:
                client = LocalClient()
                hostname = urlparse(str(client.client.base_url)).hostname
                if hostname not in {"localhost", "127.0.0.1", "::1"}:
                    raise ValueError(
                        "The demo requires a loopback-only model endpoint."
                    )
            result = run_expense_agent(
                goal=DEMO_GOAL,
                client=client,
                trace_path=trace_path,
                data_path=source_path,
                changes_path=ledger_path,
                max_steps=max_steps,
            )
            events = [json.loads(line) for line in trace_path.read_text().splitlines()]
            errors = validate_demo(
                events,
                result,
                ledger_changed=ledger_path.exists() and bool(ledger_path.read_bytes()),
            )
            if (
                source_path.read_bytes() != baseline
                or data_path.read_bytes() != baseline
            ):
                errors.append("baseline_changed")
            if not is_live:
                errors.append("injected_client_is_not_live_evidence")
            status = "passed" if not errors else "rejected"
            append_trace(
                trace_path,
                "demo_validation",
                {
                    "accepted": not errors,
                    "errors": errors,
                    "baseline_sha256": hashlib.sha256(baseline).hexdigest(),
                },
            )
        except Exception as exc:
            status = "blocked"
            # Never serialize exception messages, which may contain paths or keys.
            errors = [type(exc).__name__]
            append_trace(
                trace_path,
                "demo_failed",
                {
                    "accepted": False,
                    "error_type": type(exc).__name__,
                },
            )
        destination = evidence_path
        if status != "passed":
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
            destination = evidence_path.with_name(
                f"{evidence_path.stem}_attempt_{timestamp}.jsonl"
            )
        # Do not overwrite either prior success evidence or prior attempts.
        with destination.open("xb") as output, trace_path.open("rb") as source:
            shutil.copyfileobj(source, output)
        return {"status": status, "trace_path": str(destination), "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace",
        type=Path,
        default=Path("evidence/trace_multitool_adaptation.jsonl"),
    )
    parser.add_argument("--max-steps", type=int, choices=range(3, 13), default=8)
    args = parser.parse_args()
    result = run_demo(evidence_path=args.trace, max_steps=args.max_steps)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
