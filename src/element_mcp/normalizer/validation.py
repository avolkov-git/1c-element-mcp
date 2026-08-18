from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from ..corpus import CorpusError
from ..references import ReferenceCatalogService
from .common import iter_jsonl, sha256_text, utc_now, write_json

CORPORA = ("lang", "console", "server")
REQUIRED_INDEX_FILES = (
    "documents.jsonl",
    "chunks.jsonl",
    "index.sqlite",
    "vectors.f16.npy",
    "vector-idf.npy",
    "vector-ids.jsonl",
    "vector-meta.json",
    "manifest.json",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def validate_corpus(root: Path, name: str, *, verify_content_hashes: bool = True) -> dict[str, Any]:
    directory = root / f"docs-{name}" / "corpus"
    errors: list[str] = []
    warnings: list[str] = []
    missing = [filename for filename in REQUIRED_INDEX_FILES if not (directory / filename).is_file()]
    if missing:
        return {"corpus": name, "errors": [f"missing {filename}" for filename in missing], "warnings": []}

    try:
        documents = list(iter_jsonl(directory / "documents.jsonl"))
        chunks = list(iter_jsonl(directory / "chunks.jsonl"))
        manifest = _read_json(directory / "manifest.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"corpus": name, "errors": [f"read error: {error}"], "warnings": []}

    document_ids = [row.get("id") for row in documents]
    chunk_ids = [row.get("id") for row in chunks]
    if len(document_ids) != len(set(document_ids)):
        errors.append("duplicate document ids")
    if len(chunk_ids) != len(set(chunk_ids)):
        errors.append("duplicate chunk ids")
    known_documents = set(document_ids)
    if verify_content_hashes:
        for row in documents:
            if sha256_text(row.get("text", "")) != row.get("sha256"):
                errors.append(f"document sha mismatch: {row.get('id')}")
                break
        for row in chunks:
            if row.get("document_id") not in known_documents:
                errors.append(f"orphan chunk: {row.get('id')}")
                break
            if sha256_text(row.get("text", "")) != row.get("sha256"):
                errors.append(f"chunk sha mismatch: {row.get('id')}")
                break

    try:
        with sqlite3.connect(directory / "index.sqlite") as database:
            integrity = database.execute("PRAGMA quick_check").fetchone()[0]
            database_documents = database.execute("SELECT count(*) FROM documents").fetchone()[0]
            database_chunks = database.execute("SELECT count(*) FROM chunks").fetchone()[0]
            database_fts = database.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        if integrity != "ok":
            errors.append(f"SQLite quick_check: {integrity}")
        if database_documents != len(documents):
            errors.append(f"SQLite documents={database_documents}, JSONL={len(documents)}")
        if database_chunks != len(chunks) or database_fts != len(chunks):
            errors.append(f"SQLite chunks={database_chunks}, FTS={database_fts}, JSONL={len(chunks)}")
    except sqlite3.Error as error:
        errors.append(f"SQLite error: {error}")

    vector_shape: list[int] | None = None
    try:
        matrix = np.load(directory / "vectors.f16.npy", mmap_mode="r")
        idf = np.load(directory / "vector-idf.npy", mmap_mode="r")
        vector_ids = list(iter_jsonl(directory / "vector-ids.jsonl"))
        vector_shape = list(matrix.shape)
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks) or len(vector_ids) != len(chunks):
            errors.append(f"vector rows={matrix.shape[0]}, ids={len(vector_ids)}, chunks={len(chunks)}")
        if matrix.ndim == 2 and matrix.shape[1] != len(idf):
            errors.append(f"vector dimensions={matrix.shape[1]}, idf={len(idf)}")
    except (OSError, ValueError) as error:
        errors.append(f"vector error: {error}")

    if manifest.get("documents") != len(documents):
        errors.append(f"manifest documents={manifest.get('documents')}, JSONL={len(documents)}")
    if manifest.get("chunks") != len(chunks):
        errors.append(f"manifest chunks={manifest.get('chunks')}, JSONL={len(chunks)}")
    if not documents or not chunks:
        errors.append("empty corpus")
    artifact_sha256 = None
    if verify_content_hashes:
        artifact_sha256 = {
            filename: _sha256_file(directory / filename)
            for filename in (
                "documents.jsonl",
                "chunks.jsonl",
                "vectors.f16.npy",
                "vector-idf.npy",
                "vector-ids.jsonl",
            )
        }
    return {
        "corpus": name,
        "documents": len(documents),
        "chunks": len(chunks),
        "documents_sha256": manifest.get("documents_sha256"),
        "vector_input_sha256": manifest.get("vectors", {}).get("input_sha256"),
        "vector_shape": vector_shape,
        "artifact_sha256": artifact_sha256,
        "errors": errors,
        "warnings": warnings,
    }


def validate_corpus_root(
    root: str | Path,
    *,
    verify_content_hashes: bool = True,
    write_report: bool = False,
) -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        manifest = _read_json(root_path / "manifest.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            "status": "invalid",
            "path": str(root_path),
            "errors": [f"manifest: {error}"],
            "corpora": [],
        }
    reports = [validate_corpus(root_path, name, verify_content_hashes=verify_content_hashes) for name in CORPORA]
    errors.extend(error for report in reports for error in report["errors"])
    reference_report: dict[str, Any]
    try:
        references = ReferenceCatalogService(root_path)
        reference_report = references.status()
        listed_datasets = references.list_datasets(limit=100)["items"] if references.available else []
        reference_report["dataset_checks"] = [
            {"id": row["id"], "records": row.get("records"), "sha256": row.get("sha256")} for row in listed_datasets
        ]
        if references.available and verify_content_hashes:
            for dataset in listed_datasets:
                references.query(dataset["id"], limit=1)
            reference_report["content_hashes_verified"] = True
        elif references.available:
            reference_report["content_hashes_verified"] = False
        elif manifest.get("reference_catalog"):
            errors.append("manifest declares reference_catalog, but reference-catalog.json is missing")
        else:
            warnings.append("structured reference catalog is unavailable; full-text corpus remains usable")
    except CorpusError as error:
        reference_report = {"status": "invalid", "error": str(error)}
        errors.append(f"reference catalog: {error}")
    aggregate = {
        "documents": sum(report.get("documents", 0) for report in reports),
        "chunks": sum(report.get("chunks", 0) for report in reports),
    }
    report = {
        "status": "ready" if not errors else "invalid",
        "path": str(root_path),
        "schema_version": manifest.get("schema_version"),
        "normalizer_version": manifest.get("normalizer_version"),
        "guide_set_version": manifest.get("guide_set_version"),
        "releases": manifest.get("releases", []),
        "created_at": utc_now(),
        "aggregate": aggregate,
        "corpora": reports,
        "references": reference_report,
        "errors": errors,
        "warnings": warnings,
    }
    if write_report:
        write_json(root_path / "validation-report.json", report)
    return report
