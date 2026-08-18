from __future__ import annotations

import json
import sys
from typing import Any


def read_message() -> dict[str, Any] | None:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            if length is not None:
                break
            continue
        decoded = line.decode("ascii").strip()
        if decoded.lower().startswith("content-length:"):
            length = int(decoded.split(":", 1)[1].strip())
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)
    sys.stdout.buffer.flush()


root_uri = ""
while message := read_message():
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        root_uri = message["params"]["rootUri"]
        text_document_capabilities = message["params"]["capabilities"]["textDocument"]
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "capabilities": {
                        "definitionProvider": True,
                        "hoverProvider": True,
                        "referencesProvider": True,
                        "signatureHelpProvider": {"triggerCharacters": ["(", ","]},
                        "textDocumentSync": 1,
                    },
                    "serverInfo": {
                        "name": "Fake Element LSP",
                        "version": "9.2.4-1",
                        "clientHover": "hover" in text_document_capabilities,
                        "clientSignatureHelp": "signatureHelp" in text_document_capabilities,
                    },
                },
            }
        )
    elif method == "initialized":
        send(
            {
                "jsonrpc": "2.0",
                "id": "configuration-1",
                "method": "workspace/configuration",
                "params": {"items": [{"section": "1C.element.lsp"}]},
            }
        )
        send({"jsonrpc": "2.0", "method": "builder/builderStateChanged", "params": 0})
        send(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": f"{root_uri}/Sales/Orders.xbsl",
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 7},
                                "end": {"line": 0, "character": 16},
                            },
                            "severity": 2,
                            "source": "element",
                            "message": "Fake diagnostic",
                        }
                    ],
                },
            }
        )
    elif method == "versions/elementVersion":
        send({"jsonrpc": "2.0", "id": request_id, "result": "9.2.4"})
    elif method == "textDocument/definition":
        uri = message["params"]["textDocument"]["uri"]
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "uri": uri,
                    "range": {
                        "start": {"line": 0, "character": 7},
                        "end": {"line": 0, "character": 16},
                    },
                },
            }
        )
    elif method == "textDocument/references":
        uri = message["params"]["textDocument"]["uri"]
        location = {
            "uri": uri,
            "range": {"start": {"line": 0, "character": 7}, "end": {"line": 0, "character": 16}},
        }
        send({"jsonrpc": "2.0", "id": request_id, "result": [location, location]})
    elif method == "textDocument/hover":
        position = message["params"]["position"]
        if position["character"] == 9:
            result = None
        elif position["character"] == 10:
            result = {
                "contents": [
                    {"language": "xbsl", "value": "method FindOrder(Number: String): String"},
                    "**MarkedString documentation**",
                ]
            }
        else:
            result = {
                "contents": {"kind": "markdown", "value": "`FindOrder`: String\n\nFake documentation"},
                "range": {
                    "start": {"line": 0, "character": 7},
                    "end": {"line": 0, "character": 16},
                },
            }
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif method == "textDocument/signatureHelp":
        position = message["params"]["position"]
        if position["character"] == 9:
            result = {"signatures": []}
        else:
            result = {
                "activeSignature": 1,
                "activeParameter": 1,
                "signatures": [
                    {
                        "label": "FindOrder(Number: String): String",
                        "documentation": "First overload",
                        "parameters": [{"label": "Number: String", "documentation": "Order number"}],
                    },
                    {
                        "label": "FindOrder(Number: String, Strict: Boolean): String",
                        "documentation": {"kind": "markdown", "value": "**Second overload**"},
                        "parameters": [
                            {"label": [10, 24]},
                            {"label": "Strict: Boolean", "documentation": {"kind": "plaintext", "value": "Mode"}},
                        ],
                        "activeParameter": 1,
                    },
                ],
            }
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": request_id, "result": None})
    elif method == "exit":
        break
    elif request_id is not None and method is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": None})
