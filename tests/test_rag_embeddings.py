"""Tests for FastEmbed local embedding engine and cache verification."""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import patch

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.embeddings import (
    DEFAULT_CACHE_DIR,
    DEFAULT_EMBEDDING_DIM,
    LocalEmbeddingEngine,
)


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two float vectors."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def test_embedding_engine_initialization():
    """Verify default initialization parameters and cache directory creation."""
    engine = LocalEmbeddingEngine()
    assert engine.cache_dir == DEFAULT_CACHE_DIR
    assert engine.cache_dir.exists()
    assert engine.cache_dir.is_dir()


def test_embed_empty_texts():
    """Embedding empty sequences returns empty list."""
    engine = LocalEmbeddingEngine()
    assert engine.embed_texts([]) == []
    assert engine.embed_chunks([]) == []


def test_embed_single_query():
    """Embedding a query produces a valid 384-dimensional vector."""
    engine = LocalEmbeddingEngine()
    vector = engine.embed_query("Miesięczny limit na zakupy spożywcze")
    assert isinstance(vector, list)
    assert len(vector) == DEFAULT_EMBEDDING_DIM
    assert all(isinstance(x, float) for x in vector)
    # Check that vector is normalized (L2 norm approx 1.0)
    norm = math.sqrt(sum(x * x for x in vector))
    assert abs(norm - 1.0) < 0.01


def test_embed_chunks_pairing():
    """Embedding chunks pairs each DocumentChunk with its vector."""
    engine = LocalEmbeddingEngine()
    chunk1 = DocumentChunk(
        chunk_id="test#001",
        source="test.md",
        section="Limits",
        text="Limit wydatków na zakupy spożywcze wynosi 2500 PLN.",
        chunk_index=0,
        token_count=10,
    )
    chunk2 = DocumentChunk(
        chunk_id="test#002",
        source="test.md",
        section="Utilities",
        text="Faktury za prąd od PGE wystawiane są co miesiąc.",
        chunk_index=1,
        token_count=10,
    )

    pairs = engine.embed_chunks([chunk1, chunk2])
    assert len(pairs) == 2
    assert pairs[0][0].chunk_id == "test#001"
    assert len(pairs[0][1]) == DEFAULT_EMBEDDING_DIM
    assert pairs[1][0].chunk_id == "test#002"
    assert len(pairs[1][1]) == DEFAULT_EMBEDDING_DIM


def test_semantic_similarity_ranking():
    """Semantically related texts produce higher similarity than unrelated texts."""
    engine = LocalEmbeddingEngine()
    query_vec = engine.embed_query("Zduplikowana opłata za subskrypcję Spotify")

    spotify_vec = engine.embed_query(
        "Zasady postępowania w przypadku podwójnego obciążenia za Spotify"
    )
    power_vec = engine.embed_query(
        "Faktura za energię elektryczną i taryfy dostawcy prądu PGE"
    )

    sim_spotify = cosine_similarity(query_vec, spotify_vec)
    sim_power = cosine_similarity(query_vec, power_vec)

    assert sim_spotify > sim_power
    assert sim_spotify > 0.6


def test_offline_cached_execution(tmp_path: Path):
    """Verify embedding operates offline using cached weights without network."""
    engine = LocalEmbeddingEngine()
    # Ensure model is initialized and cached
    _ = engine.model

    # Block network requests completely
    with (
        patch("socket.socket.connect", side_effect=OSError("Network disabled")),
        patch("requests.get", side_effect=OSError("Network disabled")),
        patch("urllib.request.urlopen", side_effect=OSError("Network disabled")),
    ):
        vec = engine.embed_query("Test offline embedding capability")
        assert len(vec) == DEFAULT_EMBEDDING_DIM
