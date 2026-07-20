from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from element_mcp import __version__
from element_mcp.cli import build_parser, main


def test_package_version_matches_pyproject() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == project["project"]["version"] == "0.5.0"


def test_default_http_port_is_9900(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEMENT_MCP_PORT", raising=False)
    assert build_parser().parse_args([]).port == 9900


def test_http_refuses_public_bind_without_authentication() -> None:
    with pytest.raises(SystemExit) as error:
        main(["--transport", "streamable-http", "--host", "0.0.0.0"])
    assert error.value.code == 2


def test_invalid_environment_port_has_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEMENT_MCP_PORT", "invalid")
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([])
    assert error.value.code == 2


def test_update_arguments_are_parsed_as_paths(tmp_path: Path) -> None:
    arguments = build_parser().parse_args(
        [
            "--update-repository-path",
            str(tmp_path / "managed"),
            "--update-source-path",
            str(tmp_path / "mirror"),
            "--update-revision",
            "release",
            "--update-task-name",
            "Updater",
        ]
    )
    assert arguments.update_repository_path == tmp_path / "managed"
    assert arguments.update_source_path == tmp_path / "mirror"
    assert arguments.update_revision == "release"
    assert arguments.update_task_name == "Updater"
