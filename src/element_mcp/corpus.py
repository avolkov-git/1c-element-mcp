from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import unicodedata
from collections import Counter
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import numpy as np

CorpusName = Literal["lang", "console", "server", "all"]
CORPORA = ("lang", "console", "server")
TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_:.\-/]{2,}")


class CorpusError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CorpusError(f"Отсутствует файл корпуса: {path}") from error
    except json.JSONDecodeError as error:
        raise CorpusError(f"Повреждён JSON корпуса {path}: {error}") from error


def _iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as error:
                        raise CorpusError(f"Повреждён JSONL {path}:{line_number}: {error}") from error
    except FileNotFoundError as error:
        raise CorpusError(f"Отсутствует файл корпуса: {path}") from error


def _split_camel(token: str) -> list[str]:
    token = token.replace("::", "_").replace("/", "_").replace(".", "_").replace("-", "_")
    pieces: list[str] = []
    for part in token.split("_"):
        pieces.extend(re.findall(r"[A-ZА-ЯЁ]+(?=[A-ZА-ЯЁ][a-zа-яё]|$)|[A-ZА-ЯЁ]?[a-zа-яё]+|\d+", part))
    return [piece.lower() for piece in pieces if len(piece) > 1]


def _text_features(text: str) -> list[str]:
    raw = [token.lower() for token in TOKEN_RE.findall(unicodedata.normalize("NFKC", text))]
    features: list[str] = []
    for token in raw:
        features.append("w:" + token)
        features.extend("p:" + part for part in _split_camel(token))
        compact = re.sub(r"[^0-9a-zа-яё]", "", token)
        if 5 <= len(compact) <= 80:
            padded = "^" + compact + "$"
            features.extend("c:" + padded[index : index + 3] for index in range(len(padded) - 2))
    lexical = [feature for feature in features if feature.startswith(("w:", "p:"))]
    features.extend("b:" + lexical[index] + "+" + lexical[index + 1] for index in range(len(lexical) - 1))
    return features


def _feature_bucket(feature: str, dimensions: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8, person=b"e1c-docs").digest()
    value = struct.unpack("<Q", digest)[0]
    return value % dimensions, -1.0 if value & (1 << 63) else 1.0


def build_query_vector(query: str, idf: np.ndarray) -> np.ndarray:
    counts: Counter[int] = Counter()
    signs: dict[int, float] = {}
    for feature in _text_features(query):
        bucket, sign = _feature_bucket(feature, len(idf))
        counts[bucket] += 1
        signs[bucket] = signs.get(bucket, 0.0) + sign

    vector = np.zeros(len(idf), dtype=np.float32)
    for bucket, count in counts.items():
        sign = 1.0 if signs.get(bucket, 1.0) >= 0 else -1.0
        vector[bucket] = sign * (1.0 + math.log(count)) * float(idf[bucket])
    norm = np.linalg.norm(vector)
    if norm:
        vector /= norm
    return vector


def _fts_expression(query: str) -> str:
    tokens = TOKEN_RE.findall(query)
    if not tokens:
        raise ValueError("Запрос не содержит индексируемых слов")
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:32])


