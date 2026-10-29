"""Local same-user Windows named-pipe broker for the native TSF frontend.

This module does not register an IME or start itself on login. Use the explicit
project launcher after compiling and reviewing the native component.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import struct
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cantonese_ime import Engine
from cantonese_ime.dictionary import file_sha256
from cantonese_ime.native_protocol import decode_frame_length, dispatch, encode_response, frame
from cantonese_ime.prediction import PredictionStore


MAX_PIPE_INSTANCES = 16
IO_TIMEOUT_SECONDS = 5.0
WAIT_SLICE_MS = 100
ERROR_IO_PENDING = 997
ERROR_PIPE_CONNECTED = 535
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
TOKEN_QUERY = 0x0008
TOKEN_USER = 1
TOKEN_SESSION_ID = 12
ERROR_INSUFFICIENT_BUFFER = 122


def pipe_security_sddl(sid: str) -> str:
    """The connected user and SYSTEM, plus read/write for sandboxed apps.

    AppContainer (ALL APPLICATION PACKAGES / ALL RESTRICTED APPLICATION
    PACKAGES) hosts run with the same user SID but need their own ACE, and a
    low mandatory label lets their low-integrity token write. No Everyone or
    anonymous ACE; remote clients stay rejected by the pipe mode. Package
    ACEs can admit another user's package, so every message is separately
    authenticated against the broker's user SID and Windows session.
    """
    return (f"D:P(A;;GA;;;SY)(A;;GA;;;{sid})(A;;GRGW;;;AC)(A;;GRGW;;;S-1-15-2-2)"
            "S:(ML;;NW;;;LW)")


class _AsyncLog:
    """Keep file opens/writes out of pipe worker reconnect and request paths."""

    def __init__(self, path: Path, *, capacity: int = 4096):
        self.path = path
        self.queue = queue.Queue(maxsize=capacity)
        self.closing = threading.Event()
        self.drop_lock = threading.Lock()
        self.dropped = 0
        self.failure: OSError | None = None
        self.output = None
        self.thread = None

    def __enter__(self):
        self.output = self.path.open("a", encoding="utf-8")
        self.thread = threading.Thread(target=self._write, name="CantoneseImeLog", daemon=False)
        try:
            self.thread.start()
        except BaseException:
            self.output.close()
            raise
        return self

    def __call__(self, event: str, **fields):
        # No input/candidate/document/learning text is supplied by callers.
        record = {"time_utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with self.drop_lock:
                self.dropped += 1

    def _write(self):
        try:
            while not self.closing.is_set() or not self.queue.empty():
                try:
                    record = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                self.output.write(json.dumps(record, ensure_ascii=True) + "\n")
                self.output.flush()
            with self.drop_lock:
                dropped = self.dropped
            if dropped:
                self.output.write(json.dumps({"time_utc": datetime.now(timezone.utc).isoformat(),
                                              "event": "log_records_dropped", "count": dropped}) + "\n")
                self.output.flush()
        except OSError as error:
            self.failure = error

    def __exit__(self, exc_type, _value, _traceback):
        self.closing.set()
        self.thread.join()
        self.output.close()
        if self.failure is not None and exc_type is None:
            raise self.failure


def _last_error() -> int:
    return ctypes.get_last_error()


def _win_error(code: int) -> OSError:
    # The fallback lets mocked transport tests run on non-Windows CI too.
    if hasattr(ctypes, "WinError"):
        return ctypes.WinError(code)
    error = OSError(code, f"Windows error {code}")
    error.winerror = code
    return error


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", w.LPVOID), ("Attributes", w.DWORD)]


class Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]


class Win32:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("The named-pipe broker requires Windows.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = w.HANDLE
        self.kernel.GetCurrentProcess.argtypes = []
        self.kernel.GetCurrentThread.restype = w.HANDLE
        self.kernel.GetCurrentThread.argtypes = []
        self.kernel.GetCurrentProcessId.restype = w.DWORD
        self.kernel.GetCurrentProcessId.argtypes = []
        self.kernel.ProcessIdToSessionId.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
        self.kernel.ProcessIdToSessionId.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.kernel.LocalFree.argtypes = [w.HLOCAL]
        self.kernel.LocalFree.restype = w.HLOCAL
        self.advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        self.advapi.OpenProcessToken.restype = w.BOOL
        self.advapi.ImpersonateNamedPipeClient.argtypes = [w.HANDLE]
        self.advapi.ImpersonateNamedPipeClient.restype = w.BOOL
        self.advapi.OpenThreadToken.argtypes = [w.HANDLE, w.DWORD, w.BOOL, ctypes.POINTER(w.HANDLE)]
        self.advapi.OpenThreadToken.restype = w.BOOL
        self.advapi.RevertToSelf.argtypes = []
        self.advapi.RevertToSelf.restype = w.BOOL
        self.advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]
        self.advapi.GetTokenInformation.restype = w.BOOL
        self.advapi.ConvertSidToStringSidW.argtypes = [w.LPVOID, ctypes.POINTER(w.LPWSTR)]
        self.advapi.ConvertSidToStringSidW.restype = w.BOOL
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)]
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
        self.kernel.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(SecurityAttributes)]
        self.kernel.CreateNamedPipeW.restype = w.HANDLE
        self.kernel.ConnectNamedPipe.argtypes = [w.HANDLE, ctypes.POINTER(Overlapped)]
        self.kernel.ConnectNamedPipe.restype = w.BOOL
        self.kernel.DisconnectNamedPipe.argtypes = [w.HANDLE]
        self.kernel.DisconnectNamedPipe.restype = w.BOOL
        self.kernel.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped)]
        self.kernel.ReadFile.restype = w.BOOL
        self.kernel.WriteFile.argtypes = [w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped)]
        self.kernel.WriteFile.restype = w.BOOL
        self.kernel.CreateEventW.argtypes = [w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR]
        self.kernel.CreateEventW.restype = w.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.kernel.WaitForSingleObject.restype = w.DWORD
        self.kernel.GetOverlappedResult.argtypes = [w.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD), w.BOOL]
        self.kernel.GetOverlappedResult.restype = w.BOOL
        self.kernel.CancelIoEx.argtypes = [w.HANDLE, ctypes.POINTER(Overlapped)]
        self.kernel.CancelIoEx.restype = w.BOOL

    def checked(self, value):
        if not value:
            raise _win_error(_last_error())
        return value

    def _token_identity(self, token) -> tuple[str, int]:
        length = w.DWORD()
        first = self.advapi.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(length))
        code = _last_error() if not first else 0
        if not first and code != ERROR_INSUFFICIENT_BUFFER:
            raise _win_error(code)
        if not ctypes.sizeof(SidAndAttributes) <= length.value <= 65_536:
            raise ValueError("Token user information has an invalid size.")
        information = ctypes.create_string_buffer(length.value)
        self.checked(self.advapi.GetTokenInformation(
            token, TOKEN_USER, information, len(information), ctypes.byref(length)))
        sid = ctypes.cast(information, ctypes.POINTER(SidAndAttributes)).contents.Sid
        sid_string = w.LPWSTR()
        self.checked(self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)))
        try:
            user_sid = sid_string.value
        finally:
            if self.kernel.LocalFree(ctypes.cast(sid_string, w.HLOCAL)):
                raise _win_error(_last_error())
        if not user_sid:
            raise ValueError("Token user SID is empty.")
        session = w.DWORD()
        self.checked(self.advapi.GetTokenInformation(
            token, TOKEN_SESSION_ID, ctypes.byref(session), ctypes.sizeof(session), ctypes.byref(length)))
        if length.value != ctypes.sizeof(session):
            raise ValueError("Token session information has an invalid size.")
        return user_sid, session.value

    def identity(self) -> tuple[str, int]:
        token = w.HANDLE()
        self.checked(self.advapi.OpenProcessToken(
            self.kernel.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)))
        try:
            return self._token_identity(token)
        finally:
            self.checked(self.kernel.CloseHandle(token))

    def authenticate_client(self, handle, expected_identity: tuple[str, int]):
        """Check the last message's token, then revert before any dispatch/I/O.

        OpenAsSelf permits querying an identification-level AppContainer token;
        no engine, file or logging operation runs under the client's identity.
        A failed reversion makes the process unsafe, so exit without cleanup.
        """
        self.checked(self.advapi.ImpersonateNamedPipeClient(handle))
        token = w.HANDLE()
        try:
            self.checked(self.advapi.OpenThreadToken(
                self.kernel.GetCurrentThread(), TOKEN_QUERY, True, ctypes.byref(token)))
            if not token.value:
                raise ValueError("Client token handle is empty.")
            client_identity = self._token_identity(token)
        finally:
            if not self.advapi.RevertToSelf():
                os._exit(1)
                raise SystemExit(1)  # Defensive only: os._exit never returns.
            if token.value:
                self.checked(self.kernel.CloseHandle(token))
        if client_identity != expected_identity:
            raise PermissionError("Pipe client user or session does not match the broker.")

    def security(self, sid: str):
        descriptor = w.LPVOID()
        self.checked(self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            pipe_security_sddl(sid), 1, ctypes.byref(descriptor), None))
        return descriptor, SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)

    def create_pipe(self, name: str, attributes, *, first: bool):
        handle = self.kernel.CreateNamedPipeW(
            name, 0x00000003 | 0x40000000 | (0x00080000 if first else 0),
            0x00000008, MAX_PIPE_INSTANCES, 65_536, 65_536, 250, ctypes.byref(attributes),
        )  # duplex, byte/wait mode, reject remote clients
        if handle == ctypes.c_void_p(-1).value:
            raise _win_error(_last_error())
        return handle

    def _io(self, handle, start, stop: threading.Event, deadline: float | None,
            *, allow_connected: bool = False) -> int:
        """Keep OVERLAPPED/event/buffers alive until completion, including cancel.

        Only this worker owns I/O on its pipe. The pool closes the pipe after
        the worker exits. No pending operation can reference a freed event or
        ctypes buffer, and shutdown never closes SQLite under a live request.
        """
        if stop.is_set():
            raise InterruptedError("Broker stopping.")
        event = self.checked(self.kernel.CreateEventW(None, True, False, None))
        operation = Overlapped(hEvent=event)
        transferred = w.DWORD()
        pending = False
        try:
            started = start(ctypes.byref(operation), ctypes.byref(transferred))
            # Capture once, before any cleanup or other Windows call changes it.
            code = _last_error() if not started else 0
            if not started and allow_connected and code == ERROR_PIPE_CONNECTED:
                return 0
            if not started and code != ERROR_IO_PENDING:
                raise _win_error(code)
            pending = not started
            if pending:
                while True:
                    if stop.is_set():
                        raise InterruptedError("Broker stopping.")
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("Pipe operation timed out.")
                    waited = self.kernel.WaitForSingleObject(event, WAIT_SLICE_MS)
                    if waited == WAIT_OBJECT_0:
                        break
                    if waited == WAIT_FAILED:
                        raise _win_error(_last_error())
                    if waited != WAIT_TIMEOUT:
                        raise OSError("Unexpected pipe event wait result.")
            # Read/write byte counts are authoritative after completion. This
            # also handles an operation that completed before its first wait.
            complete = self.kernel.GetOverlappedResult(handle, ctypes.byref(operation),
                                                       ctypes.byref(transferred), False)
            code = _last_error() if not complete else 0
            pending = not complete and code == 996  # ERROR_IO_INCOMPLETE still needs cancellation.
            if not complete:
                raise _win_error(code)
            return transferred.value
        finally:
            if pending:
                # CancelIoEx alone is insufficient: drain completion before
                # releasing OVERLAPPED, event or the caller's ctypes buffer.
                self.kernel.CancelIoEx(handle, ctypes.byref(operation))
                self.kernel.GetOverlappedResult(handle, ctypes.byref(operation),
                                                ctypes.byref(transferred), True)
            self.kernel.CloseHandle(event)

    def connect(self, handle, stop: threading.Event):
        self._io(handle, lambda operation, count: self.kernel.ConnectNamedPipe(handle, operation),
                 stop, None, allow_connected=True)

    def read_exact(self, handle, size: int, stop: threading.Event) -> bytes:
        chunks = []
        remaining = size
        deadline = time.monotonic() + IO_TIMEOUT_SECONDS
        while remaining:
            buffer = ctypes.create_string_buffer(min(remaining, 65_536))
            count = self._io(handle, lambda operation, received: self.kernel.ReadFile(
                handle, buffer, len(buffer), received, operation), stop, deadline)
            if not 0 < count <= len(buffer):
                raise EOFError("Pipe closed.")
            chunks.append(buffer.raw[:count])
            remaining -= count
        return b"".join(chunks)

    def write_all(self, handle, data: bytes, stop: threading.Event):
        position = 0
        deadline = time.monotonic() + IO_TIMEOUT_SECONDS
        while position < len(data):
            chunk = data[position:position + 65_536]
            buffer = ctypes.create_string_buffer(chunk)
            count = self._io(handle, lambda operation, sent: self.kernel.WriteFile(
                handle, buffer, len(chunk), sent, operation), stop, deadline)
            if not 0 < count <= len(chunk):
                raise EOFError("Pipe closed.")
            position += count


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _run_pool(api, handles: list, engine, stop: threading.Event, log, publish_ready, predictions=None,
              *, expected_identity: tuple[str, int] | None = None):
    """Reuse a fixed pipe pool; never allocate instance17 after client16.

    Handles stay owned by serve(), workers own only their in-flight operations.
    Join every worker before the Engine context or pipe/security owners exit.
    """
    workers = []
    failures: list[Exception] = []
    failure_lock = threading.Lock()
    if expected_identity is None:
        expected_identity = api.identity()

    def raise_worker_failure():
        if failures:
            # No payload, candidate, path or exception text is written to logs.
            log("worker_failed", exception_type=type(failures[0]).__name__,
                winerror=getattr(failures[0], "winerror", None))
            raise RuntimeError("A pipe worker failed.") from failures[0]

    def worker(handle):
        try:
            while not stop.is_set():
                requests = 0
                connected = False
                try:
                    api.connect(handle, stop)
                    connected = True
                    while not stop.is_set():
                        length = decode_frame_length(api.read_exact(handle, 4, stop))
                        payload = api.read_exact(handle, length, stop)
                        # Impersonation identifies the token of this message,
                        # including a client that changed identity after connect.
                        api.authenticate_client(handle, expected_identity)
                        response = (dispatch(engine, payload) if predictions is None
                                    else dispatch(engine, payload, predictions))
                        api.write_all(handle, frame(encode_response(response)), stop)
                        requests += 1
                except (OSError, EOFError, ValueError) as error:
                    # Broken/malformed/timed-out clients release their instance.
                    # Runtime/internal protocol errors are contained by dispatch.
                    if not connected and not stop.is_set() and getattr(error, "winerror", None) not in (109, 232, 233):
                        raise
                finally:
                    api.kernel.DisconnectNamedPipe(handle)
                    if connected:
                        log("client_closed", requests=requests)
                if not connected and not stop.is_set():
                    stop.wait(0.05)  # Avoid spinning on an immediate connect failure.
        except Exception as error:
            with failure_lock:
                failures.append(error)
            stop.set()

    try:
        for number, handle in enumerate(handles):
            thread = threading.Thread(target=worker, args=(handle,),
                                      name=f"CantoneseImePipe-{number}", daemon=False)
            workers.append(thread)
            thread.start()
        raise_worker_failure()
        publish_ready()
        while not stop.wait(0.25):
            if any(not thread.is_alive() for thread in workers):
                raise RuntimeError("A pipe worker stopped unexpectedly.")
        raise_worker_failure()
    finally:
        stop.set()
        for thread in workers:
            if thread.ident is not None:
                thread.join()


def _remove_owned_ready_file(path: Path, name: str, runtime_instance: str | None = None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("pid") == os.getpid() and value.get("pipe") == name
                and value.get("runtime_instance") == runtime_instance):
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _watch_stop_file(path: Path, instance: str, stop: threading.Event):
    """Accept only this launch's request; a stale file cannot stop its successor."""
    while not stop.wait(0.1):
        try:
            if path.stat().st_size > 4096:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(value, dict) and value.get("pid") == os.getpid()
                    and value.get("runtime_instance") == instance):
                stop.set()
        except (OSError, ValueError):
            continue


