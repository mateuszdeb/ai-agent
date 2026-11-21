"""Demonstrate grounded RAG answers and abstention."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_project.llm import ModelClient
from agent_project.rag.citations import (
    VerificationResult,
    verify_and_filter_citations,
)
from agent_project.rag.graph import compile_rag_graph
from agent_project.rag.state import RAGState, create_initial_state
from agent_project.rag.vector_store import (
    VectorStore,
    build_vector_store_from_directory,
)
from agent_project.tracing import append_trace_record, make_trace_record

DEFAULT_GROUNDED_TRACE_PATH = Path("evidence/trace_rag_grounded.jsonl")
DEFAULT_ABSTAIN_TRACE_PATH = Path("evidence/trace_rag_abstain.jsonl")

DEFAULT_GROUNDED_QUERY = (
    "Ile dni wynosi termin płatności faktury za energię elektryczną "
    "w PGE Dystrybucja?"
)
DEFAULT_ABSTAIN_QUERY = (
    "Jakie są miesięczne limity kwotowe na zakup kryptowalut "
    "Bitcoin i Ethereum w budżecie?"
)


class DemoCaseResult(BaseModel):
    """Execution summary of a single demonstration scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_name: str
    query: str
    evidence_status: str
    answer: str
    citations: list[str] = Field(default_factory=list)
    citations_valid: bool
    missing_evidence: list[str] = Field(default_factory=list)
    step_count: int
    executed_nodes: list[str]
    trace_path: str


