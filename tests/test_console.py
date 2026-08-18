from __future__ import annotations

import json
import os
import urllib.parse
from base64 import b64encode
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
APPLICATION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TASK_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


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
        "application_id": None,
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
          "1C.applicationId": "cccccccc-cccc-cccc-cccc-cccccccccccc",
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
    assert connection.application_id == APPLICATION_ID


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

    public = ConsoleService(ServerSettings(config_path=main_config)).persistent_configuration()
    assert public["configured"] is True
    assert public["credential_kind"] == "access_token"
    assert "short-lived-token" not in json.dumps(public)


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
            assert urllib.parse.parse_qs((body or b"").decode()) == {"grant_type": ["CLIENT_CREDENTIALS"]}
            assert headers["Authorization"] == f"Basic {b64encode(b'client:secret').decode('ascii')}"
            return {"id_token": f"token-{token_number}", "access_token": "Not implemented"}
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


def test_console_errors_redact_credentials_and_authorization_header(tmp_path: Path) -> None:
    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        raise ConsoleRequestError(
            f"Rejected secret-value and {headers['Authorization']}",
            status_code=403,
        )

    resolver = ConsoleContextResolver(
        ServerSettings(config_path=tmp_path / "config.json"),
        environ={
            "ELEMENT_CONSOLE_URL": "http://element.local/console",
            "ELEMENT_CONSOLE_CLIENT_ID": "client",
            "ELEMENT_CONSOLE_CLIENT_SECRET": "secret-value",
        },
    )
    service = ConsoleService(resolver.settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))

    result = service.status()

    assert result["status"] == "forbidden"
    assert "secret-value" not in result["message"]
    assert "Basic " not in result["message"]
    assert "[скрыто]" in result["message"]


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
            "application_id": APPLICATION_ID,
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
        "application_id": APPLICATION_ID,
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


def test_current_application_uses_only_exact_ide_session_context(tmp_path: Path) -> None:
    requested_paths: list[str] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        requested_paths.append(urllib.parse.urlsplit(url).path)
        if url.endswith("/api/v2/spaces"):
            return [space_response(SPACE_ID, "Основное")]
        if url.endswith(f"/api/v2/applications/{APPLICATION_ID}"):
            return application_response(APPLICATION_ID, project_id=PROJECT_ID)
        raise AssertionError(url)

    service = ConsoleService(
        ServerSettings(config_path=tmp_path / "config.json"),
        client=ConsoleHttpClient(requester=requester),
    )
    service.configure_ide_session(
        {
            "server": "https://element.example/console",
            "access_token": "ide-token",
            "project_id": PROJECT_ID,
            "application_id": APPLICATION_ID,
        }
    )

    result = service.get_current_application()

    assert result["status"] == "ready"
    assert result["connection"]["source"] == "ide_session"
    assert result["application"]["id"] == APPLICATION_ID
    assert result["application"]["display_name"] == "Тестовое приложение"
    assert result["application"]["status"] == "Running"
    assert result["application"]["uri"] == "https://apps.example/test"
    assert result["application"]["source"]["project_version"] == "1.4.2"
    assert result["application"]["current_task"]["operation_type"] == "Update"
    assert result["matches_ide_project"] is True
    assert requested_paths[-1] == f"/console/api/v2/applications/{APPLICATION_ID}"
    assert "ide-token" not in json.dumps(result)


def test_current_application_is_not_inferred_for_vscode_or_standalone_console(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "1C.server": "https://element.example/console",
                "access_token": "token",
                "1C.projectId": PROJECT_ID,
                "1C.applicationId": APPLICATION_ID,
            }
        ),
        encoding="utf-8",
    )
    resolver = ConsoleContextResolver(
        ServerSettings(config_path=tmp_path / "config.json", ide_settings_path=settings_path),
        environ={},
    )
    service = ConsoleService(resolver.settings, resolver=resolver)

    result = service.get_current_application()

    assert result["status"] == "not_available"
    assert "только" in result["message"]


