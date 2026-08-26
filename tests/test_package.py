import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from dcc_mcp_marmoset import __version__
from dcc_mcp_marmoset.server import MarmosetMcpServer, _parse_args, _process_is_alive


def test_version_metadata_is_synchronized():
    root = Path(__file__).parents[1]
    assert f'version = "{__version__}"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    manifest = json.loads((root / ".release-please-manifest.json").read_text(encoding="utf-8"))
    assert manifest["."] == __version__

    skill_paths = {
        f"src/dcc_mcp_marmoset/skills/{name}/SKILL.md"
        for name in ("marmoset-diagnostics", "marmoset-lookdev", "marmoset-scene")
    }
    for skill_path in skill_paths:
        skill = (root / skill_path).read_text(encoding="utf-8")
        skill_version = re.search(r'^    version: "([^"]+)"', skill, re.MULTILINE)
        assert skill_version is not None
        assert skill_version.group(1) == __version__

    lock = (root / "uv.lock").read_text(encoding="utf-8")
    lock_package = re.search(
        r'\[\[package\]\]\nname = "dcc-mcp-marmoset"\nversion = "([^"]+)"',
        lock,
    )
    assert lock_package is not None
    assert lock_package.group(1) == __version__

    release_config = json.loads((root / "release-please-config.json").read_text(encoding="utf-8"))
    extra_files = {item["path"]: item for item in release_config["packages"]["."]["extra-files"]}
    assert skill_paths <= extra_files.keys()
    assert extra_files["uv.lock"] == {
        "type": "toml",
        "path": "uv.lock",
        "jsonpath": "$.package[?(@.name.value=='dcc-mcp-marmoset')].version",
    }


def test_current_changelog_deduplicates_the_pr_2_showcase_entry():
    changelog = (Path(__file__).parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    current_release = changelog.split("## [0.1.1]", maxsplit=1)[0]

    assert current_release.count("add Marmoset TA workflows and showcase") == 1
    assert "https://github.com/dcc-mcp/dcc-mcp-marmoset/pull/2" in current_release


def test_release_please_next_patch_updates_every_version_surface(tmp_path):
    root = Path(__file__).parents[1]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "uv lock --check" in workflow

    major, minor, patch = (int(part) for part in __version__.split("."))
    next_version = f"{major}.{minor}.{patch + 1}"
    release_config = json.loads((root / "release-please-config.json").read_text(encoding="utf-8"))
    project = tmp_path / "next-release"
    managed_paths = {
        "release-please-config.json",
        ".release-please-manifest.json",
        "pyproject.toml",
        *(item["path"] for item in release_config["packages"]["."]["extra-files"]),
    }
    for relative_path in managed_paths:
        destination = project / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative_path, destination)

    release_please_root_env = os.environ.get("RELEASE_PLEASE_17_3_0_ROOT")
    if release_please_root_env:
        release_please_root = Path(release_please_root_env).resolve()
    else:
        npm = shutil.which("npm")
        assert npm is not None
        runner = tmp_path / "release-please-runner"
        installed = subprocess.run(
            [
                npm,
                "install",
                "--prefix",
                str(runner),
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "release-please@17.3.0",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert installed.returncode == 0, installed.stderr
        release_please_root = runner / "node_modules" / "release-please"

    release_please_package = json.loads(
        (release_please_root / "package.json").read_text(encoding="utf-8")
    )
    assert release_please_package["version"] == "17.3.0"
    node = shutil.which("node")
    assert node is not None
    replay = subprocess.run(
        [
            node,
            str(root / "tools" / "replay_release_please.cjs"),
            str(release_please_root),
            str(project),
            next_version,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert replay.returncode == 0, replay.stderr
    replay_report = json.loads(replay.stdout)

    uv = shutil.which("uv")
    assert uv is not None
    lock_check = subprocess.run(
        [uv, "lock", "--check", "--directory", str(project)],
        check=False,
        capture_output=True,
        text=True,
    )

    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    manifest = json.loads((project / ".release-please-manifest.json").read_text(encoding="utf-8"))
    runtime = (project / "src" / "dcc_mcp_marmoset" / "__version__.py").read_text(encoding="utf-8")
    skill_versions = {}
    for name in ("marmoset-diagnostics", "marmoset-lookdev", "marmoset-scene"):
        skill = (project / "src" / "dcc_mcp_marmoset" / "skills" / name / "SKILL.md").read_text(
            encoding="utf-8"
        )
        match = re.search(r'^    version: "([^"]+)"', skill, re.MULTILINE)
        assert match is not None
        skill_versions[name] = match.group(1)
    lock = (project / "uv.lock").read_text(encoding="utf-8")
    lock_package = re.search(
        r'\[\[package\]\]\nname = "dcc-mcp-marmoset"\nversion = "([^"]+)"',
        lock,
    )
    assert lock_package is not None

    observed_versions = {
        "manifest": manifest["."],
        "pyproject": re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE).group(1),
        "runtime": re.search(r'__version__ = "([^"]+)"', runtime).group(1),
        "uv.lock": lock_package.group(1),
        **skill_versions,
    }
    failures = []
    if replay_report["warnings"]:
        failures.append(f"release-please warnings: {replay_report['warnings']}")
    if replay_report["errors"]:
        failures.append(f"release-please errors: {replay_report['errors']}")
    if set(observed_versions.values()) != {next_version}:
        failures.append(f"version surfaces: {observed_versions}")
    if lock_check.returncode != 0:
        failures.append(f"uv lock --check: {lock_check.stderr.strip()}")
    assert not failures, "\n".join(failures)


def test_bundled_plugin_and_skill_exist():
    package = Path(__file__).parents[1] / "src" / "dcc_mcp_marmoset"
    assert (package / "toolbag_plugin" / "__main__.py").is_file()
    assert (package / "skills" / "marmoset-scene" / "tools.yaml").is_file()


def test_server_options_bind_the_real_toolbag_pid(monkeypatch):
    captured = {}
    original = MarmosetMcpServer.__mro__[1].__module__
    assert original == "dcc_mcp_core.server_base"

    from dcc_mcp_marmoset import server as server_module

    from_env = server_module.DccServerOptions.from_env

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return from_env(*args, **kwargs)

    monkeypatch.setattr(server_module.DccServerOptions, "from_env", capture)
    instance = MarmosetMcpServer(host_pid=os.getpid())

    assert captured["dcc_pid"] == os.getpid()
    assert captured["adapter_version"] == __version__
    assert captured["instance_type"] == "gui"
    assert instance is not None


def test_cli_requires_host_identity_and_bridge_port():
    options = _parse_args(["--host-pid", "123", "--bridge-port", "4567"])
    assert options.host_pid == 123
    assert options.bridge_port == 4567


def test_process_probe_observes_current_process_without_terminating_it():
    assert _process_is_alive(os.getpid()) is True
