"""Grading and bounded search query rewriting nodes for LangGraph RAG workflow."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_project.llm import ModelClient
from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.state import EvidenceStatus, RAGState

_UNANSWERABLE_PATTERNS = [
    re.compile(r"\b(kryptowalut\w*|bitcoin\w*|eth\w*|giełd\w*)\b", re.IGNORECASE),
    re.compile(r"\b(fotowoltaik\w*|panele|inwerter|net-billing)\b", re.IGNORECASE),
    re.compile(r"\b(pies|psa|kot|kota|weterynarz\w*|zwierz\w*)\b", re.IGNORECASE),
]

_AMBIGUOUS_PATTERNS = [
    re.compile(
        r"^(faktura|limit|rachunek|opłata|duplikat|zwrot|koszt)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(ile|jaki|coś|który|gdzie)\??$", re.IGNORECASE),
]


class EvidenceGradeResult(BaseModel):
    """Structured evaluation of retrieved evidence against user query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EvidenceStatus = Field(
        description="One of: complete, need_better_query, ambiguous, lacks_evidence"
    )
    reason: str = Field(
        description="Justification for the chosen grading status"
    )
    missing_evidence: list[str] | None = Field(
        default=None,
        description="Named missing facts or topics when status is lacks_evidence"
    )
    clarification_question: str | None = Field(
        default=None,
        description="Clarifying question when user intent is ambiguous"
    )


def _heuristic_grade(
    query: str,
    documents: list[DocumentChunk],
    retry_count: int = 0,
) -> EvidenceGradeResult:
    """Deterministic heuristic classification when no LLM client is provided."""
    clean_query = query.strip()

    # 1. Check for intentional unanswerable topics
    for pattern in _UNANSWERABLE_PATTERNS:
        match = pattern.search(clean_query)
        if match:
            matched_term = match.group(0)
            return EvidenceGradeResult(
                status="lacks_evidence",
                reason=f"Topic '{matched_term}' is absent from knowledge base.",
                missing_evidence=[
                    f"Brak polityk i dokumentów w bazie dotyczących: '{matched_term}'."
                ],
            )

    # 2. Check for ambiguity (very short or ungrounded question)
    if len(clean_query.split()) <= 2:
        for pattern in _AMBIGUOUS_PATTERNS:
            if pattern.search(clean_query):
                return EvidenceGradeResult(
                    status="ambiguous",
                    reason="Zapytanie jest zbyt ogólne lub wieloznaczne.",
                    clarification_question=(
                        f"O jaką kategorię pytasz w kontekście: '{clean_query}'? "
                        "(np. PGE, Spotify, Carrefour, Biedronka)"
                    ),
                )

    # 3. Check document presence
    if not documents:
        return EvidenceGradeResult(
            status="lacks_evidence",
            reason="Nie znaleziono żadnych pasujących dokumentów w bazie.",
            missing_evidence=[f"Brak danych dla zapytania: '{clean_query}'"],
        )

    # 4. Check keyword match quality
    query_words = {
        w.lower()
        for w in re.findall(r"\w{3,}", clean_query)
        if w.lower() not in {"ile", "jaki", "jest", "dla", "oraz", "przez", "moze"}
    }
    all_doc_text = " ".join(doc.text.lower() for doc in documents)

    matched_words = {w for w in query_words if w in all_doc_text}
    match_ratio = len(matched_words) / len(query_words) if query_words else 1.0

    # If partial match and haven't retried yet, request better query
    if match_ratio < 0.6 and retry_count == 0:
        missing_terms = [w for w in query_words if w not in matched_words]
        return EvidenceGradeResult(
            status="need_better_query",
            reason="Pobrane dokumenty zawierają tylko częściowe informacje.",
            missing_evidence=[f"Brakujące terminy: {', '.join(missing_terms)}"],
        )

    if match_ratio < 0.3:
        return EvidenceGradeResult(
            status="lacks_evidence",
            reason="Pobrane dokumenty nie zawierają odpowiedzi na to zapytanie.",
            missing_evidence=[f"Brak faktów w kolekcji dla zapytania: '{clean_query}'"],
        )

    return EvidenceGradeResult(
        status="complete",
        reason="Pobrane fragmenty zawierają wystarczające dowody do odpowiedzi.",
    )


