"""CLI and programmatic entrypoint for indexing domain documents into VectorStore."""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_project.rag.vector_store import (
    VectorStore,
    build_vector_store_from_directory,
)

DEFAULT_DOCUMENTS_DIR = Path("data/documents")
DEFAULT_INDEX_PATH = Path("data/vector_index.json")


def index_documents(
    documents_dir: Path | str = DEFAULT_DOCUMENTS_DIR,
    output_path: Path | str | None = DEFAULT_INDEX_PATH,
    glob_pattern: str = "*.md",
) -> VectorStore:
    """Index documents from a directory and optionally persist the index to disk."""
    doc_path = Path(documents_dir)
    if not doc_path.exists():
        raise FileNotFoundError(f"Documents directory not found: {documents_dir}")

    store = build_vector_store_from_directory(doc_path, glob_pattern=glob_pattern)

    if output_path is not None:
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        store.save(out_file)

    return store


def main() -> None:
    """CLI entrypoint for building and persisting the domain vector index."""
    parser = argparse.ArgumentParser(
        description="Index domain knowledge documents for RAG."
    )
    parser.add_argument(
        "--docs-dir",
        type=str,
        default=str(DEFAULT_DOCUMENTS_DIR),
        help="Path to markdown documents directory",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_INDEX_PATH),
        help="Path to save persisted JSON vector index",
    )
    parser.add_argument(
        "--glob",
        type=str,
        default="*.md",
        help="File matching glob pattern for documents",
    )
    args = parser.parse_args()

    doc_path = Path(args.docs_dir)
    doc_count = len(list(doc_path.glob(args.glob)))

    print("=" * 60)
    print("         BUILDING RAG VECTOR INDEX (FastEmbed)")
    print("=" * 60)
    print(f"Documents Directory: {args.docs_dir}")
    print(f"Documents Found:     {doc_count}")

    store = index_documents(
        documents_dir=args.docs_dir,
        output_path=args.output,
        glob_pattern=args.glob,
    )

    print(f"Total Chunks:        {len(store.chunks)}")
    print(f"Total Vectors:       {len(store.vectors)}")
    if store.vectors:
        print(f"Vector Dimension:    {len(store.vectors[0])}")
    print(f"Index Persisted To:  {args.output}")
    print("=" * 60)
    print("INDEXING COMPLETED SUCCESSFULLY (EXIT CODE 0)")
    print("=" * 60)


if __name__ == "__main__":
    main()