def test_persistent_connection_is_validated_saved_and_reversibly_disabled(tmp_path: Path) -> None:
    calls: list[str] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        calls.append(url)
        if url.endswith("/sys/token"):
            return {"id_token": "standalone-token"}
        if url.endswith("/api/v2/spaces"):
            assert headers["Authorization"] == "Bearer standalone-token"
            return [space_response(SPACE_ID, "Основное")]
        raise AssertionError(url)

    settings = ServerSettings(config_path=tmp_path / "config" / "config.json")
    resolver = ConsoleContextResolver(settings, environ={})
    service = ConsoleService(settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))

    result = service.configure_persistent_connection(
        server="https://element.example/console/api/v2",
        client_id="standalone-client",
        client_secret="standalone-secret",
    )

    assert result["status"] == "ready"
    assert result["server"] == "https://element.example/console"
    assert result["client_id"] == "standalone-client"
    assert result["secret_present"] is True
    assert result["spaces_count"] == 1
    assert "standalone-secret" not in json.dumps(result)

    config_path = settings.resolved_console_config_path
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["enabled"] is True
    if os.name == "nt":
        assert "client_secret_dpapi" in stored
        assert "standalone-secret" not in config_path.read_text(encoding="utf-8")
    else:
        assert stored["client_secret"] == "standalone-secret"
        assert config_path.stat().st_mode & 0o777 == 0o600
    assert service.status()["status"] == "ready"

    disabled = service.disable_persistent_connection()
    assert disabled["status"] == "disabled"
    assert disabled["secret_present"] is True
    with pytest.raises(ConsoleConfigurationError):
        resolver.resolve()

    enabled_again = service.configure_persistent_connection(
        server="https://element.example/console",
        client_id="standalone-client",
        client_secret=None,
    )
    assert enabled_again["status"] == "ready"
    assert len([url for url in calls if url.endswith("/api/v2/spaces")]) == 3


def test_rejected_persistent_connection_is_not_saved(tmp_path: Path) -> None:
    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        raise ConsoleRequestError("Нет прав", status_code=403)

    settings = ServerSettings(config_path=tmp_path / "config" / "config.json")
    resolver = ConsoleContextResolver(settings, environ={})
    service = ConsoleService(settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))

    with pytest.raises(ConsoleRequestError):
        service.configure_persistent_connection(
            server="https://element.example/console",
            client_id="rejected-client",
            client_secret="rejected-secret",
        )

    assert not settings.resolved_console_config_path.exists()


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


def application_response(application_id: str, *, project_id: str) -> dict[str, Any]:
    return {
        "id": application_id,
        "name": "test-app",
        "display-name": "Тестовое приложение",
        "description": "Опубликованный экземпляр",
        "date-created": "2026-07-21T00:00:00Z",
        "development-mode": True,
        "debugging": True,
        "uri": "https://apps.example/test",
        "space-id": SPACE_ID,
        "technology-version": "9.2.4",
        "dbms-type": "PostgreSQL",
        "project": {"id": project_id},
        "source": {
            "type": "image",
            "project-version-id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "image-id": project_id,
            "project-name": "Текущий",
            "project-version": "1.4.2",
        },
        "current-task": {
            "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "status": "Completed",
            "operation-type": "Update",
            "start-date": "2026-07-21T01:00:00Z",
            "end-date": "2026-07-21T01:01:00Z",
        },
        "status": "Running",
        "error": None,
        "default-user-list": "must-not-be-returned",
        "user-lists": ["must-not-be-returned"],
    }


def test_console_server_info_reports_contract_without_guessing_remote_version(tmp_path: Path) -> None:
    paths: list[str] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        paths.append(urllib.parse.urlsplit(url).path)
        if url.endswith("/api/v1/status/"):
            return None
        if url.endswith("/api/v2/spaces"):
            return [space_response(SPACE_ID, "Основное")]
        raise AssertionError(url)

    service = _console_service(tmp_path, requester)

    result = service.server_info()

    assert result["status"] == "ready"
    assert result["health"] == "ready"
    assert result["api_version"] == "v2"
    assert result["contract_element_version"] == "9.2.4-6"
    assert result["server_product_version"] is None
    assert result["compatibility"] == "api_compatible_product_version_unverified"
    assert result["capabilities"]["read_only"] is True
    assert result["content_trust"] == "external_untrusted"
    assert paths == ["/console/api/v1/status/", "/console/api/v2/spaces"]


