"""Size- and SHA256-locked artifact retrieval; no downloaded code is executed."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ATTEMPTS = 3
BACKOFF_SECONDS = 1.0


class ArtifactError(RuntimeError):
    pass


class _Transient(ArtifactError):
    pass


def _require(value: bool, message: str) -> None:
    if not value:
        raise ArtifactError(message)


def _require_relative(name, label: str) -> None:
    _require(isinstance(name, str) and bool(name), f"{label} is missing")
    _require(not PurePosixPath(name).is_absolute() and all(p not in {"", ".", ".."} for p in name.split("/")),
             f"{label} must be relative without traversal")
    _require("\\" not in name and "\x00" not in name, f"Invalid {label.lower()}")


def validate_row(row: dict) -> None:
    _require(isinstance(row, dict), "Artifact entry must be an object")
    _require_relative(row.get("path", ""), "Artifact path")
    _require(type(row.get("bytes")) is int and row["bytes"] >= 0, "Artifact byte size must be exact")
    _require(isinstance(row.get("sha256"), str) and bool(re.fullmatch("[0-9a-f]{64}", row["sha256"])),
             "Artifact requires an exact SHA256")
    urls = row.get("urls", [])
    _require(isinstance(urls, list), "Artifact URLs must be a list")
    for url in urls:
        parsed = urllib.parse.urlsplit(url)
        _require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password,
                 "Artifact download requires credential-free HTTPS")
    if "extract" in row:
        spec = row["extract"]
        _require(isinstance(spec, dict) and type(spec.get("offset")) is int and spec["offset"] >= 0,
                 "Artifact extraction requires an exact container offset")
        _require_relative(spec.get("member", ""), "Archive member")
        validate_row(spec.get("from"))


def artifact_path(root: Path, row: dict) -> Path:
    validate_row(row)
    root = Path(root).resolve(strict=True)
    target = root / row["path"]
    current = root
    for part in PurePosixPath(row["path"]).parts:
        current = current / part
        _require(not current.is_symlink(), f"Linked artifact path refused: {row['path']}")
    return target


def verify_file(path: Path, row: dict) -> Path:
    """Check a regular file without following its final component or buffering it."""
    validate_row(row)
    try:
        # A FIFO must fail the regular-file check without waiting for a writer.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ArtifactError(f"Cannot open artifact {path.name}: {exc.strerror}") from exc
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_size == row["bytes"],
                 f"Artifact size/type differs: {path.name}")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(4 * 1024 * 1024, remaining))
            _require(bool(chunk), f"Artifact read ended early: {path.name}")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                 (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                 f"Artifact changed while reading: {path.name}")
        _require(digest.hexdigest() == row["sha256"], f"Artifact SHA256 differs: {path.name}")
        try:
            named = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise ArtifactError(f"Artifact path changed while reading: {path.name}") from exc
        _require(stat.S_ISREG(named.st_mode) and
                 (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns, named.st_ctime_ns) ==
                 (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                 f"Artifact path changed while reading: {path.name}")
    finally:
        os.close(fd)
    return Path(path)


def verify(root: Path, row: dict) -> Path:
    return verify_file(artifact_path(root, row), row)


def _publish(temporary: Path, target: Path, row: dict) -> None:
    """Publish a verified same-filesystem temporary without replacing a winner."""
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        # A concurrent fetch of this exact object is harmless. A different
        # destination remains intact and is rejected by its own verification.
        verify_file(target, row)


def _extract(root: Path, row: dict, cache_dirs, *, offline: bool, timeout: int) -> Path:
    """Derive a locked file from an archive embedded at a fixed offset of another locked artifact."""
    spec = row["extract"]
    container = fetch(root, spec["from"], cache_dirs, offline=offline, timeout=timeout)
    target = artifact_path(root, row)
    # libarchive's 7-Zip reader seeks from file offset 0, so carve the embedded archive first.
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=target.parent) as work:
        archive = Path(work) / "archive"
        with container.open("rb") as source, archive.open("xb") as output:
            source.seek(spec["offset"])
            shutil.copyfileobj(source, output)
        result = subprocess.run(["bsdtar", "-xOf", archive, spec["member"]], capture_output=True, timeout=timeout)
    _require(result.returncode == 0,
             f"Cannot extract {spec['member']}: {result.stderr.decode(errors='replace')[-1000:]}")
    _require(len(result.stdout) == row["bytes"], f"Extracted member size differs: {spec['member']}")
    fd, temporary = tempfile.mkstemp(prefix=".fetch-", dir=target.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(result.stdout)
            output.flush()
            os.fsync(output.fileno())
        verify_file(temporary, row)
        _publish(temporary, target, row)
    finally:
        temporary.unlink(missing_ok=True)
    return verify(root, row)


def load_lock(path: Path) -> dict:
    value = json.loads(Path(path).read_text())
    _require(isinstance(value, dict) and isinstance(value.get("artifacts"), list),
             f"Lock lacks an artifacts array: {path}")
    seen = set()
    for row in value["artifacts"]:
        validate_row(row)
        _require(row["path"] not in seen, f"Duplicate artifact path: {row['path']}")
        seen.add(row["path"])
    return value


def _download(url: str, output, row: dict, timeout: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "h7607a-arch/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        _require(urllib.parse.urlsplit(response.url).scheme == "https", "Insecure artifact redirect")
        remaining = row["bytes"]
        while True:
            chunk = response.read(min(1024 * 1024, remaining + 1))
            if not chunk:
                break
            _require(len(chunk) <= remaining, "Downloaded artifact exceeds locked size")
            output.write(chunk)
            remaining -= len(chunk)
        if remaining:
            raise _Transient("Downloaded artifact is truncated")
    output.flush()
    os.fsync(output.fileno())


def _retryable(exc: BaseException) -> bool:
    """Retry connection faults, truncation and server errors; other HTTP errors are final."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code in (408, 429)
    return isinstance(exc, (_Transient, OSError, http.client.HTTPException))


