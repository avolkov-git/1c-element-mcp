from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from starlette.testclient import TestClient

from element_mcp.config import ServerSettings
from element_mcp.console import ConsoleHttpClient, ConsoleService
from element_mcp.server import create_server


def make_source_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-b", "master"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "pyproject.toml").write_text(
        '[project]\nname = "1c-element-mcp"\nversion = "0.6.1"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(path), "add", "pyproject.toml"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True)


def test_ui_reports_running_server_and_protects_mutations(tmp_path: Path) -> None:
    server = create_server(
        ServerSettings(
            data_path=tmp_path / "data",
            config_path=tmp_path / "config.json",
            transport="streamable-http",
            host="127.0.0.1",
        )
    )
    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1") as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "MCP работает" in page.text
        assert "Каталог нормализованной документации" in page.text
        assert "Диагностика сервера Element" in page.text
        assert "Управляемые действия" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]

        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["server"] == {"state": "running", "version": "0.20.0"}

        assert client.post("/api/updates/check").status_code == 403
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        check = client.post("/api/updates/check", headers={"X-Element-MCP-Token": token})
        assert check.status_code == 200
        assert check.json()["updates"]["state"] == "unavailable"


def test_ui_configures_default_deny_managed_actions(tmp_path: Path) -> None:
    upload_root = tmp_path / "builds"
    upload_root.mkdir()
    actions_config = tmp_path / "config" / "actions.json"
    server = create_server(
        ServerSettings(
            data_path=tmp_path / "data",
            config_path=tmp_path / "config" / "config.json",
            actions_config_path=actions_config,
            transport="streamable-http",
            host="127.0.0.1",
        )
    )
    payload = {
        "enabled": True,
        "allowed_actions": ["upload_project_assembly", "stop_application"],
        "allowed_project_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
        "allowed_application_ids": ["cccccccc-cccc-cccc-cccc-cccccccccccc"],
        "upload_roots": [str(upload_root)],
        "max_upload_bytes": 64 * 1024 * 1024,
        "approval_ttl_seconds": 300,
    }

    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1") as client:
        page = client.get("/")
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        disabled = client.get("/api/actions/configuration")
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"
        assert client.post("/api/actions/configuration", json=payload).status_code == 403

        saved = client.post(
            "/api/actions/configuration",
            json=payload,
            headers={"X-Element-MCP-Token": token},
        )
        assert saved.status_code == 200
        assert saved.json()["status"] == "enabled"
        assert saved.json()["allowed_actions"] == ["stop_application", "upload_project_assembly"]

        invalid = client.post(
            "/api/actions/configuration",
            json={**payload, "allowed_application_ids": ["not-a-uuid"]},
            headers={"X-Element-MCP-Token": token},
        )
        assert invalid.status_code == 400
        assert "UUID" in invalid.json()["message"]

        turned_off = client.post(
            "/api/actions/configuration",
            json={**payload, "enabled": False},
            headers={"X-Element-MCP-Token": token},
        )
        assert turned_off.status_code == 200
        assert turned_off.json()["status"] == "disabled"

    persisted = json.loads(actions_config.read_text(encoding="utf-8"))
    assert persisted["enabled"] is False
    assert persisted["allowed_project_ids"] == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]


def test_ui_can_persist_local_update_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_repository(source)
    config_path = tmp_path / "config.json"
    server = create_server(
        ServerSettings(
            data_path=tmp_path / "data",
            config_path=config_path,
            transport="streamable-http",
            host="127.0.0.1",
        )
    )

    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1") as client:
        page = client.get("/")
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        assert client.post("/api/updates/source", json={"path": str(source)}).status_code == 403

        response = client.post(
            "/api/updates/source",
            json={"path": str(source)},
            headers={"X-Element-MCP-Token": token},
        )

        assert response.status_code == 200
        assert response.json()["updates"]["source"]["kind"] == "local"

        origin = client.post(
            "/api/updates/source",
            json={"path": None},
            headers={"X-Element-MCP-Token": token},
        )
        assert origin.status_code == 200
        assert origin.json()["updates"]["source"]["kind"] == "none"

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["update_source"] == {"kind": "remote"}


