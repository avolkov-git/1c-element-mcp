from __future__ import annotations

import re
from pathlib import Path

from starlette.testclient import TestClient

from element_mcp.config import ServerSettings
from element_mcp.server import create_server


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
        assert status.json()["server"] == {"state": "running", "version": "0.3.1"}

        assert client.post("/api/updates/check").status_code == 403
        token = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        check = client.post("/api/updates/check", headers={"X-Element-MCP-Token": token})
        assert check.status_code == 200
        assert check.json()["updates"]["state"] == "unavailable"


def test_ui_rejects_unexpected_host_header(tmp_path: Path) -> None:
    server = create_server(ServerSettings(data_path=tmp_path / "data", host="127.0.0.1"))
    with TestClient(server.streamable_http_app(), base_url="http://unexpected.example") as client:
        assert client.get("/").status_code == 403
