from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from element_mcp.config import ServerSettings
from element_mcp.runtime import (
    ApplicationManagerClient,
    RuntimeConfiguration,
    RuntimeConfigurationError,
    RuntimeDiagnosticsService,
    RuntimeRequestError,
)

APPLICATION_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
EVENT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TASK_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class StubConsole:
    def get_task(self, task_type: str, task_id: str) -> dict[str, Any]:
        assert task_type == "application"
        assert task_id == TASK_ID
        return {
            "status": "ready",
            "task": {
                "id": task_id,
                "application_id": APPLICATION_ID,
                "start_date": "2026-08-18T10:00:00Z",
                "end_date": "2026-08-18T10:30:00Z",
            },
        }


@pytest.fixture
def instance_root(tmp_path: Path) -> Path:
    root = tmp_path / "instance"
    (root / "config").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "dumps").mkdir()
    (root / "work").mkdir()
    (root / "config" / "server.yml").write_text("server: {}\n", encoding="utf-8")
    (root / "config" / "logging.yml").write_text("logging: {}\n", encoding="utf-8")
    return root


def configured_service(
    tmp_path: Path,
    instance_root: Path,
    requester,
) -> tuple[RuntimeDiagnosticsService, RuntimeConfiguration]:
    settings = ServerSettings(runtime_config_path=tmp_path / "runtime.json")
    configuration = RuntimeConfiguration(settings, environ={})
    configuration.configure(
        instance_root=instance_root,
        application_manager_enabled=True,
        server="https://element.example/manager/api/v2",
        username="manager-user",
        password="manager-password",
    )
    service = RuntimeDiagnosticsService(
        settings,
        StubConsole(),  # type: ignore[arg-type]
        configuration=configuration,
        event_client=ApplicationManagerClient(requester=requester),
    )
    return service, configuration


def event_record() -> dict[str, Any]:
    return {
        "id": EVENT_ID,
        "name": "OrderChanged",
        "description": "Authorization: Bearer top-secret",
        "presentation": "contact admin@example.test",
        "importance": "MAJOR",
        "date": "2026-08-18T10:15:00Z",
        "type": "ERROR",
        "properties": {
            "traceId": "trace-42",
            "userId": "user@example.test",
            "password": "plain-secret",
            "message": "email owner@example.test",
        },
        "propertiesDescriptions": {"traceId": "Trace identifier"},
    }


def test_runtime_configuration_validates_instance_and_never_returns_password(
    tmp_path: Path,
    instance_root: Path,
) -> None:
    settings = ServerSettings(runtime_config_path=tmp_path / "runtime.json")
    configuration = RuntimeConfiguration(settings, environ={})

    result = configuration.configure(
        instance_root=instance_root,
        application_manager_enabled=True,
        server="https://element.example/manager/api/v1",
        username="manager-user",
        password="manager-password",
    )

    assert result["instance_root"] == str(instance_root.resolve())
    assert result["application_manager"]["server"] == "https://element.example"
    assert result["application_manager"]["password_present"] is True
    assert "manager-password" not in str(result)
    assert configuration.application_manager().password == "manager-password"  # type: ignore[union-attr]
    if os.name != "nt":
        assert (tmp_path / "runtime.json").stat().st_mode & 0o777 == 0o600

    missing = tmp_path / "not-instance"
    missing.mkdir()
    with pytest.raises(RuntimeConfigurationError, match="config/server.yml"):
        configuration.configure(instance_root=missing)


def test_runtime_logs_are_allowlisted_bounded_and_redacted(tmp_path: Path, instance_root: Path) -> None:
    log_root = instance_root / "logs"
    (log_root / "server.log").write_text(
        "2026-08-18 INFO traceId=trace-42 userId=alice@example.test password=hunter2\n"
        "2026-08-18 ERROR Authorization: Bearer abc.def contact=bob@example.test\n",
        encoding="utf-8",
    )
    (log_root / "server.log.1").write_text("rotated trace-42\n", encoding="utf-8")
    (log_root / "access.log").write_bytes(b"invalid=\xff trace-42\n")
    (log_root / "random.txt").write_text("not allowed\n", encoding="utf-8")
    (log_root / "clients.log").write_bytes(b"binary\x00value")
    outside = tmp_path / "outside.log"
    outside.write_text("outside", encoding="utf-8")
    symlink_created = False
    try:
        (log_root / "debugger.log").symlink_to(outside)
        symlink_created = True
    except OSError:
        pass
    (instance_root / "daemon.pid").write_text(str(os.getpid()), encoding="ascii")

    settings = ServerSettings(runtime_config_path=tmp_path / "runtime.json")
    configuration = RuntimeConfiguration(settings, environ={"ELEMENT_INSTANCE_ROOT": str(instance_root)})
    service = RuntimeDiagnosticsService(settings, StubConsole(), configuration=configuration)  # type: ignore[arg-type]

    listed = service.list_logs()
    assert {item["log_id"] for item in listed["logs"]} == {
        "access.log",
        "clients.log",
        "server.log",
        "server.log.1",
    }
    read = service.read_log("server.log", tail_lines=20)
    text = "\n".join(read["lines"])
    assert "hunter2" not in text
    assert "abc.def" not in text
    assert "alice@example.test" not in text
    assert "bob@example.test" not in text
    assert "[USER:" in text
    assert service.read_log("clients.log")["status"] == "binary"

    searched = service.search_logs("trace-42", max_matches=10)
    assert searched["count"] == 3
    assert all(item["matched_terms"] == 1 for item in searched["matches"])
    assert any(item.get("replacement_characters") == 1 for item in searched["searched_logs"])
    with pytest.raises(RuntimeConfigurationError):
        service.read_log("../outside.log")
    if symlink_created:
        with pytest.raises(RuntimeConfigurationError):
            service.read_log("debugger.log")

    health = service.health()
    assert health["status"] == "ready"
    assert health["process"]["pid"] == os.getpid()
    assert health["disk"]["filesystem"]["total_bytes"] > 0


