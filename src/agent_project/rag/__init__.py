"""Retrieval-augmented generation for household expense questions."""

from __future__ import annotations

from agent_project.rag.chunking import (
    DocumentChunk,
    estimate_tokens,
    load_and_chunk_documents,
    split_markdown_into_chunks,
)
from agent_project.rag.citations import (
    VerificationResult,
    verify_and_filter_citations,
    verify_citations_node,
)
from agent_project.rag.embeddings import (
    DEFAULT_CACHE_DIR,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_MODEL_NAME,
    LocalEmbeddingEngine,
)
from agent_project.rag.eval_dataset import (
    DEFAULT_EVAL_DATASET_PATH,
    EvalQueryItem,
    load_evaluation_dataset,
)
from agent_project.rag.graph import (
    build_rag_graph,
    compile_rag_graph,
    decide_next_step,
    run_rag_pipeline,
)
from agent_project.rag.state import (
    EvidenceStatus,
    RAGState,
    RetrievalRecord,
    create_initial_state,
    record_retrieval,
    validate_rag_state,
)
from agent_project.rag.vector_store import (
    SearchResult,
    VectorStore,
    build_vector_store_from_directory,
)

__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_EVAL_DATASET_PATH",
    "DEFAULT_MODEL_NAME",
    "DocumentChunk",
    "EvalQueryItem",
    "EvidenceStatus",
    "LocalEmbeddingEngine",
    "RAGState",
    "RetrievalRecord",
    "SearchResult",
    "VectorStore",
    "VerificationResult",
    "build_rag_graph",
    "build_vector_store_from_directory",
    "compile_rag_graph",
    "create_initial_state",
    "decide_next_step",
    "estimate_tokens",
    "load_and_chunk_documents",
    "load_evaluation_dataset",
    "record_retrieval",
    "run_rag_pipeline",
    "split_markdown_into_chunks",
    "validate_rag_state",
    "verify_and_filter_citations",
    "verify_citations_node",
]

