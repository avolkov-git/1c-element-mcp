from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from element_mcp import __version__
from element_mcp.config import ServerSettings
from element_mcp.documentation import DocumentationService
from element_mcp.installation import discover_element_installations as find_element_installations
from element_mcp.project import ProjectService
from element_mcp.ui import register_ui
from element_mcp.updates import UpdateService

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
LOCAL_IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
CANCEL_JOB = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

SERVER_INSTRUCTIONS = """
For every question about normalized 1C:Enterprise.Element documentation, first call
get_documentation_status. If status is ready, report the exact corpus path, Element version, and document and
chunk counts; do not rebuild it. If status is invalid, report the validation errors, offer a rebuild, and never
activate the invalid corpus. If status is missing, explain that the corpus is absent and ask for explicit user
consent before creating it. Never call start_documentation_build without that consent. A build for Element
9.2.4-6 creates about 620 MB and can take several minutes.

After consent, call discover_element_installations. Use the only result after naming its path and version. If
there are multiple results, ask the user to select one. If there are none, ask for the Element server bundle
path and explain that a valid bundle contains docs, lib, ide, and executor. Do not scan the disk yourself and do
not treat a deployed server instance directory as a complete bundle.

Call start_documentation_build with bundle_path only unless the user explicitly chose output_path. Save job_id
and poll get_documentation_build_status until completed, failed, or cancelled. Do not start another build while
one is running. On completion, report the result path, Element and normalizer versions, document and chunk
counts, validation result, and that the corpus is active. On failure, report the stage and error; never claim the
corpus is active. A previous active corpus remains unchanged.

For an existing normalized corpus, call activate_documentation and report success only when status is ready.
Call cancel_documentation_build only after an explicit user request; cancellation does not remove or alter the
active corpus. For answers about Element, call search_docs first, then get_document for the selected chunk.
Preserve product_version, source_version, and provenance whenever they matter to the answer.

For questions about the user's Element source project, first call get_project_status. If it is missing, ask for
the exact project root and call connect_project only with a path explicitly supplied or confirmed by the user.
Treat the connected root as read-only. Call get_project_overview before structural analysis, use
list_project_elements to understand YAML metadata and companion XBSL/XBQL files, then search_project_code and
read_project_file for implementation details. Never infer an Element object from XBSL alone: pair code with its
YAML metadata, subsystem, environment, and visibility. Never read paths outside the connected project.
""".strip()


