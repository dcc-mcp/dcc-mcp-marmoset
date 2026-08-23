"""Marmoset Toolbag plugin entry point."""

import datetime
import json
import os
import tempfile
import traceback
from pathlib import Path

mset = None
plugin_dir = None

try:
    import mset
    from _runtime import start_runtime

    plugin_path = Path(mset.getPluginPath()).resolve()
    plugin_dir = plugin_path if plugin_path.is_dir() else plugin_path.parent
    _runtime = start_runtime(mset, plugin_dir)
    bootstrap_error = plugin_dir / "bootstrap-error.json"
    if bootstrap_error.exists():
        bootstrap_error.unlink()
except Exception as exc:
    diagnostic = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stage": "toolbag_plugin_bootstrap",
        "error_class": type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
        "traceback": traceback.format_exc(),
    }
    diagnostic_path = (
        plugin_dir / "bootstrap-error.json"
        if plugin_dir is not None
        else Path(tempfile.gettempdir(), f"dcc-mcp-marmoset-plugin-{os.getpid()}.json")
    )
    try:
        diagnostic_path.write_text(
            json.dumps(diagnostic, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception as logging_exc:
        if mset is not None:
            mset.err(f"DCC-MCP bootstrap diagnostic write failed: {logging_exc}")
    if mset is not None:
        mset.err(f"DCC-MCP Marmoset failed to start: {exc}; diagnostic: {diagnostic_path}")
    raise
