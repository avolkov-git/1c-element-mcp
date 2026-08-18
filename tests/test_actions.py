from __future__ import annotations

import json
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

import pytest

from element_mcp.actions import ManagedActionError, ManagedActionsService
from element_mcp.config import ServerSettings
from element_mcp.console import ConsoleContextResolver, ConsoleHttpClient, ConsoleRequestError, ConsoleService

SPACE_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
APPLICATION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CONFIGURATION_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
TASK_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


class Clock:
    def __init__(self) -> None:
        self.value = 1_700_000_000.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _project() -> dict[str, Any]:
    return {
        "id": PROJECT_ID,
        "name": "Demo",
        "space-id": SPACE_ID,
        "deleted": False,
    }


def _application(*, status: str = "Running", task_id: str | None = None) -> dict[str, Any]:
    return {
        "id": APPLICATION_ID,
        "name": "demo-app",
        "space-id": SPACE_ID,
        "status": status,
        "source": {
            "type": "image",
            "image-id": PROJECT_ID,
            "project-version-id": CONFIGURATION_ID,
            "project-version": "1.0.0",
        },
        "current-task": {"id": task_id, "status": "InProgress"} if task_id else None,
    }


def _assembly() -> dict[str, Any]:
    return {
        "id": CONFIGURATION_ID,
        "assembly-version": "2.0.0",
        "project-id": PROJECT_ID,
        "project-name": "Demo",
    }


def _archive(path: Path, *, version: str = "2.0.0") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Project.yaml", f"Id: {CONFIGURATION_ID}\nVersion: {version}\n")
        archive.writestr("Common/Module.xbsl", "method Run()\nendmethod\n")


def _service(tmp_path: Path, requester: Any, clock: Clock | None = None) -> ManagedActionsService:
    settings = ServerSettings(
        config_path=tmp_path / "config" / "config.json",
        actions_config_path=tmp_path / "config" / "actions.json",
        data_path=tmp_path / "data",
    )
    resolver = ConsoleContextResolver(
        settings,
        environ={
            "ELEMENT_CONSOLE_URL": "https://element.example/console",
            "ELEMENT_CONSOLE_ACCESS_TOKEN": "test-token",
        },
    )
    console = ConsoleService(settings, resolver=resolver, client=ConsoleHttpClient(requester=requester))
    selected_clock = clock or Clock()
    return ManagedActionsService(
        settings,
        console,
        clock=selected_clock,
        sleeper=selected_clock.sleep,
    )


def _configure(
    service: ManagedActionsService,
    root: Path,
    *actions: str,
    ttl: int = 300,
) -> None:
    service.configure(
        enabled=True,
        allowed_actions=list(actions),
        allowed_project_ids=[PROJECT_ID],
        allowed_application_ids=[APPLICATION_ID],
        upload_roots=[root],
        max_upload_bytes=1024 * 1024,
        approval_ttl_seconds=ttl,
    )


def test_upload_uses_exact_target_once_and_never_audits_approval_token(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        calls.append({"method": method, "path": urllib.parse.urlsplit(url).path, "body": body})
        if method == "GET" and url.endswith(f"/projects/{PROJECT_ID}"):
            return _project()
        if method == "POST" and url.endswith(f"/projects/{PROJECT_ID}/assemblies"):
            return _assembly()
        raise AssertionError((method, url))

    archive = tmp_path / "demo.zip"
    _archive(archive)
    service = _service(tmp_path, requester)
    _configure(service, tmp_path, "upload_project_assembly")

    prepared = service.prepare_upload_project_assembly(
        project_id=PROJECT_ID,
        file_path=archive,
        expected_configuration_id=CONFIGURATION_ID,
    )
    token = prepared["approval_token"]
    result = service.upload_project_assembly(token)

    assert prepared["status"] == "approval_required"
    assert result["status"] == "accepted"
    writes = [call for call in calls if call["method"] == "POST"]
    assert len(writes) == 1
    assert writes[0]["path"] == f"/console/api/v2/projects/{PROJECT_ID}/assemblies"
    assert writes[0]["body"] == archive.read_bytes()
    with pytest.raises(ManagedActionError, match="уже использован"):
        service.upload_project_assembly(token)
    audit = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "data/audit").rglob("*.json"))
    assert token not in audit
    assert "test-token" not in audit


