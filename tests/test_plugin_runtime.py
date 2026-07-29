from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from dcc_mcp_marmoset.toolbag_plugin import _runtime as runtime_module
from dcc_mcp_marmoset.toolbag_plugin._runtime import MarmosetCommands, PluginRuntime


class FakeObject:
    def __init__(self, uid, name, parent=None):
        self.uid = uid
        self.name = name
        self.parent = parent
        self.visible = True
        self.locked = False


class FakeTexture:
    sRGB = True

    def __init__(self, path="C:/textures/prop.albedo.png"):
        self.path = path


class FakeSubroutine:
    def __init__(self, name, field):
        self.name = name
        self.field = field
        self.value = None

    def getFieldNames(self):
        return [self.field]

    def getField(self, _name):
        return self.value or FakeTexture()

    def setField(self, name, value):
        assert name == self.field
        self.value = FakeTexture(value)


class FakeMaterial:
    def __init__(self, name):
        self.name = name
        self.assigned = []
        self.destroyed = False
        self.subroutines = {
            "albedo": FakeSubroutine("Albedo", "Albedo Map"),
            "surface": FakeSubroutine("Normals", "Normal Map"),
            "microsurface": FakeSubroutine("Roughness", "Roughness Map"),
            "reflectivity": FakeSubroutine("Metalness", "Metalness Map"),
            "occlusion": FakeSubroutine("Occlusion", "Occlusion Map"),
        }

    def assign(self, item, include_children):
        assert include_children is True
        self.assigned.append(item)

    def destroy(self):
        self.destroyed = True

    def getAssignedObjects(self):
        return self.assigned

    def getSubroutine(self, _name):
        return self.subroutines.get(_name)

    def setSubroutine(self, name, shader):
        assert self.subroutines[name].name == shader


class FakeMset:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.root = FakeObject(1, "Root")
        self.child = FakeObject(2, "Mesh", self.root)
        self.call_thread = None
        self.logs = []

    def log(self, message):
        self.logs.append(message)

    def getToolbagVersion(self):
        self.call_thread = threading.get_ident()
        return 5022

    def getGraphicsAdapterName(self):
        return "Test GPU"

    def getScenePath(self):
        return str(self.tmp_path / "scene.tbscene")

    def getSceneUnitScale(self):
        return 0.01

    def getAllObjects(self):
        return [self.root, self.child]

    def getSelectedObjects(self):
        return [self.child]

    def importModel(self, path):
        assert Path(path).is_file()
        return self.child

    def importMaterial(self, path, name):
        assert Path(path).is_file()
        return FakeMaterial(name)

    def saveScene(self, *_args):
        return None

    def renderCamera(self, path, *_args):
        Path(path).write_bytes(b"render")


def test_commands_inspect_and_render_with_validation(tmp_path):
    commands = MarmosetCommands(FakeMset(tmp_path))
    inspected = commands.execute("scene.inspect", {"max_objects": 1})
    assert inspected["object_count"] == 2
    assert inspected["objects"][0]["uid"] == "1"
    assert inspected["truncated"] is True

    output = tmp_path / "render.png"
    rendered = commands.execute("render.camera", {"path": str(output)})
    assert rendered["path"] == str(output)
    assert output.read_bytes() == b"render"


def test_commands_reject_unknown_methods_and_relative_paths(tmp_path):
    commands = MarmosetCommands(FakeMset(tmp_path))
    with pytest.raises(ValueError, match="Unsupported"):
        commands.execute("python.exec", {})
    with pytest.raises(ValueError, match="absolute"):
        commands.execute("scene.import_model", {"path": "relative.fbx"})


