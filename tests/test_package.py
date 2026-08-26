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
        "jsonpath": "$.package[?(@.name=='dcc-mcp-marmoset')].version",
    }


def test_current_changelog_deduplicates_the_pr_2_showcase_entry():
    changelog = (Path(__file__).parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    current_release = changelog.split("## [0.1.1]", maxsplit=1)[0]

    assert current_release.count("add Marmoset TA workflows and showcase") == 1
    assert "https://github.com/dcc-mcp/dcc-mcp-marmoset/pull/2" in current_release


def test_next_patch_version_regenerates_a_synchronized_lock(tmp_path):
    root = Path(__file__).parents[1]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "uv lock --check" in workflow

    major, minor, patch = (int(part) for part in __version__.split("."))
    next_version = f"{major}.{minor}.{patch + 1}"
    release_config = json.loads((root / "release-please-config.json").read_text(encoding="utf-8"))
    generic_paths = {
        item["path"]
        for item in release_config["packages"]["."]["extra-files"]
        if item["type"] == "generic"
    }
    assert generic_paths == {
        "src/dcc_mcp_marmoset/__version__.py",
        "src/dcc_mcp_marmoset/skills/marmoset-diagnostics/SKILL.md",
        "src/dcc_mcp_marmoset/skills/marmoset-lookdev/SKILL.md",
        "src/dcc_mcp_marmoset/skills/marmoset-scene/SKILL.md",
    }
    for generic_path in generic_paths:
        surface = (root / generic_path).read_text(encoding="utf-8")
        marker_lines = [
            line
            for line in surface.splitlines()
            if "x-release-please-version" in line and __version__ in line
        ]
        assert len(marker_lines) == 1
        assert next_version in marker_lines[0].replace(__version__, next_version)

    project = tmp_path / "next-release"
    project.mkdir()
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"uv==0.11.19"' in pyproject
    current_version_line = f'version = "{__version__}"'
    assert pyproject.count(current_version_line) == 1
    (project / "pyproject.toml").write_text(
        pyproject.replace(current_version_line, f'version = "{next_version}"'),
        encoding="utf-8",
    )
    shutil.copy2(root / "uv.lock", project / "uv.lock")

    uv = shutil.which("uv")
    assert uv is not None
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(tmp_path / "isolated-uv-cache")
    completed = subprocess.run(
        [uv, "lock", "--directory", str(project)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    regenerated_lock = (project / "uv.lock").read_text(encoding="utf-8")
    lock_package = re.search(
        r'\[\[package\]\]\nname = "dcc-mcp-marmoset"\nversion = "([^"]+)"',
        regenerated_lock,
    )
    assert lock_package is not None
    assert lock_package.group(1) == next_version


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
