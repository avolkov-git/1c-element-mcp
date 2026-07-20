from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from .config import ConfigurationStore, ServerSettings, discover_project_path

ProjectFileType = Literal["all", "metadata", "xbsl", "xbql"]

ROOT_MANIFESTS = ("Project.yaml", "Проект.yaml")
SUBSYSTEM_MANIFESTS = ("Subsystem.yaml", "Подсистема.yaml")
RESOURCE_MANIFESTS = ("Resources.yaml", "Ресурсы.yaml")
TEXT_SOURCE_EXTENSIONS = frozenset({".yaml", ".yml", ".xbsl", ".xbql"})
SEARCH_EXTENSIONS = {
    "all": TEXT_SOURCE_EXTENSIONS,
    "metadata": frozenset({".yaml", ".yml"}),
    "xbsl": frozenset({".xbsl"}),
    "xbql": frozenset({".xbql"}),
}
IGNORED_DIRECTORIES = frozenset({".git", ".idea", ".vscode", "node_modules"})
MAX_FILES = 50_000
MAX_TEXT_BYTES = 2_000_000
TOP_LEVEL_SCALAR = re.compile(r"^([^\s:#][^:]*):(?:\s*(.*))?$")


class ProjectError(RuntimeError):
    pass


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


def _is_element_metadata(path: Path) -> bool:
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

    def active_path(self) -> Path | None:
        return discover_project_path(self.settings.project_path, config_store=self.configuration)

    def project_status(self) -> dict[str, Any]:
        path = self.active_path()
        if path is None:
            return {
                "status": "missing",
                "message": "Проект 1С:Предприятие.Элемент не подключён",
                "path": None,
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
            "project": metadata,
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
        self.configuration.connect_project(root, metadata=metadata)
        return {
            "status": "ready",
            "message": "Проект проверен и подключён к MCP в режиме только для чтения",
            "path": str(root),
            "project": metadata,
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
            if path.suffix.lower() in {".yaml", ".yml"} and not _is_element_metadata(path):
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
        if path.suffix.lower() in {".yaml", ".yml"} and not _is_element_metadata(path):
            raise ProjectError(f"YAML-файл не является метаданными элемента проекта: {relative_path}")
        return path

    @staticmethod
    def _elements(root: Path, files: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        yaml_files = [path for path in files if path.suffix.lower() in {".yaml", ".yml"}]
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
