from __future__ import annotations

from pathlib import Path

import pytest

from element_mcp.corpus import CorpusRepository


def test_corpus_info_contains_versions(corpus_path: Path) -> None:
    info = CorpusRepository(corpus_path).info()
    assert [item["name"] for item in info["corpora"]] == ["lang", "console", "server"]
    assert info["corpora"][0]["versions"][0]["product_version"] == "9.2.4-6"
    assert "bundle_path" not in info["releases"][0]
    assert "documentation_path" not in info["releases"][0]


def test_search_returns_versioned_provenance(corpus_path: Path) -> None:
    result = CorpusRepository(corpus_path).search("ВебМетод", corpus="lang", limit=3)
    assert result["count"] >= 1
    first = result["results"][0]
    assert first["chunk_id"] == "lang:test:methods@9.2#c0000"
    assert first["product_version"] == "9.2.4-6"
    assert first["source_version"] == "9.2"
    assert first["provenance"] == "official-html"


def test_search_rejects_empty_query(corpus_path: Path) -> None:
    with pytest.raises(ValueError, match="двух символов"):
        CorpusRepository(corpus_path).search(" ")


def test_get_document_includes_neighbor(corpus_path: Path) -> None:
    result = CorpusRepository(corpus_path).document_context("lang:test:methods@9.2#c0000", context_chunks=1)
    assert [chunk["position"] for chunk in result["chunks"]] == [0, 1]
    assert result["chunks"][0]["selected"] is True
    assert result["chunks"][1]["selected"] is False
