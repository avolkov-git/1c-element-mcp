from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from element_mcp.actions import ManagedActionsService
from element_mcp.config import ServerSettings
from element_mcp.console import ConsoleContextResolver, ConsoleService
from element_mcp.documentation import DocumentationService
from element_mcp.graph import ProjectGraphService
from element_mcp.ide_sessions import IdeSession, IdeSessionError
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
        console.configure_ide_session(payload)
        current = console.get_current_application()
        if current.get("status") != "ready" or current.get("matches_ide_project") is not True:
            raise IdeSessionError("Console не подтвердила связь application_id с project_id текущей IDE")
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
