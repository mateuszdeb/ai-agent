"""Verify RAG citations and filter unsupported references."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.state import RAGState

_CITATION_PATTERN = re.compile(r"\[source:\s*([A-Za-z0-9_#.-]+)\]", re.IGNORECASE)


class VerificationResult(BaseModel):
    """Detailed outcome of programmatic citation verification against chunks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_valid: bool = Field(
        description="True if all citations match retrieved chunks (no hallucinations)"
    )
    valid_citations: list[str] = Field(
        default_factory=list,
        description="Unique cited identifiers verified against retrieved chunks",
    )
    invalid_citations: list[str] = Field(
        default_factory=list,
        description="Hallucinated citations not present in retrieved chunks",
    )
    cleaned_answer: str = Field(
        description="Answer text with invalid citation tags removed and clean spacing",
    )
    removed_citations: list[str] = Field(
        default_factory=list,
        description="Exact citation tags or IDs stripped from the answer text",
    )
    has_citations: bool = Field(
        description="True if original answer contained at least one citation tag"
    )
    hallucination_detected: bool = Field(
        description="True if one or more cited sources were not in retrieved chunks"
    )


def _clean_invalid_citation_tags(text: str, invalid_citations: set[str]) -> str:
    """Strip invalid [source: ...] tags from text and normalize punctuation spacing."""
    if not invalid_citations:
        return text

    invalid_lower = {c.lower() for c in invalid_citations}

    def _replace(match: re.Match[str]) -> str:
        cited = match.group(1).strip()
        if cited.lower() in invalid_lower:
            return ""
        return match.group(0)

    cleaned = _CITATION_PATTERN.sub(_replace, text)
    # Fix whitespace preceding punctuation marks
    cleaned = re.sub(r"[ \t]+([.,;!?])", r"\1", cleaned)
    # Collapse multiple inline spaces
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    # Strip each line and collapse excess empty lines
    lines = [line.strip() for line in cleaned.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def build_valid_identifier_set(retrieved_chunks: list[DocumentChunk]) -> set[str]:
    """Build a comprehensive set of valid lowercase identifiers for retrieved chunks."""
    valid_ids: set[str] = set()
    for chunk in retrieved_chunks:
        # Full chunk ID e.g. "budget#001"
        c_id = chunk.chunk_id.lower().strip()
        valid_ids.add(c_id)

        # Full source filename e.g. "household_budget_policy.md"
        src = chunk.source.lower().strip()
        valid_ids.add(src)

        # Source stem without extension e.g. "household_budget_policy"
        stem = src.rsplit(".", 1)[0]
        valid_ids.add(stem)

        # Combined formats: source#chunk_id or source#index
        valid_ids.add(f"{src}#{c_id}")
        valid_ids.add(f"{stem}#{c_id}")
        valid_ids.add(f"{src}#{chunk.chunk_index}")
        valid_ids.add(f"{stem}#{chunk.chunk_index}")

    return valid_ids


def verify_and_filter_citations(
    answer: str,
    retrieved_chunks: list[DocumentChunk],
    *,
    strip_invalid: bool = True,
    fallback_to_abstain: bool = False,
    abstain_message: str | None = None,
) -> VerificationResult:
    """Programmatically verify citations and filter hallucinations.

    - Extracts all [source: ...] citations from answer text.
    - Matches each cited source against chunk_ids and source filenames.
    - Distinguishes valid citations from hallucinations.
    - If strip_invalid=True, removes hallucinated citation tags from text.
    - If fallback_to_abstain=True and hallucinations detected, replaces answer.
    """
    clean_answer = (answer or "").strip()
    if not clean_answer:
        return VerificationResult(
            is_valid=True,
            valid_citations=[],
            invalid_citations=[],
            cleaned_answer="",
            removed_citations=[],
            has_citations=False,
            hallucination_detected=False,
        )

    raw_matches = _CITATION_PATTERN.findall(clean_answer)
    if not raw_matches:
        return VerificationResult(
            is_valid=True,
            valid_citations=[],
            invalid_citations=[],
            cleaned_answer=clean_answer,
            removed_citations=[],
            has_citations=False,
            hallucination_detected=False,
        )

    valid_id_set = build_valid_identifier_set(retrieved_chunks)

    valid_citations: list[str] = []
    invalid_citations: list[str] = []
    seen_valid: set[str] = set()
    seen_invalid: set[str] = set()

    for match in raw_matches:
        clean_match = match.strip()
        if clean_match.lower() in valid_id_set:
            if clean_match not in seen_valid:
                seen_valid.add(clean_match)
                valid_citations.append(clean_match)
        else:
            if clean_match not in seen_invalid:
                seen_invalid.add(clean_match)
                invalid_citations.append(clean_match)

    hallucination_detected = len(invalid_citations) > 0
    is_valid = not hallucination_detected

    if hallucination_detected and fallback_to_abstain:
        invalid_desc = ", ".join(invalid_citations)
        default_refusal = (
            "Nie mogę potwierdzić odpowiedzi na podstawie dostępnych dokumentów. "
            f"Wykryto nieprawidłowe cytowania: {invalid_desc}."
        )
        final_answer = abstain_message or default_refusal
        return VerificationResult(
            is_valid=False,
            valid_citations=[],
            invalid_citations=invalid_citations,
            cleaned_answer=final_answer,
            removed_citations=invalid_citations,
            has_citations=True,
            hallucination_detected=True,
        )

    if hallucination_detected and strip_invalid:
        cleaned_text = _clean_invalid_citation_tags(
            clean_answer, set(invalid_citations)
        )
        removed = list(invalid_citations)
    else:
        cleaned_text = clean_answer
        removed = []

    return VerificationResult(
        is_valid=is_valid,
        valid_citations=valid_citations,
        invalid_citations=invalid_citations,
        cleaned_answer=cleaned_text,
        removed_citations=removed,
        has_citations=True,
        hallucination_detected=hallucination_detected,
    )


def verify_citations_node(
    state: RAGState,
    *,
    fallback_to_abstain: bool = False,
) -> dict[str, Any]:
    """LangGraph execution node: Verify citations in state['answer'] and sanitize."""
    raw_answer = state.get("answer") or ""
    documents = state.get("documents", [])

    verification = verify_and_filter_citations(
        raw_answer,
        documents,
        fallback_to_abstain=fallback_to_abstain,
    )

    updates: dict[str, Any] = {
        "answer": verification.cleaned_answer,
        "citations": verification.valid_citations,
        "step_count": state.get("step_count", 0) + 1,
    }

    if verification.hallucination_detected and fallback_to_abstain:
        updates["evidence_status"] = "lacks_evidence"
        updates["missing_evidence"] = [
            f"Wykryto halucynację cytowań: {', '.join(verification.invalid_citations)}"
        ]

    return updates
