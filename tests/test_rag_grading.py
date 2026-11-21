"""Tests for evidence grading, 4 decision states, and query rewriting."""

from __future__ import annotations

from typing import Any

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.nodes.grading import (
    EvidenceGradeResult,
    evaluate_evidence,
    grade_evidence_node,
    rewrite_query_node,
)
from agent_project.rag.state import create_initial_state


class MockGradingClient:
    """Mock LLM client returning predetermined grading or rewrite output."""

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {"text": self.response_text}


def test_grade_complete_evidence():
    """Complete evidence containing relevant terms returns 'complete' status."""
    chunk = DocumentChunk(
        chunk_id="budget#001",
        source="household_budget_policy.md",
        section="Miesięczne limity kwotowe wg kategorii",
        text="Limit na Groceries (artykuły spożywcze) wynosi 2500 PLN.",
        chunk_index=0,
        token_count=10,
    )
    result = evaluate_evidence(
        query="Jaki jest miesięczny limit na artykuły spożywcze Groceries?",
        documents=[chunk],
    )
    assert isinstance(result, EvidenceGradeResult)
    assert result.status == "complete"
    assert "wystarczające" in result.reason.lower()


def test_grade_ambiguous_query():
    """Ambiguous query returns 'ambiguous' status with clarification question."""
    result = evaluate_evidence(
        query="faktura",
        documents=[],
    )
    assert result.status == "ambiguous"
    assert result.clarification_question is not None
    assert "?" in result.clarification_question


def test_grade_lacks_evidence_unanswerable_topic():
    """Unanswerable topic returns 'lacks_evidence' with named gaps."""
    chunk = DocumentChunk(
        chunk_id="budget#001",
        source="household_budget_policy.md",
        section="Budżet",
        text="Zasady budżetu domowego.",
        chunk_index=0,
        token_count=5,
    )
    result = evaluate_evidence(
        query="Jak rozliczyć zyski z kryptowalut i bitcoina w budżecie?",
        documents=[chunk],
    )
    assert result.status == "lacks_evidence"
    assert result.missing_evidence is not None
    assert len(result.missing_evidence) >= 1
    assert any("kryptowalut" in item.lower() for item in result.missing_evidence)


def test_grade_need_better_query_on_partial_match():
    """Partial match on first attempt (retry_count=0) returns 'need_better_query'."""
    chunk = DocumentChunk(
        chunk_id="spotify#001",
        source="subscriptions_policy.md",
        section="Streaming",
        text="Plan Spotify Polska kosztuje 24.99 PLN miesięcznie.",
        chunk_index=0,
        token_count=8,
    )
    result = evaluate_evidence(
        query="Procedura reklamacji i zwrotu opłaty za duplikat Spotify",
        documents=[chunk],
        retry_count=0,
    )
    assert result.status == "need_better_query"
    assert result.missing_evidence is not None


def test_grade_evidence_node_updates_state():
    """grade_evidence_node updates RAGState with evaluated status."""
    state = create_initial_state("faktura")
    updated = grade_evidence_node(state)

    assert updated["evidence_status"] == "ambiguous"
    assert updated["clarification_question"] is not None
    assert updated["step_count"] == 1


def test_rewrite_query_node_increments_retry_and_expands_query():
    """rewrite_query_node expands query and increments retry_count."""
    state = create_initial_state("Spotify")
    state["retry_count"] = 0
    state["missing_evidence"] = ["duplikat"]

    updated = rewrite_query_node(state)
    assert updated["retry_count"] == 1
    assert "spotify" in updated["query"].lower()
    assert len(updated["query"]) > len("Spotify")
    assert updated["step_count"] == 1


def test_evaluate_evidence_with_llm_client():
    """ModelClient structured JSON output is parsed into EvidenceGradeResult."""
    mock_json = (
        '{"status": "complete", "reason": "Dokumenty zawierają pełne dane.", '
        '"missing_evidence": null, "clarification_question": null}'
    )
    client = MockGradingClient(mock_json)
    chunk = DocumentChunk(
        chunk_id="c#1",
        source="s.md",
        section="Sec",
        text="Text",
        chunk_index=0,
        token_count=1,
    )
    result = evaluate_evidence("Dowolne zapytanie", [chunk], client=client)
    assert result.status == "complete"
    assert result.reason == "Dokumenty zawierają pełne dane."


def test_evaluate_evidence_llm_fallback_on_invalid_json():
    """Invalid LLM response falls back gracefully to deterministic grading."""
    client = MockGradingClient("I am not returning JSON.")
    result = evaluate_evidence(
        "Kupowanie bitcoinów i kryptowalut",
        [],
        client=client,
    )
    assert result.status == "lacks_evidence"
    assert result.missing_evidence is not None
