from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from element_mcp.config import ServerSettings
from element_mcp.console import (
    ConsoleConfigurationError,
    ConsoleContextResolver,
    ConsoleHttpClient,
    ConsoleRequestError,
    ConsoleService,
)

SPACE_ID = "11111111-1111-1111-1111-111111111111"
OTHER_SPACE_ID = "22222222-2222-2222-2222-222222222222"
PROJECT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_PROJECT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_environment_connection_is_resolved_without_exposing_secret(tmp_path: Path) -> None:
    resolver = ConsoleContextResolver(
        ServerSettings(config_path=tmp_path / "config.json"),
        environ={
            "ELEMENT_CONSOLE_URL": "https://element.example/console/",
            "ELEMENT_CONSOLE_CLIENT_ID": "client",
            "ELEMENT_CONSOLE_CLIENT_SECRET": "top-secret",
            "ELEMENT_CONSOLE_PROJECT_ID": PROJECT_ID,
        },
    )

    connection = resolver.resolve()

    assert connection.base_url == "https://element.example/console"
    assert connection.client_secret == "top-secret"
    assert connection.public_info() == {
        "base_url": "https://element.example/console",
        "source": "environment",
        "auth_kind": "client_credentials",
        "client_id_present": True,
        "project_id": PROJECT_ID,
        "space_id": None,
        "verify_tls": True,
        "ca_bundle": None,
    }
    assert "top-secret" not in json.dumps(connection.public_info())


def test_element_ide_jsonc_settings_are_supported(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        """
        {
          // Element stores these values through vscode.workspace.getConfiguration().
          "1C.server": "http://127.0.0.1:8080/console/api/v2",
          "1C.clientId": "ide-client",
          "1C.clientSecret": "ide-secret",
          "1C.projectId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }
        """,
        encoding="utf-8",
    )
    resolver = ConsoleContextResolver(
        ServerSettings(ide_settings_path=settings_path),
        environ={},
    )

    connection = resolver.resolve()

    assert connection.base_url == "http://127.0.0.1:8080/console"
    assert connection.source == "ide_settings"
    assert connection.project_id == PROJECT_ID


def test_workspace_provision_context_settings_are_supported(tmp_path: Path) -> None:
    settings_path = tmp_path / "provision.json"
    settings_path.write_text(
        json.dumps(
            {
                "settings": {
                    "paas-url": "https://element.example/console",
                    "client-id": "client",
                    "client-secret": "secret",
                    "paas-project-id": PROJECT_ID,
                    "paas-space-id": SPACE_ID,
                }
            }
        ),
        encoding="utf-8",
    )

    connection = ConsoleContextResolver(
        ServerSettings(ide_settings_path=settings_path),
        environ={},
    ).resolve()

    assert connection.project_id == PROJECT_ID
    assert connection.space_id == SPACE_ID


def test_default_console_config_is_next_to_main_config(tmp_path: Path) -> None:
    main_config = tmp_path / "config" / "config.json"
    console_config = main_config.parent / "console.json"
    console_config.parent.mkdir(parents=True)
    console_config.write_text(
        json.dumps(
            {
                "server": "https://element.example/console",
                "access_token": "short-lived-token",
            }
        ),
        encoding="utf-8",
    )

    connection = ConsoleContextResolver(ServerSettings(config_path=main_config), environ={}).resolve()

    assert connection.source == "console_config"
    assert connection.auth_kind == "access_token"


def test_stdio_discovers_workspace_settings_but_http_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    settings_path = workspace / ".vscode" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "1C.server": "https://element.example/console",
                "1C.clientId": "client",
                "1C.clientSecret": "secret",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    stdio = ConsoleContextResolver(
        ServerSettings(transport="stdio", config_path=tmp_path / "stdio-config.json"),
        environ={},
    ).resolve()

    assert stdio.source == "workspace_settings"
    with pytest.raises(ConsoleConfigurationError):
        ConsoleContextResolver(
            ServerSettings(transport="streamable-http", config_path=tmp_path / "config.json"),
            environ={},
        ).resolve()


