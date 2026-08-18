from __future__ import annotations

import hashlib
import json
import re
import secrets
import stat
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from element_mcp.config import ServerSettings
from element_mcp.console import (
    CONSOLE_CONTRACT_VERSION,
    ConsoleConfigurationError,
    ConsoleRequestError,
    ConsoleService,
    _read_settings_file,
    _write_console_settings,
)

ActionName = Literal[
    "upload_project_assembly",
    "update_application",
    "start_application",
    "stop_application",
]
TaskType = Literal["application", "deployment_instance", "group"]

ACTION_NAMES: tuple[ActionName, ...] = (
    "upload_project_assembly",
    "update_application",
    "start_application",
    "stop_application",
)
DEFAULT_APPROVAL_TTL_SECONDS = 300
MIN_APPROVAL_TTL_SECONDS = 30
MAX_APPROVAL_TTL_SECONDS = 900
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MIN_MAX_UPLOAD_BYTES = 1024
MAX_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100_000
MAX_PROJECT_YAML_BYTES = 1024 * 1024
MAX_AUDIT_DETAIL_TEXT = 1000
AMBIGUOUS_STATUSES = {None, 502, 503, 504}
SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)((?:client[_ -]?secret|password|authorization|access[_ -]?token)\s*[:=]\s*)[^\s,;]+"),
)
TERMINAL_TASK_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "completedpartially",
    "createerror",
}


class ManagedActionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    enabled: bool
    allowed_actions: frozenset[ActionName]
    allowed_project_ids: frozenset[str]
    allowed_application_ids: frozenset[str]
    upload_roots: tuple[Path, ...]
    max_upload_bytes: int
    approval_ttl_seconds: int

    def public_info(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "allowed_actions": sorted(self.allowed_actions),
            "allowed_project_ids": sorted(self.allowed_project_ids),
            "allowed_application_ids": sorted(self.allowed_application_ids),
            "upload_roots": [str(path) for path in self.upload_roots],
            "max_upload_bytes": self.max_upload_bytes,
            "approval_ttl_seconds": self.approval_ttl_seconds,
            "default_deny": True,
        }

    def fingerprint(self) -> str:
        payload = json.dumps(self.public_info(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedAction:
    approval_id: str
    action: ActionName
    target: dict[str, Any]
    request: dict[str, Any]
    precondition: dict[str, Any]
    policy_fingerprint: str
    prepared_at: float
    expires_at: float


class ActionsConfiguration:
    def __init__(self, settings: ServerSettings) -> None:
        self.path = settings.resolved_actions_config_path

    def policy(self) -> ActionPolicy:
        if not self.path.is_file():
            return ActionPolicy(
                enabled=False,
                allowed_actions=frozenset(),
                allowed_project_ids=frozenset(),
                allowed_application_ids=frozenset(),
                upload_roots=(),
                max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
                approval_ttl_seconds=DEFAULT_APPROVAL_TTL_SECONDS,
            )
        try:
            values = _read_settings_file(self.path)
        except ConsoleConfigurationError as error:
            raise ManagedActionError(str(error)) from error
        return _policy_from_values(values)

    def public_configuration(self) -> dict[str, Any]:
        try:
            policy = self.policy()
        except ManagedActionError as error:
            return {
                "status": "invalid",
                "enabled": False,
                "message": str(error),
                "default_deny": True,
            }
        return {
            "status": "enabled" if policy.enabled else "disabled",
            **policy.public_info(),
            "message": (
                "Управляемые действия включены только для перечисленных операций и целей"
                if policy.enabled
                else "Управляемые действия выключены"
            ),
        }

    def configure(
        self,
        *,
        enabled: bool,
        allowed_actions: Sequence[str],
        allowed_project_ids: Sequence[str],
        allowed_application_ids: Sequence[str],
        upload_roots: Sequence[str | Path],
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> dict[str, Any]:
        values = {
            "schema_version": 1,
            "enabled": enabled,
            "allowed_actions": list(allowed_actions),
            "allowed_project_ids": list(allowed_project_ids),
            "allowed_application_ids": list(allowed_application_ids),
            "upload_roots": [str(path) for path in upload_roots],
            "max_upload_bytes": max_upload_bytes,
            "approval_ttl_seconds": approval_ttl_seconds,
        }
        policy = _policy_from_values(values)
        _validate_enabled_policy(policy)
        _write_console_settings(self.path, values)
        return self.public_configuration()


class AuditLog:
    """Write one access-restricted immutable JSON record per audit event."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self._lock = threading.Lock()

    def record(
        self,
        event: str,
        *,
        approval_id: str | None,
        action: str | None,
        outcome: str,
        target: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        record_id = str(uuid.uuid4())
        payload = {
            "schema_version": 1,
            "record_id": record_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event[:100],
            "approval_id": approval_id,
            "action": action,
            "outcome": outcome[:100],
            "target": _safe_audit_value(target or {}),
            "details": _safe_audit_value(details or {}),
        }
        file_name = f"{time.time_ns()}-{record_id}.json"
        with self._lock:
            _write_console_settings(self.root / file_name, payload)


class ManagedActionsService:
    def __init__(
        self,
        settings: ServerSettings,
        console: ConsoleService,
        *,
        configuration: ActionsConfiguration | None = None,
        audit_log: AuditLog | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.console = console
        self.configuration = configuration or ActionsConfiguration(settings)
        self.audit_log = audit_log or AuditLog(settings.resolved_data_path / "audit" / "managed-actions")
        self.clock = clock
        self.sleeper = sleeper
        self._prepared: dict[str, PreparedAction] = {}
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def configuration_info(self) -> dict[str, Any]:
        return self.configuration.public_configuration()

    def configure(self, **values: Any) -> dict[str, Any]:
        return self.configuration.configure(**values)

    def prepare_upload_project_assembly(
        self,
        *,
        project_id: str,
        file_path: str | Path,
        expected_configuration_id: str | None = None,
    ) -> dict[str, Any]:
        policy = self._authorize("upload_project_assembly", project_id=project_id)
        selected_project = _uuid(project_id, "project_id")
        project = self._ready_resource(self.console.get_project(selected_project), "project")
        archive = _inspect_archive(Path(file_path), policy, expected_configuration_id)
        target = {
            "project_id": selected_project,
            "project_name": project.get("name"),
            "file_name": archive["file_name"],
            "file_size": archive["size_bytes"],
            "file_sha256": archive["sha256"],
            "archive_configuration_id": archive["configuration_id"],
            "archive_version": archive["version"],
        }
        return self._issue(
            "upload_project_assembly",
            policy,
            target=target,
            request={"project_id": selected_project, "file_path": str(archive["path"])},
            precondition={
                "file_sha256": archive["sha256"],
                "file_size": archive["size_bytes"],
                "configuration_id": archive["configuration_id"],
            },
            risks=[
                "Будет загружена новая сборка в выбранный проект Console.",
                "При неопределённом сетевом результате запрос не будет повторён автоматически.",
            ],
        )

    def prepare_update_application(
        self,
        *,
        application_id: str,
        project_id: str,
        assembly_version: str,
    ) -> dict[str, Any]:
        policy = self._authorize(
            "update_application",
            application_id=application_id,
            project_id=project_id,
        )
        selected_application = _uuid(application_id, "application_id")
        selected_project = _uuid(project_id, "project_id")
        version = _text(assembly_version, "assembly_version", 300)
        application = self._ready_resource(self.console.get_application(selected_application), "application")
        assembly = self._ready_resource(
            self.console.get_project_assembly(version, selected_project),
            "assembly",
        )
        current = _application_precondition(application)
        target = {
            "application_id": selected_application,
            "application_name": application.get("name"),
            "project_id": selected_project,
            "assembly_version": assembly.get("assembly_version") or version,
            "assembly_id": assembly.get("id"),
            "current_source": current,
        }
        return self._issue(
            "update_application",
            policy,
            target=target,
            request={
                "application_id": selected_application,
                "project_id": selected_project,
                "assembly_version": version,
            },
            precondition=current,
            risks=[
                "Версия опубликованного приложения будет изменена на выбранную сборку.",
                "Операция может запустить асинхронную задачу и временно изменить доступность приложения.",
            ],
        )

    def prepare_application_state_change(
        self,
        *,
        application_id: str,
        desired_state: Literal["start", "stop"],
    ) -> dict[str, Any]:
        if desired_state not in {"start", "stop"}:
            raise ManagedActionError("desired_state должен быть start или stop")
        action: ActionName = "start_application" if desired_state == "start" else "stop_application"
        policy = self._authorize(action, application_id=application_id)
        selected = _uuid(application_id, "application_id")
        application = self._ready_resource(self.console.get_application(selected), "application")
        status = self._ready_resource(self.console.get_application_status(selected), "application_status")
        precondition = {"status": status.get("status"), "current_task_id": _task_id(status)}
        return self._issue(
            action,
            policy,
            target={
                "application_id": selected,
                "application_name": application.get("name"),
                "current_status": status.get("status"),
                "desired_state": desired_state,
            },
            request={"application_id": selected},
            precondition=precondition,
            risks=[
                (
                    "Приложение будет запущено и начнёт принимать предусмотренную конфигурацией нагрузку."
                    if desired_state == "start"
                    else "Приложение будет остановлено; активные пользователи могут потерять доступ."
                ),
                "Операция может выполняться асинхронно; HTTP 200 не означает завершение задачи.",
            ],
        )

    def upload_project_assembly(self, approval_token: str) -> dict[str, Any]:
        prepared, policy = self._consume(approval_token, "upload_project_assembly")
        try:
            archive = _inspect_archive(
                Path(prepared.request["file_path"]),
                policy,
                prepared.precondition.get("configuration_id"),
            )
        except ManagedActionError as error:
            return self._precondition_failed(prepared, str(error))
        if (
            archive["sha256"] != prepared.precondition["file_sha256"]
            or archive["size_bytes"] != prepared.precondition["file_size"]
        ):
            return self._precondition_failed(prepared, "Файл изменился после подготовки")
        body = archive["path"].read_bytes()
        if len(body) != prepared.precondition["file_size"] or hashlib.sha256(body).hexdigest() != prepared.precondition[
            "file_sha256"
        ]:
            return self._precondition_failed(prepared, "Файл изменился во время чтения")
        return self._execute_request(
            prepared,
            method="POST",
            path=f"/api/v2/projects/{prepared.request['project_id']}/assemblies",
            body=body,
            content_type="application/octet-stream",
            reconciliation={
                "tool": "list_project_assemblies",
                "project_id": prepared.request["project_id"],
                "file_sha256": prepared.precondition["file_sha256"],
            },
        )

    def update_application(self, approval_token: str) -> dict[str, Any]:
        prepared, _ = self._consume(approval_token, "update_application")
        try:
            current = self._ready_resource(
                self.console.get_application(prepared.request["application_id"]),
                "application",
            )
        except ManagedActionError as error:
            return self._precondition_failed(prepared, str(error))
        if _application_precondition(current) != prepared.precondition:
            return self._precondition_failed(prepared, "Источник или задача приложения изменились после подготовки")
        body = json.dumps(
            {
                "source": {
                    "type": "repository",
                    "project-id": prepared.request["project_id"],
                    "assembly-version": prepared.request["assembly_version"],
                }
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return self._execute_request(
            prepared,
            method="POST",
            path=f"/api/v2/applications/{prepared.request['application_id']}/project/update",
            body=body,
            content_type="application/json",
            reconciliation={
                "tools": ["get_application", "get_application_project", "list_console_tasks"],
                "application_id": prepared.request["application_id"],
            },
        )

    def start_application(self, approval_token: str) -> dict[str, Any]:
        return self._execute_state_change(approval_token, "start_application", "start")

    def stop_application(self, approval_token: str) -> dict[str, Any]:
        return self._execute_state_change(approval_token, "stop_application", "stop")

    def wait_console_task(
        self,
        *,
        task_id: str,
        task_type: TaskType = "application",
        timeout_seconds: float = 30,
        initial_interval_seconds: float = 0.5,
    ) -> dict[str, Any]:
        selected = _uuid(task_id, "task_id")
        if not 0 <= timeout_seconds <= 120:
            raise ManagedActionError("timeout_seconds должен быть в диапазоне 0..120")
        if not 0.1 <= initial_interval_seconds <= 5:
            raise ManagedActionError("initial_interval_seconds должен быть в диапазоне 0.1..5")
        started = self.clock()
        interval = initial_interval_seconds
        polls = 0
        last: dict[str, Any] | None = None
        while True:
            polls += 1
            last = self.console.get_task(task_type, selected)
            if last.get("status") != "ready":
                return {"status": last.get("status", "error"), "polls": polls, "result": last}
            task = last.get("task")
            task_status = str(task.get("status", "")) if isinstance(task, Mapping) else ""
            if task_status.replace("_", "").replace("-", "").casefold() in TERMINAL_TASK_STATUSES:
                return {
                    "status": "completed",
                    "terminal_status": task_status,
                    "polls": polls,
                    "elapsed_seconds": round(self.clock() - started, 3),
                    "task": task,
                }
            elapsed = self.clock() - started
            if elapsed >= timeout_seconds:
                return {
                    "status": "timeout",
                    "terminal_status": None,
                    "polls": polls,
                    "elapsed_seconds": round(elapsed, 3),
                    "task": task,
                    "message": "Задача продолжает выполняться; действие не запускалось повторно",
                }
            delay = min(interval, timeout_seconds - elapsed)
            self.sleeper(delay)
            interval = min(interval * 1.7, 5.0)

    def _execute_state_change(
        self,
        approval_token: str,
        action: Literal["start_application", "stop_application"],
        route: Literal["start", "stop"],
    ) -> dict[str, Any]:
        prepared, _ = self._consume(approval_token, action)
        try:
            current = self._ready_resource(
                self.console.get_application_status(prepared.request["application_id"]),
                "application_status",
            )
        except ManagedActionError as error:
            return self._precondition_failed(prepared, str(error))
        precondition = {"status": current.get("status"), "current_task_id": _task_id(current)}
        if precondition != prepared.precondition:
            return self._precondition_failed(prepared, "Статус или текущая задача приложения изменились")
        return self._execute_request(
            prepared,
            method="PUT",
            path=f"/api/v2/applications/{prepared.request['application_id']}/status/{route}",
            body=None,
            content_type=None,
            reconciliation={
                "tools": ["get_application_status", "list_console_tasks"],
                "application_id": prepared.request["application_id"],
            },
        )

    def _authorize(
        self,
        action: ActionName,
        *,
        project_id: str | None = None,
        application_id: str | None = None,
    ) -> ActionPolicy:
        policy = self.configuration.policy()
        if not policy.enabled:
            raise ManagedActionError("Управляемые действия выключены в локальной конфигурации MCP")
        if action not in policy.allowed_actions:
            raise ManagedActionError(f"Действие {action} отсутствует в allowlist")
        if project_id is not None and _uuid(project_id, "project_id") not in policy.allowed_project_ids:
            raise ManagedActionError("project_id отсутствует в allowlist управляемых действий")
        if application_id is not None and _uuid(application_id, "application_id") not in policy.allowed_application_ids:
            raise ManagedActionError("application_id отсутствует в allowlist управляемых действий")
        return policy

    def _issue(
        self,
        action: ActionName,
        policy: ActionPolicy,
        *,
        target: dict[str, Any],
        request: dict[str, Any],
        precondition: dict[str, Any],
        risks: list[str],
    ) -> dict[str, Any]:
        now = self.clock()
        token = secrets.token_urlsafe(32)
        digest = _token_digest(token)
        prepared = PreparedAction(
            approval_id=str(uuid.uuid4()),
            action=action,
            target=target,
            request=request,
            precondition=precondition,
            policy_fingerprint=policy.fingerprint(),
            prepared_at=now,
            expires_at=now + policy.approval_ttl_seconds,
        )
        with self._lock:
            self._purge_locked(now)
            self._prepared[digest] = prepared
        self.audit_log.record(
            "prepared",
            approval_id=prepared.approval_id,
            action=action,
            outcome="awaiting_user_confirmation",
            target=target,
            details={"expires_at": _timestamp(prepared.expires_at), "policy_fingerprint": policy.fingerprint()},
        )
        return {
            "status": "approval_required",
            "approval_id": prepared.approval_id,
            "approval_token": token,
            "action": action,
            "target": target,
            "risks": risks,
            "prepared_at": _timestamp(now),
            "expires_at": _timestamp(prepared.expires_at),
            "one_time": True,
            "binding": "action, exact target, request parameters, precondition, policy fingerprint",
            "confirmation_required": (
                "Покажите пользователю action, target, risks и expires_at. Вызывайте execute tool только после "
                "его явного подтверждения, полученного после этого prepare."
            ),
            "elicitation": {
                "used": False,
                "reason": "Server-side prepare/execute fallback works with stateless and non-elicitation clients",
            },
        }

    def _consume(self, token: str, expected_action: ActionName) -> tuple[PreparedAction, ActionPolicy]:
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise ManagedActionError("Некорректный approval token")
        digest = _token_digest(token)
        now = self.clock()
        with self._lock:
            self._purge_locked(now)
            if digest in self._used:
                raise ManagedActionError("Approval token уже использован")
            prepared = self._prepared.pop(digest, None)
            if prepared is None:
                raise ManagedActionError("Approval token неизвестен или истёк")
            self._used[digest] = now + MAX_APPROVAL_TTL_SECONDS
        if prepared.action != expected_action:
            self._audit_rejection(prepared, "wrong_action")
            raise ManagedActionError("Approval token подготовлен для другого действия")
        if now >= prepared.expires_at:
            self._audit_rejection(prepared, "expired")
            raise ManagedActionError("Approval token истёк")
        try:
            policy = self._authorize(
                expected_action,
                project_id=prepared.request.get("project_id"),
                application_id=prepared.request.get("application_id"),
            )
        except ManagedActionError:
            self._audit_rejection(prepared, "policy_rejected")
            raise
        if policy.fingerprint() != prepared.policy_fingerprint:
            self._audit_rejection(prepared, "policy_changed")
            raise ManagedActionError("Политика управляемых действий изменилась; выполните prepare заново")
        self.audit_log.record(
            "execute_started",
            approval_id=prepared.approval_id,
            action=prepared.action,
            outcome="token_consumed",
            target=prepared.target,
        )
        return prepared, policy

    def _execute_request(
        self,
        prepared: PreparedAction,
        *,
        method: Literal["POST", "PUT"],
        path: str,
        body: bytes | None,
        content_type: str | None,
        reconciliation: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            connection = self.console.resolver.resolve()
            value = self.console.client.mutate(
                connection,
                method,
                path,
                body=body,
                content_type=content_type,
            )
        except ConsoleConfigurationError as error:
            return self._execution_failure(prepared, "configuration_error", str(error), None)
        except ConsoleRequestError as error:
            if error.status_code in AMBIGUOUS_STATUSES:
                result = {
                    "status": "outcome_unknown",
                    "approval_id": prepared.approval_id,
                    "action": prepared.action,
                    "target": prepared.target,
                    "http_status": error.status_code,
                    "message": _safe_text(str(error), 500),
                    "retry_allowed": False,
                    "reconciliation": reconciliation,
                }
                self._audit_result(prepared, "outcome_unknown", result)
                return result
            return self._execution_failure(prepared, "rejected", str(error), error.status_code)
        response = _write_response(value)
        task_id = _task_id(response)
        result = {
            "status": "accepted",
            "approval_id": prepared.approval_id,
            "action": prepared.action,
            "target": prepared.target,
            "response": response,
            "task_id": task_id,
            "task_type": "application" if task_id else None,
            "server_task_completed": False if task_id else None,
            "message": (
                "Запрос принят; используйте wait_console_task для отслеживания серверной задачи"
                if task_id
                else "HTTP-операция завершилась без task ID; результат приведён в response"
            ),
            "retry_allowed": False,
            "contract_element_version": CONSOLE_CONTRACT_VERSION,
        }
        self._audit_result(prepared, "accepted", result)
        return result

    def _precondition_failed(self, prepared: PreparedAction, message: str) -> dict[str, Any]:
        result = {
            "status": "precondition_failed",
            "approval_id": prepared.approval_id,
            "action": prepared.action,
            "target": prepared.target,
            "message": message,
            "retry_allowed": False,
            "write_sent": False,
        }
        self._audit_result(prepared, "precondition_failed", result)
        return result

    def _execution_failure(
        self,
        prepared: PreparedAction,
        status: str,
        message: str,
        http_status: int | None,
    ) -> dict[str, Any]:
        result = {
            "status": status,
            "approval_id": prepared.approval_id,
            "action": prepared.action,
            "target": prepared.target,
            "http_status": http_status,
            "message": _safe_text(message, 500),
            "retry_allowed": False,
        }
        self._audit_result(prepared, status, result)
        return result

    def _audit_rejection(self, prepared: PreparedAction, outcome: str) -> None:
        self.audit_log.record(
            "execute_rejected",
            approval_id=prepared.approval_id,
            action=prepared.action,
            outcome=outcome,
            target=prepared.target,
        )

    def _audit_result(self, prepared: PreparedAction, outcome: str, result: dict[str, Any]) -> None:
        try:
            self.audit_log.record(
                "execute_finished",
                approval_id=prepared.approval_id,
                action=prepared.action,
                outcome=outcome,
                target=prepared.target,
                details={
                    "http_status": result.get("http_status"),
                    "task_id": result.get("task_id"),
                    "message": result.get("message"),
                },
            )
        except ConsoleConfigurationError:
            # execute_started was durably written before the network mutation.
            result["audit_status"] = "final_record_failed"

    def _purge_locked(self, now: float) -> None:
        self._prepared = {
            key: value
            for key, value in self._prepared.items()
            if value.expires_at + MAX_APPROVAL_TTL_SECONDS > now
        }
        self._used = {key: expires for key, expires in self._used.items() if expires > now}

    @staticmethod
    def _ready_resource(result: Mapping[str, Any], key: str) -> dict[str, Any]:
        if result.get("status") != "ready":
            raise ManagedActionError(str(result.get("message") or f"Ресурс {key} недоступен")[:500])
        value = result.get(key)
        if not isinstance(value, Mapping):
            raise ManagedActionError(f"Console не вернула ресурс {key}")
        return dict(value)


def _policy_from_values(values: Mapping[str, Any]) -> ActionPolicy:
    enabled = _boolean(values.get("enabled", False), "enabled")
    allowed_actions = _string_list(values.get("allowed_actions", []), "allowed_actions", 4)
    unknown = set(allowed_actions) - set(ACTION_NAMES)
    if unknown:
        raise ManagedActionError(f"Неизвестные управляемые действия: {', '.join(sorted(unknown))}")
    project_values = _string_list(values.get("allowed_project_ids", []), "allowed_project_ids", 100)
    project_ids = frozenset(_uuid(value, "project_id") for value in project_values)
    application_ids = frozenset(
        _uuid(value, "application_id")
        for value in _string_list(values.get("allowed_application_ids", []), "allowed_application_ids", 100)
    )
    root_values = _string_list(values.get("upload_roots", []), "upload_roots", 20)
    roots: list[Path] = []
    for value in root_values:
        root = Path(value).expanduser().resolve()
        if enabled and not root.is_dir():
            raise ManagedActionError(f"Разрешённый каталог загрузки не найден: {root}")
        roots.append(root)
    max_upload = _bounded_int(
        values.get("max_upload_bytes", DEFAULT_MAX_UPLOAD_BYTES),
        "max_upload_bytes",
        MIN_MAX_UPLOAD_BYTES,
        MAX_MAX_UPLOAD_BYTES,
    )
    ttl = _bounded_int(
        values.get("approval_ttl_seconds", DEFAULT_APPROVAL_TTL_SECONDS),
        "approval_ttl_seconds",
        MIN_APPROVAL_TTL_SECONDS,
        MAX_APPROVAL_TTL_SECONDS,
    )
    policy = ActionPolicy(
        enabled=enabled,
        allowed_actions=frozenset(allowed_actions),  # type: ignore[arg-type]
        allowed_project_ids=project_ids,
        allowed_application_ids=application_ids,
        upload_roots=tuple(dict.fromkeys(roots)),
        max_upload_bytes=max_upload,
        approval_ttl_seconds=ttl,
    )
    _validate_enabled_policy(policy)
    return policy


def _validate_enabled_policy(policy: ActionPolicy) -> None:
    if not policy.enabled:
        return
    if not policy.allowed_actions:
        raise ManagedActionError("При включении укажите хотя бы одно разрешённое действие")
    if "upload_project_assembly" in policy.allowed_actions:
        if not policy.allowed_project_ids:
            raise ManagedActionError("Для загрузки сборки нужен allowed_project_ids")
        if not policy.upload_roots:
            raise ManagedActionError("Для загрузки сборки нужен хотя бы один upload_root")
    if (
        "update_application" in policy.allowed_actions
        and (not policy.allowed_application_ids or not policy.allowed_project_ids)
    ):
        raise ManagedActionError("Для обновления нужны allowed_application_ids и allowed_project_ids")
    if {"start_application", "stop_application"} & policy.allowed_actions and not policy.allowed_application_ids:
        raise ManagedActionError("Для запуска или остановки нужен allowed_application_ids")


def _inspect_archive(path: Path, policy: ActionPolicy, expected_configuration_id: str | None) -> dict[str, Any]:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ManagedActionError("Файл сборки не должен быть символической ссылкой")
    try:
        resolved = candidate.resolve(strict=True)
        file_stat = resolved.stat()
    except OSError as error:
        raise ManagedActionError(f"Файл сборки недоступен: {error}") from error
    if not stat.S_ISREG(file_stat.st_mode):
        raise ManagedActionError("Файл сборки должен быть обычным файлом")
    if not any(resolved.is_relative_to(root) for root in policy.upload_roots):
        raise ManagedActionError("Файл сборки находится вне разрешённых upload_roots")
    if not 0 < file_stat.st_size <= policy.max_upload_bytes:
        raise ManagedActionError(f"Размер файла сборки должен быть в диапазоне 1..{policy.max_upload_bytes} байт")
    configuration_id, version = _archive_project_metadata(resolved)
    if expected_configuration_id is not None:
        expected = _uuid(expected_configuration_id, "expected_configuration_id")
        if configuration_id != expected:
            raise ManagedActionError("Ид проекта внутри архива не совпадает с expected_configuration_id")
    return {
        "path": resolved,
        "file_name": resolved.name,
        "size_bytes": file_stat.st_size,
        "sha256": _file_sha256(resolved),
        "configuration_id": configuration_id,
        "version": version,
    }


def _archive_project_metadata(path: Path) -> tuple[str, str | None]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ManagedActionError("Архив сборки пуст или содержит слишком много записей")
            candidates = []
            for info in infos:
                normalized = info.filename.replace("\\", "/").strip("/")
                if normalized in {"Project.yaml", "Проект.yaml", "V8Assembly.yaml"}:
                    candidates.append(info)
            if len(candidates) != 1:
                raise ManagedActionError(
                    "В корне архива должен быть один Project.yaml, Проект.yaml или V8Assembly.yaml"
                )
            info = candidates[0]
            if info.flag_bits & 0x1:
                raise ManagedActionError("Зашифрованный архив сборки не поддерживается")
            if info.file_size > MAX_PROJECT_YAML_BYTES:
                raise ManagedActionError("Описание проекта в архиве слишком велико")
            with archive.open(info) as stream:
                raw = stream.read(MAX_PROJECT_YAML_BYTES + 1)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ManagedActionError(f"Файл не является корректным ZIP-архивом сборки: {error}") from error
    if len(raw) > MAX_PROJECT_YAML_BYTES:
        raise ManagedActionError("Описание проекта в архиве слишком велико")
    try:
        value = yaml.safe_load(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ManagedActionError("Не удалось прочитать описание проекта из архива") from error
    if not isinstance(value, Mapping):
        raise ManagedActionError("Описание проекта в архиве должно быть YAML-объектом")
    configuration_id = value.get("Id", value.get("Ид"))
    version = value.get("Version", value.get("Версия"))
    return _uuid(configuration_id, "Id/Ид архива"), str(version)[:300] if version is not None else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _application_precondition(application: Mapping[str, Any]) -> dict[str, Any]:
    source = application.get("source")
    source = source if isinstance(source, Mapping) else {}
    return {
        "status": application.get("status"),
        "current_task_id": _task_id(application),
        "source_project_id": source.get("project_id"),
        "source_project_version": source.get("project_version"),
        "source_project_version_id": source.get("project_version_id"),
    }


def _write_response(value: Any) -> dict[str, Any] | list[Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        allowed = {
            "id",
            "status",
            "current-task",
            "currentTask",
            "assembly-version",
            "assemblyVersion",
            "project-id",
            "projectId",
            "project-name",
            "projectName",
            "project-version",
            "projectVersion",
            "date-created",
            "dateCreated",
            "error",
        }
        return {str(key): _safe_audit_value(child) for key, child in value.items() if str(key) in allowed}
    if isinstance(value, list):
        return [_safe_audit_value(item) for item in value[:20]]
    return {"value": _safe_text(str(value), MAX_AUDIT_DETAIL_TEXT)}


def _task_id(value: Mapping[str, Any] | dict[str, Any] | list[Any] | None) -> str | None:
    if not isinstance(value, Mapping):
        return None
    task = value.get("current_task", value.get("current-task", value.get("currentTask")))
    if isinstance(task, Mapping) and isinstance(task.get("id"), str):
        try:
            return _uuid(task["id"], "task_id")
        except ManagedActionError:
            return None
    return None


def _safe_audit_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "[truncated]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _safe_text(value, MAX_AUDIT_DETAIL_TEXT)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= 50:
                break
            text_key = str(key)[:100]
            if any(secret in text_key.casefold() for secret in ("token", "secret", "password", "authorization")):
                result[text_key] = "[redacted]"
            else:
                result[text_key] = _safe_audit_value(child, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_safe_audit_value(item, depth=depth + 1) for item in list(value)[:50]]
    return _safe_text(str(value), MAX_AUDIT_DETAIL_TEXT)


def _safe_text(value: str, limit: int) -> str:
    result = value.replace("\r", " ").replace("\n", " ")
    for pattern in SENSITIVE_TEXT_PATTERNS:
        result = pattern.sub(r"\1[redacted]", result)
    return result[:limit]


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def _uuid(value: Any, name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError) as error:
        raise ManagedActionError(f"{name} должен быть UUID") from error


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManagedActionError(f"Укажите {name}")
    result = value.strip()
    if len(result) > limit or "/" in result or "\\" in result or ".." in result:
        raise ManagedActionError(f"Некорректный {name}")
    return result


def _string_list(value: Any, name: str, limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ManagedActionError(f"{name} должен быть списком длиной не более {limit}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 1000:
            raise ManagedActionError(f"Некорректное значение {name}")
        result.append(item.strip())
    return list(dict.fromkeys(result))


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ManagedActionError(f"{name} должен быть целым числом в диапазоне {minimum}..{maximum}")
    return value


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ManagedActionError(f"{name} должен быть Boolean")
