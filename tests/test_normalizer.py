from __future__ import annotations

import json
import zipfile
from pathlib import Path

from element_mcp.normalizer import build_normalized_corpus, validate_corpus_root


def _html(title: str, body: str) -> str:
    return f'<html><body><div class="theme-doc-markdown"><h1>{title}</h1><p>{body}</p></div></body></html>'


def _create_minimal_bundle(root: Path) -> Path:
    bundle = root / "server-package-with-ide-9.2.4-6"
    docs = bundle / "docs" / "help" / "ru"
    for route, title in (
        ("topics/server-install/index.html", "Установка сервера"),
        ("stdlib/types/test/index.html", "Тестовый тип"),
        ("console/api/index.html", "Console API"),
    ):
        path = docs / route
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_html(title, "Проверочный текст нормализатора документации Element."), encoding="utf-8")

    (bundle / "ide").mkdir()
    (bundle / "executor").mkdir()
    jar = bundle / "lib" / "chassis" / "modules" / "com.e1c.g5rt.server.paasmanager-9.2.4-6.jar"
    jar.parent.mkdir(parents=True)
    prefix = "com/e1c/g5rt/server/paasmanager/Configuration/src/e1c/console/"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr(
            prefix + "Test/Test.yaml",
            "ElementKind: CommonModule\nName: Test\nId: 00000000-0000-0000-0000-000000000001\n",
        )
        archive.writestr(prefix + "Test/Test.xbsl", 'export method Ping(): String\n    return "pong";\n;')
    return bundle


def test_builds_all_corpora_with_authored_guides(tmp_path: Path) -> None:
    bundle = _create_minimal_bundle(tmp_path)
    output = tmp_path / "corpus"
    manifest = build_normalized_corpus(
        output_root=output,
        bundle_path=bundle,
        product_version="9.2.4-6",
        documentation_version="9.2",
    )
    report = validate_corpus_root(output, write_report=True)

    assert manifest["normalizer_version"] == "1.1.0"
    assert manifest["guide_set_version"] == "9.2.4-6"
    assert report["status"] == "ready"
    assert {item["corpus"] for item in report["corpora"]} == {"lang", "console", "server"}
    assert (output / "docs-console" / "guides" / "10-mcp-design-and-safe-operations.md").is_file()
    assert all(item["documents"] > 1 for item in report["corpora"])
    assert report["references"]["status"] == "ready"
    assert manifest["reference_catalog"]["datasets"] >= 19
    assert (output / "docs-console/versions/9.2.4-6/reference/api-operations.jsonl").is_file()
    assert (output / "docs-server/versions/9.2.4-6/reference/components.jsonl").is_file()

    root_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert "bundle_path" not in root_manifest["releases"][0]
    release_manifest = json.loads(
        (output / "releases" / "element-9.2.4-6" / "manifest.json").read_text(encoding="utf-8")
    )
    assert "bundle_path" not in release_manifest["release"]
