"""Independent installer regressions using workspace fixtures and mocked OS APIs.

No broker, helper execution, registry write, installer or private store runs.
Resource-mapping checks load only isolated workspace PE copies as data.
"""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import ctypes
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from installer import lifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        local = Path(__file__).resolve().parents[1] / ".local" / "release-installer-tests"
        local.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="fixture-", dir=local)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "CantoneseIME"
        self.root.mkdir()
        (self.root / "native").mkdir()
        (self.root / "native/CantoneseIME.dll").write_bytes(b"old fixture DLL")
        (self.root / "native/CantoneseMachineRegistration.exe").write_bytes(b"old fixture helper")
        (self.root / "runtime-config.json").write_text('{"fixture":"old"}', encoding="utf-8")
        self.receipt = self.directory / "prepare.json"
        # Never touch the real System32 directory from tests.
        (self.directory / "System32").mkdir()
        self.system_dll = self.directory / "System32/CantoneseIME.dll"
        system = patch.object(lifecycle, "system_dll", return_value=self.system_dll)
        system.start()
        self.addCleanup(system.stop)

    def user_context(self, controller):
        stack = ExitStack()
        stack.enter_context(patch.object(lifecycle, "require_token"))
        stack.enter_context(patch.object(lifecycle, "manifest"))
        stack.enter_context(patch.object(lifecycle, "user_log", return_value=self.receipt))
        stack.enter_context(patch.object(lifecycle, "binding", side_effect=lambda hive:
            None if hive == lifecycle.winreg.HKEY_CURRENT_USER else str(self.root / "native/CantoneseIME.dll")))
        stack.enter_context(patch.object(lifecycle, "enabled", side_effect=lambda root, language: language == "0409"))
        stack.enter_context(patch.object(lifecycle, "controller", side_effect=controller))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def restore(self):
        with patch.object(lifecycle, "product_root", return_value=self.root), \
                patch.object(lifecycle.sys, "argv", ["lifecycle.py", "restore-user", "--install-root", str(self.root)]):
            lifecycle.main()

    def test_prepare_persists_recovery_intent_before_stopping_owned_ready_broker(self):
        calls = []

        def controller(root, action):
            calls.append(action)
            if action == "status":
                return {"status": "ready"}
            if action == "stop":
                recorded = lifecycle.read_json(self.receipt)
                self.assertTrue(recorded["prior_ready"])
                self.assertTrue(recorded["stop_intent"])
                self.assertEqual(recorded["old_config_sha256"], lifecycle.digest(root / "runtime-config.json"))
            return {"status": "stopped" if action == "stop" else "ready"}

        with self.user_context(controller):
            lifecycle.prepare_user(self.root, None)
            self.restore()
        self.assertEqual(calls, ["status", "stop", "start"])
        self.assertEqual(lifecycle.read_json(self.receipt)["language"], "0409")

    def test_cancellation_does_not_start_an_initially_stopped_broker(self):
        calls = []

        def controller(root, action):
            calls.append(action)
            return {"status": "stopped"}

        with self.user_context(controller):
            lifecycle.prepare_user(self.root, None)
            self.restore()
        self.assertEqual(calls, ["status", "stop"])
        self.assertFalse(lifecycle.read_json(self.receipt)["broker_stopped"])

    def test_post_stop_receipt_failure_retains_recovery_for_cancel(self):
        calls = []
        original = lifecycle.write_json

        def write(path, value):
            if value.get("broker_stopped") is True:
                raise OSError("fixture full disk after stop")
            return original(path, value)

        def controller(root, action):
            calls.append(action)
            return {"status": "stopped" if action == "stop" else "ready"}

        with self.user_context(controller):
            with patch.object(lifecycle, "write_json", side_effect=write), self.assertRaisesRegex(OSError, "full disk"):
                lifecycle.prepare_user(self.root, None)
            self.restore()
        self.assertEqual(calls, ["status", "stop", "start"])

    def test_cancel_does_not_restart_after_another_runtime_config_is_published(self):
        calls = []

        def controller(root, action):
            calls.append(action)
            return {"status": "stopped" if action == "stop" else "ready"}

        with self.user_context(controller):
            lifecycle.prepare_user(self.root, None)
            (self.root / "runtime-config.json").write_text('{"fixture":"different"}', encoding="utf-8")
            self.restore()
        self.assertEqual(calls, ["status", "stop"])

    def test_fresh_user_uses_verified_0409_fallback_without_stopping_any_process(self):
        controller = MagicMock()
        with self.user_context(controller):
            lifecycle.prepare_user(self.directory / "absent", None)
        self.assertEqual(lifecycle.read_json(self.receipt)["language"], "0409")
        controller.assert_not_called()

    def test_elevated_user_actions_are_rejected_before_user_state(self):
        with patch.object(lifecycle, "elevated", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "ordinary Windows account"):
                lifecycle.require_token(False)

    def test_migration_failure_restores_previously_ready_engine_and_preserves_error(self):
        preparation = self.directory / "prepare.json"
        lifecycle.write_json(preparation, {"existing_install": True, "prior_ready": True, "stop_intent": True})
        failure = RuntimeError("fixture HK migration failed")
        with patch.object(lifecycle, "require_token"), patch.object(lifecycle, "manifest"), \
                patch.object(lifecycle, "user_log", side_effect=lambda name: self.directory / name), \
                patch.object(lifecycle, "migrate_user_source", side_effect=failure), \
                patch.object(lifecycle, "controller", return_value={"status": "ready"}) as controller:
            with self.assertRaises(RuntimeError) as raised:
                lifecycle.finish_user(self.root, None)
        self.assertIs(raised.exception, failure)
        controller.assert_called_once_with(self.root, "start")
        self.assertTrue(lifecycle.read_json(self.directory / "activation-recovery.json")["engine_restored"])

    def test_fresh_migration_failure_does_not_claim_prior_engine_recovery(self):
        with patch.object(lifecycle, "require_token"), patch.object(lifecycle, "manifest"), \
                patch.object(lifecycle, "user_log", side_effect=lambda name: self.directory / name), \
                patch.object(lifecycle, "migrate_user_source", side_effect=RuntimeError("fixture migration")), \
                patch.object(lifecycle, "controller") as controller:
            with self.assertRaisesRegex(RuntimeError, "fixture migration"):
                lifecycle.finish_user(self.root, None)
        controller.assert_not_called()
        self.assertFalse((self.directory / "activation-recovery.json").exists())

    def test_recovery_failure_does_not_replace_original_migration_error(self):
        lifecycle.write_json(self.directory / "prepare.json", {"existing_install": True, "prior_ready": True, "stop_intent": True})
        failure = RuntimeError("original migration error")
        with patch.object(lifecycle, "require_token"), patch.object(lifecycle, "manifest"), \
                patch.object(lifecycle, "user_log", side_effect=lambda name: self.directory / name), \
                patch.object(lifecycle, "migrate_user_source", side_effect=failure), \
                patch.object(lifecycle, "controller", side_effect=RuntimeError("recovery failed")), \
                patch.object(lifecycle, "write_json", side_effect=OSError("fixture recovery log write")), redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                lifecycle.finish_user(self.root, None)
        self.assertIs(raised.exception, failure)

    def test_startup_does_not_adopt_a_command_by_path_substring(self):
        key = MagicMock()
        unrelated = '"other.exe" "' + str(self.root) + '" Cantonese'
        with patch.object(lifecycle.winreg, "CreateKeyEx", return_value=key), \
                patch.object(lifecycle.winreg, "QueryValueEx", return_value=(unrelated, lifecycle.winreg.REG_SZ)), \
                patch.object(lifecycle.winreg, "SetValueEx") as setter:
            with self.assertRaisesRegex(ValueError, "owned by another"):
                lifecycle.write_startup(self.root)
        setter.assert_not_called()

    def test_startup_accepts_exact_owned_command(self):
        key = MagicMock()
        expected = lifecycle.startup_command(self.root)
        with patch.object(lifecycle.winreg, "CreateKeyEx", return_value=key), \
                patch.object(lifecycle.winreg, "QueryValueEx", return_value=(expected, lifecycle.winreg.REG_SZ)), \
                patch.object(lifecycle.winreg, "SetValueEx") as setter:
            lifecycle.write_startup(self.root)
        self.assertEqual(setter.call_args.args[-1], expected)

    def machine_fixture(self):
        records = [{"path": relative, "sha256": lifecycle.digest(self.root / relative)}
                   for relative in ("native/CantoneseIME.dll", "native/CantoneseMachineRegistration.exe", "runtime-config.json")]
        current = {"files": records}
        (self.root / "package-manifest.json").write_text(json.dumps(current), encoding="utf-8")
        (self.root / "machine-registration.plan").write_bytes(b"old fixture plan")
        upcoming = {"files": [{"path": "native/CantoneseIME.dll", "sha256": "a" * 64},
                              {"path": "native/CantoneseMachineRegistration.exe", "sha256": "d" * 64},
                              {"path": "runtime-config.json", "sha256": "b" * 64},
                              {"path": "runtime/python/new.txt", "sha256": "c" * 64}]}
        next_manifest = self.directory / "next-manifest.json"
        next_manifest.write_text(json.dumps(upcoming), encoding="utf-8")
        return current, next_manifest

    def machine_context(self, current):
        stack = ExitStack()
        stack.enter_context(patch.object(lifecycle, "require_token"))
        stack.enter_context(patch.object(lifecycle, "manifest", return_value=current))
        stack.enter_context(patch.object(lifecycle, "binding", return_value=str(self.root / "native/CantoneseIME.dll")))
        stack.enter_context(patch.object(lifecycle, "helper"))
        stack.enter_context(patch.object(lifecycle, "refuse_active_runtime"))
        return stack

    def test_machine_rollback_journal_precedes_dll_rename(self):
        current, upcoming = self.machine_fixture()
        replace = lifecycle.os.replace
        saw_rename = []

        def checked_replace(source, target):
            if Path(source) == self.root / "native/CantoneseIME.dll":
                journal = lifecycle.read_json(self.receipt)
                self.assertEqual(journal["moved_dll"], str(target))
                self.assertTrue(any(row["path"] == "native/CantoneseIME.dll" for row in journal["files"]))
                self.assertTrue((Path(journal["backup"]) / "journal.json").is_file())
                self.assertEqual([item["path"] for item in journal["rename_intents"]],
                                 ["native/CantoneseIME.dll", "native/CantoneseMachineRegistration.exe"])
                self.assertTrue(all(not item["completed"] for item in journal["rename_intents"]))
                self.assertEqual(lifecycle.read_json(Path(journal["backup"]) / "journal.json"), journal)
                saw_rename.append(True)
            return replace(source, target)

        with self.machine_context(current), patch.object(lifecycle.os, "replace", side_effect=checked_replace):
            lifecycle.prepare_machine(self.root, self.receipt, upcoming, lifecycle.digest(upcoming))
        self.assertEqual(saw_rename, [True])

    def test_file_rollback_restores_old_dll_config_and_removes_new_only_payload(self):
        current, upcoming = self.machine_fixture()
        old_dll = (self.root / "native/CantoneseIME.dll").read_bytes()
        old_config = (self.root / "runtime-config.json").read_bytes()
        with self.machine_context(current):
            lifecycle.prepare_machine(self.root, self.receipt, upcoming, lifecycle.digest(upcoming))
            (self.root / "native/CantoneseIME.dll").write_bytes(b"new failed fixture DLL")
            (self.root / "runtime-config.json").write_bytes(b"new failed config")
            introduced = self.root / "runtime/python/new.txt"
            introduced.parent.mkdir(parents=True)
            introduced.write_bytes(b"new-only payload")
            lifecycle.rollback_machine(self.root, self.receipt)
        self.assertEqual((self.root / "native/CantoneseIME.dll").read_bytes(), old_dll)
        self.assertEqual((self.root / "runtime-config.json").read_bytes(), old_config)
        self.assertFalse(introduced.exists())

    def test_same_dll_patch_cancel_before_copy_restores_the_canonical_dll(self):
        current, upcoming = self.machine_fixture()
        next_value = lifecycle.read_json(upcoming)
        next_value["files"][0]["sha256"] = current["files"][0]["sha256"]
        next_value["files"][1]["sha256"] = current["files"][1]["sha256"]
        lifecycle.write_json(upcoming, next_value)
        original = (self.root / "native/CantoneseIME.dll").read_bytes()
        original_helper = (self.root / "native/CantoneseMachineRegistration.exe").read_bytes()
        with self.machine_context(current):
            lifecycle.prepare_machine(self.root, self.receipt, upcoming, lifecycle.digest(upcoming))
            # Simulate cancellation after preparation, before Inno copies any files.
            self.assertFalse((self.root / "native/CantoneseIME.dll").exists())
            self.assertFalse((self.root / "native/CantoneseMachineRegistration.exe").exists())
            journal = lifecycle.read_json(self.receipt)
            protected = Path(journal["backup"]) / "native/CantoneseIME.dll"
            self.assertEqual(protected.read_bytes(), original)
            self.assertEqual((Path(journal["backup"]) / "native/CantoneseMachineRegistration.exe").read_bytes(), original_helper)
            lifecycle.rollback_machine(self.root, self.receipt)
        self.assertEqual((self.root / "native/CantoneseIME.dll").read_bytes(), original)
        self.assertEqual((self.root / "native/CantoneseMachineRegistration.exe").read_bytes(), original_helper)
        self.assertEqual(lifecycle.read_json(self.receipt)["status"], "rolled_back")

    def test_partial_helper_rename_failure_skips_overwriting_correct_locked_target(self):
        current, upcoming = self.machine_fixture()
        helper = self.root / "native/CantoneseMachineRegistration.exe"
        replace, copy = lifecycle.os.replace, lifecycle.shutil.copyfile

        def fail_helper_move(source, target):
            if Path(source) == helper:
                raise PermissionError("fixture helper rename blocked")
            return replace(source, target)

        def refuse_helper_overwrite(source, target):
            if Path(target) == helper:
                raise AssertionError("The correct old helper must not be overwritten")
            return copy(source, target)

        with self.machine_context(current):
            with patch.object(lifecycle.os, "replace", side_effect=fail_helper_move), self.assertRaisesRegex(PermissionError, "rename blocked"):
                lifecycle.prepare_machine(self.root, self.receipt, upcoming, lifecycle.digest(upcoming))
            self.assertFalse((self.root / "native/CantoneseIME.dll").exists())
            with patch.object(lifecycle.shutil, "copyfile", side_effect=refuse_helper_overwrite):
                lifecycle.rollback_machine(self.root, self.receipt)
        self.assertEqual((self.root / "native/CantoneseIME.dll").read_bytes(), b"old fixture DLL")
        self.assertEqual(helper.read_bytes(), b"old fixture helper")

    def test_post_helper_move_receipt_failure_can_restore_both_from_prior_intents(self):
        current, upcoming = self.machine_fixture()
        write = lifecycle.write_json
        failed = False

        def fail_completed_helper(path, value):
            nonlocal failed
            if not failed and value.get("rename_intents", [{}])[-1].get("completed"):
                failed = True
                raise OSError("fixture receipt write after helper move")
            return write(path, value)

        with self.machine_context(current):
            with patch.object(lifecycle, "write_json", side_effect=fail_completed_helper), self.assertRaisesRegex(OSError, "after helper move"):
                lifecycle.prepare_machine(self.root, self.receipt, upcoming, lifecycle.digest(upcoming))
            self.assertFalse((self.root / "native/CantoneseIME.dll").exists())
            self.assertFalse((self.root / "native/CantoneseMachineRegistration.exe").exists())
            persisted = lifecycle.read_json(self.receipt)
            self.assertFalse(persisted["rename_intents"][-1]["completed"])
            lifecycle.rollback_machine(self.root, self.receipt)
        self.assertEqual((self.root / "native/CantoneseIME.dll").read_bytes(), b"old fixture DLL")
        self.assertEqual((self.root / "native/CantoneseMachineRegistration.exe").read_bytes(), b"old fixture helper")

    def test_legacy_receipt_without_helper_or_rename_fields_still_restores(self):
        current, _ = self.machine_fixture()
        backup = self.root.parent / "CantoneseIME-Updates/release-legacy"
        (backup / "native").mkdir(parents=True)
        dll = self.root / "native/CantoneseIME.dll"
        shutil.copyfile(dll, backup / "native/CantoneseIME.dll")
        retired = dll.with_name("CantoneseIME.previous-legacy.dll")
        os.replace(dll, retired)
        lifecycle.write_json(self.receipt, {"schema_version": 1, "status": "prepared", "existing": True,
            "backup": str(backup), "moved_dll": str(retired),
            "files": [{"path": "native/CantoneseIME.dll", "sha256": lifecycle.digest(backup / "native/CantoneseIME.dll")}]})
        with self.machine_context(current):
            lifecycle.rollback_machine(self.root, self.receipt)
        self.assertEqual(dll.read_bytes(), b"old fixture DLL")
        self.assertEqual((self.root / "native/CantoneseMachineRegistration.exe").read_bytes(), b"old fixture helper")

    def resource_fixture(self):
        prototype = Path(__file__).resolve().parents[1] / ".local/native/branding-exe-prototype/CantoneseMachineRegistration-branding-prototype.exe"
        if os.name != "nt" or not prototype.is_file():
            self.skipTest("The Windows resource-only helper prototype is unavailable.")
        helper = self.root / "native/CantoneseMachineRegistration.exe"
        shutil.copyfile(prototype, helper)
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
        api.LoadLibraryExW.restype = ctypes.c_void_p
        api.FreeLibrary.argtypes = [ctypes.c_void_p]
        api.FindResourceW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        api.FindResourceW.restype = ctypes.c_void_p
        api.CopyFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        return helper, api

    def test_real_resource_mapped_helper_cancel_before_copy_restores_both_modules(self):
        helper, api = self.resource_fixture()
        current, upcoming = self.machine_fixture()
        original = helper.read_bytes()
        replacement = self.directory / "replacement.exe"
        replacement.write_bytes(original)
        module = api.LoadLibraryExW(str(helper), None, 0x22)  # DATAFILE | IMAGE_RESOURCE, never executes code.
        self.assertTrue(module)
        try:
            self.assertTrue(api.FindResourceW(module, 101, 14))
            self.assertFalse(api.CopyFileW(str(replacement), str(helper), False))
            self.assertEqual(ctypes.get_last_error(), 32)
            with self.machine_context(current):
                lifecycle.prepare_machine(self.root, self.receipt, upcoming, lifecycle.digest(upcoming))
                self.assertFalse(helper.exists())
                lifecycle.rollback_machine(self.root, self.receipt)
            self.assertEqual(helper.read_bytes(), original)
            self.assertEqual((self.root / "native/CantoneseIME.dll").read_bytes(), b"old fixture DLL")
            self.assertTrue(api.FindResourceW(module, 101, 14))
        finally:
            api.FreeLibrary(module)

    def test_real_new_helper_resource_mapping_is_retired_before_rollback_copy(self):
        helper, api = self.resource_fixture()
        current, upcoming = self.machine_fixture()
        original = helper.read_bytes()
        with self.machine_context(current):
            lifecycle.prepare_machine(self.root, self.receipt, upcoming, lifecycle.digest(upcoming))
            helper.write_bytes(original + b"\nnew fixture PE overlay\n")
            module = api.LoadLibraryExW(str(helper), None, 0x22)
            self.assertTrue(module)
            try:
                journal = lifecycle.read_json(self.receipt)
                protected = Path(journal["backup"]) / "native/CantoneseMachineRegistration.exe"
                self.assertFalse(api.CopyFileW(str(protected), str(helper), False))
                self.assertEqual(ctypes.get_last_error(), 32)
                lifecycle.rollback_machine(self.root, self.receipt)
                self.assertEqual(helper.read_bytes(), original)
                self.assertEqual(len(list(helper.parent.glob("CantoneseMachineRegistration.failed-*.exe"))), 1)
                self.assertTrue(api.FindResourceW(module, 101, 14))
            finally:
                api.FreeLibrary(module)

    def test_completed_rollback_is_recorded_in_the_retained_protected_journal(self):
        current, upcoming = self.machine_fixture()
        with self.machine_context(current):
            lifecycle.prepare_machine(self.root, self.receipt, upcoming, lifecycle.digest(upcoming))
            backup = Path(lifecycle.read_json(self.receipt)["backup"])
            self.assertEqual(lifecycle.read_json(backup / "journal.json")["status"], "prepared")
            lifecycle.rollback_machine(self.root, self.receipt)
        retained = lifecycle.read_json(backup / "journal.json")
        self.assertEqual(retained["status"], "rolled_back")
        self.assertEqual(retained, lifecycle.read_json(self.receipt))

    def test_failed_native_helper_prints_its_failure_for_the_setup_log(self):
        (self.root / "machine-registration.plan").write_bytes(b"fixture plan\n")
        final = {"stage": "failed", "hresult": "0x80004005", "succeeded": False,
                 "detail": "Refreshed inventory does not match release"}
        output = '\n'.join((json.dumps({"stage": "registered", "succeeded": True}), json.dumps(final))) + '\n'
        child = SimpleNamespace(returncode=2, stdout=output, stderr="")
        with patch.object(lifecycle.subprocess, "run", return_value=child), \
                patch.object(lifecycle, "elevated", return_value=True), redirect_stderr(io.StringIO()) as captured:
            with self.assertRaisesRegex(RuntimeError, "refresh-registration failed"):
                lifecycle.helper(self.root, "refresh-registration")
        logged = json.loads(captured.getvalue())
        self.assertEqual(logged["last_failure"], final)
        self.assertEqual(logged["exit_code"], 2)
        self.assertEqual(Path(logged["diagnostic_file"]).read_text(encoding="utf-8"), output)

    def test_native_registration_success_is_commit_even_if_final_journal_write_fails(self):
        config = {"python_executable": "runtime/python/python.exe", "learning_enabled": True, "learning_file": None}
        lifecycle.write_json(self.root / "runtime-config.json", config)
        lifecycle.write_json(self.receipt, {"existing": True, "status": "prepared"})
        current = {"kind": lifecycle.RELEASE_KIND, "version": "1.0.0", "native_dll_sha256": "a" * 64}
        write = lifecycle.write_json

        def failing_final_write(path, value):
            if value.get("status") == "committed":
                raise OSError("fixture final receipt failure")
            return write(path, value)

        with self.machine_context(current), patch.object(lifecycle, "write_json", side_effect=failing_final_write), \
                patch.object(lifecycle, "helper") as helper, redirect_stdout(io.StringIO()) as output:
            lifecycle.install_machine(self.root, self.receipt, "b" * 64)
        helper.assert_called_once_with(self.root, "refresh-registration")
        self.assertTrue((self.root / "installation.json").is_file())
        self.assertIn("Registration committed", output.getvalue())

    def test_install_registers_a_verified_system32_copy_of_the_product_dll(self):
        config = {"python_executable": "runtime/python/python.exe", "learning_enabled": True, "learning_file": None}
        lifecycle.write_json(self.root / "runtime-config.json", config)
        lifecycle.write_json(self.receipt, {"existing": True, "status": "prepared"})
        current = {"kind": lifecycle.RELEASE_KIND, "version": "1.0.0", "native_dll_sha256": "a" * 64}
        with self.machine_context(current), patch.object(lifecycle, "helper") as helper, redirect_stdout(io.StringIO()):
            lifecycle.install_machine(self.root, self.receipt, "b" * 64)
        helper.assert_called_once_with(self.root, "refresh-registration")
        self.assertEqual(self.system_dll.read_bytes(), (self.root / "native/CantoneseIME.dll").read_bytes())
        plan = (self.root / "machine-registration.plan").read_text(encoding="utf-8")
        self.assertIn("dll_source=" + str(self.root / "native/CantoneseIME.dll") + "\n", plan)
        self.assertIn("dll_target=" + str(self.system_dll) + "\n", plan)
        self.assertEqual(sorted(path.name for path in self.system_dll.parent.iterdir()), ["CantoneseIME.dll"])

    def test_system32_copy_replaces_an_older_copy_and_both_bindings_are_owned(self):
        self.system_dll.write_bytes(b"older copy")
        lifecycle.place_system_dll(self.root)
        self.assertEqual(self.system_dll.read_bytes(), b"old fixture DLL")
        self.assertTrue(lifecycle.owned_binding(str(self.root / "native/CantoneseIME.dll"), self.root))
        self.assertTrue(lifecycle.owned_binding(str(self.system_dll), self.root))
        self.assertFalse(lifecycle.owned_binding(str(self.directory / "other.dll"), self.root))
        self.assertFalse(lifecycle.owned_binding(None, self.root))

    def test_a_mapped_system32_copy_is_renamed_for_deletion_at_restart(self):
        self.system_dll.write_bytes(b"older copy")
        unlink = Path.unlink

        def mapped(path, *args, **kwargs):
            if path == self.system_dll:
                raise PermissionError("fixture: mapped by a running app")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", mapped), patch.object(lifecycle.ctypes.windll.kernel32, "MoveFileExW") as later:
            lifecycle.retire_system_dll(self.system_dll)
        self.assertFalse(self.system_dll.exists())
        retired = [path for path in self.system_dll.parent.iterdir()]
        self.assertEqual(len(retired), 1)
        self.assertTrue(retired[0].name.startswith("CantoneseIME.previous-"))
        later.assert_called_once_with(str(retired[0]), None, 0x4)

    def test_uninstall_removes_the_system32_copy_after_registration(self):
        self.system_dll.write_bytes(b"old fixture DLL")
        with patch.object(lifecycle, "require_token"), patch.object(lifecycle, "manifest", return_value={}), \
                patch.object(lifecycle, "controller"), patch.object(lifecycle, "refuse_active_runtime"), \
                patch.object(lifecycle, "helper") as helper, patch.object(lifecycle, "remove_loaded_user_startup"):
            lifecycle.uninstall_machine(self.root)
        helper.assert_called_once_with(self.root, "remove")
        self.assertFalse(self.system_dll.exists())

    def test_fresh_failed_registration_cleanup_precedes_file_removal(self):
        lifecycle.write_json(self.receipt, {"status": "prepared", "existing": False, "registration_attempted": True})
        with patch.object(lifecycle, "require_token"), \
                patch.object(lifecycle, "binding", return_value=str(self.root / "native/CantoneseIME.dll")), \
                patch.object(lifecycle, "helper") as helper:
            lifecycle.rollback_machine(self.root, self.receipt)
        helper.assert_called_once_with(self.root, "remove")

    def test_uninstall_refuses_active_other_runtime_before_registration_or_startup_removal(self):
        with patch.object(lifecycle, "require_token"), patch.object(lifecycle, "manifest", return_value={}), \
                patch.object(lifecycle, "controller"), \
                patch.object(lifecycle, "refuse_active_runtime", side_effect=RuntimeError("other session active")), \
                patch.object(lifecycle, "helper") as helper, patch.object(lifecycle, "remove_loaded_user_startup") as startup:
            with self.assertRaisesRegex(RuntimeError, "other session active"):
                lifecycle.uninstall_machine(self.root)
        helper.assert_not_called()
        startup.assert_not_called()

    def test_payload_paths_reject_traversal_and_ads(self):
        for relative in ("../unowned", "/absolute", "native/../unowned", "native/CantoneseIME.dll:stream", "native/file."):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                lifecycle.payload_path(self.root, relative)


if __name__ == "__main__":
    unittest.main()
