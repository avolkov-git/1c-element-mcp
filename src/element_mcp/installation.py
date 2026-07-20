from __future__ import annotations

import os
import platform
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

COMPONENT_PREFIXES = (
    "1c-enterprise-element-server-with-ide-",
    "server-package-with-ide-",
)
PAAS_JAR_PATTERN = "com.e1c.g5rt.server.paasmanager-*.jar"


class InstallationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ElementInstallation:
    path: Path
    product_version: str | None
    documentation_version: str | None
    valid: bool
    missing: tuple[str, ...]
    source: str

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value["path"] = str(self.path)
        value["missing"] = list(self.missing)
        return value


def _version_key(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _version_from_jar(path: Path) -> str | None:
    match = re.match(r"com\.e1c\.g5rt\.server\.paasmanager-(.+)\.jar$", path.name)
    return match.group(1) if match else None


def inspect_element_installation(path: str | Path, *, source: str = "explicit") -> ElementInstallation:
    root = Path(path).expanduser().resolve()
    docs = root / "docs" / "help" / "ru"
    modules = root / "lib" / "chassis" / "modules"
    paas_jars = sorted(modules.glob(PAAS_JAR_PATTERN)) if modules.is_dir() else []
    version = _version_from_jar(paas_jars[-1]) if paas_jars else None
    missing: list[str] = []
    if not root.is_dir():
        missing.append("bundle-directory")
    if not docs.is_dir():
        missing.append("docs/help/ru")
    if not paas_jars:
        missing.append(f"lib/chassis/modules/{PAAS_JAR_PATTERN}")
    if not (root / "ide").is_dir():
        missing.append("ide")
    if not (root / "executor").is_dir():
        missing.append("executor")
    documentation_version = ".".join(version.split(".")[:2]) if version and "." in version else version
    return ElementInstallation(
        path=root,
        product_version=version,
        documentation_version=documentation_version,
        valid=not missing,
        missing=tuple(missing),
        source=source,
    )


def standard_component_roots(system: str | None = None) -> tuple[Path, ...]:
    operating_system = (system or platform.system()).lower()
    roots: list[Path] = []
    if operating_system == "windows":
        for variable in ("ProgramFiles", "ProgramW6432"):
            value = os.environ.get(variable)
            if value:
                roots.append(Path(value) / "1C" / "1CE" / "components")
        roots.append(Path(r"C:\Program Files\1C\1CE\components"))
    elif operating_system == "linux":
        roots.append(Path("/opt/1C/1CE/components"))
    return tuple(dict.fromkeys(roots))


def _candidate_directories(root: Path) -> Iterable[Path]:
    if root.is_dir() and any((root / marker).exists() for marker in ("docs", "lib", "executor")):
        yield root
    if not root.is_dir():
        return
    for prefix in COMPONENT_PREFIXES:
        yield from sorted(path for path in root.glob(prefix + "*") if path.is_dir())


def discover_element_installations(extra_roots: Iterable[str | Path] = ()) -> list[dict[str, Any]]:
    candidates: dict[Path, ElementInstallation] = {}
    roots = [*standard_component_roots(), *(Path(path).expanduser() for path in extra_roots)]
    for root in roots:
        for candidate_path in _candidate_directories(root):
            candidate = inspect_element_installation(candidate_path, source="standard-path")
            if candidate.valid:
                candidates[candidate.path] = candidate
    ordered = sorted(
        candidates.values(),
        key=lambda item: (_version_key(item.product_version), str(item.path)),
        reverse=True,
    )
    return [candidate.public() for candidate in ordered]


def require_element_installation(path: str | Path) -> ElementInstallation:
    candidate = inspect_element_installation(path)
    if not candidate.valid:
        raise InstallationError(
            f"Каталог не является полным серверным бандлом Element: {candidate.path}. "
            f"Отсутствует: {', '.join(candidate.missing)}"
        )
    if not candidate.product_version or not candidate.documentation_version:
        raise InstallationError(f"Не удалось определить версию Element: {candidate.path}")
    return candidate
