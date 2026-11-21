"""Local embedding engine utilizing FastEmbed with on-disk caching."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from fastembed import TextEmbedding

from agent_project.rag.chunking import DocumentChunk

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
DEFAULT_CACHE_DIR = Path(".cache/fastembed")
DEFAULT_EMBEDDING_DIM = 384


class LocalEmbeddingEngine:
    """Local text embedding engine powered by FastEmbed with cached weights."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        cache_dir: Path | str | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._model: TextEmbedding | None = None

    @property
    def model(self) -> TextEmbedding:
        """Lazily initialize the underlying FastEmbed model."""
        if self._model is None:
            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(self.cache_dir),
            )
        return self._model

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Generate normalized embeddings for a sequence of texts."""
        if not texts:
            return []
        raw_embeddings = self.model.embed(list(texts))
        return [vector.tolist() for vector in raw_embeddings]

    def embed_query(self, query: str) -> list[float]:
        """Generate an embedding vector for a single search query."""
        results = self.embed_texts([query])
        if not results:
            return [0.0] * DEFAULT_EMBEDDING_DIM
        return results[0]

    def embed_chunks(
        self,
        chunks: Sequence[DocumentChunk],
    ) -> list[tuple[DocumentChunk, list[float]]]:
        """Generate embeddings for DocumentChunk objects paired with metadata."""
        if not chunks:
            return []
        texts = [chunk.text for chunk in chunks]
        vectors = self.embed_texts(texts)
        return list(zip(chunks, vectors, strict=True))
