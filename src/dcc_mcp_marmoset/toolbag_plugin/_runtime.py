"""Pure-stdlib Toolbag plugin runtime. This module deliberately avoids Core imports."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

MAX_MESSAGE_BYTES = 1024 * 1024
_active_runtime: Optional["PluginRuntime"] = None


class MarmosetCommands:
    """Typed Toolbag API boundary. No arbitrary script execution is exposed."""

    def __init__(self, mset: Any) -> None:
        self._mset = mset
        self._commands = {
            "diagnostics.ping": self._ping,
            "scene.inspect": self._inspect_scene,
            "scene.import_model": self._import_model,
            "scene.set_visibility": self._set_visibility,
            "material.create_pbr": self._create_pbr_material,
            "scene.save": self._save_scene,
            "render.camera": self._render_camera,
        }

    def execute(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        deadline = params.pop("_dcc_mcp_deadline_unix_ms", None)
        if deadline is not None and int(deadline) < int(time.time() * 1000):
            raise RuntimeError("Toolbag request expired before main-thread execution")
        command = self._commands.get(method)
        if command is None:
            raise ValueError(f"Unsupported Toolbag command: {method}")
        return command(params)

    def _ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        _require_keys(params, set())
        return {
            "status": "ok",
            "toolbag_version": str(self._mset.getToolbagVersion()),
            "graphics_adapter": str(self._mset.getGraphicsAdapterName()),
            "scene_path": str(self._mset.getScenePath() or ""),
        }

    def _inspect_scene(self, params: Dict[str, Any]) -> Dict[str, Any]:
        _require_keys(params, {"max_objects"})
        max_objects = _bounded_int(params.get("max_objects", 500), "max_objects", 1, 5000)
        objects = list(self._mset.getAllObjects())
        selected = list(self._mset.getSelectedObjects())
        return {
            "scene_path": str(self._mset.getScenePath() or ""),
            "scene_unit_scale": float(self._mset.getSceneUnitScale()),
            "object_count": len(objects),
            "objects": [_object_summary(item) for item in objects[:max_objects]],
            "selected_object_uids": [str(item.uid) for item in selected],
            "truncated": len(objects) > max_objects,
        }

    def _import_model(self, params: Dict[str, Any]) -> Dict[str, Any]:
        _require_keys(params, {"path"})
        path = _existing_absolute_file(params.get("path"), "path")
        imported = self._mset.importModel(str(path))
        return {"path": str(path), "object": _object_summary(imported)}

    def _set_visibility(self, params: Dict[str, Any]) -> Dict[str, Any]:
        _require_keys(params, {"object_uids", "visible"})
        object_uids = params.get("object_uids")
        if not isinstance(object_uids, list) or not object_uids:
            raise ValueError("object_uids must be a non-empty array")
        if len(object_uids) > 1000:
            raise ValueError("object_uids cannot contain more than 1000 items")
        requested = [str(uid).strip() for uid in object_uids]
        if any(not uid for uid in requested) or len(set(requested)) != len(requested):
            raise ValueError("object_uids must contain unique non-empty values")
        by_uid = {str(item.uid): item for item in self._mset.getAllObjects()}
        missing = [uid for uid in requested if uid not in by_uid]
        if missing:
            raise ValueError(f"Toolbag objects not found: {', '.join(missing)}")
        visible = bool(params.get("visible", True))
        for uid in requested:
            by_uid[uid].visible = visible
        return {"visible": visible, "objects": [_object_summary(by_uid[uid]) for uid in requested]}

    def _create_pbr_material(self, params: Dict[str, Any]) -> Dict[str, Any]:
        _require_keys(
            params,
            {
                "material_name",
                "object_uid",
                "include_children",
                "base_color_path",
                "normal_path",
                "roughness_path",
                "metalness_path",
                "occlusion_path",
            },
        )
        material_name = str(params.get("material_name") or "").strip()
        if not material_name:
            raise ValueError("material_name is required")
        paths = {
            "base_color": _existing_absolute_file(params.get("base_color_path"), "base_color_path"),
            "normal": _existing_absolute_file(params.get("normal_path"), "normal_path"),
            "roughness": _existing_absolute_file(params.get("roughness_path"), "roughness_path"),
            "metalness": _existing_absolute_file(params.get("metalness_path"), "metalness_path"),
            "occlusion": _existing_absolute_file(params.get("occlusion_path"), "occlusion_path"),
        }
        object_uid = str(params.get("object_uid") or "").strip()
        target = next(
            (item for item in self._mset.getAllObjects() if str(item.uid) == object_uid),
            None,
        )
        if target is None:
            raise ValueError(f"Toolbag object not found: {object_uid}")
        material = self._mset.importMaterial(str(paths["base_color"]), material_name)
        try:
            configured_fields = {
                "base_color": _set_texture_map(
                    material,
                    "albedo",
                    "Albedo",
                    "Albedo Map",
                    paths["base_color"],
                    sRGB=True,
                ),
                "normal": _set_texture_map(
                    material, "surface", "Normals", "Normal Map", paths["normal"]
                ),
                "roughness": _set_texture_map(
                    material,
                    "microsurface",
                    "Roughness",
                    "Roughness Map",
                    paths["roughness"],
                ),
                "metalness": _set_texture_map(
                    material,
                    "reflectivity",
                    "Metalness",
                    "Metalness Map",
                    paths["metalness"],
                ),
                "occlusion": _set_texture_map(
                    material,
                    "occlusion",
                    "Occlusion",
                    "Occlusion Map",
                    paths["occlusion"],
                ),
            }
            material.assign(target, bool(params.get("include_children", True)))
        except Exception:
            material.destroy()
            raise
        return {
            "material": _material_summary(material),
            "assigned_object_uid": object_uid or None,
            "configured_fields": configured_fields,
        }

    def _save_scene(self, params: Dict[str, Any]) -> Dict[str, Any]:
        _require_keys(params, {"path"})
        raw_path = str(params.get("path") or "").strip()
        if raw_path:
            path = _absolute_output_path(raw_path, "path", {".tbscene"})
            self._mset.saveScene(str(path))
        else:
            current = str(self._mset.getScenePath() or "")
            if not current:
                raise ValueError("path is required for an unsaved Toolbag scene")
            path = Path(current)
            self._mset.saveScene()
        return {"scene_path": str(path)}

    def _render_camera(self, params: Dict[str, Any]) -> Dict[str, Any]:
        _require_keys(
            params,
            {"path", "width", "height", "sampling", "transparency", "camera"},
        )
        path = _absolute_output_path(
            params.get("path"),
            "path",
            {".png", ".jpg", ".jpeg", ".tga", ".psd", ".exr"},
        )
        width = _bounded_int(params.get("width", -1), "width", -1, 16384)
        height = _bounded_int(params.get("height", -1), "height", -1, 16384)
        sampling = _bounded_int(params.get("sampling", -1), "sampling", -1, 4096)
        if width == 0 or height == 0 or sampling == 0:
            raise ValueError("width, height, and sampling must be -1 or positive")
        camera = str(params.get("camera") or "")
        transparency = bool(params.get("transparency", False))
        self._mset.renderCamera(
            str(path),
            width,
            height,
            sampling,
            transparency,
            camera,
        )
        if not path.is_file():
            raise RuntimeError("Toolbag render completed without creating the output file")
        return {
            "path": str(path),
            "width": width,
            "height": height,
            "sampling": sampling,
            "transparency": transparency,
            "camera": camera,
        }


class PluginRuntime:
    """Own the loopback listener, main-thread queue, and child service."""

    def __init__(self, mset: Any, plugin_dir: Path) -> None:
        self._mset = mset
        self._plugin_dir = plugin_dir
        self._commands = MarmosetCommands(mset)
        self._listener: Optional[socket.socket] = None
        self._child: Optional[subprocess.Popen[Any]] = None
        self._log_handle: Optional[Any] = None
        self._token = secrets.token_urlsafe(32)
        self._lifetime_window: Optional[Any] = None
        self._stopped = False
        self._poll_callback = self.poll
        self._shutdown_callback = self.stop
        self._stop_click_callback = self._stop_from_ui

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.setblocking(False)
        self._listener = listener
        port = listener.getsockname()[1]
        self._start_child(port)
        self._mset.callbacks.onPeriodicUpdate = self._poll_callback
        self._mset.callbacks.onShutdownPlugin = self._shutdown_callback
        self._lifetime_window = self._mset.UIWindow("DCC-MCP")
        stop_button = self._mset.UIButton("Stop MCP")
        stop_button.onClick = self._stop_click_callback
        self._lifetime_window.addElement(stop_button)
        self._lifetime_window.width = 160
        self._lifetime_window.height = 48

    @property
    def running(self) -> bool:
        return (
            not self._stopped
            and self._listener is not None
            and self._child is not None
            and self._child.poll() is None
        )

    def poll(self) -> None:
        for _ in range(4):
            try:
                connection, _address = self._listener.accept() if self._listener else (None, None)
            except (BlockingIOError, OSError):
                break
            if connection is not None:
                self._handle_connection(connection)
        if self._child is not None and self._child.poll() is not None:
            self._mset.err(f"DCC-MCP Marmoset adapter exited. See: {self.log_path}")
            self._child = None

    @property
    def log_path(self) -> Path:
        return Path(tempfile.gettempdir()) / f"dcc-mcp-marmoset-{os.getpid()}.log"

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._child is not None and self._child.poll() is None:
            self._child.terminate()
            try:
                self._child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._child.kill()
        self._child = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self._lifetime_window = None

    def _stop_from_ui(self) -> None:
        self.stop()
        self._mset.shutdownPlugin()

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(0.05)
            try:
                request = json.loads(_read_line(connection).decode("utf-8"))
                response = self._execute_request(request)
            except Exception as exc:
                response = {
                    "error": {
                        "code": "invalid_request",
                        "message": str(exc) or type(exc).__name__,
                    }
                }
            encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_MESSAGE_BYTES:
                encoded = (
                    b'{"error":{"code":"response_too_large","message":"Response exceeds 1 MiB"}}'
                )
            try:
                connection.sendall(encoded + b"\n")
            except OSError:
                pass

    def _execute_request(self, request: Any) -> Dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        supplied = str(request.get("token") or "")
        if not hmac.compare_digest(supplied, self._token):
            raise PermissionError("invalid bridge token")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if method == "bridge.health":
            if params:
                raise ValueError("bridge.health accepts no parameters")
            return {"result": {"status": "ok"}}
        try:
            return {"result": self._commands.execute(method, dict(params))}
        except Exception as exc:
            return {"error": {"code": "host_error", "message": str(exc) or type(exc).__name__}}

    def _start_child(self, port: int) -> None:
        configured = os.environ.get("DCC_MCP_MARMOSET_SERVER", "").strip()
        if configured:
            server = Path(configured).expanduser().resolve()
        else:
            path_file = self._plugin_dir / "server_path.txt"
            if not path_file.is_file():
                raise RuntimeError("server_path.txt is missing; reinstall the Toolbag plugin")
            server = Path(path_file.read_text(encoding="utf-8").strip()).expanduser().resolve()
        if not server.is_file():
            raise RuntimeError(f"DCC-MCP Marmoset server not found: {server}")

        env = dict(os.environ)
        env["DCC_MCP_MARMOSET_BRIDGE_PORT"] = str(port)
        env["DCC_MCP_MARMOSET_BRIDGE_TOKEN"] = self._token
        env["DCC_MCP_MARMOSET_VERSION"] = str(self._mset.getToolbagVersion())
        if sys.platform == "win32":
            env.setdefault("DCC_MCP_UI_CONTROL_BACKEND", "windows-uia")
            env["DCC_MCP_UI_CONTROL_UIA_PROCESS_ID"] = str(os.getpid())
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self._child = subprocess.Popen(
            [str(server), "--host-pid", str(os.getpid()), "--bridge-port", str(port)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )


def _read_line(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= MAX_MESSAGE_BYTES:
        chunk = connection.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        chunks.extend(chunk if newline < 0 else chunk[:newline])
        if newline >= 0:
            break
    if len(chunks) > MAX_MESSAGE_BYTES:
        raise ValueError("request exceeds 1 MiB")
    if not chunks:
        raise ValueError("empty request")
    return bytes(chunks)


def _require_keys(params: Dict[str, Any], allowed: set[str]) -> None:
    unexpected = sorted(set(params) - allowed)
    if unexpected:
        raise ValueError(f"Unexpected parameters: {', '.join(unexpected)}")


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _existing_absolute_file(value: Any, name: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{name} must be an existing absolute file path")
    return path.resolve()


def _absolute_output_path(value: Any, name: str, extensions: set[str]) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    path = path.resolve()
    if path.suffix.lower() not in extensions:
        raise ValueError(f"{name} must use one of: {', '.join(sorted(extensions))}")
    if not path.parent.is_dir():
        raise ValueError(f"{name} parent folder does not exist")
    return path


def _object_summary(item: Any) -> Dict[str, Any]:
    parent = getattr(item, "parent", None)
    return {
        "uid": str(item.uid),
        "name": str(item.name),
        "type": type(item).__name__,
        "visible": bool(item.visible),
        "locked": bool(item.locked),
        "parent_uid": str(parent.uid) if parent is not None else None,
    }


def _material_summary(material: Any) -> Dict[str, Any]:
    slots = {}
    for slot_name in ("surface", "microsurface", "albedo", "reflectivity", "occlusion"):
        subroutine = material.getSubroutine(slot_name)
        if subroutine is None:
            slots[slot_name] = None
            continue
        slots[slot_name] = {
            "shader": str(subroutine.name),
            "fields": {
                str(field): _json_value(subroutine.getField(field))
                for field in subroutine.getFieldNames()
            },
        }
    return {
        "name": str(material.name),
        "assigned_object_uids": [str(item.uid) for item in material.getAssignedObjects()],
        "slots": slots,
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    path = getattr(value, "path", None)
    if path is not None:
        return {"texture_path": str(path), "sRGB": bool(getattr(value, "sRGB", False))}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _set_texture_map(
    material: Any,
    slot_name: str,
    shader_name: str,
    field_name: str,
    path: Path,
    *,
    sRGB: bool = False,
) -> str:
    material.setSubroutine(slot_name, shader_name)
    subroutine = material.getSubroutine(slot_name)
    if subroutine is None:
        raise RuntimeError(f"Toolbag did not create the {slot_name} subroutine")
    fields = {str(name).casefold(): str(name) for name in subroutine.getFieldNames()}
    resolved = fields.get(field_name.casefold())
    if resolved is None:
        raise RuntimeError(
            f"Toolbag {slot_name} shader '{subroutine.name}' has no '{field_name}' field; "
            f"available fields: {', '.join(fields.values())}"
        )
    subroutine.setField(resolved, str(path))
    texture = subroutine.getField(resolved)
    if texture is None or not hasattr(texture, "sRGB"):
        raise RuntimeError(f"Toolbag {slot_name} field '{resolved}' did not create a texture")
    texture.sRGB = sRGB
    return resolved


def start_runtime(mset: Any, plugin_dir: Path) -> PluginRuntime:
    """Start at most one adapter runtime inside the current Toolbag process."""
    global _active_runtime
    if _active_runtime is not None and _active_runtime.running:
        mset.log("DCC-MCP Marmoset is already running")
        return _active_runtime
    if _active_runtime is not None:
        _active_runtime.stop()
    _active_runtime = PluginRuntime(mset, plugin_dir)
    _active_runtime.start()
    return _active_runtime
