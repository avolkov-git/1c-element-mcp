from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


def discover_corpus_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()

    configured = os.environ.get("ELEMENT_DOCS_PATH")
    if configured:
        return Path(configured).expanduser().resolve()

    repository = Path(__file__).resolve().parents[2]
    candidates = (
        repository.parent / "codex-docs",
        Path.cwd() / "codex-docs",
        Path.cwd().parent / "codex-docs",
    )
    for candidate in candidates:
        if (candidate / "manifest.json").is_file():
            return candidate.resolve()

    raise ConfigurationError("Не найден корпус codex-docs. Передайте --corpus-path или задайте ELEMENT_DOCS_PATH.")


@dataclass(frozen=True, slots=True)
class ServerSettings:
    corpus_path: Path
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
