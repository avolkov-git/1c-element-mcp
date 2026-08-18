from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from element_mcp.config import ConfigurationStore, ServerSettings, discover_project_path

MAX_SETTINGS_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
CONSOLE_API_VERSION = "v2"
CONSOLE_CONTRACT_VERSION = "9.2.4-6"
MAX_EXTERNAL_TEXT = 2000
MAX_LIST_LIMIT = 100
RETRYABLE_GET_STATUSES = {429, 502, 503, 504}


class ConsoleConfigurationError(ValueError):
    pass


class ConsoleRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ConsoleConnection:
    base_url: str
    source: str
    auth_kind: Literal["access_token", "client_credentials"]
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    project_id: str | None = None
    application_id: str | None = None
    space_id: str | None = None
    verify_tls: bool = True
    ca_bundle: Path | None = None

    def public_info(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "source": self.source,
            "auth_kind": self.auth_kind,
            "client_id_present": bool(self.client_id),
            "project_id": self.project_id,
            "application_id": self.application_id,
            "space_id": self.space_id,
            "verify_tls": self.verify_tls,
            "ca_bundle": str(self.ca_bundle) if self.ca_bundle else None,
        }


Requester = Callable[[str, str, Mapping[str, str], bytes | None, ssl.SSLContext | None, float], Any]


class ConsoleContextResolver:
    def __init__(
        self,
        settings: ServerSettings,
        *,
        environ: Mapping[str, str] | None = None,
        session_store: ConsoleSessionStore | None = None,
    ) -> None:
        self.settings = settings
        self.environ = environ if environ is not None else os.environ
        self.config_store = ConfigurationStore(settings.resolved_config_path)
        self.session_store = session_store

    def resolve(self) -> ConsoleConnection:
        problems: list[str] = []
        candidates = self._candidates()
        for source, values in candidates:
            try:
                return _connection_from_values(values, source=source)
            except ConsoleConfigurationError as error:
                problems.append(str(error))
        if problems:
            raise ConsoleConfigurationError("; ".join(problems))
        raise ConsoleConfigurationError(
            "Подключение к Панели управления не настроено. Задайте ELEMENT_CONSOLE_URL и токен или "
            "Client-Id/Client-Secret, либо укажите --ide-settings-path/--console-config-path."
        )

    def _candidates(self) -> list[tuple[str, dict[str, Any]]]:
        candidates: list[tuple[str, dict[str, Any]]] = []
        if self.session_store:
            session_values = self.session_store.get()
            if session_values:
                candidates.append(("ide_session", session_values))
        environment_values = _environment_values(self.environ)
        if environment_values:
            candidates.append(("environment", environment_values))

        ide_settings = self.settings.resolved_ide_settings_path
        if ide_settings:
            candidates.append(("ide_settings", _read_settings_file(ide_settings)))

        console_config = self.settings.resolved_console_config_path
        if console_config.is_file() and console_config != ide_settings:
            console_values = _read_settings_file(console_config)
            if _parse_boolean(console_values.get("enabled", True), name="enabled"):
                candidates.append(("console_config", console_values))

        if self.settings.transport == "stdio" and ide_settings is None:
            for path in self._workspace_settings_paths():
                if path == console_config:
                    continue
                try:
                    candidates.append(("workspace_settings", _read_settings_file(path)))
                except ConsoleConfigurationError:
                    continue
        return candidates

    def _workspace_settings_paths(self) -> list[Path]:
        roots: list[Path] = []
        project = discover_project_path(self.settings.project_path, config_store=self.config_store)
        if project:
            roots.append(project)
        cwd = Path.cwd().resolve()
        if cwd not in roots:
            roots.append(cwd)

        candidates: list[Path] = []
        for root in roots:
            for relative in (
                ".vscode/settings.json",
                ".theia/settings.json",
                ".settings/settings.json",
            ):
                path = root / relative
                if path.is_file() and path not in candidates:
                    candidates.append(path)
            for path in sorted(root.glob("*.code-workspace"))[:5]:
                if path.is_file() and path not in candidates:
                    candidates.append(path)
        return candidates


class ConsoleSessionStore:
    """Keep an IDE-provided Console connection in memory, never in the corpus or MCP config."""

    def __init__(self) -> None:
        self._values: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._values) if self._values else None

    def set(self, values: Mapping[str, Any]) -> None:
        with self._lock:
            self._values = dict(values)

    def clear(self) -> None:
        with self._lock:
            self._values = None


