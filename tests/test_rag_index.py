"""Tests for document indexing entrypoint (rag/index.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent_project.rag.index import index_documents, main
from agent_project.rag.vector_store import VectorStore


def test_index_documents_smoke(tmp_path: Path) -> None:
    """index_documents indexes markdown files and saves JSON index."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    doc_file = docs_dir / "policy.md"
    doc_file.write_text(
        "# Policy\n\nTermin płatności faktury wynosi 14 dni.",
        encoding="utf-8",
    )
    output_index = tmp_path / "index.json"

    store = index_documents(
        documents_dir=docs_dir,
        output_path=output_index,
        glob_pattern="*.md",
    )

    assert isinstance(store, VectorStore)
    assert len(store.chunks) >= 1
    assert len(store.vectors) >= 1
    assert output_index.exists()
    assert output_index.stat().st_size > 50


def test_index_main_cli(tmp_path: Path, capsys: object) -> None:
    """main() CLI entrypoint runs index build and prints summary."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "test.md").write_text(
        "# Test\n\nTreść dokumentu testowego.",
        encoding="utf-8",
    )
    output_index = tmp_path / "cli_index.json"

    test_args = [
        "index.py",
        "--docs-dir",
        str(docs_dir),
        "--output",
        str(output_index),
    ]

    with patch("sys.argv", test_args):
        main()

    assert output_index.exists()
