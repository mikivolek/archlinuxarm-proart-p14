# ProArt P14 (H7407BA) port

this is a fork of [pfeifferj/archlinuxarm-proart-p16](https://github.com/pfeifferj/archlinuxarm-proart-p16),
all the hard work is there, huge kudos to pfeifferj. i just ported and tweaked on
ProArt P14 H7407BA (firmware H7407BA.303, same N1X chip) and it runs fine.

what changed vs upstream:

- acpi builder knows the P14 firmware H7407BA.303 too (same SSPM `_STA` fix,
  other offset, the ASUS SSDT needs no splice on this one)
- `nvidia_drm vblank=0` instead of `vblank=1`. with vblank on, the driver
  hands out flip timestamps way off from CLOCK_MONOTONIC and GNOME/mutter just
  freezes after a few frames (screen drawn once, mouse dead, ttys still ok).
  if you already installed with vblank=1, put the option in
  `/etc/modprobe.d/` and rebuild the initramfs, the modules load from there
- keyboard, audio and touchpad bits adjusted for the P14, hashes and locks
  updated to match, own package signing key

**ACPI tables:** same deal as upstream. the ACPI tables belong to ASUS and
NVIDIA and the Cirrus tuning firmware to ASUS and Cirrus Logic, neither is
redistributed here. no firmware table, nor anything derived from one, is in
this repo, only the hashes. dump your own P14's tables and build the overrides
yourself, see
[Building the ACPI overrides](08-documentation/current/payload/docs/acpi-overrides.md).

no warranty, works on my machine. original readme below.

---

# Arch Linux ARM on the ASUS ProArt P16 (H7607BA)

Device enablement and an installer for Arch Linux ARM on the ASUS ProArt P16
H7607BA-SE033W: NVIDIA N1X, AArch64, 128 GiB unified memory, firmware
H7607BA.300. The collection packages a patched NVIDIA kernel, the NVIDIA
display modules, keyboard and audio drivers, ACPI table overrides and an
adapter for upstream Archinstall, and builds installer partition images from
pinned inputs.

Partitioning, package installation, accounts and services stay with Arch Linux
ARM, pacman, mkinitcpio, systemd-boot and Archinstall. This repository only adds
what the hardware needs. Read the
[limits and known issues](08-documentation/current/payload/docs/limits.md)
before installing.

## Documentation

The pages in `08-documentation/current/payload/docs/` also ship in the
assembled tree and on the live USB.

| Stage | Where it runs | Result |
|-------|---------------|--------|
| [Dump and patch ACPI tables](08-documentation/current/payload/docs/acpi-overrides.md) | dump on the P16; patch in the checkout | firmware-specific overrides for early Linux boot |
| [Assemble inputs](08-documentation/current/payload/docs/assembling.md) | host with Bash, jq and Python | a verified tree with the paths the build scripts expect |
| [Build images](08-documentation/current/payload/docs/media-build.md) | native AArch64 Linux, as root | ESP and live root partition images, using prebuilt hardware packages |
| [Write the USB](08-documentation/current/payload/docs/media-build.md#writing-the-usb) | macOS and the named Lima VM | replacement partitions on the maintainer's existing stick |
| [Install](08-documentation/current/payload/docs/installing.md) | P16 booted from that USB | a new SSD system with the selected accounts, storage and packages |

Reference pages cover
[hardware support](08-documentation/current/payload/docs/hardware.md),
[development environments](08-documentation/current/payload/docs/development.md),
[maintainer builds](08-documentation/current/payload/docs/maintainer.md) and
[GPL source](08-documentation/current/payload/docs/gpl-source.md).

## Layout

| Directory | Contents |
|-----------|----------|
| `01-platform/` | kernel package and ACPI/EFI overrides |
| `02-drivers/` | NVIDIA display, keyboard, audio and SoundWire |
| `03-system/` | power management, device policy, audio routing and input files |
| `05-development/` | optional Python, PyTorch, CUDA SDK and container tooling |
| `06-installation/` | the Archinstall adapter and the offline wheel carrier reader |
| `07-build/` | image builder, USB writer, package recipes, signed repository and locked inputs |
| `08-documentation/` | documentation copied into the assembled tree |

[Maintainer builds](08-documentation/current/payload/docs/maintainer.md#component-metadata)
describes the component files that pin every input and how to update them, and
the assembled tree's layout is in its
[README](08-documentation/current/payload/README.md).

## License

The scripts, Python modules and configuration in this repository are under the
MIT license in `LICENSE`. Third-party material keeps its own license: kernel
patches and vendored kernel sources are GPL-2.0, `hid-asus.c` is
GPL-2.0-or-later, power-profiles-daemon is GPL-3.0-only, the NVIDIA open kernel
modules are dual MIT/GPL-2.0, NVIDIA's userspace driver is downloaded under
NVIDIA's license, the UCM files are BSD-3-Clause from NVIDIA, the EDK2 shell is
BSD-2-Clause-Patent and the ACPI guard EFI programs are MIT. Arch Linux ARM and
Ubuntu packages keep their distributions' licenses. The Cirrus tuning firmware
belongs to ASUS and Cirrus Logic, and the ACPI tables to ASUS and NVIDIA;
neither is redistributed (see
[Installing](08-documentation/current/payload/docs/installing.md) and
[Building the ACPI overrides](08-documentation/current/payload/docs/acpi-overrides.md)).
