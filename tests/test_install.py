import json
import runpy
import sys
import types

import pytest

from dcc_mcp_marmoset import install, server


def test_install_copies_plugin_and_records_server_path(tmp_path, monkeypatch):
    environment = tmp_path / "environment"
    environment.mkdir()
    python = environment / "python.exe"
    server_name = "dcc-mcp-marmoset.exe" if install.sys.platform == "win32" else "dcc-mcp-marmoset"
    server = environment / server_name
    python.write_bytes(b"")
    server.write_bytes(b"")
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    monkeypatch.setattr(install.sys, "executable", str(python))

    target = install.install_plugin(plugin_dir)

    assert target.name == "DCC-MCP"
    assert (target / "__main__.py").is_file()
    assert (target / "_runtime.py").is_file()
    assert (target / "server_path.txt").read_text(encoding="utf-8") == str(server.resolve())


def test_install_refuses_to_overwrite_existing_plugin(tmp_path, monkeypatch):
    environment = tmp_path / "environment"
    environment.mkdir()
    python = environment / "python.exe"
    server_name = "dcc-mcp-marmoset.exe" if install.sys.platform == "win32" else "dcc-mcp-marmoset"
    (environment / server_name).write_bytes(b"")
    plugin_dir = tmp_path / "plugins"
    (plugin_dir / install.LEGACY_PLUGIN_NAME).mkdir(parents=True)
    monkeypatch.setattr(install.sys, "executable", str(python))

    with pytest.raises(FileExistsError):
        install.install_plugin(plugin_dir)


def test_overwrite_migrates_legacy_menu_folder(tmp_path, monkeypatch):
    environment = tmp_path / "environment"
    environment.mkdir()
    python = environment / "python.exe"
    server_name = "dcc-mcp-marmoset.exe" if install.sys.platform == "win32" else "dcc-mcp-marmoset"
    (environment / server_name).write_bytes(b"")
    plugin_dir = tmp_path / "plugins"
    legacy = plugin_dir / install.LEGACY_PLUGIN_NAME
    legacy.mkdir(parents=True)
    monkeypatch.setattr(install.sys, "executable", str(python))

    target = install.install_plugin(plugin_dir, overwrite=True)

    assert target.name == "DCC-MCP"
    assert target.is_dir()
    assert not legacy.exists()


def test_standard_install_plan_requests_the_exact_plugin_dir(tmp_path, capsys):
    receipt_path = tmp_path / "receipt.json"

    with pytest.raises(SystemExit) as raised:
        server.main(
            [
                "install",
                "--json",
                "--dry-run",
                "--dcc-path",
                str(tmp_path / "Marmoset Toolbag"),
                "--python",
                sys.executable,
                "--receipt-path",
                str(receipt_path),
            ]
        )

    assert raised.value.code == install.EXIT_PREFLIGHT
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["command"] == "install"
    assert report["dcc_type"] == "marmoset"
    assert report["directly_usable"] is False
    assert report["failure_stage"] == "preflight"
    assert report["failure_reason"] == "plugin_dir_required"
    assert report["receipt_path"] == str(receipt_path.resolve())
    assert len(report["next_steps"]) == 1
    assert report["next_steps"][0]["command"][-2:] == [
        "--plugin-dir",
        "<PATH_FROM_TOOLBAG_UI>",
    ]


def _standard_args(tmp_path, command, *extra):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(exist_ok=True)
    dcc_path = tmp_path / "toolbag"
    dcc_path.write_bytes(b"")
    receipt_path = tmp_path / "marmoset.json"
    return [
        command,
        "--json",
        "--dcc-path",
        str(dcc_path),
        "--toolbag-version",
        "5.02",
        "--python",
        sys.executable,
        "--plugin-dir",
        str(plugin_dir),
        "--receipt-path",
        str(receipt_path),
        *extra,
    ]


def test_install_sop_contract_and_exit_codes_are_stable():
    schema = install.load_install_sop_schema()

    assert set(schema["required"]) == {
        "schema_version",
        "status",
        "dcc_type",
        "adapter_version",
        "core_version",
        "steps",
        "next_steps",
        "receipt_path",
        "verify",
    }
    assert schema["properties"]["schema_version"] == {"const": 1, "type": "integer"}
    assert [
        install.EXIT_OK,
        install.EXIT_PREFLIGHT,
        install.EXIT_ACQUIRE,
        install.EXIT_INSTALL,
        install.EXIT_VERIFY,
        install.EXIT_REQUIRES_RESTART,
    ] == [0, 10, 20, 30, 40, 50]


