"""Resolve every custom package input from a tree assembled out of the component payloads."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from h7607a.packages import package_plan

COLLECTION = next((p for p in Path(__file__).resolve().parents if (p / "manifest.yaml").is_file()), None)


def component_rows(collection):
    for entry in json.loads((collection / "manifest.yaml").read_text())["components"]:
        component = collection / entry["manifest"]
        for row in json.loads(component.read_text())["files"]:
            yield component.parent / row["source"], row


def pkgbuild_sources(pkgbuild):
    """Map each source filename makepkg would use to its sha256sums entry."""
    script = 'source "$1" && printf "%s\\0" "${#source[@]}" "${source[@]}" "${sha256sums[@]}"'
    fields = subprocess.run(["bash", "-c", script, "bash", str(pkgbuild)], capture_output=True, text=True,
                            check=True).stdout.split("\0")[:-1]
    count = int(fields[0])
    names = [s.split("::", 1)[0] if "::" in s else s.rsplit("/", 1)[-1] for s in fields[1:1 + count]]
    return dict(zip(names, fields[1 + count:], strict=True))


@unittest.skipUnless(COLLECTION, "needs the component collection rather than an assembled tree")
class PackagePlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        cls.pins = {}
        for source, row in component_rows(COLLECTION):
            cls.pins[row["destination"]] = (row["bytes"], row["sha256"])
            if row["destination"].startswith(("locks/", "packages/")):
                target = cls.root / row["destination"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        _, cls.artifacts, packages, _ = package_plan(cls.root)
        cls.custom = [p for p in packages.values() if p["kind"] == "custom"]

    def inputs(self, package):
        spec = json.loads((self.root / package["recipe"] / "inputs.json").read_text())
        self.assertEqual(spec["package"], package["name"])
        return spec["inputs"]

    def test_inputs_resolve_to_payload_pins(self):
        for package in self.custom:
            for entry in self.inputs(package):
                with self.subTest(package=package["name"], input=entry["filename"]):
                    row = self.artifacts.get(entry["artifact_id"])
                    self.assertIsNotNone(row)
                    self.assertEqual(self.pins.get(row["path"]), (row["bytes"], row["sha256"]))

    def test_recipe_directories_hold_only_pinned_files(self):
        for package in self.custom:
            recipe = self.root / package["recipe"]
            with self.subTest(package=package["name"]):
                self.assertEqual({p.relative_to(self.root).as_posix() for p in recipe.iterdir()},
                                 set(package["recipe_files"]))

    def test_pkgbuild_checksums_match_the_pins(self):
        for package in self.custom:
            expected = {e["filename"]: self.artifacts[e["artifact_id"]]["sha256"] for e in self.inputs(package)}
            expected.update({Path(path).name: pin["sha256"] for path, pin in package["recipe_files"].items()
                             if Path(path).name not in ("PKGBUILD", "inputs.json")})
            with self.subTest(package=package["name"]):
                self.assertEqual(pkgbuild_sources(self.root / package["recipe"] / "PKGBUILD"), expected)


if __name__ == "__main__":
    unittest.main()
