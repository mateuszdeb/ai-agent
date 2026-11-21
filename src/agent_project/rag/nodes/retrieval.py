"""Retrieval node for LangGraph RAG execution graph."""

from __future__ import annotations

from typing import Any

from agent_project.rag.state import RAGState, record_retrieval
from agent_project.rag.vector_store import SearchResult, VectorStore


def retrieve_node(
    state: RAGState,
    *,
    vector_store: VectorStore | None = None,
    top_k: int = 3,
    score_threshold: float = 0.5,
) -> dict[str, Any]:
    """LangGraph node: Retrieve relevant document chunks from VectorStore."""
    query = state["query"]
    results: list[SearchResult] = []
    if vector_store is not None:
        results = vector_store.search(
            query=query,
            top_k=top_k,
            score_threshold=score_threshold,
        )

    updated_state = record_retrieval(state, query, results)
    return {
        "retrieval_history": updated_state["retrieval_history"],
        "documents": updated_state["documents"],
        "step_count": updated_state["step_count"],
    }
