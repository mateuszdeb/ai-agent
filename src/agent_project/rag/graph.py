"""LangGraph execution graph for the stateful RAG workflow."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_project.llm import ModelClient
from agent_project.rag.nodes import (
    abstain_node,
    clarify_node,
    generate_grounded_answer_node,
    grade_evidence_node,
    retrieve_node,
    rewrite_query_node,
)
from agent_project.rag.state import RAGState, create_initial_state
from agent_project.rag.vector_store import VectorStore


def decide_next_step(state: RAGState, *, max_retries: int = 1) -> str:
    """Evaluate evidence grade and retry budget to choose next graph node.

    Transitions:
    - complete: Enough relevant evidence -> generate grounded answer
    - ambiguous: Ambiguous user intent -> ask clarification question
    - need_better_query:
        * retry_count < max_retries -> rewrite query for bounded search
        * retry_count >= max_retries -> abstain (retry limit reached)
    - lacks_evidence: Information absent from knowledge base -> abstain
    """
    status = state.get("evidence_status")
    retry_count = state.get("retry_count", 0)

    if status == "complete":
        return "generate_answer"
    if status == "ambiguous":
        return "clarify"
    if status == "need_better_query":
        if retry_count < max_retries:
            return "rewrite_query"
        return "abstain"
    if status == "lacks_evidence":
        return "abstain"
    return "abstain"


def build_rag_graph(
    *,
    vector_store: VectorStore | None = None,
    client: ModelClient | None = None,
    top_k: int = 3,
    score_threshold: float = 0.5,
    max_retries: int = 1,
) -> StateGraph:
    """Construct an uncompiled StateGraph for the stateful RAG workflow."""
    builder = StateGraph(RAGState)

    def _retrieve_step(state: RAGState) -> dict[str, Any]:
        return retrieve_node(
            state,
            vector_store=vector_store,
            top_k=top_k,
            score_threshold=score_threshold,
        )

    def _grade_step(state: RAGState) -> dict[str, Any]:
        return grade_evidence_node(state, client=client)

    def _generate_step(state: RAGState) -> dict[str, Any]:
        return generate_grounded_answer_node(state, client=client)

    def _rewrite_step(state: RAGState) -> dict[str, Any]:
        return rewrite_query_node(state, client=client)

    def _abstain_step(state: RAGState) -> dict[str, Any]:
        return abstain_node(state, client=client)

    def _clarify_step(state: RAGState) -> dict[str, Any]:
        return clarify_node(state, client=client)

    def _router(state: RAGState) -> str:
        return decide_next_step(state, max_retries=max_retries)

    builder.add_node("retrieve", _retrieve_step)
    builder.add_node("grade_evidence", _grade_step)
    builder.add_node("generate_answer", _generate_step)
    builder.add_node("rewrite_query", _rewrite_step)
    builder.add_node("abstain", _abstain_step)
    builder.add_node("clarify", _clarify_step)

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "grade_evidence")
    builder.add_conditional_edges(
        "grade_evidence",
        _router,
        {
            "generate_answer": "generate_answer",
            "rewrite_query": "rewrite_query",
            "clarify": "clarify",
            "abstain": "abstain",
        },
    )
    builder.add_edge("rewrite_query", "retrieve")
    builder.add_edge("generate_answer", END)
    builder.add_edge("abstain", END)
    builder.add_edge("clarify", END)

    return builder


def compile_rag_graph(
    *,
    vector_store: VectorStore | None = None,
    client: ModelClient | None = None,
    top_k: int = 3,
    score_threshold: float = 0.5,
    max_retries: int = 1,
    checkpointer: Any = None,
) -> CompiledStateGraph:
    """Build and compile the stateful LangGraph RAG execution graph."""
    graph_builder = build_rag_graph(
        vector_store=vector_store,
        client=client,
        top_k=top_k,
        score_threshold=score_threshold,
        max_retries=max_retries,
    )
    return graph_builder.compile(checkpointer=checkpointer)


def run_rag_pipeline(
    query: str,
    *,
    vector_store: VectorStore | None = None,
    client: ModelClient | None = None,
    top_k: int = 3,
    score_threshold: float = 0.5,
    max_retries: int = 1,
) -> RAGState:
    """Convenience entry point: execute the full RAG graph for a query."""
    compiled = compile_rag_graph(
        vector_store=vector_store,
        client=client,
        top_k=top_k,
        score_threshold=score_threshold,
        max_retries=max_retries,
    )
    initial_state = create_initial_state(query)
    result = compiled.invoke(initial_state)
    return result
