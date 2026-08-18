from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import zlib
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from .common import slugify, write_json, write_jsonl

REFERENCE_SCHEMA_VERSION = 1
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

DATASET_METADATA = {
    "lang-link-graph": ("Граф ссылок официальной документации языка.", None, "normalized-source"),
    "api-operations": ("Полные операции официального Console API.", "doc_id", "official-api-bundle"),
    "api-schemas": ("Структурированные схемы официального Console API.", "doc_id", "official-html"),
    "elements": ("Метаданные YAML-элементов Management Console.", "logical_id", "embedded-source"),
    "http-routes": ("HTTP-маршруты из исходников Management Console.", None, "embedded-source"),
    "imports": ("Импорты между исходниками Management Console.", None, "embedded-source"),
    "official-link-graph": ("Граф ссылок официальной документации Console.", None, "normalized-source"),
    "methods": ("Объявления методов XBSL/XBQL Management Console.", None, "embedded-source"),
    "subsystems": ("Сводка по подсистемам Management Console.", "subsystem", "derived-analysis"),
    "files": ("Полный инвентарь файлов серверного бандла.", "path", "bundle-inventory"),
    "jar-packages": ("Пакеты и классы Java-модулей.", None, "jar-inventory"),
    "jars": ("Инвентарь Java-модулей серверного бандла.", "path", "jar-inventory"),
    "components": ("Карта компонентов серверного бандла.", "component_id", "structured-analysis"),
    "config-files": ("Безопасная структура конфигурационных файлов.", "config_id", "structured-analysis"),
    "connections": ("Проверяемые связи компонентов сервера и IDE.", "connection_id", "structured-analysis"),
    "entrypoints": ("Точки входа и цепочки запуска бандла.", "entrypoint_id", "structured-analysis"),
    "extensions": ("Встроенные расширения IDE.", "extension_id", "package-metadata"),
    "host-modules": ("Модули хоста встроенной Theia IDE.", "module_id", "package-metadata"),
    "server-link-graph": ("Граф ссылок серверной документации.", None, "normalized-source"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _jsonl_count(path: Path) -> int:
    with path.open(encoding="utf-8") as stream:
        return sum(1 for line in stream if line.strip())


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _collect_schema_titles(value: Any) -> list[str]:
    titles: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            title = node.get("title")
            if isinstance(title, str) and any(
                key in node for key in ("type", "properties", "items", "allOf", "anyOf", "oneOf")
            ):
                clean_title = _clean(title)
                if clean_title and clean_title.casefold() not in {"schema", "object", "array"}:
                    titles.append(clean_title)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return list(dict.fromkeys(titles))


class _SchemaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.properties: list[dict[str, str]] = []
        self.depth = 0
        self.current: dict[str, str] | None = None
        self.capture: str | None = None
        self.expect_example = False

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        classes = attrs.get("class", "")
        if tag == "div" and "openapi-schema__list-item" in classes and self.current is None:
            self.depth = 1
            self.current = {"name": "", "type": "", "description": "", "example": ""}
            return
        if self.current is None:
            return
        if tag == "div":
            self.depth += 1
        elif tag == "strong" and "openapi-schema__property" in classes:
            self.capture = "name"
        elif tag == "span" and "openapi-schema__name" in classes:
            self.capture = "type"
        elif tag == "p" and not self.current["description"]:
            self.capture = "description"
        elif tag == "code" and self.expect_example:
            self.capture = "example"

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag == "div":
            self.depth -= 1
            if self.depth <= 0:
                if self.current["name"]:
                    self.properties.append({key: _clean(value) for key, value in self.current.items()})
                self.current = None
                self.capture = None
                self.expect_example = False
        elif tag in {"strong", "span", "p", "code"}:
            self.capture = None

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        text = _clean(data)
        if not text:
            return
        if text.startswith("Example:"):
            self.expect_example = True
        elif self.capture:
            self.current[self.capture] = _clean(self.current[self.capture] + " " + text)


def _scan_api_bundles(docs_root: Path) -> list[dict[str, Any]]:
    parse_pattern = re.compile(r"JSON\.parse\('((?:\\.|[^'])*)'\)")
    rows: list[dict[str, Any]] = []
    for path in sorted((docs_root / "assets" / "js").glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "@site/docs/console/" not in text or '"api":"eJ' not in text:
            continue
        for match in parse_pattern.finditer(text):
            try:
                metadata = json.loads(ast.literal_eval("'" + match.group(1) + "'"))
                encoded = (metadata.get("frontMatter") or {}).get("api")
                if not encoded:
                    continue
                api = json.loads(zlib.decompress(base64.b64decode(encoded)).decode("utf-8"))
            except (SyntaxError, ValueError, json.JSONDecodeError, zlib.error):
                continue
            permalink = str(metadata.get("permalink") or "")
            if not permalink.startswith("/docs/help/console/"):
                continue
            frontmatter = metadata.get("frontMatter") or {}
            schema_titles = _collect_schema_titles(api)
            rows.append(
                {
                    "doc_id": slugify(permalink.replace("/docs/help/", "").strip("/")),
                    "title": metadata.get("title") or frontmatter.get("title") or api.get("operationId"),
                    "method": str(api.get("method") or "").upper(),
                    "path": api.get("path") or "",
                    "description": api.get("description") or metadata.get("description") or "",
                    "tags": api.get("tags") or [],
                    "operation_id": api.get("operationId"),
                    "parameters": api.get("parameters") or [],
                    "request_body": api.get("requestBody") or {},
                    "responses": api.get("responses") or {},
                    "security": api.get("security") or [],
                    "servers": api.get("servers") or [],
                    "security_schemes": api.get("securitySchemes") or {},
                    "referenced_schema_titles": schema_titles,
                    "resolved_schema_doc_ids": [],
                    "source_url": permalink.rstrip("/") + "/",
                    "source_relpath": (
                        "docs/help/ru/" + permalink.removeprefix("/docs/help/").strip("/") + "/index.html"
                    ),
                    "source_chunk_file": path.relative_to(docs_root).as_posix(),
                }
            )
            break
    unique = {(row["method"], row["path"]): row for row in rows if row["method"] and row["path"]}
    return sorted(unique.values(), key=lambda row: (row["method"], row["path"]))


def _schema_records(docs_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((docs_root / "console" / "schemas").glob("*/index.html")):
        html = path.read_text(encoding="utf-8", errors="ignore")
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        title_text = re.sub(r"<[^>]+>", " ", title_match.group(1)) if title_match else path.parent.name
        title = _clean(title_text)
        description_match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html, re.I)
        parser = _SchemaParser()
        parser.feed(html)
        code_matches = re.findall(r'<code[^>]*class="[^"]*language-json[^"]*"[^>]*>(.*?)</code>', html, re.S | re.I)
        source_url = "/docs/help/console/schemas/" + path.parent.name + "/"
        rows.append(
            {
                "doc_id": slugify(source_url.replace("/docs/help/", "").strip("/")),
                "title": title,
                "description": _clean(description_match.group(1)) if description_match else "",
                "source_url": source_url,
                "source_relpath": "docs/help/ru/console/schemas/" + path.parent.name + "/index.html",
                "breadcrumbs": ["Панель управления", "Схемы"],
                "properties": parser.properties,
                "property_count": len(parser.properties),
                "json_example": _clean(re.sub(r"<[^>]+>", "", code_matches[0])) if code_matches else None,
            }
        )
    return rows


def build_console_api_references(docs_root: Path, reference_root: Path) -> dict[str, int]:
    schemas = _schema_records(docs_root)
    schema_ids = {row["title"]: row["doc_id"] for row in schemas}
    operations = _scan_api_bundles(docs_root)
    for operation in operations:
        operation["resolved_schema_doc_ids"] = [
            schema_ids[title] for title in operation["referenced_schema_titles"] if title in schema_ids
        ]
    write_jsonl(reference_root / "api-operations.jsonl", operations)
    write_jsonl(reference_root / "api-schemas.jsonl", schemas)
    return {"api_operations": len(operations), "api_schemas": len(schemas)}


def build_link_graph(documents: list[dict[str, Any]], destination: Path) -> None:
    routes: dict[str, str] = {}
    for row in documents:
        if row.get("route"):
            routes[str(row["route"]).rstrip("/") + "/"] = row["id"]
        if row.get("source_uri"):
            routes[str(row["source_uri"]).split("#", 1)[0]] = row["id"]
    links: dict[str, list[str]] = {}
    reverse: dict[str, list[str]] = defaultdict(list)
    for row in documents:
        targets: list[str] = []
        for href in LINK_RE.findall(row.get("text", "")):
            normalized = href.split("#", 1)[0]
            target = routes.get(normalized) or routes.get(normalized.rstrip("/") + "/")
            if target and target != row["id"] and target not in targets:
                targets.append(target)
                reverse[target].append(row["id"])
        links[row["id"]] = targets
    write_json(
        destination,
        {
            "url_to_document_id": dict(sorted(routes.items())),
            "document_links": dict(sorted(links.items())),
            "reverse_links": {key: sorted(set(value)) for key, value in sorted(reverse.items())},
        },
    )


def _children(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    items = sorted(path.iterdir(), key=lambda row: row.name)[:100]
    return [item.name + ("/" if item.is_dir() else "") for item in items]


def _component(bundle: Path, component_id: str, relative: str, title: str, summary: str) -> dict[str, Any]:
    path = bundle / relative if relative else bundle
    return {
        "doc_id": "bundle-component-" + component_id,
        "component_id": component_id,
        "title": title,
        "summary": summary,
        "bundle_path": relative or ".",
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "children": _children(path),
        "evidence_paths": [relative or "."],
    }


def _read_package(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_server_references(bundle: Path, version_root: Path) -> dict[str, int]:
    reference_root = version_root / "reference"
    components = [
        _component(bundle, "bundle-root", "", "Корень server bundle", "Самодостаточная поставка сервера Element."),
        _component(bundle, "bin", "bin", "Launcher", "Нативные точки запуска и launcher-конфигурация."),
        _component(bundle, "lib", "lib", "Серверные модули", "JVM-модули и нативные библиотеки сервера."),
        _component(bundle, "executor", "executor", "Executor", "Runtime выполнения приложений Element."),
        _component(
            bundle,
            "instance-template",
            "instance-template",
            "Шаблон экземпляра",
            "Стартовая конфигурация экземпляра.",
        ),
        _component(bundle, "docs", "docs", "Документация", "Встроенная официальная документация."),
        _component(bundle, "ide", "ide", "Встроенная IDE", "Browser-based Theia IDE."),
        _component(bundle, "theia", "ide/theia", "Theia host", "Хост и плагины встроенной IDE."),
        _component(bundle, "plugins", "ide/theia/plugins", "IDE plugins", "Локальные расширения Theia/VS Code."),
        _component(
            bundle,
            "browser-app",
            "ide/theia/products/browser-app",
            "Browser app",
            "Frontend/backend продукт Theia.",
        ),
    ]

    entrypoints: list[dict[str, Any]] = []
    entrypoint_candidates = [
        ("element-server-sh", "element-server.sh", "Shell wrapper сервера"),
        ("element-server-cmd", "element-server.cmd", "Windows wrapper сервера"),
        ("element-server-exe", "bin/element-server.exe", "Windows launcher сервера"),
        ("launcher", "bin/launcher.yml", "JVM launcher configuration"),
        ("theia-browser-app", "ide/theia/products/browser-app/package.json", "Theia browser application"),
        ("theia-bootstrap", "ide/theia/products/browser-app/src/bootstrap.js", "Theia bootstrap"),
        ("theia-backend", "ide/theia/products/browser-app/src-gen/backend/server.js", "Theia backend"),
        ("plugin-host", "ide/theia/products/browser-app/lib/backend/element-plugin-host.js", "Element plugin host"),
    ]
    for entrypoint_id, relative, title in entrypoint_candidates:
        if (bundle / relative).exists():
            entrypoints.append(
                {
                    "doc_id": "bundle-entrypoint-" + entrypoint_id,
                    "entrypoint_id": entrypoint_id,
                    "title": title,
                    "path": relative,
                    "kind": Path(relative).suffix.lstrip(".") or "executable",
                    "evidence_paths": [relative],
                    "related_component_ids": [
                        "bin"
                        if relative.startswith("bin/")
                        else "browser-app"
                        if "browser-app" in relative
                        else "bundle-root"
                    ],
                }
            )

    plugin_root = bundle / "ide" / "theia" / "plugins"
    extensions: list[dict[str, Any]] = []
    if plugin_root.is_dir():
        for directory in sorted((path for path in plugin_root.iterdir() if path.is_dir()), key=lambda row: row.name):
            package_path = directory / "package.json"
            package = _read_package(package_path)
            extensions.append(
                {
                    "extension_id": directory.name,
                    "title": package.get("displayName") or package.get("name") or directory.name,
                    "name": package.get("name") or directory.name,
                    "version": package.get("version"),
                    "description": package.get("description") or "",
                    "publisher": package.get("publisher"),
                    "engines": package.get("engines") or {},
                    "activation_events": package.get("activationEvents") or [],
                    "contributes": sorted((package.get("contributes") or {}).keys()),
                    "path": directory.relative_to(bundle).as_posix(),
                    "evidence_paths": [package_path.relative_to(bundle).as_posix()] if package_path.exists() else [],
                }
            )

    browser_package_path = bundle / "ide" / "theia" / "products" / "browser-app" / "package.json"
    browser_package = _read_package(browser_package_path)
    host_modules: list[dict[str, Any]] = []
    for section in ("dependencies", "devDependencies"):
        for name, version in sorted((browser_package.get(section) or {}).items()):
            host_modules.append(
                {
                    "module_id": slugify(name),
                    "name": name,
                    "version": version,
                    "dependency_scope": section,
                    "host": "ide/theia/products/browser-app",
                    "evidence_paths": [browser_package_path.relative_to(bundle).as_posix()],
                }
            )

    configs: list[dict[str, Any]] = []
    config_candidates = sorted((bundle / "instance-template" / "config").rglob("*.y*ml"))
    launcher = bundle / "bin" / "launcher.yml"
    if launcher.exists():
        config_candidates.append(launcher)
    for path in sorted(set(config_candidates)):
        relative = path.relative_to(bundle).as_posix()
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except yaml.YAMLError as error:
            value = {"parse_error": type(error).__name__}
        top_keys = sorted(str(key) for key in value) if isinstance(value, dict) else []
        configs.append(
            {
                "config_id": slugify(relative),
                "path": relative,
                "format": path.suffix.lstrip("."),
                "top_level_keys": top_keys,
                "sha256": _sha256(path),
                "values_redacted": True,
                "evidence_paths": [relative],
            }
        )

    connections = [
        {
            "connection_id": "launcher-to-instance-template",
            "source_component_id": "bin",
            "target_component_id": "instance-template",
            "protocol": "filesystem configuration",
            "summary": "Launcher использует конфигурацию и каталоги экземпляра.",
            "evidence_paths": ["bin/launcher.yml", "instance-template/config"],
        },
        {
            "connection_id": "browser-app-to-plugins",
            "source_component_id": "browser-app",
            "target_component_id": "plugins",
            "protocol": "Theia local plugin loading",
            "summary": "Browser app загружает локальные расширения из поставки.",
            "evidence_paths": ["ide/theia/products/browser-app/package.json", "ide/theia/plugins"],
        },
        {
            "connection_id": "ide-to-server",
            "source_component_id": "ide",
            "target_component_id": "lib",
            "protocol": "HTTP/WebSocket and process integration",
            "summary": "Встроенная IDE подключена к сервисам server bundle.",
            "evidence_paths": ["ide/theia", "instance-template/config"],
        },
        {
            "connection_id": "server-to-executor",
            "source_component_id": "lib",
            "target_component_id": "executor",
            "protocol": "runtime process and module integration",
            "summary": "Сервер использует Executor для выполнения приложений.",
            "evidence_paths": ["lib", "executor"],
        },
    ]

    datasets = {
        "components.jsonl": components,
        "entrypoints.jsonl": entrypoints,
        "extensions.jsonl": extensions,
        "host-modules.jsonl": host_modules,
        "config-files.jsonl": configs,
        "connections.jsonl": connections,
    }
    for filename, rows in datasets.items():
        write_jsonl(reference_root / filename, rows)
    return {Path(filename).stem.replace("-", "_"): len(rows) for filename, rows in datasets.items()}


def build_reference_catalog(root: Path, normalizer_version: str) -> dict[str, Any]:
    datasets: list[dict[str, Any]] = []
    for corpus in ("lang", "console", "server"):
        versions_root = root / f"docs-{corpus}" / "versions"
        if not versions_root.is_dir():
            continue
        version_roots = sorted(
            (path for path in versions_root.iterdir() if path.is_dir()),
            key=lambda row: row.name,
        )
        for version_root in version_roots:
            candidates = list((version_root / "reference").glob("*.json*"))
            if corpus == "server":
                candidates.extend((version_root / "inventory").glob("*.jsonl"))
            for path in sorted(set(candidates)):
                if path.name in {"coverage.json", "documents.jsonl"}:
                    continue
                name = path.stem
                if name == "link-graph":
                    graph_names = {
                        "lang": "lang-link-graph",
                        "console": "official-link-graph",
                        "server": "server-link-graph",
                    }
                    name = graph_names[corpus]
                description, primary_key, provenance = DATASET_METADATA.get(
                    name,
                    (f"Структурированный набор {name}.", None, "normalized-source"),
                )
                format_name = "jsonl" if path.suffix == ".jsonl" else "json"
                datasets.append(
                    {
                        "id": f"{corpus}.{version_root.name}.{name}",
                        "corpus": corpus,
                        "product_version": version_root.name,
                        "name": name,
                        "description": description,
                        "path": path.relative_to(root).as_posix(),
                        "format": format_name,
                        "records": _jsonl_count(path) if format_name == "jsonl" else None,
                        "primary_key": primary_key,
                        "provenance": provenance,
                        "sha256": _sha256(path),
                    }
                )
    catalog = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "normalizer_version": normalizer_version,
        "purpose": "Структурированные справочники для точных запросов без полнотекстового поиска.",
        "datasets": datasets,
        "summary": {
            "datasets": len(datasets),
            "jsonl_records": sum(row.get("records") or 0 for row in datasets),
        },
    }
    write_json(root / "reference-catalog.json", catalog)
    return catalog