def test_update_application_uses_documented_body_and_returns_task(tmp_path: Path) -> None:
    writes: list[dict[str, Any]] = []

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        if method == "GET" and url.endswith(f"/applications/{APPLICATION_ID}"):
            return _application()
        if method == "GET" and url.endswith(f"/projects/{PROJECT_ID}/assemblies/2.0.0"):
            return _assembly()
        if method == "POST" and url.endswith(f"/applications/{APPLICATION_ID}/project/update"):
            writes.append({"url": url, "body": body})
            return _application(task_id=TASK_ID)
        raise AssertionError((method, url))

    service = _service(tmp_path, requester)
    _configure(service, tmp_path, "update_application")
    prepared = service.prepare_update_application(
        application_id=APPLICATION_ID,
        project_id=PROJECT_ID,
        assembly_version="2.0.0",
    )

    result = service.update_application(prepared["approval_token"])

    assert result["status"] == "accepted"
    assert result["task_id"] == TASK_ID
    assert len(writes) == 1
    assert json.loads(writes[0]["body"]) == {
        "source": {
            "type": "repository",
            "project-id": PROJECT_ID,
            "assembly-version": "2.0.0",
        }
    }


def test_state_change_is_blocked_when_precondition_changes(tmp_path: Path) -> None:
    status = "Running"
    writes = 0

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        nonlocal writes
        if method == "GET" and url.endswith(f"/applications/{APPLICATION_ID}"):
            return _application(status=status)
        if method == "GET" and url.endswith(f"/applications/{APPLICATION_ID}/status"):
            return {"status": status, "current-task": None}
        if method == "PUT":
            writes += 1
            return {"status": "Starting", "current-task": {"id": TASK_ID}}
        raise AssertionError((method, url))

    service = _service(tmp_path, requester)
    _configure(service, tmp_path, "start_application", "stop_application")
    prepared = service.prepare_application_state_change(application_id=APPLICATION_ID, desired_state="start")
    status = "Stopped"

    result = service.start_application(prepared["approval_token"])

    assert result["status"] == "precondition_failed"
    assert result["write_sent"] is False
    assert writes == 0


def test_ambiguous_write_is_not_retried_and_token_is_consumed(tmp_path: Path) -> None:
    writes = 0

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        nonlocal writes
        if method == "GET" and url.endswith(f"/applications/{APPLICATION_ID}"):
            return _application()
        if method == "GET" and url.endswith(f"/projects/{PROJECT_ID}/assemblies/2.0.0"):
            return _assembly()
        if method == "POST":
            writes += 1
            raise ConsoleRequestError("gateway timeout bearer should-not-leak", status_code=504)
        raise AssertionError((method, url))

    service = _service(tmp_path, requester)
    _configure(service, tmp_path, "update_application")
    prepared = service.prepare_update_application(
        application_id=APPLICATION_ID,
        project_id=PROJECT_ID,
        assembly_version="2.0.0",
    )

    result = service.update_application(prepared["approval_token"])

    assert result["status"] == "outcome_unknown"
    assert result["retry_allowed"] is False
    assert "should-not-leak" not in json.dumps(result)
    assert writes == 1
    with pytest.raises(ManagedActionError, match="уже использован"):
        service.update_application(prepared["approval_token"])


