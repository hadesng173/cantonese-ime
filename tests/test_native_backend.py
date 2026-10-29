"""Pure mocked broker tests: no Windows DLL, pipe, server or engine is opened."""
from __future__ import annotations

import ctypes
import io
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock

from cantonese_ime.native_protocol import (
    LookupRequest, Status, decode_response, encode_request, frame,
)
from native.backend import server


class FakeKernel:
    def __init__(self):
        self.calls = []

    def DisconnectNamedPipe(self, handle):
        self.calls.append(("disconnect", handle))
        return True

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return True

    def LocalFree(self, descriptor):
        self.calls.append(("free_security", descriptor))
        return None


class FakeEngine:
    def __init__(self, api):
        self.api = api
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        # The real engine must remain alive until every transport worker exits.
        assert not self.api.active
        self.closed = True
        self.api.kernel.calls.append(("engine_closed", None))

    def lookup_page(self, raw, **kwargs):
        assert not self.closed
        return types.SimpleNamespace(next_offset=None, completion_truncated=False,
                                     candidates=(types.SimpleNamespace(text="你好", reading="nei5 hou2"),))


class FakeApi:
    def __init__(self, *, fail_create_at=None):
        self.kernel = FakeKernel()
        self.created = []
        self.active = set()
        self.lock = threading.Lock()
        self.all_connected = threading.Event()
        self.fail_create_at = fail_create_at
        self.callbacks = {}

    def identity(self):
        return "S-1-5-21-test", 7

    def authenticate_client(self, handle, expected_identity):
        assert expected_identity == self.identity()

    def security(self, sid):
        return "descriptor", "attributes"

    def create_pipe(self, name, attributes, *, first):
        if self.fail_create_at == len(self.created):
            raise OSError("mock create failure")
        if len(self.created) == server.MAX_PIPE_INSTANCES:
            raise AssertionError("Attempted a seventeenth pipe instance")
        handle = 100 + len(self.created)
        self.created.append((handle, first))
        return handle

    def connect(self, handle, stop):
        with self.lock:
            self.active.add(handle)
            if len(self.active) == server.MAX_PIPE_INSTANCES:
                self.all_connected.set()

    def read_exact(self, handle, size, stop):
        callback = self.callbacks.get("read")
        if callback:
            return callback(handle, size, stop)
        stop.wait(3)
        with self.lock:
            self.active.discard(handle)
        raise InterruptedError("mock cancellation")

    def write_all(self, handle, data, stop):
        callback = self.callbacks.get("write")
        if callback:
            callback(handle, data, stop)


