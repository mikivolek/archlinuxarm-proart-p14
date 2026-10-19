"""Small platform adapter around Archinstall 4.5's real guided installer.

Partition editing, formatting, encryption, users, passwords and packages remain
Archinstall operations. Hardware-specific hooks replace its x86 kernel/boot
defaults; native Archinstall performs filesystem and package operations.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import ssl
import subprocess
import sys
import tempfile
import time
from types import ModuleType
from urllib.parse import urlparse
from urllib.request import urlopen


class InstallError(RuntimeError):
    pass


POWER_CONFLICTS = frozenset({"tuned", "tuned-ppd", "tlp", "tlp-pd", "asusctl", "asusd", "auto-cpufreq", "system76-power"})
# Archinstall 4.5 applications/power_management.py; other choices install the package they name.
POWER_MANAGEMENT_PACKAGES = {"tuned": ("tuned", "tuned-ppd")}


def reject_power_conflicts(packages) -> None:
    conflicts = set(packages) & POWER_CONFLICTS
    if conflicts:
        raise InstallError(f"Selected packages conflict with the required P16 power service: {sorted(conflicts)}")


def power_management_packages(config) -> list[str]:
    """Packages installed by the Applications > Power management choice."""
    choice = getattr(getattr(config, "app_config", None), "power_management_config", None)
    if choice is None:
        return []
    name = str(value(choice.power_management))
    return list(POWER_MANAGEMENT_PACKAGES.get(name, (name,)))


def command(argv: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise InstallError(f"{argv[0]} failed ({result.returncode}): {detail[-8000:]}")
    return result.stdout


def load_lock(repo: Path) -> dict:
    return json.loads((repo / "locks/archinstall.json").read_text())


def verify_archinstall(lock: dict) -> Path:
    """Refuse any Archinstall other than the pinned release before using its internals.

    Locate and hash the package without importing it: importing archinstall
    runs plugin discovery, and importing its installer module runs localectl.
    """
    try:
        version = importlib.metadata.version("archinstall")
    except importlib.metadata.PackageNotFoundError:
        version = None
    if version != lock["version"]:
        raise InstallError(f"This installer requires the pinned Archinstall {lock['version']}, "
                           f"found {version or 'none'}. Disks were not changed.")
    spec = importlib.util.find_spec("archinstall")
    if spec is None or not spec.origin or Path(spec.origin).name != "__init__.py":
        raise InstallError("The Archinstall package directory was not found. Disks were not changed.")
    package = Path(spec.origin).parent
    changed = []
    for row in lock["api_files"]:
        try:
            with (package / row["path"]).open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            actual = None
        if actual != row["sha256"]:
            changed.append(row["path"])
    if changed:
        raise InstallError(f"Archinstall {version} in {package} differs from the pinned release: "
                           + ", ".join(changed) + ". Disks were not changed.")
    return package


def verify_device(repo: Path, dmi: Path = Path("/sys/class/dmi/id")) -> None:
    from h7607a.hardware import load_hardware_lock

    device = load_hardware_lock(repo)["device"]
    found = {}
    for key in ("board_name", "bios_version"):
        try:
            found[key] = (dmi / key).read_text().strip()
        except OSError:
            found[key] = "unknown"
    if found != {key: device[key] for key in found}:
        raise InstallError(f"This installer supports only the {device['sys_vendor']} {device['product_name']} "
                           f"with firmware {device['bios_version']}; this machine reports board "
                           f"{found['board_name']} with firmware {found['bios_version']}. The ACPI overrides "
                           f"it installs are built from the {device['bios_version']} firmware tables and would "
                           "not match this machine. Disks were not changed.")


def value(obj):
    return getattr(obj, "value", obj)


def canonical_device(path: str | Path) -> str:
    path = str(path)
    if not path.startswith("/dev/"):
        raise InstallError(f"Invalid block device path: {path}")
    return os.path.realpath(path)


class BlockGraph:
    """All ancestry, including dm/LVM devices appearing under multiple disks."""
    def __init__(self, document: dict):
        self.nodes: dict[str, dict] = {}
        self.parents: dict[str, set[str]] = {}

        def visit(row, parent=None):
            path = canonical_device(row["path"])
            self.nodes.setdefault(path, row)
            edges = self.parents.setdefault(path, set())
            if parent:
                edges.add(parent)
            if row.get("pkname"):
                pk = row["pkname"]
                edges.add(canonical_device(pk if pk.startswith("/") else "/dev/" + pk))
            for child in row.get("children", []):
                visit(child, path)
        for row in document.get("blockdevices", []):
            visit(row)

    @classmethod
    def read(cls):
        return cls(json.loads(command(["lsblk", "--json", "--bytes", "--paths", "--output",
            "PATH,TYPE,SIZE,PKNAME,MOUNTPOINTS,TRAN"])))

    def physical_parents(self, path: str | Path, seen=None) -> set[str]:
        path = canonical_device(path)
        seen = set() if seen is None else set(seen)
        if path in seen:
            raise InstallError("Cyclic block-device ancestry")
        seen.add(path)
        row = self.nodes.get(path)
        if row is None:
            raise InstallError(f"Unresolved block-device ancestry: {path}")
        if row["type"] == "disk":
            return {path}
        edges = self.parents.get(path, set())
        if not edges:
            raise InstallError(f"Device has no physical-disk ancestry: {path}")
        return set().union(*(self.physical_parents(p, seen) for p in edges))

    def selected_target(self, devices) -> str:
        targets = {canonical_device(device) for device in devices}
        if len(targets) != 1:
            raise InstallError("Select one internal installation disk")
        target = targets.pop()
        if self.nodes.get(target, {}).get("type") != "disk":
            raise InstallError("The installation target must be a physical disk")
        if self.nodes[target].get("tran") == "usb":
            raise InstallError("Select the internal installation disk")
        for path, row in self.nodes.items():
            mounts = row.get("mountpoints") or []
            if isinstance(mounts, str):
                mounts = [mounts]
            if any(m in {"/", "/boot", "/boot/efi"} for m in mounts):
                if target in self.physical_parents(path):
                    raise InstallError("The selected disk contains the running installer")
        return target

    def require_target(self, path: str | Path, target: str) -> None:
        if self.physical_parents(path) != {target}:
            raise InstallError(f"Selected device belongs to another physical disk: {path}")


def validate_mirror_config(config) -> None:
    if config is None:
        return
    if config.optional_repositories or config.custom_repositories:
        names = [str(value(repo)) for repo in config.optional_repositories]
        names += [repo.name for repo in config.custom_repositories]
        raise InstallError("Unsupported ARM repository selections: " + ", ".join(names) +
                           ". Reset Mirrors and repositories to use the configured ALARM repositories.")
    urls = [s.url for s in config.custom_servers]
    urls += [u for r in config.mirror_regions for u in r.urls]
    for url in urls:
        host = urlparse(url).hostname or ""
        if not (host == "mirror.archlinuxarm.org" or host.endswith(".mirror.archlinuxarm.org")):
            raise InstallError(f"This installation requires an Arch Linux ARM mirror: {host}")
        if not url.endswith("/$arch/$repo"):
            raise InstallError("ARM mirror URLs must end in /$arch/$repo")


def validate_configuration(config, graph: BlockGraph, lock: dict) -> str:
    """Runs before native unmount/partition/LVM/format operations, every time."""
    from h7607a import offline_locale

    disk = config.disk_config
    if disk is None:
        raise InstallError("Select a disk layout before installing")
    # The guided path supplies the native partition model for boot parameters.
    if value(disk.config_type) == "pre_mounted_config":
        raise InstallError("Use normal automatic or manual partitioning; pre-mounted targets are not implemented")
    modifications = disk.device_modifications
    if not modifications:
        raise InstallError("No internal-drive partition layout selected")
    target = graph.selected_target(mod.device_path for mod in modifications)
    parts = []
    for mod in modifications:
        if canonical_device(mod.device_path) != target:
            raise InstallError("All selected partitions must belong to the installation disk")
        graph.require_target(mod.device_path, target)
        parts += list(mod.partitions)
        for part in mod.partitions:
            if part.dev_path and part.is_exists_or_modify():
                graph.require_target(part.dev_path, target)
    active = [p for p in parts if not p.is_delete()]
    lvm = disk.lvm_config
    volumes = []
    if lvm:
        for group in lvm.vol_groups:
            if not group.pvs or any(p not in active for p in group.pvs):
                raise InstallError("Every LVM physical volume must be an active partition on the selected internal SSD")
            if any(value(getattr(v, "status", None)) == "delete" for v in group.volumes):
                raise InstallError("Remove deleted logical volumes from the proposed LVM layout")
            volumes += group.volumes
    encryption = disk.disk_encryption
    if encryption:
        if value(getattr(encryption, "encryption_type", None)) == "luks_on_lvm":
            raise InstallError("LUKS-on-LVM boot aliases are not implemented; use ordinary LUKS or LVM-on-LUKS")
        if encryption.hsm_device:
            raise InstallError("FIDO/TPM systemd initrd is not implemented; ordinary password LUKS is available")
        if any(p not in active for p in encryption.partitions) or any(v not in volumes for v in encryption.lvm_volumes):
            raise InstallError("Encryption references a deleted volume or one outside the selected internal SSD")
        if any(value(item.fs_type) == "linux-swap" for item in [*encryption.partitions, *encryption.lvm_volumes]):
            raise InstallError("Separate encrypted swap is not implemented; use unencrypted swap, zram, or swap in the encrypted root LVM group")
    mounts = []
    for item in active + volumes:
        subvols = getattr(item, "btrfs_subvols", [])
        for subvol in subvols:
            name = str(subvol.name)
            if not re.fullmatch(r"[A-Za-z0-9_+./@-]+", name) or any(p in {"", ".", ".."} for p in name.split("/")):
                raise InstallError("Btrfs subvolume names must be plain relative paths without traversal or spaces")
        for entry in [item, *subvols]:
            point = getattr(entry, "mountpoint", None)
            if point is None:
                continue
            point = Path(point)
            if not point.is_absolute() or ".." in point.parts:
                raise InstallError("Partition/subvolume mountpoints must be absolute and cannot contain traversal")
            if any(point == Path(p) or Path(p) in point.parents for p in ("/dev", "/proc", "/sys", "/run")):
                raise InstallError("Partitions may not replace kernel runtime filesystems")
            if point != Path("/boot") and Path("/boot") in point.parents:
                raise InstallError("Nested mounts below /boot are incompatible with the EFI boot layout")
            mounts.append(point)
    if len(set(mounts)) != len(mounts):
        raise InstallError("Partition/subvolume mountpoints must be unique")
    boot = [p for p in active if p.mountpoint == Path("/boot")]
    if len(boot) != 1 or not boot[0].is_efi() or not boot[0].is_boot() or str(value(boot[0].fs_type)) != "fat32":
        raise InstallError("The P16 boot chain requires one FAT32 EFI system partition mounted at /boot")
    length = getattr(boot[0], "length", None)
    if length is not None:
        unit = value(length.unit)
        size = length.value * (length.sector_size.normalize() if unit == "sectors" else unit)
        if size < 512 * 1024**2:
            raise InstallError("The P16 boot artifacts require an EFI partition of at least 512 MiB")
    if encryption and boot[0] in encryption.partitions:
        raise InstallError("The EFI system partition must remain unencrypted")
    roots = [p for p in active + volumes if p.is_root()]
    if len(roots) != 1 or str(value(roots[0].fs_type)) not in {"ext4", "btrfs"}:
        raise InstallError("Select one ext4 or Btrfs root filesystem")
    if encryption and value(getattr(encryption, "encryption_type", None)) == "lvm_on_luks":
        # Native 4.5 requires exactly one PV segment to generate cryptdevice.
        # Reject unsupported roots before partitioning, not after pacstrap.
        root_groups = [group for group in lvm.vol_groups if roots[0] in group.volumes] if lvm else []
        if len(root_groups) != 1 or len(root_groups[0].pvs) != 1:
            raise InstallError("LVM-on-LUKS requires the root volume group to use exactly one physical volume")
        if root_groups[0].pvs[0] not in encryption.partitions:
            raise InstallError("LVM-on-LUKS requires the root physical volume to be selected for encryption")
        if any(group != root_groups[0] and any(pv in encryption.partitions for pv in group.pvs)
               and any(value(volume.fs_type) == "linux-swap" for volume in group.volumes)
               for group in lvm.vol_groups):
            raise InstallError("Encrypted LVM swap must share the root volume group opened during boot")
    if any(p.mountpoint in {Path("/efi"), Path("/boot/efi")} for p in active):
        raise InstallError("Separate EFI/XBOOTLDR layouts are not implemented; mount the ESP at /boot")
    bc = config.bootloader_config
    if bc is None or value(bc.bootloader) != "Systemd-boot" or bc.uki or bc.plymouth:
        raise InstallError("The P16 requires its systemd-boot/EDK2 chain, without UKI or Plymouth")
    if config.kernels != [lock["kernel_package"]]:
        raise InstallError("Select the N1X kernel package")
    if config.profile_config and config.profile_config.gfx_driver is not None:
        raise InstallError("The N1X NVIDIA package supplies the graphics driver")
    if getattr(getattr(config, "auth_config", None), "u2f_config", None):
        raise InstallError("Configure U2F login after boot; its native enrollment requires a running target system")
    try:
        # The target is not populated yet; check the live glibc and kbd catalogs.
        offline_locale.validate_choices(Path("/"), config.locale_config)
    except (OSError, ValueError) as error:
        raise InstallError(f"Change the locale settings: {error}") from error
    validate_mirror_config(config.mirror_config)
    blocked = set(config.packages) & set(lock["incompatible_packages"])
    if blocked:
        raise InstallError(f"Additional packages would replace the N1X kernel or NVIDIA driver: {sorted(blocked)}")
    reject_power_conflicts(config.packages)
    conflicts = set(power_management_packages(config)) & POWER_CONFLICTS
    if conflicts:
        raise InstallError(f"Applications > Power management would install {', '.join(sorted(conflicts))}, which "
                           "conflicts with the required P16 power service; choose power-profiles-daemon or none")
    profiles = getattr(config, "development_profiles", [])
    if profiles:
        auth = getattr(config, "auth_config", None)
        if not auth or not any(u.username != "root" for u in auth.users):
            raise InstallError("Create an ordinary user for the selected development tools")
        from h7607a import cuda_sdk, development
        try:
            development.read_lock()
            if "cuda" in profiles:
                cuda_sdk.read_lock()
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            raise InstallError(f"The development tool locks cannot be used: {error}") from error
    network = getattr(config, "network_config", None)
    if network and value(network.type) == "iso" and not list(Path("/var/lib/iwd").glob("*.psk")):
        raise InstallError("Archinstall can copy iwd ISO connections only. This installer uses NetworkManager; select NetworkManager for the target and use nmtui after boot.")
    return target


def validate_mountpoint(config, mountpoint: Path) -> None:
    for path in (mountpoint, getattr(config.disk_config, "mountpoint", None)):
        if path is not None and Path(path) != Path("/mnt"):
            raise InstallError("The installation mountpoint must be /mnt")
    target = Path("/mnt")
    if target.is_symlink() or target.resolve() != target:
        raise InstallError("The installation mountpoint must not be a symlink")


def preparse_options(argv: list[str]) -> dict:
    """Read development profile selections before native argument parsing."""
    flags = {"--help", "-h", "--dry-run", "--debug", "--advanced", "--verbose"}
    valued = {"--config", "--creds", "--mountpoint"}
    options = {}
    iterator = iter(argv)
    for argument in iterator:
        key, separator, supplied = argument.partition("=")
        if key in flags and not separator:
            options[key] = True
        elif key in valued:
            operand = supplied if separator else next(iterator, None)
            if operand is None or operand.startswith("--"):
                raise InstallError(f"Missing value for {key}")
            options[key] = operand
        else:
            raise InstallError(f"Unsupported installer option: {key}")
    if options.get("--mountpoint", "/mnt") != "/mnt":
        raise InstallError("The installation mountpoint must be /mnt")
    preferences = {"development_profiles": ["python", "cpp", "ai", "containers"]}
    for key in ("--config", "--creds"):
        if key not in options:
            continue
        data = json.loads(Path(options[key]).read_text())
        if not isinstance(data, dict):
            raise InstallError("Installer configuration must be a JSON object")
        profile = data.get("profile_config") or {}
        if (profile.get("profile") or {}).get("path"):
            raise InstallError("External executable profile paths are not accepted")
        if "h7607a" in data:
            preferences = data["h7607a"]
    profiles = preferences.get("development_profiles")
    if not isinstance(profiles, list) or any(p not in {"python", "cpp", "ai", "containers", "cuda"} for p in profiles):
        raise InstallError("Unknown P16 development profile")
    return {"development_profiles": list(dict.fromkeys(profiles))}


def native_boot_parameters(installation, root, source: str, root_uuid: str) -> list[str]:
    # Archinstall 4.5 returns combined cryptdevice+root strings in its LVM branch.
    parameters = [part for item in installation._get_kernel_params(root, partuuid=False)
                  for part in shlex.split(item)]
    roots = [p for p in parameters if p.startswith("root=")]
    if len(roots) != 1:
        raise InstallError("Native storage configuration did not produce one root parameter")
    parameter = roots[0]
    if parameter.startswith("root=/dev/"):
        actual = canonical_device(source)
        encryption = getattr(installation, "_disk_encryption", None)
        if encryption and root in encryption.partitions:
            # Native partition mapper_name is 'root'; future boot names still
            # come from cryptdevice rather than an arbitrary live dm-N alias.
            mapper_name = getattr(root, "mapper_name", None)
            mapper = Path("/dev/mapper") / mapper_name if mapper_name else None
            crypt = [p for p in parameters if p.startswith("cryptdevice=")]
            if (mapper is None or canonical_device(mapper) != actual
                    or parameter != "root=/dev/mapper/root"
                    or crypt != [f"cryptdevice=UUID={root.uuid}:root"]):
                raise InstallError("Encrypted root does not match the native live and boot mappings")
            raw_uuid = command(["blkid", "-s", "UUID", "-o", "value", str(root.dev_path)]).strip()
            if raw_uuid != root.uuid:
                raise InstallError("Encrypted root UUID differs from its selected backing partition")
            return parameters
        if canonical_device(parameter[5:]) != actual:
            raise InstallError("Native root mapping differs from the mounted target filesystem")
        if not source.startswith("/dev/mapper/"):
            mapper_file = Path("/sys/class/block") / Path(actual).name / "dm/name"
            name = mapper_file.read_text().strip()
            if not re.fullmatch(r"[A-Za-z0-9_+.-]+", name):
                raise InstallError("Invalid device-mapper name")
            source = "/dev/mapper/" + name
        parameters[parameters.index(parameter)] = "root=" + source
    elif parameter != "root=UUID=" + root_uuid:
        raise InstallError("Native root UUID differs from the mounted target filesystem")
    return parameters


def validate_existing_lvm(config, graph: BlockGraph, target: str) -> None:
    """A colliding VG name must never bring an unselected USB PV into scope."""
    lvm = config.disk_config.lvm_config
    if not lvm:
        return
    names = {g.name for g in lvm.vol_groups}
    report = json.loads(command(["pvs", "--reportformat", "json", "-o", "pv_name,vg_name"]))
    for section in report.get("report", []):
        for pv in section.get("pv", []):
            if pv.get("vg_name", "").strip() in names:
                graph.require_target(pv["pv_name"].strip(), target)


MIRROR_PROBES = ("https://de3.mirror.archlinuxarm.org/aarch64/core/core.db",
                 "https://fl.us.mirror.archlinuxarm.org/aarch64/core/core.db")


def verify_online() -> bool:
    """True once one of the ALARM mirrors the locks use answers over HTTPS."""
    certificate = None
    for url in MIRROR_PROBES:
        try:
            with urlopen(url, timeout=12) as response:
                if response.status == 200 and response.read(16):
                    return True
        except Exception as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, ssl.SSLCertVerificationError):
                certificate = reason
    if certificate is not None:
        clock = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        detail = getattr(certificate, "verify_message", None) or certificate
        raise InstallError(f"The ALARM mirror certificates did not verify ({detail}). The system clock is "
                           f"probably wrong: it reads {clock}. Wait for network time synchronization or set "
                           "the clock, then restart archinstall. Disks were not changed.")
    return False


def connect_network() -> None:
    if verify_online():
        return
    print("Connect to Wi-Fi or Ethernet in NetworkManager, then quit its menu to continue.")
    subprocess.run(["systemctl", "start", "NetworkManager.service"], check=True)
    subprocess.run(["nmtui-connect"], check=True)
    if not verify_online():
        raise InstallError("The ARM package mirrors are not reachable. Run nmtui, fix the connection, and restart archinstall; disks were not changed.")


def synchronize_clock() -> None:
    """New local package signatures require a real, current installation clock."""
    subprocess.run(["timedatectl", "set-ntp", "true"], check=True)
    deadline = time.monotonic() + 90
    print("Synchronizing the clock for package signatures…", flush=True)
    while time.monotonic() < deadline:
        if command(["timedatectl", "show", "--property=NTPSynchronized", "--value"]).strip() == "yes":
            return
        time.sleep(2)
    raise InstallError("Clock synchronization did not finish within 90 seconds; check the network/time service and restart. Disks were not changed.")


def prepare_wheelhouse(repo: Path, graph: BlockGraph, directory: Path) -> dict:
    from h7607a.carrier import extract_torch
    source = command(["findmnt", "-n", "-o", "SOURCE", "--target", "/"]).strip().split("[", 1)[0]
    parents = graph.physical_parents(source)
    if len(parents) != 1:
        raise InstallError("Cannot uniquely identify the installer USB for the offline wheel carrier")
    parent = Path(parents.pop())
    if graph.nodes[str(parent)].get("tran") != "usb":
        raise InstallError("Offline wheels must come from the protected installer USB")
    info = parent.stat()
    identity = {"major": os.major(info.st_rdev), "minor": os.minor(info.st_rdev),
                "size": int(graph.nodes[str(parent)]["size"]),
                "root_uuid": command(["blkid", "-s", "UUID", "-o", "value", source]).strip()}
    result = extract_torch(parent, directory, repo, expected_parent_identity=identity)
    if not result.get("production_usb_verified"):
        raise InstallError("Offline wheel extraction did not verify the actual installer USB")
    return result


def stage_wheelhouse(repo: Path, graph: BlockGraph, directory: Path) -> Path | None:
    """Copy the carrier's PyTorch wheels, or return None to install them online in the target."""
    from h7607a.carrier import CarrierError

    try:
        return Path(prepare_wheelhouse(repo, graph, directory)["wheelhouse"])
    except (InstallError, CarrierError, OSError) as error:
        print(f"This installer USB has no usable PyTorch wheel carrier ({error}).\n"
              "The PyTorch environment will be downloaded inside the new system instead, about 3.2 GB.",
              flush=True)
        return None