@pytest.mark.parametrize("http_status", [401, 403])
def test_forbidden_write_and_wrong_execute_tool_never_retry(tmp_path: Path, http_status: int) -> None:
    writes = 0

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        nonlocal writes
        if method == "GET" and url.endswith(f"/applications/{APPLICATION_ID}"):
            return _application()
        if method == "GET" and url.endswith(f"/applications/{APPLICATION_ID}/status"):
            return {"status": "Running", "current-task": None}
        if method == "PUT":
            writes += 1
            raise ConsoleRequestError("forbidden authorization=do-not-leak", status_code=http_status)
        raise AssertionError((method, url))

    service = _service(tmp_path, requester)
    _configure(service, tmp_path, "start_application", "stop_application")
    wrong = service.prepare_application_state_change(application_id=APPLICATION_ID, desired_state="start")
    with pytest.raises(ManagedActionError, match="другого действия"):
        service.stop_application(wrong["approval_token"])
    assert writes == 0

    prepared = service.prepare_application_state_change(application_id=APPLICATION_ID, desired_state="stop")
    result = service.stop_application(prepared["approval_token"])

    assert result["status"] == "rejected"
    assert result["http_status"] == http_status
    assert "do-not-leak" not in json.dumps(result)
    assert writes == 1


def test_expired_token_and_changed_archive_never_write(tmp_path: Path) -> None:
    clock = Clock()
    writes = 0

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        nonlocal writes
        if method == "GET":
            return _project()
        writes += 1
        return _assembly()

    archive = tmp_path / "demo.zip"
    _archive(archive)
    service = _service(tmp_path, requester, clock)
    _configure(service, tmp_path, "upload_project_assembly", ttl=30)
    expired = service.prepare_upload_project_assembly(project_id=PROJECT_ID, file_path=archive)
    clock.value += 31
    with pytest.raises(ManagedActionError, match="истёк"):
        service.upload_project_assembly(expired["approval_token"])

    current = service.prepare_upload_project_assembly(project_id=PROJECT_ID, file_path=archive)
    _archive(archive, version="2.0.1")
    result = service.upload_project_assembly(current["approval_token"])

    assert result["status"] == "precondition_failed"
    assert writes == 0


def test_wait_console_task_polls_only_existing_task_until_terminal(tmp_path: Path) -> None:
    clock = Clock()
    polls = 0

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        nonlocal polls
        assert method == "GET"
        assert url.endswith(f"/tasks/application-tasks/{TASK_ID}")
        polls += 1
        return {"id": TASK_ID, "status": "Completed" if polls == 3 else "InProgress"}

    service = _service(tmp_path, requester, clock)

    result = service.wait_console_task(task_id=TASK_ID, timeout_seconds=10, initial_interval_seconds=0.5)

    assert result["status"] == "completed"
    assert result["terminal_status"] == "Completed"
    assert result["polls"] == 3
    assert clock.value > 1_700_000_000.0


def test_wait_console_task_returns_last_state_at_bounded_timeout(tmp_path: Path) -> None:
    clock = Clock()
    polls = 0

    def requester(method: str, url: str, headers: Any, body: bytes | None, context: Any, timeout: float) -> Any:
        nonlocal polls
        polls += 1
        return {"id": TASK_ID, "status": "InProgress"}

    service = _service(tmp_path, requester, clock)

    result = service.wait_console_task(task_id=TASK_ID, timeout_seconds=1, initial_interval_seconds=0.5)

    assert result["status"] == "timeout"
    assert result["task"]["status"] == "InProgress"
    assert result["elapsed_seconds"] == 1
    assert polls == 3


def test_policy_is_default_deny_and_requires_exact_uuid(tmp_path: Path) -> None:
    service = _service(tmp_path, lambda *args: _project())

    assert service.configuration_info()["status"] == "disabled"
    with pytest.raises(ManagedActionError, match="выключены"):
        service.prepare_upload_project_assembly(project_id=PROJECT_ID, file_path=tmp_path / "missing.zip")

    _configure(service, tmp_path, "upload_project_assembly")
    with pytest.raises(ManagedActionError, match="allowlist"):
        service.prepare_upload_project_assembly(
            project_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            file_path=tmp_path / "missing.zip",
        )
