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
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

from .__version__ import __version__

try:  # Core #2252 exports these after its Install SOP foundation is released.
    from dcc_mcp_core.deployment import (
        INSTALL_EXIT_ACQUIRE,
        INSTALL_EXIT_INSTALL,
        INSTALL_EXIT_OK,
        INSTALL_EXIT_PREFLIGHT,
        INSTALL_EXIT_REQUIRES_RESTART,
        INSTALL_EXIT_VERIFY,
    )
except ImportError:  # Thin compatibility for current Core releases.
    INSTALL_EXIT_OK = 0
    INSTALL_EXIT_PREFLIGHT = 10
    INSTALL_EXIT_ACQUIRE = 20
    INSTALL_EXIT_INSTALL = 30
    INSTALL_EXIT_VERIFY = 40
    INSTALL_EXIT_REQUIRES_RESTART = 50

EXIT_OK = INSTALL_EXIT_OK
EXIT_PREFLIGHT = INSTALL_EXIT_PREFLIGHT
EXIT_ACQUIRE = INSTALL_EXIT_ACQUIRE
EXIT_INSTALL = INSTALL_EXIT_INSTALL
EXIT_VERIFY = INSTALL_EXIT_VERIFY
EXIT_REQUIRES_RESTART = INSTALL_EXIT_REQUIRES_RESTART

SCHEMA_VERSION = 1
DCC_TYPE = "marmoset"
PLUGIN_NAME = "DCC-MCP"
LEGACY_PLUGIN_NAME = "dcc_mcp_marmoset"
LIFECYCLE_COMMANDS = frozenset({"install", "status", "verify", "uninstall", "upgrade"})
DEFAULT_RECEIPT_PATH = Path.home() / ".dcc-mcp" / "receipts" / "marmoset.json"
BOOTSTRAP_ERROR_NAME = "bootstrap-error.json"


def load_install_sop_schema() -> dict[str, Any]:
    """Load Core's shared SOP schema, with a temporary packaged fallback."""
    try:
        from dcc_mcp_core.deployment import load_install_sop_schema as load_shared_schema
    except ImportError:
        path = Path(__file__).resolve().parent / "schemas" / "adapter-install-sop-v1.schema.json"
        return json.loads(path.read_text(encoding="utf-8"))
    return load_shared_schema()


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


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _receipt_path(value: Optional[Path]) -> Path:
    configured = value or Path(os.environ.get("DCC_MCP_MARMOSET_RECEIPT", DEFAULT_RECEIPT_PATH))
    return configured.expanduser().resolve()


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
            "message": message,
        }
    )
    report["verify"].update(
        {"directly_usable": False, "failure_stage": stage, "failure_reason": reason}
    )
    if details:
        report["details"] = details


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
    if not path.is_file():
        if required:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_missing",
                f"No Marmoset install receipt exists at {path}.",
            )
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_invalid",
            f"The Marmoset install receipt is unreadable: {exc}",
        ) from exc
    if not isinstance(value, dict) or value.get("receipt_version") != 1:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_invalid",
            "The Marmoset install receipt has an unsupported schema.",
        )
    if value.get("dcc_type") != DCC_TYPE:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_wrong_adapter",
            "The receipt does not belong to the Marmoset adapter.",
        )
    root_text = str(value.get("plugin_root", ""))
    target_text = str(value.get("target_path", ""))
    plugin_root = Path(root_text).expanduser().resolve()
    target = Path(target_text).expanduser().resolve()
    if not root_text or not target_text or target != plugin_root / PLUGIN_NAME:
        raise LifecycleError(
            EXIT_PREFLIGHT,
            "preflight",
            "receipt_unsafe_target",
            "The receipt target is not the exact DCC-MCP folder under its recorded plugin root.",
        )
    return value


