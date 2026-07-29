from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
from typing import Any, Dict

from dcc_mcp_marmoset.server import MarmosetMcpServer


class MockToolbagBridge:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls = []
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                connection, _address = self.listener.accept()
            except (OSError, socket.timeout):
                continue
            threading.Thread(target=self._respond, args=(connection,), daemon=True).start()

    def _respond(self, connection: socket.socket) -> None:
        with connection:
            request = json.loads(connection.makefile("rb").readline().decode("utf-8"))
            assert request["token"] == self.token
            method = request["method"]
            self.calls.append(method)
            if method == "bridge.health":
                result = {"status": "ok"}
            elif method == "diagnostics.ping":
                result = {
                    "status": "ok",
                    "toolbag_version": "5022",
                    "graphics_adapter": "Mock GPU",
                    "scene_path": "",
                }
            else:
                raise AssertionError(method)
            connection.sendall(json.dumps({"result": result}).encode("utf-8") + b"\n")


class McpClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.session_id = ""
        initialized, headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "marmoset-smoke", "version": "1.0"},
                },
            }
        )
        assert "result" in initialized
        self.session_id = headers.get("Mcp-Session-Id", "")

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        response, _headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return response

    def _post(self, body: Dict[str, Any]):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read()), dict(response.getheaders())


def _text(response: Dict[str, Any]) -> str:
    return "".join(
        item.get("text", "") for item in response["result"]["content"] if item.get("type") == "text"
    )


def test_direct_mcp_search_load_and_typed_call(monkeypatch, tmp_path):
    token = "smoke-token"
    bridge = MockToolbagBridge(token)
    bridge.start()
    monkeypatch.setenv("DCC_MCP_MARMOSET_BRIDGE_PORT", str(bridge.port))
    monkeypatch.setenv("DCC_MCP_MARMOSET_BRIDGE_TOKEN", token)
    monkeypatch.setenv("DCC_MCP_GATEWAY_PORT", "0")
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_DISABLE_DEFAULT_SKILL_PATHS", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_JOB_PERSISTENCE", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_TELEMETRY", "1")

    server = MarmosetMcpServer(host_pid=os.getpid())
    server.register_builtin_actions()
    handle = server.start()
    try:
        deadline = time.time() + 3
        while "bridge.health" not in bridge.calls and time.time() < deadline:
            time.sleep(0.05)
        client = McpClient(handle.mcp_url())
        search = client.call("search_skills", {"query": "Toolbag scene"})
        assert "marmoset-scene" in _text(search)

        loaded = client.call("load_skill", {"skill_name": "marmoset-scene"})
        assert "marmoset_scene__ping" in _text(loaded)

        ping = client.call("marmoset_scene__ping", {})
        job_id = ping["result"]["structuredContent"]["job_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            status = client.call(
                "jobs_get_status",
                {"job_id": job_id, "include_result": True},
            )["result"]["structuredContent"]
            if status["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.05)
        assert status["status"] == "completed"
        assert "Marmoset Toolbag bridge is ready" in json.dumps(status["result"])
        assert "diagnostics.ping" in bridge.calls
    finally:
        server.stop()
        bridge.stop()