def _serialize_value(value: Any) -> Any:
    """Recursively convert Pydantic models and complex types for JSON logging."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_value(v) for v in value]
    return value


def run_demo_case(
    *,
    case_name: str,
    query: str,
    trace_path: Path | str,
    vector_store: VectorStore | None = None,
    client: ModelClient | None = None,
    top_k: int = 3,
) -> DemoCaseResult:
    """Execute a single scenario through LangGraph, logging a full trace."""
    out_path = Path(trace_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    # Log scenario start
    append_trace_record(
        out_path,
        make_trace_record(
            "demo_scenario_start",
            {
                "case_name": case_name,
                "query": query,
                "top_k": top_k,
            },
        ),
    )

    compiled_graph = compile_rag_graph(
        vector_store=vector_store,
        client=client,
        top_k=top_k,
    )
    initial_state = create_initial_state(query)

    accumulated_state: RAGState = dict(initial_state)  # type: ignore[arg-type]
    executed_nodes: list[str] = []

    for event in compiled_graph.stream(initial_state):
        for node_name, updates in event.items():
            executed_nodes.append(node_name)
            accumulated_state.update(updates)

            payload = {
                "case_name": case_name,
                "node": node_name,
                "step_count": accumulated_state.get("step_count", 0),
                "updates": _serialize_value(updates),
            }
            append_trace_record(
                out_path,
                make_trace_record("graph_node_execution", payload),
            )

    # Citation verification step
    answer = accumulated_state.get("answer") or ""
    documents = accumulated_state.get("documents", [])
    verification: VerificationResult = verify_and_filter_citations(
        answer, documents
    )

    append_trace_record(
        out_path,
        make_trace_record(
            "citation_verification",
            {
                "case_name": case_name,
                "is_valid": verification.is_valid,
                "valid_citations": verification.valid_citations,
                "invalid_citations": verification.invalid_citations,
                "removed_citations": verification.removed_citations,
                "hallucination_detected": verification.hallucination_detected,
                "has_citations": verification.has_citations,
            },
        ),
    )

    evidence_status = accumulated_state.get("evidence_status", "unknown")
    missing = accumulated_state.get("missing_evidence") or []
    citations = accumulated_state.get("citations") or []

    append_trace_record(
        out_path,
        make_trace_record(
            "demo_scenario_finish",
            {
                "case_name": case_name,
                "evidence_status": evidence_status,
                "citations_valid": verification.is_valid,
                "step_count": accumulated_state.get("step_count", 0),
                "executed_nodes": executed_nodes,
            },
        ),
    )

    return DemoCaseResult(
        case_name=case_name,
        query=query,
        evidence_status=evidence_status,
        answer=answer,
        citations=citations,
        citations_valid=verification.is_valid,
        missing_evidence=missing,
        step_count=accumulated_state.get("step_count", 0),
        executed_nodes=executed_nodes,
        trace_path=str(out_path),
    )


def run_rag_demo(
    *,
    grounded_trace_path: Path | str = DEFAULT_GROUNDED_TRACE_PATH,
    abstain_trace_path: Path | str = DEFAULT_ABSTAIN_TRACE_PATH,
    grounded_query: str = DEFAULT_GROUNDED_QUERY,
    abstain_query: str = DEFAULT_ABSTAIN_QUERY,
    vector_store: VectorStore | None = None,
    client: ModelClient | None = None,
) -> tuple[DemoCaseResult, DemoCaseResult]:
    """Execute the grounded answer and abstention scenarios and write their traces."""
    store = vector_store or build_vector_store_from_directory("data/documents")

    # Case 1: Grounded answer with verified citations
    case_1 = run_demo_case(
        case_name="grounded_answer",
        query=grounded_query,
        trace_path=grounded_trace_path,
        vector_store=store,
        client=client,
    )

    # Case 2: Explicit abstention with named missing evidence
    case_2 = run_demo_case(
        case_name="abstention",
        query=abstain_query,
        trace_path=abstain_trace_path,
        vector_store=store,
        client=client,
    )

    return case_1, case_2


def main() -> None:
    """Run the RAG demonstration from the command line."""
    parser = argparse.ArgumentParser(
        description="Demonstrate grounded RAG answers and abstention."
    )
    parser.add_argument(
        "--output-grounded",
        type=str,
        default=str(DEFAULT_GROUNDED_TRACE_PATH),
        help="Path for grounded execution trace JSONL",
    )
    parser.add_argument(
        "--output-abstain",
        type=str,
        default=str(DEFAULT_ABSTAIN_TRACE_PATH),
        help="Path for abstain execution trace JSONL",
    )
    parser.add_argument(
        "--query-grounded",
        type=str,
        default=DEFAULT_GROUNDED_QUERY,
        help="Query for grounded answer demonstration",
    )
    parser.add_argument(
        "--query-abstain",
        type=str,
        default=DEFAULT_ABSTAIN_QUERY,
        help="Query for abstention demonstration",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("      RAG DEMONSTRATION: GROUNDED ANSWERS AND ABSTENTION")
    print("=" * 70)

    case_1, case_2 = run_rag_demo(
        grounded_trace_path=args.output_grounded,
        abstain_trace_path=args.output_abstain,
        grounded_query=args.query_grounded,
        abstain_query=args.query_abstain,
    )

    print("\n--- SCENARIO 1: Grounded Answer with Verified Citations ---")
    print(f"Query:           {case_1.query}")
    print(f"Executed Nodes:  {' -> '.join(case_1.executed_nodes)}")
    print(f"Evidence Status: {case_1.evidence_status}")
    print(f"Citations:       {case_1.citations}")
    print(f"Citations Valid: {case_1.citations_valid}")
    print(f"Answer:          {case_1.answer}")
    print(f"Trace saved to:  {case_1.trace_path}")

    print("\n--- SCENARIO 2: Explicit Abstention with Named Missing Evidence ---")
    print(f"Query:            {case_2.query}")
    print(f"Executed Nodes:   {' -> '.join(case_2.executed_nodes)}")
    print(f"Evidence Status:  {case_2.evidence_status}")
    print(f"Missing Evidence: {case_2.missing_evidence}")
    print(f"Answer:           {case_2.answer}")
    print(f"Trace saved to:   {case_2.trace_path}")

    print("\n" + "=" * 70)
    print("DEMONSTRATION COMPLETED SUCCESSFULLY (EXIT CODE 0)")
    print("=" * 70)


if __name__ == "__main__":
    main()
