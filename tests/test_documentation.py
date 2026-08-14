from __future__ import annotations

from pathlib import Path

import pytest

from element_mcp.config import ServerSettings
from element_mcp.corpus import CorpusError
from element_mcp.documentation import DocumentationService


def test_server_can_start_without_corpus(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ELEMENT_DOCS_PATH", raising=False)
    settings = ServerSettings(config_path=tmp_path / "config.json", data_path=tmp_path / "data")
    status = DocumentationService(settings).documentation_status()
    assert status["status"] == "missing"
    assert status["path"] is None


def test_existing_corpus_can_be_activated(tmp_path: Path, corpus_path: Path) -> None:
    settings = ServerSettings(config_path=tmp_path / "config.json", data_path=tmp_path / "data")
    service = DocumentationService(settings)
    activated = service.activate(corpus_path)
    assert activated["status"] == "ready"
    assert activated["path_source"] == "configuration"
    assert activated["path_change_supported"] is True
    assert service.active_path() == corpus_path.resolve()


def test_startup_corpus_path_is_reported_as_fixed(tmp_path: Path, corpus_path: Path) -> None:
    service = DocumentationService(
        ServerSettings(
            corpus_path=corpus_path,
            config_path=tmp_path / "config.json",
            data_path=tmp_path / "data",
        )
    )

    status = service.documentation_status()

    assert status["path_source"] == "startup_argument"
    assert status["path_change_supported"] is False
    with pytest.raises(CorpusError, match="--corpus-path"):
        service.activate(tmp_path / "other-corpus")
