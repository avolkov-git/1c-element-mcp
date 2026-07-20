from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from element_mcp import updater
from element_mcp.config import ConfigurationStore, ServerSettings
from element_mcp.updates import UpdateError, UpdateService, safe_error_detail, safe_source_label


def git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def write_version(path: Path, version: str) -> None:
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "1c-element-mcp"\nversion = "{version}"\n',
        encoding="utf-8",
    )


def make_update_repositories(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "master")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    write_version(source, "0.4.0")
    git(source, "add", "pyproject.toml")
    git(source, "commit", "-m", "initial")

    target = tmp_path / "target"
    subprocess.run(["git", "clone", "--quiet", str(source), str(target)], check=True)
    previous_commit = git(target, "rev-parse", "HEAD")

    write_version(source, "0.4.1")
    git(source, "add", "pyproject.toml")
    git(source, "commit", "-m", "update")
    return source, target, previous_commit


def test_local_source_reports_available_update(tmp_path: Path) -> None:
    source, target, _ = make_update_repositories(tmp_path)
    service = UpdateService(
        ServerSettings(
            data_path=tmp_path / "data",
            update_repository_path=target,
            update_source_path=source,
        )
    )

    status = service.check()

    assert status["updates"]["state"] == "available"
    assert status["updates"]["available_version"] == "0.4.1"
    assert status["updates"]["source"]["kind"] == "local"


def test_configure_source_persists_valid_local_repository(tmp_path: Path) -> None:
    source, target, _ = make_update_repositories(tmp_path)
    config_path = tmp_path / "config.json"
    service = UpdateService(
        ServerSettings(
            config_path=config_path,
            data_path=tmp_path / "data",
            update_repository_path=target,
        )
    )

    status = service.configure_source(str(source))

    assert status["updates"]["source"] == {
        "kind": "local",
        "label": str(source.resolve()),
        "revision": "master",
    }
    assert service.configuration.update_source().path == source.resolve()  # type: ignore[union-attr]


def test_configure_source_rejects_unrelated_git_repository(tmp_path: Path) -> None:
    source = tmp_path / "unrelated"
    source.mkdir()
    git(source, "init", "-b", "master")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "different-project"\nversion = "9.9.9"\n',
        encoding="utf-8",
    )
    git(source, "add", "pyproject.toml")
    git(source, "commit", "-m", "initial")
    service = UpdateService(ServerSettings(config_path=tmp_path / "config.json", data_path=tmp_path / "data"))

    with pytest.raises(UpdateError, match="корректная версия MCP"):
        service.configure_source(str(source))


def test_closed_network_failure_does_not_report_server_failure(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "master")
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "user.name", "Test")
    write_version(repository, "0.4.0")
    git(repository, "add", "pyproject.toml")
    git(repository, "commit", "-m", "initial")
    git(repository, "remote", "add", "origin", "https://127.0.0.1:1/unavailable.git")
    service = UpdateService(ServerSettings(data_path=tmp_path / "data", update_repository_path=repository))

    status = service.check()

    assert status["server"]["state"] == "running"
    assert status["updates"]["state"] == "unavailable"
    assert status["updates"]["message"] == "Проверка обновлений недоступна. MCP продолжает работать."


def test_updater_fast_forwards_managed_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, _ = make_update_repositories(tmp_path)
    monkeypatch.setattr(updater, "_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, "_install", lambda *args, **kwargs: None)
    monkeypatch.setattr(updater.time, "sleep", lambda *args: None)

    result = updater.perform_update(
        repository_path=target,
        source_path=source,
        revision="master",
        server_task_name="test-server",
        status_path=tmp_path / "status.json",
    )

    assert result["state"] == "success"
    assert git(target, "show", "HEAD:pyproject.toml").endswith('version = "0.4.1"')


def test_updater_rolls_back_when_installation_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, previous_commit = make_update_repositories(tmp_path)
    attempts = 0

    def install(_path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise UpdateError("simulated install failure")

    monkeypatch.setattr(updater, "_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, "_install", install)
    monkeypatch.setattr(updater.time, "sleep", lambda *args: None)

    result = updater.perform_update(
        repository_path=target,
        source_path=source,
        revision="master",
        server_task_name="test-server",
        status_path=tmp_path / "status.json",
    )

    assert result["state"] == "error"
    assert result["rolled_back"] is True
    assert git(target, "rev-parse", "HEAD") == previous_commit


def test_updater_reads_selected_source_from_shared_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    selected_source = tmp_path / "selected-source"

    ConfigurationStore(config_path).configure_update_source(selected_source)
    captured: dict[str, object] = {}

    def perform(**arguments: object) -> dict[str, object]:
        captured.update(arguments)
        return {"state": "current"}

    monkeypatch.setattr(updater, "perform_update", perform)

    result = updater.main(
        [
            "--repository-path",
            str(tmp_path / "managed"),
            "--source-path",
            str(tmp_path / "installer-source"),
            "--config-path",
            str(config_path),
            "--server-task-name",
            "test-server",
            "--status-path",
            str(tmp_path / "status.json"),
        ]
    )

    assert result == 0
    assert captured["source_path"] == selected_source.resolve()


def test_safe_source_label_removes_url_credentials() -> None:
    assert safe_source_label("https://token@example.com/owner/repo.git") == "https://example.com/owner/repo.git"


def test_safe_error_detail_removes_url_credentials() -> None:
    detail = "fatal: unable to access 'https://user:secret@example.com/owner/repo.git/'"
    assert "secret" not in safe_error_detail(detail)
    assert "https://example.com/owner/repo.git/" in safe_error_detail(detail)
