"""Focused Stage 4 verification of discovery, shared guards, and real stdio."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_project.agent import TOOL_REGISTRY, execute_controlled_tool
from agent_project.mcp_server import MCP_TOOL_NAMES, create_server
from agent_project.tools import (
    assign_category,
    compute_budget_baseline,
    load_effective_transactions,
    search_transactions,
)


@pytest.fixture
def isolated_server(tmp_path):
    source = Path(__file__).resolve().parents[1] / "data" / "transactions.json"
    data_path = tmp_path / "transactions.json"
    data_path.write_bytes(source.read_bytes())
    changes_path = tmp_path / "changes.jsonl"
    return (
        create_server(data_path=data_path, changes_path=changes_path),
        data_path,
        changes_path,
    )


def test_discovery_uses_shared_schemas_and_fixed_allowlist(isolated_server):
    server, _, _ = isolated_server
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == list(MCP_TOOL_NAMES)
    for tool in tools:
        assert tool.inputSchema == TOOL_REGISTRY[tool.name]["model"].model_json_schema()
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.description
        assert "data_path" not in tool.inputSchema["properties"]
        assert "changes_path" not in tool.inputSchema["properties"]
        assert tool.annotations.readOnlyHint == (tool.name != "assign_category")


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("search_transactions", {}),
        ("search_transactions", {"merchant": "Spotify", "limit": 2}),
        ("compute_budget_baseline", {"merchant": "Spotify"}),
        ("compute_budget_baseline", {"merchant": "Biedronka"}),
    ],
)
def test_reads_match_agent_controller(name, arguments, isolated_server):
    server, data_path, changes_path = isolated_server
    result = asyncio.run(server.call_tool(name, arguments))
    assert result == execute_controlled_tool(
        name, arguments, data_path=data_path, changes_path=changes_path
    )
    assert result["status"] == "success"
    assert not changes_path.exists()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("search_transactions", {"limit": "10"}),
        ("search_transactions", {"limit": True}),
        ("search_transactions", {"limit": 0}),
        ("search_transactions", {"unexpected": "field"}),
        ("search_transactions", {"data_path": "/tmp/other.json"}),
        ("compute_budget_baseline", {"changes_path": "/tmp/other.jsonl"}),
        ("assign_category", {"transaction_id": "tx_101"}),
        (
            "assign_category",
            {"transaction_id": "tx_101", "category": "Other", "force_override": "true"},
        ),
    ],
)
def test_invalid_arguments_fail_without_writes(name, arguments, isolated_server):
    server, _, changes_path = isolated_server
    result = asyncio.run(server.call_tool(name, arguments))
    assert result["status"] == "invalid_arguments"
    assert not changes_path.exists()


@pytest.mark.parametrize(
    "name", ["unknown_tool", "delete_flagged_transaction", "get_transactions"]
)
def test_non_exported_tools_are_not_executable(name, isolated_server):
    server, _, changes_path = isolated_server
    result = asyncio.run(
        server.call_tool(name, {"transaction_id": "tx_104", "confirmed": True})
    )
    assert result["status"] == "invalid_tool"
    assert not changes_path.exists()


def test_category_write_uses_shared_ledger_and_preserves_source(isolated_server):
    server, data_path, changes_path = isolated_server
    before = data_path.read_bytes()
    arguments = {
        "transaction_id": "tx_101",
        "category": "Other",
        "force_override": True,
    }
    result = asyncio.run(server.call_tool("assign_category", arguments))
    assert result["status"] == "success"
    assert result["transaction_id"] == "tx_101"
    events = [json.loads(line) for line in changes_path.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["action_id"] == result["action_id"]
    assert data_path.read_bytes() == before

    repeated = asyncio.run(
        server.call_tool(
            "assign_category", {"transaction_id": "tx_101", "category": "Other"}
        )
    )
    assert repeated["status"] == "already_assigned"
    assert len(changes_path.read_text().splitlines()) == 1
    observed = execute_controlled_tool(
        "get_transaction_details",
        {"transaction_id": "tx_101"},
        data_path=data_path,
        changes_path=changes_path,
    )
    assert observed["data"]["category_hint"] == "Other"


def test_real_stdio_session_initializes_lists_and_calls_tools(isolated_server):
    """Use a real MCP client and subprocess, not only adapter method calls."""
    _, data_path, changes_path = isolated_server
    original = data_path.read_bytes()
    root = Path(__file__).resolve().parents[1]

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agent_project.mcp_server",
                "--data-path",
                str(data_path),
                "--changes-path",
                str(changes_path),
            ],
            cwd=root,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == set(MCP_TOOL_NAMES)
                for name, arguments, expected_status in [
                    ("search_transactions", {"merchant": "Spotify"}, "success"),
                    ("compute_budget_baseline", {"merchant": "Spotify"}, "success"),
                    ("search_transactions", {"limit": "10"}, "invalid_arguments"),
                    (
                        "assign_category",
                        {
                            "transaction_id": "tx_101",
                            "category": "Other",
                            "force_override": True,
                        },
                        "success",
                    ),
                    (
                        "assign_category",
                        {"transaction_id": "tx_101", "category": "Other"},
                        "already_assigned",
                    ),
                    (
                        "delete_flagged_transaction",
                        {"transaction_id": "tx_104", "confirmed": True},
                        "invalid_tool",
                    ),
                ]:
                    result = await session.call_tool(name, arguments)
                    assert not result.isError
                    assert result.structuredContent["status"] == expected_status
                    assert (
                        json.loads(result.content[0].text) == result.structuredContent
                    )

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))
    assert data_path.read_bytes() == original
    assert len(changes_path.read_text().splitlines()) == 1


@pytest.mark.parametrize(
    ("name", "arguments", "domain_function"),
    [
        ("search_transactions", {"merchant": "Stream"}, search_transactions),
        ("search_transactions", {"merchant": "Missing"}, search_transactions),
        ("compute_budget_baseline", {"merchant": "Stream"}, compute_budget_baseline),
        ("compute_budget_baseline", {"merchant": "Missing"}, compute_budget_baseline),
    ],
)
def test_mcp_reads_match_direct_domain_functions(
    name,
    arguments,
    domain_function,
    controlled_dataset,
):
    data_path, changes_path = controlled_dataset
    server = create_server(data_path=data_path, changes_path=changes_path)
    direct = domain_function(
        **arguments, data_path=data_path, changes_path=changes_path
    )
    through_mcp = asyncio.run(server.call_tool(name, arguments))
    assert through_mcp == {"status": "success", "data": direct}
    assert not changes_path.exists()


def test_mcp_and_direct_write_produce_equivalent_state(controlled_dataset, tmp_path):
    data_path, changes_path = controlled_dataset
    reference_ledger = tmp_path / "reference.jsonl"
    arguments = {"transaction_id": "tx_pending", "category": "Electronics"}
    server = create_server(data_path=data_path, changes_path=changes_path)
    direct = assign_category(
        **arguments, data_path=data_path, changes_path=reference_ledger
    )
    via_mcp = asyncio.run(server.call_tool("assign_category", arguments))
    assert direct["status"] == via_mcp["status"] == "success"
    assert direct["previous_category"] == via_mcp["previous_category"] is None

    def domain_state(ledger):
        return [
            {key: value for key, value in tx.items() if key != "last_action_id"}
            for tx in load_effective_transactions(data_path, ledger)
        ]

    assert domain_state(changes_path) == domain_state(reference_ledger)
    snapshot = changes_path.read_bytes()
    rejected = asyncio.run(
        server.call_tool(
            "assign_category", {"transaction_id": "tx_pending", "category": "Travel"}
        )
    )
    assert rejected["status"] == "already_assigned"
    assert changes_path.read_bytes() == snapshot
