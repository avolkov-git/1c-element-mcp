from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from element_mcp import __version__
from element_mcp.config import ServerSettings
from element_mcp.console import ConsoleService
from element_mcp.documentation import DocumentationService
from element_mcp.installation import discover_element_installations as find_element_installations
from element_mcp.language_server import LanguageServerService
from element_mcp.project import ProjectService
from element_mcp.semantic import SemanticService
from element_mcp.ui import register_ui
from element_mcp.updates import UpdateService

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
EXTERNAL_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
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
active corpus. For exact inventories, Console API contracts, schemas, server components, and startup chains, call
list_reference_datasets and then a typed reference tool or query_reference. Use search_docs and get_document for
explanations and prose. Prefer typed reference tools over generic query_reference when one exists. Never guess a
dataset id: list it first. Preserve product_version, source_version, dataset_id, and provenance whenever they matter.

For questions about the user's Element source project, first call get_project_status. If it is missing, ask for
the exact project root and call connect_project only with a path explicitly supplied or confirmed by the user.
Treat the connected root as read-only. Call get_project_overview before structural analysis, use
list_project_elements to understand YAML metadata and companion XBSL/XBQL files, then search_project_code and
read_project_file for implementation details. Never infer an Element object from XBSL alone: pair code with its
YAML metadata, subsystem, environment, and visibility. Never read paths outside the connected project.
Use lookup_symbol for declarations and find_references for identifier occurrences. Their index is syntax-aware
but lexical, not a compiler or Element Language Server: preserve ambiguity and never present a medium-confidence
reference as proven symbol resolution. Use get_related_docs to turn a symbol or project file into a documentation
query, then call get_document for the selected chunk.

For compiler-level navigation and diagnostics, first call get_language_server_status. If it is ready, prefer
get_definition, get_references, and get_project_diagnostics over lexical tools. These tools start the official
Element Language Server lazily and keep one isolated process for the active project. Preserve analysis_mode,
semantic_guarantee, source, and fallback_reason in the answer. If the Language Server is missing, call
discover_element_installations and ask the user to choose a bundle when needed; call configure_language_server
only with a user-confirmed bundle path. Never ask the user to paste a shell command as a Language Server command.
If exact tools report a lexical fallback, say that the result is not compiler-proven. Lexical fallback cannot
replace compiler diagnostics.

For questions about projects available through the Element Management Console, first call get_console_status.
The Console connection is optional and must come from the MCP process environment, a configured console file, the
local MCP UI, or an explicitly selected Element/VS Code settings file. Never ask the user to paste Client-Secret or
an access token into chat or a tool argument, and never expose credentials in an answer. If the status is missing,
explain the supported configuration sources. If it is unauthenticated or forbidden, distinguish the authorization
failure from an empty project list. Use list_console_spaces when the space is ambiguous. Use list_space_projects
for the catalog and get_console_project for one project. In an Element IDE context, omit space_id first: the server
can derive it from 1C.projectId. To associate Console metadata with local source, call match_console_project. In
Element IDE it uses the workspace and Git status supplied by the official g5rt.team.status command. In VS Code,
omit workspace_path only for stdio launched from the workspace; otherwise pass a user-confirmed local workspace.
An exact name is only a suggestion, not proof. Call connect_project only after the user confirms a candidate, unless
get_project_status already reports that the sole IDE project was selected automatically.