def test_ui_can_validate_and_activate_documentation_without_replacing_it_on_error(
    tmp_path: Path,
    corpus_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ELEMENT_DOCS_PATH", raising=False)
    config_path = tmp_path / "config.json"
    server = create_server(
        ServerSettings(
            data_path=tmp_path / "data",
            config_path=config_path,
            transport="streamable-http",
            host="127.0.0.1",
        )
    )

    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1") as client:
        page = client.get("/")
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]

        missing = client.get("/api/documentation")
        assert missing.status_code == 200
        assert missing.json()["status"] == "missing"
        assert missing.json()["path"] is None

        assert client.post("/api/documentation/activate", json={"path": str(corpus_path)}).status_code == 403
        relative = client.post(
            "/api/documentation/activate",
            json={"path": "codex-docs"},
            headers={"X-Element-MCP-Token": token},
        )
        assert relative.status_code == 400
        assert "полный путь" in relative.json()["message"]

        activated = client.post(
            "/api/documentation/activate",
            json={"path": str(corpus_path)},
            headers={"X-Element-MCP-Token": token},
        )
        assert activated.status_code == 200
        assert activated.json()["status"] == "ready"
        assert activated.json()["path"] == str(corpus_path.resolve())
        assert activated.json()["aggregate"] == {"documents": 3, "chunks": 4}

        invalid_path = tmp_path / "not-a-corpus"
        invalid_path.mkdir()
        rejected = client.post(
            "/api/documentation/activate",
            json={"path": str(invalid_path)},
            headers={"X-Element-MCP-Token": token},
        )
        assert rejected.status_code == 400
        assert rejected.json()["status"] == "invalid"

        current = client.get("/api/documentation")
        assert current.json()["status"] == "ready"
        assert current.json()["path"] == str(corpus_path.resolve())

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["active_corpus_path"] == str(corpus_path.resolve())


def test_ui_rejects_unexpected_host_header(tmp_path: Path) -> None:
    server = create_server(ServerSettings(data_path=tmp_path / "data", host="127.0.0.1"))
    with TestClient(server.streamable_http_app(), base_url="http://unexpected.example") as client:
        assert client.get("/").status_code == 403


def test_ui_configures_runtime_without_returning_application_manager_password(tmp_path: Path) -> None:
    instance_root = tmp_path / "instance"
    (instance_root / "config").mkdir(parents=True)
    (instance_root / "logs").mkdir()
    (instance_root / "config" / "server.yml").write_text("server: {}\n", encoding="utf-8")
    (instance_root / "config" / "logging.yml").write_text("logging: {}\n", encoding="utf-8")
    runtime_config = tmp_path / "runtime.json"
    server = create_server(
        ServerSettings(
            data_path=tmp_path / "data",
            config_path=tmp_path / "config.json",
            runtime_config_path=runtime_config,
            transport="streamable-http",
            host="127.0.0.1",
        )
    )

    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1") as client:
        page = client.get("/")
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        missing = client.get("/api/runtime/configuration")
        assert missing.status_code == 200
        assert missing.json()["status"] == "missing"

        payload = {
            "instance_root": str(instance_root),
            "application_manager_enabled": True,
            "server": "https://element.example/manager/api/v2",
            "username": "manager-user",
            "password": "manager-password",
            "api_version": "auto",
            "verify_tls": True,
        }
        assert client.post("/api/runtime/configuration", json=payload).status_code == 403
        saved = client.post(
            "/api/runtime/configuration",
            json=payload,
            headers={"X-Element-MCP-Token": token},
        )

        assert saved.status_code == 200
        assert saved.json()["instance_root"] == str(instance_root.resolve())
        assert saved.json()["application_manager"]["server"] == "https://element.example"
        assert saved.json()["application_manager"]["password_present"] is True
        assert "manager-password" not in saved.text
        assert "manager-password" in runtime_config.read_text(encoding="utf-8") or os.name == "nt"


