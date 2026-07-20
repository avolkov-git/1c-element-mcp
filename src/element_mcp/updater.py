from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from packaging.version import Version

from element_mcp.updates import GitRepository, UpdateError, utc_now, write_json_atomic


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply a prepared 1C Element MCP update")
    parser.add_argument("--repository-path", type=Path, required=True)
    parser.add_argument("--source-path", type=Path, default=None)
    parser.add_argument("--revision", default="master")
    parser.add_argument("--server-task-name", required=True)
    parser.add_argument("--status-path", type=Path, required=True)
    return parser


def _task(action: str, name: str, *, check: bool) -> None:
    result = subprocess.run(
        ["schtasks.exe", f"/{action}", "/TN", name],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"код {result.returncode}"
        raise UpdateError(f"Task Scheduler: {detail}")


def _install(repository_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", str(repository_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"код {result.returncode}"
        raise UpdateError(f"pip install: {detail}")


def perform_update(
    *,
    repository_path: Path,
    source_path: Path | None,
    revision: str,
    server_task_name: str,
    status_path: Path,
) -> dict[str, object]:
    repository = GitRepository(repository_path, timeout_seconds=60)
    previous_commit = repository.current_commit()
    previous_version = repository.version_at(previous_commit)
    stopped = False
    moved = False
    target_version: str | None = None

    def record(state: str, message: str, **extra: object) -> dict[str, object]:
        value: dict[str, object] = {
            "state": state,
            "message": message,
            "from_version": previous_version,
            "to_version": target_version,
            "updated_at": utc_now(),
            **extra,
        }
        write_json_atomic(status_path, value)
        return value

    try:
        record("checking", "Проверяем выбранный источник перед обновлением")
        candidate = repository.fetch_candidate(revision, source_path)
        target_version = candidate.version
        if Version(candidate.version) <= Version(previous_version):
            return record("current", "Установлена актуальная версия")

        repository.run("merge-base", "--is-ancestor", previous_commit, candidate.commit)
        record("applying", f"Устанавливаем версию {candidate.version}")

        # Give the HTTP response time to reach the browser before stopping the server task.
        time.sleep(1.5)
        _task("End", server_task_name, check=False)
        stopped = True
        time.sleep(1)

        repository.run("merge", "--ff-only", candidate.commit)
        moved = True
        _install(repository.path)

        result = record(
            "success",
            f"MCP обновлён до версии {candidate.version}",
            commit=candidate.commit,
        )
        _task("Run", server_task_name, check=True)
        return result
    except Exception as error:
        rollback_error: str | None = None
        if moved:
            try:
                repository.run("reset", "--hard", previous_commit)
                _install(repository.path)
            except Exception as rollback:
                rollback_error = str(rollback)
        result = record(
            "error",
            f"Обновление не выполнено: {error}",
            rolled_back=bool(moved and rollback_error is None),
            rollback_error=rollback_error,
        )
        if stopped:
            try:
                _task("Run", server_task_name, check=True)
            except Exception as restart_error:
                result["restart_error"] = str(restart_error)
                write_json_atomic(status_path, result)
        return result


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = perform_update(
        repository_path=args.repository_path.expanduser().resolve(),
        source_path=args.source_path.expanduser().resolve() if args.source_path else None,
        revision=args.revision,
        server_task_name=args.server_task_name,
        status_path=args.status_path.expanduser().resolve(),
    )
    return 0 if result["state"] in {"current", "success"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
