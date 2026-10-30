"""Stage a hash-checked native preview package without installing it.

The existing Python interpreter is an explicit external dependency. Compiler
tools, source dictionaries, evaluation corpora and user logs are not bundled.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_FONT_DIRECTORY = "assets/fonts/lxgw-wenkai-tc"
CANDIDATE_FONT_FILE = "LXGWWenKaiTC-Regular.ttf"
CANDIDATE_FONT_SHA256 = "6662480a3f95b4a90c80b189c679617a48adce3a505118a6af63ce5a482b83fa"
CANDIDATE_FONT_SIZE = 15341148
CANDIDATE_FONT_OFL_SHA256 = "1a13783bf4337242834b60309cdcdbeb566ae3f923576fdbba8b78db9d174def"
CANDIDATE_FONT_SOURCE_COMMIT = "7cde02609845fa4d0824dd88dced1ca4669d4825"


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(part)
    return checksum.hexdigest()


class _InputPins:
    """Keep original validated hashes, including source files not distributed."""

    def __init__(self):
        self.expected: dict[Path, str] = {}

    def bind(self, path: Path, expected: str) -> None:
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("A validated package input lacks its original SHA256 pin.")
        path = path.resolve()
        previous = self.expected.setdefault(path, expected)
        if previous != expected:
            raise ValueError(f"Conflicting validated package input pins: {path}")

    def verify(self) -> None:
        for path, expected in self.expected.items():
            if not path.is_file() or digest(path) != expected:
                raise ValueError(f"Validated package input changed after source validation: {path}")


def _read_pinned_json(path: Path, expected: str, message: str, pins: _InputPins) -> dict:
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(message)
    pins.bind(path, expected)
    return json.loads(payload.decode("utf-8"))


def _pin_locked_files(lock: dict, pins: _InputPins) -> None:
    """Use the checked in-memory lock, never newly hashed mutable notice bytes."""
    for item in lock["files"]:
        relative = (Path(lock["local_directory"]) / item["name"]).as_posix()
        pins.bind(_project_file(relative), item["sha256"])


def amd64(path: Path) -> bool:
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            return False
        stream.seek(60)
        position = struct.unpack("<I", stream.read(4))[0]
        stream.seek(position)
        return stream.read(4) == b"PE\0\0" and stream.read(2) == b"\x64\x86"


def _project_file(relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("A package source notice must be a project-relative file.")
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
        raise ValueError(f"Package source notice is missing or outside the project: {relative}")
    return path


def _candidate_font_inputs(*, pins: _InputPins | None = None) -> dict[str, Path]:
    """Select the reviewed unmodified font and every required original notice.

    Keep the hashes from the first raw lock read. A later self-consistent lock
    or freshly hashed notice must not replace an already validated input pin.
    """
    pins = pins or _InputPins()
    relative_lock = CANDIDATE_FONT_DIRECTORY + "/sources.lock.json"
    lock_path = _project_file(relative_lock)
    payload = lock_path.read_bytes()
    pins.bind(lock_path, hashlib.sha256(payload).hexdigest())
    lock = json.loads(payload.decode("utf-8"))
    fixed = {
        "schema_version": 1, "family": "LXGW WenKai TC", "style": "Regular",
        "license": "OFL-1.1", "source_repository": "https://github.com/lxgw/LxgwWenKaiTC",
        "source_commit": CANDIDATE_FONT_SOURCE_COMMIT,
        "local_directory": CANDIDATE_FONT_DIRECTORY, "font_file": CANDIDATE_FONT_FILE,
        "font_sha256": CANDIDATE_FONT_SHA256, "font_size_bytes": CANDIDATE_FONT_SIZE,
    }
    if (not isinstance(lock, dict) or set(lock) != set(fixed) | {"modified", "files"}
            or any(lock.get(key) != value for key, value in fixed.items())
            or lock.get("modified") is not False):
        raise ValueError("Candidate font lock differs from the reviewed unmodified LXGW WenKai TC Regular asset.")
    upstream = {
        "AUTHORS.txt": "AUTHORS.txt", "CONTRIBUTORS.txt": "CONTRIBUTORS.txt",
        CANDIDATE_FONT_FILE: "fonts/TTF/" + CANDIDATE_FONT_FILE,
        "OFL.txt": "OFL.txt", "UPSTREAM_README.md": "README.md",
    }
    required = set(upstream) | {"NOTICE.md"}
    records = lock.get("files")
    if (not isinstance(records, list) or len(records) != len(required)
            or any(not isinstance(item, dict) for item in records)
            or any(not isinstance(item.get("name"), str) for item in records)
            or {item.get("name") for item in records} != required):
        raise ValueError("Candidate font lock must retain every required font and license/attribution notice exactly once.")
    result = {relative_lock: lock_path}
    for item in records:
        name = item["name"]
        expected_keys = {"name", "sha256", "size_bytes"}
        if name in upstream:
            expected_keys |= {"upstream_path", "upstream_git_blob_sha"}
            if (item.get("upstream_path") != upstream[name]
                    or not isinstance(item.get("upstream_git_blob_sha"), str)
                    or not re.fullmatch(r"[a-f0-9]{40}", item["upstream_git_blob_sha"])):
                raise ValueError("Candidate font lock contains an unexpected upstream file location.")
        else:
            expected_keys.add("origin")
            if item.get("origin") != "Project-authored packaging notice":
                raise ValueError("Candidate font attribution notice has an unexpected origin.")
        size = item.get("size_bytes")
        if (set(item) != expected_keys or type(size) is not int or size <= 0 or size > 64 * 1024 * 1024
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise ValueError("Candidate font lock contains an invalid file pin.")
        if name == CANDIDATE_FONT_FILE and (item["sha256"] != CANDIDATE_FONT_SHA256 or size != CANDIDATE_FONT_SIZE):
            raise ValueError("Candidate font file pin differs from the reviewed font.")
        if name == "OFL.txt" and item["sha256"] != CANDIDATE_FONT_OFL_SHA256:
            raise ValueError("Candidate font must retain the complete original OFL copyright and license.")
        relative = CANDIDATE_FONT_DIRECTORY + "/" + name
        path = _project_file(relative)
        if path.stat().st_size != size or digest(path) != item["sha256"]:
            raise ValueError("Candidate font asset differs from its source lock: " + name)
        pins.bind(path, item["sha256"])
        result[relative] = path
    return result


def _wordnet_notice_inputs(metadata: dict, *, pins: _InputPins | None = None) -> dict[str, Path]:
    """Retain the selected source's original notices even without Markdown links."""
    pins = pins or _InputPins()
    source = metadata.get("source_metadata", {}).get("supplements", {}).get("wordnet")
    if source is None:
        return {}
    if (source.get("source_id") != "wordnet:cantown" or source.get("license") != "CC-BY-4.0"
            or source.get("insertion_phase") != "post_autoencoding"
            or source.get("build_selection_status") != "selected_default_post_autoencoding"
            or source.get("parser_file") != "cantonese_ime/wordnet.py"):
        raise ValueError("Selected Wordnet source metadata does not match the reviewed import policy.")
    lock_path = _project_file(source["source_lock_file"])
    lock = _read_pinned_json(lock_path, source["source_lock_sha256"],
                            "Selected Wordnet source lock changed after the dictionary build.", pins)
    if any(source.get(key) != value for key, value in lock.items()):
        raise ValueError("Embedded Wordnet source metadata differs from its original lock.")
    parser_path = _project_file(source["parser_file"])
    if digest(parser_path) != source["parser_sha256"]:
        raise ValueError("Wordnet parser changed after the dictionary build.")
    pins.bind(parser_path, source["parser_sha256"])
    notices = {source["source_lock_file"]: lock_path}
    records = {item["name"]: item for item in lock["files"]}
    for name in ("README.md", "ATTRIBUTION.md", "LICENSE-CC-BY-4.0.txt"):
        relative = (Path(lock["local_directory"]) / name).as_posix()
        path = _project_file(relative)
        record = records.get(name, {})
        if digest(path) != record.get("sha256") or path.stat().st_size != record.get("size_bytes"):
            raise ValueError(f"Selected Wordnet source notice differs from its locked snapshot: {name}")
        notices[relative] = path
    _pin_locked_files(lock, pins)
    return notices


