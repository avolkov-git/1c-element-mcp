from __future__ import annotations

from pathlib import Path

import pytest

from element_mcp.config import ConfigurationStore, discover_corpus_path, discover_update_source_path


def test_explicit_corpus_path_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("ELEMENT_DOCS_PATH", str(tmp_path / "environment"))
    assert discover_corpus_path(explicit) == explicit.resolve()


def test_environment_corpus_path_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "environment"
    monkeypatch.setenv("ELEMENT_DOCS_PATH", str(configured))
    assert discover_corpus_path() == configured.resolve()


def test_persisted_corpus_path_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEMENT_DOCS_PATH", raising=False)
    config = ConfigurationStore(tmp_path / "config.json")
    corpus = tmp_path / "corpus"
    config.activate(corpus)
    assert discover_corpus_path(config_store=config) == corpus.resolve()


def test_persisted_local_update_source_overrides_command_line_default(tmp_path: Path) -> None:
    config = ConfigurationStore(tmp_path / "config.json")
    source = tmp_path / "selected-source"
    config.configure_update_source(source)

    assert discover_update_source_path(tmp_path / "installer-source", config_store=config) == source.resolve()


def test_persisted_remote_source_clears_command_line_default(tmp_path: Path) -> None:
    config = ConfigurationStore(tmp_path / "config.json")
    config.configure_update_source(None)

    assert discover_update_source_path(tmp_path / "installer-source", config_store=config) is None


def test_update_source_and_active_corpus_share_configuration(tmp_path: Path) -> None:
    config = ConfigurationStore(tmp_path / "config.json")
    corpus = tmp_path / "corpus"
    source = tmp_path / "source"

    config.activate(corpus, metadata={"version": "test"})
    config.configure_update_source(source)

    assert config.active_corpus_path() == corpus.resolve()
    assert config.read()["active_corpus"] == {"version": "test"}
    assert config.update_source().path == source.resolve()  # type: ignore[union-attr]