class ConsoleHttpClient:
    def __init__(
        self,
        *,
        requester: Requester | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retry_delays: tuple[float, ...] = (0.1, 0.3),
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.requester = requester or _urlopen_json
        self.timeout = timeout
        self.retry_delays = retry_delays
        self.sleeper = sleeper
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, connection: ConsoleConnection, path: str) -> Any:
        for attempt in range(len(self.retry_delays) + 1):
            try:
                return self._authorized_request(connection, "GET", path)
            except ConsoleRequestError as error:
                retryable = error.status_code is None or error.status_code in RETRYABLE_GET_STATUSES
                if not retryable or attempt >= len(self.retry_delays):
                    raise
                self.sleeper(self.retry_delays[attempt])
        raise AssertionError("unreachable")

    def clear_tokens(self) -> None:
        with self._lock:
            self._tokens.clear()

    def _authorized_request(
        self,
        connection: ConsoleConnection,
        method: str,
        path: str,
        *,
        retry_auth: bool = True,
    ) -> Any:
        token = self._access_token(connection)
        try:
            return self._request(
                connection,
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
            )
        except ConsoleRequestError as error:
            if error.status_code == 401 and retry_auth and connection.auth_kind == "client_credentials":
                with self._lock:
                    self._tokens.pop(_connection_fingerprint(connection), None)
                return self._authorized_request(connection, method, path, retry_auth=False)
            raise

    def _access_token(self, connection: ConsoleConnection) -> str:
        if connection.auth_kind == "access_token":
            if not connection.access_token:
                raise ConsoleConfigurationError("В выбранном источнике отсутствует access token")
            return connection.access_token

        fingerprint = _connection_fingerprint(connection)
        with self._lock:
            cached = self._tokens.get(fingerprint)
        if cached:
            return cached

        if not connection.client_id or not connection.client_secret:
            raise ConsoleConfigurationError("Для client credentials нужны Client-Id и Client-Secret")
        basic = base64.b64encode(f"{connection.client_id}:{connection.client_secret}".encode()).decode("ascii")
        payload = urllib.parse.urlencode({"grant_type": "CLIENT_CREDENTIALS"}).encode("ascii")
        response = self._request(
            connection,
            "POST",
            "/sys/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=payload,
        )
        if not isinstance(response, dict):
            raise ConsoleRequestError("Панель управления вернула некорректный ответ token endpoint")
        token = response.get("id_token") or response.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise ConsoleRequestError("В ответе token endpoint отсутствует id_token/access_token")
        with self._lock:
            self._tokens[fingerprint] = token
        return token

    def _request(
        self,
        connection: ConsoleConnection,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError("Console API path must be absolute")
        context = _ssl_context(connection)
        request_headers = {"Accept": "application/json", **headers}
        try:
            return self.requester(
                method,
                f"{connection.base_url}{path}",
                request_headers,
                body,
                context,
                self.timeout,
            )
        except ConsoleRequestError as error:
            safe_message = _redact_connection_error(str(error), connection, request_headers)
            raise ConsoleRequestError(safe_message, status_code=error.status_code) from error


class ConsoleService:
    def __init__(
        self,
        settings: ServerSettings,
        *,
        resolver: ConsoleContextResolver | None = None,
        client: ConsoleHttpClient | None = None,
    ) -> None:
        self.session_store = resolver.session_store if resolver and resolver.session_store else ConsoleSessionStore()
        self.resolver = resolver or ConsoleContextResolver(settings, session_store=self.session_store)
        if self.resolver.session_store is None:
            self.resolver.session_store = self.session_store
        self.client = client or ConsoleHttpClient()

    def configure_ide_session(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and activate an IDE handoff without returning or persisting credentials."""
        allowed = {
            "server",
            "client_id",
            "client_secret",
            "access_token",
            "project_id",
            "application_id",
            "space_id",
            "verify_tls",
            "ca_bundle",
        }
        sanitized = {key: value for key, value in values.items() if key in allowed}
        connection = _connection_from_values(sanitized, source="ide_session")
        spaces = _as_items(self.client.get(connection, "/api/v2/spaces"), resource="пространств")
        self.session_store.set(sanitized)
        return {
            "status": "ready",
            "connection": connection.public_info(),
            "spaces_count": len(spaces),
            "message": "Контекст текущей IDE подключён к MCP до перезапуска сервера",
        }

    def clear_ide_session(self) -> dict[str, Any]:
        self.session_store.clear()
        return {"status": "cleared", "message": "Временный контекст IDE отключён"}

    def persistent_configuration(self) -> dict[str, Any]:
        """Return editable standalone settings without decrypting or exposing a credential."""
        path = self.resolver.settings.resolved_console_config_path
        if not path.is_file():
            return {
                "status": "missing",
                "configured": False,
                "enabled": False,
                "server": None,
                "client_id": None,
                "credential_kind": None,
                "secret_present": False,
                "secret_storage": None,
                "message": "Удалённый сервер Element не настроен",
            }
        try:
            values = _read_settings_file(path)
            enabled = _parse_boolean(values.get("enabled", True), name="enabled")
            raw_server = _first_string(values, "server", "console_url", "base_url")
            server = _normalize_base_url(raw_server) if raw_server else None
            client_id = _first_string(values, "client_id", "client-id")
        except ConsoleConfigurationError as error:
            return {
                "status": "invalid",
                "configured": False,
                "enabled": False,
                "server": None,
                "client_id": None,
                "credential_kind": None,
                "secret_present": False,
                "secret_storage": None,
                "message": str(error),
            }
        secret_storage = None
        if _first_string(values, "client_secret_dpapi", "client-secret-dpapi"):
            secret_storage = "windows_dpapi"
        elif _first_string(values, "client_secret", "client-secret"):
            secret_storage = "restricted_file"
        access_token_present = bool(_first_string(values, "access_token", "id_token", "token"))
        credential_kind = "access_token" if access_token_present else "client_credentials"
        configured = bool(server and (access_token_present or (client_id and secret_storage)))
        return {
            "status": "enabled" if enabled else "disabled",
            "configured": configured,
            "enabled": enabled,
            "server": server,
            "client_id": client_id,
            "credential_kind": credential_kind if configured else None,
            "secret_present": secret_storage is not None,
            "secret_storage": secret_storage,
            "message": (
                "Удалённый сервер Element включён"
                if enabled
                else "Удалённый сервер Element отключён; настройки сохранены"
            ),
        }

    def configure_persistent_connection(
        self,
        *,
        server: str,
        client_id: str,
        client_secret: str | None,
    ) -> dict[str, Any]:
        """Validate client credentials and persist them outside the main MCP configuration."""
        normalized_server = _normalize_base_url(server)
        normalized_client_id = client_id.strip()
        if not normalized_client_id:
            raise ConsoleConfigurationError("Укажите Client ID")

        path = self.resolver.settings.resolved_console_config_path
        supplied_secret = client_secret.strip() if isinstance(client_secret, str) else ""
        try:
            existing = _read_settings_file(path) if path.is_file() else {}
        except ConsoleConfigurationError:
            if not supplied_secret:
                raise
            existing = {}
        candidate = {
            "server": normalized_server,
            "client_id": normalized_client_id,
            "verify_tls": existing.get("verify_tls", existing.get("verify-tls", True)),
        }
        for key in ("ca_bundle", "ca-bundle", "project_id", "project-id", "space_id", "space-id"):
            if key in existing:
                candidate[key] = existing[key]

        if supplied_secret:
            candidate["client_secret"] = supplied_secret
        else:
            existing_server = _first_string(existing, "server", "console_url", "base_url")
            existing_client_id = _first_string(existing, "client_id", "client-id")
            same_identity = (
                bool(existing_server)
                and _normalize_base_url(existing_server) == normalized_server
                and existing_client_id == normalized_client_id
            )
            if not same_identity:
                raise ConsoleConfigurationError("Введите Client Secret для нового сервера или Client ID")
            for key in ("client_secret_dpapi", "client-secret-dpapi", "client_secret", "client-secret"):
                if key in existing:
                    candidate[key] = existing[key]
                    break

        connection = _connection_from_values(candidate, source="console_config")
        self.client.clear_tokens()
        spaces = _as_items(self.client.get(connection, "/api/v2/spaces"), resource="пространств")

        stored = {
            key: value
            for key, value in candidate.items()
            if key not in {"client_secret", "client-secret", "client_secret_dpapi", "client-secret-dpapi"}
        }
        stored["enabled"] = True
        if supplied_secret:
            secret_key, protected_secret = _protect_secret_for_storage(supplied_secret)
            stored[secret_key] = protected_secret
        else:
            for key in ("client_secret_dpapi", "client-secret-dpapi", "client_secret", "client-secret"):
                if key in candidate:
                    stored[key] = candidate[key]
                    break
        _write_console_settings(path, stored)
        return {
            **self.persistent_configuration(),
            "status": "ready",
            "spaces_count": len(spaces),
            "message": "Подключение проверено и включено",
        }

    def disable_persistent_connection(self) -> dict[str, Any]:
        path = self.resolver.settings.resolved_console_config_path
        if not path.is_file():
            return self.persistent_configuration()
        values = _read_settings_file(path)
        values["enabled"] = False
        _write_console_settings(path, values)
        self.client.clear_tokens()
        return self.persistent_configuration()

    def status(self) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            spaces = _as_items(self.client.get(connection, "/api/v2/spaces"), resource="пространств")
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "spaces_count": len(spaces),
                "message": "Подключение к Панели управления работает",
            }
        except ConsoleConfigurationError as error:
            return {"status": "missing", "message": str(error)}
        except ConsoleRequestError as error:
            return _request_error_payload(error)

    def server_info(self) -> dict[str, Any]:
        """Probe the documented health route and authenticated v2 API without guessing a product version."""
        try:
            connection = self.resolver.resolve()
            health = self.client.get(connection, "/api/v1/status/")
            if health not in (None, {}, ""):
                raise ConsoleRequestError("Панель управления вернула неожиданный ответ status endpoint")
            spaces = _as_items(self.client.get(connection, "/api/v2/spaces"), resource="пространств")
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "health": "ready",
                "api_version": CONSOLE_API_VERSION,
                "contract_element_version": CONSOLE_CONTRACT_VERSION,
                "server_product_version": None,
                "compatibility": "api_compatible_product_version_unverified",
                "spaces_count": len(spaces),
                "capabilities": _console_capabilities(),
                "message": (
                    "Console API v2 доступен. Документированный API не сообщает версию продукта сервера; "
                    f"контракт проверен для Element {CONSOLE_CONTRACT_VERSION}."
                ),
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return {"status": "missing", "message": str(error), **_external_metadata()}
        except ConsoleRequestError as error:
            return {**_request_error_payload(error), **_external_metadata()}

    def list_spaces(self) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            items = _as_items(self.client.get(connection, "/api/v2/spaces"), resource="пространств")
            spaces = [_space_payload(item) for item in items if isinstance(item, dict)]
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "count": len(spaces),
                "spaces": spaces,
            }
        except ConsoleConfigurationError as error:
            return {"status": "missing", "message": str(error), "count": 0, "spaces": []}
        except ConsoleRequestError as error:
            return {**_request_error_payload(error), "count": 0, "spaces": []}

    def get_project(self, project_id: str | None = None) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            selected = project_id or connection.project_id
            if not selected:
                return {
                    "status": "selection_required",
                    "message": "Не указан project_id и в окружении IDE нет выбранного проекта",
                }
            selected = _validate_uuid(selected, "project_id")
            project = self._project(connection, selected)
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "project": project,
            }
        except ConsoleConfigurationError as error:
            return {"status": "missing", "message": str(error)}
        except ConsoleRequestError as error:
            return _request_error_payload(error)

    def get_current_application(self) -> dict[str, Any]:
        """Read the published application attached to the active Element IDE session."""
        try:
            connection = self.resolver.resolve()
            if connection.source != "ide_session":
                return {
                    "status": "not_available",
                    "message": (
                        "Текущее приложение определяется только из активной Element IDE-сессии. "
                        "Обычный VS Code не выбирает приложение автоматически."
                    ),
                }
            if not connection.application_id:
                return {
                    "status": "missing",
                    "message": "Element IDE не передала 1C.applicationId для текущего опубликованного приложения",
                }
            application_id = _validate_uuid(connection.application_id, "application_id")
            application = self._application(connection, application_id)
            application_project_id = _application_project_id(application)
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "application": application,
                "ide_project_id": connection.project_id,
                "application_project_id": application_project_id,
                "matches_ide_project": (
                    _same_uuid(application_project_id, connection.project_id)
                    if application_project_id and connection.project_id
                    else None
                ),
                "message": "Получена карточка опубликованного приложения, связанного с текущей Element IDE",
            }
        except ConsoleConfigurationError as error:
            return {"status": "not_available", "message": str(error)}
        except ConsoleRequestError as error:
            return _request_error_payload(error)

    def list_space_applications(
        self,
        *,
        space_id: str | None = None,
        query: str | None = None,
        status: str | None = None,
        project_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            offset, limit = _validate_page(offset, limit)
            connection = self.resolver.resolve()
            selected_space, selection = self._select_space(connection, space_id)
            if selection is not None:
                return {**selection, "applications": [], **_external_metadata()}
            assert selected_space is not None
            items = _as_items(
                self.client.get(connection, f"/api/v2/spaces/{selected_space}/applications"),
                resource="приложений пространства",
            )
            applications = [_application_payload(item) for item in items if isinstance(item, Mapping)]
            normalized_query = query.strip().casefold() if query else None
            normalized_status = status.strip().casefold() if status else None
            normalized_project = _validate_uuid(project_id, "project_id") if project_id else None
            filtered = [
                application
                for application in applications
                if _application_matches(
                    application,
                    query=normalized_query,
                    status=normalized_status,
                    project_id=normalized_project,
                )
            ]
            page = filtered[offset : offset + limit]
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "space_id": selected_space,
                "count": len(page),
                "total": len(filtered),
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(page) < len(filtered),
                "applications": page,
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return _empty_list_result("applications", "missing", str(error))
        except ConsoleRequestError as error:
            return {
                **_empty_list_result("applications", _request_status(error.status_code), str(error)),
                "http_status": error.status_code,
            }

    def get_application(self, application_id: str | None = None) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            selected, selection = self._select_application(connection, application_id)
            if selection is not None:
                return selection
            assert selected is not None
            application = self._application(connection, selected)
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "selection_source": "explicit" if application_id else "ide_session",
                "application": application,
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return {"status": "missing", "message": str(error), **_external_metadata()}
        except ConsoleRequestError as error:
            return {**_request_error_payload(error), **_external_metadata()}

    def get_application_status(self, application_id: str | None = None) -> dict[str, Any]:
        return self._application_subresource(
            application_id,
            "status",
            "application_status",
            _application_status_payload,
        )

    def get_application_technology(self, application_id: str | None = None) -> dict[str, Any]:
        return self._application_subresource(
            application_id,
            "technology",
            "technology",
            _application_technology_payload,
        )

    def get_application_project(self, application_id: str | None = None) -> dict[str, Any]:
        return self._application_subresource(
            application_id,
            "project",
            "project",
            _application_project_payload,
        )

    def list_application_endpoints(self, application_id: str | None = None) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            selected, selection = self._select_application(connection, application_id)
            if selection is not None:
                return {**selection, "count": 0, "endpoints": []}
            assert selected is not None
            items = _as_items(
                self.client.get(connection, f"/api/v2/applications/{selected}/endpoints"),
                resource="endpoint приложения",
            )
            endpoints = [_endpoint_payload(item) for item in items if isinstance(item, Mapping)]
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "application_id": selected,
                "count": len(endpoints),
                "endpoints": endpoints,
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return _empty_list_result("endpoints", "missing", str(error))
        except ConsoleRequestError as error:
            return {
                **_empty_list_result("endpoints", _request_status(error.status_code), str(error)),
                "http_status": error.status_code,
            }

    def list_project_assemblies(
        self,
        *,
        project_id: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            offset, limit = _validate_page(offset, limit)
            connection = self.resolver.resolve()
            selected = project_id or connection.project_id
            if not selected:
                return _empty_list_result(
                    "assemblies", "selection_required", "Не указан project_id и окружение не задаёт текущий проект"
                )
            selected = _validate_uuid(selected, "project_id")
            items = _as_items(
                self.client.get(connection, f"/api/v2/projects/{selected}/assemblies"),
                resource="сборок проекта",
            )
            assemblies = [_assembly_payload(item) for item in items if isinstance(item, Mapping)]
            if query:
                needle = query.strip().casefold()
                assemblies = [item for item in assemblies if _mapping_contains(item, needle)]
            page = assemblies[offset : offset + limit]
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "project_id": selected,
                "count": len(page),
                "total": len(assemblies),
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(page) < len(assemblies),
                "assemblies": page,
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return _empty_list_result("assemblies", "missing", str(error))
        except ConsoleRequestError as error:
            return {
                **_empty_list_result("assemblies", _request_status(error.status_code), str(error)),
                "http_status": error.status_code,
            }

    def get_project_assembly(self, version: str, project_id: str | None = None) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            selected = project_id or connection.project_id
            if not selected:
                return {"status": "selection_required", "message": "Не указан project_id", **_external_metadata()}
            selected = _validate_uuid(selected, "project_id")
            version_segment = _validate_path_segment(version, "version")
            value = self.client.get(
                connection,
                f"/api/v2/projects/{selected}/assemblies/{urllib.parse.quote(version_segment, safe='')}",
            )
            if not isinstance(value, Mapping):
                raise ConsoleRequestError("Панель управления вернула некорректное описание сборки")
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "project_id": selected,
                "assembly": _assembly_payload(value),
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return {"status": "missing", "message": str(error), **_external_metadata()}
        except ConsoleRequestError as error:
            return {**_request_error_payload(error), **_external_metadata()}

    def list_tasks(
        self,
        task_type: Literal["application", "deployment_instance", "group"],
        *,
        status: str | None = None,
        operation_type: str | None = None,
        application_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            offset, limit = _validate_page(offset, limit)
            connection = self.resolver.resolve()
            path = _task_collection_path(task_type)
            items = _as_items(self.client.get(connection, path), resource="задач Console")
            tasks = [_task_payload(item, task_type) for item in items if isinstance(item, Mapping)]
            status_filter = status.strip().casefold() if status else None
            operation_filter = operation_type.strip().casefold() if operation_type else None
            application_filter = _validate_uuid(application_id, "application_id") if application_id else None
            tasks = [item for item in tasks if _task_matches(item, status_filter, operation_filter, application_filter)]
            page = tasks[offset : offset + limit]
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "task_type": task_type,
                "count": len(page),
                "total": len(tasks),
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(page) < len(tasks),
                "tasks": page,
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return _empty_list_result("tasks", "missing", str(error))
        except ConsoleRequestError as error:
            return {
                **_empty_list_result("tasks", _request_status(error.status_code), str(error)),
                "http_status": error.status_code,
            }

    def get_task(
        self,
        task_type: Literal["application", "deployment_instance", "group"],
        task_id: str,
    ) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            selected = _validate_uuid(task_id, "task_id")
            value = self.client.get(connection, f"{_task_collection_path(task_type)}/{selected}")
            if not isinstance(value, Mapping):
                raise ConsoleRequestError("Панель управления вернула некорректное описание задачи")
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "task_type": task_type,
                "task": _task_payload(value, task_type),
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return {"status": "missing", "message": str(error), **_external_metadata()}
        except ConsoleRequestError as error:
            return {**_request_error_payload(error), **_external_metadata()}

    def list_space_projects(
        self,
        *,
        space_id: str | None = None,
        include_deleted: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            selected_space, selection = self._select_space(connection, space_id)
            if selection is not None:
                return selection
            assert selected_space is not None
            raw_ids = _as_items(
                self.client.get(connection, f"/api/v2/spaces/{selected_space}/projects"),
                resource="проектов пространства",
            )
            project_ids: list[str] = []
            for item in raw_ids:
                value = item.get("id") if isinstance(item, dict) else item
                if isinstance(value, str):
                    project_ids.append(_validate_uuid(value, "project_id"))

            all_projects = _as_items(
                self.client.get(connection, "/api/v2/projects"),
                resource="проектов",
            )
            projects_by_id = {
                str(item["id"]): _project_payload(item)
                for item in all_projects
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            projects: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for project_id in project_ids:
                try:
                    project = projects_by_id.get(project_id) or self._project(connection, project_id)
                    if include_deleted or not project["deleted"]:
                        projects.append(project)
                except ConsoleRequestError as error:
                    errors.append(
                        {
                            "project_id": project_id,
                            "status": _request_status(error.status_code),
                            "http_status": error.status_code,
                            "message": str(error),
                        }
                    )

            total = len(projects)
            page = projects[offset : offset + limit]
            return {
                "status": "partial" if errors else "ready",
                "connection": connection.public_info(),
                "space_id": selected_space,
                "count": len(page),
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(page) < total,
                "projects": page,
                "errors": errors,
            }
        except ConsoleConfigurationError as error:
            return {
                "status": "missing",
                "message": str(error),
                "count": 0,
                "total": 0,
                "projects": [],
            }
        except ConsoleRequestError as error:
            return {
                **_request_error_payload(error),
                "count": 0,
                "total": 0,
                "projects": [],
            }

    def _select_space(
        self,
        connection: ConsoleConnection,
        explicit_space_id: str | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        if explicit_space_id:
            return _validate_uuid(explicit_space_id, "space_id"), None
        if connection.space_id:
            return _validate_uuid(connection.space_id, "space_id"), None
        if connection.project_id:
            current = self._project(connection, _validate_uuid(connection.project_id, "project_id"))
            if current["space_id"]:
                return _validate_uuid(current["space_id"], "space_id"), None

        items = _as_items(self.client.get(connection, "/api/v2/spaces"), resource="пространств")
        spaces = [_space_payload(item) for item in items if isinstance(item, dict)]
        if len(spaces) == 1 and spaces[0]["id"]:
            return _validate_uuid(spaces[0]["id"], "space_id"), None
        return None, {
            "status": "selection_required",
            "message": "Выберите пространство: окружение не задаёт space_id однозначно",
            "count": 0,
            "total": 0,
            "projects": [],
            "spaces": spaces,
        }

    def _project(self, connection: ConsoleConnection, project_id: str) -> dict[str, Any]:
        value = self.client.get(connection, f"/api/v2/projects/{project_id}")
        if not isinstance(value, dict):
            raise ConsoleRequestError("Панель управления вернула некорректное описание проекта")
        return _project_payload(value)

    def _application(self, connection: ConsoleConnection, application_id: str) -> dict[str, Any]:
        value = self.client.get(connection, f"/api/v2/applications/{application_id}")
        if not isinstance(value, dict):
            raise ConsoleRequestError("Панель управления вернула некорректное описание приложения")
        returned_id = value.get("id")
        if not isinstance(returned_id, str) or not _same_uuid(returned_id, application_id):
            raise ConsoleRequestError("Панель управления вернула описание другого приложения")
        return _application_payload(value)

    def _select_application(
        self,
        connection: ConsoleConnection,
        explicit_application_id: str | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        if explicit_application_id:
            return _validate_uuid(explicit_application_id, "application_id"), None
        if connection.source == "ide_session" and connection.application_id:
            return _validate_uuid(connection.application_id, "application_id"), None
        return None, {
            "status": "selection_required",
            "message": (
                "Укажите application_id. Без параметра текущее приложение доступно только из активной "
                "Element IDE-сессии; в обычном VS Code оно не выводится из проекта автоматически."
            ),
            **_external_metadata(),
        }

    def _application_subresource(
        self,
        application_id: str | None,
        path_resource: str,
        response_key: str,
        mapper: Callable[[Mapping[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            connection = self.resolver.resolve()
            selected, selection = self._select_application(connection, application_id)
            if selection is not None:
                return selection
            assert selected is not None
            value = self.client.get(connection, f"/api/v2/applications/{selected}/{path_resource}")
            if not isinstance(value, Mapping):
                raise ConsoleRequestError(f"Панель управления вернула некорректный ресурс приложения: {path_resource}")
            return {
                "status": "ready",
                "connection": connection.public_info(),
                "application_id": selected,
                response_key: mapper(value),
                **_external_metadata(),
            }
        except ConsoleConfigurationError as error:
            return {"status": "missing", "message": str(error), **_external_metadata()}
        except ConsoleRequestError as error:
            return {**_request_error_payload(error), **_external_metadata()}


def _environment_values(environ: Mapping[str, str]) -> dict[str, Any]:
    mapping = {
        "server": "ELEMENT_CONSOLE_URL",
        "client_id": "ELEMENT_CONSOLE_CLIENT_ID",
        "client_secret": "ELEMENT_CONSOLE_CLIENT_SECRET",
        "access_token": "ELEMENT_CONSOLE_ACCESS_TOKEN",
        "project_id": "ELEMENT_CONSOLE_PROJECT_ID",
        "space_id": "ELEMENT_CONSOLE_SPACE_ID",
        "verify_tls": "ELEMENT_CONSOLE_VERIFY_TLS",
        "ca_bundle": "ELEMENT_CONSOLE_CA_BUNDLE",
    }
    return {target: environ[source] for target, source in mapping.items() if environ.get(source)}


def _read_settings_file(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ConsoleConfigurationError(f"Файл настроек IDE не найден: {resolved}")
    if resolved.stat().st_size > MAX_SETTINGS_BYTES:
        raise ConsoleConfigurationError(f"Файл настроек IDE слишком велик: {resolved}")
    try:
        text = resolved.read_text(encoding="utf-8-sig")
        value = json.loads(_jsonc_to_json(text))
    except (OSError, json.JSONDecodeError) as error:
        raise ConsoleConfigurationError(f"Не удалось прочитать настройки IDE {resolved}: {error}") from error
    if not isinstance(value, dict):
        raise ConsoleConfigurationError(f"Настройки IDE должны быть JSON-объектом: {resolved}")
    nested = value.get("settings")
    if isinstance(nested, dict):
        value = {**value, **nested}
    return value


def _connection_from_values(values: Mapping[str, Any], *, source: str) -> ConsoleConnection:
    server = _first_string(values, "server", "console_url", "base_url", "1C.server", "paas-url")
    if not server:
        raise ConsoleConfigurationError(f"Источник {source} не содержит адрес Панели управления")
    base_url = _normalize_base_url(server)
    token = _first_string(values, "access_token", "id_token", "token")
    client_id = _first_string(values, "client_id", "client-id", "1C.clientId")
    client_secret = _first_string(values, "client_secret", "client-secret", "1C.clientSecret")
    protected_secret = _first_string(values, "client_secret_dpapi", "client-secret-dpapi")
    if not client_secret and protected_secret:
        client_secret = _unprotect_windows_secret(protected_secret)
    if token:
        auth_kind: Literal["access_token", "client_credentials"] = "access_token"
    elif client_id and client_secret:
        auth_kind = "client_credentials"
    else:
        raise ConsoleConfigurationError(
            f"Источник {source} не содержит access token или полной пары Client-Id/Client-Secret"
        )

    verify_tls_value = values.get("verify_tls", values.get("verify-tls", True))
    verify_tls = _parse_boolean(verify_tls_value, name="verify_tls")
    ca_bundle_value = _first_string(values, "ca_bundle", "ca-bundle")
    ca_bundle = Path(ca_bundle_value).expanduser().resolve() if ca_bundle_value else None
    if ca_bundle and not ca_bundle.is_file():
        raise ConsoleConfigurationError(f"Файл CA bundle не найден: {ca_bundle}")

    project_id = _first_string(values, "project_id", "project-id", "1C.projectId", "paas-project-id")
    application_id = _first_string(
        values,
        "application_id",
        "application-id",
        "1C.applicationId",
        "paas-application-id",
    )
    space_id = _first_string(values, "space_id", "space-id", "1C.spaceId", "paas-space-id")
    return ConsoleConnection(
        base_url=base_url,
        source=source,
        auth_kind=auth_kind,
        client_id=client_id,
        client_secret=client_secret,
        access_token=token,
        project_id=project_id,
        application_id=application_id,
        space_id=space_id,
        verify_tls=verify_tls,
        ca_bundle=ca_bundle,
    )


def _unprotect_windows_secret(value: str) -> str:
    if os.name != "nt":
        raise ConsoleConfigurationError("DPAPI-секрет можно использовать только на Windows")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    try:
        encrypted = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ConsoleConfigurationError("Некорректный DPAPI-секрет в настройках Console") from error
    encrypted_buffer = ctypes.create_string_buffer(encrypted)
    input_blob = DataBlob(len(encrypted), ctypes.cast(encrypted_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ConsoleConfigurationError("Windows DPAPI не смог расшифровать Client-Secret")
    try:
        secret = ctypes.string_at(output_blob.data, output_blob.size).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConsoleConfigurationError("DPAPI Client-Secret не является UTF-8 строкой") from error
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.data, wintypes.HLOCAL))
    if not secret:
        raise ConsoleConfigurationError("DPAPI Client-Secret пуст")
    return secret


def _protect_secret_for_storage(value: str) -> tuple[str, str]:
    if os.name == "nt":
        return "client_secret_dpapi", _protect_windows_secret(value)
    return "client_secret", value


def _protect_windows_secret(value: str) -> str:
    if os.name != "nt":
        raise ConsoleConfigurationError("DPAPI-секрет можно создать только на Windows")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    encoded = value.encode("utf-8")
    encoded_buffer = ctypes.create_string_buffer(encoded)
    input_blob = DataBlob(len(encoded), ctypes.cast(encoded_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    cryptprotect_local_machine = 0x4
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        cryptprotect_local_machine,
        ctypes.byref(output_blob),
    ):
        raise ConsoleConfigurationError("Windows DPAPI не смог защитить Client Secret")
    try:
        protected = ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.data, wintypes.HLOCAL))
    return base64.b64encode(protected).decode("ascii")


def _write_console_settings(path: Path, values: Mapping[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(dict(values), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name == "nt":
            current_sid = _current_windows_sid()
            completed = subprocess.run(
                [
                    "icacls.exe",
                    str(temporary),
                    "/inheritance:r",
                    "/grant:r",
                    "*S-1-5-18:(R)",
                    "*S-1-5-32-544:(F)",
                    f"*{current_sid}:(F)",
                ],
                check=False,
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise ConsoleConfigurationError("Не удалось ограничить доступ к файлу настроек Console")
        else:
            temporary.chmod(0o600)
        temporary.replace(resolved)
    except (OSError, subprocess.SubprocessError) as error:
        raise ConsoleConfigurationError(f"Не удалось сохранить настройки Console: {error}") from error
    finally:
        if temporary.exists():
            with suppress(OSError):
                temporary.unlink()


def _current_windows_sid() -> str:
    completed = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=False,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode == 0:
        rows = list(csv.reader(completed.stdout.splitlines()))
        if rows and len(rows[0]) >= 2 and rows[0][1].startswith("S-"):
            return rows[0][1]
    raise ConsoleConfigurationError("Не удалось определить Windows SID учётной записи MCP")


def _normalize_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConsoleConfigurationError("Адрес Панели управления должен быть абсолютным HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConsoleConfigurationError("Адрес Панели управления не должен содержать логин, пароль, query или fragment")
    path = parsed.path.rstrip("/")
    for suffix in ("/api/v2", "/api", "/sys/token"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _first_string(values: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_boolean(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConsoleConfigurationError(f"{name} должен быть логическим значением")


def _jsonc_to_json(text: str) -> str:
    without_comments: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            without_comments.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            without_comments.append(character)
            index += 1
            continue
        if character == "/" and following == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if character == "/" and following == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                index += 1
            index += 2
            continue
        without_comments.append(character)
        index += 1
    cleaned = "".join(without_comments)
    without_trailing_commas: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(cleaned):
        character = cleaned[index]
        if in_string:
            without_trailing_commas.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            without_trailing_commas.append(character)
            index += 1
            continue
        if character == ",":
            following = index + 1
            while following < len(cleaned) and cleaned[following].isspace():
                following += 1
            if following < len(cleaned) and cleaned[following] in "}]":
                index += 1
                continue
        without_trailing_commas.append(character)
        index += 1
    return "".join(without_trailing_commas)


def _ssl_context(connection: ConsoleConnection) -> ssl.SSLContext | None:
    if not connection.base_url.startswith("https://"):
        return None
    if not connection.verify_tls:
        return ssl._create_unverified_context()  # noqa: SLF001 - explicit opt-out for closed test contours
    return ssl.create_default_context(cafile=str(connection.ca_bundle) if connection.ca_bundle else None)


def _urlopen_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    context: ssl.SSLContext | None,
    timeout: float,
) -> Any:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            payload = response.read()
    except urllib.error.HTTPError as error:
        payload = error.read()
        raise ConsoleRequestError(
            _http_error_message(payload, fallback=f"HTTP {error.code}"),
            status_code=error.code,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise ConsoleRequestError(f"Не удалось обратиться к Панели управления: {reason}") from error
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConsoleRequestError("Панель управления вернула некорректный JSON") from error


def _http_error_message(payload: bytes, *, fallback: str) -> str:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    if isinstance(value, dict):
        nested = value.get("error")
        if isinstance(nested, dict):
            value = nested
        message = value.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:500]
    return fallback


def _connection_fingerprint(connection: ConsoleConnection) -> str:
    material = "\0".join(
        (
            connection.base_url,
            connection.auth_kind,
            connection.client_id or "",
            connection.client_secret or "",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _redact_connection_error(
    message: str,
    connection: ConsoleConnection,
    headers: Mapping[str, str],
) -> str:
    sensitive = [connection.client_secret, connection.access_token]
    authorization = headers.get("Authorization")
    if authorization:
        sensitive.append(authorization)
        _, _, credential = authorization.partition(" ")
        sensitive.append(credential)
    result = message
    for value in sensitive:
        if value:
            result = result.replace(value, "[скрыто]")
    return result[:500]


def _validate_uuid(value: str, name: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as error:
        raise ConsoleConfigurationError(f"{name} должен быть UUID") from error


def _same_uuid(left: str, right: str) -> bool:
    try:
        return uuid.UUID(left.strip()) == uuid.UUID(right.strip())
    except (ValueError, AttributeError):
        return False


def _as_items(value: Any, *, resource: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "data", "results", "content"):
            items = value.get(key)
            if isinstance(items, list):
                return items
    raise ConsoleRequestError(f"Панель управления вернула некорректный список {resource}")


def _space_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("id"),
        "name": value.get("name"),
        "owner": value.get("owner"),
        "date_created": value.get("date-created", value.get("dateCreated")),
        "applications_count": value.get("applications-count", value.get("applicationsCount")),
        "projects_count": value.get("projects-count", value.get("projectsCount")),
        "users_quota": value.get("users-quota", value.get("usersQuota")),
        "applications_quota": value.get("applications-quota", value.get("applicationsQuota")),
        "project_creation_allowed": value.get("project-creation-allowed", value.get("projectCreationAllowed")),
    }


def _project_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    default = value.get("default-image", value.get("defaultAssembly"))
    return {
        "id": value.get("id"),
        "parent_id": value.get("parent-id", value.get("parentId")),
        "group_id": value.get("group-id", value.get("groupId")),
        "name": value.get("name"),
        "presentation": value.get("presentation"),
        "description": value.get("description"),
        "deleted": bool(value.get("deleted", False)),
        "application_count": value.get("application-count", value.get("applicationCount")),
        "code": value.get("code"),
        "date_created": value.get("date-created", value.get("dateCreated")),
        "space_id": value.get("space-id", value.get("spaceId")),
        "project_kind": value.get("project-kind", value.get("projectKind")),
        "default_assembly": default if isinstance(default, dict) else None,
    }


def _application_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    project = value.get("project")
    source = value.get("source")
    current_task = value.get("current-task", value.get("currentTask"))
    return {
        "id": value.get("id"),
        "name": _safe_external_text(value.get("name")),
        "display_name": _safe_external_text(value.get("display-name", value.get("displayName"))),
        "description": _safe_external_text(value.get("description")),
        "date_created": value.get("date-created", value.get("dateCreated")),
        "status": value.get("status"),
        "error": _safe_external_text(value.get("error")),
        "uri": value.get("uri"),
        "development_mode": value.get("development-mode", value.get("developmentMode")),
        "debugging": value.get("debugging"),
        "space_id": value.get("space-id", value.get("spaceId")),
        "technology_version": value.get("technology-version", value.get("platformVersion")),
        "dbms_type": value.get("dbms-type", value.get("dbmsType")),
        "autostarting_scheduled_jobs": value.get(
            "autostarting-scheduled-jobs",
            value.get("autostartingScheduledJobs"),
        ),
        "autostarting_esb": value.get("autostarting-esb", value.get("autostartingEsb")),
        "project": {"id": project.get("id")} if isinstance(project, Mapping) else None,
        "source": (
            {
                "type": source.get("type"),
                "project_version_id": source.get("project-version-id", source.get("projectVersionId")),
                "project_id": source.get("image-id", source.get("projectId")),
                "project_name": source.get("project-name", source.get("projectName")),
                "project_version": source.get("project-version", source.get("projectVersion")),
                "dump_id": source.get("dump-id", source.get("dumpId")),
            }
            if isinstance(source, Mapping)
            else None
        ),
        "current_task": (_task_payload(current_task, "application") if isinstance(current_task, Mapping) else None),
        "endpoint": (_endpoint_payload(value["endpoint"]) if isinstance(value.get("endpoint"), Mapping) else None),
    }


def _application_status_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    current_task = value.get("current-task", value.get("currentTask"))
    return {
        "status": value.get("status"),
        "current_task": _task_payload(current_task, "application") if isinstance(current_task, Mapping) else None,
    }


def _application_technology_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "technology_version": value.get("technology-version", value.get("technologyVersion")),
        "date_updated": value.get("date-updated", value.get("dateUpdated")),
    }


def _application_project_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("id"),
        "developer": _safe_external_text(value.get("developer")),
        "version": _safe_external_text(value.get("version")),
        "title": _safe_external_text(value.get("title")),
        "hash": value.get("hash"),
        "date_updated": value.get("date-updated", value.get("dateUpdated")),
    }


def _endpoint_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    # Deliberately excludes certificate and domain-validation payloads: they may contain private material/tokens.
    return {
        "id": value.get("id"),
        "fqdn": _safe_external_text(value.get("fqdn")),
        "context_path": _safe_external_text(value.get("context-path", value.get("contextPath"))),
        "is_active": value.get("is-active", value.get("isActive")),
        "status": value.get("status"),
        "message": _safe_external_text(value.get("message")),
        "certificate_type": value.get("certificate-type", value.get("certificateType")),
    }


def _assembly_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("id"),
        "assembly_version": _safe_external_text(value.get("assembly-version", value.get("assemblyVersion"))),
        "created": value.get("created"),
        "project_id": value.get("project-id", value.get("projectId")),
        "project_name": _safe_external_text(value.get("project-name", value.get("projectName"))),
        "project_version": _safe_external_text(value.get("project-version", value.get("projectVersion"))),
        "project_developer": _safe_external_text(value.get("project-developer", value.get("projectDeveloper"))),
        "branch_name": _safe_external_text(value.get("branch-name", value.get("branchName"))),
        "commit_id": value.get("commit-id", value.get("commitId")),
        "comment": _safe_external_text(value.get("comment")),
        "modified": value.get("modified"),
    }


def _task_payload(
    value: Mapping[str, Any],
    task_type: Literal["application", "deployment_instance", "group"],
) -> dict[str, Any]:
    if task_type == "group":
        nested = value.get("tasks")
        nested_items = nested[:MAX_LIST_LIMIT] if isinstance(nested, list) else []
        return {
            "id": value.get("id"),
            "type": value.get("type"),
            "date_created": value.get("date-created", value.get("dateCreated")),
            "created_by": _safe_external_text(value.get("created-by", value.get("createdBy"))),
            "any_failure": value.get("any-failure", value.get("anyFailure")),
            "completed_count": value.get("completed-count", value.get("completedCount")),
            "cancelled_count": value.get("cancelled-count", value.get("cancelledCount")),
            "total_count": value.get("total-count", value.get("totalCount")),
            "status": value.get("status"),
            "error_message": _safe_external_text(value.get("error-message", value.get("errorMessage"))),
            "tasks": [_task_payload(item, "application") for item in nested_items if isinstance(item, Mapping)],
            "tasks_truncated": isinstance(nested, list) and len(nested) > MAX_LIST_LIMIT,
        }
    return {
        "id": value.get("id"),
        "status": value.get("status"),
        "operation_type": value.get("operation-type", value.get("operationType")),
        "start_date": value.get("start-date", value.get("startDate")),
        "end_date": value.get("end-date", value.get("endDate")),
        "group_id": value.get("group-id", value.get("groupId")),
        "application_id": (
            value.get("application-id", value.get("applicationId")) if task_type == "application" else None
        ),
        "error_message": _safe_external_text(value.get("error-message", value.get("errorMessage"))),
    }


def _console_capabilities() -> dict[str, Any]:
    return {
        "contract_source": "normalized Element 9.2.4-6 Console API",
        "read_only": True,
        "resources": {
            "spaces": ["list"],
            "projects": ["list", "get", "assemblies"],
            "applications": ["list", "get", "status", "technology", "project", "endpoints"],
            "tasks": ["application", "deployment_instance", "group"],
        },
        "safe_get_retry": {"attempts": 3, "statuses": sorted(RETRYABLE_GET_STATUSES)},
    }


def _external_metadata() -> dict[str, str]:
    return {
        "data_source": "element_management_console",
        "content_trust": "external_untrusted",
        "contract_element_version": CONSOLE_CONTRACT_VERSION,
    }


def _empty_list_result(key: str, status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "message": _safe_external_text(message),
        "count": 0,
        "total": 0,
        key: [],
        **_external_metadata(),
    }


def _validate_page(offset: int, limit: int) -> tuple[int, int]:
    if offset < 0:
        raise ConsoleConfigurationError("offset не может быть отрицательным")
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise ConsoleConfigurationError(f"limit должен быть от 1 до {MAX_LIST_LIMIT}")
    return offset, limit


def _validate_path_segment(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or any(character in normalized for character in "/\\\0\r\n"):
        raise ConsoleConfigurationError(f"{name} содержит недопустимое значение")
    return normalized


def _application_matches(
    application: Mapping[str, Any],
    *,
    query: str | None,
    status: str | None,
    project_id: str | None,
) -> bool:
    if query and not _mapping_contains(application, query):
        return False
    current_status = application.get("status")
    if status and (not isinstance(current_status, str) or current_status.casefold() != status):
        return False
    current_project = _application_project_id(application)
    return not project_id or bool(current_project and _same_uuid(current_project, project_id))


def _mapping_contains(value: Mapping[str, Any], needle: str) -> bool:
    return needle in " ".join(str(item) for item in value.values() if item is not None).casefold()


def _task_matches(
    task: Mapping[str, Any],
    status: str | None,
    operation_type: str | None,
    application_id: str | None,
) -> bool:
    if status and str(task.get("status", "")).casefold() != status:
        return False
    if operation_type and str(task.get("operation_type", "")).casefold() != operation_type:
        return False
    current_application = task.get("application_id")
    return not application_id or bool(
        isinstance(current_application, str) and _same_uuid(current_application, application_id)
    )


def _task_collection_path(task_type: str) -> str:
    paths = {
        "application": "/api/v2/tasks/application-tasks",
        "deployment_instance": "/api/v2/tasks/deployment-instance-tasks",
        "group": "/api/v2/tasks/group-tasks",
    }
    try:
        return paths[task_type]
    except KeyError as error:
        raise ConsoleConfigurationError("Неизвестный тип задачи Console") from error


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(?:client[_-]?secret|access[_-]?token|id[_-]?token|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)


def _safe_external_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[скрыто]", result)
    return result[:MAX_EXTERNAL_TEXT]


def _application_project_id(application: Mapping[str, Any]) -> str | None:
    project = application.get("project")
    if isinstance(project, Mapping) and isinstance(project.get("id"), str):
        return project["id"]
    source = application.get("source")
    if isinstance(source, Mapping) and isinstance(source.get("project_id"), str):
        return source["project_id"]
    return None


def _request_status(status_code: int | None) -> str:
    return {
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
    }.get(status_code, "unavailable")


def _request_error_payload(error: ConsoleRequestError) -> dict[str, Any]:
    return {
        "status": _request_status(error.status_code),
        "http_status": error.status_code,
        "message": str(error),
    }
