"""Vector store and semantic similarity search using FastEmbed embeddings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from agent_project.rag.chunking import DocumentChunk, load_and_chunk_documents
from agent_project.rag.embeddings import LocalEmbeddingEngine


class SearchResult(BaseModel):
    """Result of a vector similarity search with chunk and similarity score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk: DocumentChunk = Field(description="Matched document chunk")
    score: float = Field(
        ge=-1.0,
        le=1.0,
        description="Cosine similarity score (from -1.0 to 1.0)"
    )

    @property
    def chunk_id(self) -> str:
        """Shortcut to chunk identifier."""
        return self.chunk.chunk_id

    @property
    def source(self) -> str:
        """Shortcut to source document filename."""
        return self.chunk.source

    @property
    def section(self) -> str:
        """Shortcut to section heading."""
        return self.chunk.section

    @property
    def text(self) -> str:
        """Shortcut to chunk content."""
        return self.chunk.text


class VectorStore:
    """In-memory vector store with cosine similarity ranking and persistence."""

    def __init__(
        self,
        engine: LocalEmbeddingEngine | None = None,
    ) -> None:
        self.engine = engine or LocalEmbeddingEngine()
        self._chunks: list[DocumentChunk] = []
        self._vectors: list[list[float]] = []
        self._matrix: np.ndarray | None = None

    def __len__(self) -> int:
        """Return the number of indexed chunks."""
        return len(self._chunks)

    @property
    def chunks(self) -> list[DocumentChunk]:
        """Return all indexed document chunks."""
        return list(self._chunks)

    @property
    def vectors(self) -> list[list[float]]:
        """Return all indexed embedding vectors."""
        return list(self._vectors)

    def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        """Embed and append new DocumentChunk objects to the index."""
        if not chunks:
            return

        pairs = self.engine.embed_chunks(chunks)
        for chunk, vector in pairs:
            self._chunks.append(chunk)
            self._vectors.append(vector)

        self._matrix = np.array(self._vectors, dtype=np.float32)

    def search_by_vector(
        self,
        query_vector: list[float],
        *,
        top_k: int = 3,
        score_threshold: float = 0.5,
    ) -> list[SearchResult]:
        """Perform cosine similarity search given a query embedding vector."""
        if self._matrix is None or len(self._chunks) == 0:
            return []

        q_vec = np.array(query_vector, dtype=np.float32)
        norm_q = np.linalg.norm(q_vec)
        if norm_q > 0:
            q_vec = q_vec / norm_q

        # Matrix rows are normalized embeddings from FastEmbed
        norm_matrix = np.linalg.norm(self._matrix, axis=1, keepdims=True)
        norm_matrix = np.where(norm_matrix == 0, 1.0, norm_matrix)
        normalized_matrix = self._matrix / norm_matrix

        scores = np.dot(normalized_matrix, q_vec)

        # Sort indices by score descending
        sorted_indices = np.argsort(-scores)

        results: list[SearchResult] = []
        for idx in sorted_indices:
            score = float(scores[idx])
            if score < score_threshold:
                continue
            results.append(
                SearchResult(
                    chunk=self._chunks[idx],
                    score=round(score, 4),
                )
            )
            if len(results) >= top_k:
                break

        return results

    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        score_threshold: float = 0.5,
    ) -> list[SearchResult]:
        """Embed search query and find top-k relevant document chunks."""
        clean_query = query.strip()
        if not clean_query:
            return []

        query_vector = self.engine.embed_query(clean_query)
        return self.search_by_vector(
            query_vector=query_vector,
            top_k=top_k,
            score_threshold=score_threshold,
        )

    def save(self, destination: Path | str) -> None:
        """Persist index chunks and vectors to a JSON file."""
        dest_path = Path(destination)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        payload: dict[str, Any] = {
            "model_name": self.engine.model_name,
            "chunks": [chunk.model_dump() for chunk in self._chunks],
            "vectors": self._vectors,
        }
        dest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(
        cls,
        source: Path | str,
        engine: LocalEmbeddingEngine | None = None,
    ) -> VectorStore:
        """Load persisted index chunks and vectors from a JSON file."""
        src_path = Path(source)
        if not src_path.exists():
            raise FileNotFoundError(f"Index file not found: {source}")

        data = json.loads(src_path.read_text(encoding="utf-8"))
        store = cls(engine=engine)
        store._chunks = [DocumentChunk.model_validate(c) for c in data["chunks"]]
        store._vectors = data["vectors"]
        if store._vectors:
            store._matrix = np.array(store._vectors, dtype=np.float32)
        return store


def build_vector_store_from_directory(
    directory: Path | str,
    glob_pattern: str = "*.md",
    *,
    engine: LocalEmbeddingEngine | None = None,
    max_chunk_tokens: int = 400,
    overlap_tokens: int = 50,
) -> VectorStore:
    """Build and populate a VectorStore directly from documents in a directory."""
    chunks = load_and_chunk_documents(
        directory=directory,
        glob_pattern=glob_pattern,
        max_chunk_tokens=max_chunk_tokens,
        overlap_tokens=overlap_tokens,
    )
    store = VectorStore(engine=engine)
    store.add_chunks(chunks)
    return store
