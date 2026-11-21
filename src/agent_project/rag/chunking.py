"""Deterministic chunking and metadata preservation for RAG documents."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_HEADER_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


class DocumentChunk(BaseModel):
    """A searchable fragment of a document with preserved source metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(
        description="Deterministic unique chunk identifier, e.g. 'policy#001'"
    )
    source: str = Field(
        description="Source document name or relative path, e.g. 'policy.md'"
    )
    section: str = Field(
        description="Section heading or title under which this text appears"
    )
    text: str = Field(
        description="Clean text content of the chunk"
    )
    chunk_index: int = Field(
        ge=0,
        description="0-based sequential index of the chunk within the document"
    )
    token_count: int = Field(
        ge=0,
        description="Estimated token count for retrieval budget monitoring"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured metadata (e.g. char length, tags)"
    )


def estimate_tokens(text: str) -> int:
    """Estimate token count for a text using a whitespace and character heuristic."""
    cleaned = text.strip()
    if not cleaned:
        return 0
    words = len(cleaned.split())
    by_chars = max(1, len(cleaned) // 4)
    return max(words, by_chars)


def _split_long_section(
    section_text: str,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Split a large section text into paragraphs or sub-chunks with overlap."""
    paragraphs = [p.strip() for p in section_text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in section_text.splitlines() if p.strip()]
    if not paragraphs:
        return [section_text.strip()] if section_text.strip() else []

    sub_chunks: list[str] = []
    current_paras: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = estimate_tokens(para)
        if current_paras and (current_tokens + para_tokens > max_tokens):
            sub_chunks.append("\n\n".join(current_paras))
            # Keep overlap paragraphs from the tail if possible
            overlap_paras: list[str] = []
            overlap_count = 0
            for prev in reversed(current_paras):
                prev_tok = estimate_tokens(prev)
                if overlap_count + prev_tok <= overlap_tokens:
                    overlap_paras.insert(0, prev)
                    overlap_count += prev_tok
                else:
                    break
            current_paras = overlap_paras + [para]
            current_tokens = sum(estimate_tokens(p) for p in current_paras)
        else:
            current_paras.append(para)
            current_tokens += para_tokens

    if current_paras:
        sub_chunks.append("\n\n".join(current_paras))

    return sub_chunks


def split_markdown_into_chunks(
    text: str,
    source: str,
    *,
    max_chunk_tokens: int = 400,
    overlap_tokens: int = 50,
) -> list[DocumentChunk]:
    """Split a Markdown document into section-grounded, searchable chunks.

    Guarantees:
    1. Every chunk retains the original `source` file name.
    2. Every chunk retains the closest Markdown section heading.
    3. Generates deterministic `chunk_id` in format '{stem}#{index:03d}'.
    4. Chunk text is never empty and includes estimated token counts.
    """
    clean_text = text.strip()
    if not clean_text:
        return []

    stem = Path(source).stem.replace(" ", "_")
    matches = list(_HEADER_PATTERN.finditer(clean_text))

    sections: list[tuple[str, str]] = []

    if not matches:
        # Document without Markdown headers
        sections.append(("General", clean_text))
    else:
        # Content before first header
        first_start = matches[0].start()
        preamble = clean_text[:first_start].strip()
        if preamble:
            sections.append(("Preamble", preamble))

        for i, match in enumerate(matches):
            header_title = match.group(2).strip()
            content_start = match.end()
            content_end = (
                matches[i + 1].start() if i + 1 < len(matches) else len(clean_text)
            )
            section_body = clean_text[content_start:content_end].strip()
            full_section_content = (
                f"{header_title}\n\n{section_body}" if section_body else header_title
            )
            sections.append((header_title, full_section_content))

    chunks: list[DocumentChunk] = []
    chunk_index = 0

    for section_title, section_body in sections:
        if not section_body.strip():
            continue

        body_tokens = estimate_tokens(section_body)
        if body_tokens <= max_chunk_tokens:
            chunk_pieces = [section_body]
        else:
            chunk_pieces = _split_long_section(
                section_body,
                max_tokens=max_chunk_tokens,
                overlap_tokens=overlap_tokens,
            )

        for piece in chunk_pieces:
            clean_piece = piece.strip()
            if not clean_piece:
                continue
            chunk_id = f"{stem}#{chunk_index:03d}"
            chunk = DocumentChunk(
                chunk_id=chunk_id,
                source=source,
                section=section_title,
                text=clean_piece,
                chunk_index=chunk_index,
                token_count=estimate_tokens(clean_piece),
                metadata={
                    "char_count": len(clean_piece),
                    "source_file": source,
                },
            )
            chunks.append(chunk)
            chunk_index += 1

    return chunks


def load_and_chunk_documents(
    directory: Path | str,
    glob_pattern: str = "*.md",
    *,
    max_chunk_tokens: int = 400,
    overlap_tokens: int = 50,
) -> list[DocumentChunk]:
    """Load all matching documents from a directory and chunk them deterministically.

    Files are sorted alphabetically by filename to guarantee consistent chunk ordering
    and deterministic chunk identifiers across runs.
    """
    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        raise FileNotFoundError(f"Document directory not found: {directory}")

    files = sorted(dir_path.glob(glob_pattern))
    all_chunks: list[DocumentChunk] = []

    for file_path in files:
        if not file_path.is_file():
            continue
        text = file_path.read_text(encoding="utf-8")
        chunks = split_markdown_into_chunks(
            text=text,
            source=file_path.name,
            max_chunk_tokens=max_chunk_tokens,
            overlap_tokens=overlap_tokens,
        )
        all_chunks.extend(chunks)

    return all_chunks
