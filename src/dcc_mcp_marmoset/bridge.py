"""Bounded loopback bridge to the Toolbag plugin."""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any, Dict, Optional

MAX_MESSAGE_BYTES = 1024 * 1024


def _port() -> int:
    value = int(os.environ.get("DCC_MCP_MARMOSET_BRIDGE_PORT", "0"))
    if not 1 <= value <= 65535:
        raise RuntimeError("DCC_MCP_MARMOSET_BRIDGE_PORT must be between 1 and 65535")
    return value


def _token() -> str:
    value = os.environ.get("DCC_MCP_MARMOSET_BRIDGE_TOKEN", "")
    if not value:
        raise RuntimeError("DCC_MCP_MARMOSET_BRIDGE_TOKEN is required")
    return value


def call_host(
    method: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 620.0,
) -> Dict[str, Any]:
    """Invoke one typed Toolbag command through the plugin's main-thread queue."""
    request_params = dict(params or {})
    if method != "bridge.health":
        request_params["_dcc_mcp_deadline_unix_ms"] = int((time.time() + timeout) * 1000)
    payload = json.dumps(
        {"token": _token(), "method": method, "params": request_params},
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("Marmoset bridge request exceeds 1 MiB")

    with socket.create_connection(("127.0.0.1", _port()), timeout=min(timeout, 10.0)) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload + b"\n")
        response = _read_line(sock)

    try:
        envelope = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Marmoset bridge returned invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError("Marmoset bridge returned a non-object response")
    error = envelope.get("error")
    if error:
        message = (
            error.get("message", "Toolbag command failed") if isinstance(error, dict) else error
        )
        raise RuntimeError(str(message))
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Marmoset bridge response is missing an object result")
    return result


def is_connected() -> bool:
    """Return whether the authenticated bridge listener remains available."""
    try:
        call_host("bridge.health", timeout=8.0)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _read_line(sock: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= MAX_MESSAGE_BYTES:
        chunk = sock.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        chunks.extend(chunk if newline < 0 else chunk[:newline])
        if newline >= 0:
            break
    if len(chunks) > MAX_MESSAGE_BYTES:
        raise RuntimeError("Marmoset bridge response exceeds 1 MiB")
    if not chunks:
        raise RuntimeError("Marmoset bridge closed without a response")
    return bytes(chunks)
