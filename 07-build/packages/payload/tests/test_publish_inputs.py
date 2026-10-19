"""Offline checks of publish-inputs.sh staging and upload planning on a synthetic collection."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve()
SCRIPT = HERE.parents[4] / "publish-inputs.sh"
PREFIX = "https://github.com/owner/repo/releases/download/inputs-9/"
DATA = {"build/repo/x.pkg.tar.zst": b"signed package\n", ".artifacts/sources/y.tar.gz": b"source\n" * 9,
        "assets/k.tar.zst": b"kernel runtime\n" * 5, "assets/p.bin": b"not published\n"}
SHA = {path: hashlib.sha256(data).hexdigest() for path, data in DATA.items()}

# Stand-in for gh that keeps the release as a JSON file and never leaves the machine.
FAKE_GH = """#!{python}
import hashlib, json, os, sys
state = os.environ["FAKE_GH_STATE"]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
command = sys.argv[1:3]
if command == ["release", "view"]:
    if not os.path.exists(state):
        sys.exit(1)
    print(open(state).read())
elif command == ["release", "create"]:
    json.dump({{"assets": []}}, open(state, "w"))
elif command == ["release", "upload"]:
    release = json.load(open(state))
    for path in sys.argv[6:]:
        data = open(path, "rb").read()
        release["assets"].append({{"name": os.path.basename(path), "size": len(data), "state": "uploaded",
                                   "digest": "sha256:" + hashlib.sha256(data).hexdigest()}})
    json.dump(release, open(state, "w"))
else:
    sys.exit(2)