def create_server(settings: ServerSettings) -> FastMCP:
    documentation = DocumentationService(settings)
    project = ProjectService(settings)
    updates = UpdateService(settings)
    server = FastMCP(
        name="1C Element",
        instructions=SERVER_INSTRUCTIONS,
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        json_response=True,
    )
    # FastMCP 1.x does not expose the low-level server version in its constructor.
    # Without this assignment clients would see the SDK version instead of our SemVer.
    server._mcp_server.version = __version__
    register_ui(server, settings, updates)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_corpus_info() -> dict[str, Any]:
        """Return available corpora, Element versions, index metadata, and MCP server version."""
        return {"server_version": __version__, **documentation.corpus_info()}

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_project_status() -> dict[str, Any]:
        """Call first for project questions to check whether a valid Element source project is connected."""
        return {"server_version": __version__, **project.project_status()}

    @server.tool(annotations=LOCAL_IDEMPOTENT_WRITE, structured_output=True)
    def connect_project(
        project_path: Annotated[str, Field(min_length=1, max_length=4096)],
    ) -> dict[str, Any]:
        """Connect a user-confirmed Element project root without modifying any project file."""
        return {"server_version": __version__, **project.connect(project_path)}

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_project_overview() -> dict[str, Any]:
        """Summarize the active Element project, subsystems, element kinds, source files, and metadata issues."""
        return project.overview()

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def list_project_elements(
        query: Annotated[str | None, Field(max_length=300)] = None,
        element_kind: Annotated[str | None, Field(max_length=128)] = None,
        subsystem: Annotated[str | None, Field(max_length=1024)] = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List Element YAML entities with subsystem, environment, visibility, and companion XBSL/XBQL files."""
        return project.list_elements(
            query=query,
            element_kind=element_kind,
            subsystem=subsystem,
            offset=offset,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def search_project_code(
        query: Annotated[str, Field(min_length=2, max_length=300)],
        file_type: Literal["all", "metadata", "xbsl", "xbql"] = "all",
        case_sensitive: bool = False,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        """Search only UTF-8 Element YAML, XBSL, or XBQL files inside the connected project root."""
        return project.search(query, file_type=file_type, case_sensitive=case_sensitive, limit=limit)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def read_project_file(
        relative_path: Annotated[str, Field(min_length=1, max_length=4096)],
        start_line: Annotated[int, Field(ge=1)] = 1,
        line_count: Annotated[int, Field(ge=1, le=400)] = 200,
    ) -> dict[str, Any]:
        """Read bounded lines from an Element source file by a relative path returned by project tools."""
        return project.read_file(relative_path, start_line=start_line, line_count=line_count)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_documentation_status() -> dict[str, Any]:
        """Call first to classify the complete lang, console, and server corpus as ready, invalid, or missing."""
        return {"server_version": __version__, **documentation.documentation_status()}

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def discover_element_installations() -> dict[str, Any]:
        """After build consent, find valid Element bundles in standard Windows and Linux installation paths."""
        installations = find_element_installations()
        return {
            "count": len(installations),
            "installations": installations,
            "message": (
                "Найдены установленные серверные компоненты Element"
                if installations
                else "В стандартных каталогах серверный компонент Element не найден"
            ),
        }

    @server.tool(annotations=LOCAL_WRITE, structured_output=True)
    def start_documentation_build(
        bundle_path: Annotated[str, Field(min_length=1, max_length=4096)],
        output_path: Annotated[str | None, Field(max_length=4096)] = None,
    ) -> dict[str, Any]:
        """After explicit user consent, build lang, console, server, JSONL, SQLite, and vector indexes locally."""
        return documentation.jobs.start(bundle_path=bundle_path, output_path=output_path)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_documentation_build_status(
        job_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
    ) -> dict[str, Any]:
        """Poll a build job until completed, failed, or cancelled and return its final validation report."""
        return documentation.jobs.status(job_id)

    @server.tool(annotations=CANCEL_JOB, structured_output=True)
    def cancel_documentation_build(
        job_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
    ) -> dict[str, Any]:
        """Only on explicit request, stop this process's build without changing the active corpus."""
        return documentation.jobs.cancel(job_id)

    @server.tool(annotations=LOCAL_IDEMPOTENT_WRITE, structured_output=True)
    def activate_documentation(
        corpus_path: Annotated[str, Field(min_length=1, max_length=4096)],
    ) -> dict[str, Any]:
        """Validate an existing normalized corpus and activate it only when its status is ready."""
        return documentation.activate(corpus_path)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def search_docs(
        query: Annotated[str, Field(min_length=2, max_length=500)],
        corpus: Literal["lang", "console", "server", "all"] = "all",
        limit: Annotated[int, Field(ge=1, le=20)] = 8,
        current_only: bool = True,
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Search first for Element answers and return ranked chunks with versioned provenance."""
        return documentation.repository().search(
            query=query,
            corpus=corpus,
            limit=limit,
            current_only=current_only,
            product_version=product_version,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_document(
        chunk_id: Annotated[str, Field(min_length=1, max_length=512)],
        context_chunks: Annotated[int, Field(ge=0, le=2)] = 1,
    ) -> dict[str, Any]:
        """After search_docs, read the selected result and neighboring chunks from the same document."""
        return documentation.repository().document_context(chunk_id=chunk_id, context_chunks=context_chunks)

    return server
