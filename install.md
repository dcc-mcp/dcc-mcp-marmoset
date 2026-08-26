# Install DCC-MCP Marmoset

This is the canonical, agent-first Install SOP for the Marmoset Toolbag adapter.
Its raw instructions URL is:

```text
https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-marmoset/main/install.md
```

## Requirements and supported versions

- Marmoset Toolbag **4.03 through 4.x**, or **5.x**.
- An external Python **3.9+** environment that owns `dcc-mcp-marmoset` and
  `dcc-mcp-core>=0.20.14,<1.0.0`. Toolbag does not need those packages in its
  embedded Python.
- Permission to write Toolbag's per-user plugin folder and
  `~/.dcc-mcp/receipts/marmoset.json`.

Install or update the published wheel first:

```bash
python -m pip install --upgrade dcc-mcp-marmoset
```

The wheel ships the pure-Python Toolbag bootstrap. The installer never downloads
Toolbag and never guesses a vendor binary, plugin folder, or interpreter.

## Resolve the exact host inputs

1. In Toolbag choose **Edit > Plugins > Show User Plugin Folder** and copy the
   displayed absolute folder. This is the value for `--plugin-dir`.
2. Resolve the exact Toolbag executable or macOS application for `--dcc-path`.
3. Resolve the exact external interpreter that owns the adapter for `--python`.

Windows example:

```powershell
$toolbag = "C:\absolute\path\to\toolbag.exe"
$python = (Get-Command python).Source
$plugins = "C:\path\copied\from\Toolbag"
```

macOS example:

```bash
toolbag="/Applications/Marmoset Toolbag 5.app"
python="$(command -v python3)"
plugins="/path/copied/from/Toolbag"
```

Linux/managed-host example:

```bash
toolbag="/absolute/vendor-provided/path/to/toolbag"
python="$(command -v python3)"
plugins="/path/copied/from/Toolbag"
```

Toolbag availability is controlled by Marmoset's platform distribution. The
adapter lifecycle is cross-platform, but it cannot make an unsupported host
distribution directly usable.

Windows executable version resources and macOS `Info.plist` are used when
available. On a managed layout without reliable host metadata, pass the exact
installed value, for example `--toolbag-version 5.02`; this is explicit operator
evidence, not path-name inference.

## Install

First request a non-mutating plan. Omitting `--yes` is also plan-only.

```bash
dcc-mcp-marmoset install --json --dry-run \
  --dcc-path "$toolbag" \
  --python "$python" \
  --plugin-dir "$plugins"
```

Review the resolved host, interpreter, version, state (`fresh`, `partial`, or
`installed`), plan type (`fresh`, `repair`, or `upgrade`), receipt path, and
ordered steps. Then execute the same validated plan:

```bash
dcc-mcp-marmoset install --json --yes \
  --dcc-path "$toolbag" \
  --python "$python" \
  --plugin-dir "$plugins"
```

The installer copies a complete stage beside `DCC-MCP`, validates it, moves the
previous plugin and receipt aside, commits the staged pair, and only then removes
the backup. A commit or receipt failure restores the previous plugin and receipt.
It never performs delete-then-copy replacement.

In Toolbag choose **Edit > Plugins > Refresh**, then launch
**Edit > Plugins > DCC-MCP**. Keep the compact DCC-MCP status window open;
Toolbag only keeps plugin callbacks alive while that window exists.

## Status

Status is read-only and can use the default receipt without repeating host paths:

```bash
dcc-mcp-marmoset status --json
```

- `fresh`: neither a receipt nor a known plugin target was supplied.
- `installed`: receipt and plugin target agree.
- `upgrade`: the receipt records another adapter version.
- `partial`: an unreceipted target, missing receipted target, or invalid receipt
  needs an explicit repair; no user-owned path is silently removed.

## Verify to usable

```bash
dcc-mcp-marmoset verify --json
```

Verification checks, in order:

1. receipt ownership and exact plugin target;
2. recorded file SHA-256 digests and `server_path.txt`;
3. adapter and Core import from the recorded external `--python`;
4. fail-visible Toolbag bootstrap diagnostics; and
5. Core sidecar readiness plus the typed `marmoset_scene__ping` tool.

