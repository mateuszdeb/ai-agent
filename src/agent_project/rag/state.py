"""Graph execution state and contracts for LangGraph RAG workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypedDict

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.vector_store import SearchResult

EvidenceStatus = Literal[
    "complete",           # Relevant evidence is complete -> Answer with citations
    "need_better_query",  # A better query may help -> Search once more
    "ambiguous",          # User intent is ambiguous -> Ask for clarification
    "lacks_evidence",     # Collection lacks the evidence -> Abstain & name missing
]

VALID_EVIDENCE_STATUSES: set[str] = {
    "complete",
    "need_better_query",
    "ambiguous",
    "lacks_evidence",
}


class RetrievalRecord(TypedDict):
    """Historical trace record of a retrieval query and matched chunk IDs."""

    query: str
    chunk_ids: list[str]
    top_score: float
    timestamp: str


class RAGState(TypedDict):
    """State schema for the LangGraph RAG execution graph."""

    query: str
    retrieval_history: list[RetrievalRecord]
    documents: list[DocumentChunk]
    evidence_status: EvidenceStatus | None
    retry_count: int
    answer: str | None
    citations: list[str]
    missing_evidence: list[str] | None
    clarification_question: str | None
    step_count: int


def create_initial_state(query: str) -> RAGState:
    """Create a clean, initialized RAGState for a given user query."""
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Initial query cannot be empty or whitespace.")

    return {
        "query": clean_query,
        "retrieval_history": [],
        "documents": [],
        "evidence_status": None,
        "retry_count": 0,
        "answer": None,
        "citations": [],
        "missing_evidence": None,
        "clarification_question": None,
        "step_count": 0,
    }


def record_retrieval(
    state: RAGState,
    query: str,
    results: list[SearchResult],
) -> RAGState:
    """Return a new state with updated retrieval history and accumulated documents."""
    chunk_ids = [res.chunk_id for res in results]
    top_score = max((res.score for res in results), default=0.0)

    new_record: RetrievalRecord = {
        "query": query,
        "chunk_ids": chunk_ids,
        "top_score": round(top_score, 4),
        "timestamp": datetime.now(UTC).isoformat(),
    }

    # Deduplicate accumulated documents by chunk_id
    existing_chunks = {doc.chunk_id: doc for doc in state["documents"]}
    for res in results:
        existing_chunks[res.chunk.chunk_id] = res.chunk

    new_state = dict(state)
    new_state["retrieval_history"] = list(state["retrieval_history"]) + [new_record]
    new_state["documents"] = list(existing_chunks.values())
    new_state["step_count"] = state.get("step_count", 0) + 1
    return new_state  # type: ignore[return-value]


def validate_rag_state(state: RAGState) -> list[str]:
    """Validate internal consistency and invariants of a RAGState."""
    errors: list[str] = []

    if not state.get("query", "").strip():
        errors.append("empty_query")

    status = state.get("evidence_status")
    if status is not None and status not in VALID_EVIDENCE_STATUSES:
        errors.append(f"invalid_evidence_status:{status}")

    retry_count = state.get("retry_count", 0)
    if retry_count < 0:
        errors.append("negative_retry_count")

    # Invariant: If complete, answer should ideally be present when finalized
    if status == "complete" and state.get("answer") is not None:
        if not isinstance(state.get("citations"), list):
            errors.append("invalid_citations_type")

    # Invariant: If lacks_evidence, missing_evidence should be named
    if status == "lacks_evidence" and state.get("missing_evidence") is not None:
        missing = state["missing_evidence"]
        if not isinstance(missing, list) or not missing:
            errors.append("missing_evidence_not_named")

    # Invariant: If ambiguous, clarification question should be provided
    if status == "ambiguous" and state.get("clarification_question") is not None:
        if not state["clarification_question"].strip():
            errors.append("empty_clarification_question")

    return errors
