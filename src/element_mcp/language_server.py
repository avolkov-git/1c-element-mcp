from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from .config import ConfigurationStore, ServerSettings
from .installation import discover_element_installations, require_element_installation
from .project import ProjectError, ProjectService, _read_source_text
from .semantic import IDENTIFIER, SemanticService

MAIN_CLASS = "com.e1c.g5rt.lsp.server.appengine.AppEngineServerMain"
LSP_JAR_PATTERN = "com.e1c.g5rt.lsp.server.appengine-*.jar"
MINIMUM_JAVA_VERSION = 11
DEFAULT_REQUEST_TIMEOUT = 20.0
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class LanguageServerError(RuntimeError):
    pass


class LanguageServerUnavailable(LanguageServerError):
    pass


@dataclass(frozen=True, slots=True)
class LanguageServerRuntime:
    bundle_path: Path
    product_version: str
    lsp_version: str
    classpath: Path
    dbeng_path: Path
    java_path: Path
    java_version: int
    source: str


@dataclass(slots=True)
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


def _numeric_version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _compatible_versions(product_version: str, lsp_version: str) -> bool:
    return _numeric_version(product_version)[:3] == _numeric_version(lsp_version)[:3]


def _java_executable(path: Path | None) -> Path | None:
    if path is None:
        discovered = shutil.which("java")
        return Path(discovered).resolve() if discovered else None
    candidate = path.expanduser().resolve()
    if candidate.is_dir():
        names = ("java.exe", "java") if os.name == "nt" else ("java", "java.exe")
        for name in names:
            nested = candidate / "bin" / name
            if nested.is_file():
                return nested
    return candidate


def _java_version(path: Path) -> int:
    if not path.is_file():
        raise LanguageServerUnavailable(f"Java не найдена: {path}")
    try:
        process = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LanguageServerUnavailable(f"Не удалось запустить Java {path}: {error}") from error
    output = (process.stderr or process.stdout).strip()
    match = re.search(r'version\s+"(?P<version>\d+)(?:\.(?P<minor>\d+))?', output, re.IGNORECASE)
    if process.returncode != 0 or match is None:
        raise LanguageServerUnavailable(f"Не удалось определить версию Java {path}")
    major = int(match.group("version"))
    if major == 1 and match.group("minor"):
        major = int(match.group("minor"))
    if major < MINIMUM_JAVA_VERSION:
        raise LanguageServerUnavailable(
            f"Language Server Element требует Java {MINIMUM_JAVA_VERSION} или новее; найдена Java {major}: {path}"
        )
    return major


def inspect_language_server_runtime(bundle_path: Path, java_path: Path | None, *, source: str) -> LanguageServerRuntime:
    try:
        installation = require_element_installation(bundle_path)
    except ValueError as error:
        raise LanguageServerUnavailable(str(error)) from error
    plugin_bin = installation.path / "ide" / "theia" / "plugins" / "@1c-appengine-plugin" / "bin"
    classpath = plugin_bin / "appengine-lsp" / "repo"
    jars = sorted(classpath.glob(LSP_JAR_PATTERN)) if classpath.is_dir() else []
    if not jars:
        raise LanguageServerUnavailable(f"В бандле не найден Language Server: {classpath / LSP_JAR_PATTERN}")
    match = re.match(r"com\.e1c\.g5rt\.lsp\.server\.appengine-(.+)\.jar$", jars[-1].name)
    if match is None:
        raise LanguageServerUnavailable(f"Не удалось определить версию Language Server: {jars[-1]}")
    lsp_version = match.group(1)
    product_version = installation.product_version or ""
    if not _compatible_versions(product_version, lsp_version):
        raise LanguageServerUnavailable(
            f"Версии бандла ({product_version}) и Language Server ({lsp_version}) несовместимы"
        )
    dbeng_path = plugin_bin / "dbeng"
    if not dbeng_path.is_dir():
        raise LanguageServerUnavailable(f"В бандле не найден каталог dbeng: {dbeng_path}")
    executable = _java_executable(java_path)
    if executable is None:
        raise LanguageServerUnavailable(
            f"Java {MINIMUM_JAVA_VERSION}+ не найдена. Установите JRE/JDK и укажите путь через "
            "configure_language_server."
        )
    return LanguageServerRuntime(
        bundle_path=installation.path,
        product_version=product_version,
        lsp_version=lsp_version,
        classpath=classpath,
        dbeng_path=dbeng_path,
        java_path=executable,
        java_version=_java_version(executable),
        source=source,
    )


