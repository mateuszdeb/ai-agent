"""Tests for RAG evaluation runner (evaluate.py).

Demonstrates decoupled Retrieval vs Generation metrics: retrieval errors
must NOT mask generation correctness, and generation errors must NOT mask
retrieval correctness.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.eval_dataset import EvalQueryItem
from agent_project.rag.evaluate import (
    EvaluationReport,
    GenerationMetrics,
    QueryEvaluationResult,
    RetrievalMetrics,
    compute_aggregate_metrics,
    evaluate_single_query,
    generate_markdown_report,
    run_rag_evaluation,
)
from agent_project.rag.state import RAGState
from agent_project.rag.vector_store import SearchResult

# ---------------------------------------------------------------------------
# Shared helpers / stubs
# ---------------------------------------------------------------------------


class StubVectorStore:
    """Deterministic in-memory stub returning canned SearchResults per keyword."""

    def __init__(
        self,
        canned_results: dict[str, list[SearchResult]] | None = None,
    ) -> None:
        self.canned = canned_results or {}

    def search(
        self,
        query: str,
        top_k: int = 3,
        score_threshold: float = 0.5,
    ) -> list[SearchResult]:
        q_low = query.lower()
        for key, results in self.canned.items():
            if key.lower() in q_low:
                return results[:top_k]
        return []


def _make_chunk(
    chunk_id: str,
    text: str,
    source: str = "invoices.md",
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        source=source,
        section="Test Section",
        text=text,
        chunk_index=0,
        token_count=len(text.split()),
        metadata={},
    )


def _make_search_result(chunk_id: str, text: str, source: str) -> SearchResult:
    return SearchResult(
        chunk=_make_chunk(chunk_id, text, source),
        score=0.9,
    )


def _make_eval_item(
    *,
    query_id: str = "test_01",
    query: str = "What is the invoice total?",
    category: str = "simple",
    expected_behavior: str = "answer",
    relevant_sources: list[str] | None = None,
    expected_facts: list[str] | None = None,
) -> EvalQueryItem:
    return EvalQueryItem(
        id=query_id,
        query=query,
        category=category,  # type: ignore[arg-type]
        expected_behavior=expected_behavior,  # type: ignore[arg-type]
        expected_evidence_status="complete",
        relevant_sources=relevant_sources or ["invoices.md"],
        expected_facts=expected_facts or [],
        description="Unit test stub item",
    )


def _make_state(**overrides: Any) -> RAGState:
    """Return a minimal RAGState dict with sane defaults, overridable."""
    base: RAGState = {
        "query": "test query",
        "documents": [],
        "answer": "",
        "citations": [],
        "evidence_status": "complete",
        "step_count": 1,
        "retry_count": 0,
        "missing_evidence": [],
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


def _make_query_result(**overrides: Any) -> QueryEvaluationResult:
    defaults = dict(
        query_id="q01",
        query_text="test",
        category="simple",
        expected_behavior="answer",
        actual_behavior="answer",
        behavior_correct=True,
        retrieved_chunk_ids=["c1"],
        retrieved_sources=["invoices.md"],
        retrieval_hit=True,
        retrieval_precision=1.0,
        reciprocal_rank=1.0,
        citations=[],
        citations_valid=True,
        fact_coverage=1.0,
        answer_text="Some answer.",
        step_count=2,
        notes="",
    )
    defaults.update(overrides)
    return QueryEvaluationResult(**defaults)


# ===========================================================================
# 1. evaluate_single_query — correct behavior
# ===========================================================================


def test_evaluate_single_query_correct_behavior() -> None:
    """Retrieval hit + correct answer behavior → behavior_correct=True."""
    chunk = _make_chunk("c001", "Invoice total is 500 PLN.", "invoices.md")
    state = _make_state(
        documents=[chunk],
        answer="Invoice total is 500 PLN.",
        citations=[],
        evidence_status="complete",
        step_count=2,
    )
    item = _make_eval_item(
        expected_facts=["500 PLN"],
        relevant_sources=["invoices.md"],
    )

    result = evaluate_single_query(item, state)

    assert result.behavior_correct is True
    assert result.retrieval_hit is True
    assert result.actual_behavior == "answer"
    assert result.fact_coverage == pytest.approx(1.0)


# ===========================================================================
# 2. Decoupling: retrieval MISS but generation correct (abstain)
#    — retrieval error must NOT mask generation correctness
# ===========================================================================


def test_retrieval_miss_does_not_mask_correct_abstain() -> None:
    """Retrieval miss on an unanswerable query: abstain is still correct behavior.

    Key decoupling assertion:
        retrieval_hit=False  AND  behavior_correct=True
    """
    # No documents retrieved — simulates retrieval miss
    state = _make_state(
        documents=[],
        answer="",
        citations=[],
        evidence_status="lacks_evidence",
        step_count=1,
    )
    # Unanswerable item with expected source (retrieval should have found it,
    # but it didn't → retrieval_hit=False)
    item = _make_eval_item(
        query_id="unans_01",
        query="What is the CEO salary?",
        category="unanswerable",
        expected_behavior="abstain",
        relevant_sources=["hr_secrets.md"],  # expected but not retrieved
        expected_facts=[],
    )

    result = evaluate_single_query(item, state)

    # Generation is correct: graph correctly abstained
    assert result.behavior_correct is True, (
        "Graph correctly abstained despite retrieval miss — "
        "generation correctness should not be masked by retrieval error"
    )
    # Retrieval is independently wrong
    assert result.retrieval_hit is False, (
        "Retrieval hit should be False — relevant source was not retrieved"
    )
    assert result.actual_behavior == "abstain"


# ===========================================================================
# 3. Decoupling: retrieval HIT but wrong generation behavior
#    — generation error must NOT be hidden by retrieval success
# ===========================================================================


def test_retrieval_hit_does_not_hide_wrong_generation() -> None:
    """Retrieval succeeds but graph answers when it should abstain.

    Key decoupling assertion:
        retrieval_hit=True  AND  behavior_correct=False
    """
    chunk = _make_chunk("c002", "Some unrelated text.", "invoices.md")
    state = _make_state(
        documents=[chunk],
        answer="Some unrelated text.",
        citations=[],
        evidence_status="complete",  # incorrectly marked complete
        step_count=2,
    )
    item = _make_eval_item(
        query_id="wrong_01",
        query="What is the invoice total?",
        category="unanswerable",
        expected_behavior="abstain",
        relevant_sources=["invoices.md"],
        expected_facts=[],
    )

    result = evaluate_single_query(item, state)

    # Retrieval found the source → hit
    assert result.retrieval_hit is True, "Retrieval hit should be True"
    # But the graph answered instead of abstaining → generation is wrong
    assert result.behavior_correct is False, (
        "behavior_correct must be False — retrieval hit should not hide "
        "wrong generation behavior"
    )


# ===========================================================================
# 4. compute_aggregate_metrics — retrieval and generation metrics decouple
# ===========================================================================


def test_compute_aggregate_metrics_decoupled() -> None:
    """Retrieval and generation metrics are computed independently.

    One query has retrieval_hit=False but behavior_correct=True (abstained).
    Another has retrieval_hit=True but behavior_correct=False.
    The resulting metrics must reflect each error only in its own domain.
    """
    items = [
        _make_eval_item(
            query_id="q1",
            category="unanswerable",
            expected_behavior="abstain",
            relevant_sources=["doc.md"],
        ),
        _make_eval_item(
            query_id="q2",
            category="unanswerable",
            expected_behavior="abstain",
            relevant_sources=["doc.md"],
        ),
    ]

    results = [
        # q1: retrieval MISS, generation CORRECT (abstained)
        _make_query_result(
            query_id="q1",
            category="unanswerable",
            expected_behavior="abstain",
            actual_behavior="abstain",
            behavior_correct=True,
            retrieval_hit=False,  # ← retrieval error
            retrieval_precision=0.0,
            reciprocal_rank=0.0,
            citations=[],
        ),
        # q2: retrieval HIT, generation WRONG (answered instead of abstaining)
        _make_query_result(
            query_id="q2",
            category="unanswerable",
            expected_behavior="abstain",
            actual_behavior="answer",
            behavior_correct=False,  # ← generation error
            retrieval_hit=True,
            retrieval_precision=1.0,
            reciprocal_rank=1.0,
            citations=[],
        ),
    ]

    ret, gen = compute_aggregate_metrics(results, items)

    # Retrieval: 1 hit out of 2 → 50%
    assert ret.hit_rate == pytest.approx(0.5)
    assert ret.total_retrieval_queries == 2

    # Generation: 1 correct out of 2 → 50% overall accuracy
    assert gen.overall_accuracy == pytest.approx(0.5)
    assert gen.total_generation_queries == 2

    # Abstention: only q1 abstained correctly → 50%
    assert gen.abstention_accuracy == pytest.approx(0.5)

    # Retrieval and generation metrics are NOT equal (independent error types)
    # specifically: hit_rate reflects retrieval only, overall_accuracy generation only
    assert ret.hit_rate != gen.overall_accuracy or ret.mrr != gen.citation_precision


# ===========================================================================
# 5. generate_markdown_report — structure validation
# ===========================================================================


def test_generate_markdown_report_has_required_sections() -> None:
    """Report contains all required headers and metric tables."""
    ret = RetrievalMetrics(
        hit_rate=1.0,
        precision_at_k=0.72,
        mrr=1.0,
        total_retrieval_queries=6,
    )
    gen = GenerationMetrics(
        fact_coverage_rate=0.9,
        citation_precision=1.0,
        abstention_accuracy=1.0,
        clarification_accuracy=1.0,
        overall_accuracy=0.9,
        total_generation_queries=10,
    )
    report = EvaluationReport(
        timestamp="2026-01-01 00:00:00 UTC",
        total_queries=10,
        retrieval=ret,
        generation=gen,
        query_results=[],
    )

    md = generate_markdown_report(report)

    assert "# Raport Ewaluacji RAG" in md
    assert "Hit Rate" in md
    assert "Precision@k" in md
    assert "MRR" in md
    assert "Fact Coverage" in md
    assert "Citation Precision" in md
    assert "Abstention Accuracy" in md
    assert "Overall Accuracy" in md
    assert "Szczegółowe Wyniki" in md
    assert "Przegląd i Wnioski" in md
    assert "Analiza Przypadku Błędu (Failure Analysis)" in md
    assert "eval_06_rewrite_pge_high_bill" in md


# ===========================================================================
# 6. run_rag_evaluation — smoke test with StubVectorStore
# ===========================================================================


def test_run_rag_evaluation_smoke(tmp_path: Path) -> None:
    """run_rag_evaluation completes with StubVectorStore; returns EvaluationReport."""
    # Build a minimal single-item eval dataset JSON
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        """[
  {
    "id": "smoke_01",
    "query": "What is the total for invoice INV-001?",
    "category": "simple",
    "expected_behavior": "answer",
    "expected_evidence_status": "complete",
    "relevant_sources": ["invoices.md"],
    "expected_facts": [],
    "description": "Smoke test query"
  }
]""",
        encoding="utf-8",
    )

    chunk = _make_chunk("c_smoke", "Invoice INV-001 total is 100 PLN.", "invoices.md")
    stub_store = StubVectorStore(
        canned_results={"invoice": [SearchResult(chunk=chunk, score=0.95)]}
    )

    report = run_rag_evaluation(
        dataset_path=dataset_path,
        vector_store=stub_store,
        client=None,
        report_output_path=None,
    )

    assert isinstance(report, EvaluationReport)
    assert report.total_queries == 1
    assert len(report.query_results) == 1
    assert isinstance(report.retrieval, RetrievalMetrics)
    assert isinstance(report.generation, GenerationMetrics)
    # Pipeline ran — step count should be positive
    assert report.query_results[0].step_count >= 1


# ===========================================================================
# 7. run_rag_evaluation — report file is written
# ===========================================================================


def test_run_rag_evaluation_writes_report(tmp_path: Path) -> None:
    """run_rag_evaluation writes markdown report to the specified path."""
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        """[
  {
    "id": "file_01",
    "query": "faktura",
    "category": "ambiguous",
    "expected_behavior": "clarify",
    "expected_evidence_status": "ambiguous",
    "relevant_sources": [],
    "expected_facts": [],
    "description": "Ambiguous query test"
  }
]""",
        encoding="utf-8",
    )

    stub_store = StubVectorStore(canned_results={})
    report_path = tmp_path / "report.md"

    run_rag_evaluation(
        dataset_path=dataset_path,
        vector_store=stub_store,
        client=None,
        report_output_path=report_path,
    )

    assert report_path.exists(), "Markdown report file must be written"
    contents = report_path.read_text(encoding="utf-8")
    assert "# Raport Ewaluacji RAG" in contents
    assert len(contents) > 200
