"""Synthetic package staging only; no installed files or programs are used."""
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import package_native as package


def pe_bytes():
    header = bytearray(70)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    header[64:70] = b"PE\0\0\x64\x86"
    return bytes(header)


class NativePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-package-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root_patch = patch.object(package, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        # A tiny synthetic sfnt placeholder keeps package fixtures independent
        # of the 15 MB production font. Only this fixture's reviewed pins are
        # overridden; lock/policy/copy validation is exercised normally.
        self.font_directory = package.CANDIDATE_FONT_DIRECTORY
        font_files = []
        for name, content, upstream in (
            (package.CANDIDATE_FONT_FILE, b"\x00\x01\x00\x00synthetic reviewed font", "fonts/TTF/" + package.CANDIDATE_FONT_FILE),
            ("OFL.txt", b"Synthetic complete reviewed OFL/copyright placeholder", "OFL.txt"),
            ("AUTHORS.txt", b"Synthetic original authors", "AUTHORS.txt"),
            ("CONTRIBUTORS.txt", b"Synthetic original contributors", "CONTRIBUTORS.txt"),
            ("UPSTREAM_README.md", b"Synthetic upstream source README", "README.md"),
            ("NOTICE.md", b"Synthetic project font attribution", None),
        ):
            path = self.write(self.font_directory + "/" + name, content)
            record = {"name": name, "sha256": package.digest(path), "size_bytes": len(content)}
            if upstream:
                record.update(upstream_path=upstream, upstream_git_blob_sha="a" * 40)
            else:
                record["origin"] = "Project-authored packaging notice"
            font_files.append(record)
        self.font_file = self.root / self.font_directory / package.CANDIDATE_FONT_FILE
        for key, value in (("CANDIDATE_FONT_SHA256", package.digest(self.font_file)),
                           ("CANDIDATE_FONT_SIZE", self.font_file.stat().st_size),
                           ("CANDIDATE_FONT_OFL_SHA256", package.digest(self.root / self.font_directory / "OFL.txt"))):
            override = patch.object(package, key, value)
            override.start()
            self.addCleanup(override.stop)
        self.font_lock = {
            "schema_version": 1, "family": "LXGW WenKai TC", "style": "Regular", "license": "OFL-1.1",
            "source_repository": "https://github.com/lxgw/LxgwWenKaiTC",
            "source_commit": package.CANDIDATE_FONT_SOURCE_COMMIT, "local_directory": self.font_directory,
            "font_file": package.CANDIDATE_FONT_FILE, "font_sha256": package.CANDIDATE_FONT_SHA256,
            "font_size_bytes": package.CANDIDATE_FONT_SIZE, "modified": False, "files": font_files,
        }
        self.font_lock_path = self.json(self.font_directory + "/sources.lock.json", self.font_lock)
        self.python = self.write("external-python.exe", pe_bytes())
        self.write("cantonese_ime/__init__.py", b"# fixture\n")
        self.mapping_module = self.write("cantonese_ime/mappings.py", b"# synthetic mapping implementation\n")
        self.wordnet_parser = self.write("cantonese_ime/wordnet.py", b"# synthetic Wordnet parser\n")
        database = self.write("data/generated/lexicon.sqlite3", b"synthetic dictionary, not a production DB\n")
        self.dictionary_hash = package.digest(database)
        self.vendor = "data/vendor/cantonese-wordnet/frozen-fixture"
        files = []
        for name, content in (
            ("README.md", "Synthetic upstream README and attribution"),
            ("ATTRIBUTION.md", "Synthetic transformation/citation notice"),
            ("LICENSE-CC-BY-4.0.txt", "Synthetic full-license placeholder"),
            ("cantown.xml", "<synthetic-raw-table-not-to-be-bundled/>"),
            ("parser-attested-bases.json", '{"synthetic":true}'),
        ):
            path = self.write(self.vendor + "/" + name, content.encode())
            files.append({"name": name, "sha256": package.digest(path), "size_bytes": path.stat().st_size})
        self.wordnet_lock = {
            "source_id": "wordnet:cantown", "license": "CC-BY-4.0",
            "local_directory": self.vendor, "files": files,
            "selection_status": "Original immutable synthetic candidate preparation status",
        }
        self.lock_path = self.json("data/supplements/wordnet.sources.lock.json", self.wordnet_lock)
        selected = dict(self.wordnet_lock,
            source_lock_file="data/supplements/wordnet.sources.lock.json",
            source_lock_sha256=package.digest(self.lock_path),
            parser_file="cantonese_ime/wordnet.py", parser_sha256=package.digest(self.wordnet_parser),
            insertion_phase="post_autoencoding", build_selection_status="selected_default_post_autoencoding")
        self.dictionary_metadata = {"database_sha256": self.dictionary_hash,
                                    "source_metadata": {"supplements": {"wordnet": selected}}}
        self.source_manifest = self.json("data/generated/lexicon.manifest.json", self.dictionary_metadata)
        self.mapping_names = ("syllables.json", "spellings.json")
        for name in self.mapping_names:
            self.json("data/mappings/" + name,
                      {"source_metadata": {"database": {"sha256": self.dictionary_hash}}})
        self.mapping_manifest = {"files": {name: package.digest(self.root / "data/mappings" / name)
                                           for name in self.mapping_names},
                                 "module_sha256": package.digest(self.mapping_module)}
        self.json("data/mappings/build.manifest.json", self.mapping_manifest)
        self.write("data/vendor/scowl/2026.02.25/en_GB-ise.txt", b"meeting\n")
        dll = self.write(".local/native/build-x64/CantoneseIME.dll", pe_bytes())
        settings_app = self.write(".local/native/build-x64/CantoneseImeSettings.exe", pe_bytes() + b"settings")
        native_source = self.write("native/tsf/synthetic-source.cpp", b"// synthetic native source\n")
        self.json(".local/native/build-x64/build-manifest.json", {
            "artifacts": [{"path": str(dll), "sha256": package.digest(dll)},
                          {"path": str(settings_app), "sha256": package.digest(settings_app)}],
            "sources": [{"path": str(native_source), "sha256": package.digest(native_source)}]})
        helper = self.write(".local/native/build-x64/CantoneseMachineRegistration.exe", pe_bytes())
        self.json(".local/native/machine-registration-guard-checks.json", {
            "passed": True, "checks": [{"passed": True}], "helper_sha256": package.digest(helper)})
        for name in ("native/backend/server.py", "native/runtime/control.py", "native/runtime/Start-Runtime.ps1",
                     "native/runtime/README.md", "native/Install-NativeIme.ps1", "native/Remove-NativeIme.ps1",
                     "native/Update-NativeIme.ps1", "native/UPGRADING.md"):
            self.write(name, b"# synthetic fixture\n")
        # No Markdown source links: selected source notices must be retained
        # through the dictionary manifest, not an incidental documentation link.
        self.write("data/THIRD_PARTY_NOTICES.md", b"Synthetic distribution notice.\n")
        self.json("native/toolchain.lock.json", {"local_directory": "toolchain-fixture"})
        self.write("toolchain-fixture/LICENSE.TXT", b"synthetic toolchain notice")
        self.write("toolchain-fixture/x86_64-w64-mingw32/share/mingw32/COPYING.fixture", b"synthetic runtime notice")
        self.output = self.root / "new-package"

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def json(self, relative, value):
        return self.write(relative, (json.dumps(value, indent=2) + "\n").encode())

    def save_source_manifest(self):
        self.json("data/generated/lexicon.manifest.json", self.dictionary_metadata)

    def fake_configure(self, args, **options):
        self.assertEqual(args[:2], [str(self.python), "-B"])
        self.assertEqual(options["timeout"], 45)
        Path(args[args.index("--config") + 1]).write_text('{"synthetic_config":true}\n', encoding="utf-8")

    def stage(self):
        with patch.object(package.subprocess, "run", side_effect=self.fake_configure) as launch:
            result = package.stage(self.output, self.python)
        self.assertEqual(launch.call_count, 1)
        return result

    def assert_rejected_before_staging(self, message):
        with patch.object(package.subprocess, "run") as launch, self.assertRaisesRegex(ValueError, message):
            package.stage(self.output, self.python)
        self.assertFalse(self.output.exists())
        launch.assert_not_called()

    def test_selected_wordnet_retains_runtime_module_and_all_notices_without_bundling_raw_source(self):
        manifest = self.stage()
        records = {record["path"]: record for record in manifest["files"]}
        required = {"cantonese_ime/wordnet.py", "data/supplements/wordnet.sources.lock.json",
                    self.vendor + "/README.md", self.vendor + "/ATTRIBUTION.md",
                    self.vendor + "/LICENSE-CC-BY-4.0.txt"}
        self.assertTrue(required <= records.keys())
        self.assertNotIn(self.vendor + "/cantown.xml", records)
        self.assertNotIn(self.vendor + "/parser-attested-bases.json", records)
        for name in required:
            self.assertEqual((self.output / name).read_bytes(), (self.root / name).read_bytes())
            self.assertEqual(records[name]["sha256"], package.digest(self.output / name))
        self.assertEqual(manifest["dictionary_sha256"], self.dictionary_hash)
        self.assertFalse(manifest["installed"])
        self.assertEqual(self.lock_path.read_bytes(), (self.output / "data/supplements/wordnet.sources.lock.json").read_bytes())

    def test_registration_helper_is_bound_to_its_successful_guard_report(self):
        manifest = self.stage()
        helper = self.output / "native/CantoneseMachineRegistration.exe"
        guards = json.loads((self.output / "native/registration-guard-checks.json").read_text())
        records = {row["path"]: row for row in manifest["files"]}
        self.assertTrue(guards["passed"])
        self.assertEqual(package.digest(helper), guards["helper_sha256"])
        self.assertEqual(records["native/CantoneseMachineRegistration.exe"]["sha256"], guards["helper_sha256"])

    def test_new_helper_with_stale_guard_report_is_rejected_before_staging(self):
        helper = self.root / ".local/native/build-x64/CantoneseMachineRegistration.exe"
        helper.write_bytes(helper.read_bytes() + b"different helper revision")
        self.assert_rejected_before_staging("differs from its reviewed guard-test artifact")

    def test_failing_or_incomplete_registration_guards_are_rejected(self):
        helper = self.root / ".local/native/build-x64/CantoneseMachineRegistration.exe"
        for report in (
                {"passed": False, "checks": [{"passed": True}]},
                {"passed": True, "checks": [{"passed": False}]},
                {"passed": True, "checks": []}):
            with self.subTest(report=report):
                self.json(".local/native/machine-registration-guard-checks.json",
                          dict(report, helper_sha256=package.digest(helper)))
                self.assert_rejected_before_staging("guard checks are incomplete or failing")

    def test_guard_report_change_after_configuration_prevents_publication(self):
        report = self.root / ".local/native/machine-registration-guard-checks.json"

        def changed_after_configure(args, **options):
            self.fake_configure(args, **options)
            report.write_bytes(report.read_bytes() + b" ")

        with patch.object(package.subprocess, "run", side_effect=changed_after_configure), \
                self.assertRaisesRegex(ValueError, "Validated package input changed"):
            package.stage(self.output, self.python)
        self.assertFalse((self.output / "package-manifest.json").exists())

    def test_old_dictionary_without_wordnet_has_no_new_notice_requirement(self):
        del self.dictionary_metadata["source_metadata"]["supplements"]["wordnet"]
        self.save_source_manifest()
        (self.root / self.vendor / "README.md").unlink()
        manifest = self.stage()
        self.assertNotIn(self.vendor + "/README.md", {record["path"] for record in manifest["files"]})

    def test_modified_selected_source_lock_is_rejected(self):
        self.lock_path.write_bytes(self.lock_path.read_bytes() + b" ")
        self.assert_rejected_before_staging("source lock changed")

    def test_modified_selected_notice_is_rejected(self):
        path = self.root / self.vendor / "README.md"
        path.write_bytes(path.read_bytes() + b"changed")
        self.assert_rejected_before_staging("notice differs")

    def test_missing_full_selected_license_is_rejected(self):
        (self.root / self.vendor / "LICENSE-CC-BY-4.0.txt").unlink()
        self.assert_rejected_before_staging("missing or outside")

    def test_parser_revision_must_match_selected_dictionary_source_metadata(self):
        self.wordnet_parser.write_bytes(self.wordnet_parser.read_bytes() + b"changed")
        self.assert_rejected_before_staging("parser changed")

    def test_embedded_lock_fields_must_preserve_original_source_selection_status(self):
        self.dictionary_metadata["source_metadata"]["supplements"]["wordnet"]["selection_status"] = "Changed raw source status"
        self.save_source_manifest()
        self.assert_rejected_before_staging("differs from its original lock")

    def test_selected_source_must_retain_post_autoencoding_policy(self):
        self.dictionary_metadata["source_metadata"]["supplements"]["wordnet"]["insertion_phase"] = "before_autoencoding"
        self.save_source_manifest()
        self.assert_rejected_before_staging("reviewed import policy")

    def test_self_consistent_mapping_artifacts_bound_to_old_dictionary_are_rejected(self):
        name = "spellings.json"
        self.json("data/mappings/" + name, {"source_metadata": {"database": {"sha256": "a" * 64}}})
        self.mapping_manifest["files"][name] = package.digest(self.root / "data/mappings" / name)
        self.json("data/mappings/build.manifest.json", self.mapping_manifest)
        self.assert_rejected_before_staging("bound to another dictionary")

    def test_mapping_artifact_or_implementation_changes_are_rejected(self):
        original = (self.root / "data/mappings/syllables.json").read_bytes()
        self.write("data/mappings/syllables.json", original + b" ")
        self.assert_rejected_before_staging("differs from its build manifest")
        self.write("data/mappings/syllables.json", original)
        self.mapping_module.write_bytes(self.mapping_module.read_bytes() + b"changed")
        self.assert_rejected_before_staging("Mapping implementation changed")

    def test_reviewed_font_and_all_six_notices_are_bound_in_package_manifest(self):
        manifest = self.stage()
        records = {row["path"]: row for row in manifest["files"]}
        names = {row["name"] for row in self.font_lock["files"]} | {"sources.lock.json"}
        self.assertEqual(len(names), 7)
        for name in names:
            relative = self.font_directory + "/" + name
            self.assertEqual(records[relative]["sha256"], package.digest(self.root / relative))
            self.assertEqual((self.output / relative).read_bytes(), (self.root / relative).read_bytes())

    def test_missing_font_or_required_notice_is_rejected_before_staging(self):
        for name in (package.CANDIDATE_FONT_FILE, "OFL.txt", "NOTICE.md"):
            path = self.root / self.font_directory / name
            original = path.read_bytes()
            with self.subTest(name=name):
                path.unlink()
                self.assert_rejected_before_staging("missing or outside")
                path.write_bytes(original)

    def test_tampered_font_or_ofl_is_rejected_before_staging(self):
        for name in (package.CANDIDATE_FONT_FILE, "OFL.txt"):
            path = self.root / self.font_directory / name
            original = path.read_bytes()
            with self.subTest(name=name):
                path.write_bytes(original + b" changed")
                self.assert_rejected_before_staging("differs from its source lock")
                path.write_bytes(original)

    def test_font_lock_cannot_select_another_family_style_license_or_location(self):
        for key, value in (("family", "Other Font"), ("style", "Bold"), ("license", "Other"),
                           ("modified", True), ("source_commit", "b" * 40),
                           ("source_repository", "https://example.invalid/font"),
                           ("local_directory", "../outside"), ("font_file", "other.ttf"),
                           ("font_sha256", "b" * 64), ("font_size_bytes", 1)):
            changed = dict(self.font_lock, **{key: value})
            with self.subTest(key=key):
                self.json(self.font_directory + "/sources.lock.json", changed)
                self.assert_rejected_before_staging("reviewed unmodified")
        self.json(self.font_directory + "/sources.lock.json", self.font_lock)

    def test_self_consistent_replacement_font_is_still_not_the_reviewed_font(self):
        self.font_file.write_bytes(b"different sfnt pretending to be same family")
        value = json.loads(json.dumps(self.font_lock))
        value["font_sha256"] = package.digest(self.font_file)
        value["font_size_bytes"] = self.font_file.stat().st_size
        row = next(row for row in value["files"] if row["name"] == package.CANDIDATE_FONT_FILE)
        row.update(sha256=value["font_sha256"], size_bytes=value["font_size_bytes"])
        self.json(self.font_directory + "/sources.lock.json", value)
        self.assert_rejected_before_staging("reviewed unmodified")

    def test_self_consistent_truncated_license_is_not_the_original_full_ofl(self):
        path = self.root / self.font_directory / "OFL.txt"
        path.write_bytes(b"OFL-1.1 abbreviated label")
        value = json.loads(json.dumps(self.font_lock))
        row = next(row for row in value["files"] if row["name"] == "OFL.txt")
        row.update(sha256=package.digest(path), size_bytes=path.stat().st_size)
        self.json(self.font_directory + "/sources.lock.json", value)
        self.assert_rejected_before_staging("complete original OFL")

    def test_font_lock_requires_exact_safe_file_inventory(self):
        for failure in ("missing", "duplicate", "upstream", "size"):
            value = json.loads(json.dumps(self.font_lock))
            if failure == "missing":
                value["files"].pop()
            elif failure == "duplicate":
                value["files"][-1] = value["files"][0]
            elif failure == "upstream":
                value["files"][0]["upstream_path"] = "../../elsewhere.ttf"
            else:
                value["files"][0]["size_bytes"] = -1
            with self.subTest(failure=failure):
                self.json(self.font_directory + "/sources.lock.json", value)
                self.assert_rejected_before_staging("Candidate font lock")

    def test_font_notice_edit_after_validation_cannot_replace_original_pin(self):
        notice = self.root / self.font_directory / "NOTICE.md"
        original = package._candidate_font_inputs

        def edited(**options):
            selected = original(**options)
            notice.write_bytes(notice.read_bytes() + b" after validation")
            return selected

        with patch.object(package, "_candidate_font_inputs", side_effect=edited), \
                patch.object(package.subprocess, "run") as launch, \
                self.assertRaisesRegex(ValueError, "Validated package input changed"):
            package.stage(self.output, self.python)
        self.assertFalse(self.output.exists())
        launch.assert_not_called()

    def test_font_notice_copy_race_restoring_source_still_cannot_publish(self):
        notice = self.root / self.font_directory / "OFL.txt"
        original_bytes = notice.read_bytes()
        copy_file = package.shutil.copyfile

        def raced(source, target, **options):
            if Path(source) == notice:
                notice.write_bytes(original_bytes + b" changed during copy")
                try:
                    return copy_file(source, target, **options)
                finally:
                    notice.write_bytes(original_bytes)
            return copy_file(source, target, **options)

        with patch.object(package.shutil, "copyfile", side_effect=raced), \
                patch.object(package.subprocess, "run") as launch, \
                self.assertRaisesRegex(RuntimeError, "input changed while copying"):
            package.stage(self.output, self.python)
        self.assertEqual(notice.read_bytes(), original_bytes)
        self.assertFalse((self.output / "package-manifest.json").exists())
        launch.assert_not_called()

    def test_font_lock_and_staged_notice_are_rechecked_after_configuration(self):
        for target in (self.font_lock_path, self.output / self.font_directory / "NOTICE.md"):
            with self.subTest(target=target):
                original = self.font_lock_path.read_bytes()

                def changed(args, **options):
                    self.fake_configure(args, **options)
                    target.write_bytes(target.read_bytes() + b" after configure")

                with patch.object(package.subprocess, "run", side_effect=changed), \
                        self.assertRaises((ValueError, RuntimeError)):
                    package.stage(self.output, self.python)
                self.assertFalse((self.output / "package-manifest.json").exists())
                self.font_lock_path.write_bytes(original)
                import shutil
                shutil.rmtree(self.output)

    def test_release_rechecks_original_font_assets_after_inno_compilation(self):
        from tools import build_release as release
        for name in ("tools/build_release.py", "tools/package_native.py", "installer/CantoneseIME.iss",
                     "installer/dependencies.lock.json", "pyproject.toml", "native/tsf/icons/cantonese.ico",
                     "installer/lifecycle.py", "installer/Set-UserInputSource.ps1",
                     "installer/GETTING_STARTED.txt", "installer/RELEASE_NOTICES.txt"):
            self.write(name, b"synthetic release support input")
        compiler = self.write("compiler/ISCC.exe", pe_bytes())
        self.write("compiler/license.txt", b"synthetic compiler license")
        runtime = self.root / "private-python"
        self.write("private-python/python.exe", pe_bytes())
        output = self.root / "synthetic-release"
        notice = self.root / self.font_directory / "NOTICE.md"
        original = notice.read_bytes()

        def stage_fixture(payload, *args, **options):
            payload.mkdir()
            asset = payload / "sentinel"
            asset.write_bytes(b"stable packaged content")
            records = [{"path": "sentinel", "bytes": asset.stat().st_size, "sha256": package.digest(asset)}]
            for relative, source in package._candidate_font_inputs().items():
                target = payload / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                records.append({"path": relative, "bytes": target.stat().st_size, "sha256": package.digest(target)})
            return {"files": records}

        def compile_fixture(args, **options):
            if args[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, "7.1.0", "")
            (output / "CantoneseIME-1.0.6-windows-x64-setup.exe").write_bytes(pe_bytes())
            notice.write_bytes(original + b" changed during compile")
            return subprocess.CompletedProcess(args, 0, "synthetic compiler success", "")

        with patch.object(release, "ROOT", self.root), \
                patch.object(release, "_candidate_font_inputs", side_effect=package._candidate_font_inputs), \
                patch.object(release, "project_version", return_value="1.0.6"), \
                patch.object(release, "tools", return_value=(runtime, compiler, {"python": {}, "inno_setup": {"version": "7.1.0"}})), \
                patch.object(release, "stage", side_effect=stage_fixture), \
                patch.object(release.subprocess, "run", side_effect=compile_fixture), \
                self.assertRaisesRegex(ValueError, "Validated package input changed"):
            release.build(output, allow_fetch=False, compile_setup=True)
        self.assertFalse((output / "release-build.json").exists())
        self.assertEqual((output / "package/sentinel").read_bytes(), b"stable packaged content")
        self.assertEqual((output / "package" / self.font_directory / "NOTICE.md").read_bytes(), original)

    def test_release_rejects_new_stage_pins_even_if_original_font_notice_is_restored(self):
        from tools import build_release as release
        for name in ("tools/build_release.py", "tools/package_native.py", "installer/CantoneseIME.iss",
                     "installer/dependencies.lock.json", "pyproject.toml", "native/tsf/icons/cantonese.ico",
                     "installer/lifecycle.py", "installer/Set-UserInputSource.ps1",
                     "installer/GETTING_STARTED.txt", "installer/RELEASE_NOTICES.txt"):
            self.write(name, b"synthetic release support input")
        compiler = self.write("compiler/ISCC.exe", pe_bytes())
        self.write("compiler/license.txt", b"synthetic compiler license")
        runtime = self.root / "private-python"
        self.write("private-python/python.exe", pe_bytes())
        output = self.root / "synthetic-release"
        notice = self.root / self.font_directory / "NOTICE.md"
        original_notice, original_lock = notice.read_bytes(), self.font_lock_path.read_bytes()

        def stage_fixture(payload, *args, **options):
            # Emulate a self-consistent notice/lock edit between the release's
            # original validation and stage's independent validation, then
            # restore the source. Staged bytes remain changed and self-consistent.
            notice.write_bytes(original_notice + b" temporary new attribution")
            lock = json.loads(original_lock)
            row = next(row for row in lock["files"] if row["name"] == "NOTICE.md")
            row.update(sha256=package.digest(notice), size_bytes=notice.stat().st_size)
            self.json(self.font_directory + "/sources.lock.json", lock)
            payload.mkdir()
            records = []
            try:
                for relative, source in package._candidate_font_inputs().items():
                    target = payload / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    records.append({"path": relative, "bytes": target.stat().st_size, "sha256": package.digest(target)})
            finally:
                notice.write_bytes(original_notice)
                self.font_lock_path.write_bytes(original_lock)
            return {"files": records}

        with patch.object(release, "ROOT", self.root), \
                patch.object(release, "_candidate_font_inputs", side_effect=package._candidate_font_inputs), \
                patch.object(release, "project_version", return_value="1.0.6"), \
                patch.object(release, "tools", return_value=(runtime, compiler, {"python": {}, "inno_setup": {"version": "7.1.0"}})), \
                patch.object(release, "stage", side_effect=stage_fixture), \
                patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "7.1.0", "")) as launch, \
                self.assertRaisesRegex(ValueError, "original release input pins"):
            release.build(output, allow_fetch=False, compile_setup=True)
        self.assertEqual(launch.call_count, 1)  # Compiler version only, no compile.
        self.assertEqual(notice.read_bytes(), original_notice)
        self.assertEqual(self.font_lock_path.read_bytes(), original_lock)
        self.assertNotEqual((output / "package" / self.font_directory / "NOTICE.md").read_bytes(), original_notice)
        self.assertFalse((output / "release-build.json").exists())


if __name__ == "__main__":
    unittest.main()
