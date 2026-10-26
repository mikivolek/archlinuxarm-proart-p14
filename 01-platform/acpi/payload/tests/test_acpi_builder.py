"""Offline checks of the owner-dump ACPI builder with synthetic tables."""
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provenance/hardware"))
import acpi_builder as builder

FAKE_IASL = """#!{python}
import sys
from pathlib import Path
if sys.argv[1:] == ["-v"]:
    print("ASL+ Optimizing Compiler/Disassembler version {version}")
elif sys.argv[1:] == ["-d", "SSDT0001.bin"]:
    table = Path("SSDT0001.bin").read_bytes()
    Path("SSDT0001.dsl").write_text("/* " + table[10:24].decode() + " */\\n"
                                    "DefinitionBlock (0x00000001)\\nLocal0 = Buffer (0x29){{}}\\n")
else:
    sys.exit(2)
"""


def table(signature, oem=b"NVDA\0\0ANGUS   ", body=b"\xa3"):
    data = bytearray(signature + struct.pack("<I", 36 + len(body)) + b"\x02\x00" + oem +
                     struct.pack("<I", 1) + b"INTL" + struct.pack("<I", 0x20230628) + body)
    data[9] = -sum(data) % 256
    return bytes(data)


ASUS = table(b"SSDT", builder.ASUS_SSDT, b"\xa3\xa3")
OTHER = table(b"SSDT", b"NVDA\0\0CPUSSDT ")


class DumpTest(unittest.TestCase):
    def dump(self, files):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, data in files.items():
            Path(directory.name, name).write_bytes(data)
        return directory.name

    def pick(self, files):
        dump = self.dump(files)
        found = builder.dumped_tables(dump)
        return (builder.dumped(found, b"DSDT", "DSDT", dump),
                builder.dumped(found, b"SSDT", "ASUS SSDT", dump))

    def test_acpiview_names(self):
        dsdt = table(b"DSDT")
        files = {"DSDT0000.bin": dsdt, "SSDT0000.bin": OTHER, "SSDT0001.bin": ASUS, "notes.txt": b"x"}
        self.assertEqual(self.pick(files), (("DSDT0000.bin", dsdt), ("SSDT0001.bin", ASUS)))

    def test_sysfs_names(self):
        dsdt = table(b"DSDT")
        files = {"DSDT": dsdt, "FACP": table(b"FACP"), "SSDT1": OTHER, "SSDT2": ASUS, "SSDT3": OTHER + bytes(4)}
        self.assertEqual(self.pick(files), (("DSDT", dsdt), ("SSDT2", ASUS)))

    def test_identical_copies(self):
        dsdt = table(b"DSDT")
        files = {"DSDT": dsdt, "DSDT0000.bin": dsdt, "SSDT1": ASUS, "SSDT0001.bin": ASUS}
        self.assertEqual(self.pick(files), (("DSDT", dsdt), ("SSDT0001.bin", ASUS)))

    def test_rejects_ambiguous_or_missing_tables(self):
        for files in ({"DSDT": table(b"DSDT"), "DSDT0000.bin": table(b"DSDT", body=b"\xa4"), "SSDT1": ASUS},
                      {"DSDT": table(b"DSDT"), "SSDT1": OTHER},
                      {"SSDT1": ASUS}):
            with self.subTest(files=sorted(files)), self.assertRaises(ValueError):
                self.pick(files)

    def test_rejects_stale_tail_and_bad_checksum(self):
        corrupt = bytearray(table(b"DSDT"))
        corrupt[-1] ^= 1
        for dsdt in (table(b"DSDT") + bytes(4), bytes(corrupt)):
            with self.subTest(dsdt=dsdt), self.assertRaises(ValueError):
                builder.dumped_tables(self.dump({"DSDT0000.bin": dsdt}))


