from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from starlette.testclient import TestClient

from element_mcp.config import ServerSettings
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
        '[project]\nname = "1c-element-mcp"\nversion = "0.4.2"\n',
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
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]

        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["server"] == {"state": "running", "version": "0.4.1"}

        assert client.post("/api/updates/check").status_code == 403
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        check = client.post("/api/updates/check", headers={"X-Element-MCP-Token": token})
        assert check.status_code == 200
        assert check.json()["updates"]["state"] == "unavailable"


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


def test_ui_rejects_unexpected_host_header(tmp_path: Path) -> None:
    server = create_server(ServerSettings(data_path=tmp_path / "data", host="127.0.0.1"))
    with TestClient(server.streamable_http_app(), base_url="http://unexpected.example") as client:
        assert client.get("/").status_code == 403