def _verify_mapping_snapshot(dictionary_hash: str | None, *, pins: _InputPins | None = None) -> dict:
    pins = pins or _InputPins()
    directory = ROOT / "data/mappings"
    manifest_path = directory / "build.manifest.json"
    payload = manifest_path.read_bytes()
    pins.bind(manifest_path, hashlib.sha256(payload).hexdigest())
    manifest = json.loads(payload.decode("utf-8"))
    documents = {}
    for name in ("syllables.json", "spellings.json"):
        path = directory / name
        metadata = _read_pinned_json(path, manifest.get("files", {}).get(name),
                                    f"Mapping artifact differs from its build manifest: {name}", pins)
        documents[name] = metadata
        if dictionary_hash is not None and metadata.get("source_metadata", {}).get("database", {}).get("sha256") != dictionary_hash:
            raise ValueError(f"Mapping artifact is bound to another dictionary: {name}")
    module = ROOT / "cantonese_ime/mappings.py"
    if digest(module) != manifest.get("module_sha256"):
        raise ValueError("Mapping implementation changed after the mapping artifacts were built.")
    pins.bind(module, manifest["module_sha256"])
    return {"manifest": manifest, "documents": documents}


def _kfcd_notice_inputs(*, pins: _InputPins | None = None, mapping_snapshot: dict | None = None) -> dict[str, Path]:
    """Bind the selected Yale profile to its literal source and original notices."""
    pins = pins or _InputPins()
    snapshot = mapping_snapshot or _verify_mapping_snapshot(None, pins=pins)
    manifest = snapshot["manifest"]
    sources = [snapshot["documents"][name]
               .get("source_metadata", {}).get("yale_kfcd_numeric")
               for name in ("syllables.json", "spellings.json")]
    if sources[0] != sources[1]:
        raise ValueError("Mapping artifacts disagree on selected KFCD Yale source evidence.")
    source = sources[0]
    if source is None:
        if manifest.get("profile_version", "").startswith("cantonese-yale-ascii-v3"):
            raise ValueError("The selected Yale v3 profile lacks its KFCD source evidence.")
        return {}
    if (source.get("source_id") != "yale_kfcd_numeric" or source.get("license") != "CC-BY-3.0"
            or source.get("verifier_file") != "cantonese_ime/yale_sources.py"):
        raise ValueError("Selected KFCD Yale source metadata differs from the reviewed policy.")
    lock_path = _project_file(source["source_lock_file"])
    lock = _read_pinned_json(lock_path, source.get("source_lock_sha256"),
                            "Selected KFCD Yale source lock changed after mapping generation.", pins)
    verifier = _project_file(source["verifier_file"])
    if digest(verifier) != source.get("verifier_sha256"):
        raise ValueError("KFCD Yale source verifier changed after mapping generation.")
    pins.bind(verifier, source["verifier_sha256"])
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from cantonese_ime.yale_sources import verify_kfcd_yale_source
    current = verify_kfcd_yale_source(ROOT, lock_path, alternates=source["alternate_spellings"],
                                     extension_bases=tuple(source["literal_extension_bases"]))
    if any(source.get(key) != value for key, value in current.items()):
        raise ValueError("Selected KFCD Yale mapping evidence differs from its retained source.")
    result = {source["source_lock_file"]: lock_path}
    for item in lock["files"]:
        relative = (Path(lock["local_directory"]) / item["name"]).as_posix()
        if source["files_sha256"].get(relative) != item["sha256"]:
            raise ValueError("KFCD Yale file pin differs from validated mapping metadata.")
        pins.bind(_project_file(relative), source["files_sha256"][relative])
        if item["name"] == "pingyambiu":
            continue
        result[relative] = _project_file(relative)
    return result


