import hashlib
import json
import os
import runpy
import subprocess
import sys
import types
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from dcc_mcp_marmoset import install, server


def _assert_install_report_schema(report):
    Draft202012Validator(install.load_install_sop_schema()).validate(report)


def _install_receipted_fixture(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )
    args = _standard_args(tmp_path, "install", "--yes")
    assert install.main(args) == install.EXIT_OK
    capsys.readouterr()
    return (
        tmp_path / "plugins" / install.PLUGIN_NAME,
        tmp_path / "marmoset.json",
    )


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


def test_report_schema_version_ignores_cores_artifact_revision(tmp_path, capsys):
    """A report carries the document version, never Core's artifact revision.

    Core repurposed ``INSTALL_SOP_SCHEMA_VERSION`` into the schema artifact
    revision (the ``-vN`` file suffix) while the schema keeps pinning the report
    document's ``schema_version`` to a constant. Conflating the two once made
    every install fail preflight with ``install_schema_mismatch``.
    """

    from dcc_mcp_core.deployment import INSTALL_SOP_SCHEMA_VERSION

    assert install.INSTALL_SOP_DOCUMENT_SCHEMA_VERSION == 1
    assert (
        install.INSTALL_SOP_DOCUMENT_SCHEMA_VERSION
        == (install.load_install_sop_schema()["properties"]["schema_version"]["const"])
    )
    # Core only separates the artifact revision from the document version from
    # 0.20.34 on, and the declared floor is 0.20.14; below that both are 1 and a
    # report has nothing to get wrong, so only assert the two apart when they
    # are actually distinguishable.
    if INSTALL_SOP_SCHEMA_VERSION != install.INSTALL_SOP_DOCUMENT_SCHEMA_VERSION:
        install.main(
            ["install", "--json", "--dry-run", "--receipt-path", str(tmp_path / "guard.json")]
        )
        report = json.loads(capsys.readouterr().out)
        assert report["schema_version"] == install.INSTALL_SOP_DOCUMENT_SCHEMA_VERSION
        assert report["schema_version"] != INSTALL_SOP_SCHEMA_VERSION


