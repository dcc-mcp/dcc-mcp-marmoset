"""Out-of-process DCC-MCP server bound to one Toolbag plugin instance."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from dcc_mcp_core import DccServerOptions, HostExecutionBridge
from dcc_mcp_core.host import QueueDispatcher, StandaloneHost
from dcc_mcp_core.readiness import AdapterReadinessBinder
from dcc_mcp_core.server_base import DccServerBase

from . import bridge
from .__version__ import __version__
from .dispatcher import MarmosetBridgeDispatcher

_server: Optional["MarmosetMcpServer"] = None


def publish_scene_snapshot(snapshot: dict[str, Any]) -> None:
    """Publish the latest bounded scene inspection through Core resources."""
    if _server is not None:
        _server.set_scene_resource(snapshot)


class MarmosetMcpServer(DccServerBase):
    """DCC-MCP server backed by the pure-Python Toolbag plugin."""

    def __init__(self, port: Optional[int] = None, host_pid: Optional[int] = None) -> None:
        resolved_pid = host_pid or int(os.environ.get("DCC_MCP_MARMOSET_HOST_PID", "0"))
        if resolved_pid <= 0:
            raise ValueError("A live Marmoset Toolbag host PID is required")

        self._host_dispatcher = QueueDispatcher()
        self._host_driver = StandaloneHost(
            self._host_dispatcher,
            thread_name="dcc-mcp-marmoset-host",
        )
        execution_bridge = HostExecutionBridge(
            dispatcher=MarmosetBridgeDispatcher(),
            host_dispatcher=self._host_dispatcher,
            default_thread_affinity="main",
            default_execution="sync",
            default_timeout_hint_secs=60,
        )
        options = DccServerOptions.from_env(
            "marmoset",
            Path(__file__).resolve().parent / "skills",
            port=port,
            server_name="dcc-mcp-marmoset",
            server_version=__version__,
            adapter_version=__version__,
            dcc_pid=resolved_pid,
            instance_type="gui",
            execution_bridge=execution_bridge,
        )
        super().__init__(options=options)
        self._readiness = AdapterReadinessBinder(self)
        self._readiness_stop = threading.Event()
        self._readiness_thread: Optional[threading.Thread] = None
        self._set_bridge_readiness(False)

    def start(self, **kwargs: Any) -> Any:
        self._host_driver.start()
        try:
            handle = super().start(**kwargs)
            self._start_readiness_monitor()
            return handle
        except Exception:
            try:
                super().stop()
            finally:
                self._host_driver.stop()
            raise

    def stop(self) -> None:
        self._stop_readiness_monitor()
        try:
            super().stop()
        finally:
            self._host_driver.stop()

    def _set_bridge_readiness(self, ready: bool) -> None:
        self._readiness.mark_dispatcher_ready(
            ready,
            host_execution_bridge_ready=ready,
            main_thread_executor_ready=ready,
            dcc_ready=ready,
        )

    def _start_readiness_monitor(self) -> None:
        if self._readiness_thread is not None and self._readiness_thread.is_alive():
            return
        self._readiness_stop.clear()
        self._readiness_thread = threading.Thread(
            target=self._monitor_bridge_readiness,
            name="dcc-mcp-marmoset-readiness",
            daemon=True,
        )
        self._readiness_thread.start()

    def _monitor_bridge_readiness(self) -> None:
        while not self._readiness_stop.wait(2.0):
            if bridge.is_connected():
                self._set_bridge_readiness(True)
                return

    def _stop_readiness_monitor(self) -> None:
        self._readiness_stop.set()
        thread, self._readiness_thread = self._readiness_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._set_bridge_readiness(False)

    def _version_string(self) -> str:
        return os.environ.get("DCC_MCP_MARMOSET_VERSION", "unknown")


def start_server(
    port: Optional[int] = None,
    host_pid: Optional[int] = None,
) -> MarmosetMcpServer:
    """Start the singleton service for one live Toolbag process."""
    global _server
    if _server is None or not _server.is_running:
        _server = MarmosetMcpServer(port=port, host_pid=host_pid)
        _server.register_builtin_actions()
        _server.start()
    return _server


def stop_server() -> None:
    """Stop the singleton service."""
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:
                return True
            if error == 87:
                return False
            raise OSError(error, ctypes.FormatError(error))
        try:
            result = kernel32.WaitForSingleObject(handle, 0)
            if result == 258:
                return True
            if result == 0:
                return False
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the DCC-MCP Marmoset adapter.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--host-pid", type=int, required=True)
    parser.add_argument("--bridge-port", type=int, required=True)
    parser.add_argument("--mcp-port", type=int)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run until Toolbag exits or the service receives a stop signal."""
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    if not 1 <= args.bridge_port <= 65535:
        raise SystemExit("--bridge-port must be between 1 and 65535")
    if args.host_pid <= 0:
        raise SystemExit("--host-pid must be a positive process id")
    os.environ["DCC_MCP_MARMOSET_BRIDGE_PORT"] = str(args.bridge_port)
    os.environ["DCC_MCP_MARMOSET_HOST_PID"] = str(args.host_pid)

    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())

    start_server(port=args.mcp_port, host_pid=args.host_pid)
    try:
        while not stopped.wait(1.0):
            if not _process_is_alive(args.host_pid):
                break
    finally:
        stop_server()


if __name__ == "__main__":
    main()
