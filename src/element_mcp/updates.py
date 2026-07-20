from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from packaging.version import InvalidVersion, Version

from element_mcp import __version__
from element_mcp.config import ConfigurationError, ConfigurationStore, ServerSettings, discover_update_source_path


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UpdateCandidate:
    commit: str
    version: str
    source_kind: str
    source_label: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def safe_source_label(value: str) -> str:
    if "://" not in value:
        return value
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def safe_error_detail(value: str) -> str:
    """Remove credentials from URLs that Git may echo in stderr."""
    return re.sub(r"(?P<scheme>https?://)[^\s/@:]+(?::[^\s/@]*)?@", r"\g<scheme>", value)


class GitRepository:
    def __init__(self, path: Path, *, timeout_seconds: int = 20) -> None:
        self.path = path.expanduser().resolve()
        self.timeout_seconds = timeout_seconds

    def run(self, *arguments: str, check: bool = True, timeout_seconds: int | None = None) -> str:
        environment = dict(os.environ)
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        try:
            result = subprocess.run(
                ["git", "-C", str(self.path), *arguments],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=timeout_seconds or self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError(f"Не удалось выполнить Git: {error}") from error
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"код {result.returncode}"
            raise UpdateError(safe_error_detail(detail))
        return result.stdout.strip()

    def validate(self) -> None:
        if not self.path.is_dir():
            raise UpdateError(f"Каталог Git не найден: {self.path}")
        self.run("rev-parse", "--git-dir")

    def current_commit(self) -> str:
        return self.run("rev-parse", "HEAD")

    def version_at(self, commit: str) -> str:
        payload = self.run("show", f"{commit}:pyproject.toml")
        try:
            project = tomllib.loads(payload)["project"]
            if project["name"] != "1c-element-mcp":
                raise KeyError("project.name")
            version = str(project["version"])
            Version(version)
        except (KeyError, TypeError, tomllib.TOMLDecodeError, InvalidVersion) as error:
            raise UpdateError("В выбранной ревизии не найдена корректная версия MCP") from error
        return version

    def ensure_clean(self) -> None:
        changed = self.run("status", "--porcelain", "--untracked-files=no")
        if changed:
            raise UpdateError("В управляемом каталоге MCP есть локальные изменения; обновление остановлено")

    def fetch_candidate(self, revision: str, source_path: Path | None) -> UpdateCandidate:
        self.validate()
        self.ensure_clean()
        if source_path is not None:
            source = GitRepository(source_path)
            source.validate()
            source_label = str(source.path)
            source_kind = "local"
            fetch_source = str(source.path)
        else:
            source_label = safe_source_label(self.run("remote", "get-url", "origin"))
            source_kind = "remote"
            fetch_source = "origin"

        self.run("fetch", "--quiet", "--no-tags", fetch_source, revision, timeout_seconds=30)
        commit = self.run("rev-parse", "FETCH_HEAD")
        version = self.version_at(commit)
        return UpdateCandidate(commit=commit, version=version, source_kind=source_kind, source_label=source_label)


class UpdateService:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.repository_path = settings.resolved_update_repository_path
        self.default_source_path = settings.resolved_update_source_path
        self.configuration = ConfigurationStore(settings.resolved_config_path)
        self.status_path = settings.resolved_data_path / "update-status.json"
        self.csrf_token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._check: dict[str, Any] = {
            "state": "idle",
            "message": "Проверка обновлений ещё не выполнялась",
            "checked_at": None,
            "available_version": None,
            "candidate_commit": None,
        }

    def current_source_path(self) -> Path | None:
        try:
            return discover_update_source_path(self.default_source_path, config_store=self.configuration)
        except ConfigurationError as error:
            raise UpdateError(str(error)) from error

    def configure_source(self, value: str | None) -> dict[str, Any]:
        if value is not None:
            value = value.strip()
            if not value:
                value = None
        if value is not None and len(value) > 4096:
            raise UpdateError("Путь к локальному Git-каталогу слишком длинный")

        with self._lock:
            source_path: Path | None = None
            if value is not None:
                candidate = Path(value).expanduser()
                if not candidate.is_absolute():
                    raise UpdateError("Укажите полный путь к локальному Git-каталогу")
                source = GitRepository(candidate)
                source.validate()
                commit = source.run("rev-parse", "--verify", f"{self.settings.update_revision}^{{commit}}")
                source.version_at(commit)
                source_path = source.path

            try:
                self.configuration.configure_update_source(source_path)
            except (ConfigurationError, OSError) as error:
                raise UpdateError(f"Не удалось сохранить источник обновлений: {error}") from error
            self._check = {
                "state": "idle",
                "message": "Источник обновлений сохранён",
                "checked_at": None,
                "available_version": None,
                "candidate_commit": None,
            }
        return self.status()

    def source(self) -> dict[str, Any]:
        try:
            source_path = self.current_source_path()
        except UpdateError as error:
            return {"kind": "invalid", "label": str(error), "revision": self.settings.update_revision}
        if source_path is not None:
            return {"kind": "local", "label": str(source_path), "revision": self.settings.update_revision}
        if self.repository_path is None:
            return {"kind": "none", "label": "Источник обновлений не настроен", "revision": None}
        try:
            label = safe_source_label(GitRepository(self.repository_path).run("remote", "get-url", "origin"))
        except UpdateError:
            label = "Git remote origin"
        return {"kind": "remote", "label": label, "revision": self.settings.update_revision}

    def status(self) -> dict[str, Any]:
        persisted = read_json(self.status_path)
        update_state = persisted or {"state": "idle", "message": None, "updated_at": None}
        return {
            "server": {"state": "running", "version": __version__},
            "updates": {
                **self._check,
                "source": self.source(),
                "can_apply": bool(
                    self._check.get("state") == "available" and self.settings.update_task_name and os.name == "nt"
                ),
                "apply": update_state,
            },
        }

    def check(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return self.status()
        try:
            self._check = {
                **self._check,
                "state": "checking",
                "message": "Проверяем источник обновлений",
            }
            if self.repository_path is None:
                raise UpdateError("Каталог установленного MCP не настроен для обновлений")
            candidate = GitRepository(self.repository_path).fetch_candidate(
                self.settings.update_revision,
                self.current_source_path(),
            )
            current = Version(__version__)
            available = Version(candidate.version)
            if available > current:
                state = "available"
                message = f"Доступно обновление {candidate.version}"
            else:
                state = "current"
                message = "Установлена актуальная версия"
            self._check = {
                "state": state,
                "message": message,
                "checked_at": utc_now(),
                "available_version": candidate.version,
                "candidate_commit": candidate.commit,
            }
        except (UpdateError, InvalidVersion) as error:
            self._check = {
                "state": "unavailable",
                "message": "Проверка обновлений недоступна. MCP продолжает работать.",
                "detail": str(error),
                "checked_at": utc_now(),
                "available_version": None,
                "candidate_commit": None,
            }
        finally:
            self._lock.release()
        return self.status()

    def apply(self) -> dict[str, Any]:
        status = self.check()
        updates = status["updates"]
        if updates["state"] != "available":
            raise UpdateError(updates["message"])
        if os.name != "nt" or not self.settings.update_task_name:
            raise UpdateError("Автоматическое применение обновлений настроено только установщиком Windows Server")

        queued = {
            "state": "queued",
            "message": f"Обновление до {updates['available_version']} поставлено в очередь",
            "from_version": __version__,
            "to_version": updates["available_version"],
            "updated_at": utc_now(),
        }
        write_json_atomic(self.status_path, queued)
        try:
            result = subprocess.run(
                ["schtasks.exe", "/Run", "/TN", self.settings.update_task_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError(f"Не удалось запустить задание обновления: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"код {result.returncode}"
            raise UpdateError(f"Не удалось запустить задание обновления: {detail}")
        return {**self.status(), "accepted": True}
