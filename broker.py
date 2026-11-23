#!/usr/bin/python3
import hashlib
import json
import logging
import os
import re
import selectors
import signal
import shutil
import subprocess
import tempfile
import time
import uuid

MAX_REQUEST = 300 * 1024
MAX_CONTENT = 256 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
WORKER_TIMEOUT = 30

RUNTIME = "/run/jarvis-baby-development-broker"
WORKSPACE = "/var/lib/jarvis-baby-development/workspace"
DOCKER = "/usr/bin/docker"
IMAGE = "sha256:27f0dbf47246ef462ec785fe1e43dca89e9df42e1dc00054ed05d6e0ffa9f1c8"

WORKER_UID = 65532
WORKER_GID = 65532
WORKSPACE_GID = 980

OPERATIONS = {
    "list_files": {"path"},
    "read_file": {"path"},
    "write_file": {"path", "content", "expected_sha256"},
    "create_directory": {"path"},
    "validate_python": {"path"},
}

def reject_constant(value):
    raise ValueError("nonstandard JSON constant")

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

def parse_json(raw):
    if type(raw) is not bytes or not raw or len(raw) > MAX_REQUEST:
        raise ValueError("invalid request size")
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=no_duplicates,
        parse_constant=reject_constant,
    )

def validate_path(path, allow_root=False):
    if type(path) is not str or len(path) > 512:
        raise ValueError("invalid path")
    if allow_root and path == "":
        return path
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError("invalid path")
    parts = path.split("/")
    if len(parts) > 16:
        raise ValueError("path depth exceeded")
    if any(
        part in ("", ".", "..") or part.startswith(".") or len(part) > 128
        for part in parts
    ):
        raise ValueError("invalid path")
    return path

