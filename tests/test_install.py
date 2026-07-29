import pytest

from dcc_mcp_marmoset import install


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
