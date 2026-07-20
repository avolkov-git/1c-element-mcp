from __future__ import annotations

from pathlib import Path

import pytest

from element_mcp.config import discover_corpus_path


def test_explicit_corpus_path_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("ELEMENT_DOCS_PATH", str(tmp_path / "environment"))
    assert discover_corpus_path(explicit) == explicit.resolve()


def test_environment_corpus_path_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "environment"
    monkeypatch.setenv("ELEMENT_DOCS_PATH", str(configured))
    assert discover_corpus_path() == configured.resolve()
