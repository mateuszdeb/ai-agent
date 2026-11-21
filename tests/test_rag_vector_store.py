"""Tests for vector store, cosine search, and index persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.embeddings import LocalEmbeddingEngine
from agent_project.rag.vector_store import (
    SearchResult,
    VectorStore,
    build_vector_store_from_directory,
)


@pytest.fixture(scope="module")
def shared_engine() -> LocalEmbeddingEngine:
    """Share embedding engine across module tests to avoid re-initializing."""
    return LocalEmbeddingEngine()


def test_empty_vector_store(shared_engine: LocalEmbeddingEngine):
    """Empty vector store returns empty search results and length 0."""
    store = VectorStore(engine=shared_engine)
    assert len(store) == 0
    assert store.chunks == []
    assert store.search("Any query") == []
    assert store.search_by_vector([0.1] * 384) == []


def test_vector_store_add_and_search(shared_engine: LocalEmbeddingEngine):
    """Verify adding chunks and cosine similarity ranking."""
    store = VectorStore(engine=shared_engine)
    chunk1 = DocumentChunk(
        chunk_id="budget#001",
        source="household_budget_policy.md",
        section="Miesięczne limity kwotowe",
        text="Limit na artykuły spożywcze wynosi 2500 PLN.",
        chunk_index=0,
        token_count=8,
    )
    chunk2 = DocumentChunk(
        chunk_id="spotify#001",
        source="subscriptions_policy.md",
        section="Duplikaty",
        text="Zduplikowane obciążenie za Spotify traktowane jest jako błąd.",
        chunk_index=1,
        token_count=9,
    )
    store.add_chunks([chunk1, chunk2])
    assert len(store) == 2

    # Query matching chunk2
    results = store.search("Zduplikowana płatność za Spotify", top_k=1)
    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].chunk_id == "spotify#001"
    assert results[0].source == "subscriptions_policy.md"
    assert results[0].score > 0.6


def test_search_score_threshold_and_top_k(shared_engine: LocalEmbeddingEngine):
    """Verify top_k limiting and score_threshold cutoff."""
    store = VectorStore(engine=shared_engine)
    chunks = [
        DocumentChunk(
            chunk_id=f"c#{i}",
            source="doc.md",
            section="Section",
            text=f"Prąd i energia elektryczna PGE rachunek numer {i}",
            chunk_index=i,
            token_count=10,
        )
        for i in range(5)
    ]
    store.add_chunks(chunks)

    # Top-k limiting
    res_k2 = store.search("Faktura PGE prąd", top_k=2, score_threshold=0.1)
    assert len(res_k2) == 2

    # High threshold cutoff
    res_strict = store.search("Faktura PGE prąd", score_threshold=0.999)
    assert len(res_strict) == 0


def test_vector_store_persistence(
    shared_engine: LocalEmbeddingEngine,
    tmp_path: Path,
):
    """Verify saving index to disk and reloading produces identical results."""
    store = VectorStore(engine=shared_engine)
    chunk = DocumentChunk(
        chunk_id="warranty#001",
        source="warranties.md",
        section="Rękojmia",
        text="Paragon należy trzymać przez 24 miesiące.",
        chunk_index=0,
        token_count=7,
    )
    store.add_chunks([chunk])

    save_path = tmp_path / "test_index.json"
    store.save(save_path)
    assert save_path.exists()

    loaded = VectorStore.load(save_path, engine=shared_engine)
    assert len(loaded) == 1
    assert loaded.chunks[0].chunk_id == "warranty#001"

    # Search on loaded index works identically
    res = loaded.search("Okres rękojmi i paragon", top_k=1)
    assert len(res) == 1
    assert res[0].chunk_id == "warranty#001"


def test_build_vector_store_from_directory(shared_engine: LocalEmbeddingEngine):
    """Verify end-to-end building from real data/documents directory."""
    docs_dir = Path("data/documents")
    assert docs_dir.exists()

    store = build_vector_store_from_directory(
        directory=docs_dir,
        engine=shared_engine,
    )
    assert len(store) >= 4

    # Search 1: Electricity bill
    res_pge = store.search(
        "Termin płatności faktury za prąd PGE Dystrybucja",
        top_k=1,
    )
    assert len(res_pge) == 1
    assert res_pge[0].source == "utility_contracts_faq.md"
    assert "PGE" in res_pge[0].text
    assert res_pge[0].score > 0.7

    # Search 2: Groceries limit
    res_budget = store.search("Jaki jest miesięczny limit na jedzenie?", top_k=1)
    assert len(res_budget) == 1
    assert res_budget[0].source == "household_budget_policy.md"
    assert res_budget[0].score > 0.5

    # Search 3: Unrelated topic gets low score / no results above 0.75 threshold
    res_unrelated = store.search(
        "Kupowanie bitcoinów i kryptowalut na giełdzie",
        top_k=3,
        score_threshold=0.75,
    )
    assert len(res_unrelated) == 0