def test_create_pbr_material_assigns_exact_object(tmp_path):
    paths = {}
    for name in ("base_color", "normal", "roughness", "metalness", "occlusion"):
        paths[name] = tmp_path / f"prop.{name}.png"
        paths[name].write_bytes(b"texture")
    commands = MarmosetCommands(FakeMset(tmp_path))

    result = commands.execute(
        "material.create_pbr",
        {
            "material_name": "Prop",
            "object_uid": "2",
            "include_children": True,
            **{f"{name}_path": str(path) for name, path in paths.items()},
        },
    )

    assert result["material"]["name"] == "Prop"
    assert result["material"]["assigned_object_uids"] == ["2"]
    assert result["material"]["slots"]["albedo"]["fields"]["Albedo Map"]["texture_path"].endswith(
        "prop.base_color.png"
    )
    assert result["material"]["slots"]["albedo"]["fields"]["Albedo Map"]["sRGB"] is True


def test_set_visibility_validates_all_uids_before_mutating(tmp_path):
    fake = FakeMset(tmp_path)
    commands = MarmosetCommands(fake)

    with pytest.raises(ValueError, match="not found"):
        commands.execute("scene.set_visibility", {"object_uids": ["2", "404"], "visible": False})
    assert fake.child.visible is True

    result = commands.execute("scene.set_visibility", {"object_uids": ["2"], "visible": False})
    assert result["objects"][0]["visible"] is False


def test_runtime_polls_host_calls_on_the_main_thread(tmp_path):
    fake = FakeMset(tmp_path)
    runtime = PluginRuntime(fake, tmp_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    runtime._listener = listener
    client = socket.create_connection(listener.getsockname())
    try:
        request = {"token": runtime._token, "method": "diagnostics.ping", "params": {}}
        client.sendall(json.dumps(request).encode() + b"\n")
        main_thread_id = threading.get_ident()
        runtime.poll()
        response = json.loads(client.makefile().readline())
    finally:
        client.close()
        runtime.stop()

    assert response["result"]["status"] == "ok"
    assert fake.call_thread == main_thread_id


def test_runtime_rejects_wrong_bridge_token(tmp_path):
    runtime = PluginRuntime(FakeMset(tmp_path), tmp_path)
    with pytest.raises(PermissionError, match="token"):
        runtime._execute_request({"token": "wrong", "method": "diagnostics.ping", "params": {}})


def test_bridge_health_is_authenticated_without_entering_main_thread_queue(tmp_path):
    runtime = PluginRuntime(FakeMset(tmp_path), tmp_path)
    assert runtime._execute_request(
        {"token": runtime._token, "method": "bridge.health", "params": {}}
    ) == {"result": {"status": "ok"}}


def test_start_runtime_reuses_the_process_runtime(monkeypatch, tmp_path):
    created = []

    class FakeRuntime:
        running = True

        def __init__(self, _mset, _plugin_dir):
            created.append(self)

        def start(self):
            return None

    monkeypatch.setattr(runtime_module, "PluginRuntime", FakeRuntime)
    monkeypatch.setattr(runtime_module, "_active_runtime", None)
    mset = FakeMset(tmp_path)

    first = runtime_module.start_runtime(mset, tmp_path)
    second = runtime_module.start_runtime(mset, tmp_path)

    assert second is first
    assert len(created) == 1
    assert mset.logs == ["DCC-MCP Marmoset is already running"]


def test_windows_child_is_bound_to_native_ui_control(monkeypatch, tmp_path):
    captured = {}
    server = tmp_path / "dcc-mcp-marmoset.exe"
    server.write_bytes(b"")

    class FakeProcess:
        returncode = None

        def __init__(self, _args, **kwargs):
            captured.update(kwargs["env"])

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setenv("DCC_MCP_MARMOSET_SERVER", str(server))
    monkeypatch.delenv("DCC_MCP_UI_CONTROL_BACKEND", raising=False)
    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    monkeypatch.setattr(runtime_module.os, "getpid", lambda: 1234)
    monkeypatch.setattr(runtime_module.subprocess, "Popen", FakeProcess)
    runtime = PluginRuntime(FakeMset(tmp_path), tmp_path)
    try:
        runtime._start_child(4321)
    finally:
        runtime.stop()

    assert captured["DCC_MCP_UI_CONTROL_BACKEND"] == "windows-uia"
    assert captured["DCC_MCP_UI_CONTROL_UIA_PROCESS_ID"] == "1234"