def test_console_get_retries_only_transient_safe_requests(tmp_path: Path) -> None:
    calls = 0
    delays: list[float] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConsoleRequestError("Временно недоступно", status_code=503)
        return []

    connection = ConsoleContextResolver(
        ServerSettings(config_path=tmp_path / "config.json"),
        environ={"ELEMENT_CONSOLE_URL": "https://element.example/console", "ELEMENT_CONSOLE_ACCESS_TOKEN": "x"},
    ).resolve()
    client = ConsoleHttpClient(requester=requester, retry_delays=(0.01, 0.02), sleeper=delays.append)

    assert client.get(connection, "/api/v2/spaces") == []
    assert calls == 3
    assert delays == [0.01, 0.02]


def test_list_space_applications_filters_pages_and_sanitizes(tmp_path: Path) -> None:
    first = application_response(APPLICATION_ID, project_id=PROJECT_ID)
    first["description"] = "Bearer very-secret must be hidden"
    first["endpoint"] = {"id": "endpoint-1", "fqdn": "app.example", "certificate": "private-key"}
    second = application_response("dddddddd-dddd-dddd-dddd-dddddddddddd", project_id=OTHER_PROJECT_ID)
    second["display-name"] = "Архив"
    second["status"] = "Stopped"

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        if url.endswith(f"/api/v2/spaces/{SPACE_ID}/applications"):
            return [first, second]
        raise AssertionError(url)

    result = _console_service(tmp_path, requester).list_space_applications(
        space_id=SPACE_ID,
        query="тестовое",
        status="running",
        project_id=PROJECT_ID,
        limit=1,
    )

    assert result["count"] == result["total"] == 1
    application = result["applications"][0]
    assert application["id"] == APPLICATION_ID
    assert application["description"] == "[скрыто] must be hidden"
    assert application["endpoint"]["fqdn"] == "app.example"
    serialized = json.dumps(result)
    assert "very-secret" not in serialized
    assert "private-key" not in serialized
    assert "default-user-list" not in serialized


def test_application_catalog_distinguishes_empty_from_forbidden(tmp_path: Path) -> None:
    def empty_requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        return []

    empty = _console_service(tmp_path, empty_requester).list_space_applications(space_id=SPACE_ID)
    assert empty["status"] == "ready"
    assert empty["total"] == 0
    assert empty["applications"] == []

    def forbidden_requester(
        method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float
    ) -> Any:
        raise ConsoleRequestError("Нет прав", status_code=403)

    forbidden = _console_service(tmp_path, forbidden_requester).list_space_applications(space_id=SPACE_ID)
    assert forbidden["status"] == "forbidden"
    assert forbidden["http_status"] == 403
    assert forbidden["applications"] == []


def test_application_selection_and_subresources_work_in_ide_and_vscode(tmp_path: Path) -> None:
    paths: list[str] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        path = urllib.parse.urlsplit(url).path
        paths.append(path)
        if path.endswith(f"/applications/{APPLICATION_ID}"):
            return application_response(APPLICATION_ID, project_id=PROJECT_ID)
        if path.endswith(f"/applications/{APPLICATION_ID}/status"):
            return {"status": "Running", "current-task": {"id": TASK_ID, "status": "InProgress"}}
        if path.endswith(f"/applications/{APPLICATION_ID}/technology"):
            return {"technology-version": "9.2.4", "date-updated": "2026-08-18T00:00:00Z"}
        if path.endswith(f"/applications/{APPLICATION_ID}/project"):
            return {"id": PROJECT_ID, "developer": "acme", "version": "1.2.3", "title": "Demo"}
        if path.endswith(f"/applications/{APPLICATION_ID}/endpoints"):
            return [
                {
                    "id": "endpoint-1",
                    "fqdn": "demo.example",
                    "context-path": "/demo",
                    "certificate": "must-not-leak",
                    "domain-validation": {"token": "must-not-leak"},
                    "is-active": True,
                    "status": "Ready",
                }
            ]
        if path.endswith("/api/v2/spaces"):
            return []
        raise AssertionError(url)

    standalone = _console_service(tmp_path, requester)
    assert standalone.get_application()["status"] == "selection_required"
    assert standalone.get_application(APPLICATION_ID)["selection_source"] == "explicit"
    status = standalone.get_application_status(APPLICATION_ID)
    assert status["status"] == "ready"
    assert status["application_status"]["status"] == "Running"
    assert standalone.get_application_technology(APPLICATION_ID)["technology"]["technology_version"] == "9.2.4"
    assert standalone.get_application_project(APPLICATION_ID)["project"]["id"] == PROJECT_ID
    endpoint_result = standalone.list_application_endpoints(APPLICATION_ID)
    assert endpoint_result["endpoints"][0]["context_path"] == "/demo"
    assert "must-not-leak" not in json.dumps(endpoint_result)

    ide = ConsoleService(
        ServerSettings(config_path=tmp_path / "ide-config.json"),
        client=ConsoleHttpClient(requester=requester),
    )
    ide.configure_ide_session(
        {
            "server": "https://element.example/console",
            "access_token": "ide-token",
            "application_id": APPLICATION_ID,
        }
    )
    assert ide.get_application()["selection_source"] == "ide_session"
    assert paths.count(f"/console/api/v2/applications/{APPLICATION_ID}") == 2


