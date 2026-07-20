from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from element_mcp import __version__
from element_mcp.config import ServerSettings
from element_mcp.corpus import CorpusRepository

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def create_server(settings: ServerSettings) -> FastMCP:
    repository = CorpusRepository(settings.corpus_path)
    server = FastMCP(
        name="1C Element Documentation",
        instructions=(
            "Use these read-only tools to verify claims about 1C:Enterprise.Element. "
            "Search first, then read the selected chunk with get_document. "
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
        return {"server_version": __version__, **repository.info()}

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def search_docs(
        query: Annotated[str, Field(min_length=2, max_length=500)],
        corpus: Literal["lang", "console", "server", "all"] = "all",
        limit: Annotated[int, Field(ge=1, le=20)] = 8,
        current_only: bool = True,
        product_version: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Search the normalized Element corpus and return ranked chunks with versioned provenance."""
        return repository.search(
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
        return repository.document_context(chunk_id=chunk_id, context_chunks=context_chunks)

    return server
