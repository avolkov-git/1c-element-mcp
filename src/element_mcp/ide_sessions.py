"""Per-IDE capability sessions. Tokens never enter tool arguments or persisted settings."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PROTOCOL_VERSION = 1
SESSION_HEADER = "X-Element-IDE-Session"
CONTROL_HEADER = "X-Element-IDE-Control"
LEASE_SECONDS = 180
MAX_SESSIONS = 32


class IdeSessionError(ValueError):
    pass


class IdeSessionRegistrationError(IdeSessionError):
    """Safe, locally authored diagnostics; never include upstream bodies or credentials."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = 400,
        console_http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.console_http_status = console_http_status

    def public_info(self) -> dict[str, Any]:
        return {
            "status": "invalid" if self.status_code == 400 else "rejected",
            "code": self.code,
            "http_status": self.console_http_status,
            "message": str(self),
        }


@dataclass
class IdeSession:
    session_id: str
    project_id: str
    application_id: str
    workspace_folders: tuple[str, ...]
    expires_at: float
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    bundle: Any = field(default=None, repr=False)
    revoked: bool = False
    lock: Any = field(default_factory=threading.RLock, repr=False)

    def require_active(self) -> None:
        if self.revoked or self.clock() >= self.expires_at:
            raise IdeSessionError("IDE-сессия истекла или отозвана. Переподключите плагин к MCP.")

    @contextmanager
    def guard(self) -> Iterator[None]:
        # Revoke waits for a dispatched write; it cannot undo an operation already sent to Console.
        with self.lock:
            self.require_active()
            yield

    def public_info(self) -> dict[str, Any]:
        with self.lock:
            self.require_active()
            return {
                "status": "ready",
                "session_id": self.session_id,
                "project_id": self.project_id,
                "application_id": self.application_id,
                "workspace_folders": list(self.workspace_folders),
                "expires_in_seconds": max(0, int(self.expires_at - self.clock())),
            }

    def close(self) -> None:
        with self.lock:
            self.revoked = True
            if self.bundle is not None:
                self.bundle.actions.invalidate_approvals()
                self.bundle.language_server.close()


current_ide_session: ContextVar[IdeSession | None] = ContextVar("element_ide_session", default=None)


class RoutedService:
    """Select an isolated service graph using transport context, never model-supplied IDs."""

    def __init__(self, name: str, fallback: Any) -> None:
        self.name = name
        self.fallback = fallback

    def __getattr__(self, name: str) -> Any:
        session = current_ide_session.get()
        if session is None:
            return getattr(self.fallback, name)
        session.require_active()
        value = getattr(getattr(session.bundle, self.name), name)
        if not callable(value):
            return value

        @wraps(value)
        def guarded(*args, **kwargs):
            # Keep lifecycle cleanup from closing/restarting LSP underneath an in-flight service call.
            with session.guard():
                return value(*args, **kwargs)

        return guarded


