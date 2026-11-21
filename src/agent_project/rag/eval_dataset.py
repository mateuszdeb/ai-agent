"""Dataset schema and loader for the RAG benchmark."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_EVAL_DATASET_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "data"
    / "rag_eval_dataset.json"
)

EvalCategory = Literal["simple", "bounded_search", "ambiguous", "unanswerable"]
ExpectedBehavior = Literal["answer", "rewrite_and_answer", "clarify", "abstain"]


class EvalQueryItem(BaseModel):
    """Benchmark evaluation query entry with expected behaviors and sources."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(description="Unique query identifier")
    query: str = Field(description="User query string")
    category: EvalCategory = Field(
        description="Category: simple, bounded_search, ambiguous, unanswerable"
    )
    expected_behavior: ExpectedBehavior = Field(
        description="Expected behavior: answer, rewrite_and_answer, clarify, abstain"
    )
    expected_evidence_status: str = Field(
        description="Expected evidence status: complete, need_better_query, etc."
    )
    relevant_sources: list[str] = Field(
        default_factory=list,
        description="Source documents containing evidence for answerable questions",
    )
    expected_facts: list[str] = Field(
        default_factory=list,
        description="Key facts/numbers expected in grounded answer",
    )
    missing_evidence_topic: str | None = Field(
        default=None,
        description="Named missing topic for unanswerable queries",
    )
    description: str = Field(
        default="",
        description="Human-readable explanation of the test case intent",
    )


def load_evaluation_dataset(
    dataset_path: Path | str | None = None,
) -> list[EvalQueryItem]:
    """Load and validate the evaluation benchmark queries dataset."""
    path = Path(dataset_path) if dataset_path else DEFAULT_EVAL_DATASET_PATH
    if not path.exists():
        raise FileNotFoundError(f"Evaluation dataset not found at: {path}")

    raw_data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, list):
        raise ValueError("Evaluation dataset must be a JSON array of query objects.")

    items = [EvalQueryItem.model_validate(item) for item in raw_data]
    return items