class FirmwareTest(unittest.TestCase):
    def test_other_firmware_names_the_supported_version(self):
        with self.assertRaises(ValueError) as caught:
            builder.firmware("DSDT", table(b"DSDT"), builder.DSDT_INPUT, builder.DSDT_OUTPUT, "DSDT")
        self.assertIn(builder.FIRMWARE, str(caught.exception))

    def test_override_dump_is_recognized(self):
        dsdt = table(b"DSDT")
        with self.assertRaises(ValueError) as caught:
            builder.firmware("DSDT", dsdt, builder.DSDT_INPUT, builder.sha(dsdt), "DSDT")
        self.assertIn("h7607a-acpi.cpio", str(caught.exception))

    def test_pinned_firmware_passes(self):
        dsdt = table(b"DSDT")
        builder.firmware("DSDT", dsdt, builder.sha(dsdt), builder.DSDT_OUTPUT, "DSDT")

    def test_dsdt_selects_its_firmware_profile(self):
        dsdt = table(b"DSDT")
        profile = dict(builder.PROFILES["H7407BA.303"], dsdt_input=builder.sha(dsdt))
        with mock.patch.dict(builder.PROFILES, {"H7407BA.303": profile}):
            self.assertEqual(builder.select_profile("DSDT", dsdt), ("H7407BA.303", profile))

    def test_unknown_dsdt_names_every_supported_firmware(self):
        with self.assertRaises(ValueError) as caught:
            builder.select_profile("DSDT", table(b"DSDT"))
        for version in builder.PROFILES:
            self.assertIn(version, str(caught.exception))

    def test_override_dump_of_any_profile_is_recognized(self):
        dsdt = table(b"DSDT")
        profile = dict(builder.PROFILES["H7407BA.303"], dsdt_output=builder.sha(dsdt))
        with mock.patch.dict(builder.PROFILES, {"H7407BA.303": profile}), \
                self.assertRaises(ValueError) as caught:
            builder.select_profile("DSDT", dsdt)
        self.assertIn("h7607a-acpi.cpio", str(caught.exception))


class ArchiveTest(unittest.TestCase):
    def build(self, version, ssdt=None):
        with mock.patch.object(builder, "exact", lambda data, digest, description: data):
            return builder.archive(table(b"DSDT"), ssdt, builder.PROFILES[version])

    def test_p14_archive_holds_only_the_dsdt(self):
        result = self.build("H7407BA.303")
        self.assertIn(b"kernel/firmware/acpi/DSDT.aml", result)
        self.assertNotIn(b"SSDT.aml", result)
        self.assertEqual(len(result) % 512, 0)

    def test_p16_archive_holds_both_tables(self):
        result = self.build(builder.FIRMWARE, ASUS)
        self.assertIn(b"kernel/firmware/acpi/DSDT.aml", result)
        self.assertIn(b"kernel/firmware/acpi/SSDT.aml", result)


class EditTest(unittest.TestCase):
    def test_literal_line_commands(self):
        script = "# comment\n2,3s/Local0/RSPB/g\n2s/x/y/\n4s/a/b/\n"
        self.assertEqual(builder.edit("x\nLocal0 x Local0 x\nLocal0\na a\n", script),
                         "x\nRSPB y RSPB x\nRSPB\nb a\n")

    def test_rejects_commands_sed_would_read_differently(self):
        for script in ("1s/a.b/c/\n", "1s/a/&/\n", "1d\n", "1,2s/a/b/x\n"):
            with self.subTest(script=script), self.assertRaises(ValueError):
                builder.commands(script)

    def test_rejects_commands_that_do_not_apply(self):
        for script in ("1s/zzz/b/\n", "5s/a/b/\n", "2,1s/a/b/\n"):
            with self.subTest(script=script), self.assertRaises(ValueError):
                builder.edit("a\nb\n", script)

    def test_shipped_edits_follow_the_method_receipt(self):
        edits = builder.commands((ROOT / "provenance/hardware/power-source.sed").read_text())
        ranges = [command for command in edits if command[1] != command[2]]
        methods = json.loads((ROOT / "provenance/hardware/power-method-changes.json").read_text())
        self.assertEqual(len(ranges), len(methods))
        for (_, first, last, old, new, every), method in zip(ranges, methods):
            self.assertEqual((old, new, every), (method["local_replaced"], method["new_name"], True))
            self.assertLess(method["source_line"], first)
            wraps = [command for command in edits if command[1] == command[2] == first]
            self.assertEqual(len(wraps), 2)


class IaslTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)

    def fake(self, version):
        iasl = self.work / "iasl"
        iasl.write_text(FAKE_IASL.format(python=sys.executable, version=version))
        iasl.chmod(0o755)
        return str(iasl)

    def test_derives_asl_from_the_ssdt(self):
        iasl = self.fake("20260408")
        builder.check_iasl(iasl, self.work)
        script = "2s/0x00000001)/0x00000002)/\n3s/Local0 = Buffer/Name (RSPB, Buffer/\n3s/{}/{})/\n"
        expected = b"/* _ASUS_ASUSSSDT */\nDefinitionBlock (0x00000002)\nName (RSPB, Buffer (0x29){})\n"
        with mock.patch.object(builder, "ASL_INPUT", builder.sha(expected)):
            self.assertEqual(builder.derive_asl(ASUS, script, iasl, self.work), expected)
        with self.assertRaises(ValueError):
            builder.derive_asl(ASUS, script, iasl, self.work)

    def test_requires_iasl_20260408(self):
        for iasl in (self.fake("20260930"), str(self.work / "missing")):
            with self.subTest(iasl=iasl), self.assertRaises(ValueError):
                builder.check_iasl(iasl, self.work)

    def test_relative_iasl_path_survives_the_work_directory(self):
        iasl = self.fake("20260408")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.work)
        with tempfile.TemporaryDirectory() as work:
            self.assertEqual(builder.check_iasl("./iasl", Path(work)), iasl)


class PublishTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name).resolve()

    def test_writes_once_and_keeps_identical_files(self):
        files = {builder.ARCHIVE_PATH: b"archive", builder.ASL_PATH: b"asl"}
        self.assertEqual(builder.publish(self.repo, files), sorted(files))
        self.assertEqual((self.repo / builder.ARCHIVE_PATH).read_bytes(), b"archive")
        self.assertEqual(builder.publish(self.repo, files), [])

    def test_keeps_a_different_file(self):
        path = self.repo / builder.ARCHIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(b"older")
        with self.assertRaises(ValueError):
            builder.publish(self.repo, {builder.ASL_PATH: b"asl", builder.ARCHIVE_PATH: b"archive"})
        self.assertEqual(path.read_bytes(), b"older")
        self.assertFalse((self.repo / builder.ASL_PATH).exists())

    def test_does_not_write_through_symlinks(self):
        path = self.repo / builder.ARCHIVE_PATH
        path.parent.mkdir(parents=True)
        path.symlink_to(self.repo / "elsewhere")
        with self.assertRaises(ValueError):
            builder.publish(self.repo, {builder.ARCHIVE_PATH: b"archive"})
        self.assertFalse((self.repo / "elsewhere").exists())


@unittest.skipUnless(os.environ.get("H7407A_ACPI_DUMP"), "set H7407A_ACPI_DUMP to an H7407BA.303 table dump")
class P14OwnerDumpTest(unittest.TestCase):
    def test_reproduces_the_pinned_archive(self):
        profile = builder.PROFILES["H7407BA.303"]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            result = builder.build(os.environ["H7407A_ACPI_DUMP"], repo=repo, iasl="missing-iasl")
            expected = {builder.DSDT_PATH: profile["dsdt_input"], builder.SSDT_PATH: profile["ssdt_input"],
                        builder.ARCHIVE_PATH: profile["archive_output"]}
            self.assertEqual({path: builder.sha((repo / path).read_bytes()) for path in expected}, expected)
            self.assertEqual((result["firmware"], result["compiler"], result["written"]),
                             ("H7407BA.303", None, sorted(expected)))


@unittest.skipUnless(os.environ.get("H7607A_ACPI_DUMP") and os.environ.get("H7607A_IASL"),
                     "set H7607A_ACPI_DUMP to an H7607BA.300 table dump and H7607A_IASL to ACPICA iasl 20260408")
class OwnerDumpTest(unittest.TestCase):
    def test_reproduces_the_pinned_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "provenance/hardware").mkdir(parents=True)
            for name in ("power-method-changes.json", "power-source.sed", "power-splice.json"):
                shutil.copy(ROOT / "provenance/hardware" / name, repo / "provenance/hardware")
            result = builder.build(os.environ["H7607A_ACPI_DUMP"], repo=repo, iasl=os.environ["H7607A_IASL"])
            expected = {builder.DSDT_PATH: builder.DSDT_INPUT, builder.SSDT_PATH: builder.SSDT_INPUT,
                        builder.ASL_PATH: builder.ASL_INPUT, builder.ARCHIVE_PATH: builder.ARCHIVE_OUTPUT}
            self.assertEqual({path: builder.sha((repo / path).read_bytes()) for path in expected}, expected)
            self.assertEqual(result["written"], sorted(expected))


if __name__ == "__main__":
    unittest.main()
