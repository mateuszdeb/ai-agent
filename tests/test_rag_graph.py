"""Tests for LangGraph execution graph and loop bounding."""

from __future__ import annotations

from typing import Any

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.graph import (
    build_rag_graph,
    compile_rag_graph,
    decide_next_step,
    run_rag_pipeline,
)
from agent_project.rag.state import create_initial_state
from agent_project.rag.vector_store import (
    SearchResult,
    build_vector_store_from_directory,
)


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


class MockModelClient:
    """Mock client returning canned text responses."""

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {"text": self.text}


def _make_chunk(chunk_id: str, text: str, source: str = "doc.md") -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        source=source,
        section="Sekcja testowa",
        text=text,
        chunk_index=0,
        token_count=10,
    )


def test_decide_next_step_transitions():
    """Verify routing decision logic across all evidence statuses and retry counts."""
    state = create_initial_state("Pytanie")

    # 1. Complete -> generate_answer
    state["evidence_status"] = "complete"
    assert decide_next_step(state, max_retries=1) == "generate_answer"

    # 2. Ambiguous -> clarify
    state["evidence_status"] = "ambiguous"
    assert decide_next_step(state, max_retries=1) == "clarify"

    # 3. Lacks evidence -> abstain
    state["evidence_status"] = "lacks_evidence"
    assert decide_next_step(state, max_retries=1) == "abstain"

    # 4. Need better query: under retry budget -> rewrite_query
    state["evidence_status"] = "need_better_query"
    state["retry_count"] = 0
    assert decide_next_step(state, max_retries=1) == "rewrite_query"

    # 5. Need better query: at or above retry budget -> abstain
    state["retry_count"] = 1
    assert decide_next_step(state, max_retries=1) == "abstain"
    state["retry_count"] = 2
    assert decide_next_step(state, max_retries=1) == "abstain"

    # 6. None or unexpected -> fallback to abstain
    state["evidence_status"] = None
    assert decide_next_step(state, max_retries=1) == "abstain"


def test_build_rag_graph_structure():
    """Verify StateGraph contains all required nodes and compiles successfully."""
    builder = build_rag_graph()
    assert "retrieve" in builder.nodes
    assert "grade_evidence" in builder.nodes
    assert "generate_answer" in builder.nodes
    assert "rewrite_query" in builder.nodes
    assert "abstain" in builder.nodes
    assert "clarify" in builder.nodes

    compiled = builder.compile()
    assert compiled is not None


def test_graph_direct_path_complete():
    """Path 1: Complete evidence directly produces answer with citations."""
    chunk = _make_chunk(
        "budget#001",
        "Miesięczny limit na zakupy spożywcze to 2500 PLN.",
        source="household_budget_policy.md",
    )
    stub_store = StubVectorStore({
        "limit": [SearchResult(chunk=chunk, score=0.92)],
    })

    compiled = compile_rag_graph(vector_store=stub_store)  # type: ignore[arg-type]
    initial = create_initial_state("Jaki jest limit na zakupy spożywcze?")
    final_state = compiled.invoke(initial)

    assert final_state["evidence_status"] == "complete"
    assert final_state["retry_count"] == 0
    assert final_state["answer"] is not None
    assert "budget#001" in final_state["answer"]
    assert "budget#001" in final_state["citations"]
    assert len(final_state["retrieval_history"]) == 1


def test_graph_bounded_search_success():
    """Path 2: Partial evidence triggers query rewrite, second retrieval, and answer."""
    chunk_partial = _make_chunk(
        "spotify#001",
        "Spotify subskrypcja plan premium.",
        source="subscriptions_policy.md",
    )
    chunk_complete = _make_chunk(
        "spotify#002",
        "Procedura reklamacji opłat i duplikat transakcji Spotify opłata.",
        source="subscriptions_policy.md",
    )

    # First search matches 'spotify' with only partial chunk;
    # rewritten search with 'duplikat' matches complete chunk.
    stub_store = StubVectorStore({
        "duplikat": [SearchResult(chunk=chunk_complete, score=0.91)],
        "spotify": [SearchResult(chunk=chunk_partial, score=0.65)],
    })

    compiled = compile_rag_graph(vector_store=stub_store)  # type: ignore[arg-type]
    initial = create_initial_state("Spotify podwójna opłata")
    final_state = compiled.invoke(initial)

    # Should have retried exactly once and then completed with grounded answer
    assert final_state["retry_count"] == 1
    assert len(final_state["retrieval_history"]) == 2
    assert final_state["evidence_status"] == "complete"
    assert final_state["answer"] is not None
    assert len(final_state["citations"]) > 0


