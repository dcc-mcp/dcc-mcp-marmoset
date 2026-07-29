from __future__ import annotations

import json
import socket
import threading

from dcc_mcp_marmoset import bridge


def test_call_host_uses_bounded_authenticated_loopback_protocol(monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    observed = {}

    def serve():
        connection, _address = listener.accept()
        with connection:
            request = json.loads(connection.makefile("rb").readline().decode("utf-8"))
            observed.update(request)
            connection.sendall(b'{"result":{"status":"ok"}}\n')
        listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    monkeypatch.setenv("DCC_MCP_MARMOSET_BRIDGE_PORT", str(port))
    monkeypatch.setenv("DCC_MCP_MARMOSET_BRIDGE_TOKEN", "test-token")

    assert bridge.call_host("diagnostics.ping", timeout=2.0) == {"status": "ok"}
    thread.join(timeout=2)
    assert observed["token"] == "test-token"
    assert observed["method"] == "diagnostics.ping"
    assert observed["params"]["_dcc_mcp_deadline_unix_ms"] > 0


def test_call_host_rejects_oversized_response(monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _address = listener.accept()
        with connection:
            connection.makefile("rb").readline()
            connection.sendall(b"x" * (bridge.MAX_MESSAGE_BYTES + 1))
        listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    monkeypatch.setenv("DCC_MCP_MARMOSET_BRIDGE_PORT", str(port))
    monkeypatch.setenv("DCC_MCP_MARMOSET_BRIDGE_TOKEN", "test-token")

    try:
        bridge.call_host("diagnostics.ping", timeout=2.0)
    except RuntimeError as exc:
        assert "exceeds 1 MiB" in str(exc)
    else:
        raise AssertionError("oversized response was accepted")
    thread.join(timeout=2)


def test_health_probe_has_no_main_thread_deadline(monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    observed = {}

    def serve():
        connection, _address = listener.accept()
        with connection:
            request = json.loads(connection.makefile("rb").readline().decode("utf-8"))
            observed.update(request)
            connection.sendall(b'{"result":{"status":"ok"}}\n')
        listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    monkeypatch.setenv("DCC_MCP_MARMOSET_BRIDGE_PORT", str(port))
    monkeypatch.setenv("DCC_MCP_MARMOSET_BRIDGE_TOKEN", "test-token")

    assert bridge.call_host("bridge.health", timeout=8.0) == {"status": "ok"}
    thread.join(timeout=2)
    assert observed["params"] == {}
