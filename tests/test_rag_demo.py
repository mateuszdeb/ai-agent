"""Tests for the RAG demonstration runner (rag/demo.py).

Verifies execution of Grounded Answer and Explicit Abstention scenarios,
generation of JSONL trace files, and presence of graph nodes and citations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_project.rag.chunking import DocumentChunk
from agent_project.rag.demo import (
    DEFAULT_ABSTAIN_QUERY,
    DEFAULT_GROUNDED_QUERY,
    DemoCaseResult,
    main,
    run_demo_case,
    run_rag_demo,
)
from agent_project.rag.vector_store import SearchResult


class StubVectorStore:
    """Deterministic in-memory stub returning canned SearchResults per keyword."""

    def __init__(
        self, canned_results: dict[str, list[SearchResult]] | None = None
    ) -> None:
        self.canned = canned_results or {}

    def search(
        self,
        query: str,
        top_k: int = 3,
        score_threshold: float = 0.5,
    ) -> list[SearchResult]:
        q_low = query.lower()
        for key, results in self.canned.items():
            if key.lower() in q_low:
                return results[:top_k]
        return []


def _make_chunk(
    chunk_id: str,
    text: str,
    source: str = "utility_contracts_faq.md",
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        source=source,
        section="1. Umowa na dostawę energii elektrycznej (PGE Dystrybucja)",
        text=text,
        chunk_index=0,
        token_count=len(text.split()),
        metadata={},
    )


def test_run_demo_case_grounded_scenario(tmp_path: Path) -> None:
    """Grounded scenario executes retrieve, grade, generate_answer."""
    trace_path = tmp_path / "trace_grounded.jsonl"
    chunk = _make_chunk(
        "pge#001",
        "Termin płatności faktury wynosi 14 dni od daty wystawienia dokumentu.",
        "utility_contracts_faq.md",
    )
    stub_store = StubVectorStore(
        canned_results={"pge": [SearchResult(chunk=chunk, score=0.92)]}
    )

    result = run_demo_case(
        case_name="grounded_test",
        query="Ile dni wynosi termin płatności faktury PGE?",
        trace_path=trace_path,
        vector_store=stub_store,
        client=None,
    )

    assert isinstance(result, DemoCaseResult)
    assert result.evidence_status == "complete"
    assert result.citations_valid is True
    assert "retrieve" in result.executed_nodes
    assert "grade_evidence" in result.executed_nodes
    assert "generate_answer" in result.executed_nodes
    assert trace_path.exists()

    # Verify JSONL lines
    lines = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [entry["event"] for entry in lines]
    assert "demo_scenario_start" in events
    assert "graph_node_execution" in events
    assert "citation_verification" in events
    assert "demo_scenario_finish" in events


def test_run_demo_case_abstain_scenario(tmp_path: Path) -> None:
    """Abstention scenario executes retrieve, grade, abstain."""
    trace_path = tmp_path / "trace_abstain.jsonl"
    stub_store = StubVectorStore(canned_results={})

    result = run_demo_case(
        case_name="abstain_test",
        query="Jakie są miesięczne limity kwotowe na zakup kryptowalut Bitcoin?",
        trace_path=trace_path,
        vector_store=stub_store,
        client=None,
    )

    assert isinstance(result, DemoCaseResult)
    assert result.evidence_status == "lacks_evidence"
    assert len(result.missing_evidence) > 0
    assert any("kryptowalut" in m for m in result.missing_evidence)
    assert "abstain" in result.executed_nodes
    assert trace_path.exists()

    lines = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [entry["event"] for entry in lines]
    assert "citation_verification" in events
    assert "demo_scenario_finish" in events

    finish_entry = next(e for e in lines if e["event"] == "demo_scenario_finish")
    assert finish_entry["payload"]["evidence_status"] == "lacks_evidence"


def test_run_rag_demo_both_scenarios(tmp_path: Path) -> None:
    """run_rag_demo runs both scenarios and generates both trace JSONL files."""
    grounded_trace = tmp_path / "grounded.jsonl"
    abstain_trace = tmp_path / "abstain.jsonl"

    chunk = _make_chunk(
        "pge#001",
        "Termin płatności faktury wynosi 14 dni od daty wystawienia.",
        "utility_contracts_faq.md",
    )
    stub_store = StubVectorStore(
        canned_results={"pge": [SearchResult(chunk=chunk, score=0.95)]}
    )

    case_1, case_2 = run_rag_demo(
        grounded_trace_path=grounded_trace,
        abstain_trace_path=abstain_trace,
        grounded_query="termin płatności PGE?",
        abstain_query="limit na kryptowaluty?",
        vector_store=stub_store,
        client=None,
    )

    assert case_1.case_name == "grounded_answer"
    assert case_2.case_name == "abstention"
    assert grounded_trace.exists()
    assert abstain_trace.exists()
    assert grounded_trace.stat().st_size > 100
    assert abstain_trace.stat().st_size > 100


def test_demo_main_cli_entrypoint(tmp_path: Path, capsys: Any) -> None:
    """main() CLI entrypoint runs both scenarios and prints summary to stdout."""
    grounded_trace = tmp_path / "cli_grounded.jsonl"
    abstain_trace = tmp_path / "cli_abstain.jsonl"

    test_args = [
        "demo.py",
        "--output-grounded",
        str(grounded_trace),
        "--output-abstain",
        str(abstain_trace),
        "--query-grounded",
        DEFAULT_GROUNDED_QUERY,
        "--query-abstain",
        DEFAULT_ABSTAIN_QUERY,
    ]

    with patch("sys.argv", test_args):
        main()

    captured = capsys.readouterr()
    assert "RAG DEMONSTRATION" in captured.out
    assert "SCENARIO 1: Grounded Answer with Verified Citations" in captured.out
    assert "SCENARIO 2: Explicit Abstention with Named Missing Evidence" in captured.out
    assert "DEMONSTRATION COMPLETED SUCCESSFULLY" in captured.out
    assert grounded_trace.exists()
    assert abstain_trace.exists()