def test_graph_bounded_search_max_retries_abstain():
    """Loop bounding: after max_retries reached, router abstains rather than looping."""
    chunk_insufficient = _make_chunk(
        "spotify#001",
        "Spotify subskrypcja.",
        source="subscriptions_policy.md",
    )
    # Both initial and rewritten search return only insufficient chunk
    stub_store = StubVectorStore({
        "spotify": [SearchResult(chunk=chunk_insufficient, score=0.55)],
    })

    compiled = compile_rag_graph(
        vector_store=stub_store,  # type: ignore[arg-type]
        max_retries=1,
    )
    initial = create_initial_state("Spotify duplikat reklamacja zwrot")
    final_state = compiled.invoke(initial)

    # Must terminate without infinite loop and end with abstain after 1 retry
    assert final_state["retry_count"] == 1
    assert len(final_state["retrieval_history"]) == 2
    assert final_state["answer"] is not None
    assert "Nie mogę odpowiedzieć" in final_state["answer"]
    assert final_state["citations"] == []


def test_graph_unanswerable_immediate_abstain():
    """Path 3: Topic absent from knowledge base immediately triggers abstain."""
    stub_store = StubVectorStore({})

    compiled = compile_rag_graph(vector_store=stub_store)  # type: ignore[arg-type]
    query = "Jak rozliczyć zyski ze sprzedaży Bitcoin na giełdzie?"
    initial = create_initial_state(query)
    final_state = compiled.invoke(initial)

    assert final_state["evidence_status"] == "lacks_evidence"
    assert final_state["retry_count"] == 0
    assert final_state["answer"] is not None
    ans_low = final_state["answer"].lower()
    assert "kryptowalut" in ans_low or "bitcoin" in ans_low
    assert final_state["citations"] == []


def test_graph_ambiguous_clarify():
    """Path 4: Ambiguous query routes to clarify node."""
    stub_store = StubVectorStore({})

    compiled = compile_rag_graph(vector_store=stub_store)  # type: ignore[arg-type]
    initial = create_initial_state("faktura")
    final_state = compiled.invoke(initial)

    assert final_state["evidence_status"] == "ambiguous"
    assert final_state["retry_count"] == 0
    assert final_state["clarification_question"] is not None
    assert "faktura" in final_state["clarification_question"].lower()
    assert final_state["citations"] == []


def test_graph_with_mock_client():
    """Verify execution graph with injected ModelClient for model-generated answer."""
    chunk = _make_chunk(
        "pge#001",
        "Termin płatności faktury PGE wynosi 14 dni od wystawienia.",
        source="utility_contracts_faq.md",
    )
    stub_store = StubVectorStore({
        "pge": [SearchResult(chunk=chunk, score=0.95)],
    })

    mock_client = MockModelClient(
        "Termin płatności to 14 dni [source: pge#001]."
    )

    final_state = run_rag_pipeline(
        "Jaki jest termin płatności PGE?",
        vector_store=stub_store,  # type: ignore[arg-type]
        client=mock_client,  # type: ignore[arg-type]
    )

    assert final_state["evidence_status"] == "complete"
    assert final_state["answer"] == "Termin płatności to 14 dni [source: pge#001]."
    assert final_state["citations"] == ["pge#001"]


def test_graph_with_real_vector_store():
    """Full integration test: build VectorStore from directory and run pipeline."""
    store = build_vector_store_from_directory("data/documents")
    assert len(store) > 0

    final_state = run_rag_pipeline(
        "Jaki jest miesięczny limit na zakupy spożywcze w Biedronce?",
        vector_store=store,
    )

    assert final_state["evidence_status"] == "complete"
    assert final_state["answer"] is not None
    assert len(final_state["citations"]) > 0
    assert len(final_state["retrieval_history"]) >= 1
