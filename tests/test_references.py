from __future__ import annotations

import json
from pathlib import Path

import pytest

from element_mcp.corpus import CorpusError
from element_mcp.references import ReferenceCatalogService


def test_list_and_query_reference_datasets(corpus_path: Path) -> None:
    service = ReferenceCatalogService(corpus_path)

    listed = service.list_datasets(corpus="console", limit=2)
    assert listed["status"] == "ready"
    assert listed["total"] == 2

    result = service.query(
        "console.9.2.4-6.api-operations",
        filters={"method": "get"},
        offset=0,
        limit=1,
    )
    assert result["total"] == 1
    assert result["items"][0]["path"] == "/console/api/v2/projects/{ProjectId}"
    assert result["has_more"] is False

    first_page = service.query("console.9.2.4-6.api-operations", limit=1)
    second_page = service.query("console.9.2.4-6.api-operations", offset=1, limit=1)
    assert first_page["total"] == second_page["total"] == 2
    assert first_page["has_more"] is True
    assert first_page["items"][0]["doc_id"] != second_page["items"][0]["doc_id"]


def test_typed_api_operation_resolves_schema(corpus_path: Path) -> None:
    result = ReferenceCatalogService(corpus_path).get_api_operation(
        "GET",
        "/console/api/v2/projects/{ProjectId}",
    )

    assert result["product_version"] == "9.2.4-6"
    assert result["operation"]["title"] == "Получить проект"
    assert result["resolved_schemas"][0]["title"] == "ProjectDto"


def test_typed_server_references(corpus_path: Path) -> None:
    service = ReferenceCatalogService(corpus_path)

    component = service.get_server_component("ide")
    entrypoint = service.get_server_entrypoint("element-server")
    connections = service.get_component_connections("ide")

    assert component["component"]["title"] == "Встроенная IDE"
    assert entrypoint["entrypoint"]["path"] == "element-server.sh"
    assert connections["total"] == 1


def test_missing_reference_catalog_is_backward_compatible(tmp_path: Path) -> None:
    service = ReferenceCatalogService(tmp_path)
    assert service.status()["status"] == "unavailable"
    assert service.list_datasets()["items"] == []
    with pytest.raises(CorpusError, match="нет справочника"):
        service.get_api_schema("ProjectDto")


def test_catalog_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "reference-catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "datasets": [
                    {
                        "id": "server.9.2.files",
                        "name": "files",
                        "path": "../secret.jsonl",
                        "format": "jsonl",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CorpusError, match="границы корпуса"):
        ReferenceCatalogService(tmp_path)


def test_query_rejects_hash_mismatch(corpus_path: Path) -> None:
    path = corpus_path / "docs-console/versions/9.2.4-6/reference/api-schemas.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(CorpusError, match="SHA-256"):
        ReferenceCatalogService(corpus_path).query("console.9.2.4-6.api-schemas")
