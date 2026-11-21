"""Local MCP stdio adapter for the existing controlled expense tools.

Run with ``uv run python -m agent_project.mcp_server``. Application-owned
``--data-path`` and ``--changes-path`` options allow isolated local datasets.
These paths are deliberately not accepted as model/tool arguments.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool, ToolAnnotations

from agent_project.agent import TOOL_REGISTRY, execute_controlled_tool
from agent_project.tools import TOOL_DEFINITIONS

MCP_TOOL_NAMES = (
    "search_transactions",
    "assign_category",
    "compute_budget_baseline",
)


class HouseholdExpenseMCP(FastMCP):
    """Expose a fixed allowlist through FastMCP's public protocol handlers.

    Override discovery and dispatch to reuse the controller's strict Pydantic
    validation. FastMCP's default signature-based wrappers can coerce arguments
    before the controller sees them; this adapter passes the original values.
    No private SDK attributes or duplicate domain implementations are needed.
    """

    def __init__(
        self,
        *,
        data_path: Path | None = None,
        changes_path: Path | None = None,
    ) -> None:
        super().__init__(
            "Household Expense Checker",
            instructions=(
                "Read and categorize local household transactions. Tool results "
                "contain application-controlled status values. Category writes "
                "use an append-only ledger; deletion is not exposed over MCP."
            ),
        )
        self._data_path = data_path
        self._changes_path = changes_path

    async def list_tools(self) -> list[Tool]:
        descriptions = {
            item["function"]["name"]: item["function"]["description"]
            for item in TOOL_DEFINITIONS
        }
        return [
            Tool(
                name=name,
                description=descriptions[name],
                inputSchema=TOOL_REGISTRY[name]["model"].model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=name != "assign_category",
                    destructiveHint=False,
                    openWorldHint=False,
                ),
            )
            for name in MCP_TOOL_NAMES
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in MCP_TOOL_NAMES:
            return {
                "status": "invalid_tool",
                "error": f"Tool '{name}' is not available through MCP.",
            }
        return execute_controlled_tool(
            name,
            arguments,
            data_path=self._data_path,
            changes_path=self._changes_path,
        )


def create_server(
    *,
    data_path: Path | None = None,
    changes_path: Path | None = None,
) -> HouseholdExpenseMCP:
    """Create a server with paths fixed by the trusted host application."""
    return HouseholdExpenseMCP(data_path=data_path, changes_path=changes_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-path", type=Path, default=None)
    parser.add_argument("--changes-path", type=Path, default=None)
    args = parser.parse_args()
    create_server(data_path=args.data_path, changes_path=args.changes_path).run(
        transport="stdio"
    )


if __name__ == "__main__":
    main()
