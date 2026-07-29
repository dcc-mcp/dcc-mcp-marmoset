"""Marmoset Toolbag plugin entry point."""

import os
import tempfile
import traceback
from pathlib import Path

import mset

try:
    from _runtime import start_runtime

    plugin_path = Path(mset.getPluginPath()).resolve()
    plugin_dir = plugin_path if plugin_path.is_dir() else plugin_path.parent
    _runtime = start_runtime(mset, plugin_dir)
except Exception as exc:
    Path(tempfile.gettempdir(), f"dcc-mcp-marmoset-plugin-{os.getpid()}.log").write_text(
        traceback.format_exc(),
        encoding="utf-8",
    )
    mset.err(f"DCC-MCP Marmoset failed to start: {exc}")
    raise