class RuntimeStopTests(unittest.TestCase):
    def test_stop_request_requires_both_current_pid_and_launch_nonce(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stop.json"
            instance = str(uuid.uuid4())
            stop = threading.Event()
            states = [
                {"pid": server.os.getpid(), "runtime_instance": str(uuid.uuid4())},
                {"pid": server.os.getpid() + 1, "runtime_instance": instance},
                ["not an object"],
                {"pid": server.os.getpid(), "runtime_instance": instance},
            ]
            observed = []

            def wait(_seconds):
                observed.append(stop.is_set())
                if stop.is_set():
                    return True
                path.write_text(json.dumps(states.pop(0)), encoding="utf-8")
                return False

            with mock.patch.object(stop, "wait", side_effect=wait):
                server._watch_stop_file(path, instance, stop)
            self.assertEqual(observed, [False, False, False, False, True])

    def test_invalid_runtime_parameters_are_rejected_before_win32(self):
        paths = [Path("dictionary"), Path("ready"), Path("log")]
        with mock.patch.object(server, "Win32") as api:
            with self.assertRaisesRegex(ValueError, "together"):
                server.serve(*paths, runtime_instance=str(uuid.uuid4()))
            with self.assertRaisesRegex(ValueError, "together"):
                server.serve(*paths, stop_file=Path("stop"))
            with self.assertRaises(ValueError):
                server.serve(*paths, runtime_instance="bad", stop_file=Path("stop"))
            with self.assertRaisesRegex(ValueError, "separate"):
                server.serve(*paths, runtime_instance=str(uuid.uuid4()), stop_file=paths[0])
            with self.assertRaisesRegex(ValueError, "separate"):
                server.serve(*paths, learning_file=paths[0])
            api.assert_not_called()

    def test_explicit_learning_path_is_passed_to_engine_and_reported_ready(self):
        api = FakeApi()
        engine = FakeEngine(api)
        snapshots = []

        def factory(_database, *, mode, learning_path):
            self.assertEqual(mode, "forgiving")
            self.assertEqual(learning_path, learning)
            engine.learning_path = learning_path
            return engine

        def pool(_api, _handles, _engine, stop, _log, publish, **_kwargs):
            publish()
            snapshots.append(json.loads(ready.read_text(encoding="utf-8")))
            stop.set()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, ready, log, learning = (root / name for name in ("db", "ready", "log", "learning"))
            database.write_bytes(b"fixture")
            with mock.patch.object(server, "Win32", return_value=api), \
                 mock.patch.object(server, "Engine", side_effect=factory), \
                 mock.patch.object(server, "_run_pool", side_effect=pool):
                server.serve(database, ready, log, learning_file=learning)
        self.assertTrue(snapshots[0]["learning_requested"])
        self.assertTrue(snapshots[0]["learning_enabled"])
        self.assertEqual(snapshots[0]["learning_file"], str(learning.resolve()))

    def test_cleanup_cannot_remove_successor_nonce_even_for_same_pid_and_pipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ready.json"
            old, current = str(uuid.uuid4()), str(uuid.uuid4())
            value = {"pid": server.os.getpid(), "pipe": "same", "runtime_instance": current}
            path.write_text(json.dumps(value), encoding="utf-8")
            server._remove_owned_ready_file(path, "same", old)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), value)
            server._remove_owned_ready_file(path, "same", current)
            self.assertFalse(path.exists())

    def test_controller_launch_publishes_nonce_and_joins_stop_watcher(self):
        api = FakeApi()
        engine = FakeEngine(api)
        instance = str(uuid.uuid4())
        seen = []

        def pool(_api, _handles, _engine, stop, _log, publish, **_kwargs):
            publish()
            seen.append(json.loads(ready.read_text(encoding="utf-8"))["runtime_instance"])
            server.write_json(stop_file, {"pid": server.os.getpid(), "runtime_instance": instance})
            self.assertTrue(stop.wait(2), "Graceful stop request was not observed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, ready, log, stop_file = (root / name for name in ("db", "ready", "log", "stop"))
            database.write_bytes(b"fake dictionary")
            with mock.patch.object(server, "Win32", return_value=api), \
                 mock.patch.object(server, "Engine", return_value=engine), \
                 mock.patch.object(server, "_run_pool", side_effect=pool):
                server.serve(database, ready, log, runtime_instance=instance, stop_file=stop_file)
            self.assertFalse(ready.exists())
        self.assertEqual(seen, [instance])
        self.assertTrue(engine.closed)
        self.assertFalse(any(thread.name == "CantoneseImeStop" for thread in threading.enumerate()))


class PoolLifecycleTests(unittest.TestCase):
    def paths(self, temporary):
        root = Path(temporary)
        dictionary = root / "tiny.sqlite3"
        dictionary.write_bytes(b"fake dictionary; never opened")
        return dictionary, root / "ready.json", root / "broker.jsonl"

    def test_sixteen_connected_clients_do_not_allocate_seventeenth_listener(self):
        api = FakeApi()
        stop = threading.Event()
        engine = FakeEngine(api)
        observed = []

        def release_when_full():
            observed.append(api.all_connected.wait(3))
            stop.set()

        helper = threading.Thread(target=release_when_full)
        helper.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                database, ready, log = self.paths(temporary)
                with mock.patch.object(server, "Win32", return_value=api), \
                     mock.patch.object(server, "Engine", return_value=engine):
                    server.serve(database, ready, log, stop_event=stop)
                self.assertFalse(ready.exists())
        finally:
            stop.set()
            helper.join()
        self.assertEqual(observed, [True])
        self.assertEqual(len(api.created), 16)
        self.assertEqual([first for _, first in api.created], [True] + [False] * 15)
        self.assertTrue(engine.closed)
        self.assertFalse(api.active)
        close_calls = [x for x in api.kernel.calls if x[0] == "close"]
        self.assertEqual(close_calls, [("close", handle) for handle, _ in api.created])
        close_index = next(i for i, item in enumerate(api.kernel.calls) if item[0] == "close")
        self.assertLess(api.kernel.calls.index(("engine_closed", None)), close_index)
        self.assertEqual(api.kernel.calls[-1], ("free_security", "descriptor"))

    def test_pipe_security_allows_sandboxed_access_pending_message_authentication(self):
        sddl = server.pipe_security_sddl("S-1-5-21-1-2-3-1001")
        dacl, sacl = sddl.split("S:")
        self.assertTrue(dacl.startswith("D:P"))  # protected: no inherited ACEs
        self.assertEqual(set(dacl[3:].strip("()").split(")(")), {
            "A;;GA;;;SY", "A;;GA;;;S-1-5-21-1-2-3-1001", "A;;GRGW;;;AC", "A;;GRGW;;;S-1-15-2-2"})
        self.assertEqual(sacl, "(ML;;NW;;;LW)")
        for broad in ("WD", "AN", "AU", "BU", "NU"):
            self.assertNotIn(f";;;{broad})", dacl)

    def test_partial_pipe_creation_failure_closes_owned_handles_and_security(self):
        api = FakeApi(fail_create_at=3)
        with tempfile.TemporaryDirectory() as temporary:
            database, ready, log = self.paths(temporary)
            with mock.patch.object(server, "Win32", return_value=api), \
                 mock.patch.object(server, "Engine") as engine:
                with self.assertRaisesRegex(OSError, "mock create failure"):
                    server.serve(database, ready, log)
                engine.assert_not_called()
        self.assertEqual(api.kernel.calls, [("close", x) for x, _ in api.created]
                         + [("free_security", "descriptor")])

    def test_log_directory_failure_still_frees_security_descriptor(self):
        api = FakeApi()
        with tempfile.TemporaryDirectory() as temporary:
            database, ready, log = self.paths(temporary)
            blocker = Path(temporary) / "file-not-directory"
            blocker.write_text("keep", encoding="utf-8")
            with mock.patch.object(server, "Win32", return_value=api):
                with self.assertRaises(OSError):
                    server.serve(database, ready, blocker / log.name)
        self.assertEqual(api.created, [])
        self.assertEqual(api.kernel.calls, [("free_security", "descriptor")])

    def test_failed_thread_start_does_not_close_engine_under_existing_worker(self):
        api = FakeApi()
        stop = threading.Event()
        engine = FakeEngine(api)
        real_start = threading.Thread.start
        starts = []

        def start(thread):
            if not thread.name.startswith("CantoneseImePipe-"):
                return real_start(thread)
            starts.append(thread)
            if len(starts) == 2:
                raise RuntimeError("mock start failure")
            real_start(thread)

        with tempfile.TemporaryDirectory() as temporary:
            database, ready, log = self.paths(temporary)
            with mock.patch.object(server, "Win32", return_value=api), \
                 mock.patch.object(server, "Engine", return_value=engine), \
                 mock.patch.object(threading.Thread, "start", start):
                with self.assertRaisesRegex(RuntimeError, "mock start failure"):
                    server.serve(database, ready, log, stop_event=stop)
        self.assertTrue(engine.closed)
        self.assertTrue(stop.is_set())
        self.assertFalse(starts[0].is_alive())
        self.assertFalse(api.active)
        self.assertEqual(sum(x[0] == "close" for x in api.kernel.calls), 16)

    def test_protocol_frame_roundtrip_through_mocked_worker(self):
        api = FakeApi()
        stop = threading.Event()
        engine = FakeEngine(api)
        request = frame(encode_request(LookupRequest(42, 0, 0, 9, "neihou")))
        remaining = bytearray(request)
        writes = []

        def read(handle, size, stopping):
            if not remaining:
                stop.set()
                with api.lock:
                    api.active.discard(handle)
                raise EOFError()
            result = bytes(remaining[:size])
            del remaining[:size]
            return result

        api.callbacks["read"] = read
        api.callbacks["write"] = lambda handle, data, stopping: writes.append(data)
        server._run_pool(api, [100], engine, stop, lambda *_args, **_kwargs: None, lambda: None)
        self.assertEqual(len(writes), 1)
        size = server.decode_frame_length(writes[0][:4])
        self.assertEqual(size, len(writes[0]) - 4)
        response = decode_response(writes[0][4:], expected_request_id=42)
        self.assertEqual(response.status, Status.OK)
        self.assertEqual(response.candidates[0].text, "你好")

    def test_bad_client_frame_releases_and_reuses_same_pipe(self):
        api = FakeApi()
        stop = threading.Event()
        engine = FakeEngine(api)
        request = frame(encode_request(LookupRequest(9, 1, 0, 9, "neihou")))
        buffers = [bytearray(b"\0\0\0\0"), bytearray(request)]
        connections = []
        writes = []

        def connect(handle, stopping):
            connections.append(handle)

        def read(_handle, size, _stop):
            current = buffers[min(len(connections) - 1, 1)]
            part = bytes(current[:size])
            del current[:size]
            return part

        def write(_handle, data, _stop):
            writes.append(data)
            stop.set()

        api.connect = connect
        api.callbacks.update(read=read, write=write)
        server._run_pool(api, [100], engine, stop, lambda *_a, **_k: None, lambda: None)
        self.assertEqual(connections, [100, 100])
        self.assertEqual(sum(x == ("disconnect", 100) for x in api.kernel.calls), 2)
        self.assertEqual(decode_response(writes[0][4:]).request_id, 9)

    def test_ready_publication_failure_joins_before_engine_close(self):
        api = FakeApi()
        engine = FakeEngine(api)
        with tempfile.TemporaryDirectory() as temporary:
            database, ready, log = self.paths(temporary)
            with mock.patch.object(server, "Win32", return_value=api), \
                 mock.patch.object(server, "Engine", return_value=engine), \
                 mock.patch.object(server, "write_json", side_effect=OSError("mock ready failure")):
                with self.assertRaisesRegex(OSError, "mock ready failure"):
                    server.serve(database, ready, log)
        self.assertTrue(engine.closed)
        self.assertFalse(api.active)
        self.assertEqual(sum(x[0] == "close" for x in api.kernel.calls), 16)

    def test_foreign_ready_record_is_not_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready.json"
            ready.write_text(json.dumps({"pid": -1, "pipe": "foreign"}), encoding="utf-8")
            before = ready.read_bytes()
            server._remove_owned_ready_file(ready, "ours")
            self.assertEqual(ready.read_bytes(), before)

    def test_output_collision_rejected_before_constructing_windows_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, ready, log = self.paths(temporary)
            with mock.patch.object(server, "Win32") as api:
                with self.assertRaises(ValueError):
                    server.serve(database, database, log)
                with self.assertRaises(ValueError):
                    server.serve(database, ready, ready)
                api.assert_not_called()


class AsyncLogTests(unittest.TestCase):
    class BlockedOutput(io.StringIO):
        def __init__(self):
            super().__init__()
            self.blocked = threading.Event()
            self.release = threading.Event()
            self.snapshot = ""

        def write(self, text):
            if not self.blocked.is_set():
                self.blocked.set()
                self.release.wait(3)
            return super().write(text)

        def close(self):
            self.snapshot = self.getvalue()
            super().close()

    def test_blocked_log_disk_does_not_prevent_seventeenth_reconnect(self):
        output = self.BlockedOutput()
        api = FakeApi()
        stop = threading.Event()
        connections = []
        rearmed = threading.Event()

        def connect(handle, stopping):
            connections.append(handle)
            if len(connections) == 17:
                rearmed.set()
                stop.set()

        api.connect = connect
        # A malformed frame makes each fake client leave; one pipe is reused.
        api.callbacks["read"] = lambda *_: b"\0\0\0\0"
        with mock.patch.object(Path, "open", return_value=output):
            log = server._AsyncLog(Path("mock-only.jsonl"))
            log.__enter__()
            try:
                log("forced_slow_disk")
                self.assertTrue(output.blocked.wait(1))
                server._run_pool(api, [100], FakeEngine(api), stop, log, lambda: None)
                self.assertTrue(rearmed.is_set())
                self.assertEqual(len(connections), 17)
                self.assertFalse(output.release.is_set())
            finally:
                output.release.set()
                log.__exit__(None, None, None)
        self.assertTrue(output.closed)
        self.assertEqual(sum(json.loads(line)["event"] == "client_closed"
                             for line in output.snapshot.splitlines()), 17)

    def test_queue_is_bounded_and_records_diagnostic_loss_explicitly(self):
        output = self.BlockedOutput()
        with mock.patch.object(Path, "open", return_value=output):
            log = server._AsyncLog(Path("mock-only.jsonl"), capacity=2)
            log.__enter__()
            try:
                log("writing")
                self.assertTrue(output.blocked.wait(1))
                for _ in range(3):
                    log("queued")
                self.assertEqual(log.queue.qsize(), 2)
            finally:
                output.release.set()
                log.__exit__(None, None, None)
        records = [json.loads(line) for line in output.snapshot.splitlines()]
        self.assertEqual(records[-1]["event"], "log_records_dropped")
        self.assertEqual(records[-1]["count"], 1)


class OverlappedTests(unittest.TestCase):
    def api(self):
        # Deliberately bypass Win32.__init__: no WinDLL is loaded.
        api = server.Win32.__new__(server.Win32)
        api.kernel = mock.Mock()
        api.kernel.CreateEventW.return_value = 99
        return api

    def test_connect_error_preserved_before_event_close_overwrites_last_error(self):
        api = self.api()
        state = {"error": 5}
        api.kernel.ConnectNamedPipe.return_value = False
        api.kernel.CloseHandle.side_effect = lambda _handle: state.update(error=6)
        with mock.patch.object(server, "_last_error", side_effect=lambda: state["error"]):
            with self.assertRaises(OSError) as result:
                api.connect(123, threading.Event())
        self.assertEqual(result.exception.winerror, 5)
        api.kernel.CloseHandle.assert_called_once_with(99)
        api.kernel.GetOverlappedResult.assert_not_called()

    def test_client_connected_before_connect_call_is_success(self):
        api = self.api()
        api.kernel.ConnectNamedPipe.return_value = False
        with mock.patch.object(server, "_last_error", return_value=server.ERROR_PIPE_CONNECTED):
            api.connect(123, threading.Event())
        api.kernel.GetOverlappedResult.assert_not_called()
        api.kernel.CloseHandle.assert_called_once_with(99)

    def test_cancel_drains_pending_operation_before_event_is_closed(self):
        api = self.api()
        stop = threading.Event()
        order = []
        api.kernel.ConnectNamedPipe.return_value = False

        def wait(*_):
            stop.set()
            return server.WAIT_TIMEOUT

        api.kernel.WaitForSingleObject.side_effect = wait
        api.kernel.CancelIoEx.side_effect = lambda *_: order.append("cancel") or True
        api.kernel.GetOverlappedResult.side_effect = lambda *_: order.append("drain") or False
        api.kernel.CloseHandle.side_effect = lambda *_: order.append("close") or True
        with mock.patch.object(server, "_last_error", return_value=server.ERROR_IO_PENDING):
            with self.assertRaises(InterruptedError):
                api.connect(123, stop)
        self.assertEqual(order, ["cancel", "drain", "close"])
        self.assertTrue(api.kernel.GetOverlappedResult.call_args.args[-1])

    def test_immediate_read_handles_partial_transfers_without_losing_bytes(self):
        api = self.api()
        source = bytearray(b"abcdef")
        latest = {"count": 0}

        def read(_handle, buffer, maximum, _received, _operation):
            part = bytes(source[:min(2, maximum)])
            del source[:len(part)]
            ctypes.memmove(buffer, part, len(part))
            latest["count"] = len(part)
            return True

        def result(_handle, _operation, transferred, _wait):
            transferred._obj.value = latest["count"]
            return True

        api.kernel.ReadFile.side_effect = read
        api.kernel.GetOverlappedResult.side_effect = result
        self.assertEqual(api.read_exact(123, 6, threading.Event()), b"abcdef")
        self.assertEqual(api.kernel.CloseHandle.call_count, 3)

    def test_immediate_write_retries_only_unwritten_tail(self):
        api = self.api()
        received = bytearray()
        latest = {"count": 0}

        def write(_handle, buffer, maximum, _sent, _operation):
            size = min(maximum, 2)
            received.extend(ctypes.string_at(buffer, size))
            latest["count"] = size
            return True

        def result(_handle, _operation, transferred, _wait):
            transferred._obj.value = latest["count"]
            return True

        api.kernel.WriteFile.side_effect = write
        api.kernel.GetOverlappedResult.side_effect = result
        api.write_all(123, b"abcdef", threading.Event())
        self.assertEqual(received, b"abcdef")
        self.assertEqual(api.kernel.CloseHandle.call_count, 3)

    def test_stop_before_io_does_not_allocate_event(self):
        api = self.api()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            api.connect(123, stop)
        api.kernel.CreateEventW.assert_not_called()

    def test_expired_transfer_cancels_and_drains_instead_of_freeing_pending_buffer(self):
        api = self.api()
        api.kernel.ReadFile.return_value = False
        order = []
        api.kernel.CancelIoEx.side_effect = lambda *_: order.append("cancel") or True
        api.kernel.GetOverlappedResult.side_effect = lambda *_: order.append("drain") or False
        api.kernel.CloseHandle.side_effect = lambda *_: order.append("close") or True
        with mock.patch.object(server, "_last_error", return_value=server.ERROR_IO_PENDING), \
             mock.patch.object(server.time, "monotonic", return_value=100):
            with self.assertRaises(TimeoutError):
                api._io(123, lambda operation, count: False, threading.Event(), 99)
        self.assertEqual(order, ["cancel", "drain", "close"])


if __name__ == "__main__":
    unittest.main()
