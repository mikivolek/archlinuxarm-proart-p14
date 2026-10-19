"""Configure rootless Podman, subordinate IDs and NVIDIA CDI in the target root."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile


ROOTS = ("podman", "crun", "nvidia-container-toolkit", "shadow", "passt")
UNITS = ("nvidia-cdi-refresh.service", "nvidia-cdi-refresh.path")
CDI_ENV = {
    "NVIDIA_CTK_CDI_GENERATE_LDCONFIG_PATH": "/usr/bin/ldconfig",
    "NVIDIA_CTK_CDI_HOOK_PATH": "/usr/bin/nvidia-cdi-hook",
    "NVIDIA_CTK_CDI_OUTPUT_FILE_PATH": "/var/run/cdi/nvidia.yaml",
}
CONTAINERS_CONF = """# h7607a optional rootless container profile
[engine]
runtime = "crun"
"""
SOURCES = (
    "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html",
    "https://github.com/NVIDIA/nvidia-container-toolkit/blob/v1.20.1/cmd/nvidia-ctk/cdi/generate/generate.go",
    "https://docs.podman.io/en/latest/markdown/podman-run.1.html",
)


def package_list() -> list[str]:
    """Official ALARM roots; the installer must resolve their signed closure."""
    return list(ROOTS)


def _path(root: Path, relative: str) -> Path:
    path = root / relative
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError(f"Target path traverses a symlink: {component}")
    return path


def _text(root: Path, relative: str, optional: bool = False) -> str:
    path = _path(root, relative)
    if optional and not path.exists():
        return ""
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError(f"Missing or oversized target file: {relative}")
    return path.read_text()


def _subordinate(text: str, username: str, real_ids: set[int], user_id: int) -> tuple[str, dict]:
    intervals: list[tuple[str, int, int]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise ValueError("Malformed subordinate ID mapping")
        owner, start, count = parts[0], int(parts[1]), int(parts[2])
        if not owner or start < 1 or count < 1 or start + count > 2**32 - 1:
            raise ValueError("Invalid subordinate ID range")
        intervals.append((owner, start, count))
    own = [row for row in intervals if row[0] in (username, str(user_id))]
    if own:
        if len(own) != 1 or own[0][2] < 65536:
            raise ValueError("Existing user subordinate mapping needs explicit reconciliation")
        selected = own[0]
        candidates = [row for row in intervals if row is not selected]
    else:
        start = 100000
        while any(start < lo + count and lo < start + 65536 for _, lo, count in intervals) or any(
                start <= actual < start + 65536 for actual in real_ids):
            start += 65536
            if start + 65536 > 2**32 - 1:
                raise ValueError("No free subordinate ID range")
        selected = (username, start, 65536)
        candidates = intervals
    _, start, count = selected
    if any(start < lo + n and lo < start + count for _, lo, n in candidates) or any(
            start <= actual < start + count for actual in real_ids):
        raise ValueError("User subordinate IDs overlap another allocation or real account")
    result = text if own else text + ("\n" if text and not text.endswith("\n") else "") + f"{username}:{start}:{count}\n"
    return result, {"start": start, "count": count, "retained": bool(own)}


def _write(root: Path, name: str, data: bytes, mode: int = 0o644) -> None:
    path = _path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".h7607a-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), 0, 0)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure_target(target: Path, username: str) -> dict:
    """Write CDI configuration and subordinate IDs after package and user creation."""
    root = Path(target).absolute()
    if root == Path("/") or root.resolve() != root or not root.is_dir():
        raise ValueError("A canonical existing offline target other than / is required")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
        raise ValueError("A normal target username is required")
    passwd = [line.split(":") for line in _text(root, "etc/passwd").splitlines() if line and not line.startswith("#")]
    groups = [line.split(":") for line in _text(root, "etc/group").splitlines() if line and not line.startswith("#")]
    if any(len(row) != 7 or not row[2].isdigit() or not row[3].isdigit() for row in passwd) or any(
            len(row) != 4 or not row[2].isdigit() for row in groups):
        raise ValueError("Malformed target account database")
    matches = [row for row in passwd if row[0] == username]
    if len(matches) != 1 or not 1000 <= int(matches[0][2]) < 65534:
        raise ValueError("Target normal user must exist with UID between 1000 and 65533")
    uid = int(matches[0][2])
    helper_source = Path(__file__).resolve().parents[2] / "runtime/usr/local/bin/h7607a-container"
    helper = helper_source.read_bytes()
    subuid, uid_range = _subordinate(_text(root, "etc/subuid", True), username, {int(row[2]) for row in passwd}, uid)
    subgid, gid_range = _subordinate(_text(root, "etc/subgid", True), username, {int(row[2]) for row in groups}, uid)
    env = _text(root, "etc/nvidia-container-toolkit/nvidia-cdi-refresh.env", True)
    keys: dict[str, str] = {}
    for line in env.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, separator, value = line.partition("=")
            if not separator or key in keys:
                raise ValueError("Malformed or duplicate NVIDIA CDI refresh environment")
            keys[key] = value
    for key, value in CDI_ENV.items():
        if key in keys and keys[key] != value:
            raise ValueError("Conflicting NVIDIA CDI configuration: " + key)
        if key not in keys:
            env += ("\n" if env and not env.endswith("\n") else "") + key + "=" + value + "\n"
    writes = {
        "etc/containers/containers.conf.d/60-h7607a-rootless.conf": (CONTAINERS_CONF.encode(), 0o644),
        "usr/local/bin/h7607a-container": (helper, 0o755),
    }
    hook_dir = _path(root, "usr/share/h7607a/empty-oci-hooks")
    links: list[tuple[Path, str]] = []
    for unit in UNITS:
        name = "etc/systemd/system/multi-user.target.wants/" + unit
        path = root / name
        _path(root, str(path.parent.relative_to(root)))
        destination = "/usr/lib/systemd/system/" + unit
        if path.is_symlink():
            if os.readlink(path) != destination:
                raise ValueError("Conflicting CDI service enablement: " + name)
        elif path.exists():
            raise ValueError("Unexpected CDI service enablement file: " + name)
        links.append((path, destination))
    writes.update({"etc/subuid": (subuid.encode(), 0o644), "etc/subgid": (subgid.encode(), 0o644),
                   "etc/nvidia-container-toolkit/nvidia-cdi-refresh.env": (env.encode(), 0o644)})
    for name, (data, mode) in writes.items():
        _write(root, name, data, mode)
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_dir.chmod(0o755)
    if os.geteuid() == 0:
        os.chown(hook_dir, 0, 0)
    for path, destination in links:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_symlink():
            path.symlink_to(destination)
    return {
        "profile": "optional-rootless-podman-cdi", "username": username,
        "packages": package_list(), "subuid": uid_range, "subgid": gid_range,
        "configured_files": sorted(writes), "enabled_units": list(UNITS),
        "root_ownership_finalized": os.geteuid() == 0,
        "root_daemon_enabled": False, "target_programs_executed": False,
        "sources": list(SOURCES),
    }
