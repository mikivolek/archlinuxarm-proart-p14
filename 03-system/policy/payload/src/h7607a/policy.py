"""Apply device policy files to a mounted live or installed root."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess

SYSTEM_POLICY_FILES = (
    "etc/systemd/logind.conf.d/60-h7607a-sleep-hold.conf",
    "etc/udev/rules.d/72-h7607a-no-microphone.rules",
    "etc/systemd/system/power-profiles-daemon.service",
)
SYSTEM_POLICY_SERVICES = ("n1x-cppc-init.service", "power-profiles-daemon.service")


def _target_path(root, relative):
    name = PurePosixPath(relative)
    if name.is_absolute() or any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError("Target path must be relative without traversal")
    current = root
    for part in name.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Linked target path refused: " + relative)
    return current


def _target(target):
    target = Path(target)
    if not target.is_absolute() or target == Path("/") or target.resolve(strict=True) != target:
        raise ValueError("An existing canonical installation target is required")
    if os.geteuid() != 0:
        raise PermissionError("Target configuration requires installer root privileges")
    return target


def _overlay(repo_root):
    return (Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]) / "runtime"


def _plan(target, overlay, names):
    """Resolve every copy before writing, so a conflict leaves the target untouched."""
    planned = [(_target_path(overlay, name), _target_path(target, name)) for name in names]
    for source, destination in planned:
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError("Missing production file: " + str(source))
        if destination.exists() and (not destination.is_file() or destination.read_bytes() != source.read_bytes()):
            raise ValueError("Refusing to overwrite an existing different file: " + str(destination))
    return planned


def _copy(source, destination, mode):
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation avoids silently replacing administrator content.
        with destination.open("xb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
    os.chmod(destination, mode)
    os.chown(destination, 0, 0)
    return {"bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "mode": oct(mode)}


def configure_system_policy(target: Path, repo_root: Path | None = None, *, run=subprocess.run) -> dict:
    """Apply hardware policy independently of the installation profile or user.

    Keep manual sleep and the power button available. Restrict only capture PCM
    nodes, retaining shared playback codecs. The PPD unit changes two ordering/
    install lines: its vendor After=multi-user.target would cycle if simply
    enabled in that target. Dependency resets do not work in unit drop-ins.
    """
    target = _target(target)
    overlay = _overlay(repo_root)
    planned = _plan(target, overlay, SYSTEM_POLICY_FILES)
    vendor = _target_path(target, "usr/lib/systemd/system/power-profiles-daemon.service")
    adapted = (overlay / SYSTEM_POLICY_FILES[2]).read_text()
    expected_vendor = adapted.replace("After=n1x-cppc-init.service\n",
                                      "After=multi-user.target display-manager.target\n", 1)
    expected_vendor = expected_vendor.replace("WantedBy=multi-user.target\n", "WantedBy=graphical.target\n", 1)
    if not vendor.is_file() or vendor.read_text() != expected_vendor:
        raise ValueError("Packaged PPD unit differs from the expected two-line adaptation")
    for service in SYSTEM_POLICY_SERVICES:
        source = _target_path(target, "usr/lib/systemd/system/" + service)
        if not source.is_file() or not source.stat().st_size:
            raise ValueError("Missing packaged hardware service: " + service)
    files = {str(destination.relative_to(target)): _copy(source, destination, 0o644)
             for source, destination in planned}
    # The immutable live seed may retain links to the vendor unit and its old
    # graphical target. Reenable replaces those links using the adapted unit.
    run(["systemctl", "--root=" + str(target), "reenable", *SYSTEM_POLICY_SERVICES], check=True, timeout=30)
    # Both services must be reachable under a text-mode default target.
    for service in SYSTEM_POLICY_SERVICES:
        parent = _target_path(target, "etc/systemd/system/multi-user.target.wants")
        link = parent / service
        expected = ("/etc/systemd/system/" if service == "power-profiles-daemon.service"
                    else "/usr/lib/systemd/system/") + service
        if not link.is_symlink() or os.readlink(link) != expected:
            raise ValueError("Hardware service is not enabled for multi-user.target: " + service)
    return {"schema": "h7607a-system-policy-v1", "files": files,
            "system_services_enabled": list(SYSTEM_POLICY_SERVICES),
            "automatic_sleep": "held pending physical qualification", "manual_sleep_blocked": False,
            "power_button_policy_changed": False, "capture_pcm_access": "root only",
            "playback_pcm_policy_changed": False}