def validate_request(request):
    if type(request) is not dict:
        raise ValueError("request must be an object")
    if set(request) != {"schema_version", "task_id", "operation", "args"}:
        raise ValueError("invalid request fields")
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise ValueError("unsupported schema version")

    task_id = request["task_id"]
    if type(task_id) is not str:
        raise ValueError("invalid task ID")
    try:
        parsed = uuid.UUID(task_id)
    except ValueError:
        raise ValueError("invalid task ID") from None
    if str(parsed) != task_id:
        raise ValueError("task ID must be canonical")

    operation = request["operation"]
    if type(operation) is not str or operation not in OPERATIONS:
        raise ValueError("operation not approved")

    args = request["args"]
    if type(args) is not dict or set(args) != OPERATIONS[operation]:
        raise ValueError("invalid operation arguments")

    validate_path(args["path"], allow_root=(operation == "list_files"))

    if operation == "write_file":
        content = args["content"]
        if type(content) is not str:
            raise ValueError("content must be text")
        if len(content.encode("utf-8")) > MAX_CONTENT:
            raise ValueError("content exceeds limit")
        expected = args["expected_sha256"]
        if expected is not None and (
            type(expected) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            raise ValueError("invalid expected SHA-256")

    return request

def make_worker_task(request):
    validate_request(request)
    return {
        "schema_version": 1,
        "task_id": request["task_id"],
        "operation": request["operation"],
        "args": request["args"],
    }

def validate_worker_output(raw, task_id, operation, request):
    if type(raw) is not bytes or not raw or len(raw) > MAX_OUTPUT:
        raise ValueError("worker output size invalid")

    result = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=no_duplicates,
        parse_constant=reject_constant,
    )
    if type(result) is not dict or set(result) != {
        "ok", "task_id", "operation", "result"
    }:
        raise ValueError("invalid worker response")
    if result["ok"] is not True:
        raise ValueError("worker did not succeed")
    if result["task_id"] != task_id or result["operation"] != operation:
        raise ValueError("worker identity mismatch")

    data = result["result"]
    if type(data) is not dict:
        raise ValueError("invalid worker result")
    path = request["args"]["path"]
    if data.get("path") != path:
        raise ValueError("worker path mismatch")

    if operation == "read_file":
        if set(data) != {"path", "content", "bytes", "sha256"}:
            raise ValueError("invalid read result")
        if type(data["content"]) is not str or type(data["bytes"]) is not int:
            raise ValueError("invalid read data")
        encoded = data["content"].encode("utf-8")
        if len(encoded) != data["bytes"] or len(encoded) > MAX_CONTENT:
            raise ValueError("read result size mismatch")
        if data["sha256"] != hashlib.sha256(encoded).hexdigest():
            raise ValueError("read result hash mismatch")

    elif operation == "list_files":
        if set(data) != {"path", "entries", "file_count", "total_bytes", "truncated"}:
            raise ValueError("invalid listing result")
        if type(data["entries"]) is not list or len(data["entries"]) > 500:
            raise ValueError("invalid listing entries")
        if type(data["file_count"]) is not int or type(data["total_bytes"]) is not int:
            raise ValueError("invalid listing totals")
        if not 0 <= data["file_count"] <= 1000 or not 0 <= data["total_bytes"] <= 64 * 1024 * 1024:
            raise ValueError("listing totals exceed limits")
        if type(data["truncated"]) is not bool:
            raise ValueError("invalid listing status")
        prefix = path + "/" if path else ""
        for entry in data["entries"]:
            if type(entry) is not dict or entry.get("type") not in ("file", "directory"):
                raise ValueError("invalid listing entry")
            expected = {"path", "type", "bytes"} if entry["type"] == "file" else {"path", "type"}
            if set(entry) != expected or type(entry["path"]) is not str:
                raise ValueError("invalid listing entry fields")
            item_path = entry["path"]
            if not item_path.startswith(prefix) or item_path.startswith("/") or "\\" in item_path:
                raise ValueError("invalid listed path")
            if entry["type"] == "file":
                if type(entry["bytes"]) is not int or entry["bytes"] < 0:
                    raise ValueError("invalid listed file size")

    elif operation == "write_file":
        if set(data) != {"path", "bytes", "sha256", "created", "backup"}:
            raise ValueError("invalid write result")
        if type(data["bytes"]) is not int or not 0 <= data["bytes"] <= MAX_CONTENT:
            raise ValueError("invalid write size")
        if type(data["created"]) is not bool:
            raise ValueError("invalid write metadata")
        if type(data["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
            raise ValueError("invalid write hash")
        backup = data["backup"]
        if backup is not None and (
            type(backup) is not str
            or not re.fullmatch(r"\.jarvis-backup-[0-9a-f]{32}", backup)
        ):
            raise ValueError("invalid backup reference")

    elif operation == "create_directory":
        if set(data) != {"path", "created"} or data["created"] is not True:
            raise ValueError("invalid directory result")

    elif operation == "validate_python":
        if data.get("valid") is True:
            if set(data) != {"path", "valid", "sha256"}:
                raise ValueError("invalid Python validation result")
            if type(data["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
                raise ValueError("invalid Python validation hash")
        elif data.get("valid") is False:
            if set(data) != {"path", "valid", "line", "column", "error"}:
                raise ValueError("invalid Python syntax result")
            if type(data["error"]) is not str:
                raise ValueError("invalid Python syntax message")
            if data["line"] is not None and type(data["line"]) is not int:
                raise ValueError("invalid Python syntax line")
            if data["column"] is not None and type(data["column"]) is not int:
                raise ValueError("invalid Python syntax column")
        else:
            raise ValueError("invalid Python validation status")

    return result

def run_bounded(command):
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root"},
    )

    output = bytearray()
    errors = bytearray()
    selector = selectors.DefaultSelector()
    streams = (proc.stdout, proc.stderr)
    limits = {
        proc.stdout.fileno(): MAX_OUTPUT,
        proc.stderr.fileno(): 65536,
    }
    buffers = {
        proc.stdout.fileno(): output,
        proc.stderr.fileno(): errors,
    }
    deadline = time.monotonic() + WORKER_TIMEOUT

    def terminate_process_group():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)

    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("development worker timed out")

            events = selector.select(min(remaining, 0.25))
            for key, _ in events:
                fd = key.fileobj.fileno()
                chunk = os.read(fd, 65536)

                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue

                target = buffers[fd]
                if len(target) + len(chunk) > limits[fd]:
                    raise ValueError(
                        "development worker output limit exceeded"
                    )
                target.extend(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("development worker timed out")

        returncode = proc.wait(timeout=remaining)
        return returncode, bytes(output)

    except BaseException:
        terminate_process_group()
        raise

    finally:
        selector.close()
        for stream in streams:
            if stream and not stream.closed:
                stream.close()

def run_worker(request):
    validate_request(request)
    task = make_worker_task(request)
    task_id = task["task_id"]
    operation = task["operation"]

    directory = tempfile.mkdtemp(prefix="task-", dir=RUNTIME)
    os.chmod(directory, 0o700)
    container_name = "jarvis-baby-dev-" + uuid.uuid4().hex

    try:
        raw = json.dumps(
            task, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        task_path = os.path.join(directory, "task.json")
        fd = os.open(
            task_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o440,
        )
        try:
            os.fchmod(fd, 0o440)
            os.fchown(fd, WORKER_UID, WORKER_GID)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("task write failed")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

        command = [
            DOCKER, "run", "--pull=never",
            "--name", container_name,
            "--label", "jarvis-baby.development-worker=true",
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--security-opt", "apparmor=jarvis-baby-development-worker",
            "--pids-limit", "32",
            "--memory", "256m",
            "--memory-swap", "256m",
            "--cpus", "1.0",
            "--user", f"{WORKER_UID}:{WORKER_GID}",
            "--group-add", str(WORKSPACE_GID),
            "--mount", f"type=bind,src={task_path},dst=/input/task.json,readonly",
            "--mount", f"type=bind,src={WORKSPACE},dst=/workspace",
            IMAGE,
        ]

        returncode, stdout = run_bounded(command)
        if returncode != 0:
            raise RuntimeError("development worker execution failed")

        return validate_worker_output(
            stdout, task_id, operation, request
        )

    finally:
        try:
            subprocess.run(
                [DOCKER, "rm", "--force", container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
                close_fds=True,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root"},
            )
        except Exception:
            logging.exception("worker cleanup attempt failed")

        shutil.rmtree(directory, ignore_errors=True)
