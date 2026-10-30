"""Explicit per-user broker control. This file never registers an IME or autorun."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from native.backend.server import Win32, _last_error, _win_error

SCHEMA_VERSION = 1
WAIT_TIMEOUT = 258
INVALID_HANDLE = ctypes.c_void_p(-1).value
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SID = re.compile(r"S-1-(?:[0-9]+-)*[0-9]+\Z")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_json(path: Path) -> dict:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"Oversized JSON: {path}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def require_amd64(path: Path):
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError(f"Not a Windows executable: {path}")
        offset = struct.unpack_from("<I", header, 60)[0]
        if offset > 16 * 1024 * 1024:
            raise ValueError("Invalid PE header offset.")
        stream.seek(offset)
        if stream.read(6) != b"PE\0\0\x64\x86":
            raise ValueError(f"Expected an AMD64 PE executable: {path}")


def runtime_files(root: Path) -> set[str]:
    files = {
        "native/backend/server.py", "native/runtime/control.py", "native/runtime/Start-Runtime.ps1",
        "native/CantoneseIME.dll", "data/generated/lexicon.sqlite3", "data/generated/lexicon.manifest.json",
        "data/mappings/syllables.json", "data/mappings/spellings.json", "data/mappings/build.manifest.json",
        "data/vendor/scowl/2026.02.25/en_GB-ise.txt",
    }
    files.update(path.relative_to(root).as_posix() for path in (root / "cantonese_ime").glob("*.py"))
    # Release builds carry a private interpreter. Pin the complete distribution,
    # including its standard-library ZIP, native extensions and path policy;
    # validating python.exe alone would leave executable dependencies unchecked.
    bundled = root / "runtime/python"
    if bundled.is_dir():
        files.update(path.relative_to(root).as_posix() for path in bundled.rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    if "cantonese_ime/engine.py" not in files:
        raise ValueError("Install root has no engine package.")
    return files


def _local_file(root: Path, relative: str) -> Path:
    # Config file hashes describe local package roles, never arbitrary external paths.
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise ValueError("Runtime file keys must be relative forward-slash paths.")
    result = (root / path).resolve()
    if not result.is_relative_to(root):
        raise ValueError("Runtime file resolves outside the install root.")
    return result


@dataclass(frozen=True)
class Config:
    path: Path
    root: Path
    python: Path
    dictionary_hash: str
    python_hash: str
    files: dict[str, str]
    learning_enabled: bool = True
    learning_file: Path | None = None


def load_config(path: Path) -> Config:
    path = path.resolve()
    value = read_json(path)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported runtime config schema.")
    root = (path.parent / value.get("install_root", ".")).resolve()
    python_value = value["python_executable"]
    if not isinstance(python_value, str) or not python_value:
        raise ValueError("Config requires a Python executable path.")
    python = Path(python_value)
    if not python.is_absolute():
        python = _local_file(root, python_value)
        if not python.is_relative_to(root / "runtime/python"):
            raise ValueError("A relative Python executable must be inside runtime/python.")
    files = value.get("files_sha256")
    expected = value.get("expected_dictionary_sha256", "")
    python_hash = value.get("python_sha256", "")
    learning_enabled = value.get("learning_enabled", True)
    if not isinstance(learning_enabled, bool):
        raise ValueError("learning_enabled must be a JSON boolean.")
    learning_value = value.get("learning_file")
    if learning_value is not None and (not isinstance(learning_value, str) or not Path(learning_value).is_absolute()):
        raise ValueError("An explicit learning_file must be an absolute path.")
    learning_file = Path(learning_value).resolve() if learning_value is not None else None
    if not python.is_absolute() or not _SHA256.fullmatch(expected) or not _SHA256.fullmatch(python_hash):
        raise ValueError("Config requires a Python executable and pinned SHA256 values.")
    if not isinstance(files, dict) or set(files) != runtime_files(root):
        raise ValueError("Runtime file inventory differs from the configured package.")
    for relative, checksum in files.items():
        _local_file(root, relative)
        if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
            raise ValueError("Invalid runtime file SHA256.")
    if files["data/generated/lexicon.sqlite3"] != expected:
        raise ValueError("Dictionary hash fields disagree.")
    if learning_file in {path, python.resolve()} | {_local_file(root, name) for name in files}:
        raise ValueError("Learning memory must be separate from runtime inputs/configuration.")
    return Config(path, root, python.resolve(), expected, python_hash, files, learning_enabled, learning_file)


def verify_config(config: Config):
    require_amd64(config.python)
    if digest(config.python) != config.python_hash:
        raise ValueError("External Python executable changed; review and reconfigure it.")
    for relative, expected in sorted(config.files.items()):
        if digest(_local_file(config.root, relative)) != expected:
            raise ValueError(f"Runtime file changed: {relative}")
    require_amd64(config.root / "native/CantoneseIME.dll")
    manifest = read_json(config.root / "data/generated/lexicon.manifest.json")
    if manifest.get("database_sha256") != config.dictionary_hash:
        raise ValueError("Dictionary does not match its source/build manifest.")


def configure(root: Path, python: Path, output: Path, *, learning_enabled: bool = True,
              learning_file: Path | None = None) -> dict:
    root, python, output = root.resolve(), python.resolve(), output.resolve()
    if output == python or output in {_local_file(root, name) for name in runtime_files(root)}:
        raise ValueError("Runtime config must not overwrite an executable or runtime input.")
    if learning_file is not None:
        if not learning_file.is_absolute():
            raise ValueError("An explicit learning_file must be an absolute path.")
        learning_file = learning_file.resolve()
        if learning_file in {output, python} | {_local_file(root, name) for name in runtime_files(root)}:
            raise ValueError("Learning memory must be separate from runtime inputs/configuration.")
    require_amd64(python)
    info = json.loads(subprocess.check_output(
        [str(python), "-I", "-c", "import json,struct,sys;print(json.dumps({'version':sys.version,'bits':struct.calcsize('P')*8}))"],
        text=True, timeout=15))
    if info.get("bits") != 64:
        raise ValueError("The configured Python must run as a 64-bit process.")
    files = {name: digest(_local_file(root, name)) for name in sorted(runtime_files(root))}
    manifest = read_json(root / "data/generated/lexicon.manifest.json")
    expected = files["data/generated/lexicon.sqlite3"]
    if manifest.get("database_sha256") != expected:
        raise ValueError("Dictionary does not match its source/build manifest.")
    require_amd64(root / "native/CantoneseIME.dll")
    value = {"schema_version": SCHEMA_VERSION,
             "install_root": "." if output.parent == root else str(root),
             "python_executable": (python.relative_to(root).as_posix()
                                   if python.is_relative_to(root / "runtime/python") else str(python)),
             "python_version": info["version"],
             "python_bits": 64, "python_sha256": digest(python),
             "learning_enabled": learning_enabled,
             "learning_file": str(learning_file) if learning_file is not None else None,
             "expected_dictionary_sha256": expected, "files_sha256": files,
             "startup_policy": "explicit start; no autorun is installed by this controller"}
    write_json(output, value)
    return value


def fingerprint(config: Config) -> str:
    return hashlib.sha256(json.dumps({"files": config.files, "python": config.python_hash,
                                     "learning_enabled": config.learning_enabled,
                                     "learning_file": str(config.learning_file) if config.learning_file is not None else None},
                                     sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class State:
    directory: Path
    sid: str
    session: int

    @property
    def pipe(self):
        return rf"\\.\pipe\CantoneseIME-{self.sid}-{self.session}"

    def file(self, name: str) -> Path:
        return self.directory / name

    @property
    def learning_file(self) -> Path:
        # directory: LOCALAPPDATA/CantoneseIME/Runtime/<SID>/session-<id>
        return self.directory.parents[2] / "learning.json"

    @property
    def custom_file(self) -> Path:
        return self.directory.parents[2] / "custom.txt"

    @property
    def prediction_file(self) -> Path:
        return self.directory.parents[2] / "prediction.json"


def user_state(api) -> State:
    sid, session = api.identity()
    if not _SID.fullmatch(sid) or type(session) is not int or session < 0:
        raise ValueError("Invalid Windows user/session identity.")
    local = os.environ.get("LOCALAPPDATA")
    if not local or not Path(local).is_absolute():
        raise ValueError("LOCALAPPDATA must name the current user's absolute data directory.")
    return State(Path(local).resolve() / "CantoneseIME/Runtime" / sid / f"session-{session}", sid, session)


class Process:
    def __init__(self, api, handle, identity):
        self.api, self.handle, self.identity = api, handle, identity

    def wait(self, milliseconds: int) -> bool:
        result = self.api.kernel.WaitForSingleObject(self.handle, milliseconds)
        if result == 0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise _win_error(_last_error())


class Windows(Win32):
    """Small ownership API; handles remain open through verification and stop."""
    def __init__(self):
        super().__init__()
        k = self.kernel
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        k.GetProcessTimes.restype = w.BOOL
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.QueryFullProcessImageNameW.restype = w.BOOL
        k.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
        k.CreateMutexW.restype = w.HANDLE
        k.ReleaseMutex.argtypes = [w.HANDLE]
        k.ReleaseMutex.restype = w.BOOL
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.WaitNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD]
        k.WaitNamedPipeW.restype = w.BOOL
        k.GetNamedPipeServerProcessId.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        k.GetNamedPipeServerProcessId.restype = w.BOOL

    @contextmanager
    def lock(self, state: State):
        descriptor, attributes = self.security(state.sid)
        handle = None
        acquired = False
        try:
            handle = self.checked(self.kernel.CreateMutexW(ctypes.byref(attributes), False,
                                f"Local\\CantoneseIME.RuntimeControl-{state.sid}-{state.session}"))
            result = self.kernel.WaitForSingleObject(handle, 30000)
            acquired = result in (0, 0x80)  # Acquired normally or abandoned by a crashed controller.
            if not acquired:
                if result == WAIT_TIMEOUT:
                    raise TimeoutError("Another runtime controller is busy.")
                raise _win_error(_last_error())
            yield
        finally:
            if acquired:
                self.kernel.ReleaseMutex(handle)
            if handle:
                self.kernel.CloseHandle(handle)
            self.kernel.LocalFree(descriptor)

    @contextmanager
    def process(self, pid: int):
        if type(pid) is not int or pid <= 0:
            raise ValueError("Invalid broker PID.")
        handle = self.kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            code = _last_error()
            if code == 87:  # ERROR_INVALID_PARAMETER: process no longer exists.
                yield None
                return
            raise _win_error(code)
        token = w.HANDLE()
        string_sid = w.LPWSTR()
        try:
            created, exited, kernel, user = (w.FILETIME() for _ in range(4))
            self.checked(self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                                    ctypes.byref(kernel), ctypes.byref(user)))
            size = w.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            self.checked(self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)))
            self.checked(self.advapi.OpenProcessToken(handle, 0x0008, ctypes.byref(token)))
            needed = w.DWORD()
            first = self.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            code = _last_error() if not first else 0
            if not first and code != 122:
                raise _win_error(code)
            from native.backend.server import SidAndAttributes
            data = ctypes.create_string_buffer(needed.value)
            self.checked(self.advapi.GetTokenInformation(token, 1, data, needed, ctypes.byref(needed)))
            sid = ctypes.cast(data, ctypes.POINTER(SidAndAttributes)).contents.Sid
            self.checked(self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(string_sid)))
            session = w.DWORD()
            self.checked(self.kernel.ProcessIdToSessionId(pid, ctypes.byref(session)))
            identity = {"pid": pid, "creation_time": (created.dwHighDateTime << 32) | created.dwLowDateTime,
                        "image_path": str(Path(image.value).resolve()), "owner_sid": string_sid.value,
                        "windows_session": session.value}
            process = Process(self, handle, identity)
            yield None if process.wait(0) else process
        finally:
            if string_sid:
                self.kernel.LocalFree(ctypes.cast(string_sid, w.HLOCAL))
            if token:
                self.kernel.CloseHandle(token)
            self.kernel.CloseHandle(handle)

    def pipe_pid(self, name: str, *, timeout: float = 0.6) -> int | None:
        deadline = time.monotonic() + timeout
        while True:
            # Probe unknown servers at identification level, never impersonation.
            # FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION.
            handle = self.kernel.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40110000, None)
            if handle != INVALID_HANDLE:
                try:
                    pid = w.DWORD()
                    self.checked(self.kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)))
                    return pid.value
                finally:
                    self.kernel.CloseHandle(handle)
            code = _last_error()
            if code == 2:
                return None
            if code != 231:
                raise _win_error(code)
            if time.monotonic() >= deadline:
                raise TimeoutError("The existing broker pipe is busy; ownership cannot be verified.")
            self.kernel.WaitNamedPipeW(name, 50)


def _matches_process(process: Process, owner: dict, state: State):
    actual = process.identity
    expected = owner.get("process")
    if (actual != expected or actual.get("owner_sid") != state.sid
            or actual.get("windows_session") != state.session):
        raise RuntimeError("Ready state belongs to another process identity; refusing to control it.")


def _matches_ready(ready: dict, owner: dict, state: State):
    expected = {"pid": owner["process"]["pid"], "runtime_instance": owner["runtime_instance"],
                "pipe": state.pipe, "windows_session": state.session, "python_bits": 64,
                "dictionary_sha256": owner["dictionary_sha256"],
                "backend_source_sha256": owner["backend_source_sha256"]}
    if "learning_requested" in owner:
        expected["learning_requested"] = owner["learning_requested"]
        expected["learning_file"] = owner["learning_file"]
    if any(ready.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Broker readiness does not match this controller's launch.")


def _owned(state: State, config: Config) -> dict | None:
    path = state.file("ownership.json")
    if not path.exists():
        return None
    value = read_json(path)
    if value.get("install_root") != str(config.root):
        raise RuntimeError("Another install owns this session's broker; use its runtime config to stop it.")
    instance = value.get("runtime_instance")
    if not isinstance(instance, str) or str(uuid.UUID(instance)) != instance:
        raise ValueError("Invalid runtime ownership nonce.")
    return value


def _cleanup(state: State, owner: dict):
    # Never remove replacement ownership or another launch's stop request.
    for name in ("ownership.json", "stop.json", "backend-ready.json"):
        path = state.file(name)
        try:
            current = read_json(path)
            if (current.get("runtime_instance") == owner["runtime_instance"]
                    and current.get("pid", current.get("process", {}).get("pid")) == owner["process"]["pid"]):
                path.unlink()
        except FileNotFoundError:
            pass


def status(config: Config, api, state: State) -> dict:
    owner = _owned(state, config)
    if owner is None:
        pid = api.pipe_pid(state.pipe)
        return {"status": "unmanaged_broker" if pid is not None else "stopped", "pipe_pid": pid}
    with api.process(owner["process"]["pid"]) as process:
        if process is None:
            return {"status": "stale_state", "pid": owner["process"]["pid"]}
        _matches_process(process, owner, state)
        _matches_ready(read_json(state.file("backend-ready.json")), owner, state)
        if api.pipe_pid(state.pipe) != process.identity["pid"]:
            raise RuntimeError("The named pipe is absent or belongs to a different process.")
        return {"status": "ready", "pid": process.identity["pid"], "runtime_instance": owner["runtime_instance"],
                "dictionary_sha256": owner["dictionary_sha256"], "state_directory": str(state.directory)}


def start(config: Config, api, state: State, *, timeout: float = 20) -> dict:
    verify_config(config)
    learning_file = config.learning_file if config.learning_file is not None else state.learning_file
    if learning_file in {state.file(name).resolve() for name in ("ownership.json", "backend-ready.json", "stop.json", "backend.jsonl")}:
        raise ValueError("Learning memory must be separate from broker control/log files.")
    owner = _owned(state, config)
    if owner is not None:
        with api.process(owner["process"]["pid"]) as process:
            if process is not None:
                _matches_process(process, owner, state)
                if owner.get("runtime_fingerprint") != fingerprint(config):
                    raise RuntimeError("Running broker uses another build; stop it before starting this build.")
                return status(config, api, state)
        _cleanup(state, owner)
    if api.pipe_pid(state.pipe) is not None:
        raise RuntimeError("An unmanaged broker already owns the pipe; stop it using its original launcher.")
    state.directory.mkdir(parents=True, exist_ok=True)
    instance = str(uuid.uuid4())
    args = [str(config.python), "-B", "-I", "-u", str(config.root / "native/backend/server.py"),
            "--dictionary", str(config.root / "data/generated/lexicon.sqlite3"),
            "--ready-file", str(state.file("backend-ready.json")), "--log", str(state.file("backend.jsonl")),
            "--runtime-instance", instance, "--stop-file", str(state.file("stop.json"))]
    if config.learning_enabled:
        args += ["--learning-file", str(learning_file)]
    args += ["--custom-file", str(state.custom_file)]
    if config.learning_enabled:
        args += ["--prediction-file", str(state.prediction_file)]
    with state.file(f"{instance}.stdout.log").open("ab") as out, state.file(f"{instance}.stderr.log").open("ab") as err:
        child = subprocess.Popen(args, cwd=config.root, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                 close_fds=True, creationflags=0x08000000)  # CREATE_NO_WINDOW
    with api.process(child.pid) as process:
        if process is None:
            raise RuntimeError("Broker exited during startup; inspect the per-user stderr log.")
        if (process.identity["owner_sid"] != state.sid or process.identity["windows_session"] != state.session
                or Path(process.identity["image_path"]) != config.python):
            raise RuntimeError("Launched broker has an unexpected process identity.")
        owner = {"schema_version": SCHEMA_VERSION, "install_root": str(config.root),
                 "runtime_instance": instance, "process": process.identity,
                 "runtime_fingerprint": fingerprint(config),
                 "learning_requested": config.learning_enabled,
                 "learning_file": str(learning_file) if config.learning_enabled else None,
                 "dictionary_sha256": config.dictionary_hash,
                 "backend_source_sha256": config.files["native/backend/server.py"]}
        deadline = time.monotonic() + timeout
        try:
            write_json(state.file("ownership.json"), owner)
            while time.monotonic() < deadline:
                if process.wait(0):
                    raise RuntimeError("Broker exited before readiness; inspect the per-user stderr log.")
                try:
                    ready = read_json(state.file("backend-ready.json"))
                    if ready.get("runtime_instance") == instance:
                        _matches_ready(ready, owner, state)
                        if api.pipe_pid(state.pipe) == child.pid:
                            return status(config, api, state)
                except FileNotFoundError:
                    pass
                time.sleep(0.1)
            raise TimeoutError("Broker did not become ready within the startup deadline.")
        except BaseException:
            write_json(state.file("stop.json"), {"pid": child.pid, "runtime_instance": instance})
            if process.wait(5000):
                _cleanup(state, owner)
            raise


def stop(config: Config, api, state: State, *, timeout: float = 15) -> dict:
    owner = _owned(state, config)
    if owner is None:
        return status(config, api, state)
    with api.process(owner["process"]["pid"]) as process:
        if process is None:
            _cleanup(state, owner)
            return {"status": "stopped", "removed_stale_state": True}
        _matches_process(process, owner, state)
        write_json(state.file("stop.json"), {"pid": process.identity["pid"], "runtime_instance": owner["runtime_instance"]})
        if not process.wait(round(timeout * 1000)):
            raise TimeoutError("Broker has not exited; ownership retained. No process was forcibly terminated.")
        _cleanup(state, owner)
        return {"status": "stopped", "pid": process.identity["pid"]}


def login_command(config: Config) -> str:
    return subprocess.list2cmdline(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                                  "-File", str(config.root / "native/runtime/Start-Runtime.ps1"), "-Config", str(config.path)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "verify", "start", "status", "stop", "print-login-command"))
    parser.add_argument("--config", type=Path, default=ROOT / "runtime-config.json")
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--disable-learning", action="store_true", help="configure a conversion-only broker")
    parser.add_argument("--learning-file", type=Path, help="configure an explicit absolute store, e.g. isolated test memory")
    args = parser.parse_args()
    if args.command == "configure":
        if args.install_root is None or args.python is None:
            parser.error("configure requires --install-root and --python")
        value = configure(args.install_root, args.python, args.config, learning_enabled=not args.disable_learning,
                          learning_file=args.learning_file)
        print(json.dumps({"status": "configured", "config": str(args.config.resolve()),
                          "dictionary_sha256": value["expected_dictionary_sha256"]}))
        return
    config = load_config(args.config)
    if args.command == "print-login-command":
        print(login_command(config))
        return
    if args.command == "verify":
        verify_config(config)
        print(json.dumps({"status": "verified", "install_root": str(config.root), "python": str(config.python)}))
        return
    api = Windows()
    state = user_state(api)
    with api.lock(state):
        result = {"start": start, "status": status, "stop": stop}[args.command](config, api, state)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
