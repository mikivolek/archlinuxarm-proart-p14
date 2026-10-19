#!/usr/bin/env python3
"""Stage the ACPI table dump kit on a mounted FAT32 USB stick.

Copies the pinned EDK2 Shell to EFI/BOOT/BOOTAA64.EFI and the dump script to
EFI/BOOT/startup.nsh. Nothing is partitioned or formatted and no other file on
the stick is touched. Boot the P16 from the stick with Secure Boot disabled:
the Shell writes DSDT0000.bin, SSDT0000.bin, SSDT0001.bin, ... to the stick's
H7607A-ACPI folder, which provenance/hardware/power-archive-builder.py --dump
reads.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = "assets/hardware/uefi/Shell.efi"
SCRIPT = "assets/hardware/uefi/acpi-dump.nsh"
TARGETS = ("EFI/BOOT/BOOTAA64.EFI", "EFI/BOOT/startup.nsh")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def media_root(root):
    # An assembled tree holds Shell.efi itself; a checkout keeps it in the media component.
    candidates = [root]
    if len(root.parents) > 2:
        candidates.append(root.parents[2] / "07-build/media/payload")
    for candidate in candidates:
        if (candidate / "locks/hardware.json").is_file():
            return candidate
    raise ValueError("Cannot find locks/hardware.json, which pins Shell.efi")


def pinned_shell(root):
    rows = [row for row in json.loads((root / "locks/hardware.json").read_text())["artifacts"]
            if row["path"] == SHELL]
    require(len(rows) == 1, "locks/hardware.json must pin exactly one Shell.efi")
    data = (root / SHELL).read_bytes()
    require(len(data) == rows[0]["bytes"] and hashlib.sha256(data).hexdigest() == rows[0]["sha256"],
            f"{root / SHELL} differs from its pin in locks/hardware.json")
    return data


def stage(target, shell, script):
    target = Path(target)
    require(os.path.ismount(target), f"{target} is not the mount point of a stick")
    pending = {}
    for name, data in zip(TARGETS, (shell, script)):
        path = target / name
        if path.exists() or path.is_symlink():
            require(path.is_file() and path.read_bytes() == data,
                    f"Refusing to replace {path}; use a stick without {name}")
        else:
            pending[path] = data
    for path, data in pending.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("xb")
        try:
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return [str(path) for path in pending]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="mount point of the FAT32 USB stick")
    args = parser.parse_args()
    try:
        written = stage(args.target, pinned_shell(media_root(ROOT)), (ROOT / SCRIPT).read_bytes())
    except (ValueError, OSError) as error:
        parser.exit(1, f"Staging failed: {error}\n")
    for path in written:
        print("wrote", path)
    print("Unmount the stick and boot the P16 from it; the tables land in H7607A-ACPI.")


if __name__ == "__main__":
    main()