def test_incomplete_source_does_not_borrow_secret_from_another_source(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "1C.server": "https://attacker.invalid/console",
                "1C.clientId": "workspace-client",
            }
        ),
        encoding="utf-8",
    )
    resolver = ConsoleContextResolver(
        ServerSettings(ide_settings_path=settings_path),
        environ={"ELEMENT_CONSOLE_CLIENT_SECRET": "environment-secret"},
    )

    with pytest.raises(ConsoleConfigurationError) as error:
        resolver.resolve()

    assert "environment" in str(error.value)
    assert "ide_settings" in str(error.value)
    assert "environment-secret" not in str(error.value)


def test_client_credentials_token_is_cached_and_retried_once_after_401(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    token_number = 0

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        nonlocal token_number
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if url.endswith("/sys/token"):
            token_number += 1
            assert urllib.parse.parse_qs((body or b"").decode()) == {"grant_type": ["client_credentials"]}
            assert headers["Authorization"].startswith("Basic ")
            return {"id_token": f"token-{token_number}"}
        if headers["Authorization"] == "Bearer token-1":
            raise ConsoleRequestError("expired", status_code=401)
        return []

    resolver = ConsoleContextResolver(
        ServerSettings(config_path=tmp_path / "config.json"),
        environ={
            "ELEMENT_CONSOLE_URL": "https://element.example/console",
            "ELEMENT_CONSOLE_CLIENT_ID": "client",
            "ELEMENT_CONSOLE_CLIENT_SECRET": "secret",
        },
    )
    service = ConsoleService(resolver.settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))

    result = service.status()

    assert result["status"] == "ready"
    assert token_number == 2
    assert not any("secret" in json.dumps(call) for call in calls if call["url"].endswith("/api/v2/spaces"))


def test_list_space_projects_derives_space_from_current_ide_project(tmp_path: Path) -> None:
    requested_paths: list[str] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        requested_paths.append(urllib.parse.urlsplit(url).path)
        if url.endswith("/sys/token"):
            return {"id_token": "token"}
        if url.endswith(f"/api/v2/projects/{PROJECT_ID}"):
            return project_response(PROJECT_ID, "Текущий", space_id=SPACE_ID)
        if url.endswith(f"/api/v2/spaces/{SPACE_ID}/projects"):
            return [{"id": PROJECT_ID}, {"id": OTHER_PROJECT_ID}]
        if url.endswith("/api/v2/projects"):
            return [
                project_response(PROJECT_ID, "Текущий", space_id=SPACE_ID),
                project_response(OTHER_PROJECT_ID, "Второй", space_id=SPACE_ID),
            ]
        if url.endswith(f"/api/v2/projects/{OTHER_PROJECT_ID}"):
            return project_response(OTHER_PROJECT_ID, "Второй", space_id=SPACE_ID)
        raise AssertionError(url)

    resolver = ConsoleContextResolver(
        ServerSettings(config_path=tmp_path / "config.json"),
        environ={
            "ELEMENT_CONSOLE_URL": "http://element.local/console",
            "ELEMENT_CONSOLE_CLIENT_ID": "client",
            "ELEMENT_CONSOLE_CLIENT_SECRET": "secret",
            "ELEMENT_CONSOLE_PROJECT_ID": PROJECT_ID,
        },
    )
    service = ConsoleService(resolver.settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))

    result = service.list_space_projects()

    assert result["status"] == "ready"
    assert result["space_id"] == SPACE_ID
    assert result["total"] == 2
    assert [project["name"] for project in result["projects"]] == ["Текущий", "Второй"]
    assert f"/console/api/v2/spaces/{SPACE_ID}/projects" in requested_paths


