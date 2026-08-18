from __future__ import annotations

import hashlib
import re
import subprocess
import threading
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .project import (
    RESOURCE_MANIFESTS,
    ROOT_MANIFESTS,
    SUBSYSTEM_MANIFESTS,
    TEXT_SOURCE_EXTENSIONS,
    ProjectError,
    ProjectService,
    _first,
    _is_resource_payload,
    _iter_files,
    _read_source_text,
    _relative,
    _top_level_values,
)
from .semantic import DECLARATION, IDENTIFIER, KIND_ALIASES, _code_masks, _span_is_code

Direction = Literal["outgoing", "incoming", "both"]
ChangeStatus = Literal["modified", "added", "deleted", "renamed", "untracked", "unknown"]

MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 500
MAX_GRAPH_DEPTH = 5
MAX_GRAPH_CYCLES = 1000
MAX_CHANGED_PATHS = 200
MAX_FACT_IDENTIFIERS = 5000
MAX_DECLARATIONS_PER_FILE = 2000
MAX_GIT_OUTPUT_BYTES = 1024 * 1024
DEPENDENCY_EDGE_TYPES = frozenset(
    {
        "imports_subsystem",
        "imports_element",
        "uses_subsystem",
        "yaml_id_reference",
        "yaml_type_reference",
        "yaml_handler",
        "lexical_reference",
    }
)
LEXICAL_EDGE_TYPES = frozenset({"lexical_reference"})
STRUCTURAL_EDGE_TYPES = frozenset(
    {"metadata_file", "companion_file", "belongs_to_subsystem", "environment", "visibility", "declares"}
)
EXTERNAL_IMPORT_ROOTS = frozenset({"std", "стд", "system", "система"})

IMPORT_LINE = re.compile(r"^\s*(?:import|импорт)\s+(?P<target>[^\s;/]+)", re.IGNORECASE)
YAML_SCALAR = re.compile(r"^(?P<indent>\s*)(?P<key>[^\s:#][^:]*):(?:\s*(?P<value>.*))?$")
YAML_LIST_ITEM = re.compile(r"^\s*-\s*(?P<value>[^#]+?)\s*$")
QUALIFIED_IDENTIFIER = re.compile(r"[^\W\d]\w*(?:(?:::|\.)[^\W\d]\w*)*", re.UNICODE)
UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
HANDLER_KEYS = frozenset({"handler", "обработчик"})
TYPE_KEYS = frozenset({"type", "тип"})
USING_KEYS = frozenset({"using", "использование"})


@dataclass(frozen=True, slots=True)
class _Occurrence:
    value: str
    line: int
    column: int
    count: int = 1


@dataclass(frozen=True, slots=True)
class _Declaration:
    name: str
    kind: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class _FileFacts:
    path: str
    suffix: str
    is_element_source: bool
    imports: tuple[_Occurrence, ...]
    subsystem_uses: tuple[_Occurrence, ...]
    handlers: tuple[_Occurrence, ...]
    type_references: tuple[_Occurrence, ...]
    uuid_references: tuple[_Occurrence, ...]
    identifiers: tuple[_Occurrence, ...]
    declarations: tuple[_Declaration, ...]


@dataclass(frozen=True, slots=True)
class _Graph:
    root: Path
    signature: tuple[tuple[str, int, int], ...]
    nodes: dict[str, dict[str, Any]]
    edges: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, Any], ...]
    element_node_ids: tuple[str, ...]
    file_node_ids: dict[str, str]
    file_owner_ids: dict[str, str]
    cache: dict[str, int]
    cycles: tuple[tuple[str, ...], ...]


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}:" + hashlib.sha256(material).hexdigest()[:20]


def _evidence(path: str, line: int | None, basis: str, value: str | None = None) -> dict[str, Any]:
    return {"path": path, "line": line, "basis": basis, "value": value}


def _normalize_yaml_value(value: str) -> str:
    normalized = value.strip().rstrip(",")
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        normalized = normalized[1:-1]
    return normalized


def _public_element_node(node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: node.get(key)
        for key in (
            "id",
            "type",
            "name",
            "element_kind",
            "element_id",
            "metadata_path",
            "implementation_files",
            "subsystem",
            "environment",
            "visibility_scope",
        )
    }


