"""Marmoset Toolbag plugin entry point."""

import datetime
import json
import os
import re
import tempfile
from pathlib import Path

mset = None
plugin_dir = None
_MAX_PUBLIC_MESSAGE_CHARS = 512
_SECRET_RE = re.compile(
    r"(?i)\b(token|password|secret|api[-_]?key|authorization)\b\s*[:=]\s*[^\s,;]+"
)
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\t\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:[^\s/]+/)*[^\s,;:\"']+")


def _error_type(error):
    cls = type(error)
    module = getattr(cls, "__module__", "")
    qualname = getattr(cls, "__qualname__", getattr(cls, "__name__", "Exception"))
    identity = "%s.%s" % (module, qualname) if module and module != "builtins" else str(qualname)
    return identity[:256]


def _safe_message(error):
    fallback = _error_type(error)
    try:
        text = str(error)
    except BaseException:
        text = fallback
    text = _SECRET_RE.sub(lambda match: "%s=[REDACTED]" % match.group(1), text)
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    text = _POSIX_PATH_RE.sub("<path>", text)
    text = " ".join(text.split()) or fallback
    return text[:_MAX_PUBLIC_MESSAGE_CHARS]


try:
    import mset
    from _runtime import start_runtime

    plugin_path = Path(mset.getPluginPath()).resolve()
    plugin_dir = plugin_path if plugin_path.is_dir() else plugin_path.parent
    _runtime = start_runtime(mset, plugin_dir)
    bootstrap_error = plugin_dir / "bootstrap-error.json"
    if bootstrap_error.exists():
        bootstrap_error.unlink()
except BaseException as exc:
    diagnostic = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stage": "toolbag_plugin_bootstrap",
        "error_class": _error_type(exc),
        "message": _safe_message(exc),
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
    except BaseException:
        if mset is not None:
            try:
                mset.err("DCC-MCP bootstrap diagnostic write failed.")
            except BaseException:
                pass
    if mset is not None:
        try:
            mset.err("DCC-MCP Marmoset failed to start; diagnostic: bootstrap-error.json")
        except BaseException:
            pass
    raise