"""


def row(path, urls):
    return {"id": path, "path": path, "bytes": len(DATA[path]), "sha256": SHA[path], "urls": urls}


@unittest.skipUnless(HERE.parents[1].name == "payload" and SCRIPT.is_file(), "needs the collection checkout")
class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root, self.stage = self.tmp / "collection", self.tmp / "stage"
        self.search, self.lfs = self.tmp / "sources", self.tmp / "lfs"
        self.search.mkdir()
        (self.search / "y.tar.gz").write_bytes(DATA[".artifacts/sources/y.tar.gz"])
        sha = SHA["assets/k.tar.zst"]
        (self.lfs / sha[:2] / sha[2:4]).mkdir(parents=True)
        (self.lfs / sha[:2] / sha[2:4] / sha).write_bytes(DATA["assets/k.tar.zst"])
        self.release_lock = {"schema": "test", "release": {"repository": "owner/repo", "tag": "inputs-9"},
                             "artifacts": [row("build/repo/x.pkg.tar.zst", [PREFIX + SHA["build/repo/x.pkg.tar.zst"]]),
                                           row(".artifacts/sources/y.tar.gz", ["https://example.invalid/y.tar.gz",
                                               PREFIX + SHA[".artifacts/sources/y.tar.gz"]])]}
        self.packages_lock = {"artifacts": [row("assets/k.tar.zst", [PREFIX + SHA["assets/k.tar.zst"]]),
                                            row("assets/p.bin", [])]}

    def write(self):
        component, rows = self.root / "c", []
        files = {"locks/release-inputs.json": json.dumps(self.release_lock).encode(),
                 "locks/packages.json": json.dumps(self.packages_lock).encode(),
                 "build/repo/x.pkg.tar.zst": DATA["build/repo/x.pkg.tar.zst"], "assets/p.bin": DATA["assets/p.bin"]}
        for destination, data in files.items():
            (component / "payload" / destination).parent.mkdir(parents=True, exist_ok=True)
            (component / "payload" / destination).write_bytes(data)
            rows.append({"source": "payload/" + destination, "destination": destination,
                         "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": 420})
        rows.append({"source": "payload/assets/k.tar.zst", "destination": "assets/k.tar.zst",
                     "bytes": len(DATA["assets/k.tar.zst"]), "sha256": SHA["assets/k.tar.zst"], "mode": 420})
        text = json.dumps({"files": rows})
        (component / "component.yaml").write_text(text)
        (self.root / "manifest.yaml").write_text(json.dumps({"components": [
            {"manifest": "c/component.yaml", "sha256": hashlib.sha256(text.encode()).hexdigest()}]}))
        shutil.copy2(SCRIPT, self.root / "publish-inputs.sh")

    def run_script(self, *args, env=None):
        return subprocess.run([str(self.root / "publish-inputs.sh"), *args], capture_output=True, text=True,
                              env=env)

    def staged(self):
        return sorted(p.name for p in self.stage.iterdir()) if self.stage.exists() else []

    def test_stages_checkout_and_search_files_under_their_sha256(self):
        self.write()
        result = self.run_script(str(self.stage), str(self.search), str(self.lfs))
        self.assertEqual(result.returncode, 0, result.stderr)
        published = sorted(SHA[p] for p in DATA if p != "assets/p.bin")
        self.assertEqual(self.staged(), published)
        for path, sha in SHA.items():
            if path != "assets/p.bin":
                self.assertEqual((self.stage / sha).read_bytes(), DATA[path])
        checkout = self.root / "c/payload/build/repo/x.pkg.tar.zst"
        self.assertEqual((self.stage / SHA["build/repo/x.pkg.tar.zst"]).stat().st_ino, checkout.stat().st_ino)
        create, upload = result.stdout.splitlines()
        self.assertTrue(create.startswith("gh release create inputs-9 --repo owner/repo --draft "))
        self.assertEqual(upload.split(), ["gh", "release", "upload", "inputs-9", "--repo", "owner/repo",
                                          *[str(self.stage / sha) for sha in published]])
        self.assertIn("Staged 3 of 3", result.stderr)
        again = self.run_script(str(self.stage))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, result.stdout)

    def test_files_missing_locally_are_listed_and_left_out(self):
        self.write()
        result = self.run_script(str(self.stage))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Not found locally:\n  .artifacts/sources/\n    y.tar.gz\n  assets/\n    k.tar.zst\n",
                      result.stderr)
        self.assertEqual(self.staged(), [SHA["build/repo/x.pkg.tar.zst"]])
        self.assertEqual(len(result.stdout.splitlines()), 2)

    def test_mismatched_file_is_refused(self):
        self.write()
        (self.search / "y.tar.gz").write_bytes(b"tampered\n" * 7)
        result = self.run_script(str(self.stage), str(self.search))
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(self.search / "y.tar.gz"), result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(SHA[".artifacts/sources/y.tar.gz"], self.staged())

    def test_stale_stage_entry_is_refused(self):
        self.write()
        self.stage.mkdir()
        (self.stage / SHA["build/repo/x.pkg.tar.zst"]).write_bytes(b"stale\n")
        result = self.run_script(str(self.stage))
        self.assertEqual(result.returncode, 1)
        self.assertIn("differs from its name", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_release_url_must_be_last_and_content_addressed(self):
        self.release_lock["artifacts"][1]["urls"].reverse()
        self.write()
        result = self.run_script(str(self.stage))
        self.assertEqual(result.returncode, 1)
        self.assertIn(".artifacts/sources/y.tar.gz: the release URL must come last", result.stderr)
        self.release_lock["artifacts"][1]["urls"] = [PREFIX + "y.tar.gz"]
        self.write()
        result = self.run_script(str(self.stage))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.staged(), [])

    def fake_gh(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        # Only these commands are reachable, so the real gh cannot run.
        for name in ("bash", "dirname"):
            (bin_dir / name).symlink_to(shutil.which(name))
        (bin_dir / "python3").symlink_to(sys.executable)
        (bin_dir / "gh").write_text(FAKE_GH.format(python=sys.executable))
        (bin_dir / "gh").chmod(0o755)
        log, state = self.tmp / "gh.log", self.tmp / "release.json"
        env = {"PATH": str(bin_dir), "FAKE_GH_LOG": str(log), "FAKE_GH_STATE": str(state)}
        return env, log, state

    def test_upload_creates_the_draft_once_and_skips_uploaded_assets(self):
        self.write()
        env, log, _ = self.fake_gh()
        result = self.run_script("--upload", str(self.stage), str(self.search), str(self.lfs), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([call[:2] for call in calls], [["release", "view"], ["release", "create"],
                                                         ["release", "upload"]])
        self.assertIn("--draft", calls[1])
        self.assertEqual(calls[2][5:], [str(self.stage / sha) for sha in sorted(SHA.values())
                                        if sha != SHA["assets/p.bin"]])
        log.unlink()
        result = self.run_script("--upload", str(self.stage), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([json.loads(line)[:2] for line in log.read_text().splitlines()], [["release", "view"]])
        self.assertIn("Uploaded 0 assets", result.stderr)

    def test_upload_stops_at_an_incomplete_asset(self):
        self.write()
        env, log, state = self.fake_gh()
        sha = SHA["build/repo/x.pkg.tar.zst"]
        state.write_text(json.dumps({"assets": [{"name": sha, "size": 3, "state": "starter", "digest": None}]}))
        result = self.run_script("--upload", str(self.stage), env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"Release asset {sha} is incomplete", result.stderr)
        self.assertEqual([json.loads(line)[:2] for line in log.read_text().splitlines()], [["release", "view"]])


if __name__ == "__main__":
    unittest.main()