def test_interpreter_probe_reports_the_artifact_revision_not_the_document_field():
    """The probe compares artifact revisions, so it must not emit the document's.

    ``_INTERPRETER_PROBE`` runs inside the target interpreter and ``_probe_python``
    validates its payload. Sending the document version there made the drift check
    compare the artifact revision against the document version and fail preflight.
    """

    from dcc_mcp_core.deployment import INSTALL_SOP_SCHEMA_VERSION

    completed = subprocess.run(
        [sys.executable, "-c", install._INTERPRETER_PROBE],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["schema_artifact_revision"] == INSTALL_SOP_SCHEMA_VERSION
    assert install._probe_python(Path(sys.executable), failure_code=install.EXIT_PREFLIGHT)


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
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["installed_files"].append(
        {"path": marker.name, "sha256": hashlib.sha256(marker.read_bytes()).hexdigest()}
    )
    receipt["installed_files"].sort(key=lambda item: item["path"].casefold())
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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


def test_status_rejects_foreign_files_in_a_receipted_plugin(tmp_path, capsys, monkeypatch):
    target, receipt_path = _install_receipted_fixture(tmp_path, capsys, monkeypatch)
    (target / "user-owned.txt").write_text("not installed by the adapter", encoding="utf-8")

    assert install.main(["status", "--json", "--receipt-path", str(receipt_path)]) == 10
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "partial"
    assert report["failure_reason"] == "installed_file_set_mismatch"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "receipt_manifest_missing"),
        ("stale", "installed_file_digest_mismatch"),
    ],
)
def test_status_never_reports_installed_for_missing_or_stale_manifest(
    tmp_path, capsys, monkeypatch, mutation, reason
):
    _target, receipt_path = _install_receipted_fixture(tmp_path, capsys, monkeypatch)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        receipt.pop("installed_files")
    else:
        receipt["installed_files"][0]["sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert install.main(["status", "--json", "--receipt-path", str(receipt_path)]) == 10
    report = json.loads(capsys.readouterr().out)
    _assert_install_report_schema(report)
    assert report["status"] != "ok"
    assert report.get("install_state") != "installed"
    assert report["failure_reason"] == reason


def test_uninstall_refuses_stale_receipt_and_preserves_foreign_files(tmp_path, capsys, monkeypatch):
    target, receipt_path = _install_receipted_fixture(tmp_path, capsys, monkeypatch)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["installed_files"][0]["sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    foreign = target / "user-owned.txt"
    foreign.write_text("not installed by the adapter", encoding="utf-8")

    assert (
        install.main(["uninstall", "--json", "--yes", "--receipt-path", str(receipt_path)])
        == install.EXIT_PREFLIGHT
    )
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] in {
        "installed_file_digest_mismatch",
        "installed_file_set_mismatch",
    }
    assert target.is_dir()
    assert foreign.read_text(encoding="utf-8") == "not installed by the adapter"
    assert receipt_path.is_file()


def test_receipt_rejects_duplicate_manifest_aliases(tmp_path, capsys, monkeypatch):
    target, receipt_path = _install_receipted_fixture(tmp_path, capsys, monkeypatch)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    duplicate = dict(receipt["installed_files"][0])
    duplicate["path"] = duplicate["path"].upper()
    receipt["installed_files"].append(duplicate)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert install.main(["status", "--json", "--receipt-path", str(receipt_path)]) == 10
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == "receipt_manifest_duplicate"
    assert target.is_dir()


def test_receipt_rejects_symlinked_plugin_entries(tmp_path, capsys, monkeypatch):
    target, receipt_path = _install_receipted_fixture(tmp_path, capsys, monkeypatch)
    external = tmp_path / "external.txt"
    external.write_text("foreign", encoding="utf-8")
    os.symlink(external, target / "linked.txt")

    assert install.main(["status", "--json", "--receipt-path", str(receipt_path)]) == 10
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == "installed_path_unsafe"
    assert external.read_text(encoding="utf-8") == "foreign"


def test_uninstall_rechecks_target_identity_immediately_before_move(tmp_path, capsys, monkeypatch):
    target, receipt_path = _install_receipted_fixture(tmp_path, capsys, monkeypatch)
    original_validate = install._validate_manifest
    original_target = target.with_name("DCC-MCP-original")
    foreign = target / "foreign.txt"

    def swap_after_validation(receipt, validated_target):
        failure = original_validate(receipt, validated_target)
        if failure is None and not original_target.exists():
            os.replace(target, original_target)
            target.mkdir()
            foreign.write_text("foreign", encoding="utf-8")
        return failure

    monkeypatch.setattr(install, "_validate_manifest", swap_after_validation)

    assert (
        install.main(["uninstall", "--json", "--yes", "--receipt-path", str(receipt_path)])
        == install.EXIT_PREFLIGHT
    )
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == "install_identity_changed"
    assert foreign.read_text(encoding="utf-8") == "foreign"
    assert original_target.is_dir()


def test_uninstall_rechecks_owned_file_content_at_mutation_boundary(tmp_path, capsys, monkeypatch):
    target, receipt_path = _install_receipted_fixture(tmp_path, capsys, monkeypatch)
    entrypoint = target / "__main__.py"
    original_copytree = install.shutil.copytree

    def mutate_after_rollback_copy(source, destination, *args, **kwargs):
        result = original_copytree(source, destination, *args, **kwargs)
        candidate = install.Path(destination)
        if install.Path(source) == target and ".restore-" in candidate.name:
            entrypoint.write_text("USER_FOREIGN_DATA", encoding="utf-8")
        return result

    monkeypatch.setattr(install.shutil, "copytree", mutate_after_rollback_copy)

    assert (
        install.main(["uninstall", "--json", "--yes", "--receipt-path", str(receipt_path)])
        == install.EXIT_PREFLIGHT
    )
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == "installed_file_digest_mismatch"
    assert entrypoint.read_text(encoding="utf-8") == "USER_FOREIGN_DATA"
    assert receipt_path.is_file()
    assert not list(target.parent.glob(".DCC-MCP.uninstall-*"))
    assert not list(target.parent.glob(".DCC-MCP.restore-*"))


def test_windows_cleanup_deferral_is_persisted_and_converges_on_retry(
    tmp_path, capsys, monkeypatch
):
    plugin_root = tmp_path / "plugins"
    target = plugin_root / install.PLUGIN_NAME
    target.mkdir(parents=True)
    (target / "old.py").write_text("old", encoding="utf-8")
    locked = True
    original_rmtree = install.shutil.rmtree
    core_safe_remove = __import__(
        "dcc_mcp_core.deployment", fromlist=["safe_remove_tree"]
    ).safe_remove_tree

    def legacy_remove(path, *args, **kwargs):
        candidate = Path(path)
        if locked and candidate.parent == plugin_root and ".backup-" in candidate.name:
            raise PermissionError("simulated Toolbag lock")
        return original_rmtree(path, *args, **kwargs)

    def safe_remove(path):
        candidate = Path(path)
        if locked and candidate.parent == plugin_root and ".backup-" in candidate.name:
            return {
                "success": False,
                "status": "requires_restart",
                "requires_restart": True,
                "reason": "windows_file_lock",
                "path": str(candidate),
                "locked_path": str(candidate / "__main__.py"),
            }
        return core_safe_remove(candidate)

    monkeypatch.setattr(install.shutil, "rmtree", legacy_remove)
    monkeypatch.setattr(install, "_safe_remove_tree", safe_remove, raising=False)
    monkeypatch.setattr(install, "_is_windows_lock", lambda _exc: True)
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )
    args = _standard_args(tmp_path, "install", "--yes")

    assert install.main(args) == install.EXIT_REQUIRES_RESTART
    first = json.loads(capsys.readouterr().out)
    assert first["failure_reason"] == "windows_file_lock"
    receipt_path = tmp_path / "marmoset.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["pending_cleanup"]
    orphan = Path(receipt["pending_cleanup"][0]["path"])
    assert orphan.is_dir()

    locked = False
    assert install.main(args) == install.EXIT_OK
    capsys.readouterr()
    assert not orphan.exists()
    assert not list(plugin_root.glob(".*.backup-*"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt.get("pending_cleanup") == []


def test_receipt_replace_does_not_leave_an_untracked_locked_backup(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    monkeypatch.setattr(
        install,
        "_verify_install",
        lambda _args, _receipt: (True, "ok", {"readiness": {"success": True}}),
    )
    args = _standard_args(tmp_path, "install", "--yes")
    assert install.main(args) == install.EXIT_OK
    capsys.readouterr()

    receipt_path = tmp_path / "marmoset.json"
    original_unlink = install.os.unlink

    def deny_receipt_backup_removal(path, *args, **kwargs):
        candidate = install.Path(path)
        if candidate.name.startswith(f".{receipt_path.name}.backup-"):
            raise PermissionError("simulated locked receipt backup")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(install.os, "unlink", deny_receipt_backup_removal)

    assert install.main(args) == install.EXIT_OK
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "ok"
    assert not list(tmp_path.glob(".marmoset.json.backup-*"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["pending_cleanup"] == []

    assert install.main(args) == install.EXIT_OK
    capsys.readouterr()
    assert not list(tmp_path.glob(".marmoset.json.backup-*"))


def test_probe_wrong_shape_returns_stable_json(tmp_path, capsys, monkeypatch):
    completed = types.SimpleNamespace(returncode=0, stdout="{}\n", stderr="")
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: completed)
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)

    assert install.main(_standard_args(tmp_path, "install", "--dry-run")) == 10
    report = json.loads(capsys.readouterr().out)
    _assert_install_report_schema(report)
    assert report["failure_reason"] == "python_probe_invalid"


def test_probe_failure_redacts_credentials_and_local_paths(tmp_path, capsys, monkeypatch):
    completed = types.SimpleNamespace(
        returncode=1,
        stdout="",
        stderr=r"TOKEN=review-secret C:\private\workspace\adapter.py",
    )
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: completed)
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)

    assert install.main(_standard_args(tmp_path, "install", "--dry-run")) == 10
    report = json.loads(capsys.readouterr().out)
    _assert_install_report_schema(report)
    rendered = json.dumps(report)
    assert "review-secret" not in rendered
    assert "workspace" not in rendered
    assert "adapter.py" not in rendered
    assert len(report["message"]) <= install.MAX_PUBLIC_MESSAGE_CHARS


@pytest.mark.parametrize(
    ("adapter_version", "core_version", "reason"),
    [
        ("0.0.1", "0.20.19", "adapter_version_mismatch"),
        (install.__version__, "0.1.0", "core_version_unsupported"),
    ],
)
def test_preflight_binds_current_adapter_and_core_floor(
    tmp_path, capsys, monkeypatch, adapter_version, core_version, reason
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    executable = scripts / (
        "dcc-mcp-marmoset.exe" if install.os.name == "nt" else "dcc-mcp-marmoset"
    )
    executable.write_bytes(b"")
    completed = types.SimpleNamespace(
        returncode=0,
        stderr="",
        stdout=json.dumps(
            {
                "distribution": "dcc-mcp-marmoset",
                "adapter_version": adapter_version,
                "core_version": core_version,
                "adapter_path": str(Path(install.__file__).resolve()),
                "core_path": str(tmp_path / "dcc_mcp_core" / "__init__.py"),
                "scripts": str(scripts),
            }
        )
        + "\n",
    )
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: completed)
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)

    assert install.main(_standard_args(tmp_path, "install", "--dry-run")) == 10
    report = json.loads(capsys.readouterr().out)
    assert report["failure_reason"] == reason


