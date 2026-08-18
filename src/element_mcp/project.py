from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from .config import ConfigurationStore, ServerSettings

ProjectFileType = Literal["all", "metadata", "xbsl", "xbql"]

ROOT_MANIFESTS = ("Project.yaml", "Проект.yaml")
SUBSYSTEM_MANIFESTS = ("Subsystem.yaml", "Подсистема.yaml")
RESOURCE_MANIFESTS = ("Resources.yaml", "Ресурсы.yaml")
RESOURCE_DIRECTORIES = frozenset({"resources", "ресурсы"})
TEXT_SOURCE_EXTENSIONS = frozenset({".yaml", ".yml", ".xbsl", ".xbql"})
SEARCH_EXTENSIONS = {
    "all": TEXT_SOURCE_EXTENSIONS,
    "metadata": frozenset({".yaml", ".yml"}),
    "xbsl": frozenset({".xbsl"}),
    "xbql": frozenset({".xbql"}),
}
IGNORED_DIRECTORIES = frozenset({".git", ".idea", ".theia", ".vscode", "node_modules"})
MAX_FILES = 50_000
MAX_TEXT_BYTES = 2_000_000
MAX_WORKSPACE_DIRECTORIES = 10_000
MAX_WORKSPACE_PROJECTS = 100
MAX_WORKSPACE_FOLDERS = 16
TOP_LEVEL_SCALAR = re.compile(r"^([^\s:#][^:]*):(?:\s*(.*))?$")


class ProjectError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IdeWorkspaceContext:
    workspace_folders: tuple[Path, ...]
    project_id: str | None
    candidates: tuple[Path, ...]
    selected_path: Path | None
    git_status: dict[str, Any] | None

    def identity(self) -> tuple[tuple[Path, ...], str | None]:
        return self.workspace_folders, self.project_id


def _display_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _top_level_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = TOP_LEVEL_SCALAR.match(line)
        if match is None:
            continue
        key = match.group(1).strip()
        raw_value = (match.group(2) or "").strip()
        if raw_value:
            values[key] = _display_scalar(raw_value)
    return values


def _first(values: dict[str, str], *keys: str) -> str | None:
    return next((values[key] for key in keys if values.get(key)), None)


def _read_source_text(path: Path) -> tuple[str, bytes]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ProjectError(f"Не удалось прочитать файл проекта {path}: {error}") from error
    if len(payload) > MAX_TEXT_BYTES:
        raise ProjectError(f"Файл проекта слишком велик для чтения через MCP: {path} ({len(payload)} байт)")
    if b"\x00" in payload:
        raise ProjectError(f"Файл проекта не является текстовым: {path}")
    try:
        return payload.decode("utf-8-sig"), payload
    except UnicodeDecodeError as error:
        raise ProjectError(f"Файл проекта не имеет кодировку UTF-8: {path}") from error


def _manifest_path(root: Path) -> Path:
    manifests = [root / name for name in ROOT_MANIFESTS if (root / name).is_file()]
    if not manifests:
        raise ProjectError(
            f"В каталоге не найден Project.yaml или Проект.yaml: {root}. "
            "Укажите корень исходного проекта 1С:Предприятие.Элемент."
        )
    if len(manifests) > 1:
        raise ProjectError(f"В корне одновременно найдены два манифеста проекта: {root}")
    return manifests[0]


def _project_metadata(root: Path) -> dict[str, Any]:
    manifest = _manifest_path(root)
    text, _ = _read_source_text(manifest)
    values = _top_level_values(text)
    return {
        "manifest": manifest.name,
        "name": _first(values, "Name", "Имя"),
        "presentation": _first(values, "Presentation", "Представление"),
        "id": _first(values, "Id", "Ид"),
        "version": _first(values, "Version", "Версия"),
        "vendor": _first(values, "Vendor", "Поставщик"),
        "development_language": _first(values, "DevelopmentLanguage", "ЯзыкРазработки"),
        "default_language": _first(values, "DefaultLanguage", "ЯзыкПоУмолчанию"),
    }


def _is_resource_payload(relative_path: str) -> bool:
    path = Path(relative_path)
    return path.name not in RESOURCE_MANIFESTS and any(
        part.casefold() in RESOURCE_DIRECTORIES for part in path.parts[:-1]
    )