def test_list_space_projects_requires_selection_for_multiple_spaces(tmp_path: Path) -> None:
    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        if url.endswith("/api/v2/spaces"):
            return [space_response(SPACE_ID, "Основное"), space_response(OTHER_SPACE_ID, "Тест")]
        raise AssertionError(url)

    resolver = ConsoleContextResolver(
        ServerSettings(config_path=tmp_path / "config.json"),
        environ={
            "ELEMENT_CONSOLE_URL": "http://element.local/console",
            "ELEMENT_CONSOLE_ACCESS_TOKEN": "token",
        },
    )
    service = ConsoleService(resolver.settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))

    result = service.list_space_projects()

    assert result["status"] == "selection_required"
    assert [space["name"] for space in result["spaces"]] == ["Основное", "Тест"]


def test_console_status_distinguishes_forbidden_from_empty(tmp_path: Path) -> None:
    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        raise ConsoleRequestError("Нет прав", status_code=403)

    resolver = ConsoleContextResolver(
        ServerSettings(config_path=tmp_path / "config.json"),
        environ={
            "ELEMENT_CONSOLE_URL": "http://element.local/console",
            "ELEMENT_CONSOLE_ACCESS_TOKEN": "token",
        },
    )
    service = ConsoleService(resolver.settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))

    result = service.status()

    assert result == {"status": "forbidden", "http_status": 403, "message": "Нет прав"}


def test_verified_ide_session_is_used_without_persisting_or_exposing_secret(tmp_path: Path) -> None:
    calls: list[str] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        calls.append(url)
        if url.endswith("/sys/token"):
            return {"id_token": "ide-token"}
        if url.endswith("/api/v2/spaces"):
            assert headers["Authorization"] == "Bearer ide-token"
            return [space_response(SPACE_ID, "Основное")]
        raise AssertionError(url)

    config_path = tmp_path / "config.json"
    service = ConsoleService(
        ServerSettings(config_path=config_path),
        client=ConsoleHttpClient(requester=requester),
    )

    handoff = service.configure_ide_session(
        {
            "server": "https://element.example/console",
            "client_id": "ide-client",
            "client_secret": "ide-secret",
            "project_id": PROJECT_ID,
            "ignored": "must-not-be-stored",
        }
    )
    status = service.status()

    assert handoff["status"] == "ready"
    assert handoff["connection"]["source"] == "ide_session"
    assert status["connection"]["source"] == "ide_session"
    assert status["spaces_count"] == 1
    assert "ide-secret" not in json.dumps(handoff)
    assert not config_path.exists()
    assert service.session_store.get() == {
        "server": "https://element.example/console",
        "client_id": "ide-client",
        "client_secret": "ide-secret",
        "project_id": PROJECT_ID,
    }
    assert len([url for url in calls if url.endswith("/sys/token")]) == 1


def test_rejected_ide_session_does_not_replace_existing_connection(tmp_path: Path) -> None:
    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        if "good.example" in url and url.endswith("/sys/token"):
            return {"id_token": "good-token"}
        if "good.example" in url and url.endswith("/api/v2/spaces"):
            return []
        raise ConsoleRequestError("Нет прав", status_code=403)

    service = ConsoleService(
        ServerSettings(config_path=tmp_path / "config.json"),
        client=ConsoleHttpClient(requester=requester),
    )
    service.configure_ide_session({"server": "https://good.example/console", "access_token": "good-token"})

    with pytest.raises(ConsoleRequestError):
        service.configure_ide_session({"server": "https://bad.example/console", "access_token": "bad-token"})

    assert service.status()["connection"]["base_url"] == "https://good.example/console"


def project_response(project_id: str, name: str, *, space_id: str, deleted: bool = False) -> dict[str, Any]:
    return {
        "id": project_id,
        "parent-id": None,
        "group-id": None,
        "name": name,
        "presentation": name,
        "description": f"Описание {name}",
        "deleted": deleted,
        "application-count": 1,
        "code": name.lower(),
        "date-created": "2026-07-20T00:00:00",
        "space-id": space_id,
        "project-kind": "Project",
        "default-image": None,
    }


def space_response(space_id: str, name: str) -> dict[str, Any]:
    return {
        "id": space_id,
        "name": name,
        "owner": "owner",
        "projects-count": 2,
    }