@contextmanager
def _conversion_engine(database: Path, learning_file: Path | None, log, custom_file: Path | None = None):
    custom = {"custom_path": custom_file} if custom_file is not None else {}
    try:
        engine = Engine(database, mode="forgiving", learning_path=learning_file, **custom)
    except (OSError, ValueError):
        if learning_file is None:
            raise
        # Preserve the original file and keep conversion usable. A second failure
        # opening the dictionary itself still propagates rather than being hidden.
        log("learning_unavailable", stage="startup")
        engine = Engine(database, mode="forgiving", learning_path=None, **custom)
    if getattr(engine, "custom_error", None):
        log("custom_entries_unavailable", stage="startup")
    with engine:
        yield engine


def serve(database: Path, ready_file: Path, log_file: Path, *,
          stop_event: threading.Event | None = None,
          runtime_instance: str | None = None, stop_file: Path | None = None,
          learning_file: Path | None = None, custom_file: Path | None = None,
          prediction_file: Path | None = None):
    outputs = ([database, ready_file, log_file] + ([stop_file] if stop_file is not None else [])
               + ([learning_file] if learning_file is not None else [])
               + ([custom_file] if custom_file is not None else [])
               + ([prediction_file] if prediction_file is not None else []))
    if len({path.resolve() for path in outputs}) != len(outputs):
        raise ValueError("Dictionary, ready file, stop file, learning, custom and prediction files and log must be separate files.")
    if (runtime_instance is None) != (stop_file is None):
        raise ValueError("Runtime instance and stop file must be supplied together.")
    if runtime_instance is not None:
        if str(uuid.UUID(runtime_instance)) != runtime_instance:
            raise ValueError("Runtime instance must be a canonical UUID.")
    api = Win32()
    sid, session_id = api.identity()
    name = rf"\\.\pipe\CantoneseIME-{sid}-{session_id}"
    stop = stop_event if stop_event is not None else threading.Event()
    descriptor = None
    handles = []
    published_ready = False
    watcher = None
    try:
        descriptor, security = api.security(sid)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with _AsyncLog(log_file) as log:
            for number in range(MAX_PIPE_INSTANCES):
                handles.append(api.create_pipe(name, security, first=number == 0))
            started = time.perf_counter()
            with _conversion_engine(database, learning_file, log, custom_file) as engine:
                identity = {
                    "kind": "Cantonese IME local broker", "pid": os.getpid(), "python": sys.executable,
                    "python_bits": struct.calcsize("P") * 8, "pipe": name, "windows_session": session_id,
                    "dictionary": str(database.resolve()), "dictionary_sha256": file_sha256(database),
                    "backend_source_sha256": file_sha256(Path(__file__)),
                    "custom_file": str(custom_file.resolve()) if custom_file is not None else None,
                    "prediction_file": str(prediction_file.resolve()) if prediction_file is not None else None,
                    "learning_requested": learning_file is not None,
                    "learning_enabled": getattr(engine, "learning_path", None) is not None,
                    "learning_file": str(learning_file.resolve()) if learning_file is not None else None,
                    "remote_clients": "rejected", "pipe_acl": "current user and SYSTEM; app packages read/write; low label",
                    "client_authentication": "every message: token user SID and Windows session must match broker",
                    "max_pipe_instances": MAX_PIPE_INSTANCES,
                    "io_timeout_seconds": IO_TIMEOUT_SECONDS,
                    "diagnostic_logging": "bounded asynchronous queue; dropped-record count on shutdown",
                    "startup_ms": round((time.perf_counter() - started) * 1000, 3),
                    "ready_at_utc": datetime.now(timezone.utc).isoformat(),
                    "runtime_instance": runtime_instance,
                }
                def publish_ready():
                    nonlocal published_ready
                    write_json(ready_file, identity)
                    published_ready = True
                    log("ready", pid=os.getpid(), bits=identity["python_bits"])

                if stop_file is not None:
                    watcher = threading.Thread(target=_watch_stop_file,
                                               args=(stop_file, runtime_instance, stop),
                                               name="CantoneseImeStop", daemon=False)
                    watcher.start()
                # Prediction memory is written only when the frontend sends
                # sequence records, which it does only with the feature on.
                predictions = PredictionStore(prediction_file) if prediction_file is not None else None
                try:
                    extra = (predictions,) if predictions is not None else ()
                    _run_pool(api, handles, engine, stop, log, publish_ready, *extra,
                              expected_identity=(sid, session_id))
                finally:
                    if predictions is not None:
                        predictions.save(force=True)
    finally:
        stop.set()
        if watcher is not None and watcher.ident is not None:
            watcher.join()
        # _run_pool drains and joins I/O before this handle/security cleanup.
        for handle in handles:
            api.kernel.CloseHandle(handle)
        if descriptor is not None:
            api.kernel.LocalFree(descriptor)
        if published_ready:
            _remove_owned_ready_file(ready_file, name, runtime_instance)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dictionary", type=Path, default=ROOT / "data/generated/lexicon.sqlite3")
    parser.add_argument("--ready-file", type=Path, default=ROOT / ".local/native/backend-ready.json")
    parser.add_argument("--log", type=Path, default=ROOT / ".local/native/backend.jsonl")
    parser.add_argument("--runtime-instance", help="Controller launch UUID; requires --stop-file.")
    parser.add_argument("--stop-file", type=Path, help="Nonce-scoped graceful stop request (optional).")
    parser.add_argument("--learning-file", type=Path, help="Optional per-user local selection memory; conversion survives save errors.")
    parser.add_argument("--custom-file", type=Path, help="Optional per-user custom phrases/spellings; read only, reloaded when changed.")
    parser.add_argument("--prediction-file", type=Path, help="Optional per-user learned next-text suggestions.")
    args = parser.parse_args()
    serve(args.dictionary, args.ready_file, args.log,
          runtime_instance=args.runtime_instance, stop_file=args.stop_file, learning_file=args.learning_file,
          custom_file=args.custom_file, prediction_file=args.prediction_file)


if __name__ == "__main__":
    main()
