from __future__ import annotations

import argparse
import ipaddress
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from element_mcp import __version__
from element_mcp.config import ConfigurationError, ServerSettings, discover_corpus_path
from element_mcp.corpus import CorpusError
from element_mcp.server import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP server for 1C:Enterprise.Element documentation")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("ELEMENT_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--corpus-path", type=Path, default=None)
    parser.add_argument("--host", default=os.environ.get("ELEMENT_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("ELEMENT_MCP_PORT", "8000"))
    return parser


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port должен находиться в диапазоне 1..65535")
    if args.transport == "streamable-http" and not _is_loopback(args.host):
        parser.error(
            "Версия 0.1.0 разрешает Streamable HTTP только на loopback-интерфейсе: "
            "аутентификация удалённого доступа ещё не реализована."
        )

    try:
        corpus_path = discover_corpus_path(args.corpus_path)
        settings = ServerSettings(
            corpus_path=corpus_path,
            transport=args.transport,
            host=args.host,
            port=args.port,
        )
        server = create_server(settings)
    except (ConfigurationError, CorpusError) as error:
        parser.error(str(error))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        server.run(transport=args.transport)
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Сервер остановлен")
    return 0
