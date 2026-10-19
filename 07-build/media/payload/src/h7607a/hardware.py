"""H7607BA module indexing and systemd-boot/EDK2 configuration."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[2]
KERNEL_RELEASE = '7.0.0-1022-nvidia'
KERNEL_IMAGE = 'vmlinuz-linux-n1x'
INITRAMFS_IMAGE = 'initramfs-linux-n1x.img'
INITCPIO_HOOK = 'h7607a-preload'
INITCPIO_MODULES = ('hid_asus', 'nvme', 'xhci_pci', 'xhci_plat_hcd', 'xhci_mtk_hcd', 'usb_storage', 'uas',
                   'i2c_mt65xx', 'i2c_hid_acpi', 'usbhid', 'hid_generic', 'hid_multitouch',
                   'nvidia', 'nvidia_modeset', 'nvidia_uvm', 'nvidia_drm')
PACKAGES = {
    'linux-n1x-nvidia-bin': '7.0.0.1022.23-3',
    'nvidia-utils': '615.78.08-1',
    'nvidia-n1x-userspace-fix': '615.78.08-1',
    'nvidia-n1x-modules': '615.78.08-4',
    'h7607a-hardware': '1.0.0-4',
    'linux-firmware-mediatek': '20260916-1',
    'linux-firmware-cirrus': '20260916-1',
    'linux-firmware-whence': '20260916-1',
}
KERNEL_OPTIONS = (
    'rw', 'rootwait', 'console=tty0', 'acpi=nospcr', 'thermal.tzp=100',
    'mem_sleep_default=s2idle', 'initcall_blacklist=simpledrm_platform_driver_init',
    'fbcon=font:TER16x32', 'log_buf_len=4M', 'loglevel=3',
)
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
ARGUMENT = re.compile(r'[A-Za-z0-9_.,=:/@+\-]+')


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _root(path):
    path = Path(path).absolute()
    require(path != Path('/') and path.is_dir() and path.resolve() == path,
            'A canonical mounted target directory other than / is required')
    return path


def _under(root, relative):
    relative = Path(relative)
    require(not relative.is_absolute() and '..' not in relative.parts,
            'Relative target path required')
    path = root / relative
    require(path.resolve().is_relative_to(root) and not path.is_symlink(),
            'Target path escapes its root or is a symlink: ' + str(relative))
    return path


def _run(target, argv, *, timeout=180, api_filesystems=False, bind_dns=False, capture=True):
    """Run target tools without remounting the caller's API filesystems.

    Package/module metadata needs only the target's own executable, libraries
    and files. lsinitcpio additionally needs /dev and /proc; confine its
    arch-chroot setup/teardown to a private mount and PID namespace. These
    checks need no resolver bind or terminal input. Networked callers can
    request a resolver bind inside the private mount namespace. Kill the
    owned process group on timeout so descendants cannot retain pipes or
    continue target writes.
    """
    target = _root(target)
    require(not bind_dns or api_filesystems, 'A resolver bind requires a private mount namespace')
    prefix = (['unshare', '--mount', '--propagation', 'private', '--fork', '--pid',
               '--kill-child=KILL', '--', 'arch-chroot', *([] if bind_dns else ['-r'])]
              if api_filesystems else ['chroot'])
    command = [*prefix, str(target), *map(str, argv)]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE if capture else None,
                             stderr=subprocess.PIPE if capture else None,
                             text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired as cleanup:
            # Uninterruptible kernel I/O cannot be repaired by a timeout.
            # Do not hide it behind an unlimited wait in Popen.__exit__.
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()
            raise RuntimeError('Target command did not exit after SIGKILL; inspect kernel/storage errors: '
                               + repr(command)) from cleanup
        if isinstance(error, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from error
        raise
    if child.returncode:
        raise subprocess.CalledProcessError(child.returncode, command, output=stdout, stderr=stderr)
    return stdout or ''


def _write(root, relative, data, mode=0o644):
    path = _under(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def load_hardware_lock(repo_root=None):
    return json.loads((Path(repo_root or REPO) / 'locks/hardware.json').read_text())


def install_hardware(target, repo_root=None):
    """Regenerate module dependency indices after pacman installs platform packages."""
    target = _root(target)
    _run(target, ['depmod', '-a', KERNEL_RELEASE])
    return {'kernel_release': KERNEL_RELEASE,
            'mkinitcpio_hook': INITCPIO_HOOK, 'mkinitcpio_modules': list(INITCPIO_MODULES)}


def kernel_parameters(parameters, root_uuid, *, installer=False):
    require(isinstance(root_uuid, str) and UUID.fullmatch(root_uuid), 'Actual root filesystem UUID required')
    require(isinstance(parameters, (list, tuple)) and all(isinstance(x, str) and ARGUMENT.fullmatch(x) for x in parameters),
            'Kernel parameters must be individual safe EFI-shell arguments')
    require(len(parameters) <= 96 and sum(map(len, parameters)) <= 3072, 'Kernel parameters exceed bound')
    roots = [arg for arg in parameters if arg.startswith('root=')]
    require(len(roots) == 1, 'Exactly one native archinstall root parameter is required')
    if roots[0].startswith('root=UUID='):
        require(roots[0][10:].lower() == root_uuid.lower(), 'Root UUID differs from native kernel parameters')
    else:
        require(re.fullmatch(r'root=/dev/mapper/[A-Za-z0-9_+.-]+', roots[0]),
                'Root must be its filesystem UUID or a native LUKS/LVM mapper')
    forbidden = ('initrd=', 'systemd.run', 'systemd.unit=', 'systemd.wants=', 'rdinit=', 'init=', 'acpi_osi=',
                 'modprobe.blacklist=', 'module_blacklist=', 'nomodeset', 'single', 'emergency', 'rescue')
    require(not any(arg.startswith(forbidden) for arg in parameters), 'Analysis or incompatible kernel option refused')
    require('ro' not in parameters, 'The production root must be writable')
    result = list(parameters)
    # Retaining the EFI boot console would leave a second framebuffer writer.
    options = KERNEL_OPTIONS
    for option in options:
        key = option.split('=', 1)[0]
        existing = [arg for arg in result if arg.split('=', 1)[0] == key]
        require(not existing or existing == [option], 'Conflicting H7607BA kernel option: ' + key)
        if not existing:
            result.append(option)
    return result


def write_boot_files(target, esp, kernel_parameters, *, root_uuid, repo_root=None, installer=False):
    """Render target-local boot. Native archinstall supplies root/storage parameters.

    Run after target mkinitcpio. The caller installs systemd-boot with bootctl;
    no firmware NVRAM or block device is accessed here.
    """
    target = _root(target)
    esp = _root(esp)
    require(esp == target / 'boot', 'This production profile supports the ESP mounted at /boot')
    repo = Path(repo_root or REPO).resolve()
    lock = load_hardware_lock(repo)
    options = globals()['kernel_parameters'](kernel_parameters, root_uuid, installer=installer)
    kernel = _under(esp, KERNEL_IMAGE)
    initramfs = _under(esp, INITRAMFS_IMAGE)
    copied = {}
    for row in lock['artifacts']:
        if 'boot' not in row:
            continue
        source = _under(repo, row['path'])
        require(source.stat().st_size == row['bytes'] and digest(source) == row['sha256'],
                'Boot artifact SHA256 mismatch: ' + row['path'])
        path = _write(esp, row['boot'], source.read_bytes())
        copied[row['boot']] = digest(path)
    sentinel = 'H7607A-' + root_uuid.lower() + '.OK'
    copied[sentinel] = digest(_write(esp, sentinel, (root_uuid.lower() + '\n').encode()))
    template = (repo / 'assets/hardware/uefi/startup.nsh.in').read_text()
    startup = (template.replace('@SENTINEL@', sentinel).replace('@ROOT_UUID@', root_uuid.lower())
               .replace('@KERNEL_PARAMETERS@', ' '.join(options)))
    require(not re.search(r'@[A-Z_]+@', startup), 'Unresolved boot template token')
    copied['EFI/H7607A/startup.nsh'] = digest(_write(esp, 'EFI/H7607A/startup.nsh', startup.encode()))
    title = 'n1x p16 installer' if installer else 'Arch Linux ARM - ASUS ProArt P16'
    entry = ('title ' + title + '\nefi /EFI/H7607A/Shell.efi\noptions -startup -delay 0\n').encode()
    copied['loader/entries/h7607a.conf'] = digest(_write(esp, 'loader/entries/h7607a.conf', entry))
    copied['loader/loader.conf'] = digest(_write(esp, 'loader/loader.conf', b'default h7607a.conf\ntimeout 3\nconsole-mode keep\nauto-entries no\nauto-firmware yes\n'))
    receipt = {'schema': 'h7607a-installed-boot-files-v1', 'root_uuid': root_uuid.lower(),
               'boot_role': 'installer-usb' if installer else 'installed-system',
               'kernel_parameters': options, 'kernel_sha256': digest(kernel),
               'initramfs_sha256': digest(initramfs), 'files': copied}
    _write(target, 'var/lib/h7607a/boot-files.json', (json.dumps(receipt, indent=2) + '\n').encode())
    return receipt


