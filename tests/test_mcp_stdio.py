from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_server_exposes_read_only_tools(
    tmp_path: Path,
    corpus_path: Path,
    element_project_path: Path,
) -> None:
    async def exercise_server() -> None:
        environment = dict(os.environ)
        for key in list(environment):
            if key.startswith("ELEMENT_CONSOLE_") or key == "ELEMENT_IDE_SETTINGS_PATH":
                environment.pop(key)
        environment["ELEMENT_DOCS_PATH"] = str(corpus_path)
        environment["ELEMENT_PROJECT_PATH"] = str(element_project_path)
        environment["ELEMENT_MCP_CONFIG_PATH"] = str(tmp_path / "config.json")
        environment["ELEMENT_MCP_DATA_PATH"] = str(tmp_path / "data")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "element_mcp", "--transport", "stdio"],
            env=environment,
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "1C Element"
            assert initialized.serverInfo.version == "0.12.0"
            assert initialized.instructions is not None
            assert "first call\nget_documentation_status" in initialized.instructions
            assert "Never call start_documentation_build without that consent" in initialized.instructions
            assert "call discover_element_installations" in initialized.instructions
            assert (
                "poll get_documentation_build_status until completed, failed, or cancelled" in initialized.instructions
            )
            assert "call search_docs first, then get_document" in initialized.instructions
            assert "first call get_project_status" in initialized.instructions
            assert "Never read paths outside the connected project" in initialized.instructions
            assert "first call get_console_status" in initialized.instructions
            assert "Never ask the user to paste Client-Secret" in initialized.instructions
            assert "Use lookup_symbol for declarations" in initialized.instructions
            assert "first call get_language_server_status" in initialized.instructions
            assert "If exact tools report a lexical fallback" in initialized.instructions
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "activate_documentation",
                "cancel_documentation_build",
                "connect_project",
                "configure_language_server",
                "discover_element_installations",
                "get_corpus_info",
                "get_console_project",
                "get_console_status",
                "get_document",
                "get_documentation_build_status",
                "get_documentation_status",
                "get_definition",
                "get_language_server_status",
                "get_project_overview",
                "get_project_status",
                "get_project_diagnostics",
                "get_references",
                "get_related_docs",
                "lookup_symbol",
                "find_references",
                "list_project_elements",
                "list_console_spaces",
                "list_space_projects",
                "match_console_project",
                "read_project_file",
                "search_docs",
                "search_project_code",
                "start_documentation_build",
            }
            read_only = {
                "discover_element_installations",
                "get_corpus_info",
                "get_console_project",
                "get_console_status",
                "get_document",
                "get_documentation_build_status",
                "get_documentation_status",
                "get_definition",
                "get_language_server_status",
                "get_project_overview",
                "get_project_status",
                "get_project_diagnostics",
                "get_references",
                "get_related_docs",
                "lookup_symbol",
                "find_references",
                "list_project_elements",
                "list_console_spaces",
                "list_space_projects",
                "match_console_project",
                "read_project_file",
                "search_docs",
                "search_project_code",
            }
            for tool in tools.tools:
                assert tool.annotations is not None
                assert tool.annotations.readOnlyHint is (tool.name in read_only)

            descriptions = {tool.name: tool.description for tool in tools.tools}
            assert "explicit user consent" in (descriptions["start_documentation_build"] or "")
            assert "explicit request" in (descriptions["cancel_documentation_build"] or "")
            assert "After search_docs" in (descriptions["get_document"] or "")
            assert "YAML entities" in (descriptions["list_project_elements"] or "")
            assert "relative path" in (descriptions["read_project_file"] or "")
            assert "current IDE project" in (descriptions["list_space_projects"] or "")
            assert "bounded local workspace candidates" in (descriptions["match_console_project"] or "")
            assert "lexical ambiguity" in (descriptions["lookup_symbol"] or "")
            assert "compiler-level" in (descriptions["find_references"] or "")
            assert "Element LSP" in (descriptions["get_definition"] or "")
            assert "Element LSP" in (descriptions["get_references"] or "")
            assert "published by Element LSP" in (descriptions["get_project_diagnostics"] or "")

            result = await session.call_tool("search_docs", {"query": "ВебМетод", "corpus": "lang"})
            assert result.isError is False
            assert result.structuredContent is not None
            assert result.structuredContent["count"] >= 1

            overview = await session.call_tool("get_project_overview", {})
            assert overview.isError is False
            assert overview.structuredContent is not None
            assert overview.structuredContent["project"]["name"] == "ExampleProject"

            symbols = await session.call_tool("lookup_symbol", {"name": "FindOrder"})
            assert symbols.isError is False
            assert symbols.structuredContent is not None
            assert symbols.structuredContent["resolution"] == "exact"

            definition = await session.call_tool(
                "get_definition",
                {"relative_path": "Sales/Orders.xbsl", "line": 1, "column": 10},
            )
            assert definition.isError is False
            assert definition.structuredContent is not None
            assert definition.structuredContent["analysis_mode"] == "syntax-aware lexical fallback"
            assert definition.structuredContent["semantic_guarantee"] is False

            related = await session.call_tool("get_related_docs", {"symbol": "FindOrder", "limit": 2})
            assert related.isError is False
            assert related.structuredContent is not None
            assert related.structuredContent["documentation"]["count"] >= 1

            console = await session.call_tool("get_console_status", {})
            assert console.isError is False
            assert console.structuredContent is not None
            assert console.structuredContent["status"] == "missing"

    asyncio.run(exercise_server())


def test_stdio_server_reports_missing_corpus(tmp_path: Path) -> None:
    async def exercise_server() -> None:
        environment = dict(os.environ)
        for key in list(environment):
            if key.startswith("ELEMENT_CONSOLE_") or key == "ELEMENT_IDE_SETTINGS_PATH":
                environment.pop(key)
        environment.pop("ELEMENT_DOCS_PATH", None)
        environment.pop("ELEMENT_PROJECT_PATH", None)
        environment["ELEMENT_MCP_CONFIG_PATH"] = str(tmp_path / "config.json")
        environment["ELEMENT_MCP_DATA_PATH"] = str(tmp_path / "data")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "element_mcp", "--transport", "stdio"],
            env=environment,
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool("get_documentation_status", {})
            assert result.isError is False
            assert result.structuredContent is not None
            assert result.structuredContent["status"] == "missing"

            project = await session.call_tool("get_project_status", {})
            assert project.isError is False
            assert project.structuredContent is not None
            assert project.structuredContent["status"] == "missing"

    asyncio.run(exercise_server())
