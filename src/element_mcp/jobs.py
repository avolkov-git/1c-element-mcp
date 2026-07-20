from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ConfigurationStore, ServerSettings
from .installation import require_element_installation
from .normalizer import SUPPORTED_GUIDE_SETS

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class DocumentationJobError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def write_job_status(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def update_job_status(path: Path, **changes: Any) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    value.update(changes)
    value["updated_at"] = utc_now()
    write_job_status(path, value)
    return value


class DocumentationJobManager:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.jobs_path = settings.resolved_data_path / "jobs"
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def default_output_path(self, product_version: str) -> Path:
        return self.settings.resolved_data_path / "corpora" / f"element-{product_version}"

    def _status_path(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id):
            raise DocumentationJobError("Некорректный идентификатор задания")
        return self.jobs_path / f"{job_id}.json"

    def start(self, *, bundle_path: str | Path, output_path: str | Path | None = None) -> dict[str, Any]:
        installation = require_element_installation(bundle_path)
        product_version = str(installation.product_version)
        if product_version not in SUPPORTED_GUIDE_SETS:
            raise DocumentationJobError(
                f"Нормализатор пока не содержит проверенный комплект guides для Element {product_version}. "
                f"Поддерживаемые версии: {', '.join(SUPPORTED_GUIDE_SETS)}"
            )
        target = (
            Path(output_path).expanduser().resolve()
            if output_path
            else self.default_output_path(product_version).resolve()
        )
        bundle = installation.path
        if target == bundle or target.is_relative_to(bundle):
            raise DocumentationJobError("Корпус нельзя создавать внутри исходного серверного бандла")
        if target.exists():
            raise DocumentationJobError(
                f"Каталог результата уже существует: {target}. "
                "Подключите его через activate_documentation или выберите другой путь."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        status_path = self._status_path(job_id)
        log_path = self.jobs_path / f"{job_id}.log"
        status = {
            "job_id": job_id,
            "state": "queued",
            "phase": "prepare",
            "percent": 0,
            "message": "Подготовка нормализации",
            "bundle_path": str(bundle),
            "output_path": str(target),
            "product_version": product_version,
            "documentation_version": installation.documentation_version,
            "normalizer_guide_set": product_version,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        write_job_status(status_path, status)
        command = [
            sys.executable,
            "-m",
            "element_mcp.normalizer",
            "--bundle-path",
            str(bundle),
            "--output-path",
            str(target),
            "--config-path",
            str(self.settings.resolved_config_path),
            "--job-status-path",
            str(status_path),
            "--job-id",
            job_id,
        ]
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with log_path.open("ab") as log_stream:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=creation_flags,
            )
        self._processes[job_id] = process
        return update_job_status(status_path, state="running", pid=process.pid, log_path=str(log_path))

    def status(self, job_id: str) -> dict[str, Any]:
        path = self._status_path(job_id)
        if not path.is_file():
            raise DocumentationJobError(f"Задание не найдено: {job_id}")
        value = json.loads(path.read_text(encoding="utf-8"))
        process = self._processes.get(job_id)
        if process and process.poll() is not None and value.get("state") == "running":
            value = update_job_status(
                path,
                state="failed",
                phase="process",
                message=f"Процесс завершился с кодом {process.returncode} без итогового статуса",
            )
        return value

    def cancel(self, job_id: str) -> dict[str, Any]:
        status = self.status(job_id)
        if status.get("state") in {"completed", "failed", "cancelled"}:
            return status
        process = self._processes.get(job_id)
        if process is None or process.poll() is not None:
            raise DocumentationJobError(
                "Это задание запущено другим процессом MCP и не может быть безопасно остановлено из текущего процесса"
            )
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        output_path = Path(status["output_path"])
        staging = output_path.parent / f".{output_path.name}.building-{job_id}"
        if staging.is_dir() and staging.parent == output_path.parent:
            shutil.rmtree(staging)
        return update_job_status(
            self._status_path(job_id),
            state="cancelled",
            phase="cancelled",
            message="Нормализация остановлена пользователем",
        )


def activate_completed_corpus(config_path: str | Path, corpus_path: Path, report: dict[str, Any]) -> None:
    releases = report.get("releases") or []
    ConfigurationStore(config_path).activate(
        corpus_path,
        metadata={
            "status": report.get("status"),
            "normalizer_version": report.get("normalizer_version"),
            "guide_set_version": report.get("guide_set_version"),
            "release": releases[0] if releases else None,
            "documents": report.get("aggregate", {}).get("documents"),
            "chunks": report.get("aggregate", {}).get("chunks"),
            "validated_at": report.get("created_at"),
        },
    )