def _resolve_plugin_root(
    args: argparse.Namespace, receipt: Optional[dict[str, Any]]
) -> Optional[Path]:
    value: Any = args.plugin_dir or (receipt or {}).get("plugin_root")
    value = value or os.environ.get("DCC_MCP_MARMOSET_PLUGIN_DIR")
    return Path(str(value)).expanduser().resolve() if value else None


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
import importlib.metadata
import json
import sysconfig
import dcc_mcp_core
import dcc_mcp_marmoset
print(json.dumps({
    "adapter_version": importlib.metadata.version("dcc-mcp-marmoset"),
    "core_version": importlib.metadata.version("dcc-mcp-core"),
    "scripts": sysconfig.get_path("scripts"),
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
        diagnostic = (completed.stderr or completed.stdout).strip()[-2000:]
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
    scripts = Path(result["scripts"]).expanduser().resolve()
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
    return [
        {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path)}
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]


def _write_json_file(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _is_windows_lock(exc: OSError) -> bool:
    return os.name == "nt" and (
        isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
    )


def _rollback_replace(
    target: Path,
    target_backup: Path,
    legacy: Path,
    legacy_backup: Path,
    receipt_path: Optional[Path],
    receipt_backup: Optional[Path],
    *,
    stage_committed: bool,
    receipt_committed: bool,
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
    if receipt_path is not None and receipt_backup is not None:
        try:
            if receipt_committed and receipt_path.exists():
                receipt_path.unlink()
            if receipt_backup.exists():
                _replace_path(receipt_backup, receipt_path)
        except OSError as exc:
            failures.append(f"receipt: {exc}")
    return failures


def _install_transaction(
    plugin_root: Path,
    server_path: Path,
    *,
    receipt_path: Optional[Path],
    receipt_values: Optional[dict[str, Any]],
) -> tuple[Path, list[dict[str, Any]]]:
    source = Path(__file__).resolve().parent / "toolbag_plugin"
    target = plugin_root / PLUGIN_NAME
    legacy = plugin_root / LEGACY_PLUGIN_NAME
    token = uuid.uuid4().hex
    stage = plugin_root / f".{PLUGIN_NAME}.stage-{token}"
    target_backup = plugin_root / f".{PLUGIN_NAME}.backup-{token}"
    legacy_backup = plugin_root / f".{LEGACY_PLUGIN_NAME}.backup-{token}"
    receipt_stage: Optional[Path] = None
    receipt_backup: Optional[Path] = None
    stage_committed = False
    receipt_committed = False
    try:
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stage / "server_path.txt").write_text(str(server_path.resolve()), encoding="utf-8")
        if not (stage / "__main__.py").is_file() or not (stage / "_runtime.py").is_file():
            raise OSError("the staged Toolbag plugin is incomplete")
        if receipt_path is not None and receipt_values is not None:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_stage = receipt_path.with_name(f".{receipt_path.name}.stage-{token}")
            receipt_backup = receipt_path.with_name(f".{receipt_path.name}.backup-{token}")
            receipt_values["installed_files"] = _manifest(stage)
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
            _replace_path(target, target_backup)
        if legacy.exists():
            _replace_path(legacy, legacy_backup)
        _replace_path(stage, target)
        stage_committed = True
        if receipt_path is not None and receipt_stage is not None and receipt_backup is not None:
            if receipt_path.exists():
                _replace_path(receipt_path, receipt_backup)
            _replace_path(receipt_stage, receipt_path)
            receipt_committed = True
    except OSError as exc:
        rollback_failures = _rollback_replace(
            target,
            target_backup,
            legacy,
            legacy_backup,
            receipt_path,
            receipt_backup,
            stage_committed=stage_committed,
            receipt_committed=receipt_committed,
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
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                cleanup_failures.append(
                    {
                        "path": str(backup),
                        "reason": (
                            "windows_file_lock" if _is_windows_lock(exc) else "cleanup_failed"
                        ),
                        "message": str(exc),
                    }
                )
    if receipt_backup is not None and receipt_backup.exists():
        try:
            receipt_backup.unlink()
        except OSError as exc:
            cleanup_failures.append(
                {
                    "path": str(receipt_backup),
                    "reason": "windows_file_lock" if _is_windows_lock(exc) else "cleanup_failed",
                    "message": str(exc),
                }
            )
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
    files = receipt.get("installed_files")
    if not isinstance(files, list) or not files:
        return {"reason": "receipt_manifest_missing", "path": str(target)}
    for item in files:
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            return {"reason": "receipt_manifest_invalid", "path": str(target)}
        path = (target / str(item["path"])).resolve()
        try:
            path.relative_to(target)
        except ValueError:
            return {"reason": "receipt_manifest_unsafe", "path": str(path)}
        if not path.is_file():
            return {"reason": "installed_file_missing", "path": str(path)}
        if _sha256(path) != item["sha256"]:
            return {"reason": "installed_file_digest_mismatch", "path": str(path)}
    return None


def _verify_install(
    args: argparse.Namespace, receipt: dict[str, Any]
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    target = Path(receipt["target_path"]).resolve()
    if not target.is_dir():
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
    if not Path(configured_server).is_file():
        return False, "server_executable_missing", {"server_path": configured_server}
    manifest_failure = _validate_manifest(receipt, target)
    if manifest_failure:
        return False, manifest_failure["reason"], manifest_failure
    bootstrap_error = target / BOOTSTRAP_ERROR_NAME
    if bootstrap_error.is_file():
        try:
            diagnostic: Any = json.loads(bootstrap_error.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            diagnostic = {"path": str(bootstrap_error)}
        return False, "bootstrap_error", {"bootstrap": diagnostic}
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
        target = plugin_root / PLUGIN_NAME
        receipt_values = {
            "receipt_version": 1,
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
        )
        report["steps"][1]["status"] = "ok"
        report["steps"][2]["status"] = "ok"
        report["target_path"] = str(installed)
        if cleanup_failures:
            requires_restart = any(
                item["reason"] == "windows_file_lock" for item in cleanup_failures
            )
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
        target = Path(receipt["target_path"]).resolve()
        plugin_root = Path(receipt["plugin_root"]).resolve()
        if args.plugin_dir and args.plugin_dir.expanduser().resolve() != plugin_root:
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "plugin_dir_mismatch",
                "--plugin-dir does not match the receipt; uninstall will not inspect "
                "another folder.",
            )
        if not target.is_dir():
            raise LifecycleError(
                EXIT_PREFLIGHT,
                "preflight",
                "receipt_target_missing",
                f"The receipted plugin target is missing: {target}",
            )
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
        receipt_backup = receipt_path.with_name(f".{receipt_path.name}.uninstall-{token}")
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
        target_moved = False
        receipt_moved = False
        try:
            _replace_path(target, tombstone)
            target_moved = True
            _replace_path(receipt_path, receipt_backup)
            receipt_moved = True
            shutil.rmtree(tombstone)
        except OSError as exc:
            rollback_errors = []
            try:
                if target_moved:
                    shutil.rmtree(tombstone, ignore_errors=True)
                    if target.exists():
                        shutil.rmtree(target)
                    _replace_path(restore_copy, target)
                elif restore_copy.exists():
                    shutil.rmtree(restore_copy)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            try:
                if receipt_moved and receipt_backup.exists() and not receipt_path.exists():
                    _replace_path(receipt_backup, receipt_path)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                raise LifecycleError(
                    EXIT_INSTALL,
                    "rollback",
                    "rollback_failed",
                    "Uninstall failed and could not restore the receipted state.",
                    details={"error": str(exc), "rollback_errors": rollback_errors},
                ) from exc
            code = EXIT_REQUIRES_RESTART if _is_windows_lock(exc) else EXIT_INSTALL
            reason = "windows_file_lock" if code == EXIT_REQUIRES_RESTART else "uninstall_failed"
            raise LifecycleError(
                code,
                "uninstall",
                reason,
                f"Uninstall failed; the receipted state was restored: {exc}",
            ) from exc
        cleanup_failures = []
        for path, remove in (
            (receipt_backup, receipt_backup.unlink),
            (restore_copy, lambda: shutil.rmtree(restore_copy)),
        ):
            try:
                remove()
            except OSError as exc:
                cleanup_failures.append(
                    {
                        "path": str(path),
                        "reason": (
                            "windows_file_lock" if _is_windows_lock(exc) else "cleanup_failed"
                        ),
                        "message": str(exc),
                    }
                )
        if cleanup_failures:
            requires_restart = any(
                item["reason"] == "windows_file_lock" for item in cleanup_failures
            )
            _set_failure(
                report,
                stage="uninstall",
                reason=("windows_file_lock" if requires_restart else "uninstall_cleanup_failed"),
                message="The active plugin was removed, but generated rollback files remain.",
                status="requires_restart" if requires_restart else "partial",
                details={"pending_cleanup": cleanup_failures},
            )
            report["requires_restart"] = requires_restart
            report["install_state"] = "fresh"
            return report, EXIT_REQUIRES_RESTART if requires_restart else EXIT_INSTALL
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
    return _emit(args, report, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
