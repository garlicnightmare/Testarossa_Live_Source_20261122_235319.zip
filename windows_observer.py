import base64
import ctypes
import ctypes.wintypes
import hashlib
import io
import hmac
import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import Image, ImageGrab

BIND = "172.30.50.1"
PORT = 8766
ALLOWED_CLIENT = "172.30.50.10"
BLENDER = r"D:\blender\blender.exe"
KEY_PATH = Path(__file__).with_name("observer.key")
MAX_BODY = 4096
VISION_MAX_IMAGE = 480 * 1024
VISION_SESSION_SECONDS = 60
VISION_MAX_CAPTURES = 30
VISION_SESSIONS = {}
VISION_LOCK = threading.Lock()
MAX_SCENE = 1024 * 1024
NONCES = {}
NONCE_LOCK = threading.Lock()


def load_key():
    key = KEY_PATH.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise RuntimeError("Observer key file is invalid")
    return bytes.fromhex(key)


KEY = load_key()


def workstation():
    return {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
    }


def blender_status():
    version = None
    version_error = None
    try:
        result = subprocess.run(
            [BLENDER, "--version"], capture_output=True, text=True,
            timeout=12, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = (result.stdout or result.stderr).splitlines()
        version = lines[0][:200] if lines else None
        if result.returncode != 0:
            version_error = "Blender version command returned a non-zero exit code"
    except Exception as exc:
        version_error = type(exc).__name__

    running = False
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq blender.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        running = "blender.exe" in result.stdout.lower()
    except Exception:
        pass

    return {
        "installed": os.path.isfile(BLENDER),
        "executable": BLENDER,
        "version": version,
        "version_error": version_error,
        "running": running,
    }


def blender_snapshot(operation):
    request = json.dumps(
        {"operation": operation}, separators=(",", ":")
    ).encode("utf-8") + b"\n"

    chunks = bytearray()
    with socket.create_connection(("127.0.0.1", 8767), timeout=4) as conn:
        conn.settimeout(5)
        conn.sendall(request)
        while True:
            part = conn.recv(65536)
            if not part:
                break
            chunks.extend(part)
            if len(chunks) > MAX_SCENE:
                raise ValueError("Blender snapshot exceeds size limit")
            if b"\n" in chunks:
                break

    if not chunks or b"\n" not in chunks:
        raise ValueError("Incomplete Blender response")

    raw = bytes(chunks).split(b"\n", 1)[0]
    data = json.loads(raw.decode("utf-8"))
    if type(data) is not dict or set(data) != {"ok", "operation", "result"}:
        raise ValueError("Invalid Blender response")
    if data["ok"] is not True or data["operation"] != operation:
        raise ValueError("Blender bridge rejected request")

    result = data["result"]
    if type(result) is not dict or result.get("read_only") is not True:
        raise ValueError("Invalid read-only snapshot")
    if operation == "scene_snapshot":
        if result.get("schema_version") != 1:
            raise ValueError("Unsupported scene snapshot")
        if type(result.get("objects")) is not list or len(result["objects"]) > 500:
            raise ValueError("Invalid scene object list")
    elif operation == "project_snapshot":
        if result.get("schema_version") != 3:
            raise ValueError("Unsupported project snapshot")

        required = {
            "schema_version", "captured_at", "blender_version", "project",
            "scenes", "scenes_truncated", "collections",
            "collections_truncated", "materials", "materials_truncated",
            "actions", "actions_truncated", "objects", "objects_truncated",
            "summary", "read_only"
        }
        if set(result) != required:
            raise ValueError("Invalid project snapshot fields")

        project = result["project"]
        project_fields = {
            "filepath", "filename", "is_saved", "is_dirty",
            "scene_count", "collection_count", "material_count",
            "action_count", "object_count"
        }
        if type(project) is not dict or set(project) != project_fields:
            raise ValueError("Invalid project metadata")
        if type(project["filepath"]) is not str or len(project["filepath"]) > 2048:
            raise ValueError("Invalid project filepath")
        if project["filename"] is not None and (
            type(project["filename"]) is not str or len(project["filename"]) > 255
        ):
            raise ValueError("Invalid project filename")
        if type(project["is_saved"]) is not bool or type(project["is_dirty"]) is not bool:
            raise ValueError("Invalid project flags")
        for key in ("scene_count", "collection_count", "material_count",
                    "action_count", "object_count"):
            if type(project[key]) is not int or not 0 <= project[key] <= 100000:
                raise ValueError("Invalid project count")

        caps = {
            "scenes": 50, "collections": 500, "materials": 500,
            "actions": 500, "objects": 500
        }
        for key, cap in caps.items():
            if type(result[key]) is not list or len(result[key]) > cap:
                raise ValueError("Invalid project " + key)
        for key in ("scenes_truncated", "collections_truncated",
                    "materials_truncated", "actions_truncated",
                    "objects_truncated"):
            if type(result[key]) is not bool:
                raise ValueError("Invalid project truncation flag")

        scene_fields = {
            "name", "frame_current", "frame_start", "frame_end",
            "render_engine", "resolution_x", "resolution_y",
            "resolution_percentage", "camera", "active_object",
            "object_count", "objects", "objects_truncated"
        }
        for scene in result["scenes"]:
            if type(scene) is not dict or set(scene) != scene_fields:
                raise ValueError("Invalid project scene")
            if type(scene["objects"]) is not list or len(scene["objects"]) > 500:
                raise ValueError("Invalid scene object list")
            if type(scene["objects_truncated"]) is not bool:
                raise ValueError("Invalid scene truncation flag")

        collection_fields = {"name", "parent", "children", "objects", "objects_truncated"}
        for item in result["collections"]:
            if type(item) is not dict or set(item) != collection_fields:
                raise ValueError("Invalid project collection")
            if type(item["children"]) is not list or len(item["children"]) > 100:
                raise ValueError("Invalid collection children")
            if type(item["objects"]) is not list or len(item["objects"]) > 500:
                raise ValueError("Invalid collection objects")
            if type(item["objects_truncated"]) is not bool:
                raise ValueError("Invalid collection truncation flag")

        for item in result["materials"]:
            if type(item) is not dict or set(item) != {"name", "users", "use_nodes"}:
                raise ValueError("Invalid project material")
            if type(item["name"]) is not str or len(item["name"]) > 255:
                raise ValueError("Invalid material name")
            if type(item["users"]) is not int or item["users"] < 0:
                raise ValueError("Invalid material users")
            if type(item["use_nodes"]) is not bool:
                raise ValueError("Invalid material node flag")

        for item in result["actions"]:
            if type(item) is not dict or set(item) != {
                "name", "users", "layers", "slots", "keyframes",
                "keyframes_available"
            }:
                raise ValueError("Invalid project action")
            if type(item["name"]) is not str or len(item["name"]) > 255:
                raise ValueError("Invalid action name")
            for key in ("users", "layers", "slots"):
                if type(item[key]) is not int or item[key] < 0:
                    raise ValueError("Invalid action count")
            if type(item["keyframes_available"]) is not bool:
                raise ValueError("Invalid action keyframe flag")
            if item["keyframes"] is not None and (
                type(item["keyframes"]) is not int or item["keyframes"] < 0
            ):
                raise ValueError("Invalid action keyframe count")
            if item["keyframes_available"] != (item["keyframes"] is not None):
                raise ValueError("Inconsistent action keyframe metadata")

        summary_fields = {
            "scene_count", "collection_count", "material_count",
            "action_count", "object_count", "mesh_count", "armature_count",
            "camera_count", "light_count", "objects_truncated"
        }
        summary = result["summary"]
        if type(summary) is not dict or set(summary) != summary_fields:
            raise ValueError("Invalid project summary")
        for key in summary_fields - {"objects_truncated"}:
            if type(summary[key]) is not int or summary[key] < 0:
                raise ValueError("Invalid project summary count")
        if type(summary["objects_truncated"]) is not bool:
            raise ValueError("Invalid project summary flag")

        if type(result["captured_at"]) not in (int, float):
            raise ValueError("Invalid project timestamp")
        if type(result["blender_version"]) is not str or len(result["blender_version"]) > 100:
            raise ValueError("Invalid project Blender version")
    else:
        raise ValueError("Unsupported operation")
    return result



def vision_capture(session_token):
    if type(session_token) is not str or not 20 <= len(session_token) <= 200:
        raise ValueError("Invalid visual session token")

    now = time.time()
    with VISION_LOCK:
        session = VISION_SESSIONS.get(session_token)
        if not session or session["expires_at"] < now:
            VISION_SESSIONS.pop(session_token, None)
            raise PermissionError("Visual session expired")
        if session["captures"] >= VISION_MAX_CAPTURES:
            raise PermissionError("Visual session capture limit reached")
        if now - session["last_capture"] < 2:
            raise PermissionError("Please wait before capturing again")
        session["last_capture"] = now
        session["captures"] += 1

    # Capture is initiated only by an authenticated, explicit UI request.
    # The image remains in memory; this function never writes a screenshot file.
    shot = ImageGrab.grab(all_screens=True)
    shot = shot.convert("RGB")
    shot.thumbnail((1280, 720), Image.Resampling.LANCZOS)

    encoded = None
    for quality in (65, 55, 45):
        buffer = io.BytesIO()
        shot.save(buffer, format="JPEG", quality=quality, optimize=True)
        candidate = buffer.getvalue()
        buffer.close()
        if len(candidate) <= VISION_MAX_IMAGE:
            encoded = candidate
            break

    if encoded is None:
        shot.thumbnail((960, 540), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        shot.save(buffer, format="JPEG", quality=40, optimize=True)
        encoded = buffer.getvalue()
        buffer.close()

    if not encoded or len(encoded) > VISION_MAX_IMAGE:
        raise ValueError("Screenshot exceeds the in-memory image limit")

    width, height = shot.size
    result = {
        "image_b64": base64.b64encode(encoded).decode("ascii"),
        "media_type": "image/jpeg",
        "width": width,
        "height": height,
        "captured_at": time.time(),
    }
    del encoded, shot
    return result




# Bounded desktop control. This endpoint is reachable only after the existing
# HMAC, timestamp, client-IP, and nonce-replay checks have succeeded.
_CONTROL_KEYS = {
    "ENTER": 0x0D, "ESC": 0x1B, "TAB": 0x09, "SPACE": 0x20,
    "BACKSPACE": 0x08, "DELETE": 0x2E, "UP": 0x26, "DOWN": 0x28,
    "LEFT": 0x25, "RIGHT": 0x27, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "CTRL": 0x11, "ALT": 0x12, "SHIFT": 0x10,
}
_CONTROL_APPS = {
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "explorer": ["explorer.exe"],
    "blender": [BLENDER],
}

def _control_int(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid " + name)
    return value

def _desktop_window(title):
    if type(title) is not str or not title.strip() or len(title) > 120:
        raise ValueError("Invalid window title")

    user32 = ctypes.windll.user32
    wintypes = ctypes.wintypes

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    matches = []
    callback_errors = []

    def callback(hwnd, unused):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if 0 < length <= 512:
                buffer = ctypes.create_unicode_buffer(length + 1)
                copied = user32.GetWindowTextW(hwnd, buffer, length + 1)
                if copied > 0 and title.casefold() in buffer.value.casefold():
                    matches.append((hwnd, buffer.value))
            return True
        except Exception as exc:
            callback_errors.append(type(exc).__name__)
            return True

    callback_ref = callback_type(callback)
    if not user32.EnumWindows(callback_ref, 0):
        raise OSError("EnumWindows failed")
    if callback_errors:
        raise RuntimeError(
            "Window enumeration callback failed: " + ",".join(callback_errors)
        )
    if not matches:
        raise ValueError("No visible window matched that title")

    hwnd, matched_title = matches[0]
    user32.ShowWindow(hwnd, 9)
    if not user32.SetForegroundWindow(hwnd):
        raise RuntimeError("Windows did not permit foreground activation")
    return {"window_title": matched_title}

def desktop_control(action, args):
    if type(action) is not str or type(args) is not dict:
        raise ValueError("Invalid desktop control request")

    user32 = ctypes.windll.user32

    if action == "click":
        if set(args) - {"x", "y", "button"} or not {"x", "y"} <= set(args):
            raise ValueError("Invalid click fields")
        x = _control_int(args["x"], 0, user32.GetSystemMetrics(0) - 1, "x")
        y = _control_int(args["y"], 0, user32.GetSystemMetrics(1) - 1, "y")
        button = args.get("button", "left")
        if button not in ("left", "right", "middle"):
            raise ValueError("Invalid mouse button")
        user32.SetCursorPos(x, y)
        down, up = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }[button]
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
        return {"action": action, "accepted": True, "x": x, "y": y, "button": button}

    if action == "move":
        if set(args) != {"x", "y"}:
            raise ValueError("Invalid move fields")
        x = _control_int(args["x"], 0, user32.GetSystemMetrics(0) - 1, "x")
        y = _control_int(args["y"], 0, user32.GetSystemMetrics(1) - 1, "y")
        if not user32.SetCursorPos(x, y):
            raise RuntimeError("Cursor move failed")
        return {"action": action, "accepted": True, "x": x, "y": y}

    if action == "scroll":
        if set(args) != {"amount"}:
            raise ValueError("Invalid scroll fields")
        amount = _control_int(args["amount"], -10, 10, "scroll amount")
        if amount:
            user32.mouse_event(0x0800, 0, 0, amount * 120, 0)
        return {"action": action, "accepted": True, "amount": amount}

    if action == "type_text":
        if set(args) != {"text"} or type(args["text"]) is not str:
            raise ValueError("Invalid text fields")
        value = args["text"]
        if not value or len(value) > 500:
            raise ValueError("Text must contain 1-500 characters")
                # Complete Win32 INPUT layout; no clipboard access or shell interpretation.
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", ctypes.c_ulong),
                ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [
                ("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT)
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_ulong),
                ("union", INPUT_UNION)
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [
            ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int
        ]
        user32.SendInput.restype = ctypes.c_uint

        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        actual_size = ctypes.sizeof(INPUT)
        if actual_size != expected_size:
            raise RuntimeError(
                f"Incorrect Win32 INPUT structure size: {actual_size}; "
                f"expected {expected_size}"
            )

        sent = 0
        for char in value:
            code = ord(char)
            for flags in (0x0004, 0x0004 | 0x0002):
                item = INPUT()
                item.type = 1
                item.union.ki = KEYBDINPUT(0, code, flags, 0, None)
                ctypes.set_last_error(0)
                result = user32.SendInput(
                    1, ctypes.byref(item), ctypes.sizeof(INPUT)
                )
                if result != 1:
                    error = ctypes.get_last_error()
                    raise OSError(
                        error,
                        f"SendInput rejected Unicode keyboard input "
                        f"(sent={result}, INPUT_size={actual_size})"
                    )
                sent += 1
        return {"action": action, "accepted": True, "characters": len(value), "events": sent}

    if action == "press_key":
        if set(args) != {"key"} or type(args["key"]) is not str:
            raise ValueError("Invalid key fields")
        key = args["key"].upper()
        if key not in _CONTROL_KEYS:
            raise ValueError("Key is not on the permitted key list")
        code = _CONTROL_KEYS[key]
        user32.keybd_event(code, 0, 0, 0)
        user32.keybd_event(code, 0, 0x0002, 0)
        return {"action": action, "accepted": True, "key": key}

    if action == "launch_app":
        if set(args) != {"app"} or type(args["app"]) is not str:
            raise ValueError("Invalid app fields")
        app = args["app"].lower()
        if app not in _CONTROL_APPS:
            raise ValueError("Application is not allowlisted")
        subprocess.Popen(
            _CONTROL_APPS[app],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return {"action": action, "accepted": True, "app": app}

    if action == "switch_window":
        if set(args) != {"title"}:
            raise ValueError("Invalid window-switch fields")
        return {"action": action, "accepted": True, **_desktop_window(args["title"])}

    raise ValueError("Unsupported desktop control action")


class Handler(BaseHTTPRequestHandler):
    server_version = "JARVIS-Observer/1.2"

    def log_message(self, fmt, *args):
        return

    def reply(self, code, obj):
        data = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.client_address[0] != ALLOWED_CLIENT:
            self.reply(403, {"error": "forbidden"})
            return
        if self.path != "/v1/inspect":
            self.reply(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (ValueError, TypeError):
            self.reply(400, {"error": "bad_length"})
            return
        if length < 1 or length > MAX_BODY:
            self.reply(413, {"error": "body_size"})
            return

        body = self.rfile.read(length)
        if len(body) != length:
            self.reply(400, {"error": "incomplete_body"})
            return

        timestamp = self.headers.get("X-Jarvis-Timestamp", "")
        nonce = self.headers.get("X-Jarvis-Nonce", "")
        signature = self.headers.get("X-Jarvis-Signature", "")
        if not timestamp.isdigit() or not re.fullmatch(r"[0-9a-f]{32,128}", nonce):
            self.reply(401, {"error": "authentication"})
            return

        now = int(time.time())
        if abs(now - int(timestamp)) > 60:
            self.reply(401, {"error": "expired"})
            return

        signed = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body
        expected = hmac.new(KEY, signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            self.reply(401, {"error": "authentication"})
            return

        with NONCE_LOCK:
            for old, expiry in list(NONCES.items()):
                if expiry < now:
                    del NONCES[old]
            if nonce in NONCES:
                self.reply(409, {"error": "replay"})
                return
            if len(NONCES) >= 10000:
                self.reply(503, {"error": "nonce_capacity"})
                return
            NONCES[nonce] = now + 120

        try:
            request = json.loads(body.decode("utf-8"))
            if type(request) is not dict:
                raise ValueError()

            operation = request.get("operation")

            if operation == "desktop_control":
                if set(request) != {"operation", "action", "args"}:
                    raise ValueError()
                result = desktop_control(request["action"], request["args"])

            elif operation == "vision_start":
                if set(request) != {"operation"}:
                    raise ValueError()
                token = __import__("secrets").token_urlsafe(32)
                with VISION_LOCK:
                    VISION_SESSIONS[token] = {
                        "expires_at": time.time() + VISION_SESSION_SECONDS,
                        "captures": 0,
                        "last_capture": 0.0,
                    }
                result = {
                    "session_token": token,
                    "expires_in": VISION_SESSION_SECONDS,
                    "screenshot_persistence": False,
                }

            elif operation == "vision_capture":
                if set(request) != {"operation", "session_token"}:
                    raise ValueError()
                result = vision_capture(request["session_token"])

            elif operation == "vision_stop":
                if set(request) != {"operation", "session_token"}:
                    raise ValueError()
                token = request["session_token"]
                if type(token) is not str:
                    raise ValueError()
                with VISION_LOCK:
                    VISION_SESSIONS.pop(token, None)
                result = {"active": False, "session_cleared": True}

            else:
                if set(request) != {"operation"}:
                    raise ValueError()
                if operation == "workstation":
                    result = workstation()
                elif operation == "blender_status":
                    result = blender_status()
                elif operation in ("blender_scene_snapshot", "blender_project_snapshot"):
                    bridge_operation = (
                        "scene_snapshot" if operation == "blender_scene_snapshot"
                        else "project_snapshot"
                    )
                    try:
                        result = blender_snapshot(bridge_operation)
                    except Exception as exc:
                        self.reply(503, {
                            "error": "scene_unavailable",
                            "detail": type(exc).__name__,
                        })
                        return
                else:
                    self.reply(400, {"error": "unsupported_operation"})
                    return
            self.reply(200, {
                "ok": True, "operation": operation, "result": result
            })
        except Exception:
            try:
                with open(__file__ + ".errors.log", "a", encoding="utf-8") as log:
                    log.write("\\n--- " + time.strftime("%Y-%m-%d %H:%M:%S") + " ---\\n")
                    log.write(traceback.format_exc())
            except Exception:
                pass
            self.reply(400, {"error": "invalid_request"})


if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()