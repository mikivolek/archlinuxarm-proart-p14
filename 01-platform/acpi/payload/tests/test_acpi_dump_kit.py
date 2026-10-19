"""Offline checks of the UEFI Shell ACPI dump kit and its stick staging."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stage_acpi_dump", ROOT / "scripts/stage-acpi-dump.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)


def lock(root, data):
    (root / "locks").mkdir(parents=True)
    row = {"path": kit.SHELL, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (root / "locks/hardware.json").write_text(json.dumps({"artifacts": [row]}))
    shell = root / kit.SHELL
    shell.parent.mkdir(parents=True)
    shell.write_bytes(data)


class StageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stick = Path(self.tmp.name)
        mounted = mock.patch.object(kit.os.path, "ismount", lambda path: Path(path) == self.stick)
        mounted.start()
        self.addCleanup(mounted.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_stages_shell_and_script(self):
        written = kit.stage(self.stick, b"shell", b"script")
        self.assertEqual(len(written), 2)
        self.assertEqual((self.stick / "EFI/BOOT/BOOTAA64.EFI").read_bytes(), b"shell")
        self.assertEqual((self.stick / "EFI/BOOT/startup.nsh").read_bytes(), b"script")
        self.assertEqual(kit.stage(self.stick, b"shell", b"script"), [])

    def test_keeps_a_different_loader(self):
        loader = self.stick / "EFI/BOOT/BOOTAA64.EFI"
        loader.parent.mkdir(parents=True)
        loader.write_bytes(b"systemd-boot")
        with self.assertRaises(ValueError):
            kit.stage(self.stick, b"shell", b"script")
        self.assertEqual(loader.read_bytes(), b"systemd-boot")
        self.assertFalse((self.stick / "EFI/BOOT/startup.nsh").exists())

    def test_requires_a_mount_point(self):
        with self.assertRaises(ValueError):
            kit.stage(self.stick / "sub", b"shell", b"script")


class ShellPinTest(unittest.TestCase):
    def test_checkout_layout_and_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            collection = Path(tmp)
            media = collection / "07-build/media/payload"
            lock(media, b"pinned shell")
            component = collection / "01-platform/acpi/payload"
            component.mkdir(parents=True)
            self.assertEqual(kit.media_root(component), media)
            self.assertEqual(kit.pinned_shell(media), b"pinned shell")
            (media / kit.SHELL).write_bytes(b"other shell!")
            with self.assertRaises(ValueError):
                kit.pinned_shell(media)

    def test_assembled_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock(root, b"pinned shell")
            self.assertEqual(kit.media_root(root), root)


class DumpScriptTest(unittest.TestCase):
    def setUp(self):
        self.lines = (ROOT / kit.SCRIPT).read_bytes().decode("ascii").split("\n")

    def test_dumps_after_removing_old_dumps(self):
        commands = [line.strip() for line in self.lines]
        self.assertEqual(commands[0], "@echo -off")
        removal = commands.index("rm -q *.bin")
        self.assertLess(removal, commands.index("acpiview -s DSDT -d"))
        self.assertLess(removal, commands.index("acpiview -s SSDT -d"))

    def test_echo_arguments_are_not_flags(self):
        # The Shell's echo rejects any argument that starts with a dash.
        for line in self.lines:
            words = line.split()
            if words[:1] == ["echo"]:
                self.assertFalse(any(word.startswith("-") for word in words[1:]), line)


if __name__ == "__main__":
    unittest.main()
