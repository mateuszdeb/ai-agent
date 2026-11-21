"""Tests for evaluation benchmark dataset structure and contents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_project.rag.eval_dataset import (
    DEFAULT_EVAL_DATASET_PATH,
    EvalQueryItem,
    load_evaluation_dataset,
)
from agent_project.rag.vector_store import build_vector_store_from_directory


def test_eval_dataset_file_exists_and_loads():
    """Verify evaluation dataset JSON file exists at the canonical path."""
    assert DEFAULT_EVAL_DATASET_PATH.exists()
    items = load_evaluation_dataset()
    assert len(items) >= 10


def test_eval_dataset_category_distribution():
    """Verify benchmark distribution: 7 answerable (4/2/1) + 3 unanswerable."""
    items = load_evaluation_dataset()

    by_category: dict[str, list[EvalQueryItem]] = {}
    for it in items:
        by_category.setdefault(it.category, []).append(it)

    # 4 simple, 2 bounded search, 1 ambiguous = 7 answerable
    assert len(by_category.get("simple", [])) == 4
    assert len(by_category.get("bounded_search", [])) == 2
    assert len(by_category.get("ambiguous", [])) == 1

    # 3 unanswerable
    assert len(by_category.get("unanswerable", [])) == 3

    answerable = [
        it for it in items
        if it.expected_behavior in {"answer", "rewrite_and_answer", "clarify"}
    ]
    unanswerable = [
        it for it in items
        if it.expected_behavior == "abstain"
    ]
    assert len(answerable) == 7
    assert len(unanswerable) == 3


def test_eval_dataset_fields_integrity_and_uniqueness():
    """Verify unique IDs, non-empty queries, and consistent expected behaviors."""
    items = load_evaluation_dataset()

    ids = [it.id for it in items]
    assert len(ids) == len(set(ids)), "IDs must be unique across all benchmark items."

    for it in items:
        assert it.id.strip(), "Item ID cannot be empty."
        assert it.query.strip(), "Item query cannot be empty."
        assert it.expected_behavior in {
            "answer", "rewrite_and_answer", "clarify", "abstain"
        }
        assert it.expected_evidence_status in {
            "complete", "need_better_query", "ambiguous", "lacks_evidence"
        }


def test_eval_dataset_relevant_sources_exist_on_disk():
    """All relevant sources listed in benchmark exist in data/documents."""
    items = load_evaluation_dataset()
    docs_dir = Path("data/documents")
    assert docs_dir.exists() and docs_dir.is_dir()

    for it in items:
        if it.category in {"simple", "bounded_search"}:
            assert len(it.relevant_sources) > 0, (
                f"Item {it.id} must define at least one relevant source."
            )
            for src in it.relevant_sources:
                doc_path = docs_dir / src
                assert doc_path.exists(), (
                    f"Relevant source '{src}' for item {it.id} not found in {docs_dir}."
                )


def test_eval_dataset_unanswerable_items_have_named_missing_topic():
    """Unanswerable queries must specify missing_evidence_topic."""
    items = load_evaluation_dataset()
    unanswerable = [it for it in items if it.category == "unanswerable"]

    for it in unanswerable:
        assert it.expected_behavior == "abstain"
        assert it.expected_evidence_status == "lacks_evidence"
        assert it.relevant_sources == []
        assert it.missing_evidence_topic is not None
        assert len(it.missing_evidence_topic.strip()) > 0


def test_eval_dataset_retrieval_matches_relevant_sources():
    """Verify vector search on documents finds relevant sources for simple items."""
    store = build_vector_store_from_directory("data/documents")
    items = load_evaluation_dataset()

    simple_items = [it for it in items if it.category == "simple"]
    for it in simple_items:
        results = store.search(it.query, top_k=3, score_threshold=0.3)
        found_sources = {res.source for res in results}
        # At least one expected source must be retrieved
        overlap = set(it.relevant_sources).intersection(found_sources)
        assert len(overlap) > 0, (
            f"Query '{it.query}' did not retrieve any of {it.relevant_sources}. "
            f"Found: {found_sources}"
        )


def test_load_evaluation_dataset_error_handling(tmp_path: Path):
    """load_evaluation_dataset handles missing files and invalid structures."""
    # 1. Non-existent file
    with pytest.raises(FileNotFoundError, match="Evaluation dataset not found"):
        load_evaluation_dataset(tmp_path / "non_existent.json")

    # 2. Non-list JSON
    bad_json = tmp_path / "bad.json"
    bad_json.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON array"):
        load_evaluation_dataset(bad_json)