def _cccedict_headword_notice_inputs(metadata: dict, *, pins: _InputPins | None = None) -> dict[str, Path]:
    """Retain selected written-form notices and guard its complete build policy."""
    pins = pins or _InputPins()
    source = metadata.get("source_metadata", {}).get("supplements", {}).get("cccedict_headwords")
    if source is None:
        if ("headword_encoding" in metadata
                or "cccedict_headwords" in metadata.get("supplement_stats", {})
                or "cccedict_headwords" in metadata.get("supplement_order", [])
                or "cccedict_headwords" in metadata.get("post_autoencoding_supplements", [])):
            raise ValueError("Selected CC-CEDICT headword source block is missing from dictionary metadata.")
        return {}
    if (source.get("source_id") != "cccedict:headwords" or source.get("license") != "CC-BY-SA-4.0"
            or source.get("insertion_phase") != "post_autoencoding_post_wordnet"
            or source.get("authored_cantonese_readings") is not False
            or source.get("build_selection_status") not in (
                "selected_opt_in_post_wordnet_autoencoding", "selected_default_post_wordnet_autoencoding")):
        raise ValueError("Selected CC-CEDICT headword metadata differs from the reviewed generated-reading policy.")
    lock_path = _project_file(source["source_lock_file"])
    lock = _read_pinned_json(lock_path, source.get("source_lock_sha256"),
                            "Selected CC-CEDICT headword source lock changed after dictionary generation.", pins)
    if any(source.get(key) != value for key, value in lock.items()):
        raise ValueError("Selected CC-CEDICT headword metadata differs from its original lock.")
    for role, relative in (("parser", "cantonese_ime/cccedict_headwords.py"),
                           ("encoder", "cantonese_ime/autoencode.py"),
                           ("builder", "cantonese_ime/dictionary.py"),
                           ("build_script", "tools/build_dictionary.py")):
        if source.get(role + "_file") != relative or digest(_project_file(relative)) != source.get(role + "_sha256"):
            raise ValueError("Selected CC-CEDICT headword " + role + " changed after dictionary generation.")
        pins.bind(_project_file(relative), source[role + "_sha256"])
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from cantonese_ime.cccedict_headwords import verify_cccedict_headwords_snapshot
    verify_cccedict_headwords_snapshot((ROOT / lock["local_directory"]).resolve(), lock)
    _pin_locked_files(lock, pins)
    notices = {source["source_lock_file"]: lock_path}
    for name in ("ATTRIBUTION.md", "LICENSE-CC-BY-SA-4.0.txt", "download-page.html"):
        relative = (Path(lock["local_directory"]) / name).as_posix()
        notices[relative] = _project_file(relative)
    return notices