def test_every_lifecycle_verb_emits_the_required_schema_fields(tmp_path, capsys):
    required = set(install.load_install_sop_schema()["required"])

    for command in sorted(install.LIFECYCLE_COMMANDS):
        receipt = tmp_path / f"{command}.json"
        exit_code = install.main([command, "--json", "--dry-run", "--receipt-path", str(receipt)])
        assert exit_code in {install.EXIT_OK, install.EXIT_PREFLIGHT}
        report = json.loads(capsys.readouterr().out)
        assert required <= set(report)
        assert report["command"] == command
        assert report["schema_version"] == 1


def test_install_dry_run_is_a_complete_non_mutating_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)

    exit_code = install.main(_standard_args(tmp_path, "install", "--dry-run"))

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "planned"
    assert report["plan_type"] == "fresh"
    assert [step["id"] for step in report["steps"]] == [
        "preflight",
        "stage",
        "commit",
        "verify",
    ]
    assert "--yes" in report["next_steps"][0]["command"]
    assert not (tmp_path / "plugins" / install.PLUGIN_NAME).exists()
    assert not (tmp_path / "marmoset.json").exists()


def test_unreceipted_partial_state_plans_repair_but_uninstall_refuses_it(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    legacy = tmp_path / "plugins" / install.LEGACY_PLUGIN_NAME
    legacy.mkdir(parents=True)

    assert install.main(_standard_args(tmp_path, "status")) == install.EXIT_PREFLIGHT
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "partial"

    assert install.main(_standard_args(tmp_path, "install", "--dry-run")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["install_state"] == "partial"
    assert plan["plan_type"] == "repair"

    assert install.main(_standard_args(tmp_path, "uninstall", "--yes")) == install.EXIT_PREFLIGHT
    uninstall = json.loads(capsys.readouterr().out)
    assert uninstall["failure_reason"] == "unreceipted_plugin"
    assert legacy.is_dir()


def test_receipt_round_trip_is_convergent_and_uninstall_is_idempotent(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )
    args = _standard_args(tmp_path, "install", "--yes")

    assert install.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["directly_usable"] is True
    target = tmp_path / "plugins" / install.PLUGIN_NAME
    receipt_path = tmp_path / "marmoset.json"
    assert target.is_dir()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["target_path"] == str(target.resolve())
    assert receipt["installed_files"]

    assert install.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["install_state"] == "installed"
    assert not list((tmp_path / "plugins").glob(".*.backup-*"))

    uninstall = _standard_args(tmp_path, "uninstall", "--yes")
    assert install.main(uninstall) == 0
    assert json.loads(capsys.readouterr().out)["install_state"] == "fresh"
    assert not target.exists()
    assert not receipt_path.exists()

    assert install.main(uninstall) == 0
    assert json.loads(capsys.readouterr().out)["steps"][0]["status"] == "skipped"


def test_upgrade_requires_a_receipt_and_then_uses_the_transaction(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )

    assert install.main(_standard_args(tmp_path, "upgrade", "--dry-run")) == install.EXIT_PREFLIGHT
    assert json.loads(capsys.readouterr().out)["failure_reason"] == "receipt_missing"
    assert install.main(_standard_args(tmp_path, "install", "--yes")) == 0
    capsys.readouterr()

    assert install.main(_standard_args(tmp_path, "upgrade", "--dry-run")) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["plan_type"] == "upgrade"
    assert plan["status"] == "planned"


def test_receipt_commit_failure_restores_previous_plugin_and_receipt(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )
    args = _standard_args(tmp_path, "install", "--yes")
    assert install.main(args) == 0
    capsys.readouterr()
    target = tmp_path / "plugins" / install.PLUGIN_NAME
    marker = target / "previous-state.txt"
    marker.write_text("keep me", encoding="utf-8")
    receipt_path = tmp_path / "marmoset.json"
    previous_receipt = receipt_path.read_bytes()
    original_replace = install._replace_path

    def fail_receipt_commit(source, destination):
        if destination == receipt_path.resolve() and ".stage-" in source.name:
            raise OSError("injected receipt commit failure")
        original_replace(source, destination)

    monkeypatch.setattr(install, "_replace_path", fail_receipt_commit)

    assert install.main(args) == install.EXIT_INSTALL
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == "commit_failed"
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert receipt_path.read_bytes() == previous_receipt


def test_uninstall_delete_failure_restores_a_complete_receipted_plugin(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )
    assert install.main(_standard_args(tmp_path, "install", "--yes")) == 0
    capsys.readouterr()
    target = tmp_path / "plugins" / install.PLUGIN_NAME
    receipt_path = tmp_path / "marmoset.json"
    previous_receipt = receipt_path.read_bytes()
    original_rmtree = install.shutil.rmtree

    def fail_tombstone_delete(path, *args, **kwargs):
        candidate = install.Path(path)
        if ".uninstall-" in candidate.name and not kwargs.get("ignore_errors"):
            (candidate / "__main__.py").unlink()
            raise OSError("injected partial uninstall failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(install.shutil, "rmtree", fail_tombstone_delete)

    assert install.main(_standard_args(tmp_path, "uninstall", "--yes")) == install.EXIT_INSTALL
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == "uninstall_failed"
    assert (target / "__main__.py").is_file()
    assert receipt_path.read_bytes() == previous_receipt


def test_windows_lock_preserves_previous_plugin_and_requires_restart(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugins"
    target = plugin_root / install.PLUGIN_NAME
    target.mkdir(parents=True)
    marker = target / "previous-state.txt"
    marker.write_text("keep me", encoding="utf-8")
    server = tmp_path / "dcc-mcp-marmoset.exe"
    server.write_bytes(b"")
    original_replace = install._replace_path

    def locked_replace(source, destination):
        if source == target:
            raise PermissionError("injected Toolbag lock")
        original_replace(source, destination)

    monkeypatch.setattr(install, "_replace_path", locked_replace)
    monkeypatch.setattr(install, "_is_windows_lock", lambda _exc: True)

    with pytest.raises(install.LifecycleError) as raised:
        install._install_transaction(
            plugin_root,
            server,
            receipt_path=None,
            receipt_values=None,
        )

    assert raised.value.exit_code == install.EXIT_REQUIRES_RESTART
    assert raised.value.reason == "windows_file_lock"
    assert marker.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize(
    ("version", "supported"),
    [("4.02", False), ("4.03", True), ("4.10", True), ("5.0", True), ("6.0", False)],
)
def test_toolbag_version_matrix_is_explicit(tmp_path, monkeypatch, version, supported):
    dcc_path = tmp_path / "toolbag"
    dcc_path.write_bytes(b"")
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)

    if supported:
        assert install._inspect_toolbag(dcc_path, version)["version"] == version
    else:
        with pytest.raises(install.LifecycleError) as raised:
            install._inspect_toolbag(dcc_path, version)
        assert raised.value.reason == "unsupported_toolbag_version"


def test_verify_reports_a_stale_server_path_exactly(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )
    assert install.main(_standard_args(tmp_path, "install", "--yes")) == 0
    capsys.readouterr()
    monkeypatch.undo()
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    server_file = tmp_path / "plugins" / install.PLUGIN_NAME / "server_path.txt"
    server_file.write_text(str(tmp_path / "stale-server"), encoding="utf-8")

    assert install.main(_standard_args(tmp_path, "verify")) == install.EXIT_VERIFY
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == "server_path_stale"
    assert report["verification"]["expected"] != report["verification"]["actual"]


def test_verify_refuses_interpreter_drift_from_the_receipt(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )
    assert install.main(_standard_args(tmp_path, "install", "--yes")) == 0
    capsys.readouterr()
    other_python = tmp_path / "other-python"
    other_python.write_bytes(b"")
    verify = _standard_args(tmp_path, "verify")
    verify[verify.index("--python") + 1] = str(other_python)

    assert install.main(verify) == install.EXIT_PREFLIGHT
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == "python_mismatch"


def test_toolbag_bootstrap_failure_remains_machine_readable(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "DCC-MCP"
    plugin_dir.mkdir()
    errors = []
    fake_mset = types.SimpleNamespace(
        getPluginPath=lambda: str(plugin_dir / "__main__.py"), errors=errors
    )
    fake_mset.err = errors.append

    def fail_start(_mset, _plugin_dir):
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setitem(sys.modules, "mset", fake_mset)
    monkeypatch.setitem(sys.modules, "_runtime", types.SimpleNamespace(start_runtime=fail_start))
    entrypoint = install.Path(install.__file__).resolve().parent / "toolbag_plugin" / "__main__.py"

    with pytest.raises(RuntimeError, match="injected bootstrap failure"):
        runpy.run_path(str(entrypoint), run_name="__main__")

    diagnostic = json.loads((plugin_dir / install.BOOTSTRAP_ERROR_NAME).read_text(encoding="utf-8"))
    assert diagnostic["stage"] == "toolbag_plugin_bootstrap"
    assert diagnostic["error_class"] == "RuntimeError"
    assert "injected bootstrap failure" in diagnostic["message"]
    assert errors and "diagnostic:" in errors[-1]


def test_install_documentation_covers_the_public_lifecycle():
    root = install.Path(install.__file__).resolve().parents[2]
    guide = (root / "install.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    for heading in ("## Install", "## Status", "## Verify", "## Upgrade", "## Uninstall"):
        assert heading in guide
    for platform in ("Windows", "macOS", "Linux"):
        assert platform in guide
    for token in (
        "--json",
        "--yes",
        "--dry-run",
        "--dcc-path",
        "--python",
        "bootstrap-error.json",
        "marmoset_scene__ping",
        "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-marmoset/main/install.md",
    ):
        assert token in guide
    assert "dcc-mcp-marmoset install --json --dry-run" in readme
    assert "dcc-mcp-marmoset verify --json" in readme