class ProjectGraphService:
    """Build a bounded, explainable graph without claiming compiler-level symbol resolution."""

    def __init__(self, project: ProjectService) -> None:
        self.project = project
        self._snapshot: _Graph | None = None
        self._fact_cache: dict[str, tuple[tuple[int, int], _FileFacts]] = {}
        self._lock = threading.RLock()

    def get_element_dependencies(
        self,
        identifier: str,
        *,
        direction: Direction = "both",
        depth: int = 2,
        include_lexical: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        graph, cache_hit = self._graph()
        selected, selection = self._select_element(graph, identifier)
        if selection is not None:
            return selection
        assert selected is not None
        nodes, edges, paths, truncated = self._traverse(
            graph,
            (selected,),
            direction=direction,
            depth=depth,
            include_lexical=include_lexical,
            node_limit=limit,
            edge_limit=min(MAX_GRAPH_EDGES, limit * 4),
        )
        return {
            "status": "ready",
            "analysis_mode": "explicit structural graph with bounded lexical evidence",
            "semantic_guarantee": False,
            "element": _public_element_node(graph.nodes[selected]),
            "direction": direction,
            "depth": depth,
            "include_lexical": include_lexical,
            "count": len(nodes),
            "edge_count": len(edges),
            "truncated": truncated,
            "nodes": nodes,
            "edges": edges,
            "paths": paths,
            "graph": self._summary(graph, cache_hit),
        }

    def get_project_dependency_graph(
        self,
        *,
        subsystem: str | None = None,
        include_lexical: bool = False,
        offset: int = 0,
        limit: int = 100,
        edge_limit: int = 300,
    ) -> dict[str, Any]:
        graph, cache_hit = self._graph()
        normalized_subsystem = subsystem.casefold().strip() if subsystem else None
        candidates = [
            node
            for node in graph.nodes.values()
            if node["type"] == "element"
            and (
                normalized_subsystem is None
                or str(node.get("subsystem") or "").casefold() == normalized_subsystem
            )
        ]
        candidates.sort(key=lambda node: (str(node.get("metadata_path", "")).casefold(), node["id"]))
        total = len(candidates)
        selected = candidates[offset : offset + limit]
        selected_ids = {node["id"] for node in selected}
        public_edges = [
            edge
            for edge in graph.edges
            if edge["source"] in selected_ids
            and edge["target"] in selected_ids
            and (include_lexical or edge["type"] not in LEXICAL_EDGE_TYPES)
        ]
        edges_truncated = len(public_edges) > edge_limit
        public_edges = public_edges[:edge_limit]
        return {
            "status": "ready",
            "analysis_mode": "project element dependency graph",
            "semantic_guarantee": False,
            "subsystem": subsystem,
            "include_lexical": include_lexical,
            "total": total,
            "count": len(selected),
            "offset": offset,
            "next_offset": offset + len(selected) if offset + len(selected) < total else None,
            "nodes": [_public_element_node(node) for node in selected],
            "edges": public_edges,
            "edges_truncated": edges_truncated,
            "cycles": self._public_cycles(graph),
            "cycles_truncated": len(graph.cycles) > 20,
            "issues_count": len(graph.issues),
            "graph": self._summary(graph, cache_hit),
        }

    def analyze_change_impact(
        self,
        *,
        element: str | None = None,
        relative_paths: list[str] | None = None,
        depth: int = 3,
        include_lexical: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        graph, cache_hit = self._graph()
        start_ids: list[str] = []
        inputs: list[dict[str, Any]] = []
        if element:
            selected, selection = self._select_element(graph, element)
            if selection is not None:
                return selection
            assert selected is not None
            start_ids.append(selected)
            inputs.append({"kind": "element", "value": element, "node_id": selected})
        for relative_path in relative_paths or []:
            path = self._validated_relative_path(relative_path)
            node_id = graph.file_node_ids.get(path)
            if node_id is None:
                return {
                    "status": "not_found",
                    "message": f"Файл не входит в граф активного проекта: {path}",
                }
            start_ids.append(node_id)
            inputs.append({"kind": "file", "value": path, "node_id": node_id})
        if not start_ids:
            try:
                changed = self.get_changed_elements()
            except ProjectError as error:
                return {
                    "status": "input_required",
                    "message": "Укажите element или relative_paths: локальный Git status недоступен",
                    "changed_elements": {"status": "unavailable", "error": str(error)},
                }
            if changed.get("status") != "ready":
                return {
                    "status": "input_required",
                    "message": "Укажите element/relative_paths или передайте доступный список изменений",
                    "changed_elements": changed,
                }
            for change in changed["changes"]:
                node_id = graph.file_node_ids.get(change["path"])
                if node_id:
                    start_ids.append(node_id)
                    inputs.append({"kind": "changed_file", "value": change["path"], "node_id": node_id})
        if not start_ids:
            return {
                "status": "ready",
                "analysis_mode": "reverse dependency traversal",
                "semantic_guarantee": False,
                "inputs": inputs,
                "affected_elements": [],
                "paths": [],
                "count": 0,
                "truncated": False,
                "graph": self._summary(graph, cache_hit),
            }

        nodes, edges, paths, truncated = self._traverse(
            graph,
            tuple(dict.fromkeys(start_ids)),
            direction="incoming",
            depth=depth,
            include_lexical=include_lexical,
            node_limit=limit,
            edge_limit=min(MAX_GRAPH_EDGES, limit * 5),
        )
        affected = [node for node in nodes if node["type"] == "element" and node["id"] not in start_ids]
        return {
            "status": "ready",
            "analysis_mode": "reverse dependency traversal",
            "semantic_guarantee": False,
            "limitation": (
                "Explicit edges are structural evidence. Lexical edges are possible impact only; "
                "compiler/LSP resolution is not implied."
            ),
            "inputs": inputs,
            "depth": depth,
            "include_lexical": include_lexical,
            "count": len(affected),
            "truncated": truncated,
            "affected_elements": [_public_element_node(node) for node in affected],
            "edges": edges,
            "paths": paths,
            "graph": self._summary(graph, cache_hit),
        }

    def get_changed_elements(self, changed_paths: list[str] | None = None) -> dict[str, Any]:
        graph, cache_hit = self._graph()
        source = "explicit_paths" if changed_paths is not None else self.project._active_source()
        raw_changes: list[dict[str, Any]]
        if changed_paths is not None:
            if len(changed_paths) > MAX_CHANGED_PATHS:
                raise ValueError(f"changed_paths должен содержать не более {MAX_CHANGED_PATHS} путей")
            raw_changes = [
                {"path": self._validated_relative_path(path), "status": "unknown", "old_path": None}
                for path in changed_paths
            ]
        elif source == "ide_session":
            context = self.project.ide_workspace_context() or {}
            git = context.get("git") if isinstance(context, Mapping) else None
            modified = git.get("modified") if isinstance(git, Mapping) else None
            if modified is False:
                raw_changes = []
            else:
                return {
                    "status": "paths_required",
                    "source": "ide_git_summary",
                    "message": (
                        "Штатный g5rt.team.status сообщает только общий признак modified и не возвращает пути. "
                        "Передайте changed_paths из текущего diff; MCP не запускает второй Git внутри Element IDE."
                    ),
                    "git": git,
                    "changes": [],
                    "graph": self._summary(graph, cache_hit),
                }
        else:
            raw_changes = self._git_changes(graph.root)
            source = "local_git_status"

        total = len(raw_changes)
        changes = [self._map_change(graph, change) for change in raw_changes[:MAX_CHANGED_PATHS]]
        return {
            "status": "ready",
            "source": source,
            "total": total,
            "count": len(changes),
            "truncated": total > len(changes),
            "mapped_elements": sum(change["element"] is not None for change in changes),
            "changes": changes,
            "graph": self._summary(graph, cache_hit),
        }

    def validate_element_structure(self, identifier: str | None = None, *, limit: int = 100) -> dict[str, Any]:
        graph, cache_hit = self._graph()
        selected_id: str | None = None
        if identifier:
            selected_id, selection = self._select_element(graph, identifier)
            if selection is not None:
                return selection
        issues = list(graph.issues)
        if selected_id:
            node = graph.nodes[selected_id]
            paths = {node.get("metadata_path"), *(node.get("implementation_files") or [])}
            issues = [issue for issue in issues if issue.get("node_id") == selected_id or issue.get("path") in paths]
        errors = sum(issue["severity"] == "error" for issue in issues)
        warnings = sum(issue["severity"] == "warning" for issue in issues)
        return {
            "status": "invalid" if errors else "ready",
            "analysis_mode": "structural validation without compilation",
            "semantic_guarantee": False,
            "element": _public_element_node(graph.nodes[selected_id]) if selected_id else None,
            "errors": errors,
            "warnings": warnings,
            "total": len(issues),
            "count": min(len(issues), limit),
            "truncated": len(issues) > limit,
            "issues": issues[:limit],
            "cycles": self._public_cycles(graph),
            "graph": self._summary(graph, cache_hit),
        }

    def find_unused_project_elements(
        self,
        *,
        subsystem: str | None = None,
        include_public: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        graph, cache_hit = self._graph()
        incoming: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in graph.edges:
            if edge["type"] in DEPENDENCY_EDGE_TYPES:
                incoming[edge["target"]].append(edge)
        normalized_subsystem = subsystem.casefold().strip() if subsystem else None
        candidates = []
        for node_id in graph.element_node_ids:
            node = graph.nodes[node_id]
            if node.get("element_kind") in {"Project", "Subsystem", "Resources"}:
                continue
            if normalized_subsystem and str(node.get("subsystem") or "").casefold() != normalized_subsystem:
                continue
            visibility = str(node.get("visibility_scope") or "").casefold()
            is_public = visibility in {"inproject", "впроекте", "public", "публичный"}
            if is_public and not include_public:
                continue
            if incoming[node_id]:
                continue
            candidates.append(
                {
                    "element": _public_element_node(node),
                    "confidence": "low",
                    "basis": "no inbound explicit or lexical edge in the bounded project graph",
                    "requires_review": True,
                }
            )
        candidates.sort(key=lambda item: str(item["element"].get("metadata_path", "")).casefold())
        page = candidates[offset : offset + limit]
        return {
            "status": "ready",
            "analysis_mode": "conservative inbound-edge candidate search",
            "semantic_guarantee": False,
            "limitation": (
                "A candidate is not proven unused: dynamic access, platform conventions, external consumers, "
                "and unresolved compiler references may be absent from the graph."
            ),
            "subsystem": subsystem,
            "include_public": include_public,
            "total": len(candidates),
            "count": len(page),
            "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < len(candidates) else None,
            "candidates": page,
            "graph": self._summary(graph, cache_hit),
        }

    def _graph(self) -> tuple[_Graph, bool]:
        root = self.project._required_root()
        files = [path for path in _iter_files(root) if path.suffix.lower() in TEXT_SOURCE_EXTENSIONS]
        signature_rows: list[tuple[str, int, int]] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature_rows.append((_relative(root, path), stat.st_size, stat.st_mtime_ns))
        signature = tuple(signature_rows)
        with self._lock:
            if self._snapshot and self._snapshot.root == root and self._snapshot.signature == signature:
                return self._snapshot, True
            graph = self._build(root, files, signature)
            self._snapshot = graph
            return graph, False

    def _build(
        self,
        root: Path,
        files: list[Path],
        signature: tuple[tuple[str, int, int], ...],
    ) -> _Graph:
        parsed = 0
        reused = 0
        facts_by_path: dict[str, _FileFacts] = {}
        live_paths: set[str] = set()
        stale_paths = set(self._fact_cache)
        signature_by_path = {path: (size, mtime) for path, size, mtime in signature}
        for path in files:
            relative = _relative(root, path)
            live_paths.add(relative)
            file_signature = signature_by_path.get(relative)
            cached = self._fact_cache.get(relative)
            if file_signature and cached and cached[0] == file_signature:
                facts = cached[1]
                reused += 1
            else:
                facts = self._parse_file(path, relative)
                if file_signature:
                    self._fact_cache[relative] = (file_signature, facts)
                parsed += 1
            if facts.is_element_source:
                facts_by_path[relative] = facts
        stale_paths -= live_paths
        for stale in stale_paths:
            self._fact_cache.pop(stale, None)

        elements, element_read_issues = self.project._elements(root, files)
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        edge_keys: set[tuple[Any, ...]] = set()
        issues: list[dict[str, Any]] = [
            {"code": "unreadable_metadata", "severity": "error", **issue} for issue in element_read_issues
        ]
        file_node_ids: dict[str, str] = {}
        file_owner_ids: dict[str, str] = {}
        element_node_ids: list[str] = []
        elements_by_name: defaultdict[str, list[str]] = defaultdict(list)
        elements_by_uuid: dict[str, str] = {}
        subsystem_nodes: dict[str, str] = {}
        subsystem_aliases: defaultdict[str, list[str]] = defaultdict(list)

        def add_node(node: dict[str, Any]) -> str:
            nodes[node["id"]] = node
            return node["id"]

        def file_node(path: str) -> str:
            existing = file_node_ids.get(path)
            if existing:
                return existing
            node_id = add_node({"id": _stable_id("file", path), "type": "file", "path": path, "name": Path(path).name})
            file_node_ids[path] = node_id
            return node_id

        def add_edge(
            source: str,
            target: str,
            edge_type: str,
            confidence: str,
            evidence: dict[str, Any],
            *,
            resolution: str,
        ) -> None:
            key = (
                source,
                target,
                edge_type,
                evidence.get("path"),
                evidence.get("line"),
                evidence.get("value"),
            )
            if key in edge_keys or len(edges) >= MAX_GRAPH_EDGES * 100:
                return
            edge_keys.add(key)
            edges.append(
                {
                    "id": _stable_id("edge", *key),
                    "source": source,
                    "target": target,
                    "type": edge_type,
                    "confidence": confidence,
                    "resolution": resolution,
                    "semantic_guarantee": False,
                    "evidence": evidence,
                }
            )

        for element in elements:
            metadata_path = element["metadata_path"]
            node_id = add_node(
                {
                    "id": _stable_id("element", metadata_path),
                    "type": "element",
                    "name": element.get("name"),
                    "element_kind": element.get("element_kind"),
                    "element_id": element.get("id"),
                    "metadata_path": metadata_path,
                    "implementation_files": list(element.get("implementation_files") or []),
                    "subsystem": element.get("subsystem"),
                    "environment": element.get("environment"),
                    "visibility_scope": element.get("visibility_scope"),
                }
            )
            element_node_ids.append(node_id)
            if isinstance(element.get("name"), str):
                elements_by_name[element["name"].casefold()].append(node_id)
            if isinstance(element.get("id"), str):
                folded_id = element["id"].casefold()
                if folded_id in elements_by_uuid:
                    issues.append(
                        {
                            "code": "duplicate_element_id",
                            "severity": "error",
                            "path": metadata_path,
                            "node_id": node_id,
                            "message": f"Element ID повторяется: {element['id']}",
                        }
                    )
                else:
                    elements_by_uuid[folded_id] = node_id
            metadata_node = file_node(metadata_path)
            file_owner_ids[metadata_path] = node_id
            add_edge(
                node_id,
                metadata_node,
                "metadata_file",
                "high",
                _evidence(metadata_path, None, "element metadata path"),
                resolution="explicit",
            )
            for implementation in element.get("implementation_files") or []:
                implementation_node = file_node(implementation)
                file_owner_ids[implementation] = node_id
                add_edge(
                    node_id,
                    implementation_node,
                    "companion_file",
                    "high",
                    _evidence(implementation, None, "same-directory companion stem"),
                    resolution="explicit",
                )

            subsystem = element.get("subsystem")
            if subsystem:
                subsystem_id = subsystem_nodes.get(subsystem)
                if subsystem_id is None:
                    subsystem_id = add_node(
                        {
                            "id": _stable_id("subsystem", subsystem),
                            "type": "subsystem",
                            "name": Path(subsystem).name,
                            "path": subsystem,
                        }
                    )
                    subsystem_nodes[subsystem] = subsystem_id
                    subsystem_aliases[Path(subsystem).name.casefold()].append(subsystem_id)
                    subsystem_aliases[subsystem.replace("/", "::").casefold()].append(subsystem_id)
                add_edge(
                    node_id,
                    subsystem_id,
                    "belongs_to_subsystem",
                    "high",
                    _evidence(metadata_path, None, "nearest Subsystem.yaml/Подсистема.yaml"),
                    resolution="explicit",
                )
            for dimension, edge_type in (
                (element.get("environment"), "environment"),
                (element.get("visibility_scope"), "visibility"),
            ):
                if dimension:
                    dimension_id = _stable_id(edge_type, str(dimension).casefold())
                    if dimension_id not in nodes:
                        add_node({"id": dimension_id, "type": edge_type, "name": dimension})
                    add_edge(
                        node_id,
                        dimension_id,
                        edge_type,
                        "high",
                        _evidence(metadata_path, None, f"top-level {edge_type} metadata", str(dimension)),
                        resolution="explicit",
                    )

        for name, matches in elements_by_name.items():
            if len(matches) > 1:
                paths = [nodes[node_id]["metadata_path"] for node_id in matches]
                issues.append(
                    {
                        "code": "ambiguous_element_name",
                        "severity": "warning",
                        "path": paths[0],
                        "message": f"Имя элемента неоднозначно: {name}",
                        "candidates": paths[:20],
                    }
                )

        for path in facts_by_path:
            file_node(path)
        source_files = [
            path
            for path, facts in facts_by_path.items()
            if facts.suffix in {".xbsl", ".xbql"} and path not in file_owner_ids
        ]
        for path in source_files:
            issues.append(
                {
                    "code": "orphan_source_file",
                    "severity": "warning",
                    "path": path,
                    "message": "XBSL/XBQL-файл не связан с YAML-элементом по companion-правилу",
                }
            )

        declaration_nodes: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
        declarations_by_name: defaultdict[str, list[str]] = defaultdict(list)
        for path, facts in facts_by_path.items():
            owner = file_owner_ids.get(path)
            for declaration in facts.declarations:
                symbol_id = add_node(
                    {
                        "id": _stable_id("symbol", path, declaration.line, declaration.column, declaration.name),
                        "type": "symbol",
                        "name": declaration.name,
                        "symbol_kind": declaration.kind,
                        "path": path,
                        "line": declaration.line,
                        "column": declaration.column,
                        "owner_element_id": owner,
                    }
                )
                declaration_nodes[(path, declaration.name.casefold())].append(symbol_id)
                declarations_by_name[declaration.name.casefold()].append(symbol_id)
                add_edge(
                    file_node(path),
                    symbol_id,
                    "declares",
                    "high",
                    _evidence(path, declaration.line, f"XBSL {declaration.kind} declaration", declaration.name),
                    resolution="explicit",
                )

        def source_node(path: str) -> str:
            return file_owner_ids.get(path) or file_node(path)

        def resolve_element_name(value: str) -> list[str]:
            tail = re.split(r"::|\.", value)[-1].rstrip("?[]<>")
            return elements_by_name.get(tail.casefold(), [])

        for path, facts in facts_by_path.items():
            source = source_node(path)
            for occurrence in (*facts.imports, *facts.subsystem_uses):
                parts = occurrence.value.split("::")
                root_name = parts[0].casefold()
                edge_type = "uses_subsystem" if occurrence in facts.subsystem_uses else "imports_subsystem"
                subsystem_matches = sorted(set(subsystem_aliases.get(root_name, [])))
                if len(subsystem_matches) == 1:
                    add_edge(
                        source,
                        subsystem_matches[0],
                        edge_type,
                        "high",
                        _evidence(path, occurrence.line, "explicit import/Using declaration", occurrence.value),
                        resolution="explicit",
                    )
                elif root_name not in EXTERNAL_IMPORT_ROOTS:
                    issues.append(
                        {
                            "code": "unresolved_import" if not subsystem_matches else "ambiguous_import",
                            "severity": "warning",
                            "path": path,
                            "line": occurrence.line,
                            "message": f"Импорт не разрешён однозначно: {occurrence.value}",
                        }
                    )
                if len(parts) > 1:
                    element_matches = resolve_element_name(parts[-1])
                    if len(element_matches) == 1 and element_matches[0] != source:
                        add_edge(
                            source,
                            element_matches[0],
                            "imports_element",
                            "high",
                            _evidence(path, occurrence.line, "qualified explicit import", occurrence.value),
                            resolution="explicit_name",
                        )

            owner = file_owner_ids.get(path)
            for handler in facts.handlers:
                local_matches: list[str] = []
                if owner:
                    for implementation in nodes[owner].get("implementation_files") or []:
                        local_matches.extend(declaration_nodes.get((implementation, handler.value.casefold()), []))
                matches = local_matches or declarations_by_name.get(handler.value.casefold(), [])
                if len(matches) == 1 and owner:
                    add_edge(
                        owner,
                        matches[0],
                        "yaml_handler",
                        "high" if local_matches else "medium",
                        _evidence(path, handler.line, "YAML Handler/Обработчик reference", handler.value),
                        resolution="companion_declaration" if local_matches else "unique_project_declaration",
                    )
                elif owner:
                    issues.append(
                        {
                            "code": "missing_handler" if not matches else "ambiguous_handler",
                            "severity": "error" if not matches else "warning",
                            "path": path,
                            "line": handler.line,
                            "node_id": owner,
                            "message": f"Обработчик не разрешён однозначно: {handler.value}",
                        }
                    )
            for reference in facts.type_references:
                matches = resolve_element_name(reference.value)
                if len(matches) == 1 and matches[0] != owner:
                    add_edge(
                        source,
                        matches[0],
                        "yaml_type_reference",
                        "medium",
                        _evidence(path, reference.line, "YAML Type/Тип exact known-element name", reference.value),
                        resolution="syntax_aware_name_match",
                    )
            for reference in facts.uuid_references:
                target = elements_by_uuid.get(reference.value.casefold())
                if target and target != owner:
                    add_edge(
                        source,
                        target,
                        "yaml_id_reference",
                        "high",
                        _evidence(path, reference.line, "exact Element UUID in YAML", reference.value),
                        resolution="exact_id",
                    )

            import_lines = {occurrence.line for occurrence in facts.imports}
            for occurrence in facts.identifiers:
                if occurrence.line in import_lines:
                    continue
                matches = elements_by_name.get(occurrence.value.casefold(), [])
                if len(matches) == 1 and matches[0] != owner:
                    add_edge(
                        source,
                        matches[0],
                        "lexical_reference",
                        "medium",
                        _evidence(
                            path,
                            occurrence.line,
                            "identifier-boundary occurrence outside comments/strings",
                            occurrence.value,
                        ),
                        resolution="lexical_name_match",
                    )

        edges.sort(key=lambda edge: (edge["source"], edge["target"], edge["type"], edge["id"]))
        cycles = self._find_cycles(nodes, edges)
        issues.sort(key=lambda issue: (issue.get("path", ""), issue.get("line") or 0, issue["code"]))
        return _Graph(
            root=root,
            signature=signature,
            nodes=nodes,
            edges=tuple(edges),
            issues=tuple(issues),
            element_node_ids=tuple(element_node_ids),
            file_node_ids=file_node_ids,
            file_owner_ids=file_owner_ids,
            cache={
                "parsed_files": parsed,
                "reused_files": reused,
                "removed_files": len(stale_paths),
            },
            cycles=cycles,
        )

    @staticmethod
    def _parse_file(path: Path, relative: str) -> _FileFacts:
        if _is_resource_payload(relative):
            return _FileFacts(relative, path.suffix.lower(), False, (), (), (), (), (), (), ())
        try:
            text, _ = _read_source_text(path)
        except ProjectError:
            return _FileFacts(relative, path.suffix.lower(), False, (), (), (), (), (), (), ())
        suffix = path.suffix.lower()
        values = _top_level_values(text) if suffix in {".yaml", ".yml"} else {}
        is_element_source = suffix not in {".yaml", ".yml"} or path.name in (
            *ROOT_MANIFESTS,
            *SUBSYSTEM_MANIFESTS,
            *RESOURCE_MANIFESTS,
        ) or _first(values, "ElementKind", "ВидЭлемента") is not None
        if not is_element_source:
            return _FileFacts(relative, suffix, False, (), (), (), (), (), (), ())

        imports: list[_Occurrence] = []
        subsystem_uses: list[_Occurrence] = []
        handlers: list[_Occurrence] = []
        type_references: list[_Occurrence] = []
        uuid_references: list[_Occurrence] = []
        declarations: list[_Declaration] = []
        identifier_rows: dict[str, list[int]] = {}
        lines = text.splitlines()
        masks = _code_masks(text) if suffix in {".xbsl", ".xbql"} else None
        using_indent: int | None = None

        for line_number, line in enumerate(lines, 1):
            if masks is not None:
                import_match = IMPORT_LINE.match(line)
                if import_match and _span_is_code(
                    masks[line_number - 1], import_match.start("target"), import_match.end("target")
                ):
                    imports.append(
                        _Occurrence(import_match.group("target"), line_number, import_match.start("target") + 1)
                    )
                if suffix == ".xbsl" and len(declarations) < MAX_DECLARATIONS_PER_FILE:
                    declaration_match = DECLARATION.match(line)
                    if declaration_match and _span_is_code(
                        masks[line_number - 1],
                        declaration_match.start("keyword"),
                        declaration_match.end("name"),
                    ):
                        declarations.append(
                            _Declaration(
                                declaration_match.group("name"),
                                KIND_ALIASES[declaration_match.group("keyword").casefold()],
                                line_number,
                                declaration_match.start("name") + 1,
                            )
                        )
                for match in IDENTIFIER.finditer(line):
                    if not _span_is_code(masks[line_number - 1], match.start(), match.end()):
                        continue
                    key = match.group(0).casefold()
                    row = identifier_rows.setdefault(key, [line_number, match.start() + 1, 0])
                    row[2] += 1
                    if len(identifier_rows) >= MAX_FACT_IDENTIFIERS:
                        break
                continue

            scalar = YAML_SCALAR.match(line)
            if scalar:
                key = scalar.group("key").strip().casefold()
                value = _normalize_yaml_value(scalar.group("value") or "")
                indent = len(scalar.group("indent"))
                using_indent = indent if key in USING_KEYS and not value else None
                if value and key in HANDLER_KEYS:
                    handlers.append(_Occurrence(value, line_number, line.find(value) + 1))
                if value and key in TYPE_KEYS:
                    for match in QUALIFIED_IDENTIFIER.finditer(value):
                        type_references.append(
                            _Occurrence(match.group(0), line_number, line.find(value) + match.start() + 1)
                        )
            elif using_indent is not None:
                item = YAML_LIST_ITEM.match(line)
                current_indent = len(line) - len(line.lstrip())
                if item and current_indent > using_indent:
                    value = _normalize_yaml_value(item.group("value"))
                    subsystem_uses.append(_Occurrence(value, line_number, line.find(value) + 1))
                elif line.strip() and current_indent <= using_indent:
                    using_indent = None
            for match in UUID_PATTERN.finditer(line):
                uuid_references.append(_Occurrence(match.group(0), line_number, match.start() + 1))

        identifiers = tuple(
            _Occurrence(name, row[0], row[1], row[2])
            for name, row in sorted(identifier_rows.items(), key=lambda item: item[0])
        )
        return _FileFacts(
            relative,
            suffix,
            True,
            tuple(imports),
            tuple(subsystem_uses),
            tuple(handlers),
            tuple(type_references),
            tuple(uuid_references),
            identifiers,
            tuple(declarations),
        )

    def _select_element(self, graph: _Graph, identifier: str) -> tuple[str | None, dict[str, Any] | None]:
        query = identifier.strip()
        if not query:
            raise ValueError("Идентификатор элемента не может быть пустым")
        folded = query.casefold()
        matches = [
            node_id
            for node_id in graph.element_node_ids
            if folded
            in {
                str(graph.nodes[node_id].get("name") or "").casefold(),
                str(graph.nodes[node_id].get("element_id") or "").casefold(),
                str(graph.nodes[node_id].get("metadata_path") or "").casefold(),
            }
        ]
        if not matches:
            return None, {"status": "not_found", "message": f"Элемент проекта не найден: {query}", "candidates": []}
        if len(matches) > 1:
            return None, {
                "status": "selection_required",
                "message": f"Имя элемента неоднозначно: {query}",
                "candidates": [_public_element_node(graph.nodes[node_id]) for node_id in matches[:20]],
            }
        return matches[0], None

    @staticmethod
    def _traverse(
        graph: _Graph,
        start_ids: tuple[str, ...],
        *,
        direction: Direction,
        depth: int,
        include_lexical: bool,
        node_limit: int,
        edge_limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
        if depth < 1 or depth > MAX_GRAPH_DEPTH:
            raise ValueError(f"depth должен быть от 1 до {MAX_GRAPH_DEPTH}")
        outgoing: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        incoming: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in graph.edges:
            if not include_lexical and edge["type"] in LEXICAL_EDGE_TYPES:
                continue
            outgoing[edge["source"]].append(edge)
            incoming[edge["target"]].append(edge)
        queue = deque((node_id, 0, [node_id], []) for node_id in start_ids)
        visited = set(start_ids)
        result_edges: dict[str, dict[str, Any]] = {}
        paths: list[dict[str, Any]] = []
        truncated = False
        while queue:
            node_id, level, node_path, edge_path = queue.popleft()
            if level >= depth:
                continue
            adjacent: list[tuple[dict[str, Any], str]] = []
            if direction in {"outgoing", "both"}:
                adjacent.extend((edge, edge["target"]) for edge in outgoing[node_id])
            if direction in {"incoming", "both"}:
                adjacent.extend((edge, edge["source"]) for edge in incoming[node_id])
            for edge, target in adjacent:
                if target not in visited and len(visited) >= node_limit:
                    truncated = True
                    continue
                if edge["id"] not in result_edges and len(result_edges) >= edge_limit:
                    truncated = True
                    continue
                result_edges[edge["id"]] = edge
                if target in visited:
                    continue
                visited.add(target)
                next_node_path = [*node_path, target]
                next_edge_path = [*edge_path, edge["id"]]
                paths.append(
                    {
                        "node_ids": next_node_path,
                        "edge_ids": next_edge_path,
                        "confidence": "medium" if any(
                            result_edges[edge_id]["confidence"] != "high" for edge_id in next_edge_path
                        ) else "high",
                    }
                )
                queue.append((target, level + 1, next_node_path, next_edge_path))
        nodes = [graph.nodes[node_id] for node_id in visited if node_id in graph.nodes]
        nodes.sort(key=lambda node: (node["type"], str(node.get("name") or node.get("path") or "").casefold()))
        edges = sorted(result_edges.values(), key=lambda edge: (edge["type"], edge["source"], edge["target"]))
        return nodes[:node_limit], edges[:edge_limit], paths[:node_limit], truncated

    @staticmethod
    def _find_cycles(
        nodes: Mapping[str, Mapping[str, Any]],
        edges: Iterable[Mapping[str, Any]],
    ) -> tuple[tuple[str, ...], ...]:
        adjacency: defaultdict[str, set[str]] = defaultdict(set)
        for edge in edges:
            if edge["type"] not in DEPENDENCY_EDGE_TYPES or edge["type"] in LEXICAL_EDGE_TYPES:
                continue
            if (
                nodes.get(edge["source"], {}).get("type") == "element"
                and nodes.get(edge["target"], {}).get("type") == "element"
            ):
                adjacency[str(edge["source"])].add(str(edge["target"]))
        cycles: set[tuple[str, ...]] = set()
        complete: set[str] = set()
        for start in sorted(adjacency):
            if start in complete:
                continue
            active: list[str] = []
            active_positions: dict[str, int] = {}
            stack: list[tuple[str, Iterable[str]]] = []
            active_positions[start] = 0
            active.append(start)
            stack.append((start, iter(sorted(adjacency[start]))))
            while stack:
                node_id, targets = stack[-1]
                try:
                    target = next(targets)
                except StopIteration:
                    stack.pop()
                    active.pop()
                    active_positions.pop(node_id, None)
                    complete.add(node_id)
                    continue
                if target in active_positions:
                    cycle = active[active_positions[target] :]
                    if len(cycles) < MAX_GRAPH_CYCLES:
                        minimum_index = min(range(len(cycle)), key=cycle.__getitem__)
                        cycles.add(tuple(cycle[minimum_index:] + cycle[:minimum_index]))
                elif target not in complete:
                    active_positions[target] = len(active)
                    active.append(target)
                    stack.append((target, iter(sorted(adjacency[target]))))
        return tuple(sorted(cycles))

    @staticmethod
    def _validated_relative_path(value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts or "\0" in normalized:
            raise ValueError("changed_paths/relative_paths должны находиться внутри активного проекта")
        return path.as_posix()

    @staticmethod
    def _git_changes(root: Path) -> list[dict[str, Any]]:
        try:
            top = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=root,
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProjectError(f"Не удалось получить локальный Git status: {error}") from error
        if top.returncode != 0:
            raise ProjectError("Активный проект не находится в локальном Git-репозитории")
        try:
            git_root = Path(top.stdout.decode("utf-8").strip()).resolve()
            root.relative_to(git_root)
        except (UnicodeDecodeError, ValueError) as error:
            raise ProjectError("Git вернул некорректный или несвязанный корень репозитория") from error
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=15,
        )
        if completed.returncode != 0:
            raise ProjectError("git status завершился с ошибкой")
        if len(completed.stdout) > MAX_GIT_OUTPUT_BYTES:
            raise ProjectError("git status вернул слишком большой список изменений")
        try:
            entries = completed.stdout.decode("utf-8").split("\0")
        except UnicodeDecodeError as error:
            raise ProjectError("git status вернул пути не в UTF-8") from error
        changes: list[dict[str, Any]] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 4 or entry[2] != " ":
                continue
            code = entry[:2]
            repo_path = entry[3:]
            old_repo_path: str | None = None
            if ("R" in code or "C" in code) and index < len(entries):
                old_repo_path = entries[index] or None
                index += 1
            absolute = (git_root / repo_path).resolve()
            try:
                relative = absolute.relative_to(root).as_posix()
            except ValueError:
                continue
            old_relative = None
            if old_repo_path:
                try:
                    old_relative = (git_root / old_repo_path).resolve().relative_to(root).as_posix()
                except ValueError:
                    old_relative = None
            changes.append(
                {
                    "path": relative,
                    "old_path": old_relative,
                    "status": ProjectGraphService._change_status(code),
                    "git_code": code,
                }
            )
        return changes

    @staticmethod
    def _change_status(code: str) -> ChangeStatus:
        if code == "??":
            return "untracked"
        if "R" in code:
            return "renamed"
        if "A" in code:
            return "added"
        if "D" in code:
            return "deleted"
        if "M" in code:
            return "modified"
        return "unknown"

    @staticmethod
    def _map_change(graph: _Graph, change: Mapping[str, Any]) -> dict[str, Any]:
        path = str(change["path"])
        owner_id = graph.file_owner_ids.get(path)
        if owner_id is None and change.get("old_path"):
            owner_id = graph.file_owner_ids.get(str(change["old_path"]))
        mapping = "exact_graph_file_owner" if owner_id else "unmapped"
        element = _public_element_node(graph.nodes[owner_id]) if owner_id else None
        if element is None:
            stem_path = str(PurePosixPath(path).with_suffix(""))
            candidates = [
                graph.nodes[node_id]
                for node_id in graph.element_node_ids
                if str(PurePosixPath(str(graph.nodes[node_id].get("metadata_path") or "")).with_suffix(""))
                == stem_path
            ]
            if len(candidates) == 1:
                element = _public_element_node(candidates[0])
                mapping = "companion_stem_inference"
            elif PurePosixPath(path).suffix.lower() in {".yaml", ".yml"}:
                element = {
                    "id": None,
                    "type": "element",
                    "name": PurePosixPath(path).stem,
                    "element_kind": None,
                    "element_id": None,
                    "metadata_path": path,
                    "implementation_files": [],
                    "subsystem": str(PurePosixPath(path).parent),
                    "environment": None,
                    "visibility_scope": None,
                }
                mapping = "deleted_metadata_path_inference"
        return {
            "path": path,
            "old_path": change.get("old_path"),
            "status": change.get("status", "unknown"),
            "git_code": change.get("git_code"),
            "element": element,
            "mapping": mapping,
            "mapping_confidence": (
                "high" if mapping == "exact_graph_file_owner" else "medium" if element else None
            ),
        }

    @staticmethod
    def _summary(graph: _Graph, cache_hit: bool) -> dict[str, Any]:
        return {
            "project_path": str(graph.root),
            "signature_files": len(graph.signature),
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "elements": len(graph.element_node_ids),
            "issues": len(graph.issues),
            "cycles": len(graph.cycles),
            "cache_hit": cache_hit,
            "incremental": graph.cache,
        }

    @staticmethod
    def _public_cycles(graph: _Graph, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "node_ids": list(cycle),
                "elements": [_public_element_node(graph.nodes[node_id]) for node_id in cycle],
            }
            for cycle in graph.cycles[:limit]
        ]
