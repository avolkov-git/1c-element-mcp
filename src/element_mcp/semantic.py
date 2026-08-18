from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .corpus import CorpusName
from .documentation import DocumentationService
from .project import (
    RESOURCE_MANIFESTS,
    ROOT_MANIFESTS,
    SUBSYSTEM_MANIFESTS,
    TEXT_SOURCE_EXTENSIONS,
    ProjectError,
    ProjectFileType,
    ProjectService,
    _first,
    _iter_files,
    _read_source_text,
    _relative,
    _top_level_values,
)

IDENTIFIER = re.compile(r"[^\W\d]\w*", re.UNICODE)
DECLARATION = re.compile(
    r"^\s*(?:(?:static|abstract|async|export|override|"
    r"статический|абстрактный|асинх|экспорт|переопределение)\s+)*"
    r"(?P<keyword>method|метод|structure|структура|enum|перечисление|exception|исключение)"
    r"\s+(?P<name>[^\W\d]\w*)",
    re.IGNORECASE | re.UNICODE,
)
NAME_KEYS = frozenset({"Name", "Имя"})
ELEMENT_MANIFESTS = frozenset((*ROOT_MANIFESTS, *SUBSYSTEM_MANIFESTS, *RESOURCE_MANIFESTS))
KIND_ALIASES = {
    "method": "method",
    "метод": "method",
    "structure": "structure",
    "структура": "structure",
    "enum": "enumeration",
    "перечисление": "enumeration",
    "exception": "exception",
    "исключение": "exception",
}
TYPE_KINDS = frozenset({"structure", "enumeration", "exception"})


@dataclass(frozen=True)
class _Index:
    root: Path
    signature: tuple[tuple[str, int, int], ...]
    texts: dict[str, str]
    declarations: tuple[dict[str, Any], ...]
    file_elements: dict[str, dict[str, Any]]
    skipped_files: tuple[str, ...]


def _compact_line(line: str, maximum: int = 1200) -> str:
    return line if len(line) <= maximum else line[: maximum - 1] + "…"


def _public_element(element: dict[str, Any] | None) -> dict[str, Any] | None:
    if element is None:
        return None
    return {
        field: element.get(field)
        for field in (
            "name",
            "element_kind",
            "id",
            "environment",
            "visibility_scope",
            "subsystem",
            "metadata_path",
            "implementation_files",
        )
    }


def _symbol_id(path: str, line: int, column: int, kind: str, name: str) -> str:
    payload = f"{path}\0{line}\0{column}\0{kind}\0{name}".encode()
    return "sym:" + hashlib.sha256(payload).hexdigest()[:20]


def _declaration(
    *,
    name: str,
    symbol_kind: str,
    path: str,
    line: int,
    column: int,
    text: str,
    element: dict[str, Any] | None,
    source: str,
    basis: str,
) -> dict[str, Any]:
    return {
        "symbol_id": _symbol_id(path, line, column, symbol_kind, name),
        "name": name,
        "symbol_kind": symbol_kind,
        "declaration": {
            "path": path,
            "line": line,
            "column": column,
            "text": _compact_line(text),
        },
        "element": _public_element(element),
        "source": source,
        "confidence": "high",
        "basis": basis,
    }


