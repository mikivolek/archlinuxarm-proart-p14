"""Install NVIDIA SBSA compiler, headers and libraries into a user-owned prefix."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import shlex
import shutil
import sys
import tarfile
import tempfile

if __package__:
    from . import artifacts
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from h7607a import artifacts

LOCK_SHA256 = "2a68bd37e09dc52d469360ccdaf376e775acd3e93a6c63545a5f3e5beecd6e8a"
MARKER = ".h7607a-cuda-sdk.json"
PREREQUISITES = ("gcc", "binutils", "glibc", "make", "cmake", "ninja", "pkgconf", "python", "zlib", "ncurses", "libxml2")

def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(path):
    path = Path(os.path.abspath(Path(path).expanduser()))
    need(path.resolve() == path and not path.is_symlink(), f"Symlink path refused: {path}")
    return path


def default_lock():
    source = Path(__file__).resolve().parents[2] / "locks/cuda-sdk.json"
    return source if source.is_file() else Path("/usr/local/share/h7607a/cuda-sdk.json")


def read_lock(path=None):
    path = canonical(path or default_lock())
    need(path.is_file() and digest(path) == LOCK_SHA256, "CUDA SDK lock differs from the pinned helper")
    lock = artifacts.load_lock(path)
    need(lock.get("schema") == "h7607a-cuda-sdk-lock-v1" and lock.get("architecture") == "linux-sbsa",
         "CUDA SDK lock architecture/schema differs")
    return lock


def package_list() -> list[str]:
    """Official ALARM compile-host prerequisites; no CUDA/driver package roots."""
    return list(PREREQUISITES)


def configure_target(target, username) -> dict:
    """Stage the optional bootstrap into an offline target; never execute it."""
    target = canonical(target)
    need(target != Path("/") and target.is_dir(), "Use an offline target root")
    need(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) and username != "root", "Choose an ordinary target user")
    read_lock()
    mapping = {
        "usr/local/lib/h7607a/cuda_sdk.py": Path(__file__).read_bytes(),
        "usr/local/lib/h7607a/artifacts.py": Path(artifacts.__file__).read_bytes(),
        "usr/local/lib/h7607a/__init__.py": b'"""H7607A installed helper library."""\n',
        "usr/local/share/h7607a/cuda-sdk.json": default_lock().read_bytes(),
        "usr/local/bin/h7607a-cuda": b'#!/bin/sh\nexec /usr/bin/python3 -I -B /usr/local/lib/h7607a/cuda_sdk.py "$@"\n',
    }
    files = {}
    for name, data in mapping.items():
        path = canonical(target / name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".cuda-helper-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                os.fchmod(output.fileno(), 0o755 if name.startswith("usr/local/bin/") else 0o644)
                if os.geteuid() == 0:
                    os.fchown(output.fileno(), 0, 0)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        files["/" + name] = {"sha256": digest(path), "bytes": len(data)}
    return {"packages": package_list(), "files": files, "toolkit_installed": False,
            "automatic_downloads": False, "driver_modified": False, "target_programs_executed": False,
            "normal_user_command": "h7607a-cuda install-sdk --online"}


def user_home():
    need(os.geteuid() != 0, "Run as your ordinary user, without sudo")
    need(platform.system() == "Linux" and platform.machine() in {"aarch64", "arm64"},
         "This SDK is native Linux ARM64 SBSA, not macOS or x86")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    need(home.is_dir() and home.stat().st_uid == os.getuid(), "User home ownership differs")
    return home


def inside_home(path, home):
    path = canonical(path)
    need(path != home and path.is_relative_to(home), "SDK prefix/cache must be beneath your home directory")
    return path


def filesystem(path):
    """Free bytes and device number of the filesystem that will hold path."""
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free, path.stat().st_dev


def require_space(lock, prefix, cache):
    free, device = filesystem(prefix)
    need(free >= lock["reserve_bytes"],
         f"Installing the CUDA SDK needs {lock['reserve_bytes'] / 1024**3:.1f} GiB free on the filesystem "
         f"of {prefix}; {free / 1024**3:.1f} GiB are free")
    cache_free, cache_device = filesystem(cache)
    if cache_device != device:
        need(cache_free >= lock["download_bytes"],
             f"The CUDA download cache needs {lock['download_bytes'] / 1024**3:.1f} GiB free on the "
             f"filesystem of {cache}; {cache_free / 1024**3:.1f} GiB are free")


def component_objects(lock, cache, component_cache=None, online=False):
    need(online or component_cache is not None, "Supply --component-cache DIR or explicitly choose --online")
    sources = (canonical(component_cache),) if component_cache else ()
    if sources:
        need(sources[0].is_dir(), "Offline component cache must be a directory")
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    paths = {}
    for index, row in enumerate(lock["artifacts"], 1):
        print(f"[{index}/{len(lock['artifacts'])}] Verifying/downloading {row['component']} {row['version']}", flush=True)
        paths[row["component"]] = artifacts.fetch(cache, row, cache_dirs=sources, offline=not online)
    return paths


def member_path(name, root):
    parts = PurePosixPath(name).parts
    need(parts and parts[0] == root and not name.startswith("/")
         and all(p not in {"", ".", ".."} for p in name.split("/"))
         and "\\" not in name and "\x00" not in name, "Unsafe CUDA archive member path")
    return Path(*parts[1:]) if len(parts) > 1 else None


def safe_parent(path, root):
    need(path.is_relative_to(root), "Extraction escaped SDK prefix")
    for parent in [path, *path.parents]:
        if parent == root:
            break
        need(not parent.is_symlink(), f"CUDA archive traverses a symlink: {parent}")


def extract_component(archive_path, row, destination):
    """Flatten NVIDIA's component root; retain licenses without overwriting them.

    Device nodes, hard links, outside links and conflicting component files are
    rejected. All regular files are extracted before their relative symlinks.
    """
    artifacts.verify_file(archive_path, row)
    destination = canonical(destination)
    links, count, total = [], 0, 0
    with tarfile.open(archive_path, "r:xz") as archive:
        for entry in archive:
            relative = member_path(entry.name.rstrip("/"), row["archive_root"])
            if relative is None:
                need(entry.isdir(), "CUDA archive root must be a directory")
                continue
            # Every component supplies its own license; retain all of them.
            if len(relative.parts) == 1 and relative.name.upper().startswith(("LICENSE", "EULA")):
                relative = Path("licenses") / row["component"] / relative
            target = destination / relative
            safe_parent(target, destination)
            need(entry.isdir() or entry.isfile() or entry.issym(), "Unsupported CUDA archive entry type")
            if entry.isdir():
                need(not target.exists() or target.is_dir(), "Directory collides with SDK file")
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            if entry.issym():
                link = entry.linkname
                need(link and not PurePosixPath(link).is_absolute() and "\\" not in link and "\x00" not in link,
                     "Absolute or malformed CUDA archive symlink")
                resolved = (target.parent / link).resolve()
                need(resolved.is_relative_to(destination), "CUDA archive symlink escapes SDK prefix")
                links.append((target, link))
                continue
            need(0 <= entry.size <= 16 * 1024**3, "Unreasonable CUDA component member size")
            total += entry.size
            need(total <= 32 * 1024**3 and count < 250000, "CUDA component expansion limit exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(entry)
            need(source is not None, "Missing CUDA component bytes")
            if target.exists():
                need(target.is_file(), "CUDA components conflict in type")
                hasher = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
                need(target.stat().st_size == entry.size and digest(target) == hasher.hexdigest(),
                     f"CUDA components conflict: {relative}")
            else:
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                need(target.stat().st_size == entry.size, "Truncated CUDA component")
                target.chmod(0o755 if entry.mode & 0o111 else 0o644)
            count += 1
    for target, link in links:
        safe_parent(target.parent, destination)
        if target.is_symlink():
            need(os.readlink(target) == link, "Conflicting CUDA symlink")
        else:
            need(not target.exists(), "CUDA symlink collides with a regular file")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        need(target.resolve().is_relative_to(destination), "CUDA symlink chain escapes SDK prefix")
    return {"component": row["component"], "regular_files": count, "expanded_bytes": total, "symlinks": len(links)}


def normalize_layout(prefix):
    """Adapt the exact vendor component layout without changing compiler binaries."""
    profile = prefix / "bin/nvcc.profile"
    before = profile.read_text()
    substitutions = {
        "$(TOP)/$(_TARGET_DIR_)/include": "$(TOP)/include",
        "$(TOP)/$(_TARGET_DIR_)/lib$(_TARGET_SIZE_)": "$(TOP)/lib",
    }
    after = before
    for original, replacement in substitutions.items():
        need(after.count(original) == 2, "NVCC profile layout differs from the pinned NVIDIA archive")
        after = after.replace(original, replacement)
    profile.write_text(after)
    changes = {"bin/nvcc.profile": {"before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                                    "after_sha256": digest(profile)}}
    # Vendor .pc files assume /usr/local/cuda and the runfile targets/ tree.
    # NVIDIA component tarballs use root/lib and root/include instead.
    for pc in sorted((prefix / "pkg-config").glob("*.pc")):
        need(not pc.is_symlink() and pc.is_file(), "Linked pkg-config metadata refused")
        old = pc.read_text()
        new = old.replace("/usr/local/cuda-13.4", "${pcfiledir}/..")
        new = new.replace("/targets/sbsa-linux/", "/")
        need("/usr/local/cuda" not in new and "/targets/" not in new,
             "CUDA pkg-config paths differ from the SBSA component layout")
        pc.write_text(new)
        changes[str(pc.relative_to(prefix))] = {"before_sha256": hashlib.sha256(old.encode()).hexdigest(),
                                               "after_sha256": digest(pc)}
    lib64 = prefix / "lib64"
    need(not lib64.exists() and not lib64.is_symlink(), "Unexpected existing lib64 in component layout")
    lib64.symlink_to("lib")
    return changes


def environment_script(prefix, host):
    quote = shlex.quote
    return ("# Opt-in SDK shell environment. Do not source into the separate pinned PyTorch environment.\n"
            f"export CUDA_HOME={quote(str(prefix))}\nexport CUDA_PATH=\"$CUDA_HOME\"\n"
            "export CUDAToolkit_ROOT=\"$CUDA_HOME\"\nexport CUDACXX=\"$CUDA_HOME/bin/nvcc\"\n"
            f"export NVCC_CCBIN={quote(host['path'])}\n"
            'export PATH="$CUDA_HOME/bin:$PATH"\n'
            'export PKG_CONFIG_PATH="$CUDA_HOME/pkg-config${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"\n'
            'export LD_LIBRARY_PATH="$CUDA_HOME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n')


def install_sdk(*, prefix=None, online=False, component_cache=None, cache=None):
    home = user_home()
    lock = read_lock()
    prefix = inside_home(prefix or home / (".local/opt/cuda-" + lock["toolkit_version"]), home)
    cache = inside_home(cache or home / (".cache/h7607a/cuda-" + lock["toolkit_version"]), home)
    need(not cache.is_relative_to(prefix), "SDK cache must not be inside the installation prefix")
    if prefix.exists():
        marker = prefix / MARKER
        need(marker.is_file() and not marker.is_symlink() and prefix.stat().st_uid == os.getuid(),
             "Existing SDK prefix is not a user-owned installation; choose another --prefix")
        report = json.loads(marker.read_text())
        need(report.get("status") == "COMPLETE" and report.get("lock_sha256") == LOCK_SHA256
             and report.get("prefix") == str(prefix),
             "Existing SDK prefix does not contain a completed matching installation; choose another --prefix")
        print(f"CUDA SDK {lock['toolkit_version']} installed at {prefix}.", flush=True)
        return report
    need(online or component_cache is not None, "Supply --component-cache DIR or explicitly choose --online")
    require_space(lock, prefix, cache)
    host = {"path": lock["host_compiler"]["selected"]}
    prefix.parent.mkdir(parents=True, exist_ok=True)
    paths = component_objects(lock, cache, component_cache, online)
    stage = Path(tempfile.mkdtemp(prefix=".h7607a-cuda-", dir=prefix.parent))
    try:
        extracted = []
        for row in lock["artifacts"]:
            print(f"Extracting {row['component']}", flush=True)
            extracted.append(extract_component(paths[row["component"]], row, stage))
        transformations = normalize_layout(stage)
        (stage / "env.sh").write_text(environment_script(prefix, host))
        report = {"schema": "h7607a-cuda-sdk-install-v1", "status": "COMPLETE",
                  "toolkit_version": lock["toolkit_version"], "lock_sha256": LOCK_SHA256,
                  "prefix": str(prefix), "host_compiler": host, "components": extracted,
                  "layout_transformations": transformations, "driver_modified": False}
        (stage / MARKER).write_text(json.dumps(report, indent=2) + "\n")
        need(not prefix.exists() and not prefix.is_symlink(), "SDK destination appeared during extraction")
        stage.rename(prefix)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(f"CUDA SDK {lock['toolkit_version']} installed at {prefix}.\n"
          f"Activate: . {shlex.quote(str(prefix / 'env.sh'))}", flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install-sdk", help="Opt-in native CUDA compiler, headers and libraries")
    install.add_argument("--online", action="store_true", help="Explicitly allow NVIDIA component downloads (2.9 GB)")
    install.add_argument("--component-cache", type=Path, help="Directory containing the exact pinned NVIDIA tar.xz files; otherwise offline")
    install.add_argument("--prefix", type=Path, help="New SDK directory beneath your home")
    install.add_argument("--cache", type=Path, help="Artifact cache beneath your home")
    args = parser.parse_args(argv)
    try:
        install_sdk(prefix=args.prefix, online=args.online, component_cache=args.component_cache,
                    cache=args.cache)
    except (OSError, RuntimeError, ValueError, artifacts.ArtifactError, tarfile.TarError) as exc:
        parser.exit(1, f"CUDA SDK: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
