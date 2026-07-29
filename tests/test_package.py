import json
import os
from pathlib import Path

from dcc_mcp_marmoset import __version__
from dcc_mcp_marmoset.server import MarmosetMcpServer, _parse_args, _process_is_alive


def test_version_metadata_is_synchronized():
    root = Path(__file__).parents[1]
    assert f'version = "{__version__}"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    manifest = json.loads((root / ".release-please-manifest.json").read_text(encoding="utf-8"))
    assert manifest["."] == __version__


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
