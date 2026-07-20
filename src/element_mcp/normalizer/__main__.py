from __future__ import annotations

import argparse
import shutil
import traceback
from pathlib import Path

from element_mcp.installation import require_element_installation
from element_mcp.jobs import activate_completed_corpus, update_job_status

from .builder import build_normalized_corpus
from .golden import golden_mismatches
from .validation import validate_corpus_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a local normalized 1C:Element corpus")
    parser.add_argument("--bundle-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--config-path", type=Path, required=True)
    parser.add_argument("--job-status-path", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    target = args.output_path.expanduser().resolve()
    staging = target.parent / f".{target.name}.building-{args.job_id}"
    status_path = args.job_status_path.expanduser().resolve()

    def progress(phase: str, percent: int, message: str) -> None:
        update_job_status(
            status_path,
            state="running",
            phase=phase,
            percent=percent,
            message=message,
        )

    try:
        installation = require_element_installation(args.bundle_path)
        if target.exists():
            raise RuntimeError(f"Каталог результата уже существует: {target}")
        if staging.exists():
            shutil.rmtree(staging)
        progress("prepare", 5, "Подготовка временного каталога")
        build_normalized_corpus(
            output_root=staging,
            bundle_path=installation.path,
            product_version=str(installation.product_version),
            documentation_version=str(installation.documentation_version),
            guide_version=str(installation.product_version),
            progress=progress,
        )
        report = validate_corpus_root(staging, verify_content_hashes=True, write_report=False)
        if report["status"] != "ready":
            raise RuntimeError("Корпус не прошёл проверку: " + "; ".join(report["errors"]))
        mismatches = golden_mismatches(report, str(installation.product_version))
        if mismatches:
            raise RuntimeError("Результат не совпал с эталонным корпусом для этой версии: " + "; ".join(mismatches))
        staging.replace(target)
        report = validate_corpus_root(target, verify_content_hashes=False, write_report=True)
        activate_completed_corpus(args.config_path, target, report)
        update_job_status(
            status_path,
            state="completed",
            phase="complete",
            percent=100,
            message="Корпус создан, проверен и подключён",
            validation=report,
            output_path=str(target),
        )
        return 0
    except BaseException as error:
        update_job_status(
            status_path,
            state="failed",
            phase="failed",
            message=str(error),
            error_type=type(error).__name__,
            traceback=traceback.format_exc(),
        )
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
