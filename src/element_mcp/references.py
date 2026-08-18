from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .corpus import CorpusError

CATALOG_FILENAME = "reference-catalog.json"
CATALOG_SCHEMA_VERSION = 1
MAX_FILTERS = 8
MAX_QUERY_LENGTH = 500
MAX_RESULT_LIMIT = 100
MAX_RESULT_BYTES = 2 * 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _nested_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _nested_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_values(child)
    else:
        yield value


def _field_value(row: dict[str, Any], field: str) -> Any:
    value: Any = row
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _matches_exact(value: Any, expected: str) -> bool:
    expected_folded = expected.casefold()
    if isinstance(value, list):
        return any(_matches_exact(item, expected) for item in value)
    if isinstance(value, (dict, list)):
        return False
    return str(value).casefold() == expected_folded


def _matches_query(row: dict[str, Any], query: str) -> bool:
    needle = query.casefold()
    return any(needle in str(value).casefold() for value in _nested_values(row))


class ReferenceCatalogService:
    """Bounded, read-only access to versioned structured corpus datasets."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.catalog_path = self.root / CATALOG_FILENAME
        self._verified: dict[str, tuple[int, int]] = {}
        self._catalog = self._load_catalog()
        self._datasets = {row["id"]: row for row in self._catalog.get("datasets", [])}

    @property
    def available(self) -> bool:
        return self.catalog_path.is_file()

    def _load_catalog(self) -> dict[str, Any]:
        if not self.catalog_path.is_file():
            return {
                "schema_version": CATALOG_SCHEMA_VERSION,
                "datasets": [],
                "summary": {"datasets": 0, "jsonl_records": 0},
            }
        try:
            value = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CorpusError(f"Не удалось прочитать {CATALOG_FILENAME}: {error}") from error
        if value.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise CorpusError(
                f"Неподдерживаемая схема {CATALOG_FILENAME}: {value.get('schema_version')}; "
                f"поддерживается {CATALOG_SCHEMA_VERSION}"
            )
        datasets = value.get("datasets")
        if not isinstance(datasets, list):
            raise CorpusError(f"Поле datasets в {CATALOG_FILENAME} должно быть массивом")
        seen: set[str] = set()
        for position, dataset in enumerate(datasets):
            if not isinstance(dataset, dict):
                raise CorpusError(f"datasets[{position}] должен быть объектом")
            dataset_id = dataset.get("id")
            if not isinstance(dataset_id, str) or not dataset_id or dataset_id in seen:
                raise CorpusError(f"Некорректный или повторяющийся dataset id: {dataset_id!r}")
            seen.add(dataset_id)
            self._dataset_path(dataset)
            if dataset.get("format") not in {"json", "jsonl"}:
                raise CorpusError(f"Неподдерживаемый формат dataset {dataset_id}: {dataset.get('format')}")
            corpus = dataset.get("corpus")
            version = dataset.get("product_version")
            name = dataset.get("name")
            if corpus not in {"lang", "console", "server"} or not all(
                isinstance(value, str) and value for value in (version, name)
            ):
                raise CorpusError(f"Dataset {dataset_id} содержит некорректные corpus, product_version или name")
            if dataset_id != f"{corpus}.{version}.{name}":
                raise CorpusError(f"Dataset id не соответствует corpus/version/name: {dataset_id}")
            records = dataset.get("records")
            if records is not None and (not isinstance(records, int) or isinstance(records, bool) or records < 0):
                raise CorpusError(f"Dataset {dataset_id} содержит некорректное число records")
            expected_hash = dataset.get("sha256")
            if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                raise CorpusError(f"Dataset {dataset_id} не содержит корректный SHA-256")
            try:
                int(expected_hash, 16)
            except ValueError as error:
                raise CorpusError(f"Dataset {dataset_id} не содержит корректный SHA-256") from error
        return value

    def _dataset_path(self, dataset: dict[str, Any]) -> Path:
        raw = dataset.get("path")
        if not isinstance(raw, str) or not raw:
            raise CorpusError(f"Dataset {dataset.get('id')} не содержит относительный path")
        relative = PurePosixPath(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise CorpusError(f"Dataset {dataset.get('id')} выходит за границы корпуса")
        path = (self.root / Path(*relative.parts)).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise CorpusError(f"Dataset {dataset.get('id')} выходит за границы корпуса") from error
        return path

    def _verify_dataset(self, dataset: dict[str, Any]) -> Path:
        path = self._dataset_path(dataset)
        dataset_id = dataset["id"]
        if not path.is_file():
            raise CorpusError(f"Не найден dataset {dataset_id}: {dataset.get('path')}")
        before = path.stat()
        fingerprint = (before.st_mtime_ns, before.st_size)
        if self._verified.get(dataset_id) == fingerprint:
            return path
        expected = dataset.get("sha256")
        actual = _sha256_file(path)
        after = path.stat()
        if (after.st_mtime_ns, after.st_size) != fingerprint:
            raise CorpusError(f"Dataset {dataset_id} изменился во время проверки")
        if actual != expected:
            raise CorpusError(f"SHA-256 dataset {dataset_id} не совпадает с каталогом")
        self._verified[dataset_id] = fingerprint
        return path

    def status(self) -> dict[str, Any]:
        if not self.available:
            return {
                "status": "unavailable",
                "message": "В корпусе нет структурированного справочного каталога",
                "catalog_path": None,
                "schema_version": None,
                "datasets": 0,
                "jsonl_records": 0,
            }
        summary = self._catalog.get("summary") or {}
        return {
            "status": "ready",
            "catalog_path": str(self.catalog_path),
            "schema_version": self._catalog.get("schema_version"),
            "datasets": len(self._datasets),
            "jsonl_records": summary.get("jsonl_records", 0),
        }

    def list_datasets(
        self,
        *,
        corpus: str | None = None,
        product_version: str | None = None,
        name: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not self.available:
            return {**self.status(), "items": [], "offset": offset, "limit": limit, "total": 0}
        rows = sorted(self._datasets.values(), key=lambda row: row["id"])
        if corpus:
            rows = [row for row in rows if row.get("corpus") == corpus]
        if product_version:
            rows = [row for row in rows if row.get("product_version") == product_version]
        if name:
            rows = [row for row in rows if row.get("name") == name]
        return {
            "status": "ready",
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "items": rows[offset : offset + limit],
        }

    def _select_dataset(self, name: str, product_version: str | None = None) -> dict[str, Any]:
        matches = [
            row
            for row in self._datasets.values()
            if row.get("name") == name and (product_version is None or row.get("product_version") == product_version)
        ]
        if not matches:
            suffix = f" для Element {product_version}" if product_version else ""
            raise CorpusError(f"В активном корпусе нет справочника {name}{suffix}")
        if product_version is None:
            try:
                manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
                current = next(
                    (row.get("product_version") for row in manifest.get("releases", []) if row.get("current")),
                    None,
                )
            except (OSError, json.JSONDecodeError):
                current = None
            if current:
                current_match = next((row for row in matches if row.get("product_version") == current), None)
                if current_match:
                    return current_match
        return sorted(matches, key=lambda row: str(row.get("product_version")), reverse=True)[0]

    def _iter_rows(self, dataset: dict[str, Any]) -> Iterable[dict[str, Any]]:
        path = self._verify_dataset(dataset)
        fingerprint = (path.stat().st_mtime_ns, path.stat().st_size)
        try:
            if dataset["format"] == "jsonl":
                count = 0
                with path.open(encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, 1):
                        if not line.strip():
                            continue
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise CorpusError(f"Dataset {dataset['id']}:{line_number} должен содержать JSON-объект")
                        count += 1
                        yield value
                expected_count = dataset.get("records")
                if expected_count is not None and count != expected_count:
                    raise CorpusError(
                        f"Число записей dataset {dataset['id']} не совпадает: {count}, ожидалось {expected_count}"
                    )
                return
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                for key, child in value.items():
                    yield {"key": key, "value": child}
            elif isinstance(value, list):
                for child in value:
                    yield child if isinstance(child, dict) else {"value": child}
            else:
                yield {"value": value}
        except json.JSONDecodeError as error:
            raise CorpusError(f"Повреждён dataset {dataset['id']}: {error}") from error
        after = path.stat()
        if (after.st_mtime_ns, after.st_size) != fingerprint:
            self._verified.pop(dataset["id"], None)
            raise CorpusError(f"Dataset {dataset['id']} изменился во время чтения")

    def query(
        self,
        dataset_id: str,
        *,
        query: str | None = None,
        filters: dict[str, str] | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        dataset = self._datasets.get(dataset_id)
        if dataset is None:
            raise CorpusError(f"Неизвестный dataset id: {dataset_id}")
        if query is not None and len(query) > MAX_QUERY_LENGTH:
            raise CorpusError(f"Запрос длиннее {MAX_QUERY_LENGTH} символов")
        exact = filters or {}
        if len(exact) > MAX_FILTERS:
            raise CorpusError(f"Разрешено не более {MAX_FILTERS} точных фильтров")
        for field, value in exact.items():
            if not field or len(field) > 128 or len(str(value)) > 500:
                raise CorpusError("Некорректное имя поля или значение фильтра")
        items: list[dict[str, Any]] = []
        total = 0
        result_bytes = 0
        truncated_by_size = False
        for row in self._iter_rows(dataset):
            if query and not _matches_query(row, query):
                continue
            if any(not _matches_exact(_field_value(row, field), str(value)) for field, value in exact.items()):
                continue
            if total >= offset and len(items) < limit:
                encoded_size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
                if result_bytes + encoded_size > MAX_RESULT_BYTES:
                    truncated_by_size = True
                elif not truncated_by_size:
                    items.append(row)
                    result_bytes += encoded_size
            total += 1
        return {
            "status": "ready",
            "dataset": dataset,
            "query": query,
            "filters": exact,
            "offset": offset,
            "limit": limit,
            "total": total,
            "items": items,
            "has_more": offset + len(items) < total,
            "truncated_by_size": truncated_by_size,
        }

    def _find_one(
        self,
        name: str,
        filters: dict[str, str],
        product_version: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        dataset = self._select_dataset(name, product_version)
        result = self.query(dataset["id"], filters=filters, limit=2)
        if not result["items"]:
            raise CorpusError(f"Запись не найдена в {dataset['id']}: {filters}")
        if result["total"] > 1:
            raise CorpusError(f"Запрос неоднозначен в {dataset['id']}: {filters}")
        return dataset, result["items"][0]

    def get_api_operation(self, method: str, path: str, product_version: str | None = None) -> dict[str, Any]:
        dataset, operation = self._find_one(
            "api-operations",
            {"method": method.upper(), "path": path},
            product_version,
        )
        schemas: list[dict[str, Any]] = []
        schema_dataset: dict[str, Any] | None = None
        try:
            schema_dataset = self._select_dataset("api-schemas", dataset.get("product_version"))
            for doc_id in operation.get("resolved_schema_doc_ids", []):
                found = self.query(schema_dataset["id"], filters={"doc_id": str(doc_id)}, limit=1)
                if found["items"]:
                    schemas.append(found["items"][0])
        except CorpusError:
            schema_dataset = None
        return {
            "status": "ready",
            "product_version": dataset.get("product_version"),
            "provenance": dataset.get("provenance"),
            "dataset_id": dataset["id"],
            "operation": operation,
            "resolved_schemas": schemas,
            "schema_dataset_id": schema_dataset and schema_dataset["id"],
        }

    def get_api_schema(self, title: str, product_version: str | None = None) -> dict[str, Any]:
        dataset, schema = self._find_one("api-schemas", {"title": title}, product_version)
        return {
            "status": "ready",
            "product_version": dataset.get("product_version"),
            "provenance": dataset.get("provenance"),
            "dataset_id": dataset["id"],
            "schema": schema,
        }

    def get_server_component(self, component_id: str, product_version: str | None = None) -> dict[str, Any]:
        dataset, component = self._find_one("components", {"component_id": component_id}, product_version)
        return {
            "status": "ready",
            "product_version": dataset.get("product_version"),
            "provenance": dataset.get("provenance"),
            "dataset_id": dataset["id"],
            "component": component,
        }

    def get_server_entrypoint(self, entrypoint_id: str, product_version: str | None = None) -> dict[str, Any]:
        dataset, entrypoint = self._find_one("entrypoints", {"entrypoint_id": entrypoint_id}, product_version)
        return {
            "status": "ready",
            "product_version": dataset.get("product_version"),
            "provenance": dataset.get("provenance"),
            "dataset_id": dataset["id"],
            "entrypoint": entrypoint,
        }

    def get_component_connections(self, component_id: str, product_version: str | None = None) -> dict[str, Any]:
        dataset = self._select_dataset("connections", product_version)
        items = []
        for row in self._iter_rows(dataset):
            candidates = {
                str(row.get("source_component_id") or ""),
                str(row.get("target_component_id") or ""),
                str(row.get("source_doc_id") or ""),
                str(row.get("target_doc_id") or ""),
            }
            candidates.update(str(value) for value in row.get("related_component_ids", []))
            if component_id in candidates or any(value.endswith("-" + component_id) for value in candidates):
                items.append(row)
        return {
            "status": "ready",
            "product_version": dataset.get("product_version"),
            "provenance": dataset.get("provenance"),
            "dataset_id": dataset["id"],
            "component_id": component_id,
            "total": len(items),
            "items": items[:100],
            "truncated": len(items) > 100,
        }
