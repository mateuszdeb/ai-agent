"""Tests for chunking module and source metadata preservation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_project.rag.chunking import (
    DocumentChunk,
    estimate_tokens,
    load_and_chunk_documents,
    split_markdown_into_chunks,
)


def test_document_chunk_model_validation():
    """Verify DocumentChunk enforces strict fields and immutability."""
    chunk = DocumentChunk(
        chunk_id="doc#001",
        source="doc.md",
        section="Intro",
        text="Sample text content.",
        chunk_index=0,
        token_count=3,
    )
    assert chunk.chunk_id == "doc#001"
    assert chunk.source == "doc.md"
    assert chunk.section == "Intro"
    assert chunk.text == "Sample text content."
    assert chunk.chunk_index == 0
    assert chunk.token_count == 3
    assert chunk.metadata == {}

    # Immutable / frozen
    with pytest.raises(ValidationError):
        chunk.text = "New text"  # type: ignore[misc]

    # Forbids extra fields
    with pytest.raises(ValidationError):
        DocumentChunk(
            chunk_id="doc#002",
            source="doc.md",
            section="Intro",
            text="Text",
            chunk_index=1,
            token_count=1,
            extra_field="invalid",  # type: ignore[call-arg]
        )


def test_split_markdown_empty_or_whitespace():
    """Empty or whitespace-only documents produce zero chunks."""
    assert split_markdown_into_chunks("", source="empty.md") == []
    assert split_markdown_into_chunks("   \n\n  ", source="blank.md") == []


def test_split_markdown_without_headers():
    """Documents without markdown headers produce a General section chunk."""
    text = "Just a plain paragraph without any headers.\nAnother line of text."
    chunks = split_markdown_into_chunks(text, source="plain.txt")
    assert len(chunks) == 1
    assert chunks[0].source == "plain.txt"
    assert chunks[0].section == "General"
    assert chunks[0].chunk_id == "plain#000"
    assert "plain paragraph" in chunks[0].text
    assert chunks[0].token_count > 0


def test_split_markdown_preserves_source_and_sections():
    """Verify every chunk retains the exact source filename and section heading."""
    text = """
Preamble text before any header.

# Main Title

Introductory text under main title.

## Section 1: Limits

Rules about spending limits: maximum 500 PLN.

## Section 2: Exceptions

Rules about exceptions: only emergency fund.
"""
    chunks = split_markdown_into_chunks(text, source="policy.md")
    assert len(chunks) >= 3

    # Every chunk must retain the source filename
    for chunk in chunks:
        assert chunk.source == "policy.md"
        assert chunk.chunk_id.startswith("policy#")
        assert chunk.token_count > 0

    # Preamble preserved
    assert chunks[0].section == "Preamble"
    assert "Preamble text" in chunks[0].text

    # Section headings preserved
    sections = [c.section for c in chunks]
    assert "Main Title" in sections
    assert "Section 1: Limits" in sections
    assert "Section 2: Exceptions" in sections


def test_split_long_section_with_overlap():
    """Sections exceeding max_chunk_tokens are split with overlapping paragraphs."""
    paragraphs = [
        f"Paragraph {i}: " + "word " * 60
        for i in range(1, 10)
    ]
    long_text = "## Long Section\n\n" + "\n\n".join(paragraphs)

    chunks = split_markdown_into_chunks(
        long_text,
        source="long_doc.md",
        max_chunk_tokens=100,
        overlap_tokens=30,
    )

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.source == "long_doc.md"
        assert chunk.section == "Long Section"
        assert chunk.token_count > 0


def test_load_and_chunk_documents_deterministic(tmp_path: Path):
    """File sorting ensures deterministic chunk indices across runs."""
    doc_b = tmp_path / "b_document.md"
    doc_b.write_text("## Doc B\n\nContent for B.")

    doc_a = tmp_path / "a_document.md"
    doc_a.write_text("## Doc A\n\nContent for A.")

    chunks = load_and_chunk_documents(tmp_path)
    assert len(chunks) == 2
    # doc_a must be chunked first due to alphabetical sort
    assert chunks[0].source == "a_document.md"
    assert chunks[0].chunk_id == "a_document#000"
    assert chunks[1].source == "b_document.md"
    assert chunks[1].chunk_id == "b_document#000"


def test_load_and_chunk_nonexistent_directory():
    """Non-existent directory raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_and_chunk_documents("non_existent_folder_xyz_123")


def test_load_and_chunk_actual_domain_documents():
    """Verify chunking loads the 4 real domain documents prepared in Task 1."""
    docs_dir = Path("data/documents")
    assert docs_dir.exists(), "data/documents must exist"

    chunks = load_and_chunk_documents(docs_dir)
    assert len(chunks) >= 4, "Expected multiple chunks across domain documents"

    sources = {c.source for c in chunks}
    expected_sources = {
        "household_budget_policy.md",
        "subscriptions_policy.md",
        "utility_contracts_faq.md",
        "warranties_and_receipts.md",
    }
    assert expected_sources.issubset(sources)

    # Validate that every chunk has valid text, chunk_id, section, and tokens
    for chunk in chunks:
        assert chunk.chunk_id
        assert chunk.source in expected_sources
        assert chunk.section
        assert len(chunk.text) > 0
        assert chunk.token_count > 0


def test_estimate_tokens():
    """Check token estimation heuristic."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("   ") == 0
    assert estimate_tokens("One two three") == 3
    long_word = "a" * 40
    assert estimate_tokens(long_word) >= 10
