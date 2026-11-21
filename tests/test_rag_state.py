"""Tests for RAGState, state transitions, and LangGraph schema compatibility."""

from __future__ import annotations

import pytest
from langgraph.graph import END, START, StateGraph

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.state import (
    RAGState,
    create_initial_state,
    record_retrieval,
    validate_rag_state,
)
from agent_project.rag.vector_store import SearchResult


def test_create_initial_state():
    """Initial state creation initializes all fields with clean defaults."""
    state = create_initial_state("  Jaki jest limit na zakupy spożywcze?  ")
    assert state["query"] == "Jaki jest limit na zakupy spożywcze?"
    assert state["retrieval_history"] == []
    assert state["documents"] == []
    assert state["evidence_status"] is None
    assert state["retry_count"] == 0
    assert state["answer"] is None
    assert state["citations"] == []
    assert state["missing_evidence"] is None
    assert state["clarification_question"] is None
    assert state["step_count"] == 0

    errors = validate_rag_state(state)
    assert errors == []


def test_create_initial_state_empty_fails():
    """Empty or whitespace queries are rejected."""
    with pytest.raises(ValueError, match="cannot be empty"):
        create_initial_state("")
    with pytest.raises(ValueError, match="cannot be empty"):
        create_initial_state("   \n\t  ")


def test_record_retrieval_accumulates_and_deduplicates():
    """record_retrieval updates history, accumulates documents, and deduplicates."""
    state = create_initial_state("PGE faktura")
    chunk1 = DocumentChunk(
        chunk_id="pge#001",
        source="pge.md",
        section="Taryfa",
        text="Taryfa G11 w PGE.",
        chunk_index=0,
        token_count=5,
    )
    chunk2 = DocumentChunk(
        chunk_id="pge#002",
        source="pge.md",
        section="Termin",
        text="Termin płatności 14 dni.",
        chunk_index=1,
        token_count=5,
    )

    res1 = SearchResult(chunk=chunk1, score=0.85)
    res2 = SearchResult(chunk=chunk2, score=0.80)

    # First retrieval step
    state1 = record_retrieval(state, "PGE faktura", [res1, res2])
    assert len(state1["retrieval_history"]) == 1
    assert state1["retrieval_history"][0]["query"] == "PGE faktura"
    assert state1["retrieval_history"][0]["top_score"] == 0.85
    assert len(state1["documents"]) == 2
    assert state1["step_count"] == 1

    # Second retrieval step with overlapping chunk1 and new chunk3
    chunk3 = DocumentChunk(
        chunk_id="pge#003",
        source="pge.md",
        section="Odsetki",
        text="Odsetki 11.25%.",
        chunk_index=2,
        token_count=4,
    )
    res3 = SearchResult(chunk=chunk3, score=0.75)
    res1_repeat = SearchResult(chunk=chunk1, score=0.82)

    state2 = record_retrieval(state1, "PGE odsetki", [res1_repeat, res3])
    assert len(state2["retrieval_history"]) == 2
    # Document list should have 3 unique chunks (no duplicate chunk1)
    assert len(state2["documents"]) == 3
    doc_ids = {d.chunk_id for d in state2["documents"]}
    assert doc_ids == {"pge#001", "pge#002", "pge#003"}
    assert state2["step_count"] == 2


def test_state_validation_invariants():
    """Verify validation detects invalid statuses and inconsistent invariants."""
    # 1. Invalid status
    bad_status_state: RAGState = create_initial_state("Test")
    bad_status_state["evidence_status"] = "unknown_status"  # type: ignore[typeddict-item]
    state_errors = validate_rag_state(bad_status_state)
    assert any("invalid_evidence_status" in e for e in state_errors)

    # 2. lacks_evidence without naming missing evidence
    lacks_state: RAGState = create_initial_state("Krypto")
    lacks_state["evidence_status"] = "lacks_evidence"
    lacks_state["missing_evidence"] = []
    assert "missing_evidence_not_named" in validate_rag_state(lacks_state)

    # 3. ambiguous without clarification question
    ambig_state: RAGState = create_initial_state("Faktura")
    ambig_state["evidence_status"] = "ambiguous"
    ambig_state["clarification_question"] = "   "
    assert "empty_clarification_question" in validate_rag_state(ambig_state)

    # 4. Valid complete state
    complete_state: RAGState = create_initial_state("Budżet")
    complete_state["evidence_status"] = "complete"
    complete_state["answer"] = "Limit to 2500 PLN [source: budget#001]."
    complete_state["citations"] = ["budget#001"]
    assert validate_rag_state(complete_state) == []


def test_langgraph_stategraph_integration():
    """Verify RAGState works as a first-class schema inside LangGraph StateGraph."""
    graph_builder = StateGraph(RAGState)

    def dummy_node(state: RAGState) -> dict[str, int]:
        return {"step_count": state["step_count"] + 1}

    graph_builder.add_node("step", dummy_node)
    graph_builder.add_edge(START, "step")
    graph_builder.add_edge("step", END)

    compiled = graph_builder.compile()
    initial = create_initial_state("Test LangGraph integration")
    out = compiled.invoke(initial)

    assert out["query"] == "Test LangGraph integration"
    assert out["step_count"] == 1