For questions about the application currently attached to Element IDE, call get_current_application. This tool
has no application_id argument and works only with the temporary Element plugin handoff containing
1C.applicationId. Treat it as the exact published application instance selected for the current IDE. Do not infer
a current application in ordinary VS Code from the project name, source tree, or standalone Console credentials.
""".strip()


def create_server(settings: ServerSettings) -> FastMCP:
    documentation = DocumentationService(settings)
    project = ProjectService(settings)
    semantic = SemanticService(project, documentation)
    language_server = LanguageServerService(settings, project, semantic)
    console = ConsoleService(settings)
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
    register_ui(server, settings, updates, documentation, console, project)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_corpus_info() -> dict[str, Any]:
        """Return available corpora, Element versions, index metadata, and MCP server version."""
        return {"server_version": __version__, **documentation.corpus_info()}

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def list_reference_datasets(
        corpus: Literal["lang", "console", "server"] | None = None,
        product_version: Annotated[str | None, Field(max_length=64)] = None,
        name: Annotated[str | None, Field(max_length=128)] = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List exact structured datasets in the active corpus before using query_reference."""
        return documentation.references().list_datasets(
            corpus=corpus,
            product_version=product_version,
            name=name,
            offset=offset,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def query_reference(
        dataset_id: Annotated[str, Field(min_length=1, max_length=256)],
        query: Annotated[str | None, Field(max_length=500)] = None,
        filters: Annotated[
            dict[str, str] | None,
            Field(description="Up to 8 exact, case-insensitive field filters"),
        ] = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Query one listed structured dataset with bounded text search, exact filters, and pagination."""
        return documentation.references().query(
            dataset_id,
            query=query,
            filters=filters,
            offset=offset,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_api_operation(
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        path: Annotated[str, Field(min_length=1, max_length=512)],
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Get one exact Console API operation and its resolved schemas from the normalized bundle documentation."""
        return documentation.references().get_api_operation(method, path, product_version)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_api_schema(
        title: Annotated[str, Field(min_length=1, max_length=256)],
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Get one exact Console API schema, properties, example, version, and provenance."""
        return documentation.references().get_api_schema(title, product_version)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_server_component(
        component_id: Annotated[str, Field(min_length=1, max_length=128)],
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Get one structured server-bundle component and its evidence paths."""
        return documentation.references().get_server_component(component_id, product_version)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_server_entrypoint(
        entrypoint_id: Annotated[str, Field(min_length=1, max_length=128)],
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Get one server or embedded IDE startup entrypoint and its launch chain."""
        return documentation.references().get_server_entrypoint(entrypoint_id, product_version)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_component_connections(
        component_id: Annotated[str, Field(min_length=1, max_length=128)],
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """List verified structured connections involving one server-bundle component."""
        return documentation.references().get_component_connections(component_id, product_version)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_project_status() -> dict[str, Any]:
        """Call first for project questions to check whether a valid Element source project is connected."""
        return {"server_version": __version__, **project.project_status()}

    @server.tool(annotations=EXTERNAL_READ_ONLY, structured_output=True)
    def get_console_status() -> dict[str, Any]:
        """Call first for Console questions; verify configured auth without returning tokens or secrets."""
        return {"server_version": __version__, **console.status()}

    @server.tool(annotations=EXTERNAL_READ_ONLY, structured_output=True)
    def list_console_spaces() -> dict[str, Any]:
        """List spaces visible to the configured Element Management Console identity."""
        return console.list_spaces()

    @server.tool(annotations=EXTERNAL_READ_ONLY, structured_output=True)
    def list_space_projects(
        space_id: Annotated[str | None, Field(max_length=64)] = None,
        include_deleted: bool = False,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """List project metadata in a space; omit space_id to use the current IDE project or sole visible space."""
        return console.list_space_projects(
            space_id=space_id,
            include_deleted=include_deleted,
            offset=offset,
            limit=limit,
        )

    @server.tool(annotations=EXTERNAL_READ_ONLY, structured_output=True)
    def get_console_project(
        project_id: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Read one Console project; omit project_id to use 1C.projectId from the IDE context."""
        return console.get_project(project_id)

    @server.tool(annotations=EXTERNAL_READ_ONLY, structured_output=True)
    def get_current_application() -> dict[str, Any]:
        """Read the exact published application attached to the active Element IDE; unavailable in ordinary VS Code."""
        return {"server_version": __version__, **console.get_current_application()}

    @server.tool(annotations=EXTERNAL_READ_ONLY, structured_output=True)
    def match_console_project(
        project_id: Annotated[str | None, Field(max_length=64)] = None,
        workspace_path: Annotated[str | None, Field(max_length=4096)] = None,
    ) -> dict[str, Any]:
        """Match a Console project to bounded local workspace candidates without connecting or modifying them."""
        console_result = console.get_project(project_id)
        if console_result.get("status") != "ready":
            return console_result
        return project.match_console_project(console_result["project"], workspace_path)

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
    def get_language_server_status(start: bool = False) -> dict[str, Any]:
        """Check the official Element Language Server runtime; optionally start its isolated project process."""
        return {"server_version": __version__, **language_server.status(start=start)}

    @server.tool(annotations=LOCAL_IDEMPOTENT_WRITE, structured_output=True)
    def configure_language_server(
        bundle_path: Annotated[str, Field(min_length=1, max_length=4096)],
        java_path: Annotated[str | None, Field(max_length=4096)] = None,
    ) -> dict[str, Any]:
        """Validate and save a user-confirmed Element bundle and optional Java 11+ path for exact semantics."""
        return {
            "server_version": __version__,
            **language_server.configure(bundle_path, java_path=java_path),
        }

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_definition(
        relative_path: Annotated[str, Field(min_length=1, max_length=4096)],
        line: Annotated[int, Field(ge=1)],
        column: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Resolve a definition at a 1-based project position via Element LSP, with an explicit lexical fallback."""
        return language_server.definition(relative_path, line, column)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_references(
        relative_path: Annotated[str, Field(min_length=1, max_length=4096)],
        line: Annotated[int, Field(ge=1)],
        column: Annotated[int, Field(ge=1)],
        include_declaration: bool = True,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> dict[str, Any]:
        """Resolve references at a 1-based project position via Element LSP, with an explicit lexical fallback."""
        return language_server.references(
            relative_path,
            line,
            column,
            include_declaration=include_declaration,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_project_diagnostics(
        relative_path: Annotated[str | None, Field(max_length=4096)] = None,
        wait_seconds: Annotated[float, Field(ge=0, le=10)] = 2.0,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        """Return diagnostics published by Element LSP; scope to one relative file or the current project cache."""
        return language_server.diagnostics(
            relative_path=relative_path,
            wait_seconds=wait_seconds,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def lookup_symbol(
        name: Annotated[str, Field(min_length=1, max_length=256)],
        symbol_kind: Literal["all", "element", "method", "type", "structure", "enumeration", "exception"] = "all",
        exact: bool = True,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        """Find Element YAML entities and XBSL declarations, preserving overloads and lexical ambiguity."""
        return semantic.lookup_symbol(name, symbol_kind=symbol_kind, exact=exact, limit=limit)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def find_references(
        name: Annotated[str, Field(min_length=1, max_length=256)],
        file_type: Literal["all", "metadata", "xbsl", "xbql"] = "all",
        relative_path: Annotated[str | None, Field(max_length=4096)] = None,
        include_declarations: bool = False,
        case_sensitive: bool = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """Find exact identifier occurrences without claiming compiler-level symbol resolution."""
        return semantic.find_references(
            name,
            file_type=file_type,
            relative_path=relative_path,
            include_declarations=include_declarations,
            case_sensitive=case_sensitive,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_related_docs(
        symbol: Annotated[str | None, Field(max_length=256)] = None,
        relative_path: Annotated[str | None, Field(max_length=4096)] = None,
        corpus: Literal["lang", "console", "server", "all"] = "lang",
        limit: Annotated[int, Field(ge=1, le=20)] = 8,
        current_only: bool = True,
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Search normalized docs using context derived from a project symbol, source file, or both."""
        return semantic.related_docs(
            symbol=symbol,
            relative_path=relative_path,
            corpus=corpus,
            limit=limit,
            current_only=current_only,
            product_version=product_version,
        )

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
