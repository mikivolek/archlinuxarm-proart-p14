"""Signing accepts only custom packages recorded by the build step for the current locks."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-packages.py"
SPEC = importlib.util.spec_from_file_location("build_packages", SCRIPT)
build_packages = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_packages)

LOCK_HASHES = {"locks/packages.json": "1" * 64, "locks/hardware.json": "2" * 64}
LOCK = {"repository": "h7607a", "local_repository_path": "build/repo",
        "signing_keys": {"local": {"fingerprint": "A" * 40}, "archlinuxarm": {"fingerprint": "B" * 40}}}
PACKAGES = {"demo": {"name": "demo", "version": "1.0-1", "architecture": "aarch64", "kind": "custom"}}
PAYLOAD = b"package bytes"
FILENAME = "demo-1.0-1-aarch64.pkg.tar.zst"


def entry(**changes):
    return {"name": "demo", "version": "1.0-1", "file": FILENAME, "bytes": len(PAYLOAD),
            "sha256": hashlib.sha256(PAYLOAD).hexdigest(), **changes}


class AssembleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.built = self.tmp / "output"
        self.built.mkdir()
        (self.built / FILENAME).write_bytes(PAYLOAD)
        self.repo = self.tmp / "build/repo"
        self.signed = []

    def write_record(self, **changes):
        value = {"schema": "h7607a-native-package-build-v1", "lock_sha256": LOCK_HASHES, "packages": [entry()]}
        value.update(changes)
        (self.built / "build-record.json").write_text(json.dumps(value))

    def assemble(self):
        def run(argv, **kwargs):
            if argv[0] == "repo-add":
                for suffix in ("db", "files"):
                    (self.repo / f"h7607a.{suffix}.tar.gz").write_bytes(suffix.encode())
            return ""

        def sign(path, home, fingerprint):
            self.signed.append(Path(path).name)
            Path(str(path) + ".sig").write_bytes(b"signature")

        identity = {"pkgname": "demo", "pkgver": "1.0-1", "arch": "aarch64"}
        with mock.patch.multiple(build_packages, package_plan=lambda root: (LOCK, {}, PACKAGES, LOCK_HASHES),
                                 verify_key=lambda root, row: None, run=run, sign=sign,
                                 package_metadata=lambda path: identity), \
                contextlib.redirect_stdout(io.StringIO()):
            build_packages.assemble(self.tmp, self.built, self.repo, self.tmp / "signing-home")

    def assert_refused_before_publishing(self):
        with self.assertRaises(build_packages.PackageError):
            self.assemble()
        self.assertEqual(self.signed, [])
        self.assertFalse(self.repo.exists())

    def test_signs_the_recorded_package(self):
        self.write_record()
        self.assemble()
        self.assertIn(FILENAME, self.signed)
        manifest = json.loads((self.repo / "repository.json").read_text())
        self.assertEqual(manifest["packages"][0]["file"]["sha256"], hashlib.sha256(PAYLOAD).hexdigest())

    def test_refuses_a_package_changed_after_the_build(self):
        self.write_record()
        (self.built / FILENAME).write_bytes(PAYLOAD[::-1])
        with self.assertRaises(build_packages.PackageError):
            self.assemble()
        self.assertEqual(self.signed, [])
        self.assertFalse((self.repo / (FILENAME + ".sig")).exists())

    def test_refuses_a_package_of_another_size(self):
        self.write_record()
        (self.built / FILENAME).write_bytes(PAYLOAD + b"\n")
        self.assert_refused_before_publishing()

    def test_refuses_without_a_build_record(self):
        self.assert_refused_before_publishing()

    def test_refuses_a_record_for_other_locks(self):
        self.write_record(lock_sha256={**LOCK_HASHES, "locks/hardware.json": "3" * 64})
        self.assert_refused_before_publishing()

    def test_refuses_a_record_without_the_package(self):
        self.write_record(packages=[])
        self.assert_refused_before_publishing()

    def test_refuses_a_record_for_another_version(self):
        self.write_record(packages=[entry(version="1.0-0")])
        self.assert_refused_before_publishing()

    def test_refuses_a_record_naming_another_directory(self):
        (self.tmp / FILENAME).write_bytes(PAYLOAD)
        self.write_record(packages=[entry(file="../" + FILENAME)])
        self.assert_refused_before_publishing()


if __name__ == "__main__":
    unittest.main()
