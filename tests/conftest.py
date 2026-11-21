"""Small fixed datasets for deterministic tests, independent of project data."""

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_project.llm import MockClient


@dataclass
class ControlledClock:
    """Injectable UTC and monotonic time without sleeping or patching globals."""

    started_at: datetime = datetime(2026, 10, 25, tzinfo=UTC)
    _elapsed: float = field(default=0.0, init=False)

    def now(self) -> datetime:
        return self.started_at + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def advance(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Clock advance must be finite and non-negative.")
        self._elapsed += seconds


class RecordingMockClient(MockClient):
    """Offline scripted client with isolated replies and request snapshots."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(deepcopy(responses))
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(deepcopy({"messages": messages, "tools": tools}))
        return deepcopy(super().complete(messages, tools))


@pytest.fixture
def controlled_clock() -> ControlledClock:
    """A fresh clock per test; future TTL tests can inject its methods."""
    return ControlledClock()


@pytest.fixture
def offline_model_factory() -> type[RecordingMockClient]:
    """Build independent MockClient instances; never construct a live client."""
    return RecordingMockClient


@pytest.fixture
def controlled_dataset(tmp_path):
    """Provide isolated baseline/ledger paths and verify baseline immutability."""
    records = [
        ("tx_categorized", "Market", 40.0, "Groceries", "Weekly groceries"),
        ("tx_pending", "Pending Shop", 120.0, None, "Uncategorized purchase"),
        ("tx_dup_a", "Stream", 25.0, "Subscriptions", "Monthly subscription"),
        ("tx_dup_b", "Stream", 25.0, "Subscriptions", "Repeated subscription"),
        ("tx_fuel", "Fuel Station", 200.0, "Transport", "Fuel purchase"),
    ]
    data_path = tmp_path / "baseline.json"
    changes_path = tmp_path / "ledger.jsonl"
    data_path.write_text(
        json.dumps(
            [
                {
                    "id": tx_id,
                    "date": "2026-10-01",
                    "merchant": merchant,
                    "amount": amount,
                    "currency": "PLN",
                    "category_hint": category,
                    "description": description,
                }
                for tx_id, merchant, amount, category, description in records
            ]
        ),
        encoding="utf-8",
    )
    baseline_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
    yield data_path, changes_path
    assert hashlib.sha256(data_path.read_bytes()).hexdigest() == baseline_hash
