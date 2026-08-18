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
            if (
                key.startswith("ELEMENT_CONSOLE_")
                or key.startswith("ELEMENT_APPLICATION_MANAGER_")
                or key in {"ELEMENT_IDE_SETTINGS_PATH", "ELEMENT_INSTANCE_ROOT"}
            ):
                environment.pop(key)
        environment["ELEMENT_DOCS_PATH"] = str(corpus_path)
        environment["ELEMENT_PROJECT_PATH"] = str(element_project_path)
        environment["ELEMENT_MCP_CONFIG_PATH"] = str(tmp_path / "config.json")
        environment["ELEMENT_MCP_DATA_PATH"] = str(tmp_path / "data")
        environment["ELEMENT_RUNTIME_CONFIG_PATH"] = str(tmp_path / "runtime.json")
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
            assert initialized.serverInfo.version == "0.20.0"
            assert initialized.instructions is not None
            assert "first call\nget_documentation_status" in initialized.instructions
            assert "Never call start_documentation_build without that consent" in initialized.instructions
            assert "call discover_element_installations" in initialized.instructions
            assert (
                "poll get_documentation_build_status until completed, failed, or cancelled" in initialized.instructions
            )
            assert "call\nlist_reference_datasets" in initialized.instructions
            assert "first call get_project_status" in initialized.instructions
            assert "Never read paths outside the connected project" in initialized.instructions
            assert "first call get_console_status" in initialized.instructions
            assert "Never ask the user to paste Client-Secret" in initialized.instructions
            assert "Use lookup_symbol for declarations" in initialized.instructions
            assert "first call get_language_server_status" in initialized.instructions
            assert "If exact tools report a lexical fallback" in initialized.instructions
            assert "Use get_hover for the type and documentation" in initialized.instructions
            assert "Use get_signature_help only at a call" in initialized.instructions
            assert "An empty LSP response means" in initialized.instructions
            assert "For runtime incidents, first call get_runtime_health" in initialized.instructions
            assert "never correlate entries" in initialized.instructions
            assert "call get_current_application" in initialized.instructions
            assert "Do not infer\na current application in ordinary VS Code" in initialized.instructions
            assert "use get_element_dependencies or get_project_dependency_graph" in initialized.instructions
            assert "Never\ndescribe graph output as compiler-proven" in initialized.instructions
            assert "Never launch or\nrecommend a second Git process inside Element IDE" in initialized.instructions
            assert "first call get_managed_actions_status" in initialized.instructions
            assert "Never retry a write" in initialized.instructions
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "activate_documentation",
                "analyze_change_impact",
                "cancel_documentation_build",
                "connect_project",
                "configure_language_server",
                "discover_element_installations",
                "get_corpus_info",
                "list_reference_datasets",
                "query_reference",
                "get_api_operation",
                "get_api_schema",
                "get_server_component",
                "get_server_entrypoint",
                "get_component_connections",
                "get_console_project",
                "get_console_server_info",
                "get_console_status",
                "get_console_task",
                "get_managed_actions_status",
                "get_application",
                "get_application_event",
                "get_application_project",
                "get_application_status",
                "get_application_technology",
                "get_current_application",
                "get_document",
                "get_documentation_build_status",
                "get_documentation_status",
                "get_element_dependencies",
                "get_definition",
                "get_hover",
                "get_language_server_status",
                "get_runtime_health",
                "get_server_disk_usage",
                "get_server_process_status",
                "get_project_overview",
                "get_project_status",
                "get_project_diagnostics",
                "get_project_dependency_graph",
                "get_references",
                "get_signature_help",
                "get_related_docs",
                "lookup_symbol",
                "find_references",
                "find_unused_project_elements",
                "get_changed_elements",
                "list_project_elements",
                "list_server_logs",
                "list_console_spaces",
                "list_console_tasks",
                "list_application_endpoints",
                "list_project_assemblies",
                "list_space_applications",
                "list_space_projects",
                "match_console_project",
                "get_project_assembly",
                "read_project_file",
                "read_server_log",
                "prepare_upload_project_assembly",
                "upload_project_assembly",
                "prepare_update_application",
                "update_application",
                "prepare_application_state_change",
                "start_application",
                "stop_application",
                "wait_console_task",
                "search_application_events",
                "search_docs",
                "search_project_code",
                "search_server_logs",
                "start_documentation_build",
                "validate_element_structure",
                "trace_operation",
            }
            read_only = {
                "analyze_change_impact",
                "discover_element_installations",
                "find_unused_project_elements",
                "get_changed_elements",
                "get_corpus_info",
                "list_reference_datasets",
                "query_reference",
                "get_api_operation",
                "get_api_schema",
                "get_server_component",
                "get_server_entrypoint",
                "get_component_connections",
                "get_console_project",
                "get_console_server_info",
                "get_console_status",
                "get_console_task",
                "get_managed_actions_status",
                "get_application",
                "get_application_event",
                "get_application_project",
                "get_application_status",
                "get_application_technology",
                "get_current_application",
                "get_document",
                "get_documentation_build_status",
                "get_documentation_status",
                "get_element_dependencies",
                "get_definition",
                "get_hover",
                "get_language_server_status",
                "get_runtime_health",
                "get_server_disk_usage",
                "get_server_process_status",
                "get_project_overview",
                "get_project_status",
                "get_project_diagnostics",
                "get_project_dependency_graph",
                "get_references",
                "get_signature_help",
                "get_related_docs",
                "lookup_symbol",
                "find_references",
                "list_project_elements",
                "list_server_logs",
                "list_console_spaces",
                "list_console_tasks",
                "list_application_endpoints",
                "list_project_assemblies",
                "list_space_applications",
                "list_space_projects",
                "match_console_project",
                "get_project_assembly",
                "read_project_file",
                "read_server_log",
                "search_application_events",
                "search_docs",
                "search_project_code",
                "search_server_logs",
                "trace_operation",
                "validate_element_structure",
                "wait_console_task",
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
            assert "exact published application" in (descriptions["get_current_application"] or "")
            assert "without guessing" in (descriptions["get_console_server_info"] or "")
            assert "explicit application" in (descriptions["get_application"] or "")
            assert "without certificates" in (descriptions["list_application_endpoints"] or "")
            assert "deployment-instance" in (descriptions["list_console_tasks"] or "")
            assert "bounded local workspace candidates" in (descriptions["match_console_project"] or "")
            assert "lexical ambiguity" in (descriptions["lookup_symbol"] or "")
            assert "compiler-level" in (descriptions["find_references"] or "")
            assert "Element LSP" in (descriptions["get_definition"] or "")
            assert "Element LSP" in (descriptions["get_references"] or "")
            assert "type and documentation" in (descriptions["get_hover"] or "")
            assert "active parameter" in (descriptions["get_signature_help"] or "")
            assert "runtime incidents" in (descriptions["get_runtime_health"] or "")
            assert "arbitrary paths are rejected" in (descriptions["read_server_log"] or "")
            assert "mandatory bounded time range" in (descriptions["search_application_events"] or "")
            assert "exact IDs" in (descriptions["trace_operation"] or "")
            assert "published by Element LSP" in (descriptions["get_project_diagnostics"] or "")
            assert "structured datasets" in (descriptions["list_reference_datasets"] or "")
            assert "resolved schemas" in (descriptions["get_api_operation"] or "")
            assert "evidence and confidence" in (descriptions["get_element_dependencies"] or "")
            assert "never proves safe deletion" in (descriptions["find_unused_project_elements"] or "")
            assert "never uploads" in (descriptions["prepare_upload_project_assembly"] or "")
            assert "explicit user confirmation" in (descriptions["upload_project_assembly"] or "")

            result = await session.call_tool("search_docs", {"query": "ВебМетод", "corpus": "lang"})
            assert result.isError is False
            assert result.structuredContent is not None
            assert result.structuredContent["count"] >= 1

            references = await session.call_tool("list_reference_datasets", {"corpus": "console"})
            assert references.isError is False
            assert references.structuredContent is not None
            assert references.structuredContent["total"] == 2

            operation = await session.call_tool(
                "get_api_operation",
                {"method": "GET", "path": "/console/api/v2/projects/{ProjectId}"},
            )
            assert operation.isError is False
            assert operation.structuredContent is not None
            assert operation.structuredContent["resolved_schemas"][0]["title"] == "ProjectDto"

            overview = await session.call_tool("get_project_overview", {})
            assert overview.isError is False
            assert overview.structuredContent is not None
            assert overview.structuredContent["project"]["name"] == "ExampleProject"

            graph = await session.call_tool("get_element_dependencies", {"identifier": "Orders"})
            assert graph.isError is False
            assert graph.structuredContent is not None
            assert graph.structuredContent["status"] == "ready"
            assert graph.structuredContent["semantic_guarantee"] is False

            changed = await session.call_tool(
                "get_changed_elements",
                {"changed_paths": ["Sales/Orders.xbsl"]},
            )
            assert changed.isError is False
            assert changed.structuredContent is not None
            assert changed.structuredContent["changes"][0]["element"]["name"] == "Orders"

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

            hover = await session.call_tool(
                "get_hover",
                {"relative_path": "Sales/Orders.xbsl", "line": 1, "column": 10},
            )
            assert hover.isError is False
            assert hover.structuredContent is not None
            assert hover.structuredContent["lsp_status"] == "stopped"
            assert hover.structuredContent["analysis_mode"] == "syntax-aware lexical fallback"

            signature = await session.call_tool(
                "get_signature_help",
                {"relative_path": "Sales/Orders.xbsl", "line": 1, "column": 20},
            )
            assert signature.isError is False
            assert signature.structuredContent is not None
            assert signature.structuredContent["lsp_status"] == "stopped"
            assert signature.structuredContent["analysis_mode"] == "syntax-aware lexical fallback"

            related = await session.call_tool("get_related_docs", {"symbol": "FindOrder", "limit": 2})
            assert related.isError is False
            assert related.structuredContent is not None
            assert related.structuredContent["documentation"]["count"] >= 1

            console = await session.call_tool("get_console_status", {})
            assert console.isError is False
            assert console.structuredContent is not None
            assert console.structuredContent["status"] == "missing"

            server_info = await session.call_tool("get_console_server_info", {})
            assert server_info.isError is False
            assert server_info.structuredContent is not None
            assert server_info.structuredContent["status"] == "missing"
            assert server_info.structuredContent["contract_element_version"] == "9.2.4-6"

            explicit_application = await session.call_tool(
                "get_application",
                {"application_id": "cccccccc-cccc-cccc-cccc-cccccccccccc"},
            )
            assert explicit_application.isError is False
            assert explicit_application.structuredContent is not None
            assert explicit_application.structuredContent["status"] == "missing"

            application = await session.call_tool("get_current_application", {})
            assert application.isError is False
            assert application.structuredContent is not None
            assert application.structuredContent["status"] == "not_available"

            runtime = await session.call_tool("get_runtime_health", {})
            assert runtime.isError is False
            assert runtime.structuredContent is not None
            assert runtime.structuredContent["status"] == "missing"

            managed = await session.call_tool("get_managed_actions_status", {})
            assert managed.isError is False
            assert managed.structuredContent is not None
            assert managed.structuredContent["status"] == "disabled"

    asyncio.run(exercise_server())


def test_stdio_server_reports_missing_corpus(tmp_path: Path) -> None:
    async def exercise_server() -> None:
        environment = dict(os.environ)
        for key in list(environment):
            if (
                key.startswith("ELEMENT_CONSOLE_")
                or key.startswith("ELEMENT_APPLICATION_MANAGER_")
                or key in {"ELEMENT_IDE_SETTINGS_PATH", "ELEMENT_INSTANCE_ROOT"}
            ):
                environment.pop(key)
        environment.pop("ELEMENT_DOCS_PATH", None)
        environment.pop("ELEMENT_PROJECT_PATH", None)
        environment["ELEMENT_MCP_CONFIG_PATH"] = str(tmp_path / "config.json")
        environment["ELEMENT_MCP_DATA_PATH"] = str(tmp_path / "data")
        environment["ELEMENT_RUNTIME_CONFIG_PATH"] = str(tmp_path / "runtime.json")
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
