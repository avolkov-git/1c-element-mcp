from __future__ import annotations

import json
import re
import shutil
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from element_mcp.actions import ActionsConfiguration, ManagedActionError
from element_mcp.config import ServerSettings
from element_mcp.console import ConsoleHttpClient, ConsoleRequestError
from element_mcp.ide_sessions import (
    CONTROL_HEADER,
    SESSION_HEADER,
    IdeSessionError,
    IdeSessionRegistry,
    RoutedService,
    current_ide_session,
)
from element_mcp.server import create_server

PROJECT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROJECT_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
APP_A = "cccccccc-cccc-cccc-cccc-cccccccccccc"
APP_B = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def payload(project, application, root):
    return {
        "protocol_version": 1,
        "server": "https://element.example/console",
        "access_token": "synthetic-console-secret",
        "project_id": project,
        "application_id": application,
        "workspace_folders": [str(root)],
    }


@pytest.fixture
def running(tmp_path, element_project_path, monkeypatch):
    settings = ServerSettings(
        config_path=tmp_path / "config.json",
        console_config_path=tmp_path / "console.json",
        actions_config_path=tmp_path / "actions.json",
        data_path=tmp_path / "data",
        transport="streamable-http",
        host="127.0.0.1",
        # A global startup override must not replace either session's workspace.
        project_path=tmp_path / "unrelated-global",
    )
    ActionsConfiguration(settings).configure(
        enabled=True,
        allowed_actions=["start_application", "stop_application"],
        upload_roots=[],
    )
    projects = {APP_A: PROJECT_A, APP_B: PROJECT_B}
    writes = []

    def get(self, connection, path):
        if path == "/api/v2/spaces":
            return []
        if path.endswith("/status"):
            return {"status": "Stopped", "current-task": None}
        if path.startswith("/api/v2/projects/"):
            return {"id": path.rsplit("/", 1)[-1], "group-id": None}
        app_id = path.rsplit("/", 1)[-1]
        assert app_id in projects, path
        return {
            "id": app_id,
            "name": app_id,
            "project": {"id": projects[app_id]},
            "status": "Stopped",
            "current-task": None,
        }

    def mutate(self, connection, method, path, **kwargs):
        writes.append((connection.project_id, connection.application_id, method, path))
        return {"status": "Starting"}

    monkeypatch.setattr(ConsoleHttpClient, "get", get)
    monkeypatch.setattr(ConsoleHttpClient, "mutate", mutate)
    root_b = tmp_path / "workspace-b"
    shutil.copytree(element_project_path, root_b)
    server = create_server(settings)
    clock = [100.0]
    server.ide_sessions.clock = lambda: clock[0]
    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1:9900") as client:
        page = client.get("/")
        csrf = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)
        headers = {"X-Element-MCP-Token": csrf}

        def create(project, application, root):
            result = client.post(
                "/api/integrations/ide-sessions", headers=headers, json=payload(project, application, root)
            )
            assert result.status_code == 201, result.text
            assert result.headers["cache-control"] == "no-store"
            return result.json()

        a = create(PROJECT_A, APP_A, element_project_path)
        b = create(PROJECT_B, APP_B, root_b)
        yield SimpleNamespace(
            settings=settings,
            client=client,
            server=server,
            headers=headers,
            a=a,
            b=b,
            writes=writes,
            projects=projects,
            clock=clock,
            root_a=element_project_path,
            root_b=root_b,
        )