def _snippet(text: str, maximum: int = 1200) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= maximum:
        return compact
    cut = compact.rfind(" ", 0, maximum)
    return compact[: cut if cut >= maximum // 2 else maximum].rstrip() + "…"


class CorpusIndex:
    def __init__(self, root: Path, name: str) -> None:
        self.name = name
        self.path = root / f"docs-{name}" / "corpus"
        self.database_path = self.path / "index.sqlite"

    def ensure_available(self) -> None:
        required = (
            self.database_path,
            self.path / "manifest.json",
            self.path / "vectors.f16.npy",
            self.path / "vector-idf.npy",
            self.path / "vector-ids.jsonl",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise CorpusError("В корпусе отсутствуют обязательные файлы: " + ", ".join(missing))

    @cached_property
    def manifest(self) -> dict[str, Any]:
        return _read_json(self.path / "manifest.json")

    @cached_property
    def matrix(self) -> np.ndarray:
        try:
            return np.load(self.path / "vectors.f16.npy", mmap_mode="r")
        except (FileNotFoundError, ValueError) as error:
            raise CorpusError(f"Не удалось открыть векторный индекс {self.name}: {error}") from error

    @cached_property
    def idf(self) -> np.ndarray:
        try:
            return np.load(self.path / "vector-idf.npy")
        except (FileNotFoundError, ValueError) as error:
            raise CorpusError(f"Не удалось открыть IDF корпуса {self.name}: {error}") from error

    @cached_property
    def vector_ids(self) -> list[str]:
        return [row["chunk_id"] for row in _iter_jsonl(self.path / "vector-ids.jsonl")]

    def connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise CorpusError(f"Не найден поисковый индекс: {self.database_path}")
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def versions(self) -> list[dict[str, Any]]:
        with self.connect() as database:
            rows = database.execute(
                """
                SELECT product_version, source_version, MAX(is_current) AS is_current,
                       COUNT(DISTINCT document_id) AS documents, COUNT(*) AS chunks
                FROM chunks
                GROUP BY product_version, source_version
                ORDER BY is_current DESC, product_version DESC, source_version DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self,
        query: str,
        limit: int,
        current_only: bool,
        product_version: str | None,
    ) -> list[dict[str, Any]]:
        self.ensure_available()
        clauses: list[str] = []
        parameters: list[Any] = [_fts_expression(query)]
        if current_only:
            clauses.append("c.is_current = 1")
        if product_version:
            clauses.append("c.product_version = ?")
            parameters.append(product_version)
        where = " AND " + " AND ".join(clauses) if clauses else ""
        candidate_limit = max(limit * 8, 50)
        parameters.append(candidate_limit)

        with self.connect() as database:
            lexical_rows = database.execute(
                f"""
                SELECT c.*, bm25(chunks_fts, 5.0, 2.0, 1.5, 1.0) AS lexical_score
                FROM chunks_fts
                JOIN chunks c ON c.rowid = chunks_fts.rowid
                WHERE chunks_fts MATCH ? {where}
                ORDER BY lexical_score
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            lexical = {row["id"]: (rank, dict(row)) for rank, row in enumerate(lexical_rows, 1)}

            if self.matrix.ndim != 2 or self.matrix.shape[0] != len(self.vector_ids):
                raise CorpusError(f"Размер векторного индекса {self.name} не совпадает с vector-ids.jsonl")
            if self.matrix.shape[1] != len(self.idf):
                raise CorpusError(f"Размерность векторного индекса {self.name} не совпадает с vector-idf.npy")

            query_vector = build_query_vector(query, self.idf)
            vector_rank: dict[str, tuple[int, float]] = {}
            if self.matrix.shape[0] and np.linalg.norm(query_vector):
                scores = np.asarray(self.matrix @ query_vector, dtype=np.float32)
                count = min(len(scores), candidate_limit)
                if count == len(scores):
                    top = np.argsort(scores)[::-1]
                else:
                    top = np.argpartition(scores, -count)[-count:]
                    top = top[np.argsort(scores[top])[::-1]]
                for rank, row_index in enumerate(top, 1):
                    chunk_id = self.vector_ids[int(row_index)]
                    vector_rank[chunk_id] = (rank, float(scores[int(row_index)]))

            ranked: list[tuple[float, str]] = []
            for chunk_id in set(lexical) | set(vector_rank):
                hybrid_score = 0.0
                if chunk_id in lexical:
                    hybrid_score += 2.0 / (50 + lexical[chunk_id][0])
                if chunk_id in vector_rank:
                    hybrid_score += 0.65 / (50 + vector_rank[chunk_id][0])
                ranked.append((hybrid_score, chunk_id))
            ranked.sort(reverse=True)

            results: list[dict[str, Any]] = []
            for hybrid_score, chunk_id in ranked:
                row = lexical.get(chunk_id, (None, None))[1]
                if row is None:
                    fetched = database.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                    if fetched is None:
                        continue
                    row = dict(fetched)
                if current_only and not row["is_current"]:
                    continue
                if product_version and row["product_version"] != product_version:
                    continue
                results.append(
                    {
                        "chunk_id": row["id"],
                        "document_id": row["document_id"],
                        "logical_id": row["logical_id"],
                        "corpus": row["corpus"],
                        "title": row["title"],
                        "section": row["section"],
                        "kind": row["kind"],
                        "product_version": row["product_version"],
                        "source_version": row["source_version"],
                        "source_path": row["source_path"],
                        "normalized_path": row["normalized_path"],
                        "provenance": row["provenance"],
                        "score": round(hybrid_score, 8),
                        "vector_score": (round(vector_rank[chunk_id][1], 8) if chunk_id in vector_rank else None),
                        "snippet": _snippet(row["text"]),
                    }
                )
                if len(results) >= limit:
                    break
            return results

    def document_context(self, chunk_id: str, context_chunks: int) -> dict[str, Any]:
        with self.connect() as database:
            selected = database.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
            if selected is None:
                raise CorpusError(f"Чанк не найден: {chunk_id}")
            start = max(0, int(selected["position"]) - context_chunks)
            end = int(selected["position"]) + context_chunks
            rows = database.execute(
                """
                SELECT * FROM chunks
                WHERE document_id = ? AND position BETWEEN ? AND ?
                ORDER BY position
                """,
                (selected["document_id"], start, end),
            ).fetchall()
        return {
            "requested_chunk_id": chunk_id,
            "document_id": selected["document_id"],
            "logical_id": selected["logical_id"],
            "corpus": selected["corpus"],
            "title": selected["title"],
            "kind": selected["kind"],
            "product_version": selected["product_version"],
            "source_version": selected["source_version"],
            "source_path": selected["source_path"],
            "normalized_path": selected["normalized_path"],
            "provenance": selected["provenance"],
            "chunks": [
                {
                    "chunk_id": row["id"],
                    "section": row["section"],
                    "position": row["position"],
                    "selected": row["id"] == chunk_id,
                    "text": row["text"],
                }
                for row in rows
            ],
        }


class CorpusRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        if not (self.root / "manifest.json").is_file():
            raise CorpusError(f"Каталог не похож на codex-docs: {self.root}")
        self.indexes = {name: CorpusIndex(self.root, name) for name in CORPORA}

    @cached_property
    def manifest(self) -> dict[str, Any]:
        return _read_json(self.root / "manifest.json")

    def info(self) -> dict[str, Any]:
        corpora: list[dict[str, Any]] = []
        for name, index in self.indexes.items():
            index.ensure_available()
            corpora.append(
                {
                    "name": name,
                    "documents": index.manifest.get("documents"),
                    "chunks": index.manifest.get("chunks"),
                    "created_at": index.manifest.get("created_at"),
                    "index_schema_version": index.manifest.get("index_schema_version"),
                    "vectors": index.manifest.get("vectors"),
                    "versions": index.versions(),
                }
            )
        public_release_fields = (
            "release_id",
            "product",
            "product_version",
            "documentation_version",
            "language",
            "current",
        )
        releases = [
            {field: release[field] for field in public_release_fields if field in release}
            for release in self.manifest.get("releases", [])
        ]
        return {
            "schema_version": self.manifest.get("schema_version"),
            "created_at": self.manifest.get("created_at"),
            "releases": releases,
            "corpora": corpora,
        }

    def search(
        self,
        query: str,
        corpus: CorpusName = "all",
        limit: int = 8,
        current_only: bool = True,
        product_version: str | None = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if len(query) < 2:
            raise ValueError("Запрос должен содержать не менее двух символов")
        if corpus not in (*CORPORA, "all"):
            raise ValueError(f"Неизвестный корпус: {corpus}")
        if not 1 <= limit <= 20:
            raise ValueError("limit должен находиться в диапазоне 1..20")

        names = CORPORA if corpus == "all" else (corpus,)
        results: list[dict[str, Any]] = []
        for name in names:
            results.extend(
                self.indexes[name].search(
                    query=query,
                    limit=max(limit, 8) if corpus == "all" else limit,
                    current_only=current_only,
                    product_version=product_version,
                )
            )
        results.sort(key=lambda row: (row["score"], row.get("vector_score") or -1.0), reverse=True)
        return {
            "query": query,
            "corpus": corpus,
            "current_only": current_only,
            "product_version": product_version,
            "count": min(len(results), limit),
            "results": results[:limit],
        }

    def document_context(self, chunk_id: str, context_chunks: int = 1) -> dict[str, Any]:
        if not 0 <= context_chunks <= 2:
            raise ValueError("context_chunks должен находиться в диапазоне 0..2")
        prefix = chunk_id.split(":", 1)[0]
        if prefix in self.indexes:
            return self.indexes[prefix].document_context(chunk_id, context_chunks)
        for index in self.indexes.values():
            try:
                return index.document_context(chunk_id, context_chunks)
            except CorpusError as error:
                if not str(error).startswith("Чанк не найден:"):
                    raise
        raise CorpusError(f"Чанк не найден: {chunk_id}")
