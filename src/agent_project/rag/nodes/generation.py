"""Generation, abstention, and clarification nodes for LangGraph RAG workflow."""

from __future__ import annotations

import re
from typing import Any

from agent_project.llm import ModelClient
from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.citations import verify_and_filter_citations
from agent_project.rag.state import RAGState

_CITATION_PATTERN = re.compile(r"\[source:\s*([A-Za-z0-9_#.-]+)\]", re.IGNORECASE)


def extract_citations_from_text(text: str) -> list[str]:
    """Extract all cited source/chunk identifiers from citation tags in text."""
    matches = _CITATION_PATTERN.findall(text)
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for match in matches:
        clean = match.strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _deterministic_grounded_synthesis(
    query: str,
    documents: list[DocumentChunk],
) -> tuple[str, list[str]]:
    """Synthesize a deterministic answer directly from documents with citations."""
    if not documents:
        return (
            "Brak wystarczających fragmentów w bazie do sformułowania odpowiedzi.",
            [],
        )

    # Pick the top relevant chunks
    used_chunks = documents[:2]
    sentences: list[str] = []
    citations: list[str] = []

    for chunk in used_chunks:
        # Take the most relevant lines or paragraphs
        lines = [line.strip() for line in chunk.text.splitlines() if line.strip()]
        content_lines = [
            line for line in lines
            if not line.startswith("#") and len(line) > 10
        ]
        chosen_line = content_lines[0] if content_lines else chunk.text.strip()
        sentences.append(f"{chosen_line} [source: {chunk.chunk_id}]")
        citations.append(chunk.chunk_id)

    answer_text = "\n\n".join(sentences)
    return answer_text, citations


def generate_grounded_answer_node(
    state: RAGState,
    *,
    client: ModelClient | None = None,
) -> dict[str, Any]:
    """LangGraph node: Generate an answer with verified citation tags."""
    query = state["query"]
    documents = state.get("documents", [])

    if client is None:
        answer_text, citations = _deterministic_grounded_synthesis(query, documents)
    else:
        context_str = "\n\n".join(
            f"--- [{doc.chunk_id}] ({doc.source}) ---\n{doc.text}"
            for doc in documents
        )
        prompt = (
            "Odpowiedz na pytanie użytkownika WYŁĄCZNIE na podstawie fragmentów.\n"
            "Dla KAŻDEGO stwierdzenia dodaj przypis: [source: <chunk_id>].\n"
            "ZASADA: Cytuj wyłącznie podane chunk_id, nie zmyślaj źródeł.\n\n"
            f"Pytanie: {query}\n\n"
            f"Fragmenty:\n{context_str}\n\n"
            "Odpowiedź z cytowaniami:"
        )

        try:
            resp = client.complete(messages=[{"role": "user", "content": prompt}])
            raw_text = (resp.get("text") or "").strip()
            if raw_text:
                verification = verify_and_filter_citations(raw_text, documents)
                answer_text = verification.cleaned_answer
                citations = verification.valid_citations
            else:
                answer_text, citations = _deterministic_grounded_synthesis(
                    query, documents
                )
        except Exception:
            answer_text, citations = _deterministic_grounded_synthesis(query, documents)

    return {
        "answer": answer_text,
        "citations": citations,
        "step_count": state.get("step_count", 0) + 1,
    }


def abstain_node(
    state: RAGState,
    *,
    client: ModelClient | None = None,
) -> dict[str, Any]:
    """LangGraph node: Safely decline answering and name missing evidence."""
    query = state["query"]
    missing = state.get("missing_evidence") or []
    missing_desc = ", ".join(missing) if missing else "brak dokumentów w kolekcji"

    if client is not None:
        prompt = (
            f"Pytanie użytkownika: {query}\n"
            f"Zidentyfikowany brak dowodów w bazie: {missing_desc}\n"
            "Sformułuj uprzejmy, profesjonalny komunikat odmowy (abstention). "
            "Wyjaśnij jasno, jakich informacji brakuje w dokumentach "
            "oraz że nie można wygenerować odpowiedzi bez spekulacji."
        )
        fallback = (
            "Nie mogę odpowiedzieć na to pytanie na podstawie dostępnej bazy wiedzy. "
            f"Zidentyfikowany brak dowodów: {missing_desc}."
        )
        try:
            resp = client.complete(messages=[{"role": "user", "content": prompt}])
            answer_text = (resp.get("text") or "").strip()
            if not answer_text:
                answer_text = fallback
        except Exception:
            answer_text = fallback
    else:
        answer_text = (
            "Nie mogę odpowiedzieć na to pytanie na podstawie dostępnych dokumentów. "
            f"Zidentyfikowany brak dowodów: {missing_desc}. "
            "Dostępna baza wiedzy obejmuje: budżet domowy, subskrypcje, media PGE "
            "oraz zasady gwarancji i paragonów."
        )

    return {
        "answer": answer_text,
        "citations": [],
        "step_count": state.get("step_count", 0) + 1,
    }


def clarify_node(
    state: RAGState,
    *,
    client: ModelClient | None = None,
) -> dict[str, Any]:
    """LangGraph node: Clarification question when user intent is ambiguous."""
    query = state["query"]
    existing_question = state.get("clarification_question")

    if existing_question and existing_question.strip():
        question = existing_question.strip()
    else:
        question = (
            f"Twoje zapytanie '{query}' jest zbyt ogólne. "
            "Doprecyzuj proszę, o jaką kategorię, dostawcę lub kwotę pytasz "
            "(np. rachunki PGE, subskrypcja Spotify, zakupy w hipermarkecie)."
        )

    return {
        "answer": question,
        "clarification_question": question,
        "citations": [],
        "step_count": state.get("step_count", 0) + 1,
    }