Exit `0` with `verify.directly_usable=true` means a live Toolbag instance is
callable. Installed files alone are not readiness. If Toolbag is closed, the
plugin window is closed, or the bridge is not registered, verify returns exit
`40` and one exact start-and-verify next step.

For additional runtime inspection:

```bash
dcc-mcp-cli list
dcc-mcp-cli search --query "Toolbag bridge ping" --dcc-type marmoset
dcc-mcp-cli load-skill marmoset-scene --dcc-type marmoset
```

## Upgrade

Upgrade requires an existing receipt and uses the same transaction and rollback
contract:

```bash
python -m pip install --upgrade dcc-mcp-marmoset
dcc-mcp-marmoset upgrade --json --dry-run
dcc-mcp-marmoset upgrade --json --yes
dcc-mcp-marmoset verify --json
```

Pass explicit host/interpreter/plugin paths again if the environment moved. A
mismatch with the receipt fails preflight instead of updating a different Toolbag
profile.

## Uninstall

Uninstall consumes only the receipt. It never searches for or deletes an
unreceipted directory.

```bash
dcc-mcp-marmoset uninstall --json --dry-run
dcc-mcp-marmoset uninstall --json --yes
```

Running uninstall again is an idempotent exit `0`. Uninstall removes the Toolbag
plugin and receipt; it does not close Toolbag, remove Toolbag, or uninstall the
external Python wheel. Remove the wheel separately only after all profiles are
uninstalled:

```bash
python -m pip uninstall dcc-mcp-marmoset
```

## JSON result and exit codes

Every verb accepts the uniform flags `--json`, `--yes`, `--dry-run`,
`--dcc-path`, and `--python`, and emits Install SOP schema version 1. Every
`next_steps` entry contains one argv array, never a shell-joined command.

| Exit | Meaning |
|---:|---|
| `0` | Plan or operation completed with the reported expected/usable state. |
| `10` | Host, version, interpreter, permission, receipt, or partial-state preflight failed. |
| `20` | Reserved for pinned acquisition or integrity failure. |
| `30` | Install, uninstall, receipt commit, or rollback failed. |
| `40` | Artifacts exist, but verify-to-usable failed. |
| `50` | A proven Windows file lock requires Toolbag restart and deferred cleanup. |

Exit `50` is never inferred from a closed host. Save work and restart Toolbag
only when the result reports a locked path or pending cleanup.

## Troubleshooting

- `plugin_dir_required`: copy the exact folder from
  **Edit > Plugins > Show User Plugin Folder**. The returned JSON contains one
  structured command template; no UI automation is attempted.
- `dcc_path_missing` or `host_version_unavailable`: pass the real host path and,
  only when binary metadata is unavailable, its exact `--toolbag-version`.
- `unsupported_toolbag_version`: use Toolbag 4.03+ in the 4.x line or Toolbag
  5.x. Toolbag 4.02 and unverified future major lines fail closed.
- `target_import_failed` or `server_executable_missing`: install the wheel into
  the exact interpreter passed to `--python`, then repeat the plan.
- `server_path_stale`: the plugin points at a different or removed adapter
  executable. Run a receipted repair/upgrade from the owning interpreter.
- `bootstrap_error`: inspect `DCC-MCP/bootstrap-error.json`. It records UTC
  timestamp, bootstrap stage, error class, message, and traceback while the same
  failure remains visible in Toolbag's native error UI.
- `sidecar_not_ready`: refresh and launch DCC-MCP in Toolbag, keep its window
  open, then repeat `verify`.
- `windows_file_lock`: save work, close/restart only the reported Toolbag host,
  then repeat the exact structured command.

The old `dcc-mcp-marmoset-install --plugin-dir ... --overwrite` entry point is
retained for compatibility and prints a deprecation warning. New automation must
use the standard lifecycle commands so it receives receipts, JSON, rollback,
version preflight, and verify-to-usable results.
