from __future__ import annotations

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


def _create_index(path: Path, corpus: str, chunks: list[dict]) -> None:
    database = sqlite3.connect(path / "index.sqlite")
    database.executescript(
        """
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
                "test-sha256",
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
    return root
