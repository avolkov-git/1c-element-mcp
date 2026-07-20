from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from element_mcp import __version__
from element_mcp.config import ServerSettings
from element_mcp.documentation import DocumentationService
from element_mcp.installation import discover_element_installations as find_element_installations

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
LOCAL_IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
CANCEL_JOB = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


def create_server(settings: ServerSettings) -> FastMCP:
    documentation = DocumentationService(settings)
    server = FastMCP(
        name="1C Element Documentation",
        instructions=(
            "First call get_documentation_status when the user asks whether normalized documentation exists. "
            "If it is missing, ask before creating hundreds of megabytes, discover an installed Element bundle, "
            "then start and monitor a local documentation build. Search first, then read the selected chunk. "
            "Preserve product_version, source_version and provenance in answers."
        ),
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        json_response=True,
    )
    # FastMCP 1.x does not expose the low-level server version in its constructor.
    # Without this assignment clients would see the SDK version instead of our SemVer.
    server._mcp_server.version = __version__

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_corpus_info() -> dict[str, Any]:
        """Return available corpora, Element versions, index metadata, and MCP server version."""
        return {"server_version": __version__, **documentation.corpus_info()}

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_documentation_status() -> dict[str, Any]:
        """Check whether a complete normalized lang, console, and server corpus is configured and valid."""
        return {"server_version": __version__, **documentation.documentation_status()}

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def discover_element_installations() -> dict[str, Any]:
        """Find valid Element server component bundles in standard Windows and Linux installation paths."""
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
        """Start a background local build of lang, console, server, JSONL, SQLite, and vector indexes."""
        return documentation.jobs.start(bundle_path=bundle_path, output_path=output_path)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_documentation_build_status(
        job_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
    ) -> dict[str, Any]:
        """Return progress and the final validation report for a documentation build job."""
        return documentation.jobs.status(job_id)

    @server.tool(annotations=CANCEL_JOB, structured_output=True)
    def cancel_documentation_build(
        job_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
    ) -> dict[str, Any]:
        """Stop a documentation build started by this MCP process without touching the active corpus."""
        return documentation.jobs.cancel(job_id)

    @server.tool(annotations=LOCAL_IDEMPOTENT_WRITE, structured_output=True)
    def activate_documentation(
        corpus_path: Annotated[str, Field(min_length=1, max_length=4096)],
    ) -> dict[str, Any]:
        """Fully validate an existing normalized corpus and save it as the active MCP corpus."""
        return documentation.activate(corpus_path)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def search_docs(
        query: Annotated[str, Field(min_length=2, max_length=500)],
        corpus: Literal["lang", "console", "server", "all"] = "all",
        limit: Annotated[int, Field(ge=1, le=20)] = 8,
        current_only: bool = True,
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Search the normalized Element corpus and return ranked chunks with versioned provenance."""
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
        """Read a search result and up to two neighboring chunks on each side from the same document."""
        return documentation.repository().document_context(chunk_id=chunk_id, context_chunks=context_chunks)

    return server
