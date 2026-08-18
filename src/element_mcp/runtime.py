from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from element_mcp.config import ServerSettings
from element_mcp.console import (
    ConsoleConfigurationError,
    ConsoleService,
    _protect_secret_for_storage,
    _read_settings_file,
    _unprotect_windows_secret,
    _write_console_settings,
)

MAX_LOG_FILES = 100
MAX_LOG_READ_BYTES = 512 * 1024
MAX_LOG_SEARCH_BYTES = 2 * 1024 * 1024
MAX_LOG_LINES = 1000
MAX_LOG_MATCHES = 200
MAX_EVENT_SIZE = 100
MAX_EVENT_RANGE = timedelta(days=31)
MAX_EVENT_TEXT = 8000
MAX_EVENT_PROPERTIES = 100
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
EVENT_CONTRACT_VERSION = "9.2.4-6"
DEFAULT_TIMEOUT_SECONDS = 30.0

_LOG_NAME = re.compile(
    r"^(?:launcher(?:_\d+)?|server|clients|unclosed_resources|debugger|access|console-executor)"
    r"(?:[._-][A-Za-z0-9]+)*\.log(?:\.\d+)?$",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(authorization|client[_-]?secret|access[_-]?token|refresh[_-]?token|password|passwd|cookie)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_USER_ID = re.compile(r"(?i)(\buserId\s*[=:]\s*)([^\s,;\]}]+)")
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|authorization|cookie|session|credential|private.?key|connection.?string)"
)
_CORRELATION_KEYS = {
    "operationid",
    "operation_id",
    "traceid",
    "trace_id",
    "requestid",
    "request_id",
    "taskid",
    "task_id",
    "applicationid",
    "application_id",
    "appid",
}


class RuntimeConfigurationError(ValueError):
    pass


class RuntimeRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ApplicationManagerConnection:
    base_url: str
    username: str
    password: str = field(repr=False)
    api_version: Literal["auto", "v1", "v2"] = "auto"
    verify_tls: bool = True
    ca_bundle: Path | None = None
    source: str = "runtime_config"

    def public_info(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "username_present": bool(self.username),
            "api_version": self.api_version,
            "verify_tls": self.verify_tls,
            "ca_bundle": str(self.ca_bundle) if self.ca_bundle else None,
            "source": self.source,
        }


RuntimeRequester = Callable[
    [str, str, Mapping[str, str], bytes | None, ssl.SSLContext | None, float],
    tuple[int, Any],
]


