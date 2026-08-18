from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

import element_mcp.language_server as language_server_module
from element_mcp.config import ServerSettings
from element_mcp.documentation import DocumentationService
from element_mcp.language_server import (
    MAX_HOVER_BLOCKS,
    MAX_LSP_CONTENT_CHARS,
    LanguageServerService,
    LanguageServerTimeout,
    LanguageServerUnavailable,
    _JsonRpcProcess,
    _normalize_lsp_content,
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


def attach_fake_client(language_server: LanguageServerService, root: Path) -> _JsonRpcProcess:
    fake_server = Path(__file__).with_name("fake_lsp_server.py")
    client = _JsonRpcProcess(
        [sys.executable, str(fake_server)],
        root=root,
        notification_handler=language_server._notification,
    )
    client.start(timeout=5)
    language_server._client = client
    language_server._client_root = root
    language_server._ensure_client = lambda: client  # type: ignore[method-assign]
    return client


def test_runtime_validation_matches_bundle_lsp_and_java(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(language_server_module, "_java_version", lambda _path: 17)
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
    assert client.server_info["clientHover"] is True
    assert client.server_info["clientSignatureHelp"] is True
    assert client.server_capabilities["hoverProvider"] is True
    assert client.server_capabilities["signatureHelpProvider"]["triggerCharacters"] == ["(", ","]
    assert definition["range"]["start"] == {"line": 0, "character": 7}
    assert {method for method, _ in notifications} >= {
        "builder/builderStateChanged",
        "textDocument/publishDiagnostics",
    }
    client.stop()


def test_hover_normalizes_markup_marked_strings_empty_and_range(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    language_server = service(tmp_path, element_project_path, corpus_path)
    attach_fake_client(language_server, element_project_path)
    try:
        markdown = language_server.hover("Sales/Orders.xbsl", 1, 9)
        opened_document = (element_project_path / "Sales" / "Orders.xbsl").as_uri()
        assert language_server._documents[opened_document][1] == 1
        empty = language_server.hover("Sales/Orders.xbsl", 1, 10)
        marked_strings = language_server.hover("Sales/Orders.xbsl", 1, 11)
    finally:
        language_server.close()

    assert markdown["lsp_status"] == "ready"
    assert markdown["semantic_guarantee"] is True
    assert markdown["contents"] == [{"kind": "markdown", "value": "`FindOrder`: String\n\nFake documentation"}]
    assert markdown["range"] == {"line": 1, "column": 8, "end_line": 1, "end_column": 17}
    assert empty["lsp_status"] == "empty"
    assert empty["found"] is False
    assert empty["contents"] == []
    assert marked_strings["contents"] == [
        {"kind": "code", "language": "xbsl", "value": "method FindOrder(Number: String): String"},
        {"kind": "markdown", "value": "**MarkedString documentation**"},
    ]


def test_signature_help_normalizes_overloads_parameters_and_documentation(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    language_server = service(tmp_path, element_project_path, corpus_path)
    attach_fake_client(language_server, element_project_path)
    try:
        result = language_server.signature_help("Sales/Orders.xbsl", 1, 12)
        empty = language_server.signature_help("Sales/Orders.xbsl", 1, 10)
    finally:
        language_server.close()

    assert result["lsp_status"] == "ready"
    assert result["active_signature"] == 1
    assert result["active_parameter"] == 1
    assert result["count"] == 2
    assert result["signatures"][0]["documentation"] == [{"kind": "plaintext", "value": "First overload"}]
    assert result["signatures"][1] == {
        "label": "FindOrder(Number: String, Strict: Boolean): String",
        "documentation": [{"kind": "markdown", "value": "**Second overload**"}],
        "parameters": [
            {"label_offsets": [10, 24], "documentation": []},
            {"label": "Strict: Boolean", "documentation": [{"kind": "plaintext", "value": "Mode"}]},
        ],
        "active_parameter": 1,
    }
    assert empty["lsp_status"] == "empty"
    assert empty["signatures"] == []


def test_hover_and_signature_help_distinguish_unsupported_timeout_and_stopped(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    language_server = service(tmp_path, element_project_path, corpus_path)
    client = attach_fake_client(language_server, element_project_path)
    original_request = client.request
    try:
        client.server_capabilities["hoverProvider"] = False
        unsupported = language_server.hover("Sales/Orders.xbsl", 1, 10)

        client.server_capabilities["signatureHelpProvider"] = True

        def timeout(method: str, params=None, *, timeout: float = 20.0):
            if method == "textDocument/signatureHelp":
                raise LanguageServerTimeout("test timeout")
            return original_request(method, params, timeout=timeout)

        client.request = timeout  # type: ignore[method-assign]
        timed_out = language_server.signature_help("Sales/Orders.xbsl", 1, 12)

        def stopped(method: str, params=None, *, timeout: float = 20.0):
            if method == "textDocument/hover":
                raise LanguageServerUnavailable("test stopped process")
            return original_request(method, params, timeout=timeout)

        client.server_capabilities["hoverProvider"] = True
        client.request = stopped  # type: ignore[method-assign]
        stopped_result = language_server.hover("Sales/Orders.xbsl", 1, 10)
    finally:
        client.request = original_request  # type: ignore[method-assign]
        language_server.close()

    assert unsupported["lsp_status"] == "unsupported"
    assert unsupported["analysis_mode"] == "syntax-aware lexical fallback"
    assert timed_out["lsp_status"] == "timeout"
    assert timed_out["semantic_guarantee"] is False
    assert stopped_result["lsp_status"] == "stopped"
    assert stopped_result["semantic_guarantee"] is False


def test_lsp_position_is_one_based_and_uses_utf16(tmp_path: Path) -> None:
    source = tmp_path / "Unicode.xbsl"
    source.write_text("🙂Method()\n", encoding="utf-8")

    assert LanguageServerService._lsp_position(source, 1, 1) == {"line": 0, "character": 0}
    assert LanguageServerService._lsp_position(source, 1, 2) == {"line": 0, "character": 2}
    assert LanguageServerService._lsp_position(source, 1, 9) == {"line": 0, "character": 9}


def test_lsp_content_is_bounded() -> None:
    blocks, truncated = _normalize_lsp_content(
        ["x" * MAX_LSP_CONTENT_CHARS, *(["overflow"] * MAX_HOVER_BLOCKS)],
        string_kind="markdown",
    )

    assert truncated is True
    assert len(blocks) <= MAX_HOVER_BLOCKS
    assert sum(len(block["value"]) for block in blocks) <= MAX_LSP_CONTENT_CHARS


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(language_server_module, "_java_version", lambda _path: 17)
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