def _keyring_fingerprints(keyring: Path) -> set[str]:
    """Fingerprints in an OpenPGP keyring file; empty when gpg cannot read it."""
    listing = subprocess.run(["gpg", "--batch", "--with-colons", "--show-keys", str(keyring)],
                             capture_output=True, text=True).stdout
    return {line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")}


def finish_target_repository(target: Path, repo: Path, lock: dict) -> None:
    """The installer-only file:// repository must not break normal pacman -Syu."""
    from h7607a.hardware import _run

    package_lock = json.loads((repo / "locks/packages.json").read_text())
    key = package_lock["signing_keys"]["local"]
    public = repo / key["path"]
    destination = target / "usr/local/share/h7607a/package-signing.gpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copyfile(public, destination)
    # pacstrap verifies the local packages with the live keyring. The target keeps
    # the repository key only when a keyring it ships lists it; owners who install
    # their own builds add package-signing.gpg themselves.
    for arguments in (["--init"], ["--populate", "archlinuxarm"]):
        _run(target, ["pacman-key", *arguments], timeout=180, api_filesystems=True)
    path = target / "etc/pacman.conf"
    text = path.read_text()
    held = set(lock["hardware_packages"])
    for row in re.findall(r"^\s*IgnorePkg\s*=\s*(.*)$", text, re.M):
        held.update(row.split("#", 1)[0].split())
    output, omit = [], False
    for line in text.splitlines():
        section = re.match(r"\s*\[([^]]+)\]", line)
        if section:
            omit = section[1] == package_lock["repository"]
        if omit or re.match(r"\s*IgnorePkg\s*=", line):
            continue
        if "H7607A VERIFIED LOCAL REPOSITORY" in line:
            continue
        output.append(line)
        if section and section[1] == "options":
            output.append("IgnorePkg = " + " ".join(sorted(held)))
    path.write_text("\n".join(output) + "\n")
    shipped = any(key["fingerprint"] in _keyring_fingerprints(keyring)
                  for keyring in sorted((target / "usr/share/pacman/keyrings").glob("*.gpg")))
    if not shipped:
        try:
            _run(target, ["pacman-key", "--list-keys", key["fingerprint"]], timeout=180, api_filesystems=True)
        except subprocess.CalledProcessError:
            pass  # pacman-key --delete exits 1 for an absent key.
        else:
            _run(target, ["pacman-key", "--delete", key["fingerprint"]], timeout=180, api_filesystems=True)
    (destination.parent / "installed-hardware-packages.json").write_text(json.dumps(lock["hardware_packages"], indent=2) + "\n")


DEFERRED_COMMANDS = {"ai": "h7607a-dev install-torch --online", "cuda": "h7607a-cuda install-sdk --online"}


@contextlib.contextmanager
def read_only_bind(source: Path, parent: Path):
    parent.mkdir(parents=True, exist_ok=True)
    mountpoint = Path(tempfile.mkdtemp(prefix="h7607a-carrier-", dir=parent))
    try:
        subprocess.run(["mount", "--bind", str(source), str(mountpoint)], check=True)
    except BaseException:
        mountpoint.rmdir()
        raise
    try:
        subprocess.run(["mount", "-o", "remount,bind,ro", str(mountpoint)], check=True)
        yield mountpoint
    finally:
        if subprocess.run(["umount", str(mountpoint)]).returncode == 0:
            mountpoint.rmdir()
        else:
            print(f"Warning: {mountpoint} is still mounted until the next reboot.", flush=True)


def install_development(target: Path, repo: Path, profiles, usernames, wheelhouse: Path | None = None) -> list[dict]:
    """Install the selected development profiles without failing the installation.

    Returns one entry per failed profile and user. An entry's command, when
    set, completes that profile after the first boot.
    """
    from h7607a.development import install_selected

    failures = []

    def report(names, username, error):
        command = DEFERRED_COMMANDS.get(names[0]) if len(names) == 1 else None
        failures.append({"profiles": names, "user": username, "command": command})
        print(f"\nDevelopment tools ({', '.join(names)}) did not install for {username}: {error}", flush=True)
        print(f"After the first boot, run as {username}: {command}" if command
              else "Their packages are installed, but their setup did not finish.", flush=True)

    def attempt(names, username, **options):
        try:
            install_selected(target, username, names, repo, **options)
        except Exception as error:
            report(names, username, error)

    base = [name for name in profiles if name not in DEFERRED_COMMANDS]
    if base:
        for username in usernames:
            attempt(base, username)
    if "ai" in profiles:
        if wheelhouse is None:
            for username in usernames:
                attempt(["ai"], username, online=True)
        else:
            try:
                # Bind the RAM wheelhouse instead of copying 3 GB. arch-chroot
                # mounts its own /run, so the bind lives below target/var/cache.
                with read_only_bind(wheelhouse, Path(target) / "var/cache") as mountpoint:
                    for username in usernames:
                        attempt(["ai"], username, wheelhouse=mountpoint)
            except (OSError, subprocess.SubprocessError) as error:
                for username in usernames:
                    report(["ai"], username, error)
    if "cuda" in profiles:
        for username in usernames:
            attempt(["cuda"], username, online=True)
    return failures


SPEAKER_TUNING_CACHE = Path("/var/cache/h7607a")


def install_speaker_tuning(target: Path, repo: Path, cache: Path = SPEAKER_TUNING_CACHE) -> dict | None:
    """Install the Cirrus amplifier tuning extracted from the pinned ASUS driver package.

    The files may not be redistributed, so every installation downloads the
    package itself. Returns None after a warning when that fails.
    """
    from h7607a import artifacts, hardware

    rows = [row for row in hardware.load_hardware_lock(repo)["artifacts"] if "extract" in row]
    print("Downloading the Cirrus speaker tuning from the ASUS driver package", flush=True)
    try:
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        extracted = [(row, artifacts.fetch(cache, row).read_bytes()) for row in rows]
    except (artifacts.ArtifactError, OSError, subprocess.SubprocessError) as error:
        spec = rows[0]["extract"]
        package = (spec["from"].get("urls") or [PurePosixPath(spec["from"]["path"]).name])[0]
        names = ", ".join(PurePosixPath(row["target"]).name for row in rows)
        print(f"\nWarning: the speaker tuning was not installed: {error}\n"
              "The internal speakers then run without the ASUS tuning. To add it later, extract "
              f"{names} from the 7-Zip archive at byte offset {spec['offset']} of {package} "
              f"(member paths in {repo / 'locks/hardware.json'} on the installer USB) into "
              "/usr/lib/firmware/cirrus/ and run 'mkinitcpio -P'.", flush=True)
        return None
    root = hardware._root(target)
    for row, data in extracted:
        path = hardware._under(root, row["target"])
        if path.exists() and (not path.is_file() or path.read_bytes() != data):
            raise InstallError("Refusing to overwrite a different existing file: /" + row["target"])
    installed = {}
    for row, data in extracted:
        path = hardware._write(root, row["target"], data, 0o644)
        os.chown(path, 0, 0)
        installed["/" + row["target"]] = hashlib.sha256(data).hexdigest()
    return installed


def configure_manual_dns(installation, config) -> None:
    """Complete native manual networking's missing libc resolver link.

    Pinned Archinstall enables networkd/resolved in MANUAL mode, but only its
    ISO/IWD branches create resolv.conf. NetworkManager manages its own DNS.
    """
    network = getattr(config, "network_config", None)
    if network is None or value(network.type) != "manual":
        return
    installation.systemd_resolved_stub_mode()
    target = Path(installation.target)
    resolver = target / "etc/resolv.conf"
    if not resolver.is_symlink() or os.readlink(resolver) != "/run/systemd/resolve/stub-resolv.conf":
        raise InstallError("Manual networking did not configure the systemd-resolved resolver link")
    unit = "/usr/lib/systemd/system/systemd-resolved.service"
    enabled = target / "etc/systemd/system/sysinit.target.wants/systemd-resolved.service"
    if (not enabled.is_symlink() or os.readlink(enabled) != unit
            or not (target / unit.lstrip("/")).is_file()):
        raise InstallError("Manual networking requires the enabled systemd-resolved service")


def persist_selected_swap(installation, config) -> None:
    """Repair native genfstab's target-prefix filter omitting block swaps.

    Read only explicitly selected partitions/LVs, never global live swaps.
    Directly encrypted swap is rejected before formatting; root-VG LVs use
    their inner swap UUID after the root's normal LUKS unlock/LVM activation.
    """
    disk = getattr(config, "disk_config", None)
    if disk is None:
        return
    items = [part for modification in disk.device_modifications for part in modification.partitions]
    if disk.lvm_config:
        items += [volume for group in disk.lvm_config.vol_groups for volume in group.volumes]
    selected = [item for item in items if value(item.status) != "delete" and value(item.fs_type) == "linux-swap"]
    if not selected:
        return
    target = Path(installation.target)
    fstab = target / "etc/fstab"
    if not fstab.is_file() or fstab.is_symlink() or not fstab.resolve().is_relative_to(target.resolve()):
        raise InstallError("Selected swap requires the regular native target fstab")
    text = fstab.read_text()
    rows = [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    additions = []
    seen = set()
    for item in selected:
        path = canonical_device(item.dev_path)
        if not Path(path).is_block_device() or command(["blkid", "-s", "TYPE", "-o", "value", path]).strip() != "swap":
            raise InstallError("Selected swap is not a formatted swap block device: " + path)
        uuid = command(["blkid", "-s", "UUID", "-o", "value", path]).strip()
        if (not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid)
                or (getattr(item, "uuid", None) and item.uuid != uuid)):
            raise InstallError("Selected swap UUID differs from the native formatted device: " + path)
        if uuid in seen:
            raise InstallError("Selected swap appears more than once")
        seen.add(uuid)
        source = "UUID=" + uuid
        matches = [row for row in rows if row[0] in {source, path, str(item.dev_path)}]
        if matches:
            if len(matches) != 1 or len(matches[0]) != 6 or matches[0][:3] != [source, "none", "swap"]:
                raise InstallError("Conflicting selected swap entry in target fstab: " + path)
        else:
            additions.append(source + " none swap defaults 0 0\n")
    if additions:
        fstab.write_text(text.rstrip("\n") + "\n\n# Explicitly selected swap devices\n" + "".join(additions))


def make_native_components(repo: Path, lock: dict):
    """Import the native Archinstall API and register platform integration."""
    from archinstall.lib.global_menu import GlobalMenu
    from archinstall.lib.mirror.mirror_handler import MirrorListHandler
    from archinstall.lib.mirror.mirror_menu import MirrorMenu
    from archinstall.lib.models.mirrors import MirrorStatusEntryV3
    from archinstall.lib.models.bootloader import Bootloader, BootloaderConfiguration
    from archinstall.lib.profile.profiles_handler import profile_handler
    from archinstall.lib.plugins import plugins
    from archinstall.tui.components import tui
    from archinstall.lib.menu.helpers import Selection
    from archinstall.tui.menu_item import MenuItem, MenuItemGroup
    from archinstall.tui.result import ResultType
    from archinstall.lib.installer import Installer
    from archinstall.lib.command import SysCommand
    from archinstall.scripts import guided
    from h7607a import hardware, offline_locale, policy, target_execution

    class P16Installer(Installer):
        """Configure an offline target without booting it for localectl."""
        def run_command(self, cmd, peek_output=False):
            return target_execution.run_native(self.target, cmd, runner=SysCommand,
                                               peek_output=peek_output)

        def _chroot_argv(self, *args):
            return target_execution.chroot_argv(self.target, *args)

        def set_locale(self, locale_config):
            return offline_locale.set_locale(self, locale_config)

        def set_keyboard_language(self, language):
            return offline_locale.set_keyboard_language(self, language)

        def set_user_password(self, user):
            # Upstream callers ignore False; never report a usable installation
            # after silently failing to set its login password.
            if not super().set_user_password(user):
                raise InstallError("Failed to set password for " + user.username)
            return True

    class AlarmMirror(MirrorStatusEntryV3):
        @property
        def server_url(self):
            return self.url

    class AlarmMirrors(MirrorListHandler):
        def load_remote_mirrors(self):
            return False

        def _parse_local_mirrors(self, text):
            rows = super()._parse_local_mirrors(text)
            return {name: [AlarmMirror(**m.model_dump()) for m in values] for name, values in rows.items()}

    class AlarmMirrorMenu(MirrorMenu):
        def _define_menu_options(self):
            # Keep native ARM region/server editing. Repository selection is
            # fixed by the signed ALARM/P16 package configuration.
            return [item for item in super()._define_menu_options()
                    if item.key in {"mirror_regions", "custom_servers"}]

    # Every Archinstall profile uses the exact P16 GPU tuple, never DKMS alternatives.
    for profile in profile_handler.profiles:
        profile._support_gfx_driver = False

    class P16Menu(GlobalMenu):
        async def _mirror_configuration(self, preset=None):
            if self._mirror_list_handler is None:
                self._mirror_list_handler = AlarmMirrors()
            try:
                validate_mirror_config(preset)
                selected = await AlarmMirrorMenu(self._mirror_list_handler, preset=preset).run()
                validate_mirror_config(selected)
                return selected
            except InstallError as error:
                # Recover inside the menu without modifying live pacman.conf
                # or losing the user's other installation settings.
                await Selection(MenuItemGroup([MenuItem("Use configured ARM mirrors", value=True)]),
                                header=str(error) + "\nMirror choices will reset; other settings are preserved.",
                                allow_skip=False).show()
                return None

        async def _development(self, preset):
            names = {"python": "Python tooling", "cpp": "C/C++ build tools", "ai": "PyTorch CUDA environment",
                     "containers": "GPU containers (Podman)", "cuda": "Full CUDA SDK (large download)"}
            group = MenuItemGroup([MenuItem(label, value=name) for name, label in names.items()])
            group.set_selected_by_value(preset)
            result = await Selection(group, header="Development tools", multi=True, allow_skip=True,
                                     allow_reset=True).show()
            if result.type_ == ResultType.Reset:
                return []
            if result.type_ == ResultType.Selection:
                return result.get_values()
            return preset

        def _get_menu_options(self):
            items = super()._get_menu_options()
            items.insert(0, MenuItem("Development tools", key="development_profiles",
                         value=["python", "cpp", "ai", "containers"], action=self._development,
                         preview_action=lambda item: ", ".join(item.value or [])))
            for item in items:
                if item.key == "kernels":
                    item.value = [lock["kernel_package"]]
                    item.action = None
                    item.read_only = True
                elif item.key == "bootloader_config":
                    item.value = BootloaderConfiguration(Bootloader.Systemd)
                    item.action = None
                    item.read_only = True
            return items

    class HardwarePlugin:
        def __init__(self):
            self.initrd_active = False
            self.boot_parameters = None
            self.root_uuid = None
            self.configuration = None
            self.wheelhouse = None

        def on_pacstrap(self, packages):
            selected = []
            for package in packages:
                if package == "linux-firmware":
                    selected.extend(lock["firmware_packages"])
                else:
                    selected.append("power-profiles-daemon-n1x" if package == "power-profiles-daemon" else package)
            packages = list(dict.fromkeys(selected))
            blocked = set(packages) & set(lock["incompatible_packages"])
            if blocked:
                raise InstallError(f"Packages would replace the N1X kernel or NVIDIA driver: {sorted(blocked)}")
            if "base" in packages:
                return list(dict.fromkeys(packages + list(lock["hardware_packages"]) + ["archlinuxarm-keyring"]))
            return packages

        def on_mirrors(self, config):
            validate_mirror_config(config)
            return config

        def on_mkinitcpio(self, installation):
            if self.initrd_active:
                return False
            # Before mkinitcpio, so the initramfs hook can include the tuning.
            install_speaker_tuning(installation.target, repo)
            policy.configure_system_policy(installation.target, repo_root=repo)
            hardware.install_hardware(installation.target, repo_root=repo)
            hook = lock["initcpio_hook"]
            if hook not in installation._hooks:
                installation._hooks.insert(0, hook)
            for module in ("hid_asus", "nvidia", "nvidia_modeset", "nvidia_uvm", "nvidia_drm"):
                installation.append_mod(module)
            self.initrd_active = True
            try:
                if not installation.mkinitcpio(["-P"]):
                    raise InstallError("Target initramfs generation failed")
            finally:
                self.initrd_active = False
            return True

        def on_add_bootloader(self, installation):
            root = installation._get_root()
            if root is None:
                raise InstallError("Cannot identify installed root filesystem")
            source = command(["findmnt", "-n", "-o", "SOURCE", "--target", str(installation.target)]).strip().split("[", 1)[0]
            self.root_uuid = command(["blkid", "-s", "UUID", "-o", "value", source]).strip()
            if not re.fullmatch(r"[A-Za-z0-9-]+", self.root_uuid):
                raise InstallError("Installed root UUID is missing or malformed")
            self.boot_parameters = native_boot_parameters(installation, root, source, self.root_uuid)
            hardware.write_boot_files(installation.target, installation.target / "boot", self.boot_parameters,
                                      root_uuid=self.root_uuid, repo_root=repo)
            installation.arch_chroot("bootctl --esp-path=/boot install", peek_output=True)
            installation._helper_flags["bootloader"] = True
            return True

        def on_genfstab(self, installation):
            if not self.boot_parameters:
                raise InstallError("The P16 boot chain was not installed")
            config = self.configuration
            configure_manual_dns(installation, config)
            persist_selected_swap(installation, config)
            # Refresh boot files after package-triggered initramfs regeneration.
            hardware.write_boot_files(installation.target, installation.target / "boot", self.boot_parameters,
                                      root_uuid=self.root_uuid, repo_root=repo)
            # Holds and repository cleanup come first: a failed optional
            # profile then still leaves a system that updates safely.
            finish_target_repository(installation.target, repo, lock)
            users = config.auth_config.users if config.auth_config else []
            failures = install_development(installation.target, repo, config.development_profiles,
                                           [user.username for user in users], self.wheelhouse)
            if failures:
                try:
                    input("\nSome development tools did not install; see the messages above. Press Enter to continue.")
                except EOFError:
                    pass
            return False

    plugins["h7607a"] = HardwarePlugin()
    guided.Installer = P16Installer
    return P16Menu, AlarmMirrors


def apply_native_filesystems(disk_config, filesystem_handler, device_handler) -> None:
    """Retire native DELETE rows after their partition edit, before path reuse.

    Archinstall 4.5 keeps a deleted partition's old device path and mount data:
    its post-commit wipefs loop can touch that path, and subsequent root/mount
    selection can still choose the deleted row. Keep the original native edit,
    then clear only successfully handled DELETE rows. Restore the singleton's
    original method even if partitioning, formatting or an interrupt fails.
    """
    original = device_handler._setup_partition
    missing = object()
    previous = vars(device_handler).get("_setup_partition", missing)

    def setup(part_mod, *args, **kwargs):
        result = original(part_mod, *args, **kwargs)
        if part_mod.is_delete():
            part_mod.dev_path = None
            part_mod.mountpoint = None
            part_mod.btrfs_subvols = []
        return result

    device_handler._setup_partition = setup
    try:
        filesystem_handler(disk_config).perform_filesystem_operations()
    finally:
        if previous is missing:
            del device_handler._setup_partition
        else:
            device_handler._setup_partition = previous
    for modification in disk_config.device_modifications:
        modification.partitions = [part for part in modification.partitions if not part.is_delete()]


def guided_main(repo: Path, lock: dict, handler, components, preferences) -> None:
    from archinstall.lib.authentication.authentication_handler import AuthenticationHandler
    from archinstall.lib.applications.application_handler import ApplicationHandler
    from archinstall.lib.configuration import confirm_config
    from archinstall.lib.disk.filesystem import FilesystemHandler
    from archinstall.lib.disk.device_handler import device_handler
    from archinstall.lib.menu.util import delayed_warning
    from archinstall.lib.models.network import NetworkConfiguration, NicType
    from archinstall.lib.models.bootloader import Bootloader, BootloaderConfiguration
    from archinstall.scripts.guided import perform_installation
    from archinstall.tui.components import tui
    from archinstall.lib.plugins import plugins
    from h7607a.development import package_list

    Menu, Mirrors = components
    mirrors = Mirrors()
    config = handler.config
    config.development_profiles = preferences["development_profiles"]
    original_safe_config = config.safe_config
    config.safe_config = lambda: {**original_safe_config(), "h7607a": {"development_profiles": config.development_profiles}}
    if handler.args.silent:
        raise InstallError("This launcher requires the normal interactive installation confirmation")
    if not handler.args.config:
        config.kernels = [lock["kernel_package"]]
        config.bootloader_config = BootloaderConfiguration(Bootloader.Systemd)
        config.network_config = NetworkConfiguration(type=NicType.NM)
    plugin = plugins["h7607a"]
    plugin.configuration = config
    while True:
        result = tui.run(Menu(config, mirrors, advanced=getattr(handler.args, "advanced", False),
                              title="Arch Linux ARM · ASUS ProArt P16"))
        if result is None:
            return
        config.write_debug()
        config.save()
        if handler.args.dry_run:
            return
        if not tui.run(lambda: confirm_config(config)):
            continue
        if not delayed_warning("Starting device modifications in "):
            continue
        # Resolve the selected physical disk immediately before filesystem operations.
        graph = BlockGraph.read()
        validate_mountpoint(config, handler.args.mountpoint)
        target = validate_configuration(config, graph, lock)
        validate_existing_lvm(config, graph, target)
        config.packages = list(dict.fromkeys(config.packages + package_list(config.development_profiles)))
        with tempfile.TemporaryDirectory(prefix="h7607a-wheels-", dir="/run") as staged:
            if "ai" in config.development_profiles:
                plugin.wheelhouse = stage_wheelhouse(repo, graph, Path(staged))
            apply_native_filesystems(config.disk_config, FilesystemHandler, device_handler)
            perform_installation(handler, mirrors, AuthenticationHandler(), ApplicationHandler())
        return


def main(repo: Path) -> int:
    lock = load_lock(repo)
    if os.geteuid() != 0:
        raise InstallError("Run archinstall as root")
    verify_archinstall(lock)
    verify_device(repo)
    preferences = preparse_options(sys.argv[1:])
    if "--help" in sys.argv or "-h" in sys.argv:
        from archinstall.main import main as native_main
        return native_main()
    connect_network()
    synchronize_clock()
    from h7607a.packages import configure_live
    configure_live(repo / lock["local_repository_path"], source_root=repo)
    components = make_native_components(repo, lock)
    # Use ALARM repositories and the live NetworkManager connection.
    sys.argv += ["--script", "h7607a", "--skip-wkd", "--skip-wifi-check", "--skip-version-check"]
    module = ModuleType("archinstall.scripts.h7607a")
    module.main = lambda handler: guided_main(repo, lock, handler, components, preferences)
    sys.modules[module.__name__] = module
    from archinstall.main import main as native_main
    return native_main()
