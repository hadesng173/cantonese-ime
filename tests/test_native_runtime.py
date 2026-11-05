"""Runtime ownership/config tests only: no broker or Windows API is launched."""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import types
import unittest
from unittest import mock
import uuid

from native.runtime import control


def pe_bytes(machine=0x8664):
    header = bytearray(70)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    header[64:70] = b"PE\0\0" + struct.pack("<H", machine)
    return bytes(header)


class FakeProcess:
    def __init__(self, python, *, pid=77):
        self.identity = {"pid": pid, "creation_time": 987654321, "image_path": str(python),
                         "owner_sid": "S-1-5-21-1001", "windows_session": 1}
        self.waits = []
        self.wait_result = False

    def wait(self, milliseconds):
        self.waits.append(milliseconds)
        if callable(self.wait_result):
            return self.wait_result(milliseconds)
        return self.wait_result


class FakeApi:
    def __init__(self, process):
        self.current_process = process
        self.pipe = None

    def identity(self):
        return "S-1-5-21-1001", 1

    @contextmanager
    def process(self, _pid):
        yield self.current_process

    def pipe_pid(self, _pipe):
        return self.pipe


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "package with spaces"
        self.root.mkdir()
        self.python = self.root.parent / "external-python.exe"
        self.python.write_bytes(pe_bytes())
        (self.root / "cantonese_ime").mkdir()
        (self.root / "cantonese_ime/engine.py").write_text("# fixture", encoding="utf-8")
        (self.root / "cantonese_ime/__init__.py").write_text("# fixture", encoding="utf-8")
        for name in control.runtime_files(self.root):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(pe_bytes() if name.endswith(".dll") else b"fixture")
        self.database = self.root / "data/generated/lexicon.sqlite3"
        control.write_json(self.root / "data/generated/lexicon.manifest.json",
                           {"database_sha256": control.digest(self.database), "schema_version": 2})
        self.config_path = self.root / "runtime-config.json"
        with mock.patch.object(control.subprocess, "check_output", return_value='{"bits":64,"version":"fixture python"}'):
            control.configure(self.root, self.python, self.config_path)
        self.config = control.load_config(self.config_path)
        self.state = control.State(self.root.parent / "LocalAppData/CantoneseIME/Runtime/S-1-5-21-1001/session-1",
                                   "S-1-5-21-1001", 1)
        self.process = FakeProcess(self.python)
        self.api = FakeApi(self.process)

    def owner(self):
        return {"schema_version": 1, "install_root": str(self.root), "runtime_instance": str(uuid.uuid4()),
                "process": dict(self.process.identity), "runtime_fingerprint": control.fingerprint(self.config),
                "dictionary_sha256": self.config.dictionary_hash,
                "backend_source_sha256": self.config.files["native/backend/server.py"]}

    def ready(self, owner):
        value = {"pid": owner["process"]["pid"], "runtime_instance": owner["runtime_instance"],
                "pipe": self.state.pipe, "windows_session": 1, "python_bits": 64,
                "dictionary_sha256": owner["dictionary_sha256"],
                "backend_source_sha256": owner["backend_source_sha256"]}
        if "learning_requested" in owner:
            value.update({"learning_requested": owner["learning_requested"], "learning_file": owner["learning_file"]})
        return value

    def publish(self, owner):
        control.write_json(self.state.file("ownership.json"), owner)
        control.write_json(self.state.file("backend-ready.json"), self.ready(owner))
        self.api.pipe = self.process.identity["pid"]

    def spawn(self, args, **kwargs):
        self.assertEqual(args[:4], [str(self.python), "-B", "-I", "-u"])
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.assertTrue(kwargs["close_fds"])
        instance = args[args.index("--runtime-instance") + 1]
        owner = self.owner()
        owner["runtime_instance"] = instance
        owner["learning_requested"] = "--learning-file" in args
        owner["learning_file"] = args[args.index("--learning-file") + 1] if owner["learning_requested"] else None
        control.write_json(self.state.file("backend-ready.json"), self.ready(owner))
        self.api.pipe = self.process.identity["pid"]
        return types.SimpleNamespace(pid=self.process.identity["pid"])

    def test_config_pins_runtime_and_external_python_without_copying_dependency(self):
        control.verify_config(self.config)
        value = control.read_json(self.config_path)
        self.assertEqual(value["install_root"], ".")
        self.assertEqual(value["python_bits"], 64)
        self.assertEqual(value["python_executable"], str(self.python))
        self.assertEqual(set(value["files_sha256"]), control.runtime_files(self.root))
        self.assertNotIn("runtime-config.json", value["files_sha256"])

    def test_configure_cannot_overwrite_dictionary_or_python(self):
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            control.configure(self.root, self.python, self.database)
        self.assertEqual(self.database.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            control.configure(self.root, self.python, self.python)

    def bundled_python(self):
        directory = self.root / "runtime/python"
        directory.mkdir(parents=True)
        python = directory / "python.exe"
        python.write_bytes(pe_bytes())
        (directory / "python313.zip").write_bytes(b"private standard library fixture")
        (directory / "python313._pth").write_text("python313.zip\n.\n../..\n", encoding="utf-8")
        with mock.patch.object(control.subprocess, "check_output", return_value='{"bits":64,"version":"fixture python"}'):
            control.configure(self.root, python, self.config_path)
        return python

    def test_bundled_runtime_relocates_without_external_python(self):
        self.bundled_python()
        original = control.read_json(self.config_path)
        self.assertEqual(original["python_executable"], "runtime/python/python.exe")
        moved = self.root.parent / "relocated release with spaces"
        shutil.copytree(self.root, moved)
        self.python.unlink()
        loaded = control.load_config(moved / "runtime-config.json")
        self.assertEqual(loaded.python, moved / "runtime/python/python.exe")
        control.verify_config(loaded)

    def test_bundled_standard_library_and_path_policy_are_pinned(self):
        self.bundled_python()
        loaded = control.load_config(self.config_path)
        for name in ("python313.zip", "python313._pth"):
            with self.subTest(name=name):
                path = self.root / "runtime/python" / name
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                try:
                    with self.assertRaisesRegex(ValueError, "Runtime file changed"):
                        control.verify_config(loaded)
                finally:
                    path.write_bytes(original)
        (self.root / "runtime/python/python313.zip").unlink()
        with self.assertRaisesRegex(ValueError, "inventory"):
            control.load_config(self.config_path)

    def test_relative_interpreter_rejects_escape_and_unbundled_locations(self):
        value = control.read_json(self.config_path)
        for location in ("../external-python.exe", "native/python.exe", "runtime/python/../../../outside.exe"):
            with self.subTest(location=location):
                value["python_executable"] = location
                control.write_json(self.config_path, value)
                with self.assertRaises(ValueError):
                    control.load_config(self.config_path)

    def test_source_or_dictionary_modification_prevents_spawn(self):
        for name in ("native/backend/server.py", "data/generated/lexicon.sqlite3"):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                try:
                    with mock.patch.object(control.subprocess, "Popen") as spawn:
                        with self.assertRaisesRegex(ValueError, "Runtime file changed"):
                            control.start(self.config, self.api, self.state)
                        spawn.assert_not_called()
                finally:
                    path.write_bytes(original)

    def test_manifest_hash_must_agree_even_if_config_file_hash_is_updated(self):
        path = self.root / "data/generated/lexicon.manifest.json"
        control.write_json(path, {"database_sha256": "0" * 64})
        value = control.read_json(self.config_path)
        value["files_sha256"]["data/generated/lexicon.manifest.json"] = control.digest(path)
        control.write_json(self.config_path, value)
        with self.assertRaisesRegex(ValueError, "source/build manifest"):
            control.verify_config(control.load_config(self.config_path))

    def test_external_python_x86_or_changed_binary_is_rejected(self):
        self.python.write_bytes(pe_bytes(0x14c))
        with self.assertRaisesRegex(ValueError, "AMD64"):
            control.verify_config(self.config)
        self.python.write_bytes(pe_bytes() + b"modified")
        with self.assertRaisesRegex(ValueError, "External Python executable changed"):
            control.verify_config(self.config)

    def test_config_rejects_external_file_roles_and_missing_runtime_inventory(self):
        value = control.read_json(self.config_path)
        del value["files_sha256"]["native/backend/server.py"]
        value["files_sha256"]["../outside"] = "0" * 64
        control.write_json(self.config_path, value)
        with self.assertRaisesRegex(ValueError, "inventory"):
            control.load_config(self.config_path)
        with self.assertRaisesRegex(ValueError, "relative"):
            control._local_file(self.root, "../outside")

    def test_user_state_is_localappdata_sid_and_session_scoped(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root.parent / "Local AppData")}):
            state = control.user_state(self.api)
        self.assertEqual(state.directory, self.root.parent / "Local AppData/CantoneseIME/Runtime/S-1-5-21-1001/session-1")
        self.assertNotIn(str(self.root), str(state.directory))
        self.assertEqual(state.pipe, r"\\.\pipe\CantoneseIME-S-1-5-21-1001-1")
        self.assertEqual(state.learning_file, self.root.parent / "Local AppData/CantoneseIME/learning.json")
        another_session = control.State(state.directory.parent / "session-2", state.sid, 2)
        self.assertEqual(another_session.learning_file, state.learning_file)

    def test_start_waits_for_matching_nonce_and_actual_pipe_pid(self):
        with mock.patch.object(control.subprocess, "Popen", side_effect=self.spawn) as spawn:
            result = control.start(self.config, self.api, self.state)
        self.assertEqual(result["status"], "ready")
        owner = control.read_json(self.state.file("ownership.json"))
        self.assertEqual(owner["process"], self.process.identity)
        self.assertEqual(owner["runtime_fingerprint"], control.fingerprint(self.config))
        self.assertEqual(result["runtime_instance"], owner["runtime_instance"])
        self.assertTrue(owner["learning_requested"])
        self.assertEqual(owner["learning_file"], str(self.state.learning_file))
        self.assertFalse(self.state.file("stop.json").exists())
        spawn.assert_called_once()

    def test_running_same_build_reuses_broker_without_spawn(self):
        self.publish(self.owner())
        with mock.patch.object(control.subprocess, "Popen") as spawn:
            result = control.start(self.config, self.api, self.state)
        self.assertEqual(result["status"], "ready")
        spawn.assert_not_called()

    def test_learning_can_be_explicitly_disabled_without_mutating_personal_store(self):
        config = replace(self.config, learning_enabled=False)
        with mock.patch.object(control.subprocess, "Popen", side_effect=self.spawn) as spawn:
            result = control.start(config, self.api, self.state)
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("--learning-file", spawn.call_args.args[0])
        owner = control.read_json(self.state.file("ownership.json"))
        self.assertFalse(owner["learning_requested"])
        self.assertIsNone(owner["learning_file"])
        self.assertFalse(self.state.learning_file.exists())

    def test_custom_entries_file_is_passed_beside_learning_without_creating_it(self):
        with mock.patch.object(control.subprocess, "Popen", side_effect=self.spawn) as spawn:
            self.assertEqual(control.start(self.config, self.api, self.state)["status"], "ready")
        args = spawn.call_args.args[0]
        self.assertEqual(args[args.index("--custom-file") + 1], str(self.state.custom_file))
        self.assertEqual(self.state.custom_file.parent, self.state.learning_file.parent)
        self.assertFalse(self.state.custom_file.exists())
        # Prediction memory sits beside learning and follows the learning switch.
        self.assertEqual(args[args.index("--prediction-file") + 1], str(self.state.prediction_file))
        self.assertFalse(self.state.prediction_file.exists())

    def test_prediction_file_is_omitted_when_learning_is_disabled(self):
        config = replace(self.config, learning_enabled=False)
        with mock.patch.object(control.subprocess, "Popen", side_effect=self.spawn) as spawn:
            control.start(config, self.api, self.state)
        self.assertNotIn("--prediction-file", spawn.call_args.args[0])

    def test_explicit_test_store_overrides_personal_path_without_writing_it(self):
        test_memory = self.root.parent / "isolated-learning.json"
        with mock.patch.object(control.subprocess, "check_output", return_value='{"bits":64,"version":"fixture python"}'):
            control.configure(self.root, self.python, self.config_path, learning_file=test_memory)
        config = control.load_config(self.config_path)
        self.assertEqual(config.learning_file, test_memory)
        with mock.patch.object(control.subprocess, "Popen", side_effect=self.spawn) as spawn:
            self.assertEqual(control.start(config, self.api, self.state)["status"], "ready")
        args = spawn.call_args.args[0]
        self.assertEqual(args[args.index("--learning-file") + 1], str(test_memory))
        self.assertFalse(test_memory.exists())
        self.assertFalse(self.state.learning_file.exists())

    def test_explicit_store_cannot_overwrite_runtime_or_control_files(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            control.configure(self.root, self.python, self.config_path, learning_file=Path("relative.json"))
        with self.assertRaisesRegex(ValueError, "separate"):
            control.configure(self.root, self.python, self.config_path, learning_file=self.database)
        config = replace(self.config, learning_file=self.state.file("ownership.json"))
        with mock.patch.object(control.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "control/log"):
                control.start(config, self.api, self.state)
        spawn.assert_not_called()

    def test_different_runtime_build_requires_explicit_stop(self):
        owner = self.owner()
        owner["runtime_fingerprint"] = "0" * 64
        self.publish(owner)
        with mock.patch.object(control.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "another build"):
                control.start(self.config, self.api, self.state)
        spawn.assert_not_called()

    def test_unmanaged_pipe_prevents_second_broker_and_is_reported(self):
        self.api.pipe = 999
        self.assertEqual(control.status(self.config, self.api, self.state)["status"], "unmanaged_broker")
        with mock.patch.object(control.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                control.start(self.config, self.api, self.state)
        spawn.assert_not_called()

    def test_reused_pid_cannot_be_stopped_and_no_marker_is_written(self):
        self.publish(self.owner())
        self.process.identity["creation_time"] += 1
        with self.assertRaisesRegex(RuntimeError, "another process identity"):
            control.stop(self.config, self.api, self.state)
        self.assertFalse(self.state.file("stop.json").exists())

    def test_wrong_sid_or_session_cannot_be_stopped(self):
        for field, value in (("owner_sid", "S-1-5-21-2000"), ("windows_session", 2)):
            with self.subTest(field=field):
                owner = self.owner()
                self.process.identity[field] = value
                owner["process"] = dict(self.process.identity)
                self.publish(owner)
                with self.assertRaisesRegex(RuntimeError, "another process identity"):
                    control.stop(self.config, self.api, self.state)
                self.process.identity[field] = "S-1-5-21-1001" if field == "owner_sid" else 1

    def test_stop_uses_nonce_request_and_waits_on_original_process_handle(self):
        owner = self.owner()
        self.publish(owner)

        def wait(milliseconds):
            self.assertEqual(control.read_json(self.state.file("stop.json")),
                             {"pid": 77, "runtime_instance": owner["runtime_instance"]})
            self.assertEqual(milliseconds, 15000)
            return True

        self.process.wait_result = wait
        self.assertEqual(control.stop(self.config, self.api, self.state)["status"], "stopped")
        self.assertFalse(self.state.file("ownership.json").exists())
        self.assertFalse(self.state.file("stop.json").exists())

    def test_stop_timeout_keeps_ownership_and_does_not_kill(self):
        self.publish(self.owner())
        with self.assertRaisesRegex(TimeoutError, "No process was forcibly terminated"):
            control.stop(self.config, self.api, self.state, timeout=0.001)
        self.assertTrue(self.state.file("ownership.json").exists())
        self.assertTrue(self.state.file("stop.json").exists())

    def test_start_replaces_a_record_whose_pid_was_reused(self):
        for reused in ("same_user", "unopenable"):
            with self.subTest(reused=reused):
                self.publish(self.owner())
                self.api.pipe = None  # the old broker's pipe is gone with it
                if reused == "same_user":
                    self.process.identity["creation_time"] += 1
                    lookups = iter(())
                else:
                    lookups = iter((None,))  # OpenProcess denied on the old PID maps to no broker
                real_process = self.api.process

                @contextmanager
                def process(pid):
                    stale = next(lookups, False)
                    if stale is None:
                        yield None
                    else:
                        with real_process(pid) as current:
                            yield current

                with mock.patch.object(self.api, "process", process),                         mock.patch.object(control.subprocess, "Popen", side_effect=self.spawn) as spawn:
                    self.assertEqual(control.start(self.config, self.api, self.state)["status"], "ready")
                spawn.assert_called_once()
                self.assertEqual(control.read_json(self.state.file("ownership.json"))["process"], self.process.identity)

    def test_stale_dead_process_can_be_cleaned_without_a_stop_request(self):
        self.publish(self.owner())
        self.api.current_process = None
        self.assertEqual(control.stop(self.config, self.api, self.state)["status"], "stopped")
        self.assertFalse(self.state.file("ownership.json").exists())

    def test_ready_file_nonce_or_actual_pipe_mismatch_is_not_ready(self):
        owner = self.owner()
        self.publish(owner)
        ready = self.ready(owner)
        ready["runtime_instance"] = str(uuid.uuid4())
        control.write_json(self.state.file("backend-ready.json"), ready)
        with self.assertRaisesRegex(RuntimeError, "readiness"):
            control.status(self.config, self.api, self.state)
        control.write_json(self.state.file("backend-ready.json"), self.ready(owner))
        self.api.pipe = 999
        with self.assertRaisesRegex(RuntimeError, "named pipe"):
            control.status(self.config, self.api, self.state)

    def test_cleanup_preserves_replacement_ownership_and_ready_records(self):
        old = self.owner()
        self.publish(old)
        replacement = self.owner()
        self.publish(replacement)
        control._cleanup(self.state, old)
        self.assertEqual(control.read_json(self.state.file("ownership.json")), replacement)
        self.assertEqual(control.read_json(self.state.file("backend-ready.json")), self.ready(replacement))

    def test_start_timeout_requests_nonce_stop_and_retains_ownership_if_not_exited(self):
        with mock.patch.object(control.subprocess, "Popen", side_effect=self.spawn):
            with self.assertRaisesRegex(TimeoutError, "startup deadline"):
                control.start(self.config, self.api, self.state, timeout=0)
        owner = control.read_json(self.state.file("ownership.json"))
        self.assertEqual(control.read_json(self.state.file("stop.json")),
                         {"pid": 77, "runtime_instance": owner["runtime_instance"]})

    def test_print_login_command_is_text_only_and_quotes_paths(self):
        with mock.patch.object(control.subprocess, "Popen") as spawn:
            text = control.login_command(self.config)
        self.assertIn("-WindowStyle Hidden", text)
        self.assertIn('"' + str(self.root / "native/runtime/Start-Runtime.ps1") + '"', text)
        self.assertIn('"' + str(self.config_path) + '"', text)
        self.assertNotIn("CurrentVersion", text)
        spawn.assert_not_called()

    def test_unmanaged_pipe_probe_uses_identification_only_and_closes_handle(self):
        api = object.__new__(control.Windows)
        api.kernel = mock.Mock()
        api.kernel.CreateFileW.return_value = 123

        def server_pid(_handle, output):
            output._obj.value = 77
            return True

        api.kernel.GetNamedPipeServerProcessId.side_effect = server_pid
        self.assertEqual(api.pipe_pid(self.state.pipe), 77)
        api.kernel.CreateFileW.assert_called_once_with(
            self.state.pipe, 0xC0000000, 0, None, 3,
            0x40000000 | 0x00100000 | 0x00010000, None)
        api.kernel.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