def test_shared_schema_is_the_only_install_contract():
    from dcc_mcp_core.deployment import load_install_sop_schema

    root = Path(install.__file__).resolve().parents[2]
    # The schema is owned by dcc-mcp-core and is republished under a new `-vN`
    # artifact name whenever its identity changes (core 0.20.34 froze -v1 and
    # moved to -v2), so compare against whatever the loader resolves to now
    # rather than against one frozen revision.
    assert install.load_install_sop_schema() == load_install_sop_schema()
    vendored = root / "src" / "dcc_mcp_marmoset" / "schemas"
    assert not list(vendored.glob("adapter-install-sop-*.json"))
    assert "dcc-mcp-core>=0.20.14,<1.0.0" in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_bootstrap_capture_preserves_hostile_baseexception_identity(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "DCC-MCP"
    plugin_dir.mkdir()

    class HostileBootstrap(BaseException):
        def __str__(self):
            raise RuntimeError("secondary-rendering-error")

    original = HostileBootstrap()

    def hostile_log(_message):
        raise SystemExit("secondary-host-log-error")

    fake_mset = types.SimpleNamespace(
        getPluginPath=lambda: str(plugin_dir / "__main__.py"), err=hostile_log
    )

    def fail_start(_mset, _plugin_dir):
        raise original

    monkeypatch.setitem(sys.modules, "mset", fake_mset)
    monkeypatch.setitem(sys.modules, "_runtime", types.SimpleNamespace(start_runtime=fail_start))
    entrypoint = Path(install.__file__).resolve().parent / "toolbag_plugin" / "__main__.py"

    caught = None
    try:
        runpy.run_path(str(entrypoint), run_name="__main__")
    except BaseException as exc:
        caught = exc

    assert caught is original
    diagnostic = json.loads((plugin_dir / install.BOOTSTRAP_ERROR_NAME).read_text(encoding="utf-8"))
    assert diagnostic["error_class"].endswith("HostileBootstrap")
    assert "secondary-rendering-error" not in json.dumps(diagnostic)
    assert len(diagnostic["message"]) <= install.MAX_PUBLIC_MESSAGE_CHARS


def test_bootstrap_diagnostic_is_redacted_before_verify_json(tmp_path, capsys, monkeypatch):
    target, receipt_path = _install_receipted_fixture(tmp_path, capsys, monkeypatch)
    monkeypatch.undo()
    monkeypatch.setattr(install, "_windows_file_version", lambda _path: None)
    secret = "bootstrap-super-secret"
    diagnostic = {
        "stage": "toolbag_plugin_bootstrap",
        "error_class": "RuntimeError",
        "message": f"TOKEN={secret}",
        "traceback": rf"C:\private\host.py TOKEN={secret}",
    }
    (target / install.BOOTSTRAP_ERROR_NAME).write_text(json.dumps(diagnostic), encoding="utf-8")

    assert install.main(["verify", "--json", "--receipt-path", str(receipt_path)]) == 40
    rendered = capsys.readouterr().out
    report = json.loads(rendered)
    _assert_install_report_schema(report)
    assert report["failure_reason"] == "bootstrap_error"
    assert secret not in rendered
    assert "host.py" not in rendered


def test_unexpected_lifecycle_failure_still_emits_schema_shaped_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        install, "_handle_status", lambda _args: (_ for _ in ()).throw(KeyError("x"))
    )

    assert (
        install.main(["status", "--json", "--receipt-path", str(tmp_path / "receipt.json")])
        == install.EXIT_INSTALL
    )
    report = json.loads(capsys.readouterr().out)
    _assert_install_report_schema(report)
    assert set(install.load_install_sop_schema()["required"]) <= set(report)
    assert report["failure_reason"] == "internal_error"
    assert "KeyError" in report["details"]["error_type"]