def rpc(client, token, name, arguments=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers[SESSION_HEADER] = token
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        data = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        body = json.loads(data)
    else:
        body = response.json()
    assert "error" not in body, body
    return body["result"]


def call(client, token, name, arguments=None):
    result = rpc(client, token, name, arguments)
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_actual_http_routes_two_workspaces_and_confirmations(running):
    r = running
    a, b = r.a["mcp_token"], r.b["mcp_token"]
    for token, app_id, root in [(a, APP_A, r.root_a), (b, APP_B, r.root_b)]:
        current = call(r.client, token, "get_current_application")
        assert current["application"]["id"] == app_id
        status = call(r.client, token, "get_project_status")
        assert status["path"] == str(root)
        actions = call(r.client, token, "get_managed_actions_status")
        assert actions["can_prepare"]
        assert actions["workspace_scope"]["application_id"] == app_id

    prepared = call(r.client, a, "prepare_application_state_change", {"desired_state": "start"})
    approval = prepared["approval_token"]
    assert prepared["workspace_scope"]["session_id"] == r.a["session_id"]
    denied = rpc(r.client, b, "start_application", {"approval_token": approval})
    assert denied["isError"]
    assert r.writes == []
    assert call(r.client, a, "start_application", {"approval_token": approval})["status"] == "accepted"
    assert r.writes == [(PROJECT_A, APP_A, "PUT", f"/api/v2/applications/{APP_A}/status/start")]
    assert rpc(r.client, a, "start_application", {"approval_token": approval})["isError"]
    assert rpc(r.client, a, "prepare_application_state_change", {"desired_state": "start", "application_id": APP_B})[
        "isError"
    ]
    unbound = call(r.client, None, "get_managed_actions_status")
    assert unbound["workspace_scope"]["status"] == "missing"
    assert not unbound["can_prepare"]
    assert rpc(
        r.client,
        None,
        "prepare_application_state_change",
        {
            "desired_state": "start",
            "application_id": APP_A,
        },
    )["isError"]


def test_group_ide_keeps_application_scope_and_rechecks_membership(running, monkeypatch):
    r = running
    original = ConsoleHttpClient.get
    group_id = "99999999-9999-4999-8999-999999999999"
    space_id = "88888888-8888-4888-8888-888888888888"
    member = [True]

    def get(self, connection, path):
        if path == f"/api/v2/projects/{PROJECT_A}":
            return {"id": PROJECT_A, "group-id": group_id if member[0] else None, "space-id": space_id}
        if path == f"/api/v2/projects/{group_id}":
            return {"id": group_id, "project-kind": "Group", "space-id": space_id}
        return original(self, connection, path)

    monkeypatch.setattr(ConsoleHttpClient, "get", get)
    response = r.client.post(
        "/api/integrations/ide-sessions", headers=r.headers, json=payload(group_id, APP_A, r.root_a)
    )
    assert response.status_code == 201, response.text
    session = response.json()
    token = session["mcp_token"]
    current = call(r.client, token, "get_current_application")
    assert current["project_match_kind"] == "group"
    assert current["application_project_id"] == PROJECT_A
    assert current["application_project_group_id"] == group_id
    assert call(r.client, token, "get_project_status")["path"] == str(r.root_a)
    actions = r.server.ide_sessions.for_mcp(token).bundle.actions
    assert actions._selected_target(None, "project_id") == PROJECT_A
    ActionsConfiguration(r.settings).configure(
        enabled=True, allowed_actions=["start_application", "upload_project_assembly"], upload_roots=[str(r.root_a)]
    )
    actions._authorize("upload_project_assembly", project_id=PROJECT_A)
    for wrong_project in (group_id, PROJECT_B):
        with pytest.raises(ManagedActionError, match="не принадлежит приложению"):
            actions._authorize("upload_project_assembly", project_id=wrong_project)
    prepared = call(r.client, token, "prepare_application_state_change", {"desired_state": "start"})
    assert rpc(
        r.client,
        token,
        "prepare_application_state_change",
        {
            "desired_state": "start",
            "application_id": APP_B,
        },
    )["isError"]
    member[0] = False
    assert not call(r.client, token, "get_current_application")["matches_ide_project"]
    assert rpc(r.client, token, "start_application", {"approval_token": prepared["approval_token"]})["isError"]
    assert not r.writes


def test_concurrent_http_clients_keep_context(running):
    r = running

    def read(index):
        selected = r.a if index % 2 == 0 else r.b
        result = call(r.client, selected["mcp_token"], "get_current_application")
        assert result["application"]["id"] == selected["application_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(read, range(20)))


def test_revocation_expiry_renewal_and_transport_tokens(running):
    r = running
    path = f"/api/integrations/ide-sessions/{r.a['session_id']}"
    control = {**r.headers, CONTROL_HEADER: r.a["control_token"]}
    assert r.client.post(path, headers=r.headers, json={}).status_code == 401
    assert r.client.post(path, headers={**r.headers, CONTROL_HEADER: r.a["mcp_token"]}, json={}).status_code == 401
    for invalid in ["invalid", r.a["control_token"]]:
        assert r.client.post("/mcp", headers={SESSION_HEADER: invalid}, json={}).status_code == 401
    assert (
        r.client.post(
            "/mcp",
            headers=[
                (SESSION_HEADER, r.a["mcp_token"]),
                (SESSION_HEADER, r.b["mcp_token"]),
            ],
            json={},
        ).status_code
        == 401
    )
    assert r.client.post(path, headers=control, json={"application_id": APP_B}).status_code == 400
    r.clock[0] += 120
    renewed = r.client.post(path, headers=control, json={"git_status": {"modified": True}})
    assert renewed.status_code == 200
    assert "mcp_token" not in renewed.json()
    r.clock[0] += 120
    assert call(r.client, r.a["mcp_token"], "get_current_application")["application"]["id"] == APP_A
    assert r.client.post("/mcp", headers={SESSION_HEADER: r.b["mcp_token"]}, json={}).status_code == 401
    assert r.client.delete(path, headers=control).status_code == 200
    assert r.client.post("/mcp", headers={SESSION_HEADER: r.a["mcp_token"]}, json={}).status_code == 401
    assert r.client.post(path, headers=control, json={}).status_code == 401


def test_bad_registration_never_replaces_other_session(running):
    r = running
    for changes in [
        {"project_id": PROJECT_B},
        {"workspace_folders": []},
        {"workspace_folders": [str(r.root_a / "missing")]},
        {"protocol_version": True},
        {"protocol_version": 2},
        {"unexpected": "field"},
        {"application_id": ""},
        {"project_id": None},
    ]:
        response = r.client.post(
            "/api/integrations/ide-sessions",
            headers=r.headers,
            json={
                **payload(PROJECT_A, APP_A, r.root_a),
                **changes,
            },
        )
        assert response.status_code == 400, response.text
    assert r.client.post("/api/integrations/ide-sessions", json=payload(PROJECT_A, APP_A, r.root_a)).status_code == 403
    assert call(r.client, r.a["mcp_token"], "get_current_application")["application"]["id"] == APP_A
    assert len(r.server.ide_sessions._sessions) == 2


def test_registration_and_bound_session_ignore_broken_shared_configuration(running, monkeypatch):
    r = running
    r.settings.resolved_console_config_path.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setenv("ELEMENT_CONSOLE_URL", "https://unrelated.example/console")
    monkeypatch.setenv("ELEMENT_CONSOLE_ACCESS_TOKEN", "unrelated-secret")
    calls = []

    def get(self, connection, path):
        calls.append(path)
        assert connection.base_url == "https://element.example/console"
        assert connection.access_token == "synthetic-console-secret"
        if path == "/api/v2/spaces":
            return []
        assert path == f"/api/v2/applications/{APP_A}"
        return {"id": APP_A, "project": {"id": PROJECT_A}}

    monkeypatch.setattr(ConsoleHttpClient, "get", get)
    response = r.client.post(
        "/api/integrations/ide-sessions", headers=r.headers, json=payload(PROJECT_A, APP_A, r.root_a)
    )
    assert response.status_code == 201, response.text
    assert calls == ["/api/v2/spaces", f"/api/v2/applications/{APP_A}"]
    current = call(r.client, response.json()["mcp_token"], "get_current_application")
    assert current["matches_ide_project"] is True
    assert len(r.server.ide_sessions._sessions) == 3
    assert r.settings.resolved_console_config_path.read_text() == "{invalid json"


def test_registration_uses_ide_credentials_through_real_console_client(tmp_path, element_project_path, monkeypatch):
    settings = ServerSettings(
        config_path=tmp_path / "config.json",
        console_config_path=tmp_path / "console.json",
        ide_settings_path=tmp_path / "missing-ide.json",
        actions_config_path=tmp_path / "actions.json",
        data_path=tmp_path / "data",
        transport="streamable-http",
        host="127.0.0.1",
    )
    settings.resolved_console_config_path.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setenv("ELEMENT_CONSOLE_URL", "https://unrelated.example/console")
    monkeypatch.setenv("ELEMENT_CONSOLE_ACCESS_TOKEN", "unrelated-secret")
    calls = []

    def requester(method, url, headers, body, context, timeout):
        calls.append(url)
        assert method == ("POST" if url.endswith("/sys/token") else "GET")
        assert url.startswith("https://element.example/console/")
        if url.endswith("/sys/token"):
            expected = b64encode(b"ide-client:ide-secret").decode("ascii")
            assert headers["Authorization"] == f"Basic {expected}"
            assert body == b"grant_type=CLIENT_CREDENTIALS"
            return {"id_token": "session-console-token"}
        assert headers["Authorization"] == "Bearer session-console-token"
        if url.endswith("/api/v2/spaces"):
            return []
        assert url.endswith(f"/api/v2/applications/{APP_A}")
        return {"id": APP_A, "project": {"id": PROJECT_A}}

    monkeypatch.setattr("element_mcp.console._urlopen_json", requester)
    server = create_server(settings)
    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1:9900") as client:
        page = client.get("/")
        csrf = re.search(r'name="element-mcp-token" content="([^"]+)"', page.text).group(1)
        values = payload(PROJECT_A, APP_A, element_project_path)
        del values["access_token"]
        values.update(client_id="ide-client", client_secret="ide-secret")
        response = client.post("/api/integrations/ide-sessions", headers={"X-Element-MCP-Token": csrf}, json=values)
        assert response.status_code == 201, response.text
        current = call(client, response.json()["mcp_token"], "get_current_application")
        assert current["matches_ide_project"] is True
        assert len(calls) == 4
        assert sum(url.endswith("/sys/token") for url in calls) == 1
        for secret in ("ide-secret", "session-console-token", "unrelated-secret"):
            assert secret not in response.text
            assert secret not in json.dumps(current)
    assert settings.resolved_console_config_path.read_text() == "{invalid json"


@pytest.mark.parametrize("stage", ["authentication", "application"])
@pytest.mark.parametrize("http_status", [401, 403, 404, 429, 500, 503, None])
def test_registration_preserves_console_failure_stage_and_status(running, monkeypatch, caplog, stage, http_status):
    r = running
    original = ConsoleHttpClient.get

    def get(self, connection, path):
        if stage == "authentication" or path == f"/api/v2/applications/{APP_A}":
            raise ConsoleRequestError(
                "upstream-body synthetic-console-secret client-secret /private/path",
                status_code=http_status,
            )
        return original(self, connection, path)

    with monkeypatch.context() as patch:
        patch.setattr(ConsoleHttpClient, "get", get)
        response = r.client.post(
            "/api/integrations/ide-sessions", headers=r.headers, json=payload(PROJECT_A, APP_A, r.root_a)
        )
    assert response.status_code == (http_status if http_status in {401, 403} else 502)
    body = response.json()
    assert body["status"] == "rejected"
    assert body["code"] == f"console_{stage}_failed"
    assert body["http_status"] == http_status
    assert ("авторизацию" if stage == "authentication" else "приложение") in body["message"]
    assert response.headers["cache-control"] == "no-store"
    assert f"code={body['code']}" in caplog.text
    for secret in ("synthetic-console-secret", "client-secret", "upstream-body", "/private/path"):
        assert secret not in response.text
        assert secret not in caplog.text
    assert len(r.server.ide_sessions._sessions) == 2
    assert call(r.client, r.a["mcp_token"], "get_current_application")["application"]["id"] == APP_A


@pytest.mark.parametrize(
    ("application", "code", "status"),
    [
        ({"id": APP_A}, "application_project_missing", 400),
        ({"id": APP_A, "project": {"id": "not-a-uuid"}}, "application_project_invalid", 400),
        ({"id": APP_A, "project": {"id": PROJECT_B}}, "application_project_mismatch", 400),
        ({"id": APP_A, "source": {"image-id": PROJECT_B}}, "application_project_mismatch", 400),
        ({"id": APP_B, "project": {"id": PROJECT_A}}, "console_application_failed", 502),
        (["unexpected-array"], "console_application_failed", 502),
    ],
)
def test_registration_distinguishes_invalid_application_and_project(
    running, monkeypatch, caplog, application, code, status
):
    r = running
    original = ConsoleHttpClient.get

    def get(self, connection, path):
        if path == f"/api/v2/applications/{APP_A}":
            return application
        return original(self, connection, path)

    with monkeypatch.context() as patch:
        patch.setattr(ConsoleHttpClient, "get", get)
        response = r.client.post(
            "/api/integrations/ide-sessions", headers=r.headers, json=payload(PROJECT_A, APP_A, r.root_a)
        )
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["http_status"] is None
    assert response.headers["cache-control"] == "no-store"
    assert f"code={code}" in caplog.text
    assert len(r.server.ide_sessions._sessions) == 2
    assert call(r.client, r.a["mcp_token"], "get_current_application")["matches_ide_project"] is True


@pytest.mark.parametrize(
    "project", [{"project": {"id": f" {{{PROJECT_A.upper()}}} "}}, {"source": {"image-id": PROJECT_A}}]
)
def test_registration_accepts_official_project_fields_and_uuid_format(running, monkeypatch, project):
    r = running

    def get(self, connection, path):
        if path == "/api/v2/spaces":
            return []
        assert path == f"/api/v2/applications/{APP_A}"
        return {"id": APP_A, **project}

    monkeypatch.setattr(ConsoleHttpClient, "get", get)
    response = r.client.post(
        "/api/integrations/ide-sessions", headers=r.headers, json=payload(PROJECT_A, APP_A, r.root_a)
    )
    assert response.status_code == 201, response.text
    assert response.json()["project_id"] == PROJECT_A


def test_changed_console_association_and_revoked_approval_never_write(running):
    r = running
    prepared = call(r.client, r.a["mcp_token"], "prepare_application_state_change", {"desired_state": "start"})
    r.projects[APP_A] = PROJECT_B
    result = rpc(r.client, r.a["mcp_token"], "start_application", {"approval_token": prepared["approval_token"]})
    assert result["isError"]
    r.projects[APP_A] = PROJECT_A
    prepared_b = call(r.client, r.b["mcp_token"], "prepare_application_state_change", {"desired_state": "start"})
    r.server.ide_sessions.revoke(r.b["session_id"], r.b["control_token"])
    assert rpc(
        r.client,
        r.a["mcp_token"],
        "start_application",
        {
            "approval_token": prepared_b["approval_token"],
        },
    )["isError"]
    assert not r.writes


def test_registry_cleanup_limit_and_restart(monkeypatch):
    now = [0.0]
    bundles = []

    def factory(session, value):
        bundle = SimpleNamespace(language_server=Mock(), project=Mock(), actions=Mock())
        bundles.append(bundle)
        return bundle

    registry = IdeSessionRegistry(factory, clock=lambda: now[0])
    monkeypatch.setattr("element_mcp.ide_sessions.MAX_SESSIONS", 1)
    created = registry.create(payload(PROJECT_A, APP_A, "/workspace"))
    assert created["mcp_token"] != created["control_token"]
    assert created["mcp_token"] not in repr(registry._sessions)
    with pytest.raises(IdeSessionError, match="лимит"):
        registry.create(payload(PROJECT_B, APP_B, "/other"))
    bundles[-1].language_server.close.assert_called_once()
    now[0] = 181
    with pytest.raises(IdeSessionError):
        registry.renew(created["session_id"], created["control_token"], {})
    registry.reap()
    assert registry._sessions == {}
    bundles[0].language_server.close.assert_called_once()
    bundles[0].actions.invalidate_approvals.assert_called_once()
    replacement = registry.create(payload(PROJECT_A, APP_A, "/workspace"))
    assert replacement["mcp_token"] != created["mcp_token"]
    registry.close()
    with pytest.raises(IdeSessionError):
        registry.for_mcp(replacement["mcp_token"])


def test_initialization_service_isolation_and_workspace_boundary(running):
    r = running
    response = r.client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            SESSION_HEADER: r.a["mcp_token"],
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    assert "synthetic-console-secret" not in response.text
    a, b = (r.server.ide_sessions.for_mcp(item["mcp_token"]) for item in (r.a, r.b))
    for name in ("project", "graph", "semantic", "language_server", "console", "runtime", "actions"):
        assert getattr(a.bundle, name) is not getattr(b.bundle, name)
    assert a.bundle.language_server.settings.resolved_data_path != b.bundle.language_server.settings.resolved_data_path
    assert rpc(r.client, r.a["mcp_token"], "connect_project", {"path": str(r.root_b)})["isError"]
    assert call(r.client, r.a["mcp_token"], "get_project_status")["path"] == str(r.root_a)


def test_previously_resolved_method_cannot_restart_service_after_revoke(running):
    r = running
    session = r.server.ide_sessions.for_mcp(r.a["mcp_token"])
    marker = current_ide_session.set(session)
    try:
        method = RoutedService("project", None).project_status
    finally:
        current_ide_session.reset(marker)
    r.server.ide_sessions.revoke(r.a["session_id"], r.a["control_token"])
    with pytest.raises(IdeSessionError):
        method()


def test_legacy_policy_requires_explicit_migration(tmp_path):
    path = tmp_path / "actions.json"
    settings = ServerSettings(actions_config_path=path)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "allowed_actions": ["start_application"],
                "allowed_project_ids": [PROJECT_A],
                "allowed_application_ids": [APP_A],
            }
        )
    )
    configuration = ActionsConfiguration(settings)
    legacy = configuration.public_configuration()
    assert legacy["migration_required"]
    assert not legacy["enabled"]
    migrated = configuration.configure(enabled=True, allowed_actions=["start_application"], upload_roots=[])
    assert migrated["enabled"] and not migrated["migration_required"]
    assert "allowed_application_ids" not in path.read_text()
    for version in [[], True, 3]:
        path.write_text(json.dumps({"schema_version": version}))
        assert configuration.public_configuration()["status"] == "invalid"
