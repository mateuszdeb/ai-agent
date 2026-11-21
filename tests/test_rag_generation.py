"""Tests for generation, abstention, and clarification nodes."""

from __future__ import annotations

from typing import Any

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.nodes.generation import (
    abstain_node,
    clarify_node,
    extract_citations_from_text,
    generate_grounded_answer_node,
)
from agent_project.rag.state import create_initial_state


class MockGenerationClient:
    """Mock client returning predetermined generated answer."""

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {"text": self.response_text}


def test_extract_citations_from_text():
    """Verify regex citation extraction, deduplication, and order preservation."""
    text = (
        "Limit na zakupy to 2500 PLN [source: budget#001]. "
        "Faktura PGE wynosi 284.10 PLN [source: pge#002]. "
        "Ponowne powołanie na limit [SOURCE: budget#001]."
    )
    citations = extract_citations_from_text(text)
    assert citations == ["budget#001", "pge#002"]


def test_extract_citations_empty():
    """Text without source tags returns empty list."""
    assert extract_citations_from_text("Odpowiedź bez żadnych cytowań.") == []
    assert extract_citations_from_text("") == []


def test_generate_grounded_answer_deterministic():
    """Grounded answer node synthesizes answer with citations from documents."""
    state = create_initial_state("Jaki jest limit na jedzenie?")
    chunk = DocumentChunk(
        chunk_id="budget#001",
        source="household_budget_policy.md",
        section="Miesięczne limity kwotowe wg kategorii",
        text="Limit na Groceries wynosi 2500 PLN miesięcznie.",
        chunk_index=0,
        token_count=8,
    )
    state["documents"] = [chunk]

    updated = generate_grounded_answer_node(state)
    assert updated["answer"] is not None
    assert "budget#001" in updated["answer"]
    assert updated["citations"] == ["budget#001"]
    assert updated["step_count"] == 1


def test_generate_grounded_answer_with_mock_client():
    """Grounded answer node extracts citations from LLM-generated response."""
    state = create_initial_state("Termin płatności PGE")
    chunk = DocumentChunk(
        chunk_id="pge#001",
        source="utility_contracts_faq.md",
        section="Termin płatności",
        text="Termin płatności faktury PGE wynosi 14 dni.",
        chunk_index=0,
        token_count=8,
    )
    state["documents"] = [chunk]

    llm_reply = (
        "Zgodnie z umową termin płatności wynosi 14 dni [source: pge#001]."
    )
    client = MockGenerationClient(llm_reply)

    updated = generate_grounded_answer_node(state, client=client)
    assert updated["answer"] == llm_reply
    assert updated["citations"] == ["pge#001"]
    assert updated["step_count"] == 1


def test_abstain_node_names_missing_evidence():
    """Abstain node generates refusal and names missing evidence."""
    state = create_initial_state("Jak rozliczyć zyski z Bitcoina?")
    state["missing_evidence"] = ["Brak polityk dotyczących kryptowalut"]

    updated = abstain_node(state)
    assert updated["answer"] is not None
    assert "Nie mogę odpowiedzieć" in updated["answer"]
    assert "kryptowalut" in updated["answer"].lower()
    assert updated["citations"] == []
    assert updated["step_count"] == 1


def test_abstain_node_with_client():
    """Abstain node respects client generated refusal."""
    state = create_initial_state("Pytanie o fotowoltaikę")
    state["missing_evidence"] = ["brak dokumentów o panelach"]

    client = MockGenerationClient("Odmowa: baza nie posiada informacji o fotowoltaice.")
    updated = abstain_node(state, client=client)
    assert updated["answer"] == "Odmowa: baza nie posiada informacji o fotowoltaice."
    assert updated["citations"] == []


def test_clarify_node():
    """Clarify node returns clarification question and empty citations."""
    state = create_initial_state("faktura")
    state["clarification_question"] = "O jaką fakturę pytasz?"

    updated = clarify_node(state)
    assert updated["answer"] == "O jaką fakturę pytasz?"
    assert updated["clarification_question"] == "O jaką fakturę pytasz?"
    assert updated["citations"] == []
    assert updated["step_count"] == 1
