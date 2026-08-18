from __future__ import annotations

import html
import secrets
from functools import partial
from importlib import resources
from pathlib import Path

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from element_mcp.config import ConfigurationError, ServerSettings
from element_mcp.console import ConsoleConfigurationError, ConsoleRequestError, ConsoleService
from element_mcp.corpus import CorpusError
from element_mcp.documentation import DocumentationService
from element_mcp.project import ProjectError, ProjectService
from element_mcp.runtime import RuntimeConfigurationError, RuntimeDiagnosticsService
from element_mcp.updates import UpdateError, UpdateService


def _asset(name: str) -> str:
    return resources.files("element_mcp").joinpath("ui_assets", name).read_text(encoding="utf-8")


def register_ui(
    server,
    settings: ServerSettings,
    updates: UpdateService,
    documentation: DocumentationService,
    console: ConsoleService,
    project: ProjectService,
    runtime: RuntimeDiagnosticsService,
) -> None:
    allowed_hosts = {"127.0.0.1", "localhost", "::1", settings.host.lower()}

    def host_allowed(request: Request) -> bool:
        return (request.url.hostname or "").lower() in allowed_hosts

    def mutation_allowed(request: Request) -> bool:
        supplied = request.headers.get("x-element-mcp-token", "")
        return host_allowed(request) and secrets.compare_digest(supplied, updates.csrf_token)

    @server.custom_route("/", methods=["GET"], include_in_schema=False)
    async def index(request: Request) -> Response:
        if not host_allowed(request):
            return Response(status_code=403)
        document = _asset("index.html").replace("{{CSRF_TOKEN}}", html.escape(updates.csrf_token, quote=True))
        return HTMLResponse(
            document,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                    "form-action 'none'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @server.custom_route("/ui/styles.css", methods=["GET"], include_in_schema=False)
    async def styles(request: Request) -> Response:
        if not host_allowed(request):
            return Response(status_code=403)
        return Response(_asset("styles.css"), media_type="text/css", headers={"Cache-Control": "no-cache"})

    @server.custom_route("/ui/app.js", methods=["GET"], include_in_schema=False)
    async def script(request: Request) -> Response:
        if not host_allowed(request):
            return Response(status_code=403)
        return Response(
            _asset("app.js"),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @server.custom_route("/favicon.ico", methods=["GET"], include_in_schema=False)
    async def favicon(request: Request) -> Response:
        return Response(status_code=204 if host_allowed(request) else 403)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        if not host_allowed(request):
            return Response(status_code=403)
        return JSONResponse({"status": "ok", "version": updates.status()["server"]["version"]})

    @server.custom_route("/api/status", methods=["GET"], include_in_schema=False)
    async def status(request: Request) -> Response:
        if not host_allowed(request):
            return Response(status_code=403)
        return JSONResponse(updates.status(), headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/updates/check", methods=["POST"], include_in_schema=False)
    async def check_updates(request: Request) -> Response:
        if not mutation_allowed(request):
            return JSONResponse({"message": "Недопустимый локальный запрос"}, status_code=403)
        result = await anyio.to_thread.run_sync(updates.check)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/updates/source", methods=["POST"], include_in_schema=False)
    async def configure_update_source(request: Request) -> Response:
        if not mutation_allowed(request):
            return JSONResponse({"message": "Недопустимый локальный запрос"}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"message": "Некорректная длина запроса"}, status_code=400)
        if content_length > 8192:
            return JSONResponse({"message": "Запрос слишком большой"}, status_code=413)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"message": "Некорректные параметры источника обновлений"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("path"), (str, type(None))):
            return JSONResponse({"message": "Путь должен быть строкой или null"}, status_code=400)
        try:
            result = await anyio.to_thread.run_sync(updates.configure_source, payload["path"])
        except UpdateError as error:
            return JSONResponse({"message": str(error)}, status_code=400)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/updates/apply", methods=["POST"], include_in_schema=False)
    async def apply_update(request: Request) -> Response:
        if not mutation_allowed(request):
            return JSONResponse({"message": "Недопустимый локальный запрос"}, status_code=403)
        try:
            result = await anyio.to_thread.run_sync(updates.apply)
        except UpdateError as error:
            return JSONResponse({"message": str(error)}, status_code=409)
        return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/documentation", methods=["GET"], include_in_schema=False)
    async def documentation_status(request: Request) -> Response:
        if not host_allowed(request):
            return Response(status_code=403)
        try:
            result = await anyio.to_thread.run_sync(documentation.documentation_status)
        except (ConfigurationError, OSError) as error:
            return JSONResponse(
                {"status": "unavailable", "message": f"Не удалось прочитать настройку документации: {error}"},
                status_code=500,
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/documentation/activate", methods=["POST"], include_in_schema=False)
    async def activate_documentation(request: Request) -> Response:
        if not mutation_allowed(request):
            return JSONResponse({"message": "Недопустимый локальный запрос"}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"message": "Некорректная длина запроса"}, status_code=400)
        if content_length <= 0 or content_length > 8192:
            return JSONResponse({"message": "Некорректный размер настройки документации"}, status_code=413)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"message": "Настройка документации должна быть JSON-объектом"}, status_code=400)
        corpus_path = payload.get("path") if isinstance(payload, dict) else None
        if not isinstance(corpus_path, str) or not corpus_path.strip():
            return JSONResponse({"message": "Укажите путь к нормализованной документации"}, status_code=400)
        corpus_path = corpus_path.strip()
        if len(corpus_path) > 4096:
            return JSONResponse({"message": "Путь к документации слишком длинный"}, status_code=400)
        if not Path(corpus_path).expanduser().is_absolute():
            return JSONResponse({"message": "Укажите полный путь к нормализованной документации"}, status_code=400)
        try:
            result = await anyio.to_thread.run_sync(documentation.activate, corpus_path)
        except CorpusError as error:
            return JSONResponse({"status": "invalid", "message": str(error)}, status_code=400)
        except (ConfigurationError, OSError) as error:
            return JSONResponse(
                {"status": "unavailable", "message": f"Не удалось сохранить путь к документации: {error}"},
                status_code=500,
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/console/configuration", methods=["GET", "POST"], include_in_schema=False)
    async def configure_console_connection(request: Request) -> Response:
        if request.method == "GET":
            if not host_allowed(request):
                return Response(status_code=403)
            return JSONResponse(console.persistent_configuration(), headers={"Cache-Control": "no-store"})
        if not mutation_allowed(request):
            return JSONResponse({"message": "Недопустимый локальный запрос"}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"message": "Некорректная длина запроса"}, status_code=400)
        if content_length <= 0 or content_length > 16 * 1024:
            return JSONResponse({"message": "Некорректный размер настроек Console"}, status_code=413)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"message": "Настройки Console должны быть JSON-объектом"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return JSONResponse({"message": "Поле enabled должно быть логическим"}, status_code=400)
        try:
            if not payload["enabled"]:
                result = await anyio.to_thread.run_sync(console.disable_persistent_connection)
            else:
                server_url = payload.get("server")
                client_id = payload.get("client_id")
                client_secret = payload.get("client_secret")
                if not isinstance(server_url, str) or not isinstance(client_id, str):
                    raise ConsoleConfigurationError("Укажите адрес сервера Element и Client ID")
                if client_secret is not None and not isinstance(client_secret, str):
                    raise ConsoleConfigurationError("Client Secret должен быть строкой")
                if max(len(server_url), len(client_id), len(client_secret or "")) > 8192:
                    raise ConsoleConfigurationError("Одно из полей настроек слишком длинное")
                operation = partial(
                    console.configure_persistent_connection,
                    server=server_url,
                    client_id=client_id,
                    client_secret=client_secret,
                )
                result = await anyio.to_thread.run_sync(operation)
        except ConsoleConfigurationError as error:
            return JSONResponse({"status": "invalid", "message": str(error)}, status_code=400)
        except ConsoleRequestError as error:
            status_code = error.status_code if error.status_code in {401, 403} else 502
            return JSONResponse(
                {"status": "rejected", "http_status": error.status_code, "message": str(error)},
                status_code=status_code,
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/runtime/configuration", methods=["GET", "POST"], include_in_schema=False)
    async def configure_runtime(request: Request) -> Response:
        if request.method == "GET":
            if not host_allowed(request):
                return Response(status_code=403)
            try:
                result = await anyio.to_thread.run_sync(runtime.configuration_info)
            except RuntimeConfigurationError as error:
                return JSONResponse({"status": "invalid", "message": str(error)}, status_code=500)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        if not mutation_allowed(request):
            return JSONResponse({"message": "Недопустимый локальный запрос"}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"message": "Некорректная длина запроса"}, status_code=400)
        if content_length <= 0 or content_length > 24 * 1024:
            return JSONResponse({"message": "Некорректный размер настроек диагностики"}, status_code=413)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"message": "Настройки диагностики должны быть JSON-объектом"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("application_manager_enabled"), bool):
            return JSONResponse({"message": "Некорректные настройки диагностики"}, status_code=400)
        string_fields = ("instance_root", "server", "username", "password", "api_version")
        if any(payload.get(key) is not None and not isinstance(payload.get(key), str) for key in string_fields):
            return JSONResponse({"message": "Текстовые поля диагностики должны быть строками"}, status_code=400)
        if "verify_tls" in payload and not isinstance(payload["verify_tls"], bool):
            return JSONResponse({"message": "Поле verify_tls должно быть логическим"}, status_code=400)
        if max((len(payload.get(key) or "") for key in string_fields), default=0) > 8192:
            return JSONResponse({"message": "Одно из полей настроек слишком длинное"}, status_code=400)
        try:
            operation = partial(
                runtime.configure,
                instance_root=payload.get("instance_root", ""),
                application_manager_enabled=payload["application_manager_enabled"],
                server=payload.get("server"),
                username=payload.get("username"),
                password=payload.get("password"),
                api_version=payload.get("api_version", "auto"),
                verify_tls=payload.get("verify_tls", True),
            )
            result = await anyio.to_thread.run_sync(operation)
        except RuntimeConfigurationError as error:
            return JSONResponse({"status": "invalid", "message": str(error)}, status_code=400)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/integrations/element-console", methods=["POST", "DELETE"], include_in_schema=False)
    async def configure_element_console(request: Request) -> Response:
        if not mutation_allowed(request):
            return JSONResponse({"message": "Недопустимый локальный запрос"}, status_code=403)
        if request.method == "DELETE":
            return JSONResponse(
                {
                    "console": console.clear_ide_session(),
                    "workspace": project.clear_ide_workspace(),
                },
                headers={"Cache-Control": "no-store"},
            )
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"message": "Некорректная длина запроса"}, status_code=400)
        if content_length <= 0 or content_length > 64 * 1024:
            return JSONResponse({"message": "Некорректный размер контекста IDE"}, status_code=413)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"message": "Контекст IDE должен быть JSON-объектом"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"message": "Контекст IDE должен быть JSON-объектом"}, status_code=400)
        try:
            result = await anyio.to_thread.run_sync(console.configure_ide_session, payload)
        except ConsoleConfigurationError as error:
            return JSONResponse({"status": "invalid", "message": str(error)}, status_code=400)
        except ConsoleRequestError as error:
            status_code = error.status_code if error.status_code in {401, 403} else 502
            return JSONResponse(
                {"status": "rejected", "http_status": error.status_code, "message": str(error)},
                status_code=status_code,
            )
        workspace = None
        if "workspace_folders" in payload:
            try:
                prepared_workspace = await anyio.to_thread.run_sync(project.prepare_ide_workspace, payload)
                workspace = project.activate_ide_workspace(prepared_workspace)
            except ProjectError as error:
                workspace = {
                    "status": "invalid",
                    "message": str(error),
                    "source": "ide_session",
                }
        return JSONResponse(
            {**result, **({"workspace": workspace} if workspace is not None else {})},
            headers={"Cache-Control": "no-store"},
        )