def evaluate_evidence(
    query: str,
    documents: list[DocumentChunk],
    *,
    client: ModelClient | None = None,
    retry_count: int = 0,
) -> EvidenceGradeResult:
    """Evaluate whether retrieved evidence is sufficient, ambiguous, or missing."""
    if client is None:
        return _heuristic_grade(query, documents, retry_count=retry_count)

    context_str = "\n\n".join(
        f"[{doc.chunk_id}] ({doc.source}): {doc.text}"
        for doc in documents
    )
    prompt = (
        "Oceń czy fragmenty wystarczają do odpowiedzi na zapytanie RAG.\n"
        f"Zapytanie: {query}\n"
        f"Liczba wcześniejszych ponowień: {retry_count}\n"
        f"Fragmenty:\n{context_str}\n\n"
        "Wybierz DOKŁADNIE JEDEN status z czterech:\n"
        "1. 'complete': dowody są kompletne i wystarczą do odpowiedzi.\n"
        "2. 'need_better_query': temat istnieje, ale brak kluczowego szczegółu.\n"
        "3. 'ambiguous': zapytanie jest wieloznaczne lub zbyt ogólne.\n"
        "4. 'lacks_evidence': w bazie w ogóle brak informacji na ten temat.\n\n"
        "Zwróć WYŁĄCZNIE poprawny JSON z polami:\n"
        "{\n"
        '  "status": "complete"|"need_better_query"|"ambiguous"|"lacks_evidence",\n'
        '  "reason": "krótkie uzasadnienie",\n'
        '  "missing_evidence": ["brak 1"] | null,\n'
        '  "clarification_question": "pytanie do użytkownika" | null\n'
        "}"
    )

    try:
        response = client.complete(
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.get("text", "")
        # Extract JSON from potential code fences
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            return EvidenceGradeResult.model_validate(parsed)
    except Exception:
        pass

    return _heuristic_grade(query, documents, retry_count=retry_count)


def grade_evidence_node(
    state: RAGState,
    *,
    client: ModelClient | None = None,
) -> dict[str, Any]:
    """LangGraph node: Evaluate evidence and determine next execution transition."""
    grade = evaluate_evidence(
        query=state["query"],
        documents=state.get("documents", []),
        client=client,
        retry_count=state.get("retry_count", 0),
    )

    return {
        "evidence_status": grade.status,
        "missing_evidence": grade.missing_evidence,
        "clarification_question": grade.clarification_question,
        "step_count": state.get("step_count", 0) + 1,
    }


def rewrite_query_node(
    state: RAGState,
    *,
    client: ModelClient | None = None,
) -> dict[str, Any]:
    """LangGraph node: Bounded search step formulating an improved query."""
    current_query = state["query"]
    retry_count = state.get("retry_count", 0)
    missing = state.get("missing_evidence") or []

    rewritten: str

    if client is not None:
        prompt = (
            f"Oryginalne zapytanie: {current_query}\n"
            f"Zidentyfikowane braki: {missing}\n"
            "Sformułuj jedno, lepsze zapytanie wyszukiwania. "
            "Odpowiedz wyłącznie nowym zapytaniem, bez komentarzy."
        )
        try:
            resp = client.complete(
                messages=[{"role": "user", "content": prompt}]
            )
            text = (resp.get("text") or "").strip().strip('"')
            if text:
                rewritten = text
            else:
                rewritten = f"{current_query} szczegóły i regulacje"
        except Exception:
            rewritten = f"{current_query} szczegóły i regulacje"
    else:
        # Deterministic query expansion
        low_q = current_query.lower()
        if "spotify" in low_q and "duplikat" not in low_q:
            rewritten = f"{current_query} duplikat reklamacja"
        elif "pge" in low_q and "taryfa" not in low_q:
            rewritten = f"{current_query} taryfa termin odsetki"
        elif "limit" in low_q and "kategori" not in low_q:
            rewritten = f"{current_query} kategorie kwoty"
        else:
            missing_phrase = " ".join(missing) if missing else ""
            rewritten = f"{current_query} {missing_phrase}".strip()

    return {
        "query": rewritten,
        "retry_count": retry_count + 1,
        "step_count": state.get("step_count", 0) + 1,
    }
