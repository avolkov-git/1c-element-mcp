from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

from element_mcp.config import ServerSettings
from element_mcp.documentation import DocumentationService
from element_mcp.language_server import (
    LanguageServerService,
    _JsonRpcProcess,
    inspect_language_server_runtime,
)
from element_mcp.project import ProjectService
from element_mcp.semantic import SemanticService


def make_bundle(root: Path) -> Path:
    (root / "docs" / "help" / "ru").mkdir(parents=True)
    modules = root / "lib" / "chassis" / "modules"
    modules.mkdir(parents=True)
    (modules / "com.e1c.g5rt.server.paasmanager-9.2.4-6.jar").touch()
    repo = root / "ide" / "theia" / "plugins" / "@1c-appengine-plugin" / "bin" / "appengine-lsp" / "repo"
    repo.mkdir(parents=True)
    (repo / "com.e1c.g5rt.lsp.server.appengine-9.2.4-1.jar").touch()
    (repo.parent.parent / "dbeng").mkdir()
    (root / "executor").mkdir()
    return root


def make_java(root: Path) -> Path:
    java = root / "java"
    java.write_text("#!/bin/sh\necho 'openjdk version \"17.0.1\"' >&2\n", encoding="utf-8")
    java.chmod(0o755)
    return java


def service(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
    *,
    bundle_path: Path | None = None,
) -> LanguageServerService:
    settings = ServerSettings(
        project_path=element_project_path,
        element_bundle_path=bundle_path,
        corpus_path=corpus_path,
        config_path=tmp_path / "config.json",
        data_path=tmp_path / "data",
    )
    project = ProjectService(settings)
    semantic = SemanticService(project, DocumentationService(settings))
    return LanguageServerService(settings, project, semantic)


def test_runtime_validation_matches_bundle_lsp_and_java(tmp_path: Path) -> None:
    runtime = inspect_language_server_runtime(
        make_bundle(tmp_path / "bundle"),
        make_java(tmp_path),
        source="test",
    )

    assert runtime.product_version == "9.2.4-6"
    assert runtime.lsp_version == "9.2.4-1"
    assert runtime.java_version == 17


def test_json_rpc_process_initializes_and_handles_server_requests(tmp_path: Path) -> None:
    notifications: list[tuple[str, object]] = []
    fake_server = Path(__file__).with_name("fake_lsp_server.py")
    client = _JsonRpcProcess(
        [sys.executable, str(fake_server)],
        root=tmp_path,
        notification_handler=lambda method, params: notifications.append((method, params)),
    )

    client.start(timeout=5)
    definition = client.request(
        "textDocument/definition",
        {"textDocument": {"uri": (tmp_path / "Source.xbsl").as_uri()}, "position": {"line": 0, "character": 8}},
        timeout=5,
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not notifications:
        time.sleep(0.01)

    assert client.element_version == "9.2.4"
    assert client.server_info["name"] == "Fake Element LSP"
    assert definition["range"]["start"] == {"line": 0, "character": 7}
    assert {method for method, _ in notifications} >= {
        "builder/builderStateChanged",
        "textDocument/publishDiagnostics",
    }
    client.stop()


def test_definition_falls_back_honestly_when_lsp_is_missing(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    language_server = service(
        tmp_path,
        element_project_path,
        corpus_path,
        bundle_path=tmp_path / "missing-bundle",
    )

    result = language_server.definition("Sales/Orders.xbsl", 1, 10)

    assert result["status"] == "ready"
    assert result["analysis_mode"] == "syntax-aware lexical fallback"
    assert result["semantic_guarantee"] is False
    assert result["query"]["symbol"] == "FindOrder"
    assert result["count"] == 1
    assert "полным серверным бандлом" in result["fallback_reason"]


def test_configure_persists_validated_runtime(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    language_server = service(tmp_path, element_project_path, corpus_path)
    bundle = make_bundle(tmp_path / "bundle")
    java = make_java(tmp_path)

    result = language_server.configure(bundle, java_path=java)

    assert result["status"] == "ready"
    assert result["product_version"] == "9.2.4-6"
    assert language_server.configuration.language_server_configuration() == {
        "bundle_path": bundle.resolve(),
        "java_path": java.resolve(),
    }


@pytest.mark.skipif(shutil.which("java") is None, reason="Java is needed for the real bundle smoke test")
def test_real_bundle_runtime_is_recognized_when_available() -> None:
    bundle = Path("/Users/aleksandrvolkov/Downloads/server-package-with-ide-9.2.4-6")
    if not bundle.is_dir():
        pytest.skip("Local Element bundle is not available")

    runtime = inspect_language_server_runtime(bundle, None, source="local-smoke-test")

    assert runtime.product_version == "9.2.4-6"
    assert runtime.lsp_version == "9.2.4-1"