def fetch(root: Path, row: dict, cache_dirs=(), *, offline: bool = False, timeout: int = 45) -> Path:
    """Reuse a verified file, otherwise atomically fetch one exact locked object.

    Cache candidates are explicit directories, searched by filename or SHA256.
    Same-filesystem reuse hardlinks immutable artifacts to avoid duplicate GBs.
    Existing corrupt destinations are errors, never silently overwritten.
    URLs are tried in order, each up to ATTEMPTS times with doubling backoff.
    """
    target = artifact_path(root, row)
    if target.exists():
        return verify(root, row)
    target.parent.mkdir(parents=True, exist_ok=True)
    artifact_path(root, row)  # Reject any linked parent introduced by a caller.
    for directory in cache_dirs:
        for name in (target.name, row["sha256"]):
            source = Path(directory) / name
            if not source.exists() or source.is_symlink():
                continue
            try:
                verify_file(source, row)
            except ArtifactError:
                continue
            fd, temporary = tempfile.mkstemp(prefix=".fetch-", dir=target.parent)
            os.close(fd)
            temporary = Path(temporary)
            try:
                temporary.unlink()
                try:
                    os.link(source, temporary, follow_symlinks=False)
                except OSError:
                    _require(shutil.disk_usage(target.parent).free >= row["bytes"] + 64 * 1024**2,
                             "Insufficient space for artifact copy")
                    shutil.copyfile(source, temporary, follow_symlinks=False)
                verify_file(temporary, row)
                _publish(temporary, target, row)
                return verify(root, row)
            finally:
                temporary.unlink(missing_ok=True)
    if "extract" in row:
        return _extract(root, row, cache_dirs, offline=offline, timeout=timeout)
    _require(not offline, f"Artifact unavailable offline: {row['path']}")
    _require(bool(row.get("urls")), f"No download URL for local artifact: {row['path']}")
    _require(shutil.disk_usage(target.parent).free >= row["bytes"] + 64 * 1024**2,
             "Insufficient space for artifact download")
    failures = []
    for url in row["urls"]:
        for attempt in range(1, ATTEMPTS + 1):
            fd, temporary = tempfile.mkstemp(prefix=".fetch-", dir=target.parent)
            temporary = Path(temporary)
            try:
                with os.fdopen(fd, "wb") as output:
                    _download(url, output, row, timeout)
                verify_file(temporary, row)
                _publish(temporary, target, row)
                return verify(root, row)
            except (OSError, ArtifactError, http.client.HTTPException) as exc:
                if attempt == ATTEMPTS or not _retryable(exc):
                    failures.append(f"{url}: {exc}" + (f" after {attempt} attempts" if attempt > 1 else ""))
                    break
            finally:
                temporary.unlink(missing_ok=True)
            time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise ArtifactError(f"All locked sources failed for {row['path']}: {'; '.join(failures)}")
