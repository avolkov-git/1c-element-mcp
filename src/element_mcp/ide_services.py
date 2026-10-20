from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal

from element_mcp.actions import ManagedActionsService
from element_mcp.config import ServerSettings
from element_mcp.console import ConsoleContextResolver, ConsoleRequestError, ConsoleService
from element_mcp.documentation import DocumentationService
from element_mcp.graph import ProjectGraphService
from element_mcp.ide_sessions import IdeSession, IdeSessionRegistrationError
from element_mcp.language_server import LanguageServerService
from element_mcp.project import ProjectService
from element_mcp.runtime import RuntimeDiagnosticsService
from element_mcp.semantic import SemanticService


class BoundConsoleResolver(ConsoleContextResolver):
    def __init__(self, settings, connection, session: IdeSession):
        super().__init__(settings, environ={})
        self.connection, self.session = connection, session

    def resolve(self):
        self.session.require_active()
        return self.connection


def _console_read_error(
    stage: Literal["authentication", "application"], http_status: int | None
) -> IdeSessionRegistrationError:
    operation = (
        "проверить авторизацию IDE в Console" if stage == "authentication" else "прочитать приложение IDE в Console"
    )
    reason = {
        401: "Console отклонила авторизацию.",
        403: "Console запретила доступ.",
        404: "Console не нашла запрошенный ресурс.",
    }.get(http_status, "Проверьте доступ MCP к Console и корректность её ответа.")
    http_detail = f" (HTTP {http_status})" if http_status is not None else ""
    return IdeSessionRegistrationError(
        f"Не удалось {operation}{http_detail}. {reason}",
        code=f"console_{stage}_failed",
        status_code=http_status if http_status in {401, 403} else 502,
        console_http_status=http_status,
    )


@dataclass
class IdeServices:
    project: ProjectService
    graph: ProjectGraphService
    semantic: SemanticService
    language_server: LanguageServerService
    console: ConsoleService
    runtime: RuntimeDiagnosticsService
    actions: ManagedActionsService

    @classmethod
    def create(
        cls,
        settings: ServerSettings,
        documentation: DocumentationService,
        session: IdeSession,
        payload: dict[str, Any],
    ) -> IdeServices:
        project = ProjectService(settings, isolated_ide=True)
        context = project.prepare_ide_workspace(payload)
        project.activate_ide_workspace(context)
        session.workspace_folders = tuple(str(path) for path in context.workspace_folders)
        console = ConsoleService(settings)
        try:
            console.configure_ide_session(payload)
        except ConsoleRequestError as error:
            raise _console_read_error("authentication", error.status_code) from None
        current = console.get_current_application()
        if current.get("status") != "ready":
            raise _console_read_error("application", current.get("http_status"))
        project_id = current.get("application_project_id")
        if not project_id:
            raise IdeSessionRegistrationError(
                "В карточке приложения Console отсутствует ID проекта (project.id или source.image-id). "
                "Связь с проектом IDE не подтверждена.",
                code="application_project_missing",
            )
        try:
            uuid.UUID(project_id.strip())
        except (ValueError, TypeError, AttributeError):
            raise IdeSessionRegistrationError(
                "В карточке приложения Console указан некорректный UUID проекта. Связь с проектом IDE не подтверждена.",
                code="application_project_invalid",
            ) from None
        if current.get("matches_ide_project") is not True:
            raise IdeSessionRegistrationError(
                "Проект приложения из Console не совпадает с 1C.projectId текущей IDE. "
                "Проверьте соответствие 1C.applicationId и 1C.projectId.",
                code="application_project_mismatch",
            )
        console.resolver = BoundConsoleResolver(settings, console.resolver.resolve(), session)
        graph = ProjectGraphService(project)
        semantic = SemanticService(project, documentation)
        # Two windows using the same project still need separate LSP metadata directories.
        lsp_settings = replace(settings, data_path=settings.resolved_data_path / "ide-sessions" / session.session_id)
        language_server = LanguageServerService(lsp_settings, project, semantic)
        return cls(
            project,
            graph,
            semantic,
            language_server,
            console,
            RuntimeDiagnosticsService(settings, console),
            ManagedActionsService(settings, console, ide_session=session),
        )
