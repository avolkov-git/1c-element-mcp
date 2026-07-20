from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from element_mcp.config import ConfigurationStore, ServerSettings, discover_project_path

MAX_SETTINGS_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0


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
            "space_id": self.space_id,
            "verify_tls": self.verify_tls,
            "ca_bundle": str(self.ca_bundle) if self.ca_bundle else None,
        }


Requester = Callable[[str, str, Mapping[str, str], bytes | None, ssl.SSLContext | None, float], Any]


class ConsoleContextResolver:
    def __init__(self, settings: ServerSettings, *, environ: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self.environ = environ if environ is not None else os.environ
        self.config_store = ConfigurationStore(settings.resolved_config_path)

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
        environment_values = _environment_values(self.environ)
        if environment_values:
            candidates.append(("environment", environment_values))

        ide_settings = self.settings.resolved_ide_settings_path
        if ide_settings:
            candidates.append(("ide_settings", _read_settings_file(ide_settings)))

        console_config = self.settings.resolved_console_config_path
        if console_config.is_file() and console_config != ide_settings:
            candidates.append(("console_config", _read_settings_file(console_config)))

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


class ConsoleHttpClient:
    def __init__(self, *, requester: Requester | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.requester = requester or _urlopen_json
        self.timeout = timeout
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, connection: ConsoleConnection, path: str) -> Any:
        return self._authorized_request(connection, "GET", path)

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
        payload = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii")
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
        return self.requester(
            method,
            f"{connection.base_url}{path}",
            {"Accept": "application/json", **headers},
            body,
            context,
            self.timeout,
        )


class ConsoleService:
    def __init__(
        self,
        settings: ServerSettings,
        *,
        resolver: ConsoleContextResolver | None = None,
        client: ConsoleHttpClient | None = None,
    ) -> None:
        self.resolver = resolver or ConsoleContextResolver(settings)
        self.client = client or ConsoleHttpClient()

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
    space_id = _first_string(values, "space_id", "space-id", "1C.spaceId", "paas-space-id")
    return ConsoleConnection(
        base_url=base_url,
        source=source,
        auth_kind=auth_kind,
        client_id=client_id,
        client_secret=client_secret,
        access_token=token,
        project_id=project_id,
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


def _validate_uuid(value: str, name: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as error:
        raise ConsoleConfigurationError(f"{name} должен быть UUID") from error


def _as_items(value: Any, *, resource: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "data", "results"):
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