def test_project_assemblies_are_bounded_and_exact_version_is_encoded(tmp_path: Path) -> None:
    urls: list[str] = []
    assembly = {
        "id": "assembly-1",
        "assembly-version": "1.2 beta",
        "project-id": PROJECT_ID,
        "project-name": "Demo",
        "branch-name": "main",
        "commit-id": "abc123",
        "comment": "client_secret=do-not-leak release",
        "modified": False,
    }

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        urls.append(url)
        if url.endswith(f"/projects/{PROJECT_ID}/assemblies"):
            return [assembly]
        if url.endswith(f"/projects/{PROJECT_ID}/assemblies/1.2%20beta"):
            return assembly
        raise AssertionError(url)

    service = _console_service(tmp_path, requester)
    listed = service.list_project_assemblies(project_id=PROJECT_ID, query="beta")
    exact = service.get_project_assembly("1.2 beta", PROJECT_ID)

    assert listed["count"] == 1
    assert listed["assemblies"][0]["comment"] == "[скрыто] release"
    assert exact["assembly"]["assembly_version"] == "1.2 beta"
    assert urls[-1].endswith("/assemblies/1.2%20beta")
    assert "do-not-leak" not in json.dumps([listed, exact])


@pytest.mark.parametrize(
    ("task_type", "collection"),
    [
        ("application", "application-tasks"),
        ("deployment_instance", "deployment-instance-tasks"),
        ("group", "group-tasks"),
    ],
)
def test_console_task_types_use_exact_routes_and_safe_dtos(
    tmp_path: Path,
    task_type: str,
    collection: str,
) -> None:
    paths: list[str] = []
    task = {
        "id": TASK_ID,
        "status": "Completed",
        "operation-type": "Update",
        "application-id": APPLICATION_ID,
        "error-message": "access_token=do-not-leak failure",
        "tasks": [{"id": TASK_ID, "status": "Completed", "application-id": APPLICATION_ID}],
    }

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        path = urllib.parse.urlsplit(url).path
        paths.append(path)
        if path.endswith(f"/{collection}"):
            return [task]
        if path.endswith(f"/{collection}/{TASK_ID}"):
            return task
        raise AssertionError(url)

    service = _console_service(tmp_path, requester)
    listed = service.list_tasks(task_type, status="completed", limit=10)  # type: ignore[arg-type]
    exact = service.get_task(task_type, TASK_ID)  # type: ignore[arg-type]

    assert listed["count"] == 1
    assert exact["task"]["id"] == TASK_ID
    assert paths == [f"/console/api/v2/tasks/{collection}", f"/console/api/v2/tasks/{collection}/{TASK_ID}"]
    assert "do-not-leak" not in json.dumps([listed, exact])


def _console_service(tmp_path: Path, requester: Any) -> ConsoleService:
    settings = ServerSettings(config_path=tmp_path / "config.json")
    resolver = ConsoleContextResolver(
        settings,
        environ={
            "ELEMENT_CONSOLE_URL": "https://element.example/console",
            "ELEMENT_CONSOLE_ACCESS_TOKEN": "test-token",
        },
    )
    return ConsoleService(settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))
