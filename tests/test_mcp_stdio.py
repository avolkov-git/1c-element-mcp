from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_server_exposes_read_only_tools(corpus_path: Path) -> None:
    async def exercise_server() -> None:
        environment = dict(os.environ)
        environment["ELEMENT_DOCS_PATH"] = str(corpus_path)
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
            assert initialized.serverInfo.version == "0.1.0"
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {"get_corpus_info", "search_docs", "get_document"}
            for tool in tools.tools:
                assert tool.annotations is not None
                assert tool.annotations.readOnlyHint is True

            result = await session.call_tool("search_docs", {"query": "ВебМетод", "corpus": "lang"})
            assert result.isError is False
            assert result.structuredContent is not None
            assert result.structuredContent["count"] >= 1

    asyncio.run(exercise_server())