def _metadata_name_declaration(
    path: str,
    text: str,
    element: dict[str, Any],
) -> dict[str, Any] | None:
    expected = element.get("name")
    if not expected:
        return None
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([^\s:#][^:]*):(?:\s*(.*))?$", line)
        if match is None or match.group(1).strip() not in NAME_KEYS:
            continue
        raw_value = (match.group(2) or "").strip()
        displayed = raw_value[1:-1] if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] else raw_value
        if displayed != expected:
            continue
        column = line.find(raw_value) + 1
        if raw_value[:1] in {'"', "'"}:
            column += 1
        return _declaration(
            name=expected,
            symbol_kind="element",
            path=path,
            line=line_number,
            column=column,
            text=line,
            element=element,
            source="metadata",
            basis="top-level Name/Имя of Element YAML metadata",
        )
    return None


def _code_masks(text: str) -> list[list[bool]]:
    """Mark XBSL/XBQL code while excluding strings and both comment forms."""
    masks: list[list[bool]] = []
    in_block_comment = False
    in_string = False
    interpolation_depth = 0
    interpolation_string = False
    for line in text.splitlines():
        mask = [True] * len(line)
        index = 0
        while index < len(line):
            if in_block_comment:
                mask[index] = False
                if line.startswith("*/", index):
                    if index + 1 < len(line):
                        mask[index + 1] = False
                    in_block_comment = False
                    index += 2
                else:
                    index += 1
                continue
            if interpolation_string:
                mask[index] = False
                if line[index] == "\\" and index + 1 < len(line):
                    mask[index + 1] = False
                    index += 2
                elif line[index] == '"':
                    interpolation_string = False
                    index += 1
                else:
                    index += 1
                continue
            if interpolation_depth:
                if line.startswith("//", index):
                    for comment_index in range(index, len(line)):
                        mask[comment_index] = False
                    break
                if line.startswith("/*", index):
                    mask[index] = False
                    if index + 1 < len(line):
                        mask[index + 1] = False
                    in_block_comment = True
                    index += 2
                    continue
                if line[index] == '"':
                    mask[index] = False
                    interpolation_string = True
                elif line[index] == "{":
                    interpolation_depth += 1
                elif line[index] == "}":
                    mask[index] = False
                    interpolation_depth -= 1
                index += 1
                continue
            if in_string:
                mask[index] = False
                if line[index] == "\\" and index + 1 < len(line):
                    mask[index + 1] = False
                    index += 2
                elif index + 1 < len(line) and line[index] in {"$", "%"} and line[index + 1] == "{":
                    mask[index + 1] = False
                    interpolation_depth = 1
                    index += 2
                elif line[index] == '"':
                    in_string = False
                    index += 1
                else:
                    index += 1
                continue
            if line.startswith("//", index):
                for comment_index in range(index, len(line)):
                    mask[comment_index] = False
                break
            if line.startswith("/*", index):
                mask[index] = False
                if index + 1 < len(line):
                    mask[index + 1] = False
                in_block_comment = True
                index += 2
                continue
            if line[index] == '"':
                mask[index] = False
                in_string = True
            index += 1
        masks.append(mask)
    return masks


def _span_is_code(mask: list[bool], start: int, end: int) -> bool:
    return start < end and end <= len(mask) and all(mask[start:end])


def _kind_matches(requested: str, actual: str) -> bool:
    if requested == "all":
        return True
    if requested == "type":
        return actual in TYPE_KINDS
    return requested == actual


class SemanticService:
    """Best-effort project navigation backed by syntax-aware lexical indexing."""

    def __init__(self, project: ProjectService, documentation: DocumentationService) -> None:
        self.project = project
        self.documentation = documentation
        self._cached_index: _Index | None = None

    def lookup_symbol(
        self,
        name: str,
        *,
        symbol_kind: str = "all",
        exact: bool = True,
        limit: int = 20,
    ) -> dict[str, Any]:
        query = name.strip()
        if not query:
            raise ValueError("Имя символа не может быть пустым")
        index = self._index()
        folded = query.casefold()
        matches = [
            declaration
            for declaration in index.declarations
            if _kind_matches(symbol_kind, declaration["symbol_kind"])
            and (declaration["name"].casefold() == folded if exact else folded in declaration["name"].casefold())
        ]
        matches.sort(
            key=lambda item: (
                item["name"].casefold() != folded,
                item["symbol_kind"] != "element",
                item["declaration"]["path"].casefold(),
                item["declaration"]["line"],
            )
        )
        resolution = "not_found" if not matches else "exact" if len(matches) == 1 else "ambiguous"
        return {
            "status": "ready",
            "analysis_mode": "syntax-aware lexical index",
            "semantic_guarantee": False,
            "limitation": (
                "Совпадения объявлений синтаксически точны, но перегрузки, области видимости и разрешение типов "
                "не проверяются компилятором/LSP."
            ),
            "query": query,
            "symbol_kind": symbol_kind,
            "exact": exact,
            "resolution": resolution,
            "total": len(matches),
            "count": min(len(matches), limit),
            "truncated": len(matches) > limit,
            "matches": matches[:limit],
            "index": self._index_summary(index),
        }

    def find_references(
        self,
        name: str,
        *,
        file_type: ProjectFileType = "all",
        relative_path: str | None = None,
        include_declarations: bool = False,
        case_sensitive: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        query = name.strip()
        if IDENTIFIER.fullmatch(query) is None:
            raise ValueError("find_references принимает одно корректное имя XBSL-идентификатора")
        index = self._index()
        selected_path: str | None = None
        if relative_path is not None:
            selected_path = _relative(index.root, self.project._resolve_source_file(index.root, relative_path))

        flags = 0 if case_sensitive else re.IGNORECASE
        token = re.compile(rf"(?<!\w){re.escape(query)}(?!\w)", flags | re.UNICODE)
        declaration_locations = {
            (
                item["declaration"]["path"],
                item["declaration"]["line"],
                item["declaration"]["column"],
            )
            for item in index.declarations
            if (item["name"] == query if case_sensitive else item["name"].casefold() == query.casefold())
        }
        occurrences: list[dict[str, Any]] = []
        declaration_count = 0
        reference_count = 0

        allowed_extensions = {
            "all": TEXT_SOURCE_EXTENSIONS,
            "metadata": frozenset({".yaml", ".yml"}),
            "xbsl": frozenset({".xbsl"}),
            "xbql": frozenset({".xbql"}),
        }[file_type]
        for path, text in index.texts.items():
            if selected_path is not None and path != selected_path:
                continue
            suffix = Path(path).suffix.lower()
            if suffix not in allowed_extensions:
                continue
            code_masks = _code_masks(text) if suffix in {".xbsl", ".xbql"} else None
            for line_number, line in enumerate(text.splitlines(), 1):
                for match in token.finditer(line):
                    column = match.start() + 1
                    if code_masks is not None and not _span_is_code(
                        code_masks[line_number - 1], match.start(), match.end()
                    ):
                        continue
                    is_declaration = (path, line_number, column) in declaration_locations
                    if is_declaration:
                        declaration_count += 1
                    else:
                        reference_count += 1
                    if is_declaration and not include_declarations:
                        continue
                    occurrences.append(
                        {
                            "role": "declaration" if is_declaration else "reference",
                            "path": path,
                            "line": line_number,
                            "column": column,
                            "text": _compact_line(line),
                            "source": "metadata" if suffix in {".yaml", ".yml"} else suffix[1:],
                            "element": _public_element(index.file_elements.get(path)),
                            "confidence": "high" if is_declaration else "medium",
                            "basis": (
                                "indexed declaration"
                                if is_declaration
                                else "exact identifier-boundary match outside comments and string literals"
                            ),
                        }
                    )

        occurrences.sort(key=lambda item: (item["path"].casefold(), item["line"], item["column"]))
        return {
            "status": "ready",
            "analysis_mode": "syntax-aware lexical index",
            "semantic_guarantee": False,
            "limitation": (
                "Одноимённые ссылки могут относиться к разным перегрузкам, локальным символам или типам; "
                "для доказанного разрешения нужен Language Server Element."
            ),
            "query": query,
            "file_type": file_type,
            "relative_path": selected_path,
            "case_sensitive": case_sensitive,
            "include_declarations": include_declarations,
            "declaration_count": declaration_count,
            "reference_count": reference_count,
            "count": min(len(occurrences), limit),
            "truncated": len(occurrences) > limit,
            "results": occurrences[:limit],
            "index": self._index_summary(index),
        }

    def related_docs(
        self,
        *,
        symbol: str | None = None,
        relative_path: str | None = None,
        corpus: CorpusName = "lang",
        limit: int = 8,
        current_only: bool = True,
        product_version: str | None = None,
    ) -> dict[str, Any]:
        symbol = symbol.strip() if symbol else None
        if not symbol and not relative_path:
            raise ValueError("Укажите symbol, relative_path или оба значения")
        index = self._index()
        lookup = self.lookup_symbol(symbol, limit=10) if symbol else None
        file_context = self._file_context(index, relative_path) if relative_path else None
        terms: list[str] = []
        if symbol:
            terms.append(symbol)
        kinds = {match["symbol_kind"] for match in (lookup or {}).get("matches", [])}
        if file_context:
            element = file_context.get("element") or {}
            terms.extend(value for value in (element.get("name"), element.get("element_kind")) if value)
            kinds.update(item["symbol_kind"] for item in file_context["declarations"][:10])
            suffix = Path(file_context["path"]).suffix.lower()
            if suffix == ".xbql":
                terms.extend(("язык запросов", "XBQL"))
        if "method" in kinds:
            terms.extend(("метод", "объявление", "параметры", "возвращаемый тип"))
        if "structure" in kinds:
            terms.extend(("структура", "поля", "конструктор"))
        if "enumeration" in kinds:
            terms.extend(("перечисление", "элементы перечисления"))
        if "exception" in kinds:
            terms.extend(("исключение", "обработка исключений"))
        if "element" in kinds or file_context and file_context.get("element"):
            terms.extend(("элемент проекта", "метаданные", "окружение", "область видимости"))
        if not kinds:
            terms.extend(("1С Элемент", "программный интерфейс"))
        derived_query = " ".join(dict.fromkeys(terms))[:500]
        search = self.documentation.repository().search(
            query=derived_query,
            corpus=corpus,
            limit=limit,
            current_only=current_only,
            product_version=product_version,
        )
        return {
            "status": "ready",
            "analysis_mode": "project-context-enriched corpus search",
            "symbol": symbol,
            "relative_path": file_context["path"] if file_context else None,
            "derived_query": derived_query,
            "project_context": {
                "symbol_lookup": lookup,
                "file": file_context,
            },
            "documentation": search,
            "next_step": "Выберите chunk_id из documentation.results и вызовите get_document.",
        }

    def _index(self) -> _Index:
        root = self.project._required_root()
        files = list(_iter_files(root))
        signature_rows: list[tuple[str, int, int]] = []
        for path in files:
            if path.suffix.lower() not in TEXT_SOURCE_EXTENSIONS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            signature_rows.append((_relative(root, path), stat.st_size, stat.st_mtime_ns))
        signature = tuple(signature_rows)
        if (
            self._cached_index is not None
            and self._cached_index.root == root
            and self._cached_index.signature == signature
        ):
            return self._cached_index

        elements, _ = self.project._elements(root, files)
        file_elements: dict[str, dict[str, Any]] = {}
        for element in elements:
            file_elements[element["metadata_path"]] = element
            for implementation in element["implementation_files"]:
                file_elements[implementation] = element

        texts: dict[str, str] = {}
        skipped: list[str] = []
        for path in files:
            suffix = path.suffix.lower()
            if suffix not in TEXT_SOURCE_EXTENSIONS:
                continue
            relative = _relative(root, path)
            try:
                text, _ = _read_source_text(path)
            except ProjectError:
                skipped.append(relative)
                continue
            if suffix in {".yaml", ".yml"}:
                values = _top_level_values(text)
                if path.name not in ELEMENT_MANIFESTS and _first(values, "ElementKind", "ВидЭлемента") is None:
                    continue
            texts[relative] = text

        declarations: list[dict[str, Any]] = []
        for path, text in texts.items():
            suffix = Path(path).suffix.lower()
            element = file_elements.get(path)
            if suffix in {".yaml", ".yml"} and element:
                declaration = _metadata_name_declaration(path, text, element)
                if declaration:
                    declarations.append(declaration)
                continue
            if suffix != ".xbsl":
                continue
            code_masks = _code_masks(text)
            for line_number, line in enumerate(text.splitlines(), 1):
                match = DECLARATION.match(line)
                if match is None or not _span_is_code(
                    code_masks[line_number - 1], match.start("keyword"), match.end("name")
                ):
                    continue
                name = match.group("name")
                keyword = match.group("keyword").casefold()
                symbol_kind = KIND_ALIASES[keyword]
                declarations.append(
                    _declaration(
                        name=name,
                        symbol_kind=symbol_kind,
                        path=path,
                        line=line_number,
                        column=match.start("name") + 1,
                        text=line,
                        element=element,
                        source="xbsl",
                        basis=f"XBSL declaration keyword {match.group('keyword')}",
                    )
                )
        declarations.sort(
            key=lambda item: (
                item["name"].casefold(),
                item["declaration"]["path"].casefold(),
                item["declaration"]["line"],
            )
        )
        self._cached_index = _Index(
            root=root,
            signature=signature,
            texts=texts,
            declarations=tuple(declarations),
            file_elements=file_elements,
            skipped_files=tuple(skipped),
        )
        return self._cached_index

    @staticmethod
    def _index_summary(index: _Index) -> dict[str, Any]:
        return {
            "project_path": str(index.root),
            "source_files": len(index.texts),
            "declarations": len(index.declarations),
            "skipped_files": list(index.skipped_files[:20]),
            "skipped_files_truncated": len(index.skipped_files) > 20,
        }

    def _file_context(self, index: _Index, relative_path: str) -> dict[str, Any]:
        path = _relative(index.root, self.project._resolve_source_file(index.root, relative_path))
        return {
            "path": path,
            "element": _public_element(index.file_elements.get(path)),
            "declarations": [
                declaration for declaration in index.declarations if declaration["declaration"]["path"] == path
            ][:50],
        }