def _yue_wiktionary_notice_inputs(metadata: dict, *, pins: _InputPins | None = None) -> dict[str, Path]:
    """Retain the selected explicit source's history/license attribution notices."""
    pins = pins or _InputPins()
    source = metadata.get("source_metadata", {}).get("supplements", {}).get("yue_wiktionary")
    if source is None:
        if ("yue_wiktionary" in metadata
                or "yue_wiktionary" in metadata.get("supplement_stats", {})
                or "yue_wiktionary" in metadata.get("supplement_order", [])
                or "yue_wiktionary" in metadata.get("post_autoencoding_supplements", [])):
            raise ValueError("Selected Yue Wiktionary source block is missing from dictionary metadata.")
        return {}
    if (source.get("source_id") != "wiktionary:yue" or source.get("license") != "CC-BY-SA-4.0"
            or source.get("insertion_phase") != "post_autoencoding_post_wordnet_post_cccedict_headwords"
            or source.get("build_selection_status") not in (
                "selected_opt_in_explicit_post_cccedict", "selected_default_explicit_post_cccedict")
            or source.get("source_explicit_cantonese_readings") is not True
            or source.get("independent_linguistic_approval") is not False):
        raise ValueError("Selected Yue Wiktionary metadata differs from the reviewed explicit-source policy.")
    lock_path = _project_file(source["source_lock_file"])
    lock = _read_pinned_json(lock_path, source.get("source_lock_sha256"),
                            "Selected Yue Wiktionary source lock changed after dictionary generation.", pins)
    if any(source.get(key) != value for key, value in lock.items()):
        raise ValueError("Selected Yue Wiktionary metadata differs from its original lock.")
    for role, relative in (("parser", "cantonese_ime/yue_wiktionary.py"),
                           ("builder", "cantonese_ime/dictionary.py"),
                           ("build_script", "tools/build_dictionary.py")):
        if source.get(role + "_file") != relative or digest(_project_file(relative)) != source.get(role + "_sha256"):
            raise ValueError("Selected Yue Wiktionary " + role + " changed after dictionary generation.")
        pins.bind(_project_file(relative), source[role + "_sha256"])
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from cantonese_ime.yue_wiktionary import verify_yue_wiktionary_snapshot
    verify_yue_wiktionary_snapshot((ROOT / lock["local_directory"]).resolve(), lock)
    _pin_locked_files(lock, pins)
    notices = {source["source_lock_file"]: lock_path}
    # The retained lock includes template revision/history attribution links.
    # Original template text, archive, inventory and research data stay out.
    for name in ("ATTRIBUTION.md", "LICENSE-CC-BY-SA-4.0.txt",
                 "yue-page-licensing-sample.html", "wikimedia-terms.html"):
        relative = (Path(lock["local_directory"]) / name).as_posix()
        notices[relative] = _project_file(relative)
    return notices


