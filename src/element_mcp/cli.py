from __future__ import annotations

import argparse
import ipaddress
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from element_mcp import __version__
from element_mcp.config import ServerSettings
from element_mcp.server import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP server for 1C:Enterprise.Element documentation and projects")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("ELEMENT_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--corpus-path", type=Path, default=None)
    parser.add_argument(
        "--project-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_PROJECT_PATH")) else None,
    )
    parser.add_argument(
        "--element-bundle-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_BUNDLE_PATH")) else None,
    )
    parser.add_argument(
        "--java-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_JAVA_PATH")) else None,
    )
    parser.add_argument(
        "--console-config-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_CONSOLE_CONFIG_PATH")) else None,
    )
    parser.add_argument(
        "--runtime-config-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_RUNTIME_CONFIG_PATH")) else None,
    )
    parser.add_argument(
        "--actions-config-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_ACTIONS_CONFIG_PATH")) else None,
    )
    parser.add_argument(
        "--ide-settings-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_IDE_SETTINGS_PATH")) else None,
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_MCP_CONFIG_PATH")) else None,
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_MCP_DATA_PATH")) else None,
    )
    parser.add_argument("--host", default=os.environ.get("ELEMENT_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("ELEMENT_MCP_PORT", "9900"))
    parser.add_argument(
        "--update-repository-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_MCP_UPDATE_REPOSITORY_PATH")) else None,
    )
    parser.add_argument(
        "--update-source-path",
        type=Path,
        default=Path(value) if (value := os.environ.get("ELEMENT_MCP_UPDATE_SOURCE_PATH")) else None,
    )
    parser.add_argument("--update-revision", default=os.environ.get("ELEMENT_MCP_UPDATE_REVISION", "master"))
    parser.add_argument("--update-task-name", default=os.environ.get("ELEMENT_MCP_UPDATE_TASK_NAME"))
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
            f"Версия {__version__} разрешает Streamable HTTP только на loopback-интерфейсе: "
            "аутентификация удалённого доступа ещё не реализована."
        )
    if not args.update_revision or any(character.isspace() for character in args.update_revision):
        parser.error("--update-revision должен быть непустым именем ветки или ревизии без пробелов")

    settings = ServerSettings(
        corpus_path=args.corpus_path.expanduser().resolve() if args.corpus_path else None,
        project_path=args.project_path.expanduser().resolve() if args.project_path else None,
        element_bundle_path=(args.element_bundle_path.expanduser().resolve() if args.element_bundle_path else None),
        java_path=args.java_path.expanduser().resolve() if args.java_path else None,
        console_config_path=(args.console_config_path.expanduser().resolve() if args.console_config_path else None),
        runtime_config_path=(args.runtime_config_path.expanduser().resolve() if args.runtime_config_path else None),
        actions_config_path=(args.actions_config_path.expanduser().resolve() if args.actions_config_path else None),
        ide_settings_path=args.ide_settings_path.expanduser().resolve() if args.ide_settings_path else None,
        config_path=args.config_path.expanduser().resolve() if args.config_path else None,
        data_path=args.data_path.expanduser().resolve() if args.data_path else None,
        transport=args.transport,
        host=args.host,
        port=args.port,
        update_repository_path=(
            args.update_repository_path.expanduser().resolve() if args.update_repository_path else None
        ),
        update_source_path=args.update_source_path.expanduser().resolve() if args.update_source_path else None,
        update_revision=args.update_revision,
        update_task_name=args.update_task_name,
    )
    server = create_server(settings)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        server.run(transport=args.transport)
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Сервер остановлен")
    return 0
