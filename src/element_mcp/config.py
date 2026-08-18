from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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

    def active_project_path(self) -> Path | None:
        configured = self.read().get("active_project_path")
        return Path(configured).expanduser().resolve() if configured else None

    def language_server_configuration(self) -> dict[str, Path | None]:
        configured = self.read().get("language_server")
        if configured is None:
            return {"bundle_path": None, "java_path": None}
        if not isinstance(configured, dict):
            raise ConfigurationError(f"Некорректная конфигурация Language Server в {self.path}")
        result: dict[str, Path | None] = {}
        for key in ("bundle_path", "java_path"):
            value = configured.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ConfigurationError(f"Некорректное поле language_server.{key} в {self.path}")
            result[key] = Path(value).expanduser().resolve() if value else None
        return result

    def update_source(self) -> UpdateSourceConfiguration | None:
        configured = self.read().get("update_source")
        if configured is None:
            return None
        if not isinstance(configured, dict) or configured.get("kind") not in {"local", "remote"}:
            raise ConfigurationError(f"Некорректный источник обновлений в {self.path}")
        kind = configured["kind"]
        path = configured.get("path")
        if kind == "local":
            if not isinstance(path, str) or not path.strip():
                raise ConfigurationError(f"Для локального источника не указан путь в {self.path}")
            return UpdateSourceConfiguration(kind="local", path=Path(path).expanduser().resolve())
        return UpdateSourceConfiguration(kind="remote", path=None)

    def configure_update_source(self, path: str | Path | None) -> None:
        value = self.read()
        value["schema_version"] = CONFIG_SCHEMA_VERSION
        value["update_source"] = (
            {"kind": "local", "path": str(Path(path).expanduser().resolve())}
            if path is not None
            else {"kind": "remote"}
        )
        self._write(value)

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
        self._write(value)

    def connect_project(self, project_path: str | Path, *, metadata: dict[str, Any] | None = None) -> None:
        value = self.read()
        value.update(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "active_project_path": str(Path(project_path).expanduser().resolve()),
            }
        )
        if metadata:
            value["active_project"] = metadata
        self._write(value)

    def configure_language_server(self, bundle_path: str | Path, *, java_path: str | Path | None = None) -> None:
        value = self.read()
        value["schema_version"] = CONFIG_SCHEMA_VERSION
        value["language_server"] = {
            "bundle_path": str(Path(bundle_path).expanduser().resolve()),
            "java_path": str(Path(java_path).expanduser().resolve()) if java_path else None,
        }
        self._write(value)

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


@dataclass(frozen=True, slots=True)
class UpdateSourceConfiguration:
    kind: Literal["local", "remote"]
    path: Path | None


def discover_update_source_path(
    explicit: str | Path | None = None,
    *,
    config_store: ConfigurationStore | None = None,
) -> Path | None:
    stored = (config_store or ConfigurationStore()).update_source()
    if stored is not None:
        return stored.path
    return Path(explicit).expanduser().resolve() if explicit else None


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


def discover_project_path(
    explicit: str | Path | None = None,
    *,
    config_store: ConfigurationStore | None = None,
) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()

    configured = os.environ.get("ELEMENT_PROJECT_PATH")
    if configured:
        return Path(configured).expanduser().resolve()

    return (config_store or ConfigurationStore()).active_project_path()


@dataclass(frozen=True, slots=True)
class ServerSettings:
    corpus_path: Path | None = None
    project_path: Path | None = None
    element_bundle_path: Path | None = None
    java_path: Path | None = None
    console_config_path: Path | None = None
    runtime_config_path: Path | None = None
    ide_settings_path: Path | None = None
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
    def resolved_project_path(self) -> Path | None:
        return self.project_path.expanduser().resolve() if self.project_path else None

    @property
    def resolved_element_bundle_path(self) -> Path | None:
        return self.element_bundle_path.expanduser().resolve() if self.element_bundle_path else None

    @property
    def resolved_java_path(self) -> Path | None:
        return self.java_path.expanduser().resolve() if self.java_path else None

    @property
    def resolved_console_config_path(self) -> Path:
        if self.console_config_path:
            return self.console_config_path.expanduser().resolve()
        return self.resolved_config_path.parent / "console.json"

    @property
    def resolved_runtime_config_path(self) -> Path:
        if self.runtime_config_path:
            return self.runtime_config_path.expanduser().resolve()
        return self.resolved_config_path.parent / "runtime.json"

    @property
    def resolved_ide_settings_path(self) -> Path | None:
        return self.ide_settings_path.expanduser().resolve() if self.ide_settings_path else None

    @property
    def resolved_update_repository_path(self) -> Path | None:
        return self.update_repository_path.expanduser().resolve() if self.update_repository_path else None

    @property
    def resolved_update_source_path(self) -> Path | None:
        return self.update_source_path.expanduser().resolve() if self.update_source_path else None