def _digest(token: str) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
        raise IdeSessionError("Некорректный токен IDE-сессии")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class IdeSessionRegistry:
    def __init__(self, factory: Callable[[IdeSession, Mapping[str, Any]], Any], *, clock=time.monotonic) -> None:
        self.factory = factory
        self.clock = clock
        self._sessions: dict[str, tuple[IdeSession, str, str]] = {}
        self._lock = threading.RLock()

    def reap(self) -> None:
        with self._lock:
            candidates = [
                (key, session)
                for key, (session, _, _) in self._sessions.items()
                if session.revoked or self.clock() >= session.expires_at
            ]
        for key, session in candidates:
            # Serialize the expiry decision with heartbeat renewal, without blocking the whole registry.
            with session.lock:
                if not session.revoked and self.clock() < session.expires_at:
                    continue
                with self._lock:
                    self._sessions.pop(key, None)
                session.close()

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if type(payload.get("protocol_version")) is not int or payload["protocol_version"] != PROTOCOL_VERSION:
            raise IdeSessionError("Требуется protocol_version=1; обновите плагин")
        allowed = {
            "protocol_version",
            "server",
            "client_id",
            "client_secret",
            "access_token",
            "project_id",
            "application_id",
            "workspace_folders",
            "git_status",
            "space_id",
            "verify_tls",
            "ca_bundle",
        }
        if set(payload) - allowed:
            raise IdeSessionError("Неизвестные поля IDE-контекста")
        try:
            project_id = str(uuid.UUID(payload["project_id"]))
            application_id = str(uuid.UUID(payload["application_id"]))
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise IdeSessionError("IDE должна передать корректные project_id и application_id") from error
        folders = payload.get("workspace_folders")
        if not isinstance(folders, list) or not folders or any(not isinstance(p, str) or not p for p in folders):
            raise IdeSessionError("IDE должна передать непустой workspace_folders")
        self.reap()
        session = IdeSession(
            session_id=str(uuid.uuid4()),
            project_id=project_id,
            application_id=application_id,
            workspace_folders=tuple(folders),
            expires_at=self.clock() + LEASE_SECONDS,
            clock=self.clock,
        )
        # Fully validate before publishing; no existing session is replaced on a bad handoff.
        session.bundle = self.factory(session, {**payload, "project_id": project_id, "application_id": application_id})
        session.expires_at = self.clock() + LEASE_SECONDS
        mcp_token, control_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                session.close()
                raise IdeSessionError("Достигнут лимит IDE-сессий; закройте неиспользуемые подключения")
            self._sessions[session.session_id] = (session, _digest(mcp_token), _digest(control_token))
        return {
            "protocol_version": PROTOCOL_VERSION,
            **session.public_info(),
            "mcp_header": SESSION_HEADER,
            "mcp_token": mcp_token,
            "control_header": CONTROL_HEADER,
            "control_token": control_token,
            "lease_seconds": LEASE_SECONDS,
            "renew_after_seconds": 60,
        }

    def for_mcp(self, token: str) -> IdeSession:
        digest = _digest(token)
        with self._lock:
            found = next(
                (session for session, value, _ in self._sessions.values() if secrets.compare_digest(digest, value)),
                None,
            )
        if found is None:
            raise IdeSessionError("IDE-сессия неизвестна; переподключите плагин")
        found.require_active()
        return found

    def controlled(self, session_id: str, token: str) -> IdeSession:
        digest = _digest(token)
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is None or not secrets.compare_digest(digest, entry[2]):
            raise IdeSessionError("IDE-сессия неизвестна или управляющий токен неверен")
        entry[0].require_active()
        return entry[0]

    def renew(self, session_id: str, token: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) - {"git_status"}:
            raise IdeSessionError("Контекст сессии неизменяемый; создайте новую сессию для другого workspace")
        session = self.controlled(session_id, token)
        with session.guard():
            if "git_status" in payload:
                session.bundle.project.refresh_ide_git_status(payload["git_status"])
            session.expires_at = self.clock() + LEASE_SECONDS
            return {**session.public_info(), "lease_seconds": LEASE_SECONDS, "renew_after_seconds": 60}

    def revoke(self, session_id: str, token: str) -> dict[str, Any]:
        session = self.controlled(session_id, token)
        session.close()
        with self._lock:
            self._sessions.pop(session_id, None)
        return {"status": "revoked", "session_id": session_id}

    def close(self) -> None:
        with self._lock:
            sessions = [value[0] for value in self._sessions.values()]
            self._sessions.clear()
        for session in sessions:
            session.close()


class IdeSessionMiddleware:
    def __init__(self, app: ASGIApp, registry: IdeSessionRegistry, mcp_path: str) -> None:
        self.app, self.registry, self.mcp_path = app, registry, mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != self.mcp_path.rstrip("/"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        values = request.headers.getlist(SESSION_HEADER)
        try:
            if len(values) > 1:
                raise IdeSessionError("Повторяющийся заголовок IDE-сессии")
            session = self.registry.for_mcp(values[0]) if values else None
        except IdeSessionError as error:
            await JSONResponse(
                {"status": "ide_session_required", "message": str(error)},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        marker = current_ide_session.set(session)
        try:
            await self.app(scope, receive, send)
        finally:
            current_ide_session.reset(marker)


class SessionFastMCP(FastMCP):
    ide_sessions: IdeSessionRegistry

    def streamable_http_app(self):
        app = super().streamable_http_app()
        original_lifespan = app.router.lifespan_context

        async def cleanup():
            while True:
                await anyio.sleep(30)
                await anyio.to_thread.run_sync(self.ide_sessions.reap)

        @asynccontextmanager
        async def lifespan(application):
            try:
                async with original_lifespan(application), anyio.create_task_group() as group:
                    group.start_soon(cleanup)
                    try:
                        yield
                    finally:
                        group.cancel_scope.cancel()
            finally:
                self.ide_sessions.close()

        app.router.lifespan_context = lifespan
        app.add_middleware(
            IdeSessionMiddleware,
            registry=self.ide_sessions,
            mcp_path=self.settings.streamable_http_path,
        )
        return app
