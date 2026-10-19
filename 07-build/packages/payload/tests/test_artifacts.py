"""Offline check of locked extraction from an archive embedded in another artifact."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from h7607a.artifacts import ArtifactError, fetch


def pin(path, data):
    return {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class ExtractTest(unittest.TestCase):
    def test_embedded_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = b"tuning\n" * 100
            member = tmp / "src/XU_Ext/fw/a.bin"
            member.parent.mkdir(parents=True)
            member.write_bytes(payload)
            archive = tmp / "a.7z"
            subprocess.run(["bsdtar", "--format", "7zip", "-cf", archive, "-C", tmp / "src", "XU_Ext"], check=True)
            prefix = b"MZ" + bytes(510)
            container = prefix + archive.read_bytes()
            cache = tmp / "cache"
            cache.mkdir()
            (cache / "driver.exe").write_bytes(container)
            root = tmp / "root"
            root.mkdir()
            row = {**pin("assets/a.bin", payload),
                   "extract": {"from": pin(".artifacts/driver.exe", container),
                               "offset": len(prefix), "member": "XU_Ext/fw/a.bin"}}
            self.assertEqual(fetch(root, row, [cache], offline=True).read_bytes(), payload)
            (root / "assets/a.bin").unlink()
            with self.assertRaises(ArtifactError):
                fetch(root, {**row, "sha256": "0" * 64}, [cache], offline=True)


if __name__ == "__main__":
    unittest.main()
