"""Release installer lifecycle. No code is loaded from a user's data directory.

Machine actions use only the fixed, protected Program Files product root.
User actions reject elevation; only the user's owned startup value is changed.
Learning files are deliberately never opened, copied, truncated or removed.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as w
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import uuid
import winreg

CLSID = "{67718E6F-49A1-4F54-88D6-30578FC70C5D}"
PROFILE = "{A7136205-2245-45CC-A6AA-702B3F2E8D42}"
CLASS_KEY = rf"Software\Classes\CLSID\{CLSID}\InprocServer32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CantoneseIME"
RELEASE_KIND = "Cantonese IME native AMD64 release runtime package"
PREVIEW_KIND = "Cantonese IME native AMD64 preview runtime package"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_json(path: Path) -> dict:
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Installer metadata is oversized.")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected installer object metadata.")
    return value


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def elevated() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def require_token(admin: bool):
    if os.name != "nt" or struct.calcsize("P") != 8 or elevated() != admin:
        raise RuntimeError("Administrator access is required." if admin else
                           "Use the ordinary Windows account: do not choose Run as administrator.")


def product_root(value: str) -> Path:
    # SHGetFolderPath CSIDL_PROGRAM_FILES avoids trusting a user-supplied env var.
    output = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x26, None, 0, output) != 0:
        raise OSError("Cannot resolve the native Program Files folder.")
    expected = Path(output.value) / "CantoneseIME"
    selected = Path(value).absolute()
    if os.path.normcase(str(selected)) != os.path.normcase(str(expected)):
        raise ValueError("The installer only owns Program Files\\CantoneseIME.")
    if selected.exists() and selected.resolve() != selected:
        raise ValueError("Reparse points are not accepted at the product root.")
    return selected


def payload_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or any(char in relative for char in '\\:<>"|?*'):
        raise ValueError("Invalid package path.")
    parts = relative.split("/")
    if PurePosixPath(relative).is_absolute() or any(not part or part in (".", "..") or part.endswith((".", " ")) for part in parts):
        raise ValueError("Package paths must stay inside the product root.")
    result = root.joinpath(*parts)
    if result.exists() and result.resolve() != result.absolute():
        raise ValueError("Package paths must not contain links.")
    return result


def manifest(root: Path, expected: str | None = None, *, all_files: bool = True) -> dict:
    path = root / "package-manifest.json"
    if expected is not None and digest(path) != expected:
        raise ValueError("The release package manifest changed.")
    value = read_json(path)
    if value.get("schema_version") != 1 or value.get("kind") not in (RELEASE_KIND, PREVIEW_KIND):
        raise ValueError("Unknown installed package kind.")
    seen = set()
    for record in value["files"]:
        target = payload_path(root, record["path"])
        if record["path"].casefold() in seen:
            raise ValueError("Duplicate package path.")
        seen.add(record["path"].casefold())
        if all_files and (not target.is_file() or target.stat().st_size != record["bytes"] or digest(target) != record["sha256"]):
            raise ValueError("Installed payload validation failed: " + record["path"])
    for name in ("native/CantoneseIME.dll", "native/CantoneseMachineRegistration.exe", "native/runtime/control.py", "runtime-config.json"):
        if name.casefold() not in seen:
            raise ValueError("Package is missing " + name)
    return value


def binding(hive) -> str | None:
    try:
        with winreg.OpenKey(hive, CLASS_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, kind = winreg.QueryValueEx(key, "")
            if kind != winreg.REG_SZ:
                raise ValueError("Unexpected product COM binding type.")
            model, _ = winreg.QueryValueEx(key, "ThreadingModel")
            if model != "Apartment":
                raise ValueError("Unexpected product COM thread model.")
            return value
    except FileNotFoundError:
        return None


def same_path(left: str, right: Path) -> bool:
    return os.path.normcase(str(Path(left).absolute())) == os.path.normcase(str(right.absolute()))


def system_dll() -> Path:
    """The registered copy. Games such as World of Warcraft load text-service
    DLLs only from System32; the product files stay under Program Files."""
    buffer = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not count or count >= len(buffer):
        raise OSError("Cannot read the System32 directory.")
    return Path(buffer.value) / "CantoneseIME.dll"


def owned_binding(actual: str | None, root: Path) -> bool:
    # Installs before the System32 copy bound the Program Files DLL directly.
    return actual is not None and (same_path(actual, root / "native/CantoneseIME.dll") or same_path(actual, system_dll()))


def retire_system_dll(target: Path):
    """Remove the System32 copy; one mapped by running apps is renamed and deleted at restart."""
    if not target.exists():
        return
    try:
        target.unlink()
    except PermissionError:
        retired = target.with_name(target.stem + ".previous-" + uuid.uuid4().hex + target.suffix)
        os.replace(target, retired)
        ctypes.windll.kernel32.MoveFileExW(str(retired), None, 0x4)  # MOVEFILE_DELAY_UNTIL_REBOOT


def place_system_dll(root: Path):
    source, target = root / "native/CantoneseIME.dll", system_dll()
    if target.is_file() and digest(target) == digest(source):
        return
    staged = target.with_name(target.stem + ".new-" + uuid.uuid4().hex + target.suffix)
    shutil.copyfile(source, staged)
    try:
        if digest(staged) != digest(source):
            raise ValueError("The System32 DLL copy differs from the product DLL.")
        retire_system_dll(target)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def controller(root: Path, command: str, *, python: Path | None = None) -> dict:
    config = read_json(root / "runtime-config.json")
    configured = Path(config["python_executable"])
    if not configured.is_absolute():
        configured = payload_path(root, configured.as_posix())
    interpreter = python or configured
    if digest(configured) != config["python_sha256"]:
        raise ValueError("Installed private/external interpreter changed.")
    result = subprocess.run([str(interpreter), "-B", str(root / "native/runtime/control.py"), command,
                             "--config", str(root / "runtime-config.json")], capture_output=True,
                            text=True, encoding="utf-8", timeout=70, creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError("Runtime " + command + " failed: " + result.stderr[-2000:])
    return json.loads(result.stdout)


def helper(root: Path, action: str, language: str | None = None) -> list[dict]:
    plan = root / "machine-registration.plan"
    command = [str(root / "native/CantoneseMachineRegistration.exe"), "--action", action,
               "--manifest", str(plan), "--manifest-sha256", digest(plan)]
    if language:
        command += ["--language", language]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=50,
                            creationflags=0x08000000)
    log = root / ("installer-machine-" + action + ".jsonl") if elevated() else user_log("profile-" + action + ".jsonl")
    try:
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
    except OSError:
        if result.returncode:
            raise
    if result.returncode:
        failures = []
        for line in result.stdout.splitlines():
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get("succeeded") is False:
                    failures.append(entry)
            except ValueError:
                continue
        print(json.dumps({"status": "native_helper_failed", "action": action,
                          "exit_code": result.returncode, "last_failure": failures[-1] if failures else None,
                          "diagnostic_file": str(log)}, ensure_ascii=True), file=sys.stderr)
        raise RuntimeError("Windows input profile " + action + " failed; see " + str(log))
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def user_log(name: str) -> Path:
    root = Path(os.environ["LOCALAPPDATA"]) / "CantoneseIME/Installer"
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def enabled(root: Path, language: str) -> bool:
    for item in helper(root, "inspect-user", language):
        if item.get("step") == "user_enabled_before" or item.get("stage") == "user_enabled_before":
            return item.get("detail") == "true"
    raise RuntimeError("Windows did not report the profile's enabled state.")


def prepare_user(root: Path, receipt: Path | None):
    require_token(False)
    receipt = user_log("prepare.json")
    result = {"schema_version": 1, "prepared": True, "existing_install": root.exists(), "language": "0409", "broker_stopped": False, "prior_ready": False, "stop_intent": False}
    write_json(receipt, result)
    if root.exists():
        manifest(root)
        override = binding(winreg.HKEY_CURRENT_USER)
        if override is not None:
            raise RuntimeError("An old per-user development COM override exists. Remove that exact preview override before installing this release.")
        machine = binding(winreg.HKEY_LOCAL_MACHINE)
        if not owned_binding(machine, root):
            raise ValueError("Existing files do not have this product's exact machine binding.")
        result["enabled_languages"] = [language for language in ("0409", "0c04") if enabled(root, language)]
        result["language"] = "0409" if "0409" in result["enabled_languages"] else "0c04" if "0c04" in result["enabled_languages"] else "0409"
        prior = controller(root, "status")
        result["prior_ready"] = prior.get("status") == "ready"
        result["old_config_sha256"] = digest(root / "runtime-config.json")
        result["stop_intent"] = True
        # The ordinary-user recovery record precedes the stop request. A
        # subsequent receipt error can still restore an engine that was ready.
        write_json(receipt, result)
        state = controller(root, "stop")
        if state.get("status") not in ("stopped", "stale_state"):
            raise RuntimeError("The owned broker did not stop safely.")
        result["broker_stopped"] = result["prior_ready"]
    write_json(receipt, result)


def prepare_machine(root: Path, receipt: Path, next_manifest: Path, expected: str):
    require_token(True)
    if digest(next_manifest) != expected:
        raise ValueError("The embedded release manifest changed.")
    upcoming = read_json(next_manifest)
    journal = {"schema_version": 1, "operation": "release-install", "status": "prepared", "existing": root.exists(), "files": [], "moved_dll": None}
    if root.exists():
        current = manifest(root)
        helper(root, "dry-run-remove")
        refuse_active_runtime(root, current)
        actual = binding(winreg.HKEY_LOCAL_MACHINE)
        if not owned_binding(actual, root):
            raise ValueError("Refusing to replace an unrelated machine COM binding.")
        next_files = {record["path"]: record["sha256"] for record in upcoming["files"]}
        old_files = {record["path"] for record in current["files"]}
        journal["introduced_files"] = [name for name in next_files if name not in old_files and not payload_path(root, name).exists()]
        changed = [record["path"] for record in current["files"] if next_files.get(record["path"]) != record["sha256"]]
        # Both modules can remain resource-mapped by Windows. Back them up
        # even when unchanged, before vacating their canonical install paths.
        changed += ["native/CantoneseIME.dll", "native/CantoneseMachineRegistration.exe"]
        changed += [name for name in ("package-manifest.json", "machine-registration.plan", "installation.json") if (root / name).exists()]
        backup = root.parent / "CantoneseIME-Updates" / ("release-" + uuid.uuid4().hex)
        backup.mkdir(parents=True)
        journal["backup"] = str(backup)
        for name in sorted(set(changed)):
            source, target = payload_path(root, name), payload_path(backup, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            journal["files"].append({"path": name, "sha256": digest(source)})
        # Renaming mapped modules leaves existing mappings intact. No target
        # app or Explorer is closed. Persist BOTH intents before the first move.
        journal["rename_intents"] = []
        for relative, legacy_field in (("native/CantoneseIME.dll", "moved_dll"),
                                       ("native/CantoneseMachineRegistration.exe", "moved_helper")):
            source = payload_path(root, relative)
            retired = source.with_name(source.stem + ".previous-" + uuid.uuid4().hex + source.suffix)
            journal[legacy_field] = str(retired)
            journal["rename_intents"].append({"path": relative, "retired_path": str(retired), "completed": False})
        write_json(receipt, journal)
        write_json(backup / "journal.json", journal)
        for intent in journal["rename_intents"]:
            os.replace(payload_path(root, intent["path"]), Path(intent["retired_path"]))
            intent["completed"] = True
            write_json(receipt, journal)
            write_json(backup / "journal.json", journal)
    write_json(receipt, journal)


def install_machine(root: Path, receipt: Path, expected: str):
    require_token(True)
    current = manifest(root, expected)
    config = read_json(root / "runtime-config.json")
    if (current["kind"] != RELEASE_KIND or config["python_executable"] != "runtime/python/python.exe"
            or config.get("learning_enabled") is not True or config.get("learning_file") is not None):
        raise ValueError("The release must use the private runtime and ordinary per-user learning.")
    actual = binding(winreg.HKEY_LOCAL_MACHINE)
    if actual is not None and not owned_binding(actual, root):
        raise ValueError("Machine registration belongs to another installation.")
    dll = root / "native/CantoneseIME.dll"
    place_system_dll(root)
    plan = "\n".join(("CANTONESE_IME_MACHINE_V1", "clsid=" + CLSID, "profile=" + PROFILE,
                      "languages=0409,0c04", "dll_source=" + str(dll), "dll_target=" + str(system_dll()),
                      "dll_sha256=" + current["native_dll_sha256"])) + "\n"
    (root / "machine-registration.plan").write_text(plan, encoding="utf-8")
    journal = read_json(receipt)
    journal["registration_attempted"] = True
    write_json(receipt, journal)
    write_json(root / "installation.json", {"version": current["version"], "package_manifest_sha256": expected,
               "registration_plan_sha256": digest(root / "machine-registration.plan"),
               "installed_at_utc": datetime.now(timezone.utc).isoformat(), "installer": "Inno Setup", "startup_policy": "per-user, non-elevated"})
    # This is the commit boundary. Every required file write precedes the
    # helper, which rolls back its own failed registration. Successful
    # registration must not subsequently be described as an old-state rollback.
    helper(root, "refresh-registration" if actual is not None else "install")
    journal["status"] = "committed"
    try:
        write_json(receipt, journal)
        if "backup" in journal:
            write_json(Path(journal["backup"]) / "journal.json", journal)
    except OSError as error:
        # The installed, verified machine state remains the committed result.
        # A receipt failure cannot justify restoring files under new metadata.
        print(json.dumps({"warning": "Registration committed; journal update failed", "detail": str(error)}))


def refuse_active_runtime(root: Path, current: dict):
    """Restart Manager inventories in-use runtime files without closing apps."""
    class UniqueProcess(ctypes.Structure):
        _fields_ = [("pid", w.DWORD), ("started", w.FILETIME)]
    class ProcessInfo(ctypes.Structure):
        _fields_ = [("process", UniqueProcess), ("name", w.WCHAR * 256), ("service", w.WCHAR * 64),
                    ("kind", w.DWORD), ("status", w.ULONG), ("session", w.DWORD), ("restartable", w.BOOL)]
    api = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    handle = w.DWORD()
    key = ctypes.create_unicode_buffer(33)
    code = api.RmStartSession(ctypes.byref(handle), 0, key)
    if code:
        raise ctypes.WinError(code)
    try:
        names = [str(root / "data/generated/lexicon.sqlite3")]
        names += [str(payload_path(root, item["path"])) for item in current["files"]
                  if item["path"].startswith("runtime/python/") and item["path"].lower().endswith((".exe", ".dll", ".pyd", ".zip"))]
        resources = (w.LPCWSTR * len(names))(*names)
        code = api.RmRegisterResources(handle, len(names), resources, 0, None, 0, None)
        if code:
            raise ctypes.WinError(code)
        needed, count, reason = w.UINT(), w.UINT(), w.DWORD()
        code = api.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reason))
        if code == 234:
            values = (ProcessInfo * needed.value)()
            count.value = needed.value
            code = api.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count), values, ctypes.byref(reason))
            if code:
                raise ctypes.WinError(code)
            if any(values[index].process.pid != os.getpid() for index in range(count.value)):
                raise RuntimeError("Another session still uses the keyboard engine. Sign out other Windows users or stop Cantonese IME, then retry. No application was closed.")
        elif code:
            raise ctypes.WinError(code)
    finally:
        api.RmEndSession(handle)


def startup_command(root: Path) -> str:
    return subprocess.list2cmdline([str(root / "runtime/python/pythonw.exe"), "-B", str(root / "installer/lifecycle.py"), "login", "--install-root", str(root)])


def write_startup(root: Path):
    command = startup_command(root)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
        try:
            previous, kind = winreg.QueryValueEx(key, RUN_NAME)
        except FileNotFoundError:
            previous, kind = None, None
        if previous is not None and previous != command:
            raise ValueError("The CantoneseIME startup name is owned by another command.")
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)


def migrate_user_source(root: Path) -> dict:
    system = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system))
    if not count or count >= len(system):
        raise OSError("Cannot resolve native Windows PowerShell.")
    powershell = Path(system.value) / "WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(root / "installer/Set-UserInputSource.ps1"), "-InstallRoot", str(root)],
                            capture_output=True, text=True, encoding="utf-8", timeout=120,
                            creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError("Hong Kong input-source activation failed: " + result.stderr[-3000:])
    value = json.loads(result.stdout.lstrip("\ufeff"))
    if value.get("status") != "ready" or value.get("language") != "0c04":
        raise RuntimeError("Windows did not verify the Hong Kong input source.")
    return value


def finish_user(root: Path, receipt: Path | None, *, startup: bool = True):
    require_token(False)
    manifest(root)
    try:
        source = migrate_user_source(root)
    except Exception:
        try:
            preparation = user_log("prepare.json")
            prior = read_json(preparation) if preparation.exists() else {}
            if prior.get("existing_install") and prior.get("prior_ready") and prior.get("stop_intent"):
                recovery = {"status": "activation_failed", "prior_engine_recovery_attempted": True}
                try:
                    state = controller(root, "start")
                    recovery["engine_status"] = state.get("status")
                    recovery["engine_restored"] = state.get("status") == "ready"
                except Exception as error:
                    recovery["engine_restored"] = False
                    recovery["error"] = str(error)
                write_json(user_log("activation-recovery.json"), recovery)
        except Exception as error:
            print(json.dumps({"status": "activation_recovery_failed", "error": str(error)}), file=sys.stderr)
        raise
    language = source["language"]
    if startup:
        write_startup(root)
    else:
        remove_startup(root)
    state = controller(root, "start")
    if state.get("status") != "ready":
        raise RuntimeError("The keyboard engine did not become ready.")
    write_json(user_log("activation.json"), {"status": "ready", "language": language, "pid": state.get("pid"),
               "legacy_retained_as_default": source.get("legacy_retained_as_default", False), "learning_preserved": True})


def login(root: Path):
    require_token(False)
    manifest(root)
    controller(root, "start")


def remove_startup(root: Path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
            value, kind = winreg.QueryValueEx(key, RUN_NAME)
            if kind == winreg.REG_SZ and value == startup_command(root):
                winreg.DeleteValue(key, RUN_NAME)
    except FileNotFoundError:
        pass


def remove_loaded_user_startup(root: Path):
    # Remove this exact command from loaded user hives only. Do not load or
    # edit another user's profile files, and never infer ownership from text.
    index = 0
    while True:
        try:
            sid = winreg.EnumKey(winreg.HKEY_USERS, index)
        except OSError:
            break
        index += 1
        if not sid.startswith("S-1-5-21-") or sid.endswith("_Classes"):
            continue
        try:
            with winreg.OpenKey(winreg.HKEY_USERS, sid + "\\" + RUN_KEY, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
                value, kind = winreg.QueryValueEx(key, RUN_NAME)
                if kind == winreg.REG_SZ and value == startup_command(root):
                    winreg.DeleteValue(key, RUN_NAME)
        except OSError as error:
            # Machine removal is already committed. A locked/missing loaded
            # user key must not turn that into a misleading failed uninstall.
            print(json.dumps({"warning": "An owned startup entry could not be cleaned", "sid": sid, "error": str(error)}))


def uninstall_machine(root: Path):
    require_token(True)
    manifest(root)
    # This stops only the controller's exact PID/creation-time/SID/session match.
    # It cannot kill another user's process or an unrelated application.
    # With over-the-shoulder elevation the administrator has a different SID.
    # Never pretend that administrator's HKCU is the original user. Stop the
    # exact owned current session if present; other sessions are handled by
    # Windows' in-use-file/restart behavior without forced termination.
    controller(root, "stop")
    refuse_active_runtime(root, manifest(root, all_files=False))
    helper(root, "remove")
    retire_system_dll(system_dll())
    remove_loaded_user_startup(root)
    # No access to learning.json or dictionary selection history occurs here.


def rollback_machine(root: Path, receipt: Path):
    require_token(True)
    if not receipt.exists():
        return
    journal = read_json(receipt)
    if journal["status"] == "committed":
        return
    if journal["existing"]:
        backup = Path(journal["backup"])
        if backup.parent != root.parent / "CantoneseIME-Updates":
            raise ValueError("Unexpected protected backup root.")
        for record in journal["files"]:
            source, target = payload_path(backup, record["path"]), payload_path(root, record["path"])
            if digest(source) != record["sha256"]:
                raise ValueError("Protected rollback copy changed.")
            target.parent.mkdir(parents=True, exist_ok=True)
            # A failed prepare rename may leave the correct, mapped old module
            # in place. Rewriting those bytes would fail despite correct state.
            if target.is_file() and digest(target) == record["sha256"]:
                continue
            if record["path"] in ("native/CantoneseIME.dll", "native/CantoneseMachineRegistration.exe") and target.exists():
                os.replace(target, target.with_name(target.stem + ".failed-" + uuid.uuid4().hex + target.suffix))
            shutil.copyfile(source, target)
        for name in journal.get("introduced_files", []):
            payload_path(root, name).unlink(missing_ok=True)
        manifest(root)
        # The registered System32 copy must match the restored product DLL again.
        actual = binding(winreg.HKEY_LOCAL_MACHINE)
        if actual is not None and same_path(actual, system_dll()):
            place_system_dll(root)
    elif journal.get("registration_attempted") and binding(winreg.HKEY_LOCAL_MACHINE) is not None:
        # Fresh registration may have completed before a later file/receipt
        # error. Remove its exact fixed identity before Inno removes files.
        helper(root, "remove")
        retire_system_dll(system_dll())
    journal["status"] = "rolled_back"
    if journal["existing"]:
        write_json(Path(journal["backup"]) / "journal.json", journal)
    write_json(receipt, journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare-user", "prepare-machine", "install-machine", "finish-user", "login", "stop-user", "uninstall-machine", "rollback-machine", "restore-user", "verify"))
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--next-manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--no-startup", action="store_true")
    args = parser.parse_args()
    root = product_root(args.install_root)
    if args.action == "prepare-user":
        prepare_user(root, args.receipt)
    elif args.action == "prepare-machine":
        prepare_machine(root, args.receipt, args.next_manifest, args.manifest_sha256)
    elif args.action == "install-machine":
        install_machine(root, args.receipt, args.manifest_sha256)
    elif args.action == "finish-user":
        finish_user(root, args.receipt, startup=not args.no_startup)
    elif args.action == "login":
        login(root)
    elif args.action == "stop-user":
        require_token(False)
        manifest(root)
        controller(root, "stop")
    elif args.action == "uninstall-machine":
        uninstall_machine(root)
    elif args.action == "rollback-machine":
        rollback_machine(root, args.receipt)
    elif args.action == "restore-user":
        require_token(False)
        receipt = user_log("prepare.json")
        if receipt.exists() and read_json(receipt).get("prior_ready") and read_json(receipt).get("stop_intent") and root.exists():
            manifest(root)
            if digest(root / "runtime-config.json") == read_json(receipt).get("old_config_sha256"):
                controller(root, "start")
    else:
        manifest(root, args.manifest_sha256)
    print(json.dumps({"action": args.action, "status": "complete"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        try:
            path = (Path(sys.executable).parent.parent.parent / "installer-machine-error.json") if elevated() else user_log("lifecycle-error.json")
            write_json(path, {"status": "failed", "action": sys.argv[1] if len(sys.argv) > 1 else None,
                              "error": str(error), "time_utc": datetime.now(timezone.utc).isoformat()})
        except OSError:
            pass
        if sys.executable.lower().endswith("pythonw.exe"):
            ctypes.windll.user32.MessageBoxW(None, str(error), "Cantonese IME", 0x10)
        raise SystemExit(2)
