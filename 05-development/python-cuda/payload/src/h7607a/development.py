"""Optional developer tools and the exact native ARM64 PyTorch environment.

Package installation belongs to the system installer. The AI environment belongs
to the ordinary user and never invokes sudo or installs into system Python.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile

if __package__:
    from . import artifacts
else:
    # Installed launcher uses -I; load only our root-owned sibling package.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from h7607a import artifacts

LOCK_SHA256 = "7a416cc0b44293bb3389ee287746b0d76018740b7def7743e208f068caea0b17"
WHEEL_NAME = "nvidia_cusparselt_cu13-0.8.1-py3-none-manylinux2014_aarch64.whl"
WHEEL_SHA256 = "4dca476c50bf4780d46cd0bfbd82e2bc10a08e4fef7950917ce8d7578d22a23f"
DIST = "nvidia_cusparselt_cu13-0.8.1.dist-info"
ORIGINAL_WHEEL = (b"Wheel-Version: 1.0\nGenerator: setuptools (80.9.0)\n"
                  b"Root-Is-Purelib: true\nTag: py3-none-manylinux2014_sbsa\n\n")
FIXED_WHEEL = ORIGINAL_WHEEL.replace(b"manylinux2014_sbsa", b"manylinux2014_aarch64")
LIBRARY_SHA256 = "fbb03bbf8d2238670f24256c5452281c2a660162899b838bb4d0cc06dad027df"
MARKER = ".h7607a-torch.json"
PROFILE_PACKAGES = {
    "python": ["python", "python-pip"],
    "cpp": ["base-devel", "cmake", "ninja", "pkgconf"],
    "ai": ["python", "python-pip"],
}
BOOTSTRAP = "import sys;sys.path.insert(0,sys.argv.pop(1));from pip._internal.cli.main import main;raise SystemExit(main())"


def need(value, message):
    if not value:
        raise RuntimeError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular(path):
    path = Path(path)
    need(path.is_file() and not path.is_symlink(), f"Expected a regular file: {path}")
    return path


def canonical(path):
    path = Path(os.path.abspath(Path(path).expanduser()))
    need(path.resolve() == path and not path.is_symlink(), f"Linked path is not accepted: {path}")
    return path


def default_lock():
    source = Path(__file__).resolve()
    repository = source.parents[2] / "locks/development.json"
    return repository if repository.is_file() else Path("/usr/local/share/h7607a/development.json")


def read_lock(path=None):
    path = regular(path or default_lock())
    need(sha256(path) == LOCK_SHA256, "Development lock differs from this pinned helper")
    value = json.loads(path.read_text())
    need(value.get("schema") == "h7607a-development-lock-v1" and len(value["artifacts"]) == 31,
         "Unexpected development lock schema or wheel count")
    names = [row["filename"] for row in value["artifacts"]]
    need(len(set(names)) == 31 and all(Path(name).name == name and name.endswith(".whl") for name in names),
         "Wheel names must be unique plain filenames")
    need(hashlib.sha256(value["requirements"].encode()).hexdigest() == value["requirements_sha256"],
         "Requirements content differs from the original offline lock")
    return value


def package_list(profiles):
    profiles = set(profiles)
    unknown = profiles - set(PROFILE_PACKAGES) - {"containers", "cuda"}
    need(not unknown, "Unknown development profiles: " + ", ".join(sorted(unknown)))
    packages = {package for profile in profiles if profile in PROFILE_PACKAGES
                for package in PROFILE_PACKAGES[profile]}
    if "containers" in profiles:
        from . import containers
        packages.update(containers.package_list())
    if "cuda" in profiles:
        from . import cuda_sdk
        packages.update(cuda_sdk.package_list())
    return sorted(packages)


def configure_target(target, username, profiles=()):
    """Write persistent tooling into an offline populated root; do not run it."""
    target = canonical(target)
    need(target != Path("/") and target.is_dir(), "Use a mounted target root, never the running host root")
    need(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) and username != "root", "Invalid normal target username")
    profiles = sorted(set(profiles))
    packages = package_list(profiles)
    read_lock()
    files = {}
    if profiles:
        mapping = {
            "usr/local/lib/h7607a/development.py": Path(__file__).read_bytes(),
            "usr/local/lib/h7607a/artifacts.py": Path(artifacts.__file__).read_bytes(),
            "usr/local/lib/h7607a/__init__.py": b'"""H7607A installed helper library."""\n',
            "usr/local/share/h7607a/development.json": default_lock().read_bytes(),
            "usr/local/bin/h7607a-dev": ("#!/bin/sh\nexec /usr/bin/python3 -I -B "
                                          "/usr/local/lib/h7607a/development.py \"$@\"\n").encode(),
        }
        for name, data in mapping.items():
            path = canonical(target / name)
            path.parent.mkdir(parents=True, exist_ok=True)
            need(not path.exists() or path.is_file(), "Unexpected target helper type")
            path.write_bytes(data)
            path.chmod(0o755 if name.startswith("usr/local/bin/") else 0o644)
            if os.geteuid() == 0:
                os.chown(path, 0, 0)
            files["/" + name] = {"sha256": sha256(path), "bytes": len(data)}
    result = {"profiles": profiles, "packages": packages, "files": files,
              "ai_environment_installed": False, "automatic_downloads": False,
              "normal_user_command": "h7607a-dev install-torch --online" if "ai" in profiles else None,
              "cuda_sdk_installed": False}
    if "containers" in profiles:
        from . import containers
        result["containers"] = containers.configure_target(target, username)
    if "cuda" in profiles:
        from . import cuda_sdk
        result["cuda"] = cuda_sdk.configure_target(target, username)
    return result


def target_account(target, username):
    rows = [row.split(":") for row in regular(canonical(target / "etc/passwd")).read_text().splitlines()
            if row and not row.startswith("#")]
    found = [row for row in rows if len(row) == 7 and row[0] == username]
    need(len(found) == 1 and found[0][2].isdigit() and 1000 <= int(found[0][2]) < 65534,
         "Selected development profiles require an existing normal target user")
    row = found[0]
    home = Path(row[5])
    need(home.is_absolute() and home not in (Path("/"), Path("/root")) and ".." not in home.parts,
         "Target user home must be an absolute normal-user path")
    host_home = canonical(target / str(home).lstrip("/"))
    need(host_home.is_dir(), "Create the target user's home before installing development profiles")
    if os.geteuid() == 0:
        need(host_home.stat().st_uid == int(row[2]), "Target home is not owned by its normal user")
    return {"uid": int(row[2]), "home": str(home), "host_home": host_home}


def install_selected(target, username, profiles, repo_root, *, wheelhouse=None,
                     online=False, cuda_component_cache=None, runner=None):
    """Complete selected profiles inside an offline target as its ordinary user.

    The caller has installed package_list(profiles) and created the user. The
    PyTorch wheels come from the preserved USB carrier through wheelhouse, or,
    with online and no wheelhouse, from downloads the user runs inside the
    target. Nothing is downloaded on the live system and no implicit network
    fallback is allowed. runner is injectable for command substitution.
    """
    target = canonical(target)
    repo_root = canonical(repo_root)
    need(target != Path("/") and target.is_dir(), "A mounted offline target is required")
    profiles = sorted(set(profiles))
    package_list(profiles)
    lock = read_lock(repo_root / "locks/development.json")
    account = target_account(target, username)
    result = configure_target(target, username, profiles)
    result["selected_profiles_installed"] = False
    result["commands"] = []
    def user_run(command):
        from h7607a.hardware import _run

        arguments = ["/usr/bin/runuser", "-u", username, "--", *command]
        argv = ["unshare", "--mount", "--propagation", "private", "--fork", "--pid",
                "--kill-child=KILL", "--", "arch-chroot", *([] if online else ["-r"]),
                str(target), *arguments]
        result["commands"].append(argv)
        if runner is not None:
            runner(argv, check=True)
        else:
            # Keep package/SDK progress visible; isolate runtime mounts and reap
            # descendants on interruption or timeout, including daemon children.
            _run(target, arguments, timeout=7200, api_filesystems=True,
                 bind_dns=online, capture=False)
    if "ai" in profiles:
        venv = account["home"] + "/.local/share/h7607a/venvs/torch"
        if wheelhouse is None:
            need(online, "Supply the carrier wheelhouse or explicitly choose online installation")
            user_run(["/usr/local/bin/h7607a-dev", "install-torch", "--online", "--venv", venv])
        else:
            # A carrier bind beneath target/var/cache avoids a second 3 GB copy.
            # arch-chroot mounts fresh /run, /dev, /proc and /sys; never stage there.
            objects = wheel_objects(lock, wheelhouse)
            source_root = canonical(wheelhouse)
            staged = None
            try:
                if (source_root.is_relative_to(target) and source_root.relative_to(target).parts
                        and source_root.relative_to(target).parts[0] not in ("run", "dev", "proc", "sys")):
                    inside = "/" + source_root.relative_to(target).as_posix()
                else:
                    directory = canonical(target / "var/cache")
                    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
                    staged = Path(tempfile.mkdtemp(prefix="h7607a-ai-wheels-", dir=directory))
                    staged.chmod(0o755)
                    for filename, source in objects.items():
                        destination = staged / filename
                        # A readable immutable cache may be shared without changing its mode.
                        if source.stat().st_mode & 0o444 == 0o444 and not source.stat().st_mode & 0o022:
                            try:
                                os.link(source, destination)
                            except OSError:
                                shutil.copyfile(source, destination)
                                destination.chmod(0o444)
                        else:
                            shutil.copyfile(source, destination)
                            destination.chmod(0o444)
                    inside = "/" + staged.relative_to(target).as_posix()
                user_run(["/usr/local/bin/h7607a-dev", "install-torch", "--wheelhouse", inside, "--venv", venv])
            finally:
                if staged is not None:
                    shutil.rmtree(staged)
        result.update(ai_environment_installed=True, ai_venv=venv,
                      normal_user_command="source " + shlex.quote(venv + "/bin/activate"))
    if "cuda" in profiles:
        from . import cuda_sdk
        command = ["/usr/local/bin/h7607a-cuda", "install-sdk"]
        if cuda_component_cache is not None:
            cache = canonical(cuda_component_cache)
            need(cache.is_relative_to(target), "Stage the CUDA component cache beneath the target root")
            command.extend(["--component-cache", "/" + cache.relative_to(target).as_posix()])
        if online:
            command.append("--online")
        need(cuda_component_cache is not None or online, "Selected CUDA SDK needs its component cache or explicit online access")
        user_run(command)
        sdk_lock = cuda_sdk.read_lock(repo_root / "locks/cuda-sdk.json")
        sdk_prefix = account["home"] + "/.local/opt/cuda-" + sdk_lock["toolkit_version"]
        result.update(cuda_sdk_installed=True, cuda_prefix=sdk_prefix)
        result["cuda"].update(toolkit_installed=True)
    result["selected_profiles_installed"] = True
    return result


def check_object(path, row):
    path = regular(path)
    return artifacts.verify_file(path, row)


def fetch_object(row, destination):
    """Download one exact wheel into a private cache, publishing only after hash verification."""
    destination = canonical(destination)
    if destination.exists():
        return check_object(destination, row)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return artifacts.fetch(destination.parent, dict(row, path=destination.name))


def wheel_objects(lock, wheelhouse=None, online=False, cache=None):
    need(wheelhouse is not None or online, "Supply --wheelhouse DIR or explicitly choose --online")
    root = canonical(wheelhouse) if wheelhouse else None
    cache = canonical(cache or Path.home() / ".cache/h7607a/wheels")
    paths = {}
    for index, row in enumerate(lock["artifacts"], 1):
        candidate = None
        if root:
            # Accept either a flat wheelhouse, repo artifact root, or offline carrier root.
            for relative in (row["filename"], row["path"], row["path"].removeprefix("assets/development/")):
                found = canonical(root / relative)
                if found.exists():
                    candidate = check_object(found, row)
                    break
        if candidate is None:
            need(online, "Offline wheel is missing: " + row["filename"])
            print(f"[{index}/31] Downloading or verifying {row['filename']}", flush=True)
            candidate = fetch_object(row, cache / row["filename"])
        paths[row["filename"]] = candidate
    return paths


def normalize_cusparselt_metadata(venv, wheel):
    """Apply the native-proven metadata-only vendor tag repair; preserve library bytes."""
    wheel = regular(wheel)
    need(wheel.name == WHEEL_NAME and sha256(wheel) == WHEEL_SHA256,
         "cuSPARSELt repair requires the pinned NVIDIA wheel")
    site = canonical(canonical(venv) / "lib/python3.14/site-packages")
    dist = canonical(site / DIST)
    wheel_path, record_path = regular(dist / "WHEEL"), regular(dist / "RECORD")
    need(wheel_path.stat().st_nlink == record_path.stat().st_nlink == 1
         and wheel_path.stat().st_size < 1024 * 1024 and record_path.stat().st_size < 1024 * 1024,
         "Unreasonably large cuSPARSELt metadata")
    wheel_relative = DIST + "/WHEEL"
    library_relative = "nvidia/cusparselt/lib/libcusparseLt.so.0"
    with zipfile.ZipFile(wheel) as archive:
        vendor_wheel = archive.read(wheel_relative)
        vendor_record = list(csv.reader(io.StringIO(archive.read(DIST + "/RECORD").decode())))
    need(vendor_wheel == ORIGINAL_WHEEL and wheel_path.read_bytes() in (ORIGINAL_WHEEL, FIXED_WHEEL),
         "cuSPARSELt WHEEL metadata differs from the pinned upstream tag")
    library = regular(canonical(site / library_relative))
    with library.open("rb") as stream:
        header = stream.read(20)
    need(header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 183,
         "cuSPARSELt library is not little-endian AArch64 ELF64")
    library_sha = sha256(library)
    need(library_sha == LIBRARY_SHA256, "cuSPARSELt library differs from the native-proven library")
    def record_hash(data):
        return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
    library_row = [library_relative, "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(library_sha)).decode().rstrip("="), str(library.stat().st_size)]
    original_row = [wheel_relative, record_hash(ORIGINAL_WHEEL), str(len(ORIGINAL_WHEEL))]
    fixed_row = [wheel_relative, record_hash(FIXED_WHEEL), str(len(FIXED_WHEEL))]
    need([row for row in vendor_record if row and row[0] == library_relative] == [library_row]
         and [row for row in vendor_record if row and row[0] == wheel_relative] == [original_row],
         "Vendor RECORD does not prove the exact library and original metadata")
    before = record_path.read_bytes()
    records = list(csv.reader(io.StringIO(before.decode())))
    before_wheel = wheel_path.read_bytes()
    wanted = fixed_row if before_wheel == FIXED_WHEEL else original_row
    need([row for row in records if row and row[0] == wheel_relative] == [wanted]
         and [row for row in records if row and row[0] == library_relative] == [library_row],
         "Installed RECORD differs before the metadata repair")
    for row in records:
        if row and row[0] == wheel_relative:
            row[:] = fixed_row
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(records)
    after = buffer.getvalue().encode()
    wheel_path.write_bytes(FIXED_WHEEL)
    record_path.write_bytes(after)
    need(wheel_path.read_bytes() == FIXED_WHEEL and record_path.read_bytes() == after
         and sha256(library) == library_sha and sha256(wheel) == WHEEL_SHA256,
         "Metadata repair readback or unchanged library verification failed")
    return {"status": "ALREADY_APPLIED" if before_wheel == FIXED_WHEEL else "APPLIED",
            "upstream_wheel_sha256": WHEEL_SHA256, "library_sha256": library_sha,
            "before_WHEEL_sha256": hashlib.sha256(before_wheel).hexdigest(),
            "after_WHEEL_sha256": hashlib.sha256(FIXED_WHEEL).hexdigest(),
            "before_RECORD_sha256": hashlib.sha256(before).hexdigest(),
            "after_RECORD_sha256": hashlib.sha256(after).hexdigest(),
            "library_modified": False, "vendor_wheel_modified": False}


def clean_environment():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "PIP_", "LD_"))}
    env.update(PIP_CONFIG_FILE="/dev/null", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def run(command):
    subprocess.run([str(item) for item in command], check=True, env=clean_environment())


def install_torch(args):
    need(os.geteuid() != 0, "Run as your ordinary user. Do not use sudo for the AI environment")
    need(platform.system() == "Linux" and platform.machine() == "aarch64"
         and sys.version_info[:2] == (3, 14), "This exact wheel lock requires aarch64 Linux and CPython 3.14")
    lock = read_lock(args.lock)
    venv = canonical(args.venv or Path.home() / ".local/share/h7607a/venvs/torch")
    need(venv.is_relative_to(Path.home().resolve()), "Choose a venv inside your own home directory")
    if venv.exists():
        marker = regular(venv / MARKER)
        prior = json.loads(marker.read_text())
        need(prior.get("lock_sha256") == LOCK_SHA256 and prior.get("status") == "COMPLETE"
             and venv.stat().st_uid == os.getuid(),
             f"Existing environment is not a completed matching installation: {venv}. "
             "Choose a new --venv path, or remove this incomplete environment yourself before retrying")
        print(f"Environment already available: {venv}")
        return
    need(args.wheelhouse or args.online, "Supply --wheelhouse DIR or explicitly choose --online (about 3.2 GB)")
    parent = venv.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    wheels = wheel_objects(lock, args.wheelhouse, args.online)
    # Venv entry-point scripts contain absolute paths, so create at its final path.
    # A failed setup retains an explicit INCOMPLETE marker; never reuse arbitrary contents.
    venv.mkdir(mode=0o700)
    receipt = {"schema": "h7607a-torch-install-v1", "lock_sha256": LOCK_SHA256,
               "status": "INCOMPLETE", "uid": os.getuid(), "venv": str(venv)}
    marker = venv / MARKER
    marker.write_text(json.dumps(receipt, indent=2) + "\n")
    run([sys.executable, "-I", "-B", "-m", "venv", "--without-pip", venv])
    with tempfile.TemporaryDirectory(prefix="h7607a-pip-") as temporary:
        inputs = Path(temporary)
        for name, source in wheels.items():
            (inputs / name).symlink_to(source)
        pip_row = next(row for row in lock["artifacts"] if row["name"] == "pip")
        requirements = inputs / "requirements.lock"
        requirements.write_text(lock["requirements"] + f"pip=={pip_row['version']} --hash=sha256:{pip_row['sha256']}\n")
        run([venv / "bin/python", "-I", "-B", "-c", BOOTSTRAP, wheels[pip_row["filename"]],
             "--isolated", "install", "--no-index", "--no-cache-dir", "--no-deps", "--only-binary=:all:",
             "--require-hashes", "--find-links", inputs, "-r", requirements])
    receipt["cusparselt_metadata_repair"] = normalize_cusparselt_metadata(venv, wheels[WHEEL_NAME])
    receipt.update(status="COMPLETE", system_python_modified=False)
    marker.write_text(json.dumps(receipt, indent=2) + "\n")
    marker.chmod(0o600)
    print(f"Ready: {venv}\nActivate: source {venv}/bin/activate")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("install-torch", help="Create the pinned CUDA PyTorch environment as your ordinary user")
    command.add_argument("--wheelhouse", type=Path, help="Flat wheel directory, repo root, or offline carrier root")
    command.add_argument("--online", action="store_true", help="Explicitly allow roughly 3.2 GB of pinned HTTPS downloads")
    command.add_argument("--venv", type=Path)
    command.add_argument("--lock", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        install_torch(args)
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError, zipfile.BadZipFile) as error:
        print("h7607a-dev: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
