from __future__ import annotations

import zipfile
from pathlib import Path

from element_mcp.installation import discover_element_installations, inspect_element_installation


def make_bundle(root: Path, version: str = "9.2.4-6") -> Path:
    bundle = root / f"1c-enterprise-element-server-with-ide-{version}"
    (bundle / "docs" / "help" / "ru").mkdir(parents=True)
    (bundle / "ide").mkdir()
    (bundle / "executor").mkdir()
    jar = bundle / "lib" / "chassis" / "modules" / f"com.e1c.g5rt.server.paasmanager-{version}.jar"
    jar.parent.mkdir(parents=True)
    with zipfile.ZipFile(jar, "w"):
        pass
    return bundle


def test_inspect_valid_bundle_detects_versions(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    installation = inspect_element_installation(bundle)
    assert installation.valid is True
    assert installation.product_version == "9.2.4-6"
    assert installation.documentation_version == "9.2"


def test_discovery_scans_only_supplied_component_root(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    found = discover_element_installations([tmp_path])
    assert [item["path"] for item in found] == [str(bundle.resolve())]