class RuntimeConfiguration:
    def __init__(self, settings: ServerSettings, *, environ: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self.environ = environ if environ is not None else os.environ
        self.path = settings.resolved_runtime_config_path

    def instance_root(self) -> tuple[Path | None, str]:
        explicit = self.environ.get("ELEMENT_INSTANCE_ROOT")
        if explicit:
            return Path(explicit).expanduser().resolve(), "environment"
        values = self._values()
        configured = values.get("instance_root")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser().resolve(), "runtime_config"
        for candidate in _standard_instance_roots():
            if candidate.is_dir():
                return candidate.resolve(), "standard_path"
        return None, "missing"

    def application_manager(self) -> ApplicationManagerConnection | None:
        environment_url = self.environ.get("ELEMENT_APPLICATION_MANAGER_URL")
        if environment_url:
            return _application_manager_from_values(
                {
                    "enabled": True,
                    "server": environment_url,
                    "username": self.environ.get("ELEMENT_APPLICATION_MANAGER_USERNAME"),
                    "password": self.environ.get("ELEMENT_APPLICATION_MANAGER_PASSWORD"),
                    "api_version": self.environ.get("ELEMENT_APPLICATION_MANAGER_API_VERSION", "auto"),
                    "verify_tls": self.environ.get("ELEMENT_APPLICATION_MANAGER_VERIFY_TLS", "true"),
                    "ca_bundle": self.environ.get("ELEMENT_APPLICATION_MANAGER_CA_BUNDLE"),
                },
                source="environment",
            )
        values = self._values().get("application_manager")
        if not isinstance(values, Mapping) or not _boolean(values.get("enabled", False), "enabled"):
            return None
        return _application_manager_from_values(values, source="runtime_config")

    def public_configuration(self) -> dict[str, Any]:
        root, source = self.instance_root()
        values = self._values()
        manager_values = values.get("application_manager")
        manager_mapping = manager_values if isinstance(manager_values, Mapping) else {}
        manager_enabled = _boolean(manager_mapping.get("enabled", False), "enabled")
        password_present = bool(manager_mapping.get("password") or manager_mapping.get("password_dpapi"))
        try:
            manager = self.application_manager()
            manager_status = "configured" if manager else "disabled"
            manager_message = None
        except RuntimeConfigurationError as error:
            manager = None
            manager_status = "invalid"
            manager_message = str(error)
        return {
            "status": "configured" if root else "missing",
            "instance_root": str(root) if root else None,
            "instance_root_source": source,
            "application_manager": {
                "status": manager_status,
                "enabled": manager_enabled,
                "server": manager.base_url if manager else manager_mapping.get("server"),
                "username": manager.username if manager else manager_mapping.get("username"),
                "api_version": manager.api_version if manager else manager_mapping.get("api_version", "auto"),
                "verify_tls": manager.verify_tls if manager else manager_mapping.get("verify_tls", True),
                "password_present": password_present,
                "password_storage": (
                    "windows_dpapi"
                    if manager_mapping.get("password_dpapi")
                    else "restricted_file"
                    if manager_mapping.get("password")
                    else None
                ),
                "message": manager_message,
            },
        }

    def configure(
        self,
        *,
        instance_root: str | Path,
        application_manager_enabled: bool = False,
        server: str | None = None,
        username: str | None = None,
        password: str | None = None,
        api_version: Literal["auto", "v1", "v2"] = "auto",
        verify_tls: bool = True,
    ) -> dict[str, Any]:
        root = _validate_instance_root(Path(instance_root).expanduser().resolve())
        existing = self._values()
        stored: dict[str, Any] = {"schema_version": 1, "instance_root": str(root)}
        manager_existing = existing.get("application_manager")
        manager_existing = dict(manager_existing) if isinstance(manager_existing, Mapping) else {}
        manager: dict[str, Any] = {"enabled": bool(application_manager_enabled)}
        if application_manager_enabled:
            if not isinstance(server, str) or not isinstance(username, str):
                raise RuntimeConfigurationError("Укажите адрес и имя пользователя Application Manager")
            normalized_url = _normalize_manager_url(server)
            normalized_username = username.strip()
            if not normalized_username:
                raise RuntimeConfigurationError("Укажите имя пользователя Application Manager")
            if api_version not in {"auto", "v1", "v2"}:
                raise RuntimeConfigurationError("Версия Application Manager API должна быть auto, v1 или v2")
            supplied = password.strip() if isinstance(password, str) else ""
            same_identity = (
                manager_existing.get("server") == normalized_url
                and manager_existing.get("username") == normalized_username
            )
            if not supplied and not same_identity:
                raise RuntimeConfigurationError("Введите пароль для нового подключения Application Manager")
            manager.update(
                {
                    "server": normalized_url,
                    "username": normalized_username,
                    "api_version": api_version,
                    "verify_tls": bool(verify_tls),
                }
            )
            if supplied:
                key, protected = _protect_secret_for_storage(supplied)
                manager["password_dpapi" if key.endswith("_dpapi") else "password"] = protected
            else:
                for key in ("password_dpapi", "password"):
                    if key in manager_existing:
                        manager[key] = manager_existing[key]
                        break
                else:
                    raise RuntimeConfigurationError("Введите пароль Application Manager")
        stored["application_manager"] = manager
        _write_console_settings(self.path, stored)
        return self.public_configuration()

    def _values(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            return _read_settings_file(self.path)
        except ConsoleConfigurationError as error:
            raise RuntimeConfigurationError(str(error)) from error


class ApplicationManagerClient:
    def __init__(
        self,
        *,
        requester: RuntimeRequester | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.requester = requester or _urlopen_json
        self.timeout = timeout

    def search_events(
        self,
        connection: ApplicationManagerConnection,
        application_id: str,
        request: Mapping[str, Any],
    ) -> tuple[list[Any], str]:
        versions = [connection.api_version] if connection.api_version != "auto" else ["v2", "v1"]
        last_error: RuntimeRequestError | None = None
        for version in versions:
            try:
                if version == "v2":
                    status, value = self._request(
                        connection,
                        "POST",
                        f"/manager/api/v2/applications/{application_id}/eventlog",
                        request,
                    )
                else:
                    query = urllib.parse.urlencode(_v1_event_parameters(request), doseq=False)
                    status, value = self._request(
                        connection,
                        "GET",
                        f"/manager/api/v1/applications/{application_id}/eventlog?{query}",
                        None,
                    )
                if status == 204 or value is None:
                    return [], version
                if not isinstance(value, list):
                    raise RuntimeRequestError("Application Manager вернул некорректный список событий")
                return value, version
            except RuntimeRequestError as error:
                last_error = error
                if connection.api_version != "auto" or error.status_code not in {404, 405, 501}:
                    raise
        assert last_error is not None
        raise last_error

    def get_event(
        self,
        connection: ApplicationManagerConnection,
        application_id: str,
        event_id: str,
    ) -> Any:
        status, value = self._request(
            connection,
            "GET",
            f"/manager/api/v1/applications/{application_id}/eventlog/{event_id}",
            None,
        )
        return None if status == 204 else value

    def _request(
        self,
        connection: ApplicationManagerConnection,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
    ) -> tuple[int, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        credential = base64.b64encode(f"{connection.username}:{connection.password}".encode()).decode("ascii")
        headers = {"Accept": "application/json", "Authorization": f"Basic {credential}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        context = _ssl_context(connection.verify_tls, connection.ca_bundle)
        try:
            status, value = self.requester(method, connection.base_url + path, headers, body, context, self.timeout)
        except RuntimeRequestError as error:
            message = str(error).replace(connection.password, "[скрыто]").replace(credential, "[скрыто]")
            raise RuntimeRequestError(message[:500], status_code=error.status_code) from error
        if status in {401, 403}:
            raise RuntimeRequestError("Application Manager отклонил учётные данные", status_code=status)
        if status == 404:
            raise RuntimeRequestError("Ресурс Application Manager не найден", status_code=status)
        if status == 503:
            raise RuntimeRequestError("Журнал событий приложения сейчас недоступен", status_code=status)
        if status >= 400:
            raise RuntimeRequestError(f"Application Manager вернул HTTP {status}", status_code=status)
        return status, value


class RuntimeDiagnosticsService:
    def __init__(
        self,
        settings: ServerSettings,
        console: ConsoleService,
        *,
        configuration: RuntimeConfiguration | None = None,
        event_client: ApplicationManagerClient | None = None,
    ) -> None:
        self.configuration = configuration or RuntimeConfiguration(settings)
        self.console = console
        self.event_client = event_client or ApplicationManagerClient()

    def configure(self, **values: Any) -> dict[str, Any]:
        return self.configuration.configure(**values)

    def configuration_info(self) -> dict[str, Any]:
        return self.configuration.public_configuration()

    def health(self) -> dict[str, Any]:
        try:
            root, source = self.configuration.instance_root()
        except RuntimeConfigurationError as error:
            return {"status": "invalid", "message": str(error), "capabilities": self._capabilities(False)}
        if root is None:
            return {
                "status": "missing",
                "message": "Каталог экземпляра Element не настроен и не найден по стандартному пути",
                "capabilities": self._capabilities(False),
            }
        try:
            root = _validate_instance_root(root)
        except RuntimeConfigurationError as error:
            return {"status": "invalid", "message": str(error), "instance_root": str(root)}
        process = self.process_status()
        logs = self.list_logs()
        disk = self.disk_usage()
        manager_available = False
        try:
            manager = self.configuration.application_manager()
            event_status = "configured" if manager else "missing"
            event_connection = manager.public_info() if manager else None
            manager_available = manager is not None
        except RuntimeConfigurationError as error:
            event_status = "invalid"
            event_connection = None
            event_error = str(error)
        else:
            event_error = None
        overall = "ready" if process.get("running") else "degraded"
        return {
            "status": overall,
            "instance_root": str(root),
            "instance_root_source": source,
            "process": process,
            "disk": disk,
            "logs": {"status": logs["status"], "count": logs["count"]},
            "application_events": {
                "status": event_status,
                "connection": event_connection,
                **({"message": event_error} if event_error else {}),
            },
            "capabilities": self._capabilities(manager_available),
        }

    def process_status(self) -> dict[str, Any]:
        root = self._root()
        pid_file = root / "daemon.pid"
        if not pid_file.is_file():
            return {
                "status": "stopped",
                "running": False,
                "pid": None,
                "pid_file": "daemon.pid",
                "message": "Файл daemon.pid отсутствует",
            }
        try:
            raw = pid_file.read_text(encoding="ascii", errors="strict").strip()
            pid = int(raw)
            if pid <= 0:
                raise ValueError
        except (OSError, UnicodeError, ValueError):
            return {
                "status": "invalid_pid_file",
                "running": False,
                "pid": None,
                "pid_file": "daemon.pid",
            }
        running = _process_exists(pid)
        identity = _process_matches_instance(pid, root) if running else None
        return {
            "status": "running" if running else "stale_pid",
            "running": running,
            "pid": pid,
            "pid_file": "daemon.pid",
            "identity_matches_instance": identity,
            "identity_verified": identity is not None,
        }

    def disk_usage(self) -> dict[str, Any]:
        root = self._root()
        total, used, free = shutil.disk_usage(root)
        directories: dict[str, Any] = {}
        for name in ("logs", "dumps", "work"):
            path = root / name
            size, files, truncated = _bounded_directory_size(path)
            directories[name] = {"bytes": size, "files": files, "truncated": truncated, "exists": path.is_dir()}
        return {
            "status": "ready",
            "filesystem": {"total_bytes": total, "used_bytes": used, "free_bytes": free},
            "directories": directories,
        }

    def list_logs(self) -> dict[str, Any]:
        root = self._root()
        log_root = root / "logs"
        if not log_root.is_dir():
            return {"status": "missing", "count": 0, "logs": [], "message": "Каталог logs отсутствует"}
        logs: list[dict[str, Any]] = []
        for path in sorted(log_root.iterdir(), key=lambda item: item.name.casefold()):
            if len(logs) >= MAX_LOG_FILES:
                break
            if not _safe_log_file(path, log_root):
                continue
            try:
                stat = path.stat()
            except OSError:
                # Rotation may replace a file between enumeration and stat.
                continue
            logs.append(
                {
                    "log_id": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    "empty": stat.st_size == 0,
                }
            )
        return {
            "status": "ready",
            "count": len(logs),
            "truncated": len(logs) >= MAX_LOG_FILES,
            "logs": logs,
            "source": "trusted_instance_log_root",
        }

    def read_log(self, log_id: str, *, tail_lines: int = 200) -> dict[str, Any]:
        if not 1 <= tail_lines <= MAX_LOG_LINES:
            raise RuntimeConfigurationError(f"tail_lines должен быть в диапазоне 1..{MAX_LOG_LINES}")
        path = self._log_path(log_id)
        data, start = _read_tail(path, MAX_LOG_READ_BYTES)
        if b"\x00" in data:
            return {"status": "binary", "log_id": path.name, "lines": [], "message": "Файл похож на бинарный"}
        decoded = data.decode("utf-8", errors="replace")
        lines = decoded.splitlines()[-tail_lines:]
        redacted = [_sanitize_text(line, limit=MAX_EVENT_TEXT) for line in lines]
        return {
            "status": "ready",
            "log_id": path.name,
            "size_bytes": path.stat().st_size,
            "window_start_byte": start,
            "tail_lines": tail_lines,
            "count": len(redacted),
            "encoding": "utf-8" if "\ufffd" not in decoded else "utf-8-with-replacement",
            "lines": redacted,
            "redaction": _redaction_policy(),
        }

    def search_logs(
        self,
        query: str,
        *,
        log_ids: Sequence[str] | None = None,
        max_matches: int = 100,
    ) -> dict[str, Any]:
        needle = query.strip()
        if not needle:
            raise RuntimeConfigurationError("Укажите непустую строку поиска")
        if len(needle) > 300:
            raise RuntimeConfigurationError("Строка поиска длиннее 300 символов")
        if not 1 <= max_matches <= MAX_LOG_MATCHES:
            raise RuntimeConfigurationError(f"max_matches должен быть в диапазоне 1..{MAX_LOG_MATCHES}")
        selected = list(log_ids) if log_ids else [item["log_id"] for item in self.list_logs().get("logs", [])]
        if len(selected) > MAX_LOG_FILES:
            raise RuntimeConfigurationError(f"Можно искать не более чем в {MAX_LOG_FILES} логах")
        return self._search_log_identifiers([needle], selected, max_matches=max_matches)

    def search_application_events(
        self,
        *,
        application_id: str | None,
        start_instant: str,
        final_instant: str,
        size: int = 50,
        anchor_event_id: str | None = None,
        search_substring: str | None = None,
        operation_id: str | None = None,
        importance: Sequence[str] | None = None,
        kind: Sequence[str] | None = None,
        names: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        selected = self._application_id(application_id)
        start, final = _event_range(start_instant, final_instant)
        if not 1 <= size <= MAX_EVENT_SIZE:
            raise RuntimeConfigurationError(f"size должен быть в диапазоне 1..{MAX_EVENT_SIZE}")
        anchor = _uuid(anchor_event_id, "anchor_event_id") if anchor_event_id else None
        request = {
            "size": size,
            "anchorEventId": anchor,
            "searchSubstring": _optional_text(search_substring, "search_substring", 300),
            "operationId": _optional_text(operation_id, "operation_id", 300),
            "startInstant": _instant_text(start),
            "finalInstant": _instant_text(final),
            "importance": _enum_values(importance, {"CRITICAL", "MAJOR", "GENERAL", "MINOR"}, "importance"),
            "kind": _enum_values(
                kind,
                {"INFORMATION", "EVENT", "ERROR", "START_OPERATION", "END_OPERATION"},
                "kind",
            ),
            "names": _text_values(names, "names"),
        }
        try:
            manager = self._manager()
            raw_events, api_version = self.event_client.search_events(manager, selected, request)
        except RuntimeConfigurationError as error:
            return {"status": "missing", "resource": "application_events", "message": str(error)}
        except RuntimeRequestError as error:
            return _runtime_request_error(error, resource="application_events")
        events = [_event_payload(value) for value in raw_events[:size] if isinstance(value, Mapping)]
        return {
            "status": "ready",
            "application_id": selected,
            "api_version": api_version,
            "contract_element_version": EVENT_CONTRACT_VERSION,
            "time_range": {"start": _instant_text(start), "final": _instant_text(final)},
            "anchor_event_id": anchor,
            "count": len(events),
            "has_more": len(raw_events) >= size,
            "next_anchor_event_id": events[-1].get("id") if len(raw_events) >= size and events else None,
            "events": events,
            "source": "application_event_log",
            "redaction": _redaction_policy(),
        }

    def get_application_event(self, *, application_id: str | None, event_id: str) -> dict[str, Any]:
        selected = self._application_id(application_id)
        selected_event = _uuid(event_id, "event_id")
        try:
            manager = self._manager()
            value = self.event_client.get_event(manager, selected, selected_event)
        except RuntimeConfigurationError as error:
            return {"status": "missing", "resource": "application_event", "message": str(error)}
        except RuntimeRequestError as error:
            return _runtime_request_error(error, resource="application_event")
        if value in (None, ""):
            return {"status": "not_found", "application_id": selected, "event_id": selected_event}
        if not isinstance(value, Mapping):
            return {
                "status": "invalid_response",
                "application_id": selected,
                "message": "Application Manager вернул некорректное событие",
            }
        return {
            "status": "ready",
            "application_id": selected,
            "event": _event_payload(value),
            "source": "application_event_log",
            "contract_element_version": EVENT_CONTRACT_VERSION,
            "redaction": _redaction_policy(),
        }

    def trace_operation(
        self,
        *,
        task_id: str | None = None,
        task_type: Literal["application", "deployment_instance", "group"] = "application",
        application_id: str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        operation_id: str | None = None,
        start_instant: str | None = None,
        final_instant: str | None = None,
        max_matches: int = 100,
    ) -> dict[str, Any]:
        if not 1 <= max_matches <= MAX_LOG_MATCHES:
            raise RuntimeConfigurationError(f"max_matches должен быть в диапазоне 1..{MAX_LOG_MATCHES}")
        identifiers = {
            key: _optional_text(value, key, 300)
            for key, value in {
                "task_id": task_id,
                "application_id": application_id,
                "trace_id": trace_id,
                "request_id": request_id,
                "operation_id": operation_id,
            }.items()
            if value is not None
        }
        identifiers = {key: value for key, value in identifiers.items() if value}
        if "application_id" in identifiers:
            identifiers["application_id"] = _uuid(identifiers["application_id"], "application_id")
        if not identifiers:
            raise RuntimeConfigurationError("Укажите хотя бы один точный идентификатор операции")
        task_result: dict[str, Any] | None = None
        selected_application = application_id
        if task_id:
            task_result = self.console.get_task(task_type, _uuid(task_id, "task_id"))
            task = task_result.get("task") if task_result.get("status") == "ready" else None
            if isinstance(task, Mapping) and not selected_application and isinstance(task.get("application_id"), str):
                selected_application = task["application_id"]
            if isinstance(task, Mapping):
                start_instant = start_instant or task.get("start_date")
                final_instant = final_instant or task.get("end_date")
        exact_values = [value for value in identifiers.values() if value]
        gaps: list[str] = []
        try:
            logs = self._search_log_identifiers(exact_values, None, max_matches=max_matches)
        except RuntimeConfigurationError as error:
            logs = {"status": "missing", "count": 0, "matches": [], "message": str(error)}
            gaps.append("Локальные server logs недоступны")
        event_result: dict[str, Any] | None = None
        if operation_id and selected_application and start_instant:
            final_value = final_instant or _instant_text(datetime.now(UTC))
            try:
                event_result = self.search_application_events(
                    application_id=selected_application,
                    start_instant=start_instant,
                    final_instant=final_value,
                    operation_id=operation_id,
                    size=min(MAX_EVENT_SIZE, max_matches),
                )
                if event_result.get("status") != "ready":
                    gaps.append("Application Event Log не вернул готовый результат")
            except RuntimeConfigurationError as error:
                event_result = {"status": "missing", "count": 0, "events": [], "message": str(error)}
                gaps.append("Application Event Log недоступен")
        elif operation_id:
            gaps.append("Для журнала событий нужны application_id и start_instant; final_instant можно опустить")
        return {
            "status": "ready" if not gaps else "partial",
            "correlation": identifiers,
            "matching_policy": "exact_identifier_only",
            "task": task_result,
            "server_logs": logs,
            "application_events": event_result,
            "gaps": gaps,
            "sources": [
                {
                    "kind": "console_task",
                    "available": task_result is not None and task_result.get("status") == "ready",
                },
                {"kind": "server_log", "available": logs.get("status") == "ready"},
                {
                    "kind": "application_event_log",
                    "available": event_result is not None and event_result.get("status") == "ready",
                },
            ],
        }

    def _search_log_identifiers(
        self,
        identifiers: Sequence[str],
        log_ids: Sequence[str] | None,
        *,
        max_matches: int,
    ) -> dict[str, Any]:
        selected = (
            list(dict.fromkeys(log_ids))
            if log_ids is not None
            else [item["log_id"] for item in self.list_logs().get("logs", [])]
        )
        if len(selected) > MAX_LOG_FILES:
            raise RuntimeConfigurationError(f"Можно искать не более чем в {MAX_LOG_FILES} логах")
        needles = [(item, item.casefold()) for item in identifiers]
        matches: list[dict[str, Any]] = []
        searched: list[dict[str, Any]] = []
        for log_id in selected:
            path = self._log_path(log_id)
            data, start = _read_tail(path, MAX_LOG_SEARCH_BYTES)
            if b"\x00" in data:
                searched.append({"log_id": path.name, "status": "binary", "window_start_byte": start})
                continue
            decoded = data.decode("utf-8", errors="replace")
            for line_number, line in enumerate(decoded.splitlines(), start=1):
                folded = line.casefold()
                found = [original for original, needle in needles if needle in folded]
                if not found:
                    continue
                matches.append(
                    {
                        "log_id": path.name,
                        "line_in_window": line_number,
                        "window_start_byte": start,
                        "matched_terms": len(found),
                        "text": _sanitize_text(line, limit=MAX_EVENT_TEXT),
                    }
                )
                if len(matches) >= max_matches:
                    break
            searched.append(
                {
                    "log_id": path.name,
                    "status": "ready",
                    "window_start_byte": start,
                    "replacement_characters": decoded.count("\ufffd"),
                }
            )
            if len(matches) >= max_matches:
                break
        return {
            "status": "ready",
            "count": len(matches),
            "truncated": len(matches) >= max_matches,
            "searched_logs": searched,
            "matches": matches,
            "source": "trusted_instance_log_root",
            "redaction": _redaction_policy(),
        }

    def _application_id(self, explicit: str | None) -> str:
        if explicit:
            return _uuid(explicit, "application_id")
        try:
            connection = self.console.resolver.resolve()
        except ConsoleConfigurationError as error:
            raise RuntimeConfigurationError(
                "Укажите application_id; активный контекст Element IDE недоступен"
            ) from error
        if connection.source != "ide_session" or not connection.application_id:
            raise RuntimeConfigurationError(
                "Укажите application_id. Без него журнал текущего приложения доступен только из Element IDE"
            )
        return _uuid(connection.application_id, "application_id")

    def _manager(self) -> ApplicationManagerConnection:
        try:
            connection = self.configuration.application_manager()
        except RuntimeConfigurationError:
            raise
        if connection is None:
            raise RuntimeConfigurationError(
                "Application Manager не настроен. Задайте подключение в локальном UI или через "
                "ELEMENT_APPLICATION_MANAGER_*"
            )
        return connection

    def _root(self) -> Path:
        root, _ = self.configuration.instance_root()
        if root is None:
            raise RuntimeConfigurationError(
                "Каталог экземпляра Element не настроен. Укажите его в локальном UI или ELEMENT_INSTANCE_ROOT"
            )
        return _validate_instance_root(root)

    def _log_path(self, log_id: str) -> Path:
        if not isinstance(log_id, str) or not _LOG_NAME.fullmatch(log_id) or Path(log_id).name != log_id:
            raise RuntimeConfigurationError("Укажите log_id, возвращённый list_server_logs")
        log_root = self._root() / "logs"
        path = (log_root / log_id).resolve()
        if not _safe_log_file(path, log_root):
            raise RuntimeConfigurationError("Лог не найден или не является разрешённым обычным файлом")
        return path

    @staticmethod
    def _capabilities(events: bool) -> dict[str, Any]:
        return {
            "runtime_health": True,
            "process_status": True,
            "disk_usage": True,
            "server_logs": True,
            "application_events": events,
            "trace_operation": True,
            "read_only": True,
        }


def _application_manager_from_values(values: Mapping[str, Any], *, source: str) -> ApplicationManagerConnection:
    server = values.get("server")
    username = values.get("username")
    password = values.get("password")
    protected = values.get("password_dpapi")
    if not isinstance(password, str) and isinstance(protected, str):
        try:
            password = _unprotect_windows_secret(protected)
        except ConsoleConfigurationError as error:
            raise RuntimeConfigurationError(str(error)) from error
    if (
        not isinstance(server, str)
        or not isinstance(username, str)
        or not username.strip()
        or not isinstance(password, str)
        or not password
    ):
        raise RuntimeConfigurationError("Подключение Application Manager настроено не полностью")
    api_version = values.get("api_version", "auto")
    if api_version not in {"auto", "v1", "v2"}:
        raise RuntimeConfigurationError("Некорректная версия Application Manager API")
    verify_tls = _boolean(values.get("verify_tls", True), "verify_tls")
    ca_value = values.get("ca_bundle")
    ca_bundle = Path(ca_value).expanduser().resolve() if isinstance(ca_value, str) and ca_value.strip() else None
    if ca_bundle and not ca_bundle.is_file():
        raise RuntimeConfigurationError(f"CA bundle не найден: {ca_bundle}")
    return ApplicationManagerConnection(
        base_url=_normalize_manager_url(server),
        username=username.strip(),
        password=password,
        api_version=api_version,
        verify_tls=verify_tls,
        ca_bundle=ca_bundle,
        source=source,
    )


def _normalize_manager_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeConfigurationError("Адрес Application Manager должен быть полным HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeConfigurationError("Адрес Application Manager не должен содержать credentials, query или fragment")
    path = re.sub(r"/manager/api/v[12]$", "", parsed.path.rstrip("/"))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _validate_instance_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    required = (resolved / "config" / "server.yml", resolved / "config" / "logging.yml")
    if not resolved.is_dir() or not all(item.is_file() for item in required):
        raise RuntimeConfigurationError(
            "Каталог не похож на instance root Element: нужны config/server.yml и config/logging.yml"
        )
    return resolved


def _standard_instance_roots() -> list[Path]:
    if os.name == "nt":
        program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        return [program_data / "1C" / "1CE" / "instances" / "1c-enterprise-element-server-with-ide"]
    return [Path("/var/opt/1C/1CE/instances/1c-enterprise-element-server-with-ide")]


def _safe_log_file(path: Path, log_root: Path) -> bool:
    try:
        return (
            path.parent.resolve() == log_root.resolve()
            and _LOG_NAME.fullmatch(path.name) is not None
            and path.is_file()
            and not path.is_symlink()
        )
    except OSError:
        return False


def _read_tail(path: Path, limit: int) -> tuple[bytes, int]:
    size = path.stat().st_size
    start = max(0, size - limit)
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read(limit)
    if start and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    return data, start


def _bounded_directory_size(path: Path, *, max_files: int = 10_000) -> tuple[int, int, bool]:
    if not path.is_dir():
        return 0, 0, False
    total = 0
    files = 0
    for root, _, names in os.walk(path, followlinks=False):
        for name in names:
            candidate = Path(root) / name
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                total += candidate.stat().st_size
                files += 1
            except OSError:
                continue
            if files >= max_files:
                return total, files, True
    return total, files, False


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_matches_instance(pid: int, root: Path) -> bool | None:
    if os.name == "nt":
        return None
    command = Path(f"/proc/{pid}/cmdline")
    if not command.is_file():
        return None
    try:
        value = command.read_bytes()[: 64 * 1024].replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except OSError:
        return None
    return str(root) in value or "element-server" in value or "com.e1c.chassis.app.service" in value


def _event_range(start_value: str, final_value: str) -> tuple[datetime, datetime]:
    start = _instant(start_value, "start_instant")
    final = _instant(final_value, "final_instant")
    if final <= start:
        raise RuntimeConfigurationError("final_instant должен быть позже start_instant")
    if final - start > MAX_EVENT_RANGE:
        raise RuntimeConfigurationError("Интервал журнала событий не должен превышать 31 день")
    return start, final


def _instant(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigurationError(f"Укажите {name} в ISO 8601 с часовым поясом")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeConfigurationError(f"{name} должен быть датой ISO 8601") from error
    if parsed.tzinfo is None:
        raise RuntimeConfigurationError(f"{name} должен содержать часовой пояс")
    return parsed.astimezone(UTC)


def _instant_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _uuid(value: str, name: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as error:
        raise RuntimeConfigurationError(f"{name} должен быть UUID") from error


def _optional_text(value: str | None, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeConfigurationError(f"{name} должен быть строкой")
    result = value.strip()
    if len(result) > limit:
        raise RuntimeConfigurationError(f"{name} длиннее {limit} символов")
    return result or None


def _enum_values(values: Sequence[str] | None, allowed: set[str], name: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raise RuntimeConfigurationError(f"{name} должен быть списком")
    if len(values) > len(allowed):
        raise RuntimeConfigurationError(f"Слишком много значений {name}")
    result = [str(value).strip().upper() for value in values]
    if any(value not in allowed for value in result):
        raise RuntimeConfigurationError(f"Некорректное значение {name}")
    return list(dict.fromkeys(result))


def _text_values(values: Sequence[str] | None, name: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raise RuntimeConfigurationError(f"{name} должен быть списком")
    if len(values) > 50:
        raise RuntimeConfigurationError(f"Слишком много значений {name}")
    result: list[str] = []
    for value in values:
        item = _optional_text(value, name, 300)
        if item:
            result.append(item)
    return list(dict.fromkeys(result))


def _v1_event_parameters(request: Mapping[str, Any]) -> dict[str, str]:
    result = {
        "size": str(request["size"]),
        "startInstant": str(request["startInstant"]),
        "finalInstant": str(request["finalInstant"]),
    }
    for key in ("anchorEventId", "searchSubstring", "operationId"):
        if request.get(key):
            result[key] = str(request[key])
    for key in ("importance", "kind", "names"):
        values = request.get(key)
        if isinstance(values, Sequence) and not isinstance(values, str) and values:
            result[key] = ",".join(str(value) for value in values)
    return result


def _event_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    properties = value.get("properties")
    descriptions = value.get("propertiesDescriptions", value.get("properties-descriptions"))
    safe_properties: dict[str, Any] = {}
    redacted_count = 0
    if isinstance(properties, Mapping):
        for index, (raw_key, raw_value) in enumerate(properties.items()):
            if index >= MAX_EVENT_PROPERTIES:
                break
            key = str(raw_key)[:300]
            if _SENSITIVE_KEY.search(key):
                safe_properties[key] = "[СКРЫТО]"
                redacted_count += 1
            elif key.replace("-", "").replace("_", "").casefold() in {"userid", "username", "user"}:
                safe_properties[key] = _pseudonym(str(raw_value))
                redacted_count += 1
            else:
                safe_properties[key] = _sanitize_value(raw_value)
    safe_descriptions: dict[str, str] = {}
    if isinstance(descriptions, Mapping):
        for index, (raw_key, raw_value) in enumerate(descriptions.items()):
            if index >= MAX_EVENT_PROPERTIES:
                break
            safe_descriptions[str(raw_key)[:300]] = _sanitize_text(str(raw_value), limit=1000)
    return {
        "id": value.get("id"),
        "name": _sanitize_text(str(value.get("name", "")), limit=1000),
        "description": _sanitize_optional(value.get("description")),
        "presentation": _sanitize_optional(value.get("presentation")),
        "importance": value.get("importance"),
        "date": value.get("date"),
        "type": value.get("type", value.get("eventType")),
        "properties": safe_properties,
        "properties_descriptions": safe_descriptions,
        "properties_truncated": isinstance(properties, Mapping) and len(properties) > MAX_EVENT_PROPERTIES,
        "redacted_properties": redacted_count,
    }


def _sanitize_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _sanitize_text(value, limit=MAX_EVENT_TEXT)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= 50:
                break
            text_key = str(key)[:300]
            result[text_key] = "[СКРЫТО]" if _SENSITIVE_KEY.search(text_key) else _sanitize_value(child)
        return result
    if isinstance(value, Sequence):
        return [_sanitize_value(item) for item in list(value)[:50]]
    return _sanitize_text(str(value), limit=MAX_EVENT_TEXT)


def _sanitize_optional(value: Any) -> str | None:
    return _sanitize_text(str(value), limit=MAX_EVENT_TEXT) if value is not None else None


def _sanitize_text(value: str, *, limit: int) -> str:
    result = _BEARER.sub(lambda match: f"{match.group(1)} [СКРЫТО]", value)
    result = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[СКРЫТО]", result)
    result = _EMAIL.sub("[EMAIL]", result)
    result = _USER_ID.sub(lambda match: f"{match.group(1)}{_pseudonym(match.group(2))}", result)
    return result[:limit]


def _pseudonym(value: str) -> str:
    return f"[USER:{hashlib.sha256(value.encode()).hexdigest()[:10]}]"


def _redaction_policy() -> dict[str, Any]:
    return {
        "credentials": "removed",
        "email": "removed",
        "user_id": "pseudonymized",
        "event_properties": "sensitive-key redaction and bounded values",
        "stack_traces": f"bounded to {MAX_EVENT_TEXT} characters per value",
    }


def _runtime_request_error(error: RuntimeRequestError, *, resource: str) -> dict[str, Any]:
    if error.status_code == 401:
        status = "unauthenticated"
    elif error.status_code == 403:
        status = "forbidden"
    elif error.status_code == 404:
        status = "not_found"
    elif error.status_code == 503:
        status = "unavailable"
    else:
        status = "error"
    return {"status": status, "resource": resource, "http_status": error.status_code, "message": str(error)[:500]}


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise RuntimeConfigurationError(f"{name} должен быть логическим")


def _ssl_context(verify_tls: bool, ca_bundle: Path | None) -> ssl.SSLContext | None:
    if not verify_tls:
        return ssl._create_unverified_context()  # noqa: S323
    if ca_bundle:
        return ssl.create_default_context(cafile=str(ca_bundle))
    return None


def _urlopen_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    context: ssl.SSLContext | None,
    timeout: float,
) -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method, headers=dict(headers), data=body)
    try:
        with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
            status = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeRequestError(f"Не удалось обратиться к Application Manager: {error}") from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeRequestError("Ответ Application Manager превышает допустимый размер", status_code=status)
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeRequestError("Application Manager вернул некорректный JSON", status_code=status) from error