def _is_element_metadata(path: Path, root: Path) -> bool:
    if _is_resource_payload(_relative(root, path)):
        return False
    if path.name in (*ROOT_MANIFESTS, *SUBSYSTEM_MANIFESTS, *RESOURCE_MANIFESTS):
        return True
    try:
        text, _ = _read_source_text(path)
    except ProjectError:
        return False
    values = _top_level_values(text)
    return _first(values, "ElementKind", "ВидЭлемента") is not None


def _iter_files(root: Path):
    count = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in IGNORED_DIRECTORIES and not name.startswith(".") and not (current / name).is_symlink()
        )
        for name in sorted(file_names):
            path = current / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            count += 1
            if count > MAX_FILES:
                raise ProjectError(f"В проекте больше {MAX_FILES} файлов; уточните корень проекта")
            yield path


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _subsystem_for(path: Path, root: Path, subsystem_directories: set[Path]) -> str | None:
    current = path.parent
    while current != root and root in current.parents:
        if current in subsystem_directories:
            return _relative(root, current)
        current = current.parent
    return None


class ProjectService:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.configuration = ConfigurationStore(settings.resolved_config_path)
        self._ide_context: IdeWorkspaceContext | None = None
        self._ide_lock = threading.RLock()

    def active_path(self) -> Path | None:
        fixed = self._fixed_path()
        if fixed is not None:
            return fixed
        with self._ide_lock:
            if self._ide_context is not None:
                return self._ide_context.selected_path
        return self.configuration.active_project_path()

    def project_status(self) -> dict[str, Any]:
        path = self.active_path()
        ide_context = self.ide_workspace_context()
        if path is None:
            if ide_context is not None and ide_context["candidate_count"] > 1:
                return {
                    "status": "selection_required",
                    "message": "В workspace IDE найдено несколько проектов Element; выберите корень",
                    "path": None,
                    "source": "ide_session",
                    "ide_context": ide_context,
                }
            return {
                "status": "missing",
                "message": "Проект 1С:Предприятие.Элемент не подключён",
                "path": None,
                "source": "ide_session" if ide_context is not None else None,
                **({"ide_context": ide_context} if ide_context is not None else {}),
            }
        try:
            root = self._validated_root(path)
            metadata = _project_metadata(root)
        except ProjectError as error:
            return {
                "status": "invalid",
                "message": "Подключённый каталог не прошёл проверку проекта Element",
                "path": str(path),
                "error": str(error),
            }
        return {
            "status": "ready",
            "message": "Проект 1С:Предприятие.Элемент подключён и доступен только для чтения",
            "path": str(root),
            "source": self._active_source(),
            "project": metadata,
            **({"ide_context": ide_context} if ide_context is not None else {}),
        }

    def connect(self, project_path: str | Path) -> dict[str, Any]:
        supplied = Path(project_path).expanduser()
        if supplied.is_file() and supplied.name in ROOT_MANIFESTS:
            supplied = supplied.parent
        root = self._validated_root(supplied.resolve())
        fixed_path = self.settings.resolved_project_path or (
            Path(value).expanduser().resolve() if (value := os.environ.get("ELEMENT_PROJECT_PATH")) else None
        )
        if fixed_path is not None and root != fixed_path:
            raise ProjectError(
                "Путь проекта зафиксирован параметром --project-path или ELEMENT_PROJECT_PATH. "
                "Измените параметр и перезапустите MCP."
            )
        metadata = _project_metadata(root)
        source = "configuration"
        with self._ide_lock:
            if self._ide_context is not None and self._fixed_path() is None:
                if root not in self._ide_context.candidates:
                    raise ProjectError(
                        "Выбранный корень не входит в текущий workspace Element IDE. "
                        "Используйте один из путей, возвращённых get_project_status."
                    )
                self._ide_context = replace(self._ide_context, selected_path=root)
                source = "ide_session"
            else:
                self.configuration.connect_project(root, metadata=metadata)
        return {
            "status": "ready",
            "message": "Проект проверен и подключён к MCP в режиме только для чтения",
            "path": str(root),
            "source": source,
            "project": metadata,
            **({"ide_context": self.ide_workspace_context()} if source == "ide_session" else {}),
        }

    def prepare_ide_workspace(self, values: Mapping[str, Any]) -> IdeWorkspaceContext:
        raw_folders = values.get("workspace_folders")
        if not isinstance(raw_folders, list) or len(raw_folders) > MAX_WORKSPACE_FOLDERS:
            raise ProjectError(f"workspace_folders должен быть массивом не более {MAX_WORKSPACE_FOLDERS} путей")
        folders: list[Path] = []
        for raw_folder in raw_folders:
            if not isinstance(raw_folder, str) or not raw_folder.strip() or len(raw_folder) > 4096:
                raise ProjectError("Каждый workspace folder должен быть непустым локальным путём")
            folder = Path(raw_folder).expanduser().resolve()
            if not folder.is_dir():
                raise ProjectError(f"Workspace folder не существует или недоступен серверу MCP: {folder}")
            if folder not in folders:
                folders.append(folder)

        project_id = values.get("project_id")
        if project_id is not None and (not isinstance(project_id, str) or len(project_id.strip()) > 64):
            raise ProjectError("project_id контекста IDE должен быть строкой не длиннее 64 символов")
        candidates = tuple(self._discover_project_roots(tuple(folders)))
        selected = candidates[0] if len(candidates) == 1 else None
        return IdeWorkspaceContext(
            workspace_folders=tuple(folders),
            project_id=project_id.strip() if isinstance(project_id, str) and project_id.strip() else None,
            candidates=candidates,
            selected_path=selected,
            git_status=self._sanitize_ide_git_status(values.get("git_status")),
        )

    def activate_ide_workspace(self, context: IdeWorkspaceContext) -> dict[str, Any]:
        with self._ide_lock:
            previous = self._ide_context
            if (
                context.selected_path is None
                and previous is not None
                and previous.identity() == context.identity()
                and previous.selected_path in context.candidates
            ):
                context = replace(context, selected_path=previous.selected_path)
            self._ide_context = context
        return self.ide_workspace_context() or {}

    def clear_ide_workspace(self) -> dict[str, Any]:
        with self._ide_lock:
            self._ide_context = None
        return {"status": "cleared", "message": "Временный workspace-контекст IDE отключён"}

    def ide_workspace_context(self) -> dict[str, Any] | None:
        with self._ide_lock:
            context = self._ide_context
        if context is None:
            return None
        candidates = [self._candidate_payload(path) for path in context.candidates]
        status = "ready" if context.selected_path else "selection_required" if candidates else "missing"
        return {
            "status": status,
            "source": "ide_session",
            "project_id": context.project_id,
            "workspace_folders": [str(path) for path in context.workspace_folders],
            "candidate_count": len(candidates),
            "candidates": candidates,
            "selected_path": str(context.selected_path) if context.selected_path else None,
            "git": context.git_status,
        }

    def workspace_projects(self, workspace_path: str | Path | None = None) -> dict[str, Any]:
        if workspace_path is None:
            ide_context = self.ide_workspace_context()
            if ide_context is not None:
                return {"status": "ready", **ide_context}
            if self.settings.transport != "stdio":
                return {
                    "status": "path_required",
                    "message": "Для HTTP MCP укажите локальный путь workspace, доступный серверу",
                    "candidate_count": 0,
                    "candidates": [],
                }
            roots = (Path.cwd().resolve(),)
            source = "process_working_directory"
        else:
            root = Path(workspace_path).expanduser().resolve()
            if not root.is_dir():
                raise ProjectError(f"Workspace не существует или недоступен: {root}")
            roots = (root,)
            source = "explicit_workspace"
        candidates = self._discover_project_roots(roots)
        return {
            "status": "ready" if candidates else "missing",
            "source": source,
            "workspace_folders": [str(path) for path in roots],
            "candidate_count": len(candidates),
            "candidates": [self._candidate_payload(path) for path in candidates],
            "selected_path": None,
            "git": None,
            "message": (
                "Найдены проекты Element в workspace"
                if candidates
                else "В указанном workspace не найдены Project.yaml или Проект.yaml"
            ),
        }

    def match_console_project(
        self,
        console_project: Mapping[str, Any],
        workspace_path: str | Path | None = None,
    ) -> dict[str, Any]:
        workspace = self.workspace_projects(workspace_path)
        if workspace.get("status") not in {"ready", "missing"}:
            return {"status": workspace["status"], "console_project": dict(console_project), "workspace": workspace}

        names = {
            str(value).strip().casefold()
            for value in (console_project.get("name"), console_project.get("presentation"), console_project.get("code"))
            if isinstance(value, str) and value.strip()
        }
        candidates = workspace.get("candidates", [])
        exact_matches = []
        for candidate in candidates:
            metadata = candidate.get("project", {})
            local_names = {
                str(value).strip().casefold()
                for value in (metadata.get("name"), metadata.get("presentation"), Path(candidate["path"]).name)
                if isinstance(value, str) and value.strip()
            }
            if names & local_names:
                exact_matches.append(candidate)

        selected_path = workspace.get("selected_path")
        if selected_path:
            suggestion = next((item for item in candidates if item.get("path") == selected_path), None)
            return {
                "status": "ready",
                "message": "Проект Console связан с выбранным корнем текущей IDE-сессии",
                "console_project": dict(console_project),
                "workspace": workspace,
                "suggestion": suggestion,
                "match_reason": "ide_session_selection",
                "confirmation_required": False,
            }
        if len(exact_matches) == 1:
            suggestion = exact_matches[0]
            reason = "exact_name"
        elif len(candidates) == 1:
            suggestion = candidates[0]
            reason = "sole_candidate"
        else:
            suggestion = None
            reason = None
        return {
            "status": "confirmation_required" if suggestion else "selection_required",
            "message": (
                "Найден вероятный локальный корень; подтвердите его перед подключением"
                if suggestion
                else "Выберите локальный проект для связи с проектом Console"
            ),
            "console_project": dict(console_project),
            "workspace": workspace,
            "suggestion": suggestion,
            "match_reason": reason,
            "confirmation_required": True,
        }

    def overview(self) -> dict[str, Any]:
        root = self._required_root()
        files = list(_iter_files(root))
        elements, issues = self._elements(root, files)
        extensions = Counter(path.suffix.lower() or "[no-extension]" for path in files)
        kinds = Counter(element["element_kind"] for element in elements)
        subsystems = []
        for element in elements:
            if element["element_kind"] != "Subsystem":
                continue
            parent = Path(element["metadata_path"]).parent.as_posix()
            subsystems.append({"name": element["name"], "path": "" if parent == "." else parent})
        paired = sum(bool(element["implementation_files"]) for element in elements)
        return {
            "status": "ready",
            "path": str(root),
            "project": _project_metadata(root),
            "files": {
                "total": len(files),
                "by_extension": dict(sorted(extensions.items())),
                "readable_source_extensions": sorted(TEXT_SOURCE_EXTENSIONS),
            },
            "elements": {
                "total": len(elements),
                "with_implementation": paired,
                "metadata_only": len(elements) - paired,
                "by_kind": dict(sorted(kinds.items(), key=lambda item: (-item[1], item[0]))),
            },
            "subsystems": subsystems,
            "issues": issues[:50],
            "issues_truncated": len(issues) > 50,
        }

    def list_elements(
        self,
        *,
        query: str | None = None,
        element_kind: str | None = None,
        subsystem: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        root = self._required_root()
        elements, issues = self._elements(root, list(_iter_files(root)))
        normalized_query = query.casefold().strip() if query else None
        normalized_kind = element_kind.casefold().strip() if element_kind else None
        normalized_subsystem = subsystem.casefold().strip() if subsystem else None

        filtered: list[dict[str, Any]] = []
        for element in elements:
            if normalized_kind and element["element_kind"].casefold() != normalized_kind:
                continue
            if normalized_subsystem and (element["subsystem"] or "").casefold() != normalized_subsystem:
                continue
            searchable = " ".join(
                str(element.get(field) or "") for field in ("name", "element_kind", "id", "metadata_path", "subsystem")
            ).casefold()
            if normalized_query and normalized_query not in searchable:
                continue
            filtered.append(element)

        page = filtered[offset : offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(filtered) else None
        return {
            "status": "ready",
            "query": query,
            "element_kind": element_kind,
            "subsystem": subsystem,
            "total": len(filtered),
            "count": len(page),
            "offset": offset,
            "next_offset": next_offset,
            "elements": page,
            "issues": issues[:20],
            "issues_truncated": len(issues) > 20,
        }

    def search(
        self,
        query: str,
        *,
        file_type: ProjectFileType = "all",
        case_sensitive: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        root = self._required_root()
        needle = query if case_sensitive else query.casefold()
        results: list[dict[str, Any]] = []
        files_scanned = 0
        skipped_files: list[str] = []

        for path in _iter_files(root):
            if path.suffix.lower() not in SEARCH_EXTENSIONS[file_type]:
                continue
            if path.suffix.lower() in {".yaml", ".yml"} and not _is_element_metadata(path, root):
                continue
            try:
                text, _ = _read_source_text(path)
            except ProjectError:
                skipped_files.append(_relative(root, path))
                continue
            files_scanned += 1
            for line_number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                column = haystack.find(needle)
                if column < 0:
                    continue
                compact_line = line if len(line) <= 1200 else line[:1199] + "…"
                results.append(
                    {
                        "path": _relative(root, path),
                        "line": line_number,
                        "column": column + 1,
                        "text": compact_line,
                    }
                )
                if len(results) >= limit:
                    return {
                        "status": "ready",
                        "query": query,
                        "file_type": file_type,
                        "case_sensitive": case_sensitive,
                        "count": len(results),
                        "truncated": True,
                        "files_scanned": files_scanned,
                        "skipped_files": skipped_files[:20],
                        "results": results,
                    }
        return {
            "status": "ready",
            "query": query,
            "file_type": file_type,
            "case_sensitive": case_sensitive,
            "count": len(results),
            "truncated": False,
            "files_scanned": files_scanned,
            "skipped_files": skipped_files[:20],
            "results": results,
        }

    def read_file(self, relative_path: str, *, start_line: int = 1, line_count: int = 200) -> dict[str, Any]:
        root = self._required_root()
        path = self._resolve_source_file(root, relative_path)
        text, payload = _read_source_text(path)
        lines = text.splitlines()
        start_index = min(start_line - 1, len(lines))
        selected = lines[start_index : start_index + line_count]
        end_line = start_index + len(selected)
        return {
            "status": "ready",
            "path": _relative(root, path),
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "truncated": end_line < len(lines),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "content": "\n".join(selected),
        }

    def _fixed_path(self) -> Path | None:
        if self.settings.resolved_project_path is not None:
            return self.settings.resolved_project_path
        configured = os.environ.get("ELEMENT_PROJECT_PATH")
        return Path(configured).expanduser().resolve() if configured else None

    def _active_source(self) -> str:
        if self._fixed_path() is not None:
            return "fixed_configuration"
        with self._ide_lock:
            if self._ide_context is not None:
                return "ide_session"
        return "configuration"

    @staticmethod
    def _candidate_payload(path: Path) -> dict[str, Any]:
        return {"path": str(path), "project": _project_metadata(path)}

    @staticmethod
    def _discover_project_roots(workspace_folders: tuple[Path, ...]) -> list[Path]:
        candidates: list[Path] = []
        directories_scanned = 0
        for workspace_root in workspace_folders:
            for directory, directory_names, file_names in os.walk(workspace_root, followlinks=False):
                directories_scanned += 1
                if directories_scanned > MAX_WORKSPACE_DIRECTORIES:
                    raise ProjectError(
                        f"Workspace содержит больше {MAX_WORKSPACE_DIRECTORIES} каталогов; укажите более точный путь"
                    )
                current = Path(directory)
                directory_names[:] = sorted(
                    name
                    for name in directory_names
                    if name not in IGNORED_DIRECTORIES
                    and not name.startswith(".")
                    and not (current / name).is_symlink()
                )
                if not any(name in file_names for name in ROOT_MANIFESTS):
                    continue
                resolved = current.resolve()
                try:
                    resolved.relative_to(workspace_root)
                except ValueError as error:
                    raise ProjectError("Обнаруженный проект находится за пределами workspace") from error
                ProjectService._validated_root(resolved)
                if resolved not in candidates:
                    candidates.append(resolved)
                if len(candidates) > MAX_WORKSPACE_PROJECTS:
                    raise ProjectError(
                        f"В workspace найдено больше {MAX_WORKSPACE_PROJECTS} проектов Element; "
                        "укажите более точный путь"
                    )
        return sorted(candidates, key=lambda path: str(path).casefold())

    @staticmethod
    def _sanitize_ide_git_status(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ProjectError("git_status контекста IDE должен быть объектом")
        result: dict[str, Any] = {"source": "g5rt.team.status"}
        string_fields = {
            "commit_id": "commitId",
            "branch_name": "branchName",
            "command_status": "commandStatus",
            "current_head": "currentHead",
        }
        for target, source in string_fields.items():
            raw = value.get(source, value.get(target))
            if raw is None:
                result[target] = None
            elif isinstance(raw, str) and len(raw) <= 512:
                result[target] = raw
            else:
                raise ProjectError(f"Некорректное поле git_status.{source}")
        modified = value.get("modified")
        if modified is not None and not isinstance(modified, bool):
            raise ProjectError("Некорректное поле git_status.modified")
        result["modified"] = modified
        ahead_behind = value.get("aheadBehind", value.get("ahead_behind"))
        if ahead_behind is None:
            result["ahead_behind"] = None
        elif isinstance(ahead_behind, Mapping):
            ahead = ahead_behind.get("ahead")
            behind = ahead_behind.get("behind")
            if not all(isinstance(item, int) and 0 <= item <= 1_000_000 for item in (ahead, behind)):
                raise ProjectError("Некорректное поле git_status.aheadBehind")
            result["ahead_behind"] = {"ahead": ahead, "behind": behind}
        else:
            raise ProjectError("Некорректное поле git_status.aheadBehind")
        return result

    def _required_root(self) -> Path:
        path = self.active_path()
        if path is None:
            raise ProjectError(
                "Проект Element не подключён. Вызовите get_project_status и попросите пользователя "
                "указать корень проекта."
            )
        return self._validated_root(path)

    @staticmethod
    def _validated_root(path: Path) -> Path:
        root = path.expanduser().resolve()
        if not root.is_dir():
            raise ProjectError(f"Каталог проекта не существует или недоступен: {root}")
        _project_metadata(root)
        return root

    @staticmethod
    def _resolve_source_file(root: Path, relative_path: str) -> Path:
        requested = Path(relative_path)
        if requested.is_absolute():
            raise ProjectError("read_project_file принимает только относительный путь внутри активного проекта")
        path = (root / requested).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ProjectError("Запрошенный файл находится за пределами активного проекта") from error
        if not path.is_file():
            raise ProjectError(f"Файл проекта не найден: {relative_path}")
        if path.suffix.lower() not in TEXT_SOURCE_EXTENSIONS:
            raise ProjectError("Через MCP можно читать только исходные файлы Element: .yaml, .yml, .xbsl и .xbql")
        if path.suffix.lower() in {".yaml", ".yml"} and not _is_element_metadata(path, root):
            raise ProjectError(f"YAML-файл не является метаданными элемента проекта: {relative_path}")
        return path

    @staticmethod
    def _elements(root: Path, files: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        yaml_files = [
            path
            for path in files
            if path.suffix.lower() in {".yaml", ".yml"} and not _is_resource_payload(_relative(root, path))
        ]
        subsystem_directories = {path.parent for path in yaml_files if path.name in SUBSYSTEM_MANIFESTS}
        source_files_by_directory: dict[Path, list[Path]] = {}
        for path in files:
            if path.suffix.lower() in {".xbsl", ".xbql"}:
                source_files_by_directory.setdefault(path.parent, []).append(path)

        elements: list[dict[str, Any]] = []
        issues: list[dict[str, str]] = []
        for path in yaml_files:
            try:
                text, _ = _read_source_text(path)
            except ProjectError as error:
                issues.append({"path": _relative(root, path), "error": str(error)})
                continue
            values = _top_level_values(text)
            if path.name in ROOT_MANIFESTS:
                kind = "Project"
            elif path.name in SUBSYSTEM_MANIFESTS:
                kind = "Subsystem"
            elif path.name in RESOURCE_MANIFESTS:
                kind = "Resources"
            else:
                kind = _first(values, "ElementKind", "ВидЭлемента")
                if kind is None:
                    continue

            stem = path.stem
            implementations = sorted(
                _relative(root, candidate)
                for candidate in source_files_by_directory.get(path.parent, [])
                if candidate.stem == stem or candidate.name.startswith(stem + ".")
            )
            subsystem = _subsystem_for(path, root, subsystem_directories)
            name = _first(values, "Name", "Имя")
            if name is None and kind == "Project":
                name = root.name
            elif name is None and kind in {"Subsystem", "Resources"}:
                name = path.parent.name

            elements.append(
                {
                    "name": name,
                    "element_kind": kind,
                    "id": _first(values, "Id", "Ид"),
                    "environment": _first(values, "Environment", "Окружение"),
                    "visibility_scope": _first(values, "VisibilityScope", "ОбластьВидимости"),
                    "subsystem": subsystem,
                    "metadata_path": _relative(root, path),
                    "implementation_files": implementations,
                }
            )
        elements.sort(key=lambda item: (item["metadata_path"].casefold(), item["element_kind"].casefold()))
        return elements, issues
