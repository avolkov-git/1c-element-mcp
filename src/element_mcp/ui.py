from __future__ import annotations

import html
import secrets
from importlib import resources

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from element_mcp.config import ServerSettings
from element_mcp.updates import UpdateError, UpdateService


def _asset(name: str) -> str:
    return resources.files("element_mcp").joinpath("ui_assets", name).read_text(encoding="utf-8")


def register_ui(server, settings: ServerSettings, updates: UpdateService) -> None:
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

    @server.custom_route("/api/updates/apply", methods=["POST"], include_in_schema=False)
    async def apply_update(request: Request) -> Response:
        if not mutation_allowed(request):
            return JSONResponse({"message": "Недопустимый локальный запрос"}, status_code=403)
        try:
            result = await anyio.to_thread.run_sync(updates.apply)
        except UpdateError as error:
            return JSONResponse({"message": str(error)}, status_code=409)
        return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})
