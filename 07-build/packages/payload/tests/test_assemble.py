"""Offline checks of assemble.sh pin verification and fetch planning on a synthetic collection."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve()
COLLECTION = HERE.parents[4]
SCRIPT = COLLECTION / "assemble.sh"
LOCKS = ["locks/live-base.json", "locks/live-tools.json", "locks/packages.json", "locks/release-inputs.json"]

# Stand-ins for the assembled tree's scripts; each records its call in the log.
FETCH = """import json, os, shutil, sys
with open(os.environ["TEST_LOG"], "a") as log:
    log.write(json.dumps(["fetch", os.getcwd(), sys.argv[1:]]) + "\\n")
for lock in sys.argv[1:]:
    for row in json.load(open(lock))["artifacts"]:
        if not os.path.exists(row["path"]):
            os.makedirs(os.path.dirname(row["path"]), exist_ok=True)
            shutil.copyfile(os.path.join(os.environ["TEST_REMOTE"], row["sha256"]), row["path"])
"""
LOGGER = """import json, os, sys
with open(os.environ["TEST_LOG"], "a") as log:
    log.write(json.dumps([sys.argv[1], sorted(os.listdir("build/cache"))]) + "\\n")
"""
ROOTFS = b"rootfs bytes\n" * 32


def pin(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@unittest.skipUnless(HERE.parents[1].name == "payload" and SCRIPT.is_file() and shutil.which("jq"),
                     "needs the collection checkout and jq")
class AssembleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "collection"
        self.remote = self.tmp / "remote"
        self.log = self.tmp / "log"
        self.remote.mkdir()
        (self.remote / pin(ROOTFS)["sha256"]).write_bytes(ROOTFS)
        self.files = {"scripts/fetch.py": FETCH.encode(), "scripts/release.py": LOGGER.encode(),
                      "scripts/build-live.py": LOGGER.encode(), "docs/a.txt": b"document\n"}
        base = {"path": "build/cache/root.tar.gz", **pin(ROOTFS), "urls": ["https://example.invalid/root.tar.gz"]}
        self.locks = {name: {"artifacts": [base] if name == LOCKS[0] else []} for name in LOCKS}
        self.absent = {"build/cache/root.tar.gz": {}}

    def write(self, edit=None, manifest_sha256=None):
        component = self.root / "c"
        rows = []
        files = dict(self.files, **{name: json.dumps(lock).encode() for name, lock in self.locks.items()})
        for destination, data in files.items():
            (component / "payload" / destination).parent.mkdir(parents=True, exist_ok=True)
            (component / "payload" / destination).write_bytes(data)
            rows.append({"source": "payload/" + destination, "destination": destination, **pin(data),
                         "mode": 493 if destination.startswith("scripts/") else 420})
        for destination, extra in self.absent.items():
            data = ROOTFS if destination == "build/cache/root.tar.gz" else b"absent\n"
            rows.append({"source": "payload/" + destination, "destination": destination, **pin(data),
                         "mode": 420, **extra})
        if edit:
            edit(component)
        text = json.dumps({"files": sorted(rows, key=lambda r: r["destination"]),
                           "links": [{"destination": "build/cache/root.link", "target": "root.tar.gz"}]})
        (component / "component.yaml").write_text(text)
        digest = manifest_sha256 or hashlib.sha256(text.encode()).hexdigest()
        (self.root / "manifest.yaml").write_text(json.dumps({"components": [
            {"manifest": "c/component.yaml", "sha256": digest}]}))
        shutil.copy2(SCRIPT, self.root / "assemble.sh")

    def run_script(self, *args):
        env = dict(os.environ, TEST_LOG=str(self.log), TEST_REMOTE=str(self.remote))
        result = subprocess.run([str(self.root / "assemble.sh"), *args], capture_output=True, text=True, env=env)
        events = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, events

    def test_present_inputs_are_copied_and_sealed_without_fetching(self):
        self.absent = {}
        self.files["build/cache/root.tar.gz"] = ROOTFS
        self.write()
        out = self.tmp / "out"
        result, events = self.run_script("--fetch", "assemble", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((out / "docs/a.txt").read_bytes(), b"document\n")
        self.assertEqual((out / "scripts/fetch.py").stat().st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(out / "build/cache/root.link"), "root.tar.gz")
        self.assertEqual(events, [["seal", ["root.link", "root.tar.gz"]]])

    def test_build_runs_after_seal(self):
        self.absent = {}
        self.files["build/cache/root.tar.gz"] = ROOTFS
        self.write()
        result, events = self.run_script("build", str(self.tmp / "out"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([event[0] for event in events], ["seal", "build"])

    def test_mismatched_file_is_named(self):
        self.write(lambda c: (c / "payload/docs/a.txt").write_bytes(b"tampered\n"))
        out = self.tmp / "out"
        result, events = self.run_script("assemble", str(out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("c/payload/docs/a.txt", result.stderr)
        self.assertFalse(out.exists())

    def test_lfs_pointer_is_refused(self):
        pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\n"
        self.write(lambda c: (c / "payload/docs/a.txt").write_bytes(pointer))
        result, _ = self.run_script("assemble", str(self.tmp / "out"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("c/payload/docs/a.txt: Git LFS pointer", result.stderr)

    def test_component_hash_is_checked(self):
        self.write(manifest_sha256="0" * 64)
        result, _ = self.run_script("assemble", str(self.tmp / "out"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("c/component.yaml", result.stderr)

    def test_absent_input_prints_fetch_command_without_network(self):
        self.write()
        out = self.tmp / "out"
        result, events = self.run_script("assemble", str(out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("  c/payload/build/cache/\n    root.tar.gz\n", result.stderr)
        self.assertIn(f"assemble.sh --fetch assemble {out}", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual(events, [])

    def test_fetch_downloads_before_seal(self):
        self.write()
        out = self.tmp / "out"
        result, events = self.run_script("--fetch", "assemble", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((out / "build/cache/root.tar.gz").read_bytes(), ROOTFS)
        self.assertEqual(events, [["fetch", str(out.resolve()), LOCKS], ["seal", ["root.link", "root.tar.gz"]]])

    def test_lock_only_inputs_are_fetched_only_on_request(self):
        self.absent = {}
        self.files["build/cache/root.tar.gz"] = ROOTFS
        source = b"kernel source\n" * 8
        (self.remote / pin(source)["sha256"]).write_bytes(source)
        self.locks[LOCKS[3]]["artifacts"].append({"path": ".artifacts/sources/src.tar.gz", **pin(source),
                                                 "urls": ["https://example.invalid/src.tar.gz"]})
        self.write()
        result, events = self.run_script("assemble", str(self.tmp / "offline"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([event[0] for event in events], ["seal"])
        self.assertFalse((self.tmp / "offline/.artifacts").exists())
        self.log.unlink()
        out = self.tmp / "out"
        result, events = self.run_script("--fetch", "assemble", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fetching 1 file, 0 MB after copying.", result.stderr)
        self.assertEqual([event[0] for event in events], ["fetch", "seal"])
        self.assertEqual((out / ".artifacts/sources/src.tar.gz").read_bytes(), source)

    def test_failed_fetch_leaves_resume_command_and_no_seal(self):
        (self.remote / pin(ROOTFS)["sha256"]).unlink()
        self.write()
        out = self.tmp / "out"
        result, events = self.run_script("--fetch", "build", str(out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("python3 -B scripts/release.py seal && python3 -B scripts/build-live.py build", result.stderr)
        self.assertEqual([event[0] for event in events], ["fetch"])

    def test_lock_pin_must_match_component_pin(self):
        self.locks[LOCKS[0]]["artifacts"][0]["sha256"] = "0" * 64
        self.write()
        result, events = self.run_script("--fetch", "assemble", str(self.tmp / "out"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("lock pin differs", result.stderr)
        self.assertEqual(events, [])

    def test_provided_file_allows_assemble_but_not_build(self):
        self.absent["acpi/table.aml"] = {"provided_by": "generated on the target machine"}
        self.write()
        result, events = self.run_script("--fetch", "build", str(self.tmp / "refused"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("table.aml: generated on the target machine", result.stderr)
        self.assertFalse((self.tmp / "refused").exists())
        self.assertEqual(events, [])
        result, events = self.run_script("--fetch", "assemble", str(self.tmp / "out"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("before building", result.stderr)
        self.assertEqual([event[0] for event in events], ["fetch", "seal"])

    def test_unsourced_file_stops_before_download(self):
        self.absent["assets/unknown.bin"] = {}
        self.write()
        out = self.tmp / "out"
        result, events = self.run_script("--fetch", "assemble", str(out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown.bin: no URL", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual(events, [])

    def test_existing_output_is_refused(self):
        self.write()
        (self.tmp / "out").mkdir()
        result, _ = self.run_script("--fetch", "assemble", str(self.tmp / "out"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)


if __name__ == "__main__":
    unittest.main()