def test_ui_accepts_verified_ide_handoff_without_returning_secret(
    tmp_path: Path,
    monkeypatch,
    element_project_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def configure(self, values):
        captured.update(values)
        return {
            "status": "ready",
            "connection": {"source": "ide_session", "client_id_present": True},
            "spaces_count": 2,
        }

    monkeypatch.setattr(ConsoleService, "configure_ide_session", configure)
    server = create_server(ServerSettings(data_path=tmp_path / "data", host="127.0.0.1"))
    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1") as client:
        page = client.get("/")
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        payload = {
            "server": "https://element.example/console",
            "client_id": "ide-client",
            "client_secret": "ide-secret",
            "project_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "application_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "workspace_folders": [str(element_project_path)],
            "git_status": {
                "commitId": "abc123",
                "branchName": "main",
                "modified": False,
                "commandStatus": "noConflict",
            },
        }
        assert client.post("/api/integrations/element-console", json=payload).status_code == 403

        response = client.post(
            "/api/integrations/element-console",
            json=payload,
            headers={"X-Element-MCP-Token": token},
        )

        assert response.status_code == 200
        assert response.json()["connection"]["source"] == "ide_session"
        assert response.json()["workspace"]["status"] == "ready"
        assert response.json()["workspace"]["selected_path"] == str(element_project_path.resolve())
        assert response.json()["workspace"]["git"]["source"] == "g5rt.team.status"
        assert "ide-secret" not in response.text
        assert captured["client_secret"] == "ide-secret"
        assert captured["application_id"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"

        unavailable_workspace = client.post(
            "/api/integrations/element-console",
            json={**payload, "workspace_folders": [str(tmp_path / "missing-workspace")]},
            headers={"X-Element-MCP-Token": token},
        )
        assert unavailable_workspace.status_code == 200
        assert unavailable_workspace.json()["status"] == "ready"
        assert unavailable_workspace.json()["workspace"]["status"] == "invalid"


def test_ui_configures_and_disables_remote_element_without_returning_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def get(self, connection, path):
        assert path == "/api/v2/spaces"
        assert connection.client_secret == "remote-secret"
        return [{"id": "11111111-1111-1111-1111-111111111111"}]

    monkeypatch.setattr(ConsoleHttpClient, "get", get)
    config_path = tmp_path / "config" / "config.json"
    server = create_server(
        ServerSettings(
            data_path=tmp_path / "data",
            config_path=config_path,
            transport="streamable-http",
            host="127.0.0.1",
        )
    )
    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1") as client:
        page = client.get("/")
        assert "Удалённый сервер Element" in page.text
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]

        missing = client.get("/api/console/configuration")
        assert missing.json()["status"] == "missing"
        assert client.post("/api/console/configuration", json={"enabled": False}).status_code == 403

        saved = client.post(
            "/api/console/configuration",
            json={
                "enabled": True,
                "server": "https://element.example/console/api/v2",
                "client_id": "remote-client",
                "client_secret": "remote-secret",
            },
            headers={"X-Element-MCP-Token": token},
        )

        assert saved.status_code == 200
        assert saved.json()["status"] == "ready"
        assert saved.json()["server"] == "https://element.example/console"
        assert saved.json()["secret_present"] is True
        assert "remote-secret" not in saved.text

        public = client.get("/api/console/configuration")
        assert public.json()["status"] == "enabled"
        assert "remote-secret" not in public.text

        disabled = client.post(
            "/api/console/configuration",
            json={"enabled": False},
            headers={"X-Element-MCP-Token": token},
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"
        assert disabled.json()["secret_present"] is True
