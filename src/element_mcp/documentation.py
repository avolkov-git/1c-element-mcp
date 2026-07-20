from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ConfigurationStore, ServerSettings, discover_corpus_path
from .corpus import CorpusError, CorpusRepository
from .jobs import DocumentationJobManager
from .normalizer import NORMALIZER_VERSION, SUPPORTED_GUIDE_SETS
from .normalizer.validation import validate_corpus_root


class DocumentationService:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.configuration = ConfigurationStore(settings.resolved_config_path)
        self.jobs = DocumentationJobManager(settings)
        self._repository: CorpusRepository | None = None
        self._repository_path: Path | None = None
        self._manifest_mtime_ns: int | None = None

    def active_path(self) -> Path | None:
        return discover_corpus_path(self.settings.corpus_path, config_store=self.configuration)

    def documentation_status(self) -> dict[str, Any]:
        path = self.active_path()
        if path is None:
            return {
                "status": "missing",
                "message": "Нормализованный корпус документации не настроен",
                "path": None,
                "normalizer_version": NORMALIZER_VERSION,
                "supported_element_versions": list(SUPPORTED_GUIDE_SETS),
                "default_corpora_path": str((self.settings.resolved_data_path / "corpora").resolve()),
            }
        report = validate_corpus_root(path, verify_content_hashes=False, write_report=False)
        report["message"] = (
            "Нормализованный корпус найден и готов к работе"
            if report["status"] == "ready"
            else "Корпус найден, но не прошёл проверку"
        )
        report["managed_by_current_normalizer"] = report.get("normalizer_version") == NORMALIZER_VERSION
        report["current_normalizer_version"] = NORMALIZER_VERSION
        return report

    def activate(self, corpus_path: str | Path) -> dict[str, Any]:
        path = Path(corpus_path).expanduser().resolve()
        report = validate_corpus_root(path, verify_content_hashes=True, write_report=False)
        if report["status"] != "ready":
            raise CorpusError("Корпус не прошёл проверку: " + "; ".join(report["errors"]))
        releases = report.get("releases") or []
        self.configuration.activate(
            path,
            metadata={
                "normalizer_version": report.get("normalizer_version"),
                "guide_set_version": report.get("guide_set_version"),
                "release": releases[0] if releases else None,
                "documents": report.get("aggregate", {}).get("documents"),
                "chunks": report.get("aggregate", {}).get("chunks"),
                "validated_at": report.get("created_at"),
            },
        )
        self._repository = None
        self._repository_path = None
        self._manifest_mtime_ns = None
        return {**report, "message": "Корпус проверен и подключён"}

    def repository(self) -> CorpusRepository:
        path = self.active_path()
        if path is None:
            raise CorpusError(
                "Нормализованный корпус не настроен. "
                "Вызовите get_documentation_status, затем discover_element_installations и start_documentation_build."
            )
        manifest = path / "manifest.json"
        try:
            mtime_ns = manifest.stat().st_mtime_ns
        except OSError as error:
            raise CorpusError(f"Не найден манифест активного корпуса: {manifest}") from error
        if self._repository is None or self._repository_path != path or self._manifest_mtime_ns != mtime_ns:
            self._repository = CorpusRepository(path)
            self._repository_path = path
            self._manifest_mtime_ns = mtime_ns
        return self._repository

    def corpus_info(self) -> dict[str, Any]:
        status = self.documentation_status()
        if status["status"] != "ready":
            return status
        return {"status": "ready", "path": status["path"], **self.repository().info()}
