"""Install the pure-Python Toolbag plugin into the user-selected plugin folder."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PLUGIN_NAME = "DCC-MCP"
LEGACY_PLUGIN_NAME = "dcc_mcp_marmoset"


def install_plugin(plugin_dir: Path, *, overwrite: bool = False) -> Path:
    """Copy the bundled plugin and bind it to this environment's server executable."""
    root = plugin_dir.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Toolbag user plugin folder does not exist: {root}")

    source = Path(__file__).resolve().parent / "toolbag_plugin"
    target = root / PLUGIN_NAME
    existing = [path for path in (target, root / LEGACY_PLUGIN_NAME) if path.exists()]
    if existing:
        if not overwrite:
            raise FileExistsError(f"Plugin already exists: {existing[0]}")
        for path in existing:
            shutil.rmtree(path)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    scripts_dir = Path(sys.executable).resolve().parent
    executable = scripts_dir / (
        "dcc-mcp-marmoset.exe" if sys.platform == "win32" else "dcc-mcp-marmoset"
    )
    if not executable.is_file():
        executable = Path(shutil.which("dcc-mcp-marmoset") or "")
    if not executable.is_file():
        shutil.rmtree(target)
        raise RuntimeError("dcc-mcp-marmoset executable was not found in this Python environment")
    (target / "server_path.txt").write_text(str(executable.resolve()), encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Install the DCC-MCP plugin for Toolbag.")
    parser.add_argument(
        "--plugin-dir",
        type=Path,
        required=True,
        help="Folder opened by Toolbag: Edit > Plugins > Show User Plugin Folder.",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    print(install_plugin(args.plugin_dir, overwrite=args.overwrite))


if __name__ == "__main__":
    main()
