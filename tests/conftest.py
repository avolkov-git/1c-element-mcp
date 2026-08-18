from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

CHUNKS = {
    "lang": [
        {
            "id": "lang:test:methods@9.2#c0000",
            "document_id": "lang:test:methods@9.2",
            "logical_id": "lang:test:methods",
            "kind": "language-reference",
            "title": "Веб-методы",
            "section": "Объявление",
            "position": 0,
            "text": "Аннотация ВебМетод объявляет метод, доступный через HTTP.",
        },
        {
            "id": "lang:test:methods@9.2#c0001",
            "document_id": "lang:test:methods@9.2",
            "logical_id": "lang:test:methods",
            "kind": "language-reference",
            "title": "Веб-методы",
            "section": "Параметры",
            "position": 1,
            "text": "Параметры веб-метода описываются в сигнатуре метода.",
        },
    ],
    "console": [
        {
            "id": "console:test:api@9.2#c0000",
            "document_id": "console:test:api@9.2",
            "logical_id": "console:test:api",
            "kind": "console-guide",
            "title": "API Панели управления",
            "section": "Авторизация",
            "position": 0,
            "text": "API Панели управления проверяет токен доступа.",
        }
    ],
    "server": [
        {
            "id": "server:test:lsp@9.2#c0000",
            "document_id": "server:test:lsp@9.2",
            "logical_id": "server:test:lsp",
            "kind": "server-guide",
            "title": "Language Server",
            "section": "Диагностика",
            "position": 0,
            "text": "LSP возвращает синтаксические диагностики проекта.",
        }
    ],
}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_reference_jsonl(path: Path, rows: list[dict]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"records": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _create_index(path: Path, corpus: str, chunks: list[dict]) -> None:
    database = sqlite3.connect(path / "index.sqlite")
    database.executescript(
        """
        CREATE TABLE documents(id TEXT PRIMARY KEY);
        CREATE TABLE chunks(
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL, logical_id TEXT NOT NULL,
            corpus TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
            section TEXT, position INTEGER NOT NULL, product_version TEXT NOT NULL,
            source_version TEXT NOT NULL, is_current INTEGER NOT NULL,
            source_path TEXT, normalized_path TEXT, provenance TEXT NOT NULL,
            tags TEXT NOT NULL, sha256 TEXT NOT NULL, text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            title, section, tags, text, content='chunks', content_rowid='rowid', tokenize='unicode61'
        );
        """
    )
    for document_id in sorted({chunk["document_id"] for chunk in chunks}):
        database.execute("INSERT INTO documents VALUES (?)", (document_id,))
    for chunk in chunks:
        database.execute(
            """
            INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk["id"],
                chunk["document_id"],
                chunk["logical_id"],
                corpus,
                chunk["kind"],
                chunk["title"],
                chunk["section"],
                chunk["position"],
                "9.2.4-6",
                "9.2",
                1,
                f"source/{corpus}.html",
                f"docs-{corpus}/versions/9.2.4-6/test.md",
                "official-html",
                "[]",
                hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest(),
                chunk["text"],
            ),
        )
    database.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    database.commit()
    database.close()

    dimensions = 16
    np.save(path / "vectors.f16.npy", np.zeros((len(chunks), dimensions), dtype=np.float16))
    np.save(path / "vector-idf.npy", np.ones(dimensions, dtype=np.float32))
    with (path / "vector-ids.jsonl").open("w", encoding="utf-8") as stream:
        for chunk in chunks:
            stream.write(json.dumps({"chunk_id": chunk["id"]}, ensure_ascii=False) + "\n")
    documents = []
    for document_id in sorted({chunk["document_id"] for chunk in chunks}):
        text = "\n\n".join(chunk["text"] for chunk in chunks if chunk["document_id"] == document_id)
        documents.append({"id": document_id, "text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    with (path / "documents.jsonl").open("w", encoding="utf-8") as stream:
        for document in documents:
            stream.write(json.dumps(document, ensure_ascii=False) + "\n")
    with (path / "chunks.jsonl").open("w", encoding="utf-8") as stream:
        for chunk in chunks:
            value = {
                **chunk,
                "document_id": chunk["document_id"],
                "sha256": hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest(),
            }
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    _write_json(
        path / "vector-meta.json",
        {"rows": len(chunks), "dimensions": dimensions, "schema_version": 1},
    )
    _write_json(
        path / "manifest.json",
        {
            "documents": len({chunk["document_id"] for chunk in chunks}),
            "chunks": len(chunks),
            "created_at": "2026-07-20T00:00:00+00:00",
            "index_schema_version": 1,
            "vectors": {"rows": len(chunks), "dimensions": dimensions, "schema_version": 1},
        },
    )


@pytest.fixture
def corpus_path(tmp_path: Path) -> Path:
    root = tmp_path / "codex-docs"
    root.mkdir()
    _write_json(
        root / "manifest.json",
        {
            "schema_version": 1,
            "created_at": "2026-07-20T00:00:00+00:00",
            "releases": [
                {
                    "release_id": "element-9.2.4-6",
                    "product_version": "9.2.4-6",
                    "documentation_version": "9.2",
                    "current": True,
                }
            ],
        },
    )
    for corpus, chunks in CHUNKS.items():
        path = root / f"docs-{corpus}" / "corpus"
        path.mkdir(parents=True)
        _create_index(path, corpus, chunks)
    reference_rows = {
        "console.9.2.4-6.api-operations": (
            "docs-console/versions/9.2.4-6/reference/api-operations.jsonl",
            "api-operations",
            "console",
            [
                {
                    "doc_id": "console-get-project",
                    "title": "Получить проект",
                    "method": "GET",
                    "path": "/console/api/v2/projects/{ProjectId}",
                    "parameters": [{"name": "ProjectId", "in": "path", "required": True}],
                    "responses": {"200": {"description": "OK"}},
                    "resolved_schema_doc_ids": ["console-schemas-projectdto"],
                    "source_url": "/docs/help/console/get-project/",
                },
                {
                    "doc_id": "console-post-project",
                    "title": "Создать проект",
                    "method": "POST",
                    "path": "/console/api/v2/projects",
                    "parameters": [],
                    "responses": {"201": {"description": "Created"}},
                    "resolved_schema_doc_ids": ["console-schemas-projectdto"],
                    "source_url": "/docs/help/console/post-project/",
                },
            ],
        ),
        "console.9.2.4-6.api-schemas": (
            "docs-console/versions/9.2.4-6/reference/api-schemas.jsonl",
            "api-schemas",
            "console",
            [
                {
                    "doc_id": "console-schemas-projectdto",
                    "title": "ProjectDto",
                    "description": "Карточка проекта",
                    "properties": [{"name": "id", "type": "string"}],
                    "source_url": "/docs/help/console/schemas/projectdto/",
                }
            ],
        ),
        "server.9.2.4-6.components": (
            "docs-server/versions/9.2.4-6/reference/components.jsonl",
            "components",
            "server",
            [
                {
                    "doc_id": "bundle-component-ide",
                    "component_id": "ide",
                    "title": "Встроенная IDE",
                    "summary": "Theia IDE",
                    "evidence_paths": ["ide/theia"],
                }
            ],
        ),
        "server.9.2.4-6.entrypoints": (
            "docs-server/versions/9.2.4-6/reference/entrypoints.jsonl",
            "entrypoints",
            "server",
            [
                {
                    "doc_id": "bundle-entrypoint-element-server",
                    "entrypoint_id": "element-server",
                    "title": "Element server",
                    "path": "element-server.sh",
                    "launch_chain": ["element-server.sh", "bin/element-server"],
                }
            ],
        ),
        "server.9.2.4-6.connections": (
            "docs-server/versions/9.2.4-6/reference/connections.jsonl",
            "connections",
            "server",
            [
                {
                    "connection_id": "ide-to-server",
                    "source_component_id": "ide",
                    "target_component_id": "server-runtime",
                    "protocol": "HTTP",
                    "evidence_paths": ["ide/theia", "instance-template/config"],
                }
            ],
        ),
    }
    datasets = []
    records = 0
    for dataset_id, (relative, name, corpus, rows) in reference_rows.items():
        metadata = _write_reference_jsonl(root / relative, rows)
        records += metadata["records"]
        datasets.append(
            {
                "id": dataset_id,
                "corpus": corpus,
                "product_version": "9.2.4-6",
                "name": name,
                "description": f"Test {name}",
                "path": relative,
                "format": "jsonl",
                "records": metadata["records"],
                "primary_key": None,
                "provenance": "test-fixture",
                "sha256": metadata["sha256"],
            }
        )
    _write_json(
        root / "reference-catalog.json",
        {
            "schema_version": 1,
            "purpose": "Test reference catalog",
            "datasets": datasets,
            "summary": {"datasets": len(datasets), "jsonl_records": records},
        },
    )
    return root


@pytest.fixture
def element_project_path(tmp_path: Path) -> Path:
    root = tmp_path / "example-project"
    sales = root / "Sales"
    types = sales / "Types"
    types.mkdir(parents=True)
    (root / "Project.yaml").write_text(
        "\n".join(
            [
                "Id: 11111111-1111-1111-1111-111111111111",
                "Presentation: Example application",
                "Vendor: ExampleVendor",
                "Name: ExampleProject",
                "Version: 1.2.3",
                "DevelopmentLanguage: English",
                "DefaultLanguage: Russian",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "Project.xbsl").write_text(
        'import Sales\n\n@ProjectUpdate(Id = "Update", Order = 1)\nmethod OnUpdate()\n;\n',
        encoding="utf-8",
    )
    (root / "Project.xprj").write_bytes(b"PK\x03\x04test-project-archive")
    (root / "gitflic-ci.yaml").write_text("job:\n  script: SECRET_VALUE\n", encoding="utf-8")
    (sales / "Subsystem.yaml").write_text("Using:\n    - Types\n", encoding="utf-8")
    (sales / "Orders.yaml").write_text(
        "\n".join(
            [
                "ElementKind: CommonModule",
                "Id: 22222222-2222-2222-2222-222222222222",
                "Name: Orders",
                "Environment: Server",
                "VisibilityScope: InProject",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (sales / "Orders.xbsl").write_text(
        "method FindOrder(Number: String): String\n    return Number\n;\n",
        encoding="utf-8",
    )
    (sales / "OrdersQueries.xbql").write_text(
        "Select Reference From Orders Where Number = &Number\n",
        encoding="utf-8",
    )
    (types / "OrderDto.yaml").write_text(
        "\n".join(
            [
                "ElementKind: Structure",
                "Id: 33333333-3333-3333-3333-333333333333",
                "Name: OrderDto",
                "Environment: ClientAndServer",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (types / "OrderDto.xbsl").write_text(
        "method Presentation(): String\n    return Number\n;\n",
        encoding="utf-8",
    )
    return root
