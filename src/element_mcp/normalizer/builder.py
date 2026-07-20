#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import yaml

from .common import (
    build_corpus_index,
    document_record,
    html_to_markdown,
    iter_jsonl,
    normalize_markdown,
    sha256_bytes,
    slugify,
    utc_now,
    write_json,
    write_jsonl,
)

NORMALIZER_VERSION = "1.0.0"
CORPUS_SCHEMA_VERSION = 1
SUPPORTED_GUIDE_SETS = ("9.2.4-6",)

# The original corpus builder used its repository root as a module constant. The
# packaged normalizer keeps the same build functions, but points them at an
# isolated staging directory for each job.
ROOT = Path.cwd()
TEXT_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".xml",
    ".properties",
    ".sh",
    ".xbsl",
    ".xbql",
    ".xprj",
    ".xasm",
    ".xcore",
    ".xtext",
    ".ecore",
    ".genmodel",
    ".plist",
}
CONSOLE_PREFIX = "com/e1c/g5rt/server/paasmanager/Configuration/src/e1c/console/"


def safe_reset(path: Path, required_parent: Path) -> None:
    path = path.resolve()
    parent = required_parent.resolve()
    if path.parent != parent or not path.name or path.name in {".", ".."}:
        raise RuntimeError(f"Refusing to reset unsafe generated path: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def frontmatter(record: dict) -> str:
    metadata = {
        "logical_id": record["logical_id"],
        "product_version": record["product_version"],
        "source_version": record["source_version"],
        "provenance": record["provenance"],
        "source_path": record.get("source_path"),
        "source_uri": record.get("source_uri"),
        "sha256": record["sha256"],
    }
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n"


def add_normalized_file(version_root: Path, relative: Path, record: dict) -> None:
    destination = version_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(frontmatter(record) + record["text"], encoding="utf-8", newline="\n")


def route_from_html(root: Path, source: Path) -> str:
    relative = source.relative_to(root).as_posix()
    if relative.endswith("/index.html"):
        relative = relative[:-10]
    elif relative.endswith(".html"):
        relative = relative[:-5]
    return relative.strip("/")


def build_language(release: dict) -> tuple[list[dict], dict]:
    bundle = Path(release["bundle_path"])
    docs_root = Path(release["documentation_path"])
    source_version = str(release["documentation_version"])
    product_version = str(release["product_version"])
    corpus_root = ROOT / "docs-lang"
    versions_root = corpus_root / "versions"
    # Use the bundle version as the snapshot key: several patch bundles can ship
    # documentation with the same minor label (for example 9.2).
    version_root = versions_root / product_version
    safe_reset(version_root, versions_root)
    documents: list[dict] = []
    failures: list[dict] = []
    section_counts: Counter[str] = Counter()

    for section in ("topics", "stdlib"):
        for source in sorted((docs_root / section).rglob("*.html")):
            route = route_from_html(docs_root, source)
            try:
                text, metadata = html_to_markdown(source)
            except Exception as error:
                failures.append({"source": str(source.relative_to(bundle)), "error": repr(error)})
                continue
            if len(text.strip()) < 20:
                target = metadata.get("source_uri")
                if target:
                    title = source.parent.name.replace("-", " ")
                    text = (
                        f"# Перенаправление: {title}\n\n"
                        f"Эта устаревшая страница перенаправляет на [{target}]({target}).\n"
                    )
                    metadata["title"] = f"Перенаправление: {title}"
                    metadata["redirect_target"] = target
                else:
                    failures.append(
                        {
                            "source": str(source.relative_to(bundle)),
                            "error": "empty-normalized-document-without-target",
                        }
                    )
                    continue
            logical_id = "lang:official:" + route.replace("/", ":")
            rel_md = Path("normalized") / route / "index.md"
            tags = ["1c-element", "official-docs", section, *route.split("/")[:3]]
            record = document_record(
                logical_id=logical_id,
                corpus="lang",
                kind="language-topic" if section == "topics" else "standard-library",
                title=metadata["title"],
                product_version=product_version,
                source_version=source_version,
                current=bool(release.get("current")),
                source_path=str(source.relative_to(bundle)),
                source_uri=metadata.get("source_uri"),
                normalized_path=str(rel_md),
                provenance="official-html",
                tags=tags,
                text=text,
                extra={
                    "modified": metadata.get("modified"),
                    "route": "/" + route + "/",
                    "redirect_target": metadata.get("redirect_target"),
                },
            )
            add_normalized_file(version_root, rel_md, record)
            documents.append(record)
            section_counts[section] += 1

    write_jsonl(version_root / "documents.jsonl", documents)
    coverage = {
        "source_html": sum(1 for section in ("topics", "stdlib") for _ in (docs_root / section).rglob("*.html")),
        "normalized_documents": len(documents),
        "by_section": dict(section_counts),
        "failures": failures,
        "created_at": utc_now(),
    }
    write_json(version_root / "coverage.json", coverage)
    return documents, coverage


def find_paas_jar(bundle: Path, product_version: str) -> Path:
    exact = bundle / "lib" / "chassis" / "modules" / f"com.e1c.g5rt.server.paasmanager-{product_version}.jar"
    if exact.exists():
        return exact
    matches = sorted((bundle / "lib" / "chassis" / "modules").glob("com.e1c.g5rt.server.paasmanager-*.jar"))
    if not matches:
        raise FileNotFoundError("Management Console JAR was not found")
    return matches[-1]


METHOD_RE = re.compile(
    r"(?m)^(?:\s*@[^\n]+\n)*\s*(?:export\s+)?method\s+([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*)\s*\(([^)]*)\)(?:\s*:\s*([^\n;]+))?"
)
IMPORT_RE = re.compile(r"(?m)^\s*import\s+([^\n;]+)")


def parse_console_yaml(path: str, text: str) -> dict:
    try:
        value = yaml.safe_load(text)
        return value if isinstance(value, dict) else {"value_type": type(value).__name__}
    except Exception as error:
        # A malformed localized string must not remove the element from the catalog.
        fallback: dict = {"parse_error": str(error)}
        for key in ("ElementKind", "Id", "Name", "Environment", "VisibilityScope", "RootUrl"):
            match = re.search(rf"(?m)^{key}:\s*([^\n#]+)", text)
            if match:
                fallback[key] = match.group(1).strip().strip("'\"")
        return fallback


def build_console(release: dict) -> tuple[list[dict], dict]:
    bundle = Path(release["bundle_path"])
    product_version = str(release["product_version"])
    docs_version = str(release["documentation_version"])
    corpus_root = ROOT / "docs-console"
    versions_root = corpus_root / "versions"
    version_root = versions_root / product_version
    safe_reset(version_root, versions_root)
    source_root = version_root / "source" / "e1c" / "console"
    jar_path = find_paas_jar(bundle, product_version)
    documents: list[dict] = []
    elements: list[dict] = []
    methods: list[dict] = []
    imports: list[dict] = []
    routes: list[dict] = []
    subsystem_files: dict[str, list[dict]] = defaultdict(list)
    extension_counts: Counter[str] = Counter()
    yaml_errors: list[dict] = []

    with zipfile.ZipFile(jar_path) as archive:
        names = sorted(
            name for name in archive.namelist() if name.startswith(CONSOLE_PREFIX) and not name.endswith("/")
        )
        for name in names:
            relative_text = name[len(CONSOLE_PREFIX) :]
            relative = Path(relative_text)
            raw = archive.read(name)
            destination = source_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            extension = relative.suffix.lower() or "(none)"
            extension_counts[extension] += 1
            subsystem = relative.parts[0] if len(relative.parts) > 1 else "_root"
            source_item = {
                "path": relative.as_posix(),
                "subsystem": subsystem,
                "extension": extension,
                "bytes": len(raw),
                "sha256": sha256_bytes(raw),
            }
            subsystem_files[subsystem].append(source_item)
            if extension not in TEXT_EXTENSIONS:
                continue
            text = raw.decode("utf-8", errors="replace")
            logical_id = "console:source:" + relative.as_posix().replace("/", ":")
            title = relative.stem
            kind = "console-source"
            tags = ["management-console", "embedded-source", subsystem, extension.lstrip(".")]
            extra: dict = {"subsystem": subsystem, "extension": extension}
            if extension == ".yaml":
                metadata = parse_console_yaml(relative_text, text)
                if "parse_error" in metadata:
                    yaml_errors.append({"path": relative_text, "error": metadata["parse_error"]})
                element = {
                    "logical_id": logical_id,
                    "path": relative_text,
                    "subsystem": subsystem,
                    "element_kind": metadata.get("ElementKind"),
                    "name": metadata.get("Name"),
                    "id": metadata.get("Id"),
                    "environment": metadata.get("Environment"),
                    "visibility_scope": metadata.get("VisibilityScope"),
                    "root_url": metadata.get("RootUrl"),
                    "sha256": source_item["sha256"],
                }
                elements.append(element)
                if metadata.get("ElementKind"):
                    kind = "console-metadata-" + slugify(str(metadata["ElementKind"]))
                    tags.append(str(metadata["ElementKind"]))
                if metadata.get("Name"):
                    title = str(metadata["Name"])
                root_url = metadata.get("RootUrl")
                templates = metadata.get("UrlTemplates")
                if root_url and isinstance(templates, list):
                    for template in templates:
                        if not isinstance(template, dict):
                            continue
                        route_template = str(template.get("Template", ""))
                        for method in template.get("Methods", []) or []:
                            if not isinstance(method, dict):
                                continue
                            routes.append(
                                {
                                    "service": metadata.get("Name", relative.stem),
                                    "source_path": relative_text,
                                    "subsystem": subsystem,
                                    "root_url": root_url,
                                    "template_name": template.get("Name"),
                                    "template": route_template,
                                    "route": (str(root_url).rstrip("/") + "/" + route_template.lstrip("/")).replace(
                                        "//", "/"
                                    ),
                                    "public_route_hint": "/console/api"
                                    + (str(root_url).rstrip("/") + "/" + route_template.lstrip("/")).replace("//", "/"),
                                    "http_method": method.get("Method"),
                                    "handler": method.get("Handler"),
                                    "access_control": metadata.get("AccessControl"),
                                }
                            )
                extra["element"] = {
                    key: value for key, value in element.items() if key not in {"sha256", "path", "logical_id"}
                }
            if extension in {".xbsl", ".xbql"}:
                file_methods = []
                for match in METHOD_RE.finditer(text):
                    item = {
                        "source_path": relative_text,
                        "subsystem": subsystem,
                        "name": match.group(1),
                        "parameters": match.group(2).strip(),
                        "return_type": (match.group(3) or "").strip(),
                        "line": text.count("\n", 0, match.start()) + 1,
                    }
                    methods.append(item)
                    file_methods.append(item["name"])
                file_imports = [match.group(1).strip() for match in IMPORT_RE.finditer(text)]
                imports.extend(
                    {"source_path": relative_text, "subsystem": subsystem, "target": target} for target in file_imports
                )
                extra["symbols"] = file_methods
                extra["imports"] = file_imports
            record = document_record(
                logical_id=logical_id,
                corpus="console",
                kind=kind,
                title=title,
                product_version=product_version,
                source_version=product_version,
                current=bool(release.get("current")),
                source_path=f"{jar_path.relative_to(bundle)}!/{name}",
                normalized_path=str(Path("source/e1c/console") / relative),
                provenance="embedded-source",
                tags=tags,
                text=f"# {title}\n\nИсточник: `{relative_text}`\n\n```{extension.lstrip('.')}\n{text.rstrip()}\n```\n",
                extra=extra,
            )
            documents.append(record)

    docs_root = Path(release["documentation_path"])
    official_count = 0
    for source in sorted((docs_root / "console").rglob("*.html")):
        route = route_from_html(docs_root, source)
        text, metadata = html_to_markdown(source)
        if len(text.strip()) < 20:
            continue
        logical_id = "console:official:" + route.replace("/", ":")
        relative_md = Path(*route.split("/")) / "index.md"
        rel_from_version = Path("normalized/official-api") / relative_md
        record = document_record(
            logical_id=logical_id,
            corpus="console",
            kind="console-api-reference",
            title=metadata["title"],
            product_version=product_version,
            source_version=docs_version,
            current=bool(release.get("current")),
            source_path=str(source.relative_to(bundle)),
            source_uri=metadata.get("source_uri"),
            normalized_path=str(rel_from_version),
            provenance="official-html",
            tags=["management-console", "official-api", *route.split("/")[:4]],
            text=text,
            extra={"modified": metadata.get("modified"), "route": "/" + route + "/"},
        )
        add_normalized_file(version_root, rel_from_version, record)
        documents.append(record)
        official_count += 1

    reference_root = version_root / "reference"
    write_jsonl(reference_root / "elements.jsonl", sorted(elements, key=lambda row: row["path"]))
    write_jsonl(reference_root / "methods.jsonl", sorted(methods, key=lambda row: (row["source_path"], row["line"])))
    write_jsonl(reference_root / "imports.jsonl", sorted(imports, key=lambda row: (row["source_path"], row["target"])))
    write_jsonl(
        reference_root / "http-routes.jsonl",
        sorted(routes, key=lambda row: (str(row["route"]), str(row["http_method"]))),
    )

    subsystem_root = version_root / "subsystems"
    subsystem_summary: list[dict] = []
    for subsystem, files in sorted(subsystem_files.items()):
        element_subset = [row for row in elements if row["subsystem"] == subsystem]
        route_subset = [row for row in routes if row["subsystem"] == subsystem]
        method_subset = [row for row in methods if row["subsystem"] == subsystem]
        kinds = Counter(row.get("element_kind") or "Unknown" for row in element_subset)
        extensions = Counter(row["extension"] for row in files)
        summary = {
            "subsystem": subsystem,
            "files": len(files),
            "extensions": dict(extensions),
            "element_kinds": dict(kinds),
            "methods": len(method_subset),
            "http_routes": len(route_subset),
            "imports": sum(1 for row in imports if row["subsystem"] == subsystem),
        }
        subsystem_summary.append(summary)
        lines = [
            f"# Подсистема `{subsystem}`",
            "",
            f"Автоматически построенный полный индекс исходников подсистемы Management Console {product_version}.",
            "",
            "## Покрытие",
            "",
            f"- Файлов: {len(files)}",
            f"- XBSL-методов: {len(method_subset)}",
            f"- HTTP-маршрутов: {len(route_subset)}",
            f"- Импортов: {summary['imports']}",
            "- Типы элементов: "
            + (", ".join(f"{key}: {value}" for key, value in kinds.most_common()) or "нет YAML-элементов"),
            "",
        ]
        if route_subset:
            lines.extend(["## HTTP-маршруты", "", "| Метод | Маршрут | Обработчик | Источник |", "|---|---|---|---|"])
            for route in route_subset:
                lines.append(
                    f"| {route.get('http_method') or ''} | `{route['route']}` | "
                    f"`{route.get('handler') or ''}` | `{route['source_path']}` |"
                )
            lines.append("")
        lines.extend(["## Файлы", ""])
        for item in sorted(files, key=lambda row: row["path"]):
            lines.append(f"- `{item['path']}` ({item['extension']}, {item['bytes']} байт)")
        text = normalize_markdown("\n".join(lines))
        destination = subsystem_root / (slugify(subsystem) + ".md")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        logical_id = "console:subsystem:" + subsystem
        documents.append(
            document_record(
                logical_id=logical_id,
                corpus="console",
                kind="console-subsystem-index",
                title=f"Подсистема {subsystem}",
                product_version=product_version,
                source_version=product_version,
                current=bool(release.get("current")),
                source_path=str(jar_path.relative_to(bundle)),
                normalized_path=str(destination.relative_to(version_root)),
                provenance="derived-analysis",
                tags=["management-console", "subsystem", subsystem],
                text=text,
                extra={"coverage": summary},
            )
        )
    write_jsonl(reference_root / "subsystems.jsonl", subsystem_summary)
    write_jsonl(version_root / "documents.jsonl", documents)
    coverage = {
        "jar": str(jar_path.relative_to(bundle)),
        "embedded_files": sum(extension_counts.values()),
        "extension_counts": dict(extension_counts),
        "source_documents": sum(1 for row in documents if row["provenance"] == "embedded-source"),
        "official_api_documents": official_count,
        "subsystems": len(subsystem_files),
        "yaml_elements": len(elements),
        "xbsl_methods": len(methods),
        "imports": len(imports),
        "http_routes": len(routes),
        "yaml_errors": yaml_errors,
        "created_at": utc_now(),
    }
    write_json(version_root / "coverage.json", coverage)
    return documents, coverage


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def parse_manifest(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    unfolded = re.sub(r"\n ", "", text)
    result: dict[str, str] = {}
    for line in unfolded.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def jar_group(name: str) -> str:
    lowered = name.lower()
    for marker, group in [
        ("lsp", "language-server"),
        ("server.", "server"),
        ("xbsl", "xbsl"),
        ("xbql", "xbql"),
        ("appengine", "appengine"),
        ("appliedobjects", "standard-library"),
        ("ui.", "ui"),
        ("esb", "integration"),
        ("security", "security"),
        ("auth", "authentication"),
        ("jdbc", "database"),
        ("postgres", "database"),
        ("theia", "ide"),
        ("debug", "debugger"),
    ]:
        if marker in lowered:
            return group
    if name.startswith(("com.e1c", "com._1c")):
        return "platform"
    return "third-party"


def build_server(release: dict) -> tuple[list[dict], dict]:
    bundle = Path(release["bundle_path"])
    product_version = str(release["product_version"])
    docs_version = str(release["documentation_version"])
    corpus_root = ROOT / "docs-server"
    versions_root = corpus_root / "versions"
    version_root = versions_root / product_version
    safe_reset(version_root, versions_root)
    inventory_root = version_root / "inventory"
    documents: list[dict] = []
    file_rows: list[dict] = []
    extension_counts: Counter[str] = Counter()
    directory_counts: Counter[str] = Counter()

    for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
        relative = path.relative_to(bundle).as_posix()
        stat = path.stat()
        extension = path.suffix.lower() or "(none)"
        top = relative.split("/", 1)[0]
        extension_counts[extension] += 1
        directory_counts[top] += 1
        file_rows.append(
            {
                "path": relative,
                "bytes": stat.st_size,
                "extension": extension,
                "top_directory": top,
                "sha256": file_sha256(path),
            }
        )
    write_jsonl(inventory_root / "files.jsonl", file_rows)

    jar_paths: list[Path] = []
    for base in [
        bundle / "lib" / "chassis" / "modules",
        bundle / "executor" / "lib",
        bundle / "ide" / "theia" / "plugins" / "@1c-appengine-plugin",
    ]:
        if base.exists():
            jar_paths.extend(base.rglob("*.jar"))
    jar_paths = sorted(set(jar_paths))
    jar_rows: list[dict] = []
    package_rows: list[dict] = []
    bad_jars: list[dict] = []
    module_root = version_root / "modules"
    for jar_path in jar_paths:
        relative = jar_path.relative_to(bundle).as_posix()
        try:
            with zipfile.ZipFile(jar_path) as archive:
                names = archive.namelist()
                manifest = (
                    parse_manifest(archive.read("META-INF/MANIFEST.MF")) if "META-INF/MANIFEST.MF" in names else {}
                )
        except Exception as error:
            bad_jars.append({"path": relative, "error": repr(error)})
            continue
        classes = [
            name[:-6].replace("/", ".")
            for name in names
            if name.endswith(".class") and not name.startswith("META-INF/versions/")
        ]
        resources = [name for name in names if not name.endswith("/") and not name.endswith(".class")]
        packages: Counter[str] = Counter()
        for class_name in classes:
            package = class_name.rsplit(".", 1)[0] if "." in class_name else "(default)"
            packages[package] += 1
        group = jar_group(jar_path.name)
        row = {
            "path": relative,
            "filename": jar_path.name,
            "bytes": jar_path.stat().st_size,
            "sha256": next((item["sha256"] for item in file_rows if item["path"] == relative), file_sha256(jar_path)),
            "group": group,
            "main_class": manifest.get("Main-Class"),
            "implementation_title": manifest.get("Implementation-Title"),
            "implementation_version": manifest.get("Implementation-Version"),
            "automatic_module_name": manifest.get("Automatic-Module-Name"),
            "classes": len(classes),
            "resources": len(resources),
            "packages": len(packages),
        }
        jar_rows.append(row)
        for package, count in sorted(packages.items()):
            samples = [name for name in classes if name.startswith(package + ".")][:20]
            package_rows.append({"jar": relative, "package": package, "classes": count, "sample_classes": samples})
        title = jar_path.name[:-4]
        lines = [
            f"# Модуль `{title}`",
            "",
            f"Группа: `{group}`.",
            "",
            f"- JAR: `{relative}`",
            f"- Размер: {row['bytes']} байт",
            f"- SHA-256: `{row['sha256']}`",
            f"- Классов: {len(classes)}",
            f"- Ресурсов: {len(resources)}",
            f"- Пакетов: {len(packages)}",
        ]
        for key in ("main_class", "implementation_title", "implementation_version", "automatic_module_name"):
            if row.get(key):
                lines.append(f"- {key}: `{row[key]}`")
        lines.extend(["", "## Пакеты", "", "| Пакет | Классов |", "|---|---:|"])
        for package, count in packages.most_common():
            lines.append(f"| `{package}` | {count} |")
        text = normalize_markdown("\n".join(lines))
        module_path = module_root / (slugify(relative).replace("/", "__") + ".md")
        module_path.parent.mkdir(parents=True, exist_ok=True)
        module_path.write_text(text, encoding="utf-8")
        documents.append(
            document_record(
                logical_id="server:jar:" + relative.replace("/", ":"),
                corpus="server",
                kind="server-java-module",
                title=title,
                product_version=product_version,
                source_version=product_version,
                current=bool(release.get("current")),
                source_path=relative,
                normalized_path=str(module_path.relative_to(version_root)),
                provenance="jar-inventory",
                tags=["server", "java-module", group, jar_path.name],
                text=text,
                extra={"jar": row},
            )
        )
    write_jsonl(inventory_root / "jars.jsonl", jar_rows)
    write_jsonl(inventory_root / "jar-packages.jsonl", package_rows)

    selected_root = version_root / "normalized" / "bundle-files"
    selected_count = 0
    for row in file_rows:
        relative = Path(row["path"])
        if relative.parts[0] == "docs" or relative.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        relative_text = relative.as_posix()
        keep = (
            len(relative.parts) == 1
            or relative.parts[0] in {"bin", "instance-template"}
            or relative_text.startswith("executor/README")
            or relative.parent.as_posix() == "ide/theia/plugins/@1c-appengine-plugin"
            and relative.name in {"README.md", "package.json", "package.nls.json"}
        )
        if not keep:
            continue
        source = bundle / relative
        try:
            text_content = source.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        destination = selected_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text_content, encoding="utf-8", newline="\n")
        text = f"# Файл `{relative_text}`\n\n```{relative.suffix.lstrip('.')}\n{text_content.rstrip()}\n```\n"
        documents.append(
            document_record(
                logical_id="server:file:" + relative_text.replace("/", ":"),
                corpus="server",
                kind="server-bundle-file",
                title=relative.name,
                product_version=product_version,
                source_version=product_version,
                current=bool(release.get("current")),
                source_path=relative_text,
                normalized_path=str(destination.relative_to(version_root)),
                provenance="bundle-file",
                tags=["server", "bundle-file", relative.parts[0], relative.suffix.lstrip(".")],
                text=text,
            )
        )
        selected_count += 1

    plugin = bundle / "ide" / "theia" / "plugins" / "@1c-appengine-plugin"
    source_map = plugin / "dist" / "extension.js.map"
    plugin_source_count = 0
    if source_map.exists():
        try:
            value = json.loads(source_map.read_text(encoding="utf-8"))
            for name, content in zip(value.get("sources", []), value.get("sourcesContent", []), strict=False):
                if not content or "node_modules" in name or name.startswith("webpack/runtime"):
                    continue
                clean = re.sub(r"^(webpack://[^/]+/|webpack:///)", "", name).lstrip("./")
                if not clean or clean.startswith("external "):
                    continue
                destination = version_root / "ide-plugin-sources" / clean
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8", newline="\n")
                documents.append(
                    document_record(
                        logical_id="server:ide-plugin-source:" + clean.replace("/", ":"),
                        corpus="server",
                        kind="ide-plugin-source",
                        title=Path(clean).name,
                        product_version=product_version,
                        source_version=product_version,
                        current=bool(release.get("current")),
                        source_path=str(source_map.relative_to(bundle)) + "!/" + name,
                        normalized_path=str(destination.relative_to(version_root)),
                        provenance="embedded-source",
                        tags=["ide", "theia", "appengine-plugin", Path(clean).suffix.lstrip(".")],
                        text=f"# IDE plugin: `{clean}`\n\n```typescript\n{content.rstrip()}\n```\n",
                    )
                )
                plugin_source_count += 1
        except Exception as error:
            bad_jars.append({"path": str(source_map.relative_to(bundle)), "error": "source-map: " + repr(error)})

    lsp_candidates = sorted((plugin / "bin" / "appengine-lsp" / "repo").glob("com.e1c.g5rt.lsp.server.appengine-*.jar"))
    lsp_resource_count = 0
    if lsp_candidates:
        lsp_jar = lsp_candidates[-1]
        with zipfile.ZipFile(lsp_jar) as archive:
            for name in sorted(archive.namelist()):
                suffix = Path(name).suffix.lower()
                is_model = name.startswith("model/") and suffix in {".xcore", ".ecore", ".genmodel"}
                is_grammar = suffix == ".xtext"
                is_example = suffix in {".xbsl", ".xbql", ".yaml"} and not name.startswith(
                    ("com/", "org/", "META-INF/", "model/")
                )
                if not (is_model or is_grammar or is_example) or name.endswith("/"):
                    continue
                raw = archive.read(name)
                content = raw.decode("utf-8", errors="replace")
                category = "models" if is_model else "grammars" if is_grammar else "examples"
                destination = version_root / "lsp-resources" / category / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                documents.append(
                    document_record(
                        logical_id="server:lsp-resource:" + name.replace("/", ":"),
                        corpus="server",
                        kind="lsp-" + category.rstrip("s"),
                        title=Path(name).name,
                        product_version=product_version,
                        source_version=product_version,
                        current=bool(release.get("current")),
                        source_path=str(lsp_jar.relative_to(bundle)) + "!/" + name,
                        normalized_path=str(destination.relative_to(version_root)),
                        provenance="embedded-source",
                        tags=["lsp", category, suffix.lstrip(".")],
                        text=f"# LSP resource: `{name}`\n\n```{suffix.lstrip('.')}\n{content.rstrip()}\n```\n",
                    )
                )
                lsp_resource_count += 1

    # Relevant official administrator/server/IDE pages are duplicated deliberately.
    docs_root = Path(release["documentation_path"])
    server_keywords = re.compile(
        r"(server|сервер|control-panel|панел|cluster|кластер|instance|экземпляр|deploy|развер|ide-|debug|отлад|monitor|admin|администр|install|установ|application|приложен)",
        re.I,
    )
    official_server_docs = 0
    for source in sorted((docs_root / "topics").rglob("*.html")):
        route = route_from_html(docs_root, source)
        if not server_keywords.search(route):
            continue
        text, metadata = html_to_markdown(source)
        if len(text.strip()) < 20:
            continue
        relative_md = Path("normalized/official-topics") / route / "index.md"
        record = document_record(
            logical_id="server:official:" + route.replace("/", ":"),
            corpus="server",
            kind="server-official-topic",
            title=metadata["title"],
            product_version=product_version,
            source_version=docs_version,
            current=bool(release.get("current")),
            source_path=str(source.relative_to(bundle)),
            source_uri=metadata.get("source_uri"),
            normalized_path=str(relative_md),
            provenance="official-html",
            tags=["server", "official-docs", *route.split("/")[:3]],
            text=text,
            extra={"route": "/" + route + "/", "modified": metadata.get("modified")},
        )
        add_normalized_file(version_root, relative_md, record)
        documents.append(record)
        official_server_docs += 1

    write_jsonl(version_root / "documents.jsonl", documents)
    coverage = {
        "bundle_files": len(file_rows),
        "bundle_bytes": sum(row["bytes"] for row in file_rows),
        "top_directories": dict(directory_counts),
        "extension_counts": dict(extension_counts),
        "inventoried_jars": len(jar_rows),
        "jar_packages": len(package_rows),
        "bad_jars": bad_jars,
        "normalized_bundle_files": selected_count,
        "ide_plugin_sources": plugin_source_count,
        "lsp_resources": lsp_resource_count,
        "official_server_topics": official_server_docs,
        "created_at": utc_now(),
    }
    write_json(version_root / "coverage.json", coverage)
    return documents, coverage


def guide_documents(corpus_name: str, product_version: str, current: bool) -> list[dict]:
    corpus_root = ROOT / f"docs-{corpus_name}"
    result: list[dict] = []
    paths = [
        corpus_root / "README.md",
        *(sorted((corpus_root / "guides").glob("*.md")) if (corpus_root / "guides").exists() else []),
    ]
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", text, re.M)
        title = title_match.group(1) if title_match else path.stem
        relative = path.relative_to(corpus_root).as_posix()
        result.append(
            document_record(
                logical_id=f"{corpus_name}:guide:" + relative.replace("/", ":"),
                corpus=corpus_name,
                kind=f"{corpus_name}-guide",
                title=title,
                product_version=product_version,
                source_version=product_version,
                current=current,
                source_path=relative,
                normalized_path=relative,
                provenance="derived-analysis",
                tags=[corpus_name, "guide", path.stem],
                text=text,
            )
        )
    return result


def install_guide_set(output_root: Path, guide_version: str) -> None:
    """Install our authored analytical guides into a new local corpus."""
    if guide_version not in SUPPORTED_GUIDE_SETS:
        raise RuntimeError(
            f"Нет аналитических руководств для Element {guide_version}. "
            f"Поддерживаемые версии: {', '.join(SUPPORTED_GUIDE_SETS)}"
        )
    source_root = resources.files("element_mcp.normalizer").joinpath("guide_sets", guide_version)
    for filename in ("README.md", "CORPUS_FORMAT.md", "VERSIONING.md"):
        (output_root / filename).write_bytes(source_root.joinpath(filename).read_bytes())
    for corpus_name in ("lang", "console", "server"):
        source_corpus = source_root.joinpath(f"docs-{corpus_name}")
        target_corpus = output_root / f"docs-{corpus_name}"
        target_corpus.mkdir(parents=True, exist_ok=True)
        readme = source_corpus.joinpath("README.md")
        (target_corpus / "README.md").write_bytes(readme.read_bytes())
        target_guides = target_corpus / "guides"
        target_guides.mkdir(parents=True, exist_ok=True)
        for guide in sorted(source_corpus.joinpath("guides").iterdir(), key=lambda item: item.name):
            if guide.name.endswith(".md"):
                (target_guides / guide.name).write_bytes(guide.read_bytes())


def build_normalized_corpus(
    *,
    output_root: Path,
    bundle_path: Path,
    product_version: str,
    documentation_version: str,
    release_id: str | None = None,
    guide_version: str | None = None,
    progress: Callable[[str, int, str], None] | None = None,
) -> dict:
    """Build all three corpora from one local Element server bundle."""
    global ROOT
    ROOT = output_root.expanduser().resolve()
    bundle = bundle_path.expanduser().resolve()
    docs = bundle / "docs" / "help" / "ru"
    if ROOT.exists() and any(ROOT.iterdir()):
        raise RuntimeError(f"Каталог сборки должен быть пустым: {ROOT}")
    ROOT.mkdir(parents=True, exist_ok=True)
    if not docs.is_dir():
        raise FileNotFoundError(f"Не найдена документация Element: {docs}")

    selected_guides = guide_version or product_version
    install_guide_set(ROOT, selected_guides)
    release = {
        "release_id": release_id or f"element-{product_version}",
        "product": "1c-enterprise-element",
        "product_version": product_version,
        "documentation_version": documentation_version,
        "language": "ru",
        "current": True,
        "bundle_path": str(bundle),
        "documentation_path": str(docs),
    }
    per_corpus: dict[str, list[dict]] = {"lang": [], "console": [], "server": []}
    summary = {"release": release, "corpora": {}, "created_at": utc_now()}
    builders = (("lang", build_language), ("console", build_console), ("server", build_server))
    for position, (corpus_name, builder) in enumerate(builders):
        if progress:
            progress(corpus_name, 10 + position * 25, f"Нормализация корпуса {corpus_name}")
        documents, coverage = builder(release)
        per_corpus[corpus_name].extend(documents)
        summary["corpora"][corpus_name] = {
            "coverage": coverage,
            "diff": build_diff(documents, None),
        }
    release_root = ROOT / "releases" / release["release_id"]
    release_root.mkdir(parents=True, exist_ok=True)
    public_release_fields = (
        "release_id",
        "product",
        "product_version",
        "documentation_version",
        "language",
        "current",
    )
    public_release = {key: release[key] for key in public_release_fields}
    write_json(release_root / "manifest.json", {**summary, "release": public_release})
    if progress:
        progress("index", 85, "Построение JSONL, SQLite и векторных индексов")
    aggregate([release], per_corpus)

    manifest = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "guide_set_version": selected_guides,
        "created_at": utc_now(),
        "releases": [public_release],
        "corpora": {name: len(documents) for name, documents in per_corpus.items()},
    }
    write_json(ROOT / "manifest.json", manifest)
    if progress:
        progress("validate", 95, "Проверка целостности корпуса")
    return manifest


