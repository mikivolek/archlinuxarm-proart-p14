# ASUS ProArt P16 (H7607BA) Arch Linux ARM source

This tree builds the Arch Linux ARM installer for the ASUS ProArt P16
H7607BA-SE033W (NVIDIA N1X, AArch64, 128 GiB, firmware H7607BA.300), and the
live installer carries a copy of it in `/opt/h7607a-arch`. It installs through
upstream Archinstall with a device adapter, hardware packages and configuration
files. Building the images reuses the bundled prebuilt hardware packages; the
build scripts in `provenance/hardware/` compile the kernel and drivers
separately.

## Limits

- Only firmware H7607BA.300 is supported. The early ACPI archive, built from
  this machine's own tables, overrides its DSDT and SSDT on every boot of the
  live and installed systems, and the installer checks the board and BIOS
  version only once the live system is up, so check both in firmware setup
  first and do not update the BIOS afterwards.
- The USB writer supports only the maintainer's existing stick; there is no
  procedure yet for preparing a blank one.
- Secure Boot must be disabled.
- Automatic idle, lid and suspend-key actions are disabled.
- Camera, microphone capture, touchscreen and stylus, fingerprint login and the
  NPU are excluded.

## Layout

| Path | Contents |
|------|----------|
| `installer/h7607a.py` | entry point behind the live system's `archinstall` command |
| `src/h7607a/` | Archinstall adapter, provisioning, policy and build modules |
| `runtime/` | device policy copied into the target, and the UCM, WirePlumber, libinput and helper files that `h7607a-hardware` packages |
| `packages/` | pacman recipes for the custom packages |
| `build/repo/` | signed package repository |
| `assets/hardware/` | hardware payloads; `assets/keys/` holds public signing keys |
| `provenance/hardware/` | patched sources, diffs, build scripts and receipts behind prebuilt binaries |
| `locks/` | versions, source URLs, byte counts and SHA-256 values for every pinned artifact |
| `scripts/` | image builder, USB writer, package builder and fetcher |

Generate build outputs outside this tree. Private signing keys never belong in
it, in the live filesystem or on an installed system.

## Building the images

Run on native AArch64 Linux 6.4 or newer, as root, with at least 10 GiB of RAM,
Python 3.13 or newer, GnuPG/GPGV, libarchive `bsdtar`, Zstandard, tar,
e2fsprogs, dosfstools, util-linux and the systemd tools. The bundled signed
repository is reused:

```sh
python3 scripts/release.py seal
sudo python3 scripts/build-live.py build
```

[docs/media-build.md](docs/media-build.md) describes the build phases, outputs
and export.

## Installing

The boot sequence is UEFI => systemd-boot => EDK2 Shell => Linux, which loads
the early ACPI archive followed by the initramfs; nothing is flashed to the
firmware. Archinstall installs a fresh system rather than copying the live
root, and only the hardware packages are pinned.

1. Check that firmware setup reports H7607BA.300 on the supported P16 and
   disable Secure Boot. Connect AC power and the prepared installer USB.
   Hold Esc during firmware startup, pick the USB UEFI entry and boot
   `n1x p16 installer`.
2. Connect Ethernet or set up Wi-Fi with `nmtui`, then run `archinstall`. It
   first checks the Archinstall release, the board name and the BIOS version.
   It needs to reach `de3.mirror.archlinuxarm.org` or
   `fl.us.mirror.archlinuxarm.org` and an NTP server; if the clock does not
   synchronize within 90 seconds it stops before touching any disk.
3. Set the hostname, locale, timezone, accounts, disk layout and packages in
   [Archinstall 4.5](https://github.com/archlinux/archinstall/tree/78982a624824aa415f59bf88c515676a589e3c55).
   The kernel and bootloader are fixed.
4. Pick the internal SSD. Create a FAT32 ESP mounted at `/boot`, ideally
   1 GiB and at least 512 MiB, and an ext4 or Btrfs root.
5. Root can be plain, LUKS with a password, LVM, or LVM on LUKS with a single
   encrypted physical volume. Swap can be zram, a plain partition or a swap LV
   in the encrypted root volume group.
6. Do not pick TPM or FIDO unlocking, unified kernel images, Plymouth, another
   ESP mountpoint, LUKS on LVM, separately encrypted swap or a root spanning
   several encrypted physical volumes. The installer rejects them.
7. Confirm the disk operations and let Archinstall finish.
8. Shut down, remove the USB and boot the internal SSD.

Every installation gets the device policy (root-only ALSA capture nodes,
logind sleep hold, the adapted `power-profiles-daemon` unit and CPPC
initialization) and `h7607a-hardware`, which carries the camera blacklist, the
playback-only UCM for the speakers and headphone jack, the WirePlumber policy
without camera monitors or Bluetooth capture roles, the PixArt touchpad quirk,
`h7607a-brightness` for the panel and keyboard backlight and `h7607a-audio`
for playback volume. Audio, Bluetooth and networking packages come from
Archinstall's own options. The installer also downloads the pinned ASUS audio
driver package and installs the three Cirrus speaker tuning files from it in
`/usr/lib/firmware/cirrus/`; if that fails, the speakers run untuned and the
installer prints how to add the files later.

The "Development tools" menu defaults to Python, C/C++, the PyTorch CUDA
environment and GPU containers, and needs an ordinary user. The PyTorch
environment reads its wheels from a raw carrier region on the installer USB
(`locks/usb-layout.json`); without that carrier, the installer downloads them
inside the target. A failing development profile does not stop the
installation; for PyTorch and the CUDA SDK it prints the command that completes
it after the first boot. The installer never reads or changes the USB's backup
partition.

## Development environments

The PyTorch environment holds 31 pinned Python 3.14 wheels, including PyTorch
2.14.1+cu132 and Triton 3.8.0. It is a virtual environment on the system's
Python 3.14, which is not held, so a Python minor upgrade breaks it until the
lock is updated:

```sh
source ~/.local/share/h7607a/venvs/torch/bin/activate
```

The optional CUDA 13.4.3 SDK comes from 28 locked SBSA archives, including
`nvcc` 13.4.92, and needs about 20 GiB during installation. Use it from a
separate shell:

```sh
source ~/.local/opt/cuda-13.4.3/env.sh
```

`h7607a-container run IMAGE COMMAND...` runs an ARM64 image under rootless
Podman with NVIDIA CDI injection, `keep-id`, `keep-groups` and
`no-new-privileges`.

## After installation

The installer removes its local package repository from `pacman.conf` and adds
`IgnorePkg` holds for the kernel, the NVIDIA packages, `h7607a-hardware`,
`power-profiles-daemon-n1x` and the MediaTek, Cirrus and WHENCE firmware
packages, so ordinary updates come from the ALARM repositories without
replacing the device tuple: the kernel, driver, firmware and power packages
kept at compatible versions. The held packages get no updates, security fixes
included, until a new build is installed. The repository is signed with the
maintainer's Arch Linux key, which `archlinux-keyring` ships, so the target
keeps trusting it. A key of your own is deleted from the target's keyring after
installation; its public part stays in
`/usr/local/share/h7607a/package-signing.gpg`.

Logs from target commands go to `/var/log/h7607a/`. The live system caps its
persistent journal at 128 MiB.

## Known issues

- The Cirrus tuning files in `/usr/lib/firmware/cirrus/` belong to no package.
