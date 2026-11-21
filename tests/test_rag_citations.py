"""Tests for programmatic citation verification and hallucination filtering."""

from __future__ import annotations

from typing import Any

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.citations import (
    VerificationResult,
    verify_and_filter_citations,
    verify_citations_node,
)
from agent_project.rag.nodes.generation import generate_grounded_answer_node
from agent_project.rag.state import create_initial_state


def _make_chunk(chunk_id: str, source: str, text: str) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        source=source,
        section="Sekcja testowa",
        text=text,
        chunk_index=0,
        token_count=10,
    )


class MockLLMClient:
    """Mock client returning canned text with mixed citations."""

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {"text": self.response_text}


def test_valid_citations_all_match():
    """Valid citations matching retrieved chunks are preserved and verified."""
    chunk1 = _make_chunk(
        "budget#001",
        "household_budget_policy.md",
        "Limit na zakupy to 2500 PLN.",
    )
    chunk2 = _make_chunk(
        "pge#002",
        "utility_contracts_faq.md",
        "Termin płatności to 14 dni.",
    )
    retrieved = [chunk1, chunk2]

    answer = (
        "Limit na jedzenie wynosi 2500 PLN [source: budget#001]. "
        "Faktura PGE ma termin 14 dni [source: pge#002]."
    )

    res = verify_and_filter_citations(answer, retrieved)

    assert isinstance(res, VerificationResult)
    assert res.is_valid is True
    assert res.hallucination_detected is False
    assert res.has_citations is True
    assert res.valid_citations == ["budget#001", "pge#002"]
    assert res.invalid_citations == []
    assert res.removed_citations == []
    assert res.cleaned_answer == answer


def test_hallucinated_citations_detected_and_filtered():
    """Hallucinated citations are identified, reported, and stripped from text."""
    chunk = _make_chunk(
        "budget#001",
        "household_budget_policy.md",
        "Limit na zakupy to 2500 PLN.",
    )
    retrieved = [chunk]

    answer = (
        "Limit na zakupy to 2500 PLN [source: budget#001]. "
        "Limit na kryptowaluty to 10000 PLN [source: crypto_fake#999]."
    )

    res = verify_and_filter_citations(answer, retrieved)

    assert res.is_valid is False
    assert res.hallucination_detected is True
    assert res.valid_citations == ["budget#001"]
    assert res.invalid_citations == ["crypto_fake#999"]
    assert res.removed_citations == ["crypto_fake#999"]
    # Hallucinated tag must be removed while valid tag is preserved
    assert "[source: budget#001]" in res.cleaned_answer
    assert "crypto_fake#999" not in res.cleaned_answer
    assert res.cleaned_answer.endswith("Limit na kryptowaluty to 10000 PLN.")


def test_answer_without_citations():
    """Answer with no citation tags is valid and reports has_citations=False."""
    chunk = _make_chunk(
        "budget#001",
        "household_budget_policy.md",
        "Limit na zakupy to 2500 PLN.",
    )
    answer = "Odpowiedź bez żadnych odnośników źródłowych."

    res = verify_and_filter_citations(answer, [chunk])

    assert res.is_valid is True
    assert res.has_citations is False
    assert res.hallucination_detected is False
    assert res.valid_citations == []
    assert res.invalid_citations == []
    assert res.cleaned_answer == answer


def test_all_citations_hallucinated():
    """Answer where all citations are hallucinated removes all tags."""
    chunk = _make_chunk(
        "pge#001",
        "utility_contracts_faq.md",
        "PGE taryfa G11.",
    )
    answer = (
        "Zmyślona informacja A [source: fake#001]. "
        "Zmyślona informacja B [source: fake#002]."
    )

    res = verify_and_filter_citations(answer, [chunk])

    assert res.is_valid is False
    assert res.hallucination_detected is True
    assert res.valid_citations == []
    assert res.invalid_citations == ["fake#001", "fake#002"]
    assert "fake#001" not in res.cleaned_answer
    assert "fake#002" not in res.cleaned_answer
    assert res.cleaned_answer == "Zmyślona informacja A. Zmyślona informacja B."


def test_fallback_to_abstain_on_hallucination():
    """Option fallback_to_abstain replaces answer when hallucination occurs."""
    chunk = _make_chunk(
        "budget#001",
        "household_budget_policy.md",
        "Limit na zakupy to 2500 PLN.",
    )
    answer = "Fakt zmyślony z fałszywym źródłem [source: imaginary_doc#777]."

    res = verify_and_filter_citations(
        answer,
        [chunk],
        fallback_to_abstain=True,
    )

    assert res.is_valid is False
    assert res.hallucination_detected is True
    assert res.valid_citations == []
    assert "Nie mogę potwierdzić odpowiedzi" in res.cleaned_answer
    assert "imaginary_doc#777" in res.cleaned_answer


def test_case_insensitivity_and_source_variants():
    """Verification supports case-insensitivity and source filename matching."""
    chunk = _make_chunk(
        "budget#001",
        "household_budget_policy.md",
        "Treść dokumentu.",
    )
    retrieved = [chunk]

    answer = (
        "Fakt 1 [SOURCE: BUDGET#001]. "
        "Fakt 2 [source: household_budget_policy.md]. "
        "Fakt 3 [source: household_budget_policy]."
    )

    res = verify_and_filter_citations(answer, retrieved)

    assert res.is_valid is True
    assert res.hallucination_detected is False
    assert len(res.valid_citations) == 3
    assert res.invalid_citations == []


def test_verify_citations_node_langgraph():
    """LangGraph node execution updates state with sanitized text and citations."""
    state = create_initial_state("Pytanie o limit")
    chunk = _make_chunk(
        "budget#001",
        "household_budget_policy.md",
        "Limit 2500 PLN.",
    )
    state["documents"] = [chunk]
    state["answer"] = (
        "Limit to 2500 PLN [source: budget#001]. "
        "Inny wymysł [source: bad#999]."
    )

    # Standard mode: sanitizes text and extracts valid citations
    updates = verify_citations_node(state, fallback_to_abstain=False)
    assert updates["answer"] == "Limit to 2500 PLN [source: budget#001]. Inny wymysł."
    assert updates["citations"] == ["budget#001"]
    assert updates["step_count"] == 1

    # Fallback to abstain mode: marks lacks_evidence and sets refusal
    refusal_updates = verify_citations_node(state, fallback_to_abstain=True)
    assert "Nie mogę potwierdzić" in refusal_updates["answer"]
    assert refusal_updates["citations"] == []
    assert refusal_updates["evidence_status"] == "lacks_evidence"
    assert "bad#999" in refusal_updates["missing_evidence"][0]


def test_generation_node_automatically_filters_hallucinations():
    """generate_grounded_answer_node verifies and filters LLM hallucinations."""
    state = create_initial_state("PGE opłata")
    chunk = _make_chunk(
        "pge#001",
        "utility_contracts_faq.md",
        "Termin płatności 14 dni.",
    )
    state["documents"] = [chunk]

    # LLM hallucinates an unretrieved source alongside valid source
    hallucinating_text = (
        "Termin to 14 dni [source: pge#001]. "
        "Cena prądu wynosi 1.20 PLN [source: wikipedia#energy]."
    )
    mock_client = MockLLMClient(hallucinating_text)

    output = generate_grounded_answer_node(state, client=mock_client)  # type: ignore[arg-type]

    # wikipedia#energy must be filtered out by programmatic code check
    assert "wikipedia#energy" not in output["citations"]
    assert output["citations"] == ["pge#001"]
    assert "wikipedia#energy" not in output["answer"]
    assert "[source: pge#001]" in output["answer"]