def build_diff(version_documents: list[dict], previous_documents: list[dict] | None) -> dict:
    current = {row["logical_id"]: row for row in version_documents}
    previous = {row["logical_id"]: row for row in previous_documents or []}
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = sorted(key for key in set(current) & set(previous) if current[key]["sha256"] != previous[key]["sha256"])
    unchanged = sorted(key for key in set(current) & set(previous) if current[key]["sha256"] == previous[key]["sha256"])
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged_count": len(unchanged),
        "summary": {"added": len(added), "changed": len(changed), "removed": len(removed), "unchanged": len(unchanged)},
    }


def load_sources() -> list[dict]:
    value = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    releases = value.get("releases", [])
    if not releases:
        raise RuntimeError("sources.yaml contains no releases")
    for release in releases:
        if not Path(release["bundle_path"]).is_dir():
            raise FileNotFoundError(release["bundle_path"])
        if not Path(release["documentation_path"]).is_dir():
            raise FileNotFoundError(release["documentation_path"])
    return releases


def aggregate(releases: list[dict], per_corpus: dict[str, list[dict]]) -> None:
    current_release = next((row for row in releases if row.get("current")), releases[-1])
    for corpus_name in ("lang", "console", "server"):
        documents = per_corpus[corpus_name]
        documents.extend(guide_documents(corpus_name, str(current_release["product_version"]), True))
        # A logical source can occur only once per source version in an aggregate.
        unique = {(row["id"], row["sha256"]): row for row in documents}
        documents = sorted(unique.values(), key=lambda row: (row["logical_id"], row["source_version"]))
        build_corpus_index(ROOT / f"docs-{corpus_name}" / "corpus", documents)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build normalized 1C:Element documentation corpora")
    parser.add_argument("--all", action="store_true", help="build all corpora (default)")
    parser.add_argument("--corpus", choices=["lang", "console", "server"], action="append")
    parser.add_argument("--reindex-only", action="store_true", help="rebuild indexes from existing version documents")
    args = parser.parse_args()
    selected = set(args.corpus or ["lang", "console", "server"])
    releases = load_sources()
    per_corpus: dict[str, list[dict]] = {"lang": [], "console": [], "server": []}
    if args.reindex_only:
        for corpus_name in per_corpus:
            versions = ROOT / f"docs-{corpus_name}" / "versions"
            per_corpus[corpus_name] = [
                row for file in sorted(versions.glob("*/documents.jsonl")) for row in iter_jsonl(file)
            ]
        aggregate(releases, per_corpus)
        write_json(
            ROOT / "manifest.json",
            {
                "schema_version": 1,
                "created_at": utc_now(),
                "releases": releases,
                "corpora": {key: len(value) for key, value in per_corpus.items()},
            },
        )
        print("Reindex done", flush=True)
        return 0
    release_summaries: list[dict] = []
    previous_by_corpus: dict[str, list[dict] | None] = {"lang": None, "console": None, "server": None}
    for release in releases:
        summary = {"release": release, "corpora": {}, "created_at": utc_now()}
        print(f"Building {release['release_id']} from {release['bundle_path']}", flush=True)
        for corpus_name, builder in (("lang", build_language), ("console", build_console), ("server", build_server)):
            if corpus_name not in selected:
                existing = ROOT / f"docs-{corpus_name}" / "versions"
                docs = (
                    [row for file in sorted(existing.glob("*/documents.jsonl")) for row in iter_jsonl(file)]
                    if existing.exists()
                    else []
                )
                per_corpus[corpus_name].extend(docs)
                continue
            print(f"  - {corpus_name}", flush=True)
            docs, coverage = builder(release)
            per_corpus[corpus_name].extend(docs)
            diff = build_diff(docs, previous_by_corpus[corpus_name])
            previous_by_corpus[corpus_name] = docs
            summary["corpora"][corpus_name] = {"coverage": coverage, "diff": diff}
        release_root = ROOT / "releases" / release["release_id"]
        release_root.mkdir(parents=True, exist_ok=True)
        write_json(release_root / "manifest.json", summary)
        release_summaries.append(summary)
    aggregate(releases, per_corpus)
    write_json(
        ROOT / "manifest.json",
        {
            "schema_version": 1,
            "created_at": utc_now(),
            "releases": [row["release"] for row in release_summaries],
            "corpora": {key: len(value) for key, value in per_corpus.items()},
        },
    )
    print("Done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
