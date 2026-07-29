# Install DCC-MCP Marmoset

## Requirements

- Marmoset Toolbag 4.03+ or 5.x.
- Python 3.9+ outside Toolbag.
- `dcc-mcp-cli` 0.19.86+ on `PATH` for agent control.

## Install or update

1. Close any running **DCC-MCP Marmoset** plugin window.
2. Install the adapter:

   ```powershell
   python -m pip install --upgrade dcc-mcp-marmoset
   ```

3. In Toolbag choose **Edit > Plugins > Show User Plugin Folder**.
4. Copy the folder path and install the bundled pure-Python plugin:

   ```powershell
   dcc-mcp-marmoset-install --plugin-dir "C:\path\shown\by\Toolbag" --overwrite
   ```

5. In Toolbag choose **Edit > Plugins > Refresh**, then launch
   **dcc_mcp_marmoset**.

The installer records the absolute server executable in the copied plugin.
Set `DCC_MCP_MARMOSET_SERVER` before launching Toolbag only when a managed
deployment needs to override that executable.

## Verify

```powershell
dcc-mcp-cli list
dcc-mcp-cli search --query "Toolbag bridge ping" --dcc-type marmoset
dcc-mcp-cli load-skill marmoset-scene --dcc-type marmoset
dcc-mcp-cli search --query "Toolbag bridge ping" --dcc-type marmoset
dcc-mcp-cli describe <returned-tool-slug>
dcc-mcp-cli call <returned-tool-slug> --json '{}'
```

A healthy instance reports the Toolbag process as its bound host. Toolbag only
keeps plugin callbacks alive while a plugin `UIWindow` remains visible, so one
compact `DCC-MCP` lifetime window stays open. Closing it or Toolbag stops the
adapter child process; stale rows are not valid call targets.

## Troubleshooting

- If the plugin reports a missing server, reinstall it from the same Python
  environment that provides `dcc-mcp-marmoset`.
- If `list` shows no instance, inspect the log path displayed by the plugin and
  run `dcc-mcp-cli doctor`.
- Do not fall back to arbitrary Toolbag scripting. Fix the typed bridge/tool or
  add a focused typed tool for repeatable workflows.