def test_application_events_use_v2_bounds_anchor_and_redaction(tmp_path: Path, instance_root: Path) -> None:
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def requester(method, url, headers, body, context, timeout):
        calls.append((method, url, dict(headers), body))
        return 200, [event_record()]

    service, _ = configured_service(tmp_path, instance_root, requester)
    result = service.search_application_events(
        application_id=APPLICATION_ID,
        start_instant="2026-08-18T10:00:00+03:00",
        final_instant="2026-08-18T11:00:00+03:00",
        size=1,
        anchor_event_id=EVENT_ID,
        operation_id="operation-42",
        importance=["major"],
        kind=["error"],
        names=["OrderChanged"],
    )

    assert result["status"] == "ready"
    assert result["api_version"] == "v2"
    assert result["time_range"] == {"start": "2026-08-18T07:00:00Z", "final": "2026-08-18T08:00:00Z"}
    assert result["next_anchor_event_id"] == EVENT_ID
    event = result["events"][0]
    assert event["properties"]["traceId"] == "trace-42"
    assert event["properties"]["password"] == "[СКРЫТО]"
    assert event["properties"]["userId"].startswith("[USER:")
    assert "top-secret" not in str(result)
    assert "example.test" not in str(result)
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith(f"/manager/api/v2/applications/{APPLICATION_ID}/eventlog")
    assert calls[0][3] is not None and b'"operationId": "operation-42"' in calls[0][3]
    assert "manager-password" not in str(result)

    with pytest.raises(RuntimeConfigurationError, match="часовой пояс"):
        service.search_application_events(
            application_id=APPLICATION_ID,
            start_instant="2026-08-18T10:00:00",
            final_instant="2026-08-18T11:00:00Z",
        )
    with pytest.raises(RuntimeConfigurationError, match="31 день"):
        service.search_application_events(
            application_id=APPLICATION_ID,
            start_instant="2026-01-01T00:00:00Z",
            final_instant="2026-08-18T11:00:00Z",
        )


def test_application_events_fall_back_to_v1_and_get_exact_event(tmp_path: Path, instance_root: Path) -> None:
    calls: list[tuple[str, str]] = []

    def requester(method, url, headers, body, context, timeout):
        calls.append((method, url))
        if method == "POST":
            return 404, {"message": "unsupported"}
        if url.endswith(f"/eventlog/{EVENT_ID}"):
            return 200, event_record()
        return 200, [event_record()]

    service, _ = configured_service(tmp_path, instance_root, requester)
    result = service.search_application_events(
        application_id=APPLICATION_ID,
        start_instant="2026-08-18T10:00:00Z",
        final_instant="2026-08-18T11:00:00Z",
        search_substring="failure",
    )
    assert result["api_version"] == "v1"
    assert calls[0][0] == "POST" and calls[1][0] == "GET"
    assert "startInstant=2026-08-18T10%3A00%3A00Z" in calls[1][1]

    single = service.get_application_event(application_id=APPLICATION_ID, event_id=EVENT_ID)
    assert single["status"] == "ready"
    assert single["event"]["id"] == EVENT_ID


def test_application_event_auth_failure_and_trace_keep_sources_separate(
    tmp_path: Path,
    instance_root: Path,
) -> None:
    (instance_root / "logs" / "server.log").write_text(
        f"task={TASK_ID} operation=operation-42 traceId=trace-42\n",
        encoding="utf-8",
    )

    def requester(method, url, headers, body, context, timeout):
        raise RuntimeRequestError("bad manager-password credential", status_code=401)

    service, _ = configured_service(tmp_path, instance_root, requester)
    failed = service.search_application_events(
        application_id=APPLICATION_ID,
        start_instant="2026-08-18T10:00:00Z",
        final_instant="2026-08-18T11:00:00Z",
    )
    assert failed["status"] == "unauthenticated"
    assert "manager-password" not in str(failed)

    traced = service.trace_operation(
        task_id=TASK_ID,
        operation_id="operation-42",
        trace_id="trace-42",
    )
    assert traced["server_logs"]["count"] == 1
    assert traced["application_events"]["status"] == "unauthenticated"
    assert traced["matching_policy"] == "exact_identifier_only"
    assert [item["kind"] for item in traced["sources"]] == [
        "console_task",
        "server_log",
        "application_event_log",
    ]
