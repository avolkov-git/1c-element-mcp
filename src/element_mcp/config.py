from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_config_path, user_data_path

APP_NAME = "1c-element-mcp"
CONFIG_SCHEMA_VERSION = 1


class ConfigurationError(ValueError):
    pass


def default_config_path() -> Path:
    return user_config_path(APP_NAME, appauthor=False) / "config.json"


def default_data_path() -> Path:
    return user_data_path(APP_NAME, appauthor=False)


class ConfigurationStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_config_path()

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": CONFIG_SCHEMA_VERSION}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"Не удалось прочитать конфигурацию MCP {self.path}: {error}") from error
        if not isinstance(value, dict):
            raise ConfigurationError(f"Некорректная конфигурация MCP: {self.path}")
        return value

    def active_corpus_path(self) -> Path | None:
        configured = self.read().get("active_corpus_path")
        return Path(configured).expanduser().resolve() if configured else None

    def activate(self, corpus_path: str | Path, *, metadata: dict[str, Any] | None = None) -> None:
        value = self.read()
        value.update(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "active_corpus_path": str(Path(corpus_path).expanduser().resolve()),
            }
        )
        if metadata:
            value["active_corpus"] = metadata
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def discover_corpus_path(
    explicit: str | Path | None = None,
    *,
    config_store: ConfigurationStore | None = None,
) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()

    configured = os.environ.get("ELEMENT_DOCS_PATH")
    if configured:
        return Path(configured).expanduser().resolve()

    stored = (config_store or ConfigurationStore()).active_corpus_path()
    if stored:
        return stored

    return None


@dataclass(frozen=True, slots=True)
class ServerSettings:
    corpus_path: Path | None = None
    config_path: Path | None = None
    data_path: Path | None = None
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 9900
    update_repository_path: Path | None = None
    update_source_path: Path | None = None
    update_revision: str = "master"
    update_task_name: str | None = None

    @property
    def resolved_config_path(self) -> Path:
        return self.config_path.expanduser().resolve() if self.config_path else default_config_path()

    @property
    def resolved_data_path(self) -> Path:
        return self.data_path.expanduser().resolve() if self.data_path else default_data_path()

    @property
    def resolved_update_repository_path(self) -> Path | None:
        return self.update_repository_path.expanduser().resolve() if self.update_repository_path else None

    @property
    def resolved_update_source_path(self) -> Path | None:
        return self.update_source_path.expanduser().resolve() if self.update_source_path else None