def stage(output: Path, python: Path, *, bundled_runtime: Path | None = None, release: bool = False) -> dict:
    if output.exists():
        raise ValueError("Output already exists; choose a new package directory.")
    if not amd64(python):
        raise ValueError("The selected external Python must be AMD64.")
    pins = _InputPins()
    candidate_font = _candidate_font_inputs(pins=pins)
    python_hash = digest(python)
    pins.bind(python, python_hash)
    dictionary = ROOT / "data/generated/lexicon.sqlite3"
    dictionary_manifest = ROOT / "data/generated/lexicon.manifest.json"
    manifest_payload = dictionary_manifest.read_bytes()
    pins.bind(dictionary_manifest, hashlib.sha256(manifest_payload).hexdigest())
    metadata = json.loads(manifest_payload.decode("utf-8"))
    dictionary_hash = digest(dictionary)
    if dictionary_hash != metadata["database_sha256"]:
        raise ValueError("Dictionary differs from its source manifest.")
    pins.bind(dictionary, dictionary_hash)
    mapping_snapshot = _verify_mapping_snapshot(dictionary_hash, pins=pins)
    wordnet_notices = _wordnet_notice_inputs(metadata, pins=pins)
    kfcd_notices = _kfcd_notice_inputs(pins=pins, mapping_snapshot=mapping_snapshot)
    headword_notices = _cccedict_headword_notice_inputs(metadata, pins=pins)
    yue_notices = _yue_wiktionary_notice_inputs(metadata, pins=pins)
    build_manifest_path = ROOT / ".local/native/build-x64/build-manifest.json"
    build_payload = build_manifest_path.read_bytes()
    pins.bind(build_manifest_path, hashlib.sha256(build_payload).hexdigest())
    build = json.loads(build_payload.decode("utf-8-sig"))
    dll = ROOT / ".local/native/build-x64/CantoneseIME.dll"
    built = next(item for item in build["artifacts"] if Path(item["path"]).name == dll.name)
    if not amd64(dll) or digest(dll) != built["sha256"]:
        raise ValueError("Native DLL does not match the recorded AMD64 build.")
    pins.bind(dll, built["sha256"])
    settings_app = ROOT / ".local/native/build-x64/CantoneseImeSettings.exe"
    settings_built = next((item for item in build["artifacts"] if Path(item["path"]).name == settings_app.name), None)
    if settings_built is None or not amd64(settings_app) or digest(settings_app) != settings_built["sha256"]:
        raise ValueError("Settings executable does not match the recorded AMD64 build.")
    pins.bind(settings_app, settings_built["sha256"])
    # Package exactly the source revision used by the recorded native build.
    for item in build["sources"]:
        if digest(Path(item["path"])) != item["sha256"]:
            raise ValueError(f"Native source changed after compilation: {item['path']}")
        pins.bind(Path(item["path"]), item["sha256"])
    helper = ROOT / ".local/native/build-x64/CantoneseMachineRegistration.exe"
    guard_report = ROOT / ".local/native/machine-registration-guard-checks.json"
    guard_payload = guard_report.read_bytes()
    pins.bind(guard_report, hashlib.sha256(guard_payload).hexdigest())
    guards = json.loads(guard_payload.decode("utf-8"))
    if guards.get("passed") is not True or not guards.get("checks") or not all(item.get("passed") is True for item in guards["checks"]):
        raise ValueError("Registration helper guard checks are incomplete or failing.")
    if not amd64(helper) or digest(helper) != guards["helper_sha256"]:
        raise ValueError("Registration helper differs from its reviewed guard-test artifact.")
    pins.bind(helper, guards["helper_sha256"])

    selected: dict[str, Path] = {}

    def add(relative: str, source: Path | None = None):
        path = source or ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(f"Required package input is missing: {path}")
        selected[relative] = path

    for path in sorted((ROOT / "cantonese_ime").glob("*.py")):
        add(path.relative_to(ROOT).as_posix(), path)
    for relative in ("data/generated/lexicon.sqlite3", "data/generated/lexicon.manifest.json",
                     "data/mappings/syllables.json", "data/mappings/spellings.json", "data/mappings/build.manifest.json",
                     "data/vendor/scowl/2026.02.25/en_GB-ise.txt",
                     "native/backend/server.py", "native/runtime/control.py",
                     "native/runtime/Start-Runtime.ps1", "native/runtime/README.md",
                     "native/Install-NativeIme.ps1", "native/Remove-NativeIme.ps1",
                     "native/Update-NativeIme.ps1", "native/UPGRADING.md"):
        add(relative)
    if release:
        # The standard release installer owns installation and removal. The
        # legacy manually-reviewed PowerShell package workflow is developer
        # tooling, and cannot manage a private-runtime release manifest.
        for relative in ("native/Install-NativeIme.ps1", "native/Remove-NativeIme.ps1",
                         "native/Update-NativeIme.ps1", "native/UPGRADING.md"):
            selected.pop(relative)
    add("native/CantoneseIME.dll", dll)
    add("native/CantoneseImeSettings.exe", settings_app)
    add("native/CantoneseMachineRegistration.exe", helper)
    add("native/registration-guard-checks.json", guard_report)
    add("native/build-manifest.json", ROOT / ".local/native/build-x64/build-manifest.json")
    for relative, source in candidate_font.items():
        add(relative, source)
    add("data/THIRD_PARTY_NOTICES.md")
    # Retain local notices, declarations, source locks and audit links. Tables
    # and corpora themselves are not among this notice's Markdown references.
    notices = (ROOT / "data/THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", notices):
        if "://" in link or link.startswith("#"):
            continue
        source = (ROOT / "data" / link).resolve()
        source.relative_to(ROOT)
        add(source.relative_to(ROOT).as_posix(), source)
    for relative, source in wordnet_notices.items():
        add(relative, source)
    for relative, source in kfcd_notices.items():
        add(relative, source)
    for relative, source in headword_notices.items():
        add(relative, source)
    for relative, source in yue_notices.items():
        add(relative, source)
    toolchain = json.loads((ROOT / "native/toolchain.lock.json").read_text(encoding="utf-8"))
    toolchain_root = ROOT / toolchain["local_directory"]
    add("licenses/LLVM-LICENSE.TXT", toolchain_root / "LICENSE.TXT")
    for path in sorted((toolchain_root / "x86_64-w64-mingw32/share/mingw32").glob("COPYING*")):
        add("licenses/" + path.name, path)
    if bundled_runtime is not None:
        bundled_runtime = bundled_runtime.resolve(strict=True)
        for path in sorted(bundled_runtime.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                add("runtime/python/" + path.relative_to(bundled_runtime).as_posix(), path)
        if python.resolve() != (bundled_runtime / "python.exe").resolve():
            raise ValueError("A private runtime must use its own python.exe.")
        if not (bundled_runtime / "LICENSE.txt").is_file():
            raise ValueError("The private Python runtime lacks its original license.")
    elif release:
        raise ValueError("Release packages require a private Python runtime.")

    # Preserve original source-lock/build pins. Fresh hashes cannot replace a
    # validated notice hash if another writer edits a file after validation.
    expected = {name: (pins.expected[path.resolve()] if path.resolve() in pins.expected else digest(path))
                for name, path in selected.items()}
    pins.verify()
    output.mkdir(parents=True)
    records = []
    for name, source in selected.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if digest(target) != expected[name] or digest(source) != expected[name]:
            raise RuntimeError(f"Package input changed while copying: {name}")
        records.append({"path": name, "bytes": target.stat().st_size, "sha256": expected[name]})
    config = output / "runtime-config.json"
    configured_python = output / "runtime/python/python.exe" if bundled_runtime is not None else python
    subprocess.run([str(configured_python), "-B", str(output / "native/runtime/control.py"), "configure",
                    "--install-root", str(output), "--python", str(configured_python), "--config", str(config)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
    records.append({"path": config.name, "bytes": config.stat().st_size, "sha256": digest(config)})
    # Recheck both original sources and copied bytes. Restoring an edited source
    # cannot bless a changed staged notice, even after configuration succeeds.
    pins.verify()
    for name, source in selected.items():
        if digest(source) != expected[name] or digest(output / name) != expected[name]:
            raise RuntimeError(f"Package input or staged file changed before publication: {name}")
    manifest = {
        "schema_version": 1,
        "kind": "Cantonese IME native AMD64 release runtime package" if release else "Cantonese IME native AMD64 preview runtime package",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "native_dll_sha256": built["sha256"],
        "dictionary_sha256": dictionary_hash,
        "files": sorted(records, key=lambda item: item["path"]),
        "total_payload_bytes": sum(item["bytes"] for item in records),
        "installed": False,
        "notes": ["Staging does not change registry, startup or input methods.",
                  "Persistent TSF installation and ordinary-application acceptance remain separate.",
                  ("A private AMD64 Python runtime is included; no compiler or evaluation corpus is included."
                   if bundled_runtime is not None else "No compiler, interpreter distribution, raw dictionary table or evaluation corpus is included.")],
    }
    if bundled_runtime is not None:
        manifest["bundled_python"] = {"path": "runtime/python/python.exe", "sha256": python_hash, "architecture": "AMD64"}
    else:
        manifest["external_python"] = {"path": str(python.resolve()), "sha256": python_hash, "architecture": "AMD64"}
    (output / "package-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/native/packages" /
                        datetime.now(timezone.utc).strftime("preview-%Y%m%d-%H%M%S"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    manifest = stage(args.output.resolve(), args.python.resolve(strict=True))
    print(json.dumps({"output": str(args.output.resolve()), "files": len(manifest["files"]),
                      "bytes": manifest["total_payload_bytes"], "installed": False}, indent=2))


if __name__ == "__main__":
    main()
