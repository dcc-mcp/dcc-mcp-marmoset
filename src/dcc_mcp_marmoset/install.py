"""Agent-first installation lifecycle for the Marmoset Toolbag adapter."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Sequence

from dcc_mcp_core.deployment import (
    INSTALL_EXIT_ACQUIRE,
    INSTALL_EXIT_INSTALL,
    INSTALL_EXIT_OK,
    INSTALL_EXIT_PREFLIGHT,
    INSTALL_EXIT_REQUIRES_RESTART,
    INSTALL_EXIT_VERIFY,
    INSTALL_SOP_SCHEMA_VERSION,
)
from dcc_mcp_core.deployment import (
    load_install_sop_schema as _load_install_sop_schema,
)
from dcc_mcp_core.deployment import (
    safe_remove_tree as _safe_remove_tree,
)

from .__version__ import __version__

EXIT_OK = INSTALL_EXIT_OK
EXIT_PREFLIGHT = INSTALL_EXIT_PREFLIGHT
EXIT_ACQUIRE = INSTALL_EXIT_ACQUIRE
EXIT_INSTALL = INSTALL_EXIT_INSTALL
EXIT_VERIFY = INSTALL_EXIT_VERIFY
EXIT_REQUIRES_RESTART = INSTALL_EXIT_REQUIRES_RESTART

SCHEMA_VERSION = INSTALL_SOP_SCHEMA_VERSION
RECEIPT_VERSION = 1
DCC_TYPE = "marmoset"
DISTRIBUTION_NAME = "dcc-mcp-marmoset"
MIN_CORE_VERSION = "0.20.14"
PLUGIN_NAME = "DCC-MCP"
LEGACY_PLUGIN_NAME = "dcc_mcp_marmoset"
LIFECYCLE_COMMANDS = frozenset({"install", "status", "verify", "uninstall", "upgrade"})
DEFAULT_RECEIPT_PATH = Path.home() / ".dcc-mcp" / "receipts" / "marmoset.json"
BOOTSTRAP_ERROR_NAME = "bootstrap-error.json"
MAX_PUBLIC_MESSAGE_CHARS = 512
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\+[-.0-9A-Za-z]+)?$")
_CLEANUP_NAME_RE = re.compile(
    rf"^\.(?:{re.escape(PLUGIN_NAME)}|{re.escape(LEGACY_PLUGIN_NAME)})"
    r"\.(?:backup|stage|uninstall|restore)-[0-9a-f]{32}$"
)
_SECRET_RE = re.compile(
    r"(?i)\b(token|password|secret|api[-_]?key|authorization)\b\s*[:=]\s*[^\s,;]+"
)
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\t\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:[^\s/]+/)*[^\s,;:\"']+")

_PathIdentity = tuple[int, int, int, int, int]
_OwnedFileSnapshot = tuple[str, str, _PathIdentity]
_OwnedTargetSnapshot = tuple[_PathIdentity, tuple[_OwnedFileSnapshot, ...]]


def load_install_sop_schema() -> dict[str, Any]:
    """Load the published Core Install SOP schema."""
    return _load_install_sop_schema()


def _safe_exception_type(exc: BaseException) -> str:
    cls = type(exc)
    module = getattr(cls, "__module__", "")
    qualname = getattr(cls, "__qualname__", getattr(cls, "__name__", "Exception"))
    identity = f"{module}.{qualname}" if module and module != "builtins" else str(qualname)
    return identity[:256]


def _public_text(value: Any, fallback: str) -> str:
    try:
        text = str(value)
    except BaseException:
        text = fallback
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    text = _POSIX_PATH_RE.sub("<path>", text)
    text = " ".join(text.split()) or fallback
    return text[:MAX_PUBLIC_MESSAGE_CHARS]


def _safe_public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key)[:128]: _safe_public_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_public_value(item) for item in value[:64]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _public_text(value, "unavailable")


def _schema_sha256(schema: dict[str, Any]) -> str:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _release_version(value: Any) -> Optional[tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = _RELEASE_VERSION_RE.fullmatch(value)
    return tuple(int(part) for part in match.groups()) if match else None


class LifecycleError(RuntimeError):
    """A classified Install SOP failure."""

    def __init__(
        self,
        exit_code: int,
        stage: str,
        reason: str,
        message: str,
        *,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stage = stage
        self.reason = reason
        self.details = details or {}


class _UnsafeInstalledPath(OSError):
    def __init__(self, path: Path) -> None:
        super().__init__("installed path is not an ordinary file or directory")
        self.path = path


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _absolute_path(value: Any) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _path_identity(path: Path) -> Optional[_PathIdentity]:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(getattr(metadata, "st_file_attributes", 0)),
        int(metadata.st_ctime_ns),
    )


def _manifest_path(value: Any) -> Optional[tuple[str, str]]:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or value != candidate.as_posix():
        return None
    if any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        return None
    return value, normalized.casefold()


def _generated_cleanup_path(path: Path, plugin_root: Path) -> bool:
    return path.parent == plugin_root and _CLEANUP_NAME_RE.fullmatch(path.name) is not None


def _receipt_path(value: Optional[Path]) -> Path:
    configured = value or Path(os.environ.get("DCC_MCP_MARMOSET_RECEIPT", DEFAULT_RECEIPT_PATH))
    return _absolute_path(configured)


def _base_report(command: str, receipt_path: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "running",
        "dcc_type": DCC_TYPE,
        "command": command,
        "adapter_version": __version__,
        "core_version": _distribution_version("dcc-mcp-core"),
        "steps": [],
        "next_steps": [],
        "receipt_path": str(receipt_path),
        "verify": {
            "directly_usable": False,
            "failure_stage": None,
            "failure_reason": None,
        },
        "directly_usable": False,
        "failure_stage": None,
        "failure_reason": None,
        "requires_restart": False,
    }


def _set_failure(
    report: dict[str, Any],
    *,
    stage: str,
    reason: str,
    message: str,
    status: str = "failed",
    details: Optional[dict[str, Any]] = None,
) -> None:
    report.update(
        {
            "status": status,
            "failure_stage": stage,
            "failure_reason": reason,
            "message": _public_text(message, reason),
        }
    )
    report["verify"].update(
        {"directly_usable": False, "failure_stage": stage, "failure_reason": reason}
    )
    if details:
        report["details"] = _safe_public_value(details)


def _next_step(
    step_id: str,
    description: str,
    why: str,
    command: Sequence[str],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "description": description,
        "why": why,
        "command": [str(item) for item in command],
        **extra,
    }


def _plugin_dir_next_step(args: argparse.Namespace, receipt_path: Path) -> dict[str, Any]:
    command = ["dcc-mcp-marmoset", args.command, "--json", "--dry-run"]
    for flag, value in (
        ("--dcc-path", args.dcc_path),
        ("--python", args.python),
        ("--receipt-path", receipt_path),
        ("--toolbag-version", args.toolbag_version),
    ):
        if value:
            command.extend([flag, str(value)])
    command.extend(["--plugin-dir", "<PATH_FROM_TOOLBAG_UI>"])
    return _next_step(
        "capture-plugin-directory",
        "Copy Toolbag's exact user plugin folder and repeat this plan.",
        "Toolbag exposes this per-user folder in its GUI and the adapter must not guess it.",
        command,
        host="Marmoset Toolbag",
        menu_path="Edit > Plugins > Show User Plugin Folder",
        action="Copy the displayed absolute folder path into --plugin-dir.",
    )


def _command_for(args: argparse.Namespace, receipt_path: Path, command: str) -> list[str]:
    result = ["dcc-mcp-marmoset", command, "--json"]
    for flag, value in (
        ("--dcc-path", args.dcc_path),
        ("--python", args.python),
        ("--plugin-dir", args.plugin_dir),
        ("--receipt-path", receipt_path),
        ("--toolbag-version", args.toolbag_version),
        ("--registry-dir", args.registry_dir),
    ):
        if value:
            result.extend([flag, str(value)])
    return result


def _execute_next_step(args: argparse.Namespace, receipt_path: Path) -> dict[str, Any]:
    command = _command_for(args, receipt_path, args.command)
    command.insert(3, "--yes")
    return _next_step(
        "execute-plan",
        f"Execute the validated {args.command} plan.",
        "Planning and dry-run modes never mutate Toolbag's plugin folder.",
        command,
    )


def _verify_next_step(args: argparse.Namespace, receipt_path: Path) -> dict[str, Any]:
    return _next_step(
        "start-toolbag-and-verify",
        "Refresh Toolbag's plugins, launch DCC-MCP, and repeat verification.",
        "Only a live Toolbag bridge can prove typed host readiness.",
        _command_for(args, receipt_path, "verify"),
        host="Marmoset Toolbag",
        menu_path="Edit > Plugins > Refresh; Edit > Plugins > DCC-MCP",
        action="Refresh plugins, launch DCC-MCP, keep its status window open, then run command.",
    )


def _emit(args: argparse.Namespace, report: dict[str, Any], exit_code: int) -> int:
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['command']}: {report['status']}")
        if report.get("message"):
            print(report["message"])
        for next_step in report["next_steps"]:
            print("Next:", " ".join(next_step["command"]))
    return exit_code


def _read_receipt(path: Path, *, required: bool = False) -> Optional[dict[str, Any]]:
    if not os.path.lexists(path):
        if required:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_missing",
                f"No Marmoset install receipt exists at {path}.",
            )
        return None
    if _is_link_or_reparse(path) or not path.is_file():
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_path_unsafe",
            "The Marmoset receipt must be an ordinary file, not a link or reparse point.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_invalid",
            f"The Marmoset install receipt is unreadable: {exc}",
        ) from exc
    if (
        not isinstance(value, dict)
        or isinstance(value.get("receipt_version"), bool)
        or value.get("receipt_version") != RECEIPT_VERSION
    ):
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_invalid",
            "The Marmoset install receipt has an unsupported schema.",
        )
    if value.get("distribution") != DISTRIBUTION_NAME or value.get("dcc_type") != DCC_TYPE:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_wrong_adapter",
            "The receipt does not belong to the Marmoset adapter.",
        )
    for field in (
        "adapter_version",
        "core_version",
        "dcc_path",
        "host_version",
        "python",
        "plugin_root",
        "target_path",
        "server_path",
    ):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_invalid",
                f"The receipt field {field} must be a non-empty string.",
            )
    if (
        _release_version(value["adapter_version"]) is None
        or _release_version(value["core_version"]) is None
    ):
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_version_invalid",
            "The receipt contains a malformed adapter or Core version.",
        )
    plugin_root = _absolute_path(value["plugin_root"])
    target = _absolute_path(value["target_path"])
    if target.parent != plugin_root or target.name != PLUGIN_NAME:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_unsafe_target",
            "The receipt target is not the exact DCC-MCP folder under its recorded plugin root.",
        )
    for candidate in (plugin_root, target):
        if candidate.exists() and _is_link_or_reparse(candidate):
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_unsafe_target",
                "The receipt cannot authorize a linked or reparse-point plugin location.",
            )
    files = value.get("installed_files")
    if not isinstance(files, list) or not files:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_manifest_missing",
            "The receipt does not contain a non-empty installed file manifest.",
        )
    aliases: set[str] = set()
    canonical_paths: list[str] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_manifest_invalid",
                "Each receipt manifest entry must contain only path and sha256 strings.",
            )
        normalized = _manifest_path(item.get("path"))
        if normalized is None or not isinstance(item.get("sha256"), str):
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_manifest_invalid",
                "The receipt manifest contains an unsafe path or digest.",
            )
        relative, alias = normalized
        if not _SHA256_RE.fullmatch(item["sha256"]):
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_manifest_invalid",
                "The receipt manifest contains a malformed SHA-256 digest.",
            )
        if alias in aliases:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_manifest_duplicate",
                "The receipt manifest contains duplicate or aliased paths.",
            )
        aliases.add(alias)
        canonical_paths.append(relative)
    if canonical_paths != sorted(canonical_paths, key=lambda item: item.casefold()):
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_manifest_invalid",
            "The receipt manifest is not in canonical path order.",
        )
    pending = value.get("pending_cleanup", [])
    if not isinstance(pending, list):
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_cleanup_invalid",
            "The receipt pending cleanup set must be a list.",
        )
    for item in pending:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_cleanup_invalid",
                "The receipt pending cleanup entry is malformed.",
            )
        cleanup = _absolute_path(item["path"])
        if not _generated_cleanup_path(cleanup, plugin_root):
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_cleanup_unsafe",
                "The receipt pending cleanup entry is outside the adapter-owned set.",
            )
    value["pending_cleanup"] = pending
    return value


def _resolve_plugin_root(
    args: argparse.Namespace, receipt: Optional[dict[str, Any]]
) -> Optional[Path]:
    value: Any = args.plugin_dir or (receipt or {}).get("plugin_root")
    value = value or os.environ.get("DCC_MCP_MARMOSET_PLUGIN_DIR")
    return _absolute_path(value) if value else None


def _resolve_dcc_path(
    args: argparse.Namespace, receipt: Optional[dict[str, Any]]
) -> Optional[Path]:
    value: Any = args.dcc_path or (receipt or {}).get("dcc_path")
    value = value or os.environ.get("DCC_MCP_MARMOSET_DCC_PATH")
    return Path(str(value)).expanduser().resolve() if value else None


def _resolve_python(args: argparse.Namespace, receipt: Optional[dict[str, Any]]) -> Path:
    value: Any = args.python or (receipt or {}).get("python") or sys.executable
    return Path(str(value)).expanduser().resolve()


def _windows_file_version(path: Path) -> Optional[str]:
    if os.name != "nt" or not path.is_file():
        return None
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not ctypes.windll.version.VerQueryValueW(
        buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
    ):
        return None

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("signature", ctypes.c_uint32),
            ("structure_version", ctypes.c_uint32),
            ("file_version_ms", ctypes.c_uint32),
            ("file_version_ls", ctypes.c_uint32),
            ("product_version_ms", ctypes.c_uint32),
            ("product_version_ls", ctypes.c_uint32),
            ("file_flags_mask", ctypes.c_uint32),
            ("file_flags", ctypes.c_uint32),
            ("file_os", ctypes.c_uint32),
            ("file_type", ctypes.c_uint32),
            ("file_subtype", ctypes.c_uint32),
            ("file_date_ms", ctypes.c_uint32),
            ("file_date_ls", ctypes.c_uint32),
        ]

    info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
    return ".".join(
        str(part)
        for part in (
            info.product_version_ms >> 16,
            info.product_version_ms & 0xFFFF,
            info.product_version_ls >> 16,
            info.product_version_ls & 0xFFFF,
        )
    )


def _macos_bundle_version(path: Path) -> Optional[str]:
    bundle = path.parent.parent.parent if path.is_file() and path.parent.name == "MacOS" else path
    info_path = bundle / "Contents" / "Info.plist"
    if not info_path.is_file():
        return None
    try:
        with info_path.open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    value = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
    return str(value) if value else None


def _toolbag_version_tuple(value: str) -> Optional[tuple[int, int, int]]:
    match = re.search(r"(?<!\d)(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _inspect_toolbag(dcc_path: Path, explicit_version: Optional[str]) -> dict[str, Any]:
    if not dcc_path.exists():
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "dcc_path_missing",
            f"Marmoset Toolbag path does not exist: {dcc_path}",
        )
    metadata_version = _windows_file_version(dcc_path) or _macos_bundle_version(dcc_path)
    if metadata_version and explicit_version:
        actual = _toolbag_version_tuple(metadata_version)
        supplied = _toolbag_version_tuple(explicit_version)
        if actual and supplied and actual[:2] != supplied[:2]:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "host_version_mismatch",
                f"Toolbag metadata reports {metadata_version}, not {explicit_version}.",
            )
    version = metadata_version or explicit_version
    parsed = _toolbag_version_tuple(version or "")
    if parsed is None:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "host_version_unavailable",
            "Toolbag version metadata is unavailable; pass its exact version "
            "with --toolbag-version.",
        )
    if not (parsed[0] == 5 or (parsed[0] == 4 and parsed[1] >= 3)):
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "unsupported_toolbag_version",
            f"Toolbag {version} is unsupported; use Toolbag 4.03+ or 5.x.",
        )
    return {
        "path": str(dcc_path),
        "version": str(version),
        "version_source": "binary_metadata" if metadata_version else "explicit",
        "supported_range": "4.03+ or 5.x",
    }


_INTERPRETER_PROBE = """
import hashlib
import importlib.metadata
import json
import sysconfig
import dcc_mcp_core
import dcc_mcp_marmoset
from dcc_mcp_core.deployment import INSTALL_SOP_SCHEMA_VERSION, load_install_sop_schema
schema = load_install_sop_schema()
schema_bytes = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
print(json.dumps({
    "distribution": "dcc-mcp-marmoset",
    "adapter_version": importlib.metadata.version("dcc-mcp-marmoset"),
    "adapter_module_version": dcc_mcp_marmoset.__version__,
    "adapter_path": dcc_mcp_marmoset.__file__,
    "core_version": importlib.metadata.version("dcc-mcp-core"),
    "core_path": dcc_mcp_core.__file__,
    "scripts": sysconfig.get_path("scripts"),
    "schema_version": INSTALL_SOP_SCHEMA_VERSION,
    "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
}))
"""


def _probe_python(python: Path, *, failure_code: int) -> dict[str, Any]:
    stage = "preflight" if failure_code == EXIT_PREFLIGHT else "import"
    if not python.is_file():
        raise LifecycleError(
            failure_code, stage, "python_missing", f"Target interpreter does not exist: {python}"
        )
    try:
        completed = subprocess.run(
            [str(python), "-c", _INTERPRETER_PROBE],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(
            failure_code, stage, "python_probe_failed", f"Could not run target Python: {exc}"
        ) from exc
    if completed.returncode != 0:
        diagnostic = _public_text(
            (completed.stderr or completed.stdout).strip()[-2000:],
            "target interpreter import failed",
        )
        raise LifecycleError(
            failure_code,
            stage,
            "target_import_failed",
            f"Target Python cannot import the adapter and Core: {diagnostic}",
        )
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise LifecycleError(
            failure_code,
            stage,
            "python_probe_invalid",
            "Target Python returned invalid probe data.",
        ) from exc
    required = {
        "distribution",
        "adapter_version",
        "core_version",
        "adapter_path",
        "core_path",
        "scripts",
    }
    if not isinstance(result, dict) or any(
        key not in result or not isinstance(result[key], str) or not result[key] for key in required
    ):
        raise LifecycleError(
            failure_code,
            stage,
            "python_probe_invalid",
            "Target Python returned incomplete probe data.",
        )
    if result["distribution"] != DISTRIBUTION_NAME:
        raise LifecycleError(
            failure_code,
            stage,
            "adapter_identity_mismatch",
            "Target Python resolved a different adapter distribution.",
        )
    if result["adapter_version"] != __version__:
        raise LifecycleError(
            failure_code,
            stage,
            "adapter_version_mismatch",
            "Target Python does not resolve the running adapter version.",
        )
    core_version = _release_version(result["core_version"])
    core_floor = _release_version(MIN_CORE_VERSION)
    if core_version is None or core_floor is None or core_version < core_floor:
        raise LifecycleError(
            failure_code,
            stage,
            "core_version_unsupported",
            f"Target Python requires dcc-mcp-core {MIN_CORE_VERSION} or newer.",
        )
    extended = {
        "adapter_module_version",
        "schema_version",
        "schema_sha256",
    }
    if any(key not in result for key in extended):
        raise LifecycleError(
            failure_code,
            stage,
            "python_probe_invalid",
            "Target Python did not report the shared Install SOP contract.",
        )
    if result["adapter_module_version"] != result["adapter_version"]:
        raise LifecycleError(
            failure_code,
            stage,
            "adapter_identity_mismatch",
            "Target Python package metadata and adapter module version disagree.",
        )
    if (
        isinstance(result["schema_version"], bool)
        or result["schema_version"] != SCHEMA_VERSION
        or result["schema_sha256"] != _schema_sha256(load_install_sop_schema())
    ):
        raise LifecycleError(
            failure_code,
            stage,
            "install_schema_mismatch",
            "Target Python does not expose the current shared Install SOP schema.",
        )
    for key in ("adapter_path", "core_path"):
        module_path = _absolute_path(result[key])
        if not module_path.is_file() or _is_link_or_reparse(module_path):
            raise LifecycleError(
                failure_code,
                stage,
                "python_probe_invalid",
                f"Target Python reported an unsafe {key}.",
            )
    scripts = _absolute_path(result["scripts"])
    server = scripts / ("dcc-mcp-marmoset.exe" if os.name == "nt" else "dcc-mcp-marmoset")
    if not server.is_file():
        raise LifecycleError(
            failure_code,
            stage,
            "server_executable_missing",
            f"dcc-mcp-marmoset is missing from the target scripts folder: {server}",
        )
    return {**result, "python": str(python), "server_path": str(server)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(root: Path) -> list[dict[str, str]]:
    if _is_link_or_reparse(root) or not root.is_dir():
        raise _UnsafeInstalledPath(root)
    result: list[dict[str, str]] = []
    for current_text, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_text)
        kept_directories = []
        for name in directory_names:
            candidate = current / name
            if name == "__pycache__":
                continue
            if _is_link_or_reparse(candidate) or not candidate.is_dir():
                raise _UnsafeInstalledPath(candidate)
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            candidate = current / name
            if candidate.suffix == ".pyc" or name == BOOTSTRAP_ERROR_NAME:
                continue
            if _is_link_or_reparse(candidate):
                raise _UnsafeInstalledPath(candidate)
            try:
                metadata = os.lstat(candidate)
            except OSError as exc:
                raise _UnsafeInstalledPath(candidate) from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise _UnsafeInstalledPath(candidate)
            result.append(
                {"path": candidate.relative_to(root).as_posix(), "sha256": _sha256(candidate)}
            )
    return sorted(result, key=lambda item: item["path"].casefold())


def _write_json_file(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _atomic_write_json_file(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(f".{path.name}.stage-{uuid.uuid4().hex}")
    try:
        _write_json_file(stage, value)
        _replace_path(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _is_windows_lock(exc: OSError) -> bool:
    return os.name == "nt" and (
        isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
    )


def _cleanup_failure(path: Path, operation: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path),
        "operation": operation,
        "reason": str(result.get("reason") or "cleanup_failed"),
        "requires_restart": bool(result.get("requires_restart")),
    }


def _remove_generated_tree(
    path: Path, plugin_root: Path, operation: str
) -> Optional[dict[str, Any]]:
    if not _generated_cleanup_path(path, plugin_root) or _is_link_or_reparse(path):
        return {
            "path": str(path),
            "operation": operation,
            "reason": "cleanup_path_unsafe",
            "requires_restart": False,
        }
    result = _safe_remove_tree(path)
    return None if result.get("success") else _cleanup_failure(path, operation, result)


def _orphan_cleanup_paths(plugin_root: Path) -> list[Path]:
    try:
        children = list(plugin_root.iterdir())
    except OSError:
        return []
    return sorted(
        (path for path in children if _generated_cleanup_path(path, plugin_root)),
        key=lambda path: path.name.casefold(),
    )


def _converge_cleanup(
    plugin_root: Path,
    receipt_path: Path,
    receipt: Optional[dict[str, Any]],
    *,
    include_orphans: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    recorded = {
        _absolute_path(item["path"]): str(item.get("operation") or "install_cleanup")
        for item in (receipt or {}).get("pending_cleanup", [])
    }
    if include_orphans:
        for orphan in _orphan_cleanup_paths(plugin_root):
            recorded.setdefault(orphan, "orphan_cleanup")
    if not recorded:
        return [], False
    failures = []
    finalizing_uninstall = any(operation == "uninstall_cleanup" for operation in recorded.values())
    for path, operation in recorded.items():
        failure = _remove_generated_tree(path, plugin_root, operation)
        if failure:
            failures.append(failure)
    if receipt is not None:
        receipt["pending_cleanup"] = failures
        if not failures and finalizing_uninstall and not (plugin_root / PLUGIN_NAME).exists():
            receipt_path.unlink(missing_ok=True)
            return [], True
        _atomic_write_json_file(receipt_path, receipt)
    return failures, False


def _rollback_replace(
    target: Path,
    target_backup: Path,
    legacy: Path,
    legacy_backup: Path,
    *,
    stage_committed: bool,
) -> list[str]:
    failures = []
    try:
        if stage_committed and target.exists():
            shutil.rmtree(target)
        if target_backup.exists():
            _replace_path(target_backup, target)
    except OSError as exc:
        failures.append(f"target: {exc}")
    try:
        if legacy_backup.exists():
            _replace_path(legacy_backup, legacy)
    except OSError as exc:
        failures.append(f"legacy: {exc}")
    return failures


def _install_transaction(
    plugin_root: Path,
    server_path: Path,
    *,
    receipt_path: Optional[Path],
    receipt_values: Optional[dict[str, Any]],
    expected_receipt: Optional[dict[str, Any]] = None,
    expected_target_snapshot: Optional[_OwnedTargetSnapshot] = None,
) -> tuple[Path, list[dict[str, Any]]]:
    source = Path(__file__).resolve().parent / "toolbag_plugin"
    target = plugin_root / PLUGIN_NAME
    legacy = plugin_root / LEGACY_PLUGIN_NAME
    token = uuid.uuid4().hex
    stage = plugin_root / f".{PLUGIN_NAME}.stage-{token}"
    target_backup = plugin_root / f".{PLUGIN_NAME}.backup-{token}"
    legacy_backup = plugin_root / f".{LEGACY_PLUGIN_NAME}.backup-{token}"
    receipt_stage: Optional[Path] = None
    stage_committed = False
    try:
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stage / "server_path.txt").write_text(str(server_path.resolve()), encoding="utf-8")
        if not (stage / "__main__.py").is_file() or not (stage / "_runtime.py").is_file():
            raise OSError("the staged Toolbag plugin is incomplete")
        if receipt_path is not None and receipt_values is not None:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_stage = receipt_path.with_name(f".{receipt_path.name}.stage-{token}")
            receipt_values["installed_files"] = _manifest(stage)
            receipt_values["pending_cleanup"] = []
            _write_json_file(receipt_stage, receipt_values)
    except OSError as exc:
        shutil.rmtree(stage, ignore_errors=True)
        if receipt_stage is not None and receipt_stage.exists():
            receipt_stage.unlink()
        raise LifecycleError(
            EXIT_INSTALL,
            "install",
            "stage_failed",
            f"Could not create a complete staged plugin and receipt: {exc}",
        ) from exc
    try:
        if target.exists():
            identity_failure = (
                _revalidate_target_snapshot(expected_receipt, target, expected_target_snapshot)
                if expected_receipt is not None and expected_target_snapshot is not None
                else None
            )
            if identity_failure is not None:
                raise LifecycleError(
                    EXIT_PREFLIGHT,
                    "preflight",
                    identity_failure["reason"],
                    "The installed plugin changed immediately before replacement.",
                    details=identity_failure,
                )
            _replace_path(target, target_backup)
        if legacy.exists():
            _replace_path(legacy, legacy_backup)
        _replace_path(stage, target)
        stage_committed = True
        if receipt_path is not None and receipt_stage is not None:
            _replace_path(receipt_stage, receipt_path)
    except LifecycleError:
        shutil.rmtree(stage, ignore_errors=True)
        if receipt_stage is not None:
            receipt_stage.unlink(missing_ok=True)
        raise
    except OSError as exc:
        rollback_failures = _rollback_replace(
            target,
            target_backup,
            legacy,
            legacy_backup,
            stage_committed=stage_committed,
        )
        shutil.rmtree(stage, ignore_errors=True)
        if receipt_stage is not None:
            receipt_stage.unlink(missing_ok=True)
        if rollback_failures:
            raise LifecycleError(
                EXIT_INSTALL,
                "rollback",
                "rollback_failed",
                "Install commit failed and previous state could not be fully restored.",
                details={"commit_error": str(exc), "rollback_errors": rollback_failures},
            ) from exc
        code = EXIT_REQUIRES_RESTART if _is_windows_lock(exc) else EXIT_INSTALL
        reason = "windows_file_lock" if code == EXIT_REQUIRES_RESTART else "commit_failed"
        raise LifecycleError(
            code,
            "install",
            reason,
            f"Install commit failed; previous state was restored: {exc}",
            details={"locked_path": str(getattr(exc, "filename", "") or target)},
        ) from exc
    cleanup_failures: list[dict[str, Any]] = []
    for backup in (target_backup, legacy_backup):
        if backup.exists():
            failure = _remove_generated_tree(backup, plugin_root, "install_cleanup")
            if failure:
                cleanup_failures.append(failure)
    if cleanup_failures and receipt_path is not None and receipt_values is not None:
        receipt_values["pending_cleanup"] = cleanup_failures
        _atomic_write_json_file(receipt_path, receipt_values)
    return target, cleanup_failures


def _server_executable_from_current_python() -> Path:
    scripts_dir = Path(sys.executable).resolve().parent
    executable = scripts_dir / (
        "dcc-mcp-marmoset.exe" if sys.platform == "win32" else "dcc-mcp-marmoset"
    )
    if not executable.is_file():
        executable = Path(shutil.which("dcc-mcp-marmoset") or "")
    if not executable.is_file():
        raise RuntimeError("dcc-mcp-marmoset executable was not found in this Python environment")
    return executable.resolve()


def install_plugin(plugin_dir: Path, *, overwrite: bool = False) -> Path:
    """Legacy compatibility using the staged replacement primitive."""
    root = plugin_dir.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Toolbag user plugin folder does not exist: {root}")
    existing = [path for path in (root / PLUGIN_NAME, root / LEGACY_PLUGIN_NAME) if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(f"Plugin already exists: {existing[0]}")
    installed, _cleanup = _install_transaction(
        root,
        _server_executable_from_current_python(),
        receipt_path=None,
        receipt_values=None,
    )
    return installed


def _state(receipt: Optional[dict[str, Any]], plugin_root: Path) -> str:
    target_exists = (plugin_root / PLUGIN_NAME).exists()
    legacy_exists = (plugin_root / LEGACY_PLUGIN_NAME).exists()
    if receipt is None:
        return "partial" if target_exists or legacy_exists else "fresh"
    if not target_exists:
        return "partial"
    if receipt.get("adapter_version") != __version__:
        return "upgrade"
    return "installed"


def _validate_manifest(receipt: dict[str, Any], target: Path) -> Optional[dict[str, str]]:
    try:
        actual = _manifest(target)
    except _UnsafeInstalledPath as exc:
        return {"reason": "installed_path_unsafe", "path": str(exc.path)}
    expected = {item["path"]: item["sha256"] for item in receipt["installed_files"]}
    observed = {item["path"]: item["sha256"] for item in actual}
    missing = sorted(set(expected) - set(observed), key=str.casefold)
    extra = sorted(set(observed) - set(expected), key=str.casefold)
    if missing:
        return {"reason": "installed_file_missing", "path": str(target / missing[0])}
    if extra:
        return {"reason": "installed_file_set_mismatch", "path": str(target / extra[0])}
    for relative, digest in expected.items():
        if observed[relative] != digest:
            return {
                "reason": "installed_file_digest_mismatch",
                "path": str(target / relative),
            }
    return None


def _validated_target_identity(
    receipt: dict[str, Any], target: Path
) -> tuple[Optional[dict[str, str]], Optional[_OwnedTargetSnapshot]]:
    before = _path_identity(target)
    if before is None or _is_link_or_reparse(target) or not target.is_dir():
        return {"reason": "installed_path_unsafe", "path": str(target)}, None
    failure = _validate_manifest(receipt, target)
    if _path_identity(target) != before:
        return {"reason": "install_identity_changed", "path": str(target)}, None
    if failure is not None:
        return failure, None
    files: list[_OwnedFileSnapshot] = []
    for item in receipt["installed_files"]:
        relative = item["path"]
        candidate = target.joinpath(*PurePosixPath(relative).parts)
        identity = _path_identity(candidate)
        if identity is None or _is_link_or_reparse(candidate) or not candidate.is_file():
            return {"reason": "installed_path_unsafe", "path": str(candidate)}, None
        try:
            digest = _sha256(candidate)
        except OSError:
            return {"reason": "installed_path_unsafe", "path": str(candidate)}, None
        if _path_identity(candidate) != identity:
            return {"reason": "install_identity_changed", "path": str(candidate)}, None
        if digest != item["sha256"]:
            return {"reason": "installed_file_digest_mismatch", "path": str(candidate)}, None
        files.append((relative, digest, identity))
    if _path_identity(target) != before:
        return {"reason": "install_identity_changed", "path": str(target)}, None
    return None, (before, tuple(files))


def _revalidate_target_snapshot(
    receipt: Optional[dict[str, Any]],
    target: Path,
    expected: Optional[_OwnedTargetSnapshot],
) -> Optional[dict[str, str]]:
    if receipt is None or expected is None:
        return {"reason": "install_identity_changed", "path": str(target)}
    failure, observed = _validated_target_identity(receipt, target)
    if failure is not None:
        return failure
    if observed != expected:
        return {"reason": "install_identity_changed", "path": str(target)}
    return None


def _verify_install(
    args: argparse.Namespace, receipt: dict[str, Any]
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    target = _absolute_path(receipt["target_path"])
    if not target.is_dir() or _is_link_or_reparse(target):
        return False, "plugin_missing", {"target_path": str(target)}
    server_file = target / "server_path.txt"
    if not server_file.is_file():
        return False, "server_path_missing", {"server_path": str(server_file)}
    configured_server = server_file.read_text(encoding="utf-8").strip()
    if configured_server != receipt.get("server_path"):
        return (
            False,
            "server_path_stale",
            {
                "expected": receipt.get("server_path"),
                "actual": configured_server,
            },
        )
    configured_server_path = _absolute_path(configured_server)
    if not configured_server_path.is_file() or _is_link_or_reparse(configured_server_path):
        return False, "server_executable_missing", {"server_path": configured_server}
    bootstrap_error = target / BOOTSTRAP_ERROR_NAME
    if bootstrap_error.is_file():
        try:
            diagnostic: Any = json.loads(bootstrap_error.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            diagnostic = {"stage": "toolbag_plugin_bootstrap", "message": "unreadable"}
        if not isinstance(diagnostic, dict):
            diagnostic = {"stage": "toolbag_plugin_bootstrap", "message": "invalid"}
        safe_diagnostic = {
            "stage": _public_text(
                diagnostic.get("stage", "toolbag_plugin_bootstrap"),
                "toolbag_plugin_bootstrap",
            ),
            "error_class": _public_text(
                diagnostic.get("error_class", "BootstrapError"), "BootstrapError"
            ),
            "message": _public_text(
                diagnostic.get("message", "Toolbag bootstrap failed."),
                "Toolbag bootstrap failed.",
            ),
        }
        return False, "bootstrap_error", {"bootstrap": safe_diagnostic}
    manifest_failure, _identity = _validated_target_identity(receipt, target)
    if manifest_failure:
        return False, manifest_failure["reason"], manifest_failure
    python = _resolve_python(args, receipt)
    try:
        python_probe = _probe_python(python, failure_code=EXIT_VERIFY)
    except LifecycleError as exc:
        return False, exc.reason, {"message": str(exc), **exc.details}
    if python_probe["adapter_version"] != receipt.get("adapter_version"):
        return (
            False,
            "adapter_version_mismatch",
            {
                "receipt": receipt.get("adapter_version"),
                "python": python_probe["adapter_version"],
            },
        )

    from dcc_mcp_core.install_lifecycle import wait_for_sidecar_ready

    readiness = wait_for_sidecar_ready(
        args.registry_dir,
        dcc_type=DCC_TYPE,
        timeout_secs=max(0.0, args.readiness_timeout),
        probe_tool="marmoset_scene__ping",
        probe_arguments={},
    )
    if not readiness.get("success"):
        return False, "sidecar_not_ready", {"readiness": readiness, "python": python_probe}
    return True, "ok", {"python": python_probe, "readiness": readiness}


def _handle_install(args: argparse.Namespace, *, upgrade: bool) -> tuple[dict[str, Any], int]:
    receipt_path = _receipt_path(args.receipt_path)
    report = _base_report(args.command, receipt_path)
    try:
        receipt = _read_receipt(receipt_path)
        if upgrade and receipt is None:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_missing",
                "Upgrade requires an existing receipt; use install for a fresh state.",
            )
        plugin_root = _resolve_plugin_root(args, receipt)
        if plugin_root is None:
            _set_failure(
                report,
                stage="preflight",
                reason="plugin_dir_required",
                message="The exact Toolbag user plugin folder is required.",
            )
            report["next_steps"] = [_plugin_dir_next_step(args, receipt_path)]
            return report, EXIT_PREFLIGHT
        if not plugin_root.is_dir():
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "plugin_dir_missing",
                f"Toolbag user plugin folder does not exist: {plugin_root}",
            )
        if _is_link_or_reparse(plugin_root):
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "plugin_dir_unsafe",
                "Toolbag's plugin folder must not be a link or reparse point.",
            )
        if receipt and Path(receipt["plugin_root"]).resolve() != plugin_root:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "plugin_dir_mismatch",
                "--plugin-dir does not match the existing receipt.",
            )
        dcc_path = _resolve_dcc_path(args, receipt)
        if dcc_path is None:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "dcc_path_required",
                "Pass the exact Marmoset Toolbag executable/application with --dcc-path.",
            )
        host = _inspect_toolbag(
            dcc_path, args.toolbag_version or (receipt or {}).get("host_version")
        )
        python = _resolve_python(args, receipt)
        python_probe = _probe_python(python, failure_code=EXIT_PREFLIGHT)
        report["core_version"] = python_probe["core_version"]
        target = plugin_root / PLUGIN_NAME
        expected_target_snapshot = None
        if receipt is not None and target.exists():
            manifest_failure, expected_target_snapshot = _validated_target_identity(receipt, target)
            if manifest_failure:
                raise LifecycleError(
                    EXIT_PREFLIGHT,
                    "preflight",
                    manifest_failure["reason"],
                    "The existing receipted plugin no longer matches its exact owned set.",
                    details=manifest_failure,
                )
        if args.yes and not args.dry_run:
            cleanup_failures, finalized_uninstall = _converge_cleanup(
                plugin_root,
                receipt_path,
                receipt,
                include_orphans=expected_target_snapshot is not None,
            )
            if cleanup_failures:
                requires_restart = any(item.get("requires_restart") for item in cleanup_failures)
                _set_failure(
                    report,
                    stage="install",
                    reason=(
                        "windows_file_lock" if requires_restart else "previous_state_cleanup_failed"
                    ),
                    message="A prior staged operation still requires bounded cleanup.",
                    status="requires_restart" if requires_restart else "partial",
                    details={"pending_cleanup": cleanup_failures},
                )
                report["requires_restart"] = requires_restart
                report["next_steps"] = [_execute_next_step(args, receipt_path)]
                return report, EXIT_REQUIRES_RESTART if requires_restart else EXIT_INSTALL
            if finalized_uninstall:
                receipt = None
                expected_target_snapshot = None
        install_state = _state(receipt, plugin_root)
        plan_type = (
            "upgrade"
            if upgrade or install_state == "upgrade"
            else ("repair" if install_state == "partial" else "fresh")
        )
        report.update(
            {
                "install_state": install_state,
                "plan_type": plan_type,
                "plugin_root": str(plugin_root),
                "dcc": host,
                "python": python_probe,
            }
        )
        report["steps"] = [
            {"id": "preflight", "status": "ok"},
            {"id": "stage", "status": "planned"},
            {"id": "commit", "status": "planned"},
            {"id": "verify", "status": "planned"},
        ]
        if args.dry_run or not args.yes:
            report["status"] = "planned"
            report["next_steps"] = [_execute_next_step(args, receipt_path)]
            return report, EXIT_OK
        receipt_values = {
            "receipt_version": RECEIPT_VERSION,
            "distribution": DISTRIBUTION_NAME,
            "dcc_type": DCC_TYPE,
            "adapter_version": python_probe["adapter_version"],
            "core_version": python_probe["core_version"],
            "dcc_path": str(dcc_path),
            "host_version": host["version"],
            "host_version_source": host["version_source"],
            "python": str(python),
            "plugin_root": str(plugin_root),
            "target_path": str(target),
            "server_path": python_probe["server_path"],
        }
        installed, cleanup_failures = _install_transaction(
            plugin_root,
            Path(python_probe["server_path"]),
            receipt_path=receipt_path,
            receipt_values=receipt_values,
            expected_receipt=receipt,
            expected_target_snapshot=expected_target_snapshot,
        )
        report["steps"][1]["status"] = "ok"
        report["steps"][2]["status"] = "ok"
        report["target_path"] = str(installed)
        if cleanup_failures:
            requires_restart = any(item.get("requires_restart") for item in cleanup_failures)
            _set_failure(
                report,
                stage="install",
                reason="windows_file_lock" if requires_restart else "previous_state_cleanup_failed",
                message=("New plugin is committed, but old-state cleanup did not complete."),
                status="requires_restart" if requires_restart else "partial",
                details={"pending_cleanup": cleanup_failures},
            )
            report["requires_restart"] = requires_restart
            report["next_steps"] = [_execute_next_step(args, receipt_path)]
            return report, EXIT_REQUIRES_RESTART if requires_restart else EXIT_INSTALL
        current_receipt = _read_receipt(receipt_path, required=True)
        assert current_receipt is not None
        usable, reason, verification = _verify_install(args, current_receipt)
        report["verification"] = verification
        if not usable:
            report["steps"][3]["status"] = "failed"
            _set_failure(
                report,
                stage="readiness" if reason == "sidecar_not_ready" else "verify",
                reason=reason,
                message="Plugin files are installed, but Toolbag readiness is not yet proven.",
            )
            report["next_steps"] = [_verify_next_step(args, receipt_path)]
            return report, EXIT_VERIFY
        report["steps"][3]["status"] = "ok"
        report["status"] = "ok"
        report["directly_usable"] = True
        report["verify"].update(
            {"directly_usable": True, "failure_stage": None, "failure_reason": None}
        )
        return report, EXIT_OK
    except LifecycleError as exc:
        status = "requires_restart" if exc.exit_code == EXIT_REQUIRES_RESTART else "failed"
        _set_failure(
            report,
            stage=exc.stage,
            reason=exc.reason,
            message=str(exc),
            status=status,
            details=exc.details,
        )
        report["requires_restart"] = exc.exit_code == EXIT_REQUIRES_RESTART
        return report, exc.exit_code


def _handle_status(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    receipt_path = _receipt_path(args.receipt_path)
    report = _base_report(args.command, receipt_path)
    try:
        receipt = _read_receipt(receipt_path)
        plugin_root = _resolve_plugin_root(args, receipt)
        if receipt is None and plugin_root is None:
            report.update({"status": "ok", "install_state": "fresh"})
            report["steps"] = [{"id": "inspect", "status": "ok"}]
            return report, EXIT_OK
        assert plugin_root is not None
        install_state = _state(receipt, plugin_root)
        if receipt is not None and receipt.get("pending_cleanup"):
            requires_restart = any(
                item.get("requires_restart") for item in receipt["pending_cleanup"]
            )
            _set_failure(
                report,
                stage="install",
                reason="windows_file_lock" if requires_restart else "pending_cleanup",
                message="A prior staged operation still has adapter-owned cleanup pending.",
                status="requires_restart" if requires_restart else "partial",
                details={"pending_cleanup": receipt["pending_cleanup"]},
            )
            report.update(
                {
                    "install_state": install_state,
                    "plugin_root": str(plugin_root),
                    "requires_restart": requires_restart,
                    "steps": [{"id": "inspect", "status": "partial"}],
                }
            )
            return report, EXIT_REQUIRES_RESTART if requires_restart else EXIT_PREFLIGHT
        if receipt is not None and (plugin_root / PLUGIN_NAME).exists():
            manifest_failure, _identity = _validated_target_identity(
                receipt, plugin_root / PLUGIN_NAME
            )
            if manifest_failure:
                _set_failure(
                    report,
                    stage="preflight",
                    reason=manifest_failure["reason"],
                    message="The receipted plugin no longer matches its exact owned set.",
                    status="partial",
                    details=manifest_failure,
                )
                report.update(
                    {
                        "install_state": "partial",
                        "plugin_root": str(plugin_root),
                        "steps": [{"id": "inspect", "status": "partial"}],
                    }
                )
                return report, EXIT_PREFLIGHT
        status = "ok" if install_state in {"installed", "upgrade"} else "partial"
        report.update(
            {"status": status, "install_state": install_state, "plugin_root": str(plugin_root)}
        )
        report["steps"] = [{"id": "inspect", "status": status}]
        if install_state == "partial":
            _set_failure(
                report,
                stage="preflight",
                reason="partial_install",
                message="Plugin files and receipt do not describe one complete state.",
                status="partial",
            )
            return report, EXIT_PREFLIGHT
        return report, EXIT_OK
    except LifecycleError as exc:
        _set_failure(report, stage=exc.stage, reason=exc.reason, message=str(exc))
        return report, exc.exit_code


def _handle_verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    receipt_path = _receipt_path(args.receipt_path)
    report = _base_report(args.command, receipt_path)
    try:
        receipt = _read_receipt(receipt_path, required=True)
        assert receipt is not None
        if receipt.get("pending_cleanup"):
            requires_restart = any(
                item.get("requires_restart") for item in receipt["pending_cleanup"]
            )
            _set_failure(
                report,
                stage="install",
                reason="windows_file_lock" if requires_restart else "pending_cleanup",
                message="Complete the recorded staged cleanup before verifying Toolbag readiness.",
                status="requires_restart" if requires_restart else "partial",
                details={"pending_cleanup": receipt["pending_cleanup"]},
            )
            report["requires_restart"] = requires_restart
            return report, EXIT_REQUIRES_RESTART if requires_restart else EXIT_PREFLIGHT
        if args.python and args.python.expanduser().resolve() != Path(receipt["python"]).resolve():
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "python_mismatch",
                "--python does not match the interpreter recorded by the receipt.",
            )
        if (
            args.dcc_path
            and args.dcc_path.expanduser().resolve() != Path(receipt["dcc_path"]).resolve()
        ):
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "dcc_path_mismatch",
                "--dcc-path does not match the Toolbag installation recorded by the receipt.",
            )
        plugin_root = _resolve_plugin_root(args, receipt)
        if plugin_root != Path(receipt["plugin_root"]).resolve():
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "plugin_dir_mismatch",
                "--plugin-dir does not match the receipt.",
            )
        dcc_path = _resolve_dcc_path(args, receipt)
        assert dcc_path is not None
        report["dcc"] = _inspect_toolbag(
            dcc_path, args.toolbag_version or receipt.get("host_version")
        )
        usable, reason, verification = _verify_install(args, receipt)
        report["verification"] = verification
        report["steps"] = [
            {"id": "receipt", "status": "ok"},
            {"id": "artifact", "status": "ok" if reason != "plugin_missing" else "failed"},
            {
                "id": "import",
                "status": ("ok" if usable or reason == "sidecar_not_ready" else "failed"),
            },
            {"id": "readiness", "status": "ok" if usable else "failed"},
        ]
        if not usable:
            stage = (
                "readiness"
                if reason == "sidecar_not_ready"
                else ("bootstrap" if reason == "bootstrap_error" else "verify")
            )
            _set_failure(
                report,
                stage=stage,
                reason=reason,
                message="Marmoset is installed, but verify-to-usable did not complete.",
            )
            report["next_steps"] = [_verify_next_step(args, receipt_path)]
            return report, EXIT_VERIFY
        report["status"] = "ok"
        report["directly_usable"] = True
        report["verify"].update(
            {"directly_usable": True, "failure_stage": None, "failure_reason": None}
        )
        return report, EXIT_OK
    except LifecycleError as exc:
        _set_failure(report, stage=exc.stage, reason=exc.reason, message=str(exc))
        return report, exc.exit_code


def _handle_uninstall(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    receipt_path = _receipt_path(args.receipt_path)
    report = _base_report(args.command, receipt_path)
    try:
        receipt = _read_receipt(receipt_path)
        if receipt is None:
            plugin_root = _resolve_plugin_root(args, None)
            unreceipted = plugin_root is not None and (
                (plugin_root / PLUGIN_NAME).exists() or (plugin_root / LEGACY_PLUGIN_NAME).exists()
            )
            if unreceipted:
                _set_failure(
                    report,
                    stage="preflight",
                    reason="unreceipted_plugin",
                    message=(
                        "An unreceipted plugin exists; uninstall refuses to remove ambiguous files."
                    ),
                    status="partial",
                )
                report["install_state"] = "partial"
                report["plugin_root"] = str(plugin_root)
                report["steps"] = [{"id": "receipt", "status": "failed"}]
                return report, EXIT_PREFLIGHT
            report.update({"status": "ok", "install_state": "fresh"})
            report["steps"] = [{"id": "uninstall", "status": "skipped"}]
            return report, EXIT_OK
        target = _absolute_path(receipt["target_path"])
        plugin_root = _absolute_path(receipt["plugin_root"])
        if args.plugin_dir and _absolute_path(args.plugin_dir) != plugin_root:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "plugin_dir_mismatch",
                "--plugin-dir does not match the receipt; uninstall will not inspect "
                "another folder.",
            )
        if receipt.get("pending_cleanup"):
            report.update({"plugin_root": str(plugin_root), "target_path": str(target)})
            report["steps"] = [
                {"id": "receipt", "status": "ok"},
                {"id": "cleanup", "status": "planned"},
            ]
            if args.dry_run or not args.yes:
                report["status"] = "planned"
                report["next_steps"] = [_execute_next_step(args, receipt_path)]
                return report, EXIT_OK
            cleanup_failures, finalized = _converge_cleanup(plugin_root, receipt_path, receipt)
            if cleanup_failures:
                requires_restart = any(item.get("requires_restart") for item in cleanup_failures)
                _set_failure(
                    report,
                    stage="uninstall",
                    reason="windows_file_lock" if requires_restart else "uninstall_cleanup_failed",
                    message="The bounded uninstall cleanup has not completed.",
                    status="requires_restart" if requires_restart else "partial",
                    details={"pending_cleanup": cleanup_failures},
                )
                report["requires_restart"] = requires_restart
                return report, EXIT_REQUIRES_RESTART if requires_restart else EXIT_INSTALL
            if finalized:
                report["steps"][1]["status"] = "ok"
                report.update({"status": "ok", "install_state": "fresh"})
                return report, EXIT_OK
        if not target.is_dir():
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_target_missing",
                f"The receipted plugin target is missing: {target}",
            )
        manifest_failure, target_snapshot = _validated_target_identity(receipt, target)
        if manifest_failure:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                manifest_failure["reason"],
                "Uninstall refuses a plugin that does not match the exact receipted owned set.",
                details=manifest_failure,
            )
        assert target_snapshot is not None
        report.update({"plugin_root": str(plugin_root), "target_path": str(target)})
        report["steps"] = [
            {"id": "receipt", "status": "ok"},
            {"id": "remove", "status": "planned"},
        ]
        if args.dry_run or not args.yes:
            report["status"] = "planned"
            report["next_steps"] = [_execute_next_step(args, receipt_path)]
            return report, EXIT_OK
        token = uuid.uuid4().hex
        tombstone = plugin_root / f".{PLUGIN_NAME}.uninstall-{token}"
        restore_copy = plugin_root / f".{PLUGIN_NAME}.restore-{token}"
        try:
            shutil.copytree(target, restore_copy)
        except OSError as exc:
            shutil.rmtree(restore_copy, ignore_errors=True)
            raise LifecycleError(
                EXIT_INSTALL,
                "uninstall",
                "uninstall_stage_failed",
                f"Could not stage a rollback copy before uninstall: {exc}",
            ) from exc
        identity_failure = _revalidate_target_snapshot(receipt, target, target_snapshot)
        if identity_failure is not None:
            shutil.rmtree(restore_copy, ignore_errors=True)
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                identity_failure["reason"],
                "The installed plugin changed immediately before uninstall.",
                details=identity_failure,
            )
        try:
            _replace_path(target, tombstone)
        except OSError as exc:
            shutil.rmtree(restore_copy, ignore_errors=True)
            code = EXIT_REQUIRES_RESTART if _is_windows_lock(exc) else EXIT_INSTALL
            reason = "windows_file_lock" if code == EXIT_REQUIRES_RESTART else "uninstall_failed"
            raise LifecycleError(
                code,
                "uninstall",
                reason,
                f"Uninstall failed; the receipted state was restored: {exc}",
            ) from exc
        removal = _safe_remove_tree(tombstone)
        if not removal.get("success"):
            requires_restart = bool(removal.get("requires_restart"))
            if not requires_restart:
                rollback_errors = []
                try:
                    if target.exists():
                        shutil.rmtree(target)
                    _replace_path(restore_copy, target)
                    shutil.rmtree(tombstone, ignore_errors=True)
                except OSError as rollback_exc:
                    rollback_errors.append(_public_text(rollback_exc, "rollback failed"))
                if rollback_errors:
                    raise LifecycleError(
                        EXIT_INSTALL,
                        "rollback",
                        "rollback_failed",
                        "Uninstall failed and could not restore the receipted state.",
                        details={"rollback_errors": rollback_errors},
                    )
                raise LifecycleError(
                    EXIT_INSTALL,
                    "uninstall",
                    "uninstall_failed",
                    "Uninstall cleanup failed; the exact receipted state was restored.",
                )
            pending = [
                _cleanup_failure(tombstone, "uninstall_cleanup", removal),
                {
                    "path": str(restore_copy),
                    "operation": "uninstall_cleanup",
                    "reason": "rollback_copy_pending",
                    "requires_restart": True,
                },
            ]
            receipt["pending_cleanup"] = pending
            _atomic_write_json_file(receipt_path, receipt)
            _set_failure(
                report,
                stage="uninstall",
                reason="windows_file_lock",
                message=(
                    "The plugin was detached, but Windows requires a restart for bounded cleanup."
                ),
                status="requires_restart",
                details={"pending_cleanup": pending},
            )
            report.update({"requires_restart": True, "install_state": "fresh"})
            return report, EXIT_REQUIRES_RESTART
        cleanup_failure = _remove_generated_tree(restore_copy, plugin_root, "uninstall_cleanup")
        if cleanup_failure:
            receipt["pending_cleanup"] = [cleanup_failure]
            _atomic_write_json_file(receipt_path, receipt)
            requires_restart = bool(cleanup_failure.get("requires_restart"))
            _set_failure(
                report,
                stage="uninstall",
                reason="windows_file_lock" if requires_restart else "uninstall_cleanup_failed",
                message="The plugin was removed, but its adapter-owned rollback copy remains.",
                status="requires_restart" if requires_restart else "partial",
                details={"pending_cleanup": [cleanup_failure]},
            )
            report.update({"requires_restart": requires_restart, "install_state": "fresh"})
            return report, EXIT_REQUIRES_RESTART if requires_restart else EXIT_INSTALL
        try:
            receipt_path.unlink()
        except OSError as exc:
            raise LifecycleError(
                EXIT_INSTALL,
                "uninstall",
                "receipt_remove_failed",
                f"The plugin was removed but its receipt could not be deleted: {exc}",
            ) from exc
        report["steps"][1]["status"] = "ok"
        report.update({"status": "ok", "install_state": "fresh"})
        return report, EXIT_OK
    except LifecycleError as exc:
        status = "requires_restart" if exc.exit_code == EXIT_REQUIRES_RESTART else "failed"
        _set_failure(
            report,
            stage=exc.stage,
            reason=exc.reason,
            message=str(exc),
            status=status,
            details=exc.details,
        )
        report["requires_restart"] = exc.exit_code == EXIT_REQUIRES_RESTART
        return report, exc.exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the DCC-MCP Marmoset installation.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in sorted(LIFECYCLE_COMMANDS):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--json", action="store_true")
        subparser.add_argument("--yes", action="store_true")
        subparser.add_argument("--dry-run", action="store_true")
        subparser.add_argument("--dcc-path", type=Path)
        subparser.add_argument("--python", type=Path)
        subparser.add_argument("--plugin-dir", type=Path)
        subparser.add_argument("--receipt-path", type=Path)
        subparser.add_argument("--toolbag-version")
        subparser.add_argument("--registry-dir", type=Path)
        subparser.add_argument("--readiness-timeout", type=float, default=2.0)
    return parser


def _legacy_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Deprecated compatibility installer; use dcc-mcp-marmoset install."
    )
    parser.add_argument("--plugin-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(list(argv))
    print(
        "warning: dcc-mcp-marmoset-install is deprecated; use dcc-mcp-marmoset install",
        file=sys.stderr,
    )
    print(install_plugin(args.plugin_dir, overwrite=args.overwrite))
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one lifecycle verb or the deprecated installer alias."""
    raw = list(argv) if argv is not None else sys.argv[1:]
    if not raw or raw[0] not in LIFECYCLE_COMMANDS:
        return _legacy_main(raw)
    args = _build_parser().parse_args(raw)
    try:
        if args.command == "install":
            report, exit_code = _handle_install(args, upgrade=False)
        elif args.command == "upgrade":
            report, exit_code = _handle_install(args, upgrade=True)
        elif args.command == "status":
            report, exit_code = _handle_status(args)
        elif args.command == "verify":
            report, exit_code = _handle_verify(args)
        else:
            report, exit_code = _handle_uninstall(args)
    except Exception as exc:
        report = _base_report(args.command, _receipt_path(args.receipt_path))
        _set_failure(
            report,
            stage="internal",
            reason="internal_error",
            message="The lifecycle command failed without a classified result.",
            details={"error_type": _safe_exception_type(exc)},
        )
        exit_code = EXIT_INSTALL
    return _emit(args, report, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
