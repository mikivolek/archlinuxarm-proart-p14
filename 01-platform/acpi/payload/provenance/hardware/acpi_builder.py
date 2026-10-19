#!/usr/bin/env python3
"""Build the H7607BA.300 ACPI overrides from this P16's own firmware tables.

No firmware table, nor anything derived from one, is distributed. Dump the
DSDT and SSDTs of your own P16 into a directory and pass it as --dump:

  UEFI: scripts/stage-acpi-dump.py stages the EDK2 Shell and a dump script on
  a mounted FAT32 stick. Boot the P16 from it with Secure Boot disabled; the
  tables land in the stick's H7607A-ACPI folder.

  Linux booted without h7607a-acpi.cpio, such as another distribution's live
  system on this P16:
    mkdir DIR
    for table in /sys/firmware/acpi/tables/DSDT /sys/firmware/acpi/tables/SSDT*; do
        sudo cat "$table" | tee "DIR/${table##*/}" > /dev/null
    done

The DSDT and the ASUS SSDT (OEM ID _ASUS_, table ID ASUSSSDT) must be those of
firmware H7607BA.300. power-source.dsl is that SSDT disassembled by ACPICA iasl
20260408 and edited by power-source.sed. SSPM changes only its _STA predicate,
and six compiled ASUS methods are spliced into the original SSDT, since
compiling the whole table would rewrite unrelated AML. The two tables, the ASL
and the archive are written to their collection paths once every pinned hash
matches.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[2]
FIRMWARE = "H7607BA.300"
DSDT_INPUT = "0b9ed92671568cf36bade16262d068d1a4f3c85634159de46ae00bf0f727a402"
SSDT_INPUT = "46d9ed16af06093cdb849df69646683317d53d7342dfd38f24eab00795d396f0"
ASL_INPUT = "cff6e8be5665686c95bd83b5e2d73ed0abbacddecc416e57dbd29f91a746472a"
DSDT_OUTPUT = "80e0ad4a051704003d09029cb600a9814e1cd370ce774b9749dc09d2232b5aaf"
SSDT_OUTPUT = "9b28ffc14c19f8b7ea2278de88ff9ec070f5f70401107d81fef067a916aa9695"
ARCHIVE_OUTPUT = "311dc5b456f1b7a9728814cd8ca105370bdca8383facec141f23217f43eac51d"
METHODS = ("RRA9", "RRA8", "WRA9", "WRA8", "RWEC", "GBST")
ASUS_SSDT = b"_ASUS_ASUSSSDT"
DSDT_PATH = "assets/hardware/uefi/n1x-dsdt-baseline.bin"
SSDT_PATH = "assets/hardware/uefi/n1x-asus-ssdt-baseline.bin"
ASL_PATH = "provenance/hardware/power-source.dsl"
ARCHIVE_PATH = "assets/hardware/acpi/h7607a-acpi.cpio"
IASL_SOURCE = ("https://github.com/acpica/acpica/releases/download/20260408/acpica-unix-20260408.tar.gz "
               "(SHA-256 e66ceb26d6d514ce164fe22f5a4f7ca165cc38349d7a97f41a21f19b364647a2)")
EDIT = re.compile(r"(\d+)(?:,(\d+))?s/([^/]+)/([^/]*)/(g?)")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def exact(data, digest, description):
    require(sha(data) == digest, f"{description} SHA-256 mismatch")
    return data


def table(data, signature, oem, revision):
    require(len(data) >= 36 and data[:4] == signature, "Wrong ACPI signature")
    require(struct.unpack_from("<I", data, 4)[0] == len(data), "Wrong ACPI length")
    require(sum(data) % 256 == 0, "Invalid ACPI checksum")
    require(data[10:24] == oem, "Wrong ACPI OEM identity")
    require(struct.unpack_from("<I", data, 24)[0] == revision, "Wrong OEM revision")


def pkg_length(data, offset):
    require(0 <= offset < len(data), "Truncated AML package length")
    first = data[offset]
    following = first >> 6
    require(offset + following < len(data), "Truncated AML package length")
    length = first & (0x0f if following else 0x3f)
    for index in range(following):
        length |= data[offset + 1 + index] << (4 + index * 8)
    return length, following + 1


def encode_length(length, width):
    require(1 <= width <= 4, "Invalid AML length width")
    require(0 <= length < (1 << (6 if width == 1 else 4 + 8 * (width - 1))),
            "New AML package length changes its encoded width")
    if width == 1:
        return bytes([length])
    return bytes([((width - 1) << 6) | (length & 15)] +
                 [(length >> (4 + i * 8)) & 255 for i in range(width - 1)])


def patch_dsdt(original):
    exact(original, DSDT_INPUT, "Firmware DSDT")
    table(original, b"DSDT", b"NVDA\0\0ANGUS   ", 4)
    start = 0x10269
    predicate = b"_OSI\x0dWindows 2022\0"
    require(original[start:start + len(predicate)] == predicate, "SSPM predicate changed")
    modified = bytearray(original)
    modified[start:start + len(predicate)] = b"\0" + b"\xa3" * (len(predicate) - 1)
    struct.pack_into("<I", modified, 24, 5)
    modified[9] = 0
    modified[9] = -sum(modified) % 256
    table(modified, b"DSDT", b"NVDA\0\0ANGUS   ", 5)
    return exact(bytes(modified), DSDT_OUTPUT, "SSPM override")


def compiled_method(compiled, name, digest):
    matches = []
    for offset in range(36, len(compiled) - 6):
        if compiled[offset] != 0x14:
            continue
        length, width = pkg_length(compiled, offset + 1)
        name_start = offset + 1 + width
        if compiled[name_start:name_start + 4] == name.encode("ascii"):
            end = offset + 1 + length
            require(end <= len(compiled), f"Truncated {name} method")
            matches.append(compiled[offset:end])
    require(len(matches) == 1, f"Expected exactly one compiled {name} method")
    return exact(matches[0], digest, f"Compiled {name} method")


def splice_ssdt(original, compiled, specification):
    exact(original, SSDT_INPUT, "Firmware ASUS SSDT")
    table(original, b"SSDT", b"_ASUS_ASUSSSDT", 1)
    require(tuple(row["method"] for row in specification["methods"]) == METHODS,
            "Unexpected ASUS method selection")
    patches = []
    for row in specification["methods"]:
        start, end = row["original_start"], row["original_end"]
        exact(original[start:end], row["original_sha256"], f"Original {row['method']} method")
        replacement = compiled_method(compiled, row["method"], row["replacement_sha256"])
        require(len(replacement) == row["new_bytes"], "Changed method length")
        patches.append((start, end, replacement))
    for row in specification["containers"]:
        for kind, opcode in (("scope", b"\x10"), ("device", b"\x5b\x82")):
            start, end = row[f"{kind}_start"], row[f"{kind}_end"]
            require(original[start:start + len(opcode)] == opcode, "Wrong enclosing AML object")
            offset = start + len(opcode)
            old_length, width = pkg_length(original, offset)
            require(offset + old_length == end, "Wrong enclosing AML extent")
            delta = sum(len(data) - (b - a) for a, b, data in patches if start <= a < b <= end)
            require(delta == row["scope_added_bytes"], "Wrong enclosing AML length adjustment")
            patches.append((offset, offset + width, encode_length(old_length + delta, width)))
    modified = bytearray(original)
    last_start = len(original)
    for start, end, data in sorted(patches, reverse=True):
        require(36 <= start < end <= last_start, "Overlapping AML replacements")
        modified[start:end] = data
        last_start = start
    struct.pack_into("<I", modified, 4, len(modified))
    struct.pack_into("<I", modified, 24, 2)
    modified[9] = 0
    modified[9] = -sum(modified) % 256
    table(modified, b"SSDT", b"_ASUS_ASUSSSDT", 2)
    return exact(bytes(modified), SSDT_OUTPUT, "ASUS power override")


def member(name, data, mode, inode):
    name = name.encode("ascii") + b"\0"
    fields = (inode, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1,
              0, len(data), 0, 0, 0, 0, len(name), 0)
    entry = b"070701" + b"".join(f"{value:08x}".encode() for value in fields) + name
    entry += bytes(-len(entry) % 4)
    entry += data
    return entry + bytes(-len(entry) % 4)


def archive(dsdt, ssdt):
    exact(dsdt, DSDT_OUTPUT, "SSPM override")
    exact(ssdt, SSDT_OUTPUT, "ASUS power override")
    result = b"".join(member(name, b"", stat.S_IFDIR | 0o755, inode)
                      for inode, name in enumerate(("kernel", "kernel/firmware", "kernel/firmware/acpi"), 1))
    result += member("kernel/firmware/acpi/DSDT.aml", dsdt, stat.S_IFREG | 0o644, 4)
    result += member("kernel/firmware/acpi/SSDT.aml", ssdt, stat.S_IFREG | 0o644, 5)
    result += member("TRAILER!!!", b"", 0, 6)
    result += bytes(-len(result) % 512)
    return exact(result, ARCHIVE_OUTPUT, "Production ACPI archive")


def dumped_tables(dump):
    """Map b"DSDT" and b"SSDT" to {table: [file names]} for the DSDT and ASUS SSDT dumps in dump."""
    dump = Path(dump)
    require(dump.is_dir(), f"{dump} is not a directory")
    found = {b"DSDT": {}, b"SSDT": {}}
    for path in sorted(dump.iterdir()):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            head = stream.read(24)
        if head[:4] not in found or head[:4] == b"SSDT" and head[10:24] != ASUS_SSDT:
            continue
        data = path.read_bytes()
        require(len(data) >= 36 and struct.unpack_from("<I", data, 4)[0] == len(data),
                f"{path.name} does not hold exactly one table; delete old dumps and dump again")
        require(sum(data) % 256 == 0, f"{path.name} has an invalid ACPI checksum; dump again")
        found[head[:4]].setdefault(data, []).append(path.name)
    return found


def dumped(found, signature, description, dump):
    tables = found[signature]
    require(tables, f"No {description} dump in {dump}; pass the H7607A-ACPI folder of the dump stick "
            "or a copy of /sys/firmware/acpi/tables")
    require(len(tables) == 1, f"Different {description} dumps in {dump}: " +
            ", ".join(sorted(name for names in tables.values() for name in names)))
    data, names = next(iter(tables.items()))
    return names[0], data


def firmware(name, data, expected, override, description):
    digest = sha(data)
    require(digest != override,
            f"{name} is the {description} from h7607a-acpi.cpio, not the firmware's; dump it with "
            "the UEFI Shell kit or from a Linux booted without that archive")
    require(digest == expected,
            f"{name} is not the {description} of firmware {FIRMWARE} (SHA-256 {digest}); "
            f"these overrides are only for BIOS {FIRMWARE}")


def commands(script):
    """Parse power-source.sed into (script line, first, last, old, new, global) tuples."""
    result = []
    for number, line in enumerate(script.split("\n"), 1):
        if not line or line.startswith("#"):
            continue
        match = EDIT.fullmatch(line)
        # Characters with a meaning in sed patterns or replacements would make sed disagree.
        require(match is not None and not set(match[3] + match[4]) & set(".*[]\\^$&"),
                f"power-source.sed line {number} is not a literal s command")
        result.append((number, int(match[1]), int(match[2] or match[1]), match[3], match[4], bool(match[5])))
    return result


def edit(text, script):
    """Apply power-source.sed to text as sed -f would."""
    lines = text.split("\n")
    count = len(lines) - (lines[-1] == "")
    for number, first, last, old, new, every in commands(script):
        require(1 <= first <= last <= count, f"power-source.sed line {number} addresses lines outside the ASL")
        changed = False
        for index in range(first - 1, last):
            if old in lines[index]:
                lines[index] = lines[index].replace(old, new, -1 if every else 1)
                changed = True
        require(changed, f"power-source.sed line {number} does not apply")
    return "\n".join(lines)


def run_iasl(iasl, arguments, work):
    result = subprocess.run([iasl, *arguments], cwd=work, capture_output=True, text=True, errors="replace")
    output = (result.stdout + result.stderr).strip()
    require(result.returncode == 0, f"{iasl} {' '.join(arguments)} failed:\n{output[-2000:]}")
    return output


def check_iasl(iasl, work):
    """Return the absolute path of iasl, which runs from the work directory, once it is 20260408."""
    located = shutil.which(iasl)
    require(located is not None, f"{iasl} was not found; build ACPICA iasl 20260408 from {IASL_SOURCE}")
    located = os.path.abspath(located)
    require(re.search(r"version 20260408\b", run_iasl(located, ["-v"], work)) is not None,
            f"{iasl} is not ACPICA iasl 20260408; build it from {IASL_SOURCE}")
    return located


def derive_asl(ssdt, script, iasl, work):
    # The disassembly header names its input file, and the pinned ASL came from SSDT0001.bin.
    (work / "SSDT0001.bin").write_bytes(ssdt)
    run_iasl(iasl, ["-d", "SSDT0001.bin"], work)
    asl = edit((work / "SSDT0001.dsl").read_bytes().decode("latin-1"), script).encode("latin-1")
    require(sha(asl) == ASL_INPUT,
            f"power-source.dsl derived with {iasl} has SHA-256 {sha(asl)}, not {ASL_INPUT}; "
            "it needs a 64-bit build of ACPICA iasl 20260408")
    return asl


def publish(repo, files):
    """Write verified results to their collection paths, keeping identical existing files."""
    pending = {}
    for relative, data in files.items():
        path = repo / relative
        require(path.parent.resolve().is_relative_to(repo), f"{path.parent} resolves outside {repo}")
        if path.exists() or path.is_symlink():
            require(path.is_file() and not path.is_symlink() and path.read_bytes() == data,
                    f"Refusing to replace {path}, which differs from the verified result; remove it first")
        else:
            pending[path] = data
    for path, data in pending.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("xb")
        try:
            with stream:
                stream.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return sorted(path.relative_to(repo).as_posix() for path in pending)


def pins(files):
    return {name: {"bytes": len(data), "sha256": sha(data)} for name, data in files.items()}


def build(dump, *, repo=REPO, iasl="iasl", sspm_only=False):
    repo = Path(repo).resolve()
    found = dumped_tables(dump)
    dsdt_name, dsdt = dumped(found, b"DSDT", "DSDT", dump)
    firmware(dsdt_name, dsdt, DSDT_INPUT, DSDT_OUTPUT, "DSDT")
    overrides = {"DSDT.aml": patch_dsdt(dsdt)}
    files = {DSDT_PATH: dsdt}
    names = {"DSDT": dsdt_name}
    compiler = None
    if not sspm_only:
        ssdt_name, ssdt = dumped(found, b"SSDT", "ASUS SSDT (OEM ID _ASUS_, table ID ASUSSSDT)", dump)
        firmware(ssdt_name, ssdt, SSDT_INPUT, SSDT_OUTPUT, "ASUS SSDT")
        names["SSDT"] = ssdt_name
        source = repo / "provenance/hardware"
        methods = json.loads((source / "power-method-changes.json").read_text())
        require(tuple(row["method"] for row in methods) == METHODS, "Unexpected ASL method changes")
        with tempfile.TemporaryDirectory(prefix="h7607a-acpi-") as work:
            work = Path(work)
            iasl = check_iasl(iasl, work)
            asl = derive_asl(ssdt, (source / "power-source.sed").read_text(), iasl, work)
            (work / "power.dsl").write_bytes(asl)
            run_iasl(iasl, ["-p", "power", "power.dsl"], work)
            compiled = (work / "power.aml").read_bytes()
        compiler = "ACPICA iasl 20260408"
        overrides["SSDT.aml"] = splice_ssdt(ssdt, compiled, json.loads((source / "power-splice.json").read_text()))
        files.update({SSDT_PATH: ssdt, ASL_PATH: asl,
                      ARCHIVE_PATH: archive(overrides["DSDT.aml"], overrides["SSDT.aml"])})
    written = publish(repo, files)
    return {"schema": 2, "firmware": FIRMWARE, "compiler": compiler, "dump": names,
            "overrides": pins(overrides), "outputs": pins(files), "written": written}


def cli(*, sspm_only=False):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dump", required=True, type=Path, metavar="DIR",
                        help="Directory holding this P16's DSDT and SSDT dumps")
    if not sspm_only:
        parser.add_argument("--iasl", default="iasl", help="ACPICA iasl 20260408 executable")
    args = parser.parse_args()
    try:
        result = build(args.dump, iasl=getattr(args, "iasl", "iasl"), sspm_only=sspm_only)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"ACPI build failed: {error}\n")
    print(json.dumps(result, indent=2))