class _JsonRpcProcess:
    """Minimal, bounded LSP JSON-RPC client over a child process's stdio."""

    def __init__(self, command: list[str], *, root: Path, notification_handler) -> None:
        self.command = command
        self.root = root
        self.notification_handler = notification_handler
        self.process: subprocess.Popen[bytes] | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._next_id = 1
        self._stderr: deque[str] = deque(maxlen=100)
        self._protocol_noise: deque[str] = deque(maxlen=20)
        self.server_capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.element_version: Any = None
        self._closed = False

    def start(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise LanguageServerUnavailable(f"Не удалось запустить Language Server: {error}") from error
        threading.Thread(target=self._read_stdout, name="element-lsp-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="element-lsp-stderr", daemon=True).start()
        root_uri = self.root.as_uri()
        initialized = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "1c-element-mcp"},
                "locale": "ru-RU",
                "rootPath": str(self.root),
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
                "capabilities": {
                    "workspace": {"configuration": True, "workspaceFolders": True},
                    "window": {"workDoneProgress": True},
                    "textDocument": {
                        "synchronization": {"dynamicRegistration": False, "didSave": True},
                        "definition": {"dynamicRegistration": False, "linkSupport": True},
                        "references": {"dynamicRegistration": False},
                        "publishDiagnostics": {
                            "relatedInformation": True,
                            "versionSupport": True,
                            "tagSupport": {"valueSet": [1, 2]},
                        },
                    },
                },
                "trace": "off",
            },
            timeout=timeout,
        )
        if isinstance(initialized, dict):
            self.server_capabilities = initialized.get("capabilities") or {}
            self.server_info = initialized.get("serverInfo") or {}
        self.notify("initialized", {})
        try:
            self.element_version = self.request("versions/elementVersion", timeout=min(timeout, 5.0))
        except LanguageServerError:
            self.element_version = None

    def request(self, method: str, params: Any = None, *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Any:
        process = self.process
        if process is None or process.poll() is not None:
            raise LanguageServerUnavailable(self.failure_message("Language Server не запущен"))
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self.notify("$/cancelRequest", {"id": request_id})
            raise LanguageServerError(f"Language Server не ответил на {method} за {timeout:g} с")
        response = pending.response or {}
        if "error" in response:
            error = response["error"]
            raise LanguageServerError(f"Language Server вернул ошибку для {method}: {error}")
        return response.get("result")

    def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def stop(self) -> None:
        process = self.process
        if process is None or self._closed:
            return
        self._closed = True
        if process.poll() is None:
            try:
                self.request("shutdown", timeout=3)
                self.notify("exit")
                process.wait(timeout=3)
            except (LanguageServerError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        self._fail_pending("Language Server остановлен")

    def failure_message(self, prefix: str) -> str:
        details = " | ".join(list(self._stderr)[-5:] or list(self._protocol_noise)[-5:])
        return f"{prefix}: {details}" if details else prefix

    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise LanguageServerUnavailable(self.failure_message("Language Server завершился"))
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            raise LanguageServerError("LSP-сообщение превышает допустимый размер")
        framed = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        try:
            with self._write_lock:
                process.stdin.write(framed)
                process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            message = self.failure_message(f"Связь с Language Server потеряна: {error}")
            raise LanguageServerUnavailable(message) from error

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                content_length: int | None = None
                while True:
                    line = process.stdout.readline()
                    if not line:
                        self._fail_pending(self.failure_message("Language Server завершил поток вывода"))
                        return
                    if line in {b"\r\n", b"\n"}:
                        if content_length is not None:
                            break
                        continue
                    decoded = line.decode("ascii", errors="replace").strip()
                    if decoded.lower().startswith("content-length:"):
                        try:
                            content_length = int(decoded.split(":", 1)[1].strip())
                        except ValueError:
                            content_length = None
                    elif content_length is None:
                        self._protocol_noise.append(decoded[:500])
                if content_length is None or not 0 <= content_length <= MAX_MESSAGE_BYTES:
                    self._fail_pending("Language Server прислал сообщение недопустимого размера")
                    return
                payload = process.stdout.read(content_length)
                if len(payload) != content_length:
                    self._fail_pending("Language Server оборвал LSP-сообщение")
                    return
                try:
                    message = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._protocol_noise.append("Некорректное JSON-RPC сообщение")
                    continue
                if isinstance(message, dict):
                    self._handle(message)
        except OSError as error:
            self._fail_pending(f"Ошибка чтения Language Server: {error}")

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in iter(process.stderr.readline, b""):
            self._stderr.append(line.decode("utf-8", errors="replace").strip()[:1000])

    def _handle(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" in message:
            self._handle_server_request(message)
            return
        if "id" in message:
            with self._pending_lock:
                pending = self._pending.pop(message["id"], None)
            if pending is not None:
                pending.response = message
                pending.event.set()
            return
        method = message.get("method")
        if isinstance(method, str):
            self.notification_handler(method, message.get("params"))

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        if method == "workspace/configuration":
            result = [self._configuration_item(item) for item in params.get("items", [])]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self.root.as_uri(), "name": self.root.name}]
        elif method in {"client/registerCapability", "client/unregisterCapability", "window/workDoneProgress/create"}:
            result = None
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "1c-element-mcp keeps the project read-only"}
        else:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"Unsupported client method: {method}"},
                }
            )
            return
        self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    @staticmethod
    def _configuration_item(item: dict[str, Any]) -> Any:
        section = str(item.get("section") or "")
        settings = {"locale": "ru_RU", "moduleBuildDelayMs": 200}
        if section in {"1C.element.lsp", "1C.element"}:
            return settings
        if section.endswith("locale"):
            return settings["locale"]
        if section.endswith("moduleBuildDelayMs"):
            return settings["moduleBuildDelayMs"]
        return None

    def _fail_pending(self, message: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.response = {"error": {"code": -32097, "message": message}}
            request.event.set()


class LanguageServerService:
    def __init__(self, settings: ServerSettings, project: ProjectService, semantic: SemanticService) -> None:
        self.settings = settings
        self.project = project
        self.semantic = semantic
        self.configuration = ConfigurationStore(settings.resolved_config_path)
        self._client: _JsonRpcProcess | None = None
        self._client_root: Path | None = None
        self._client_runtime: LanguageServerRuntime | None = None
        self._documents: dict[str, tuple[str, int]] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diagnostic_versions: dict[str, int | None] = {}
        self._builder_state: int | None = None
        self._indexer_state: int | None = None
        self._condition = threading.Condition()
        self._lifecycle_lock = threading.RLock()
        atexit.register(self.close)

    def configure(self, bundle_path: str | Path, *, java_path: str | Path | None = None) -> dict[str, Any]:
        supplied_bundle = Path(bundle_path).expanduser().resolve()
        fixed_bundle = self.settings.resolved_element_bundle_path
        if fixed_bundle is not None and fixed_bundle != supplied_bundle:
            raise LanguageServerError(
                "Путь бандла зафиксирован параметром --element-bundle-path или ELEMENT_BUNDLE_PATH; "
                "измените конфигурацию запуска MCP."
            )
        selected_java = Path(java_path).expanduser().resolve() if java_path else self.settings.resolved_java_path
        runtime = inspect_language_server_runtime(supplied_bundle, selected_java, source="user-confirmed")
        self.configuration.configure_language_server(runtime.bundle_path, java_path=runtime.java_path)
        self.close()
        return {"status": "ready", "message": "Language Server Element настроен", **self._runtime_public(runtime)}

    def status(self, *, start: bool = False) -> dict[str, Any]:
        try:
            root = self.project._required_root()
        except ProjectError as error:
            return {
                "status": "missing",
                "message": str(error),
                "project_path": None,
                "process": "stopped",
            }
        try:
            runtime = self._resolve_runtime()
            client = self._ensure_client() if start else self._running_client(root)
        except (LanguageServerError, ValueError) as error:
            return {
                "status": "missing" if isinstance(error, LanguageServerUnavailable) else "error",
                "message": str(error),
                "project_path": str(root),
                "process": "stopped",
                "fallback": "syntax-aware lexical index",
            }
        result = {
            "status": "ready",
            "message": (
                "Language Server Element готов" if client else "Language Server настроен и будет запущен по требованию"
            ),
            "project_path": str(root),
            "process": "running" if client else "stopped",
            "fallback": "syntax-aware lexical index",
            **self._runtime_public(runtime),
        }
        if client:
            result.update(
                {
                    "reported_element_version": client.element_version,
                    "server_info": client.server_info,
                    "capabilities": {
                        key: key in client.server_capabilities
                        for key in ("definitionProvider", "referencesProvider", "textDocumentSync")
                    },
                    "builder_state": self._builder_state,
                    "indexer_state": self._indexer_state,
                }
            )
        return result

    def definition(self, relative_path: str, line: int, column: int) -> dict[str, Any]:
        try:
            client, path, uri = self._document(relative_path)
            result = client.request(
                "textDocument/definition",
                {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": column - 1}},
            )
            raw_locations = [] if result is None else result if isinstance(result, list) else [result]
            return {
                "status": "ready",
                "analysis_mode": "Element Language Server",
                "semantic_guarantee": True,
                "source": "element-language-server",
                "query": {"path": path.relative_to(self._client_root).as_posix(), "line": line, "column": column},
                "count": len(raw_locations),
                "locations": [self._public_location(item) for item in raw_locations],
                "language_server": self.status(),
            }
        except LanguageServerError as error:
            return self._fallback_definition(relative_path, line, column, str(error))

    def references(
        self,
        relative_path: str,
        line: int,
        column: int,
        *,
        include_declaration: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            client, path, uri = self._document(relative_path)
            result = client.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line - 1, "character": column - 1},
                    "context": {"includeDeclaration": include_declaration},
                },
            )
            raw_locations = result if isinstance(result, list) else []
            return {
                "status": "ready",
                "analysis_mode": "Element Language Server",
                "semantic_guarantee": True,
                "source": "element-language-server",
                "query": {"path": path.relative_to(self._client_root).as_posix(), "line": line, "column": column},
                "include_declaration": include_declaration,
                "count": min(len(raw_locations), limit),
                "truncated": len(raw_locations) > limit,
                "locations": [self._public_location(item) for item in raw_locations[:limit]],
                "language_server": self.status(),
            }
        except LanguageServerError as error:
            return self._fallback_references(relative_path, line, column, include_declaration, limit, str(error))

    def diagnostics(
        self,
        *,
        relative_path: str | None = None,
        wait_seconds: float = 2.0,
        limit: int = 200,
    ) -> dict[str, Any]:
        try:
            if relative_path:
                self._document(relative_path)
            else:
                self._ensure_client()
            deadline = time.monotonic() + wait_seconds
            with self._condition:
                while time.monotonic() < deadline and self._builder_state != 0:
                    self._condition.wait(deadline - time.monotonic())
            rows: list[dict[str, Any]] = []
            for uri, diagnostics in sorted(self._diagnostics.items()):
                if relative_path and self._uri_relative_path(uri) != Path(relative_path).as_posix():
                    continue
                for diagnostic in diagnostics:
                    rows.append(self._public_diagnostic(uri, diagnostic))
            return {
                "status": "ready",
                "analysis_mode": "Element Language Server",
                "semantic_guarantee": True,
                "source": "element-language-server",
                "scope": relative_path or "published project diagnostics",
                "complete": self._builder_state == 0,
                "builder_state": self._builder_state,
                "indexer_state": self._indexer_state,
                "count": min(len(rows), limit),
                "truncated": len(rows) > limit,
                "diagnostics": rows[:limit],
                "language_server": self.status(),
            }
        except LanguageServerError as error:
            return {
                "status": "unavailable",
                "message": str(error),
                "analysis_mode": "none",
                "semantic_guarantee": False,
                "diagnostics": [],
                "fallback": "Лексический индекс не может достоверно воспроизводить диагностику компилятора.",
                "language_server": self.status(),
            }

    def close(self) -> None:
        with self._lifecycle_lock:
            client = self._client
            self._client = None
            self._client_root = None
            self._client_runtime = None
            self._documents.clear()
            self._diagnostics.clear()
            self._builder_state = None
            self._indexer_state = None
        if client is not None:
            client.stop()

    def _resolve_runtime(self) -> LanguageServerRuntime:
        fixed_bundle = self.settings.resolved_element_bundle_path
        fixed_java = self.settings.resolved_java_path
        if fixed_bundle is not None:
            return inspect_language_server_runtime(fixed_bundle, fixed_java, source="startup-argument")
        stored = self.configuration.language_server_configuration()
        if stored["bundle_path"] is not None:
            return inspect_language_server_runtime(
                stored["bundle_path"], fixed_java or stored["java_path"], source="mcp-configuration"
            )
        installations = discover_element_installations()
        if len(installations) == 1:
            return inspect_language_server_runtime(
                Path(installations[0]["path"]), fixed_java, source="single-standard-installation"
            )
        if installations:
            paths = ", ".join(item["path"] for item in installations[:5])
            raise LanguageServerUnavailable(
                f"Найдено несколько бандлов Element; выберите один через configure_language_server: {paths}"
            )
        raise LanguageServerUnavailable(
            "Бандл Element для Language Server не настроен. Укажите подтверждённый путь через "
            "configure_language_server."
        )

    def _ensure_client(self) -> _JsonRpcProcess:
        root = self.project._required_root()
        runtime = self._resolve_runtime()
        with self._lifecycle_lock:
            running = self._running_client(root)
            if running is not None and self._client_runtime == runtime:
                return running
            if self._client is not None:
                self.close()
            metadata_key = hashlib.sha256(f"{root}\0{runtime.product_version}".encode()).hexdigest()[:20]
            metadata = self.settings.resolved_data_path / "language-server" / metadata_key
            metadata.mkdir(parents=True, exist_ok=True)
            classpath = str(runtime.classpath / "*")
            command = [
                str(runtime.java_path),
                "-Dfile.encoding=UTF-8",
                "-Xmx1024m",
                "-XX:-OmitStackTraceInFastThrow",
                "--add-opens=java.base/java.lang=ALL-UNNAMED",
                "--add-opens=java.base/java.nio=ALL-UNNAMED",
                "--add-opens=java.base/sun.nio.ch=ALL-UNNAMED",
                "-cp",
                classpath,
                MAIN_CLASS,
                "--locale",
                "ru_RU",
                "--metadata",
                str(metadata),
                "--use-client-indent",
                "--dbeng",
                str(runtime.dbeng_path),
            ]
            client = _JsonRpcProcess(command, root=root, notification_handler=self._notification)
            self._client = client
            self._client_root = root
            self._client_runtime = runtime
            try:
                client.start()
            except Exception:
                self._client = None
                self._client_root = None
                self._client_runtime = None
                client.stop()
                raise
            return client

    def _running_client(self, root: Path) -> _JsonRpcProcess | None:
        client = self._client
        if client is None or self._client_root != root or client.process is None or client.process.poll() is not None:
            return None
        return client

    def _document(self, relative_path: str) -> tuple[_JsonRpcProcess, Path, str]:
        if not relative_path:
            raise ValueError("relative_path не может быть пустым")
        if not isinstance(relative_path, str):
            raise ValueError("relative_path должен быть строкой")
        root = self.project._required_root()
        path = self.project._resolve_source_file(root, relative_path)
        client = self._ensure_client()
        text, payload = _read_source_text(path)
        digest = hashlib.sha256(payload).hexdigest()
        uri = path.as_uri()
        previous = self._documents.get(uri)
        if previous is None:
            version = 1
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self._language_id(path),
                        "version": version,
                        "text": text,
                    }
                },
            )
        elif previous[0] != digest:
            version = previous[1] + 1
            client.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]},
            )
        else:
            version = previous[1]
        self._documents[uri] = (digest, version)
        return client, path, uri

    def _notification(self, method: str, params: Any) -> None:
        with self._condition:
            if method == "textDocument/publishDiagnostics" and isinstance(params, dict):
                uri = params.get("uri")
                if isinstance(uri, str):
                    self._diagnostics[uri] = list(params.get("diagnostics") or [])
                    self._diagnostic_versions[uri] = params.get("version")
            elif method == "builder/builderStateChanged":
                self._builder_state = params if params in {0, 1} else None
            elif method == "builder/indexerStateChanged":
                self._indexer_state = params if params in {0, 1} else None
            self._condition.notify_all()

    def _fallback_definition(self, relative_path: str, line: int, column: int, reason: str) -> dict[str, Any]:
        symbol = self._symbol_at(relative_path, line, column)
        lookup = self.semantic.lookup_symbol(symbol, limit=50)
        return {
            "status": "ready",
            "analysis_mode": "syntax-aware lexical fallback",
            "semantic_guarantee": False,
            "source": "lexical-index",
            "fallback_reason": reason,
            "query": {"path": relative_path, "line": line, "column": column, "symbol": symbol},
            "count": lookup["count"],
            "locations": [item["declaration"] for item in lookup["matches"]],
            "ambiguity": lookup["resolution"],
            "language_server": self.status(),
        }

    def _fallback_references(
        self,
        relative_path: str,
        line: int,
        column: int,
        include_declaration: bool,
        limit: int,
        reason: str,
    ) -> dict[str, Any]:
        symbol = self._symbol_at(relative_path, line, column)
        references = self.semantic.find_references(
            symbol,
            include_declarations=include_declaration,
            limit=limit,
        )
        return {
            **references,
            "analysis_mode": "syntax-aware lexical fallback",
            "source": "lexical-index",
            "fallback_reason": reason,
            "query_position": {"path": relative_path, "line": line, "column": column, "symbol": symbol},
            "language_server": self.status(),
        }

    def _symbol_at(self, relative_path: str, line: int, column: int) -> str:
        root = self.project._required_root()
        path = self.project._resolve_source_file(root, relative_path)
        text, _ = _read_source_text(path)
        lines = text.splitlines()
        if line < 1 or line > len(lines):
            raise ValueError(f"Строка {line} находится за пределами файла {relative_path}")
        source_line = lines[line - 1]
        if column < 1 or column > len(source_line) + 1:
            raise ValueError(f"Колонка {column} находится за пределами строки {line}")
        index = column - 1
        for match in IDENTIFIER.finditer(source_line):
            if match.start() <= index < match.end() or index == match.end() == len(source_line):
                return match.group(0)
        raise ValueError(f"В позиции {relative_path}:{line}:{column} нет идентификатора")

    def _public_location(self, location: dict[str, Any]) -> dict[str, Any]:
        uri = location.get("uri") or location.get("targetUri")
        range_value = location.get("range") or location.get("targetSelectionRange") or location.get("targetRange") or {}
        start = range_value.get("start") or {}
        end = range_value.get("end") or {}
        return {
            "path": self._uri_relative_path(uri) if isinstance(uri, str) else None,
            "uri": uri,
            "line": int(start.get("line", 0)) + 1,
            "column": int(start.get("character", 0)) + 1,
            "end_line": int(end.get("line", start.get("line", 0))) + 1,
            "end_column": int(end.get("character", start.get("character", 0))) + 1,
        }

    def _public_diagnostic(self, uri: str, diagnostic: dict[str, Any]) -> dict[str, Any]:
        range_value = diagnostic.get("range") or {}
        start = range_value.get("start") or {}
        end = range_value.get("end") or {}
        return {
            "path": self._uri_relative_path(uri),
            "line": int(start.get("line", 0)) + 1,
            "column": int(start.get("character", 0)) + 1,
            "end_line": int(end.get("line", start.get("line", 0))) + 1,
            "end_column": int(end.get("character", start.get("character", 0))) + 1,
            "severity": diagnostic.get("severity"),
            "code": diagnostic.get("code"),
            "source": diagnostic.get("source"),
            "message": diagnostic.get("message"),
            "tags": diagnostic.get("tags") or [],
        }

    def _uri_relative_path(self, uri: str) -> str | None:
        if self._client_root is None:
            return None
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        uri_path = f"//{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
        path = Path(url2pathname(uri_path)).resolve()
        try:
            return path.relative_to(self._client_root).as_posix()
        except ValueError:
            return None

    @staticmethod
    def _language_id(path: Path) -> str:
        return {".xbsl": "xbsl", ".xbql": "xbql", ".yaml": "yaml", ".yml": "yaml"}.get(path.suffix.lower(), "plaintext")

    @staticmethod
    def _runtime_public(runtime: LanguageServerRuntime) -> dict[str, Any]:
        return {
            "bundle_path": str(runtime.bundle_path),
            "product_version": runtime.product_version,
            "lsp_version": runtime.lsp_version,
            "java_path": str(runtime.java_path),
            "java_version": runtime.java_version,
            "configuration_source": runtime.source,
        }
