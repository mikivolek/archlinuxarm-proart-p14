"""Read the preserved, pinned USB wheel carrier without writing to the device.

Only the 31 exact PyTorch wheels are copied. The raw header is authenticated as
opaque bytes, never interpreted as paths or extraction instructions. Production
block-device reads additionally bind the opened whole disk to the live root USB.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import time

from . import development


LAYOUT_SHA256 = "f95fdf333bc3a18d95bea2fff220c069bc96fb72b6d1c53e75165bfd474013b2"
CHUNK = 4 * 1024**2
BLKGETSIZE64 = 0x80081272
BLKSSZGET = 0x1268
MAX_SECONDS = 1800


class CarrierError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CarrierError(message)


def _canonical(path):
    path = Path(os.path.abspath(path))
    require(path.resolve() == path and not path.is_symlink(), "Linked carrier path refused: " + str(path))
    return path


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _load_layout(repo_root):
    root = _canonical(repo_root)
    path = _canonical(root / "locks/usb-layout.json")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 1024**2, "Invalid USB layout lock file")
        data = os.read(fd, info.st_size + 1)
        require(len(data) == info.st_size and _sha(data) == LAYOUT_SHA256, "USB layout lock differs from the pinned carrier layout")
    finally:
        os.close(fd)
    value = json.loads(data)
    require(value.get("schema") == "h7607a-existing-analysis-usb-layout-v1", "Unexpected USB layout schema")
    return value


def _safe_object_path(value):
    require(isinstance(value, str) and 0 < len(value) <= 512, "Invalid carrier object path")
    parts = value.split("/")
    require(str(PurePosixPath(value)) == value and not value.startswith("/")
            and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,200}", part) and part not in (".", "..") for part in parts),
            "Unsafe carrier object path")
    return value


def _plan(layout, lock):
    whole = layout.get("whole_bytes")
    require(type(whole) is int and whole > 2 * 1024**2 and whole % 512 == 0, "Invalid whole-device size")
    header_start, header_size = layout.get("carrier_header_offset"), layout.get("carrier_header_bytes")
    require(type(header_start) is int and type(header_size) is int and header_start % 4096 == 0
            and header_size == 1024**2 and 0 < header_start < header_start + header_size < whole - 16896,
            "Invalid carrier header extent")
    sha_re = re.compile(r"[0-9a-f]{64}")
    require(isinstance(layout.get("carrier_header_sha256"), str) and sha_re.fullmatch(layout["carrier_header_sha256"]),
            "Missing carrier header pin")
    regions = {"mbr": (0, 512), "primary": (512, 512), "primary_array": (1024, 16384),
               "backup": (whole - 512, 512), "backup_array": (whole - 16896, 16384)}
    hashes = layout.get("gpt_sha256", {})
    require(set(hashes) == set(regions) and all(isinstance(value, str) and sha_re.fullmatch(value) for value in hashes.values()),
            "Exact five GPT region pins are required")
    partitions = layout.get("partitions")
    require(isinstance(partitions, list) and len(partitions) == 3, "Expected three preserved partitions")
    reserved = [(0, 17408), (whole - 16896, whole)]
    for row in partitions:
        lo, size = row.get("offset"), row.get("bytes")
        require(type(lo) is int and type(size) is int and size > 0 and lo > 0 and lo + size <= whole - 16896,
                "Partition extent exceeds device")
        reserved.append((lo, lo + size))
    objects = layout.get("carrier_objects")
    require(isinstance(objects, list) and len(objects) == 39, "Exact 39-object carrier required")
    intervals = [(header_start, header_start + header_size)]
    paths, names = set(), set()
    by_path = {}
    for row in objects:
        require(isinstance(row, dict), "Invalid carrier row")
        path = _safe_object_path(row.get("path"))
        start, size = row.get("offset"), row.get("bytes")
        require(type(start) is int and type(size) is int and size > 0 and start % 4096 == 0
                and start >= header_start + header_size and start + size <= whole - 16896,
                "Carrier object extent exceeds bounds")
        require(isinstance(row.get("sha256"), str) and sha_re.fullmatch(row["sha256"]), "Carrier object hash is invalid")
        require(path.casefold() not in paths, "Duplicate carrier object path")
        paths.add(path.casefold())
        intervals.append((start, start + size))
        by_path[path] = row
    ordered = sorted(intervals)
    require(all(previous[1] <= following[0] for previous, following in zip(ordered, ordered[1:])),
            "Carrier header/object extents overlap")
    require(all(not (a < d and c < b) for a, b in intervals for c, d in reserved),
            "Carrier overlaps a partition or GPT")
    selected = []
    require(isinstance(lock.get("artifacts"), list) and len(lock["artifacts"]) == 31, "Exact 31-wheel lock required")
    for item in lock["artifacts"]:
        relative = item.get("path", "")
        require(relative.startswith("assets/development/torch/"), "Wheel lock path is outside Torch payload")
        carrier_path = relative.removeprefix("assets/development/")
        row = by_path.get(carrier_path)
        require(row is not None and row["sha256"] == item.get("sha256") and row["bytes"] == item.get("bytes"),
                "Carrier wheel does not match the exact development lock: " + carrier_path)
        filename = item.get("filename")
        require(isinstance(filename, str) and filename == PurePosixPath(carrier_path).name
                and filename.endswith(".whl") and filename.casefold() not in names,
                "Invalid or duplicate output wheel filename")
        names.add(filename.casefold())
        selected.append(dict(row, filename=filename))
    require({row["path"] for row in objects if row["path"].startswith("torch/")} == {row["path"] for row in selected},
            "Carrier contains unexpected or missing Torch objects")
    return regions, selected


def _read_exact(fd, offset, size, whole, deadline):
    require(type(offset) is int and type(size) is int and offset >= 0 and 0 < size <= CHUNK
            and offset + size <= whole, "Raw read exceeds bound")
    result = bytearray()
    while len(result) < size:
        require(time.monotonic() < deadline, "Carrier read deadline exceeded")
        block = os.pread(fd, size - len(result), offset + len(result))
        require(bool(block), "Short carrier read")
        result.extend(block)
    return bytes(result)


def _verify_regions(fd, layout, regions, deadline):
    whole = layout["whole_bytes"]
    for name, (offset, size) in regions.items():
        require(_sha(_read_exact(fd, offset, size, whole, deadline)) == layout["gpt_sha256"][name],
                "Preserved GPT region differs: " + name)
    header = _read_exact(fd, layout["carrier_header_offset"], layout["carrier_header_bytes"], whole, deadline)
    require(_sha(header) == layout["carrier_header_sha256"], "Preserved carrier header differs")


def _small_text(path):
    data = Path(path).read_bytes()
    require(len(data) <= 4096, "Oversized sysfs identity attribute")
    return data.decode("ascii").strip()


def _query(argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=15, check=False, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    require(result.returncode == 0 and len(result.stdout) <= 1024**2, "Live USB identity query failed")
    return json.loads(result.stdout)


def _block_identity(fd, device, layout, expected):
    require(isinstance(expected, dict), "Installer must supply the proven live USB parent identity")
    info = os.fstat(fd)
    major, minor = os.major(info.st_rdev), os.minor(info.st_rdev)
    require(expected.get("major") == major and expected.get("minor") == minor
            and expected.get("size") == layout["whole_bytes"] and expected.get("root_uuid") == layout["root_uuid"],
            "Opened USB differs from installer parent identity")
    size = struct.unpack("Q", fcntl.ioctl(fd, BLKGETSIZE64, bytes(8)))[0]
    sector = struct.unpack("I", fcntl.ioctl(fd, BLKSSZGET, bytes(4)))[0]
    require(size == layout["whole_bytes"] and sector == 512, "USB capacity or logical sector size differs")
    physical = Path(f"/sys/dev/block/{major}:{minor}").resolve(strict=True)
    require(physical.is_relative_to("/sys/devices") and not (physical / "partition").exists()
            and any(re.fullmatch(r"usb[0-9]+", part) for part in physical.parts), "Carrier source is not a whole USB device")
    require(_small_text(physical / "removable") == "1" and int(_small_text(physical / "size")) * 512 == size,
            "USB physical capacity/removable identity differs")
    model = " ".join(_small_text(physical / "device/model").split())
    require(model in ("Ultra", "Ultra USB 3.0"), "USB model differs")
    rows = _query(["/usr/bin/findmnt", "--json", "--mountpoint", "/", "--output", "SOURCE,UUID,MAJ:MIN"])["filesystems"]
    require(len(rows) == 1 and rows[0].get("uuid") == layout["root_uuid"], "Live root UUID differs from recorded USB")
    root_number = rows[0].get("maj:min", "")
    require(re.fullmatch(r"[0-9]+:[0-9]+", root_number), "Live root block identity is absent")
    root_sys = Path("/sys/dev/block", root_number).resolve(strict=True)
    require(root_sys.parent == physical and _small_text(root_sys / "partition") == "2",
            "Opened carrier is not the live root's USB parent")
    require(device.stat().st_rdev == info.st_rdev, "USB node changed after open")
    return {"kind": "block", "major": major, "minor": minor, "size": size,
            "root_uuid": layout["root_uuid"], "sysfs": str(physical), "model": model}


def _source(fd, device, layout, expected):
    info = os.fstat(fd)
    require((fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE) == os.O_RDONLY, "Carrier source must be opened read-only")
    if stat.S_ISBLK(info.st_mode):
        return _block_identity(fd, device, layout, expected)
    require(stat.S_ISREG(info.st_mode) and expected is None and info.st_size == layout["whole_bytes"],
            "Source must be the proven block device or an exact-size regular fixture")
    return {"kind": "regular-fixture", "size": info.st_size, "device": info.st_dev, "inode": info.st_ino}


def _write_all(fd, block):
    view = memoryview(block)
    while view:
        count = os.write(fd, view)
        require(count > 0, "Short wheel output write")
        view = view[count:]


def _destination_policy(fstype, number, backing_node, source_node):
    """Only tmpfs or a distinct direct block filesystem; no opaque stacks."""
    if fstype == "tmpfs":
        require(number.startswith("0:"), "Unexpected tmpfs backing identity")
        return {"kind": "tmpfs", "number": number}
    require(fstype in ("ext2", "ext3", "ext4", "xfs") and backing_node is not None,
            "Wheel output requires tmpfs or a directly backed ext filesystem/xfs")
    node, source = Path(backing_node), Path(source_node)
    require(node.is_relative_to("/sys/devices") and "virtual" not in node.parts,
            "Unqualified stacked/loop output storage; use tmpfs or the direct internal target")
    require(node != source and not node.is_relative_to(source),
            "Wheel output is on the protected source USB")
    return {"kind": "independent-block-filesystem", "number": number, "sysfs": str(node)}


def _output_storage(destination, output_info, identity):
    if identity["kind"] == "regular-fixture":
        return {"kind": "fixture-output"}
    rows = _query(["/usr/bin/findmnt", "--json", "--target", str(destination),
                   "--output", "FSTYPE,MAJ:MIN"])["filesystems"]
    number = f"{os.major(output_info.st_dev)}:{os.minor(output_info.st_dev)}"
    require(len(rows) == 1 and rows[0].get("maj:min") == number, "Wheel output mount identity differs")
    link = Path("/sys/dev/block", number)
    node = link.resolve(strict=True) if link.exists() else None
    return _destination_policy(rows[0].get("fstype"), number, node, identity["sysfs"])


def _verify_written(directory_fd, name, row, inode, deadline):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == inode and info.st_size == row["bytes"],
                "Output wheel identity or size changed")
        digest = hashlib.sha256()
        remaining = row["bytes"]
        while remaining:
            require(time.monotonic() < deadline, "Wheel readback deadline exceeded")
            block = os.read(fd, min(CHUNK, remaining))
            require(bool(block), "Short output wheel readback")
            digest.update(block)
            remaining -= len(block)
        require(not os.read(fd, 1) and digest.hexdigest() == row["sha256"], "Output wheel SHA256 readback differs")
    finally:
        os.close(fd)


def extract_torch(device: Path, destination: Path, repo_root: Path, expected_parent_identity=None) -> dict:
    """Extract the exact wheels to an existing private, empty, caller-owned dir.

    For a block device expected_parent_identity requires integer major/minor/size
    plus root_uuid. Its live-root parent relationship is independently rechecked.
    Regular files are supported only as explicit fixture images (identity=None)
    and are never reported as a production USB proof. Completed output is 0755,
    wheels 0444, so an ordinary target user can consume the sealed wheelhouse.
    """
    device, destination = _canonical(device), _canonical(destination)
    require(device != destination and destination != Path("/"), "Invalid extraction destination")
    layout = _load_layout(repo_root)
    lock = development.read_lock(Path(repo_root) / "locks/development.json")
    regions, selected = _plan(layout, lock)
    deadline = time.monotonic() + MAX_SECONDS
    source_fd = os.open(device, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    output_fd = None
    created: dict[str, tuple[int, int]] = {}
    try:
        before = os.fstat(source_fd)
        identity = _source(source_fd, device, layout, expected_parent_identity)
        _verify_regions(source_fd, layout, regions, deadline)
        output_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        output_info = os.fstat(output_fd)
        require(output_info.st_uid == os.geteuid() and not output_info.st_mode & 0o077,
                "Wheel destination must be caller-owned and private (0700)")
        require(not os.listdir(output_fd), "Wheel destination must be empty")
        output_storage = _output_storage(destination, output_info, identity)
        total = sum(row["bytes"] for row in selected)
        require(shutil.disk_usage(destination).free >= total + 64 * 1024**2, "Insufficient space for carrier wheels")
        # Exclusive creation also arbitrates concurrent attempts before any wheel.
        owner_fd = os.open(".extracting", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o600, dir_fd=output_fd)
        owner_info = os.fstat(owner_fd)
        created[".extracting"] = (owner_info.st_dev, owner_info.st_ino)
        os.close(owner_fd)
        records = []
        copied = 0
        last_notice = 0
        for index, row in enumerate(selected, 1):
            name = row["filename"]
            partial = "." + name + ".partial"
            fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=output_fd)
            entry = os.fstat(fd)
            created[partial] = (entry.st_dev, entry.st_ino)
            digest = hashlib.sha256()
            offset = 0
            try:
                while offset < row["bytes"]:
                    block = _read_exact(source_fd, row["offset"] + offset, min(CHUNK, row["bytes"] - offset),
                                        layout["whole_bytes"], deadline)
                    digest.update(block)
                    _write_all(fd, block)
                    offset += len(block)
                    copied += len(block)
                    now = time.monotonic()
                    if now - last_notice >= 3:
                        print(f"Copying pinned Torch wheels: {index}/31, {copied / 1e9:.2f}/{total / 1e9:.2f} GB", flush=True)
                        last_notice = now
                require(digest.hexdigest() == row["sha256"], "Carrier wheel SHA256 differs: " + name)
                require(os.fstat(fd).st_size == row["bytes"], "Written wheel size differs")
                os.fsync(fd)
                os.fchmod(fd, 0o444)
            finally:
                os.close(fd)
            _verify_written(output_fd, partial, row, created[partial], deadline)
            os.rename(partial, name, src_dir_fd=output_fd, dst_dir_fd=output_fd)
            created[name] = created.pop(partial)
            records.append({"filename": name, "path": row["path"], "bytes": row["bytes"], "sha256": digest.hexdigest()})
        _verify_regions(source_fd, layout, regions, deadline)
        require(_source(source_fd, device, layout, expected_parent_identity) == identity, "Source identity changed during extraction")
        after = os.fstat(source_fd)
        if identity["kind"] == "regular-fixture":
            require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), "Fixture source changed during extraction")
        require(not destination.is_symlink() and _canonical(destination) == destination
                and (destination.stat().st_dev, destination.stat().st_ino) == (output_info.st_dev, output_info.st_ino),
                "Wheel destination pathname changed during extraction")
        require(_output_storage(destination, output_info, identity) == output_storage,
                "Wheel output backing filesystem changed during extraction")
        os.unlink(".extracting", dir_fd=output_fd)
        created.pop(".extracting")
        os.fchmod(output_fd, 0o755)
        os.fsync(output_fd)
        print(f"Torch wheel carrier verified: 31 files, {copied / 1e9:.2f} GB. USB was read-only.", flush=True)
        return {"schema": "h7607a-carrier-extraction-v1", "wheelhouse": str(destination), "objects": records,
                "bytes": copied, "layout_sha256": LAYOUT_SHA256, "development_lock_sha256": development.LOCK_SHA256,
                "source": str(device), "source_identity": identity, "source_modified": False,
                "output_storage": output_storage,
                "production_usb_verified": identity["kind"] == "block", "gpu_qualified": False}
    except BaseException:
        # Delete only files this invocation created, using its retained directory
        # descriptor and inode identities; unrelated replacements are untouched.
        if output_fd is not None:
            for name, inode in created.items():
                try:
                    info = os.stat(name, dir_fd=output_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == inode:
                        os.unlink(name, dir_fd=output_fd)
                except FileNotFoundError:
                    pass
        raise
    finally:
        if output_fd is not None:
            os.close(output_fd)
        os.close(source_fd)
