# Arch Linux ARM on the ASUS ProArt P16 (H7607BA)

Device enablement and an installer for Arch Linux ARM on the ASUS ProArt P16
H7607BA-SE033W: NVIDIA N1X, AArch64, 128 GiB unified memory, firmware
H7607BA.300. The collection packages a patched NVIDIA kernel, the NVIDIA
display modules, keyboard and audio drivers, ACPI table overrides and an
adapter for upstream Archinstall, and builds installer partition images from
pinned inputs.

Partitioning, package installation, accounts and services stay with Arch Linux
ARM, pacman, mkinitcpio, systemd-boot and Archinstall. This repository only adds
what the hardware needs.

## Status and limits

- Tested on one machine with firmware H7607BA.300. The ACPI overrides are built
  from that firmware's tables, which you dump from your own machine; see
  [Building the ACPI overrides](#building-the-acpi-overrides). The live USB and
  the installed system load them on every boot, and the installer checks the
  board and BIOS version only once the live system is up, so check both in
  firmware setup before booting the USB and do not update the BIOS afterwards.
- Secure Boot must be disabled.
- Automatic idle, lid and suspend-key actions are disabled.
- Camera, microphone capture, touchscreen and stylus, fingerprint login and the
  NPU are excluded.
- The large pinned inputs are not in git and are downloaded on request; see
  [Building from a fresh clone](#building-from-a-fresh-clone).
- The USB writer supports only the maintainer's existing stick; there is no
  procedure yet for preparing a blank one.

## Process overview

| Stage | Where it runs | Result |
|-------|---------------|--------|
| [Dump and patch ACPI tables](#building-the-acpi-overrides) | dump on the P16; patch in the checkout | firmware-specific overrides for early Linux boot |
| [Assemble inputs](#assembling-the-inputs) | host with Bash, jq and Python | a verified tree with the paths the build scripts expect |
| [Build images](#building-the-installer-images) | native AArch64 Linux, as root | ESP and live root partition images, using prebuilt hardware packages |
| [Write the USB](#writing-the-usb) | macOS and the named Lima VM | replacement partitions on the maintainer's existing stick |
| [Install](#installing) | P16 booted from that USB | a new SSD system with the selected accounts, storage and packages |

Building images does not compile the kernel or drivers; see
[Rebuilding packages and binaries](#rebuilding-packages-and-binaries).

## Hardware support

| Area | What is used |
|------|--------------|
| Kernel | `linux-n1x-nvidia-bin 7.0.0.1022.23-3`, kernel release `7.0.0-1022-nvidia`, built from [NVIDIA/NV-Kernels `2234cd7f`](https://github.com/NVIDIA/NV-Kernels/tree/2234cd7f8f8a9fdbceef37f82c531016aab8cf73) with the ACPI override allocation patch |
| Boot | systemd-boot, the pinned EDK2 shell, the kernel, an early ACPI archive with the SSPM DSDT and ASUS SSDT overrides built from the machine's own tables, and the mkinitcpio image |
| Display | `nvidia-n1x-modules 615.78.08-4` from [open-gpu-kernel-modules `1a82fbfd`](https://github.com/NVIDIA/open-gpu-kernel-modules/tree/1a82fbfda8f321b056eb3568850f9316c5e465c5) with the DRM Normal-NC framebuffer mapping patch and [PR 1368](https://github.com/NVIDIA/open-gpu-kernel-modules/pull/1368), plus `nvidia-utils 615.78.08` and the Tegra RM API library |
| Keyboard | patched `hid-asus` loaded before udev; panel and keyboard backlight through `h7607a-brightness` and logind; Fn-lock left alone |
| Touchpad | device-specific PixArt pressure and palm quirk for libinput |
| Audio | MediaTek machine driver, the SoundWire interrupt-gate backport, Cirrus CS35L56 tuning firmware extracted from the ASUS driver package during installation, and playback-only UCM derived from [alsa-ucm-conf PR 862](https://github.com/alsa-project/alsa-ucm-conf/pull/862) for the speakers and headphone jack |
| Power | CPPC initialization and `power-profiles-daemon-n1x 0.30.nvidia3001c25-2` from [power-profiles-daemon MR 236](https://gitlab.freedesktop.org/upower/power-profiles-daemon/-/merge_requests/236) (commit `3001c25a`); no `asusd` or `asusctl` |
| Wi-Fi and Bluetooth | MT7925 through `linux-firmware-mediatek` |

The internal panel was tested at 3840x2400, 120 Hz, with scale 2.

Every boot uses this kernel command line:

```text
rw rootwait console=tty0 acpi=nospcr thermal.tzp=100 mem_sleep_default=s2idle initcall_blacklist=simpledrm_platform_driver_init fbcon=font:TER16x32 log_buf_len=4M loglevel=3
```

Device policy blacklists `uvcvideo`, makes ALSA capture nodes root-only and
turns off the WirePlumber camera monitors and Bluetooth headset capture roles.
Playback is unaffected.

## Layout

Each numbered directory holds components. A component's `component.yaml` lists
its files with source and destination paths, byte counts, SHA-256 values and
modes, and `manifest.yaml` pins every `component.yaml` by hash. The files use
JSON syntax, which is valid YAML 1.2, so `jq` reads them.

| Directory | Contents |
|-----------|----------|
| `01-platform/` | kernel package and ACPI/EFI overrides |
| `02-drivers/` | NVIDIA display, keyboard, audio and SoundWire |
| `03-system/` | power management, device policy, audio routing and input files |
| `05-development/` | optional Python, PyTorch, CUDA SDK and container tooling |
| `06-installation/` | the Archinstall adapter and the offline wheel carrier reader |
| `07-build/` | image builder, USB writer, package recipes, signed repository and locked inputs |
| `08-documentation/` | documentation copied into the assembled tree |

Each `provenance/` directory holds the patched sources, diffs, build scripts and
receipts behind a prebuilt binary. When you change a file, update its byte count
and SHA-256 in `component.yaml` and the component hash in `manifest.yaml`.

### Component metadata

- `type` is `patch` for modified upstream code, `custom` for code written here
  and `direct` for upstream artifacts and configuration used as they are.
- `repo_url` and `checkout_hash` name the upstream source, and `sparse` the
  checkout paths; they are `null` where no upstream checkout exists.
- `dependencies` records tool and package versions, `depends_on` names other
  components, and items may list further requirements.
- `files[].source` is relative to the component directory and
  `files[].destination` to the assembled tree. Item-level script, patch and
  artifact references and `links` resolve inside the assembled tree.

## Building

### Building from a fresh clone

Git holds the recipes, sources and pins but not most large inputs, such as
the ALARM rootfs and packages, the signed repository under
`07-build/locked-inputs/payload/build/`, the NVIDIA `.run` and the kernel
runtime tarball. `./assemble.sh --fetch` downloads them from the URLs in their
locks, upstream first. The last URL of each one is its asset on this
repository's `inputs-1` release, named by its SHA-256: ALARM keeps only the
current version of each package and replaces the rootfs in place, so once
upstream moves on, only the release copy matches the pins.
`07-build/locked-inputs/payload/locks/release-inputs.json` lists the release
files that no other lock covers.

On a fresh clone, [build the ACPI overrides](#building-the-acpi-overrides)
in the checkout first, then run `sudo ./assemble.sh --fetch build DIR` on the
[required build host](#building-the-installer-images), with a new absolute
output path for `DIR`. The packages come signed with the
maintainer's key; to use your own, see
[Signing with your own key](#signing-with-your-own-key).

### Building the ACPI overrides

No license allows redistributing the ASUS and NVIDIA ACPI tables or anything
derived from them, so the overrides are built from a dump of your own P16 with
firmware H7607BA.300. The resulting archive is loaded during early boot;
nothing is flashed to the firmware. The dump stick only collects the original
tables and is separate from the installer USB.

1. Mount a FAT32 USB stick and run
   `python3 01-platform/acpi/payload/scripts/stage-acpi-dump.py MOUNTPOINT`. It
   copies the pinned EDK2 Shell to `EFI/BOOT/BOOTAA64.EFI` and the dump script
   to `EFI/BOOT/startup.nsh`, and changes nothing else on the stick.
2. Boot the P16 from that stick with Secure Boot disabled. The Shell writes
   `DSDT0000.bin`, `SSDT0000.bin`, `SSDT0001.bin` and so on to the stick's
   `H7607A-ACPI` folder. A Linux system booted on the P16 without these
   overrides can copy `/sys/firmware/acpi/tables/DSDT` and `SSDT*` instead.
3. Build ACPICA `iasl` 20260408 from
   [acpica-unix-20260408.tar.gz](https://github.com/acpica/acpica/releases/download/20260408/acpica-unix-20260408.tar.gz)
   (SHA-256 `e66ceb26d6d514ce164fe22f5a4f7ca165cc38349d7a97f41a21f19b364647a2`).
4. In the checkout, run the builder on the dump folder:

   ```sh
   python3 01-platform/acpi/payload/provenance/hardware/power-archive-builder.py \
       --dump /mnt/stick/H7607A-ACPI --iasl /path/to/iasl
   ```

   It selects the DSDT and the ASUS SSDT, stops unless they match the pinned
   H7607BA.300 hashes, and writes the two tables, `power-source.dsl` and
   `h7607a-acpi.cpio` to their component paths. `.gitignore` keeps them out of
   commits.

### Assembling the inputs

`assemble.sh` checks every component file against its pin, copies the
component payloads into a new directory, applies the recorded file modes,
creates the repository links and writes the source manifest. It needs Bash,
`jq` and Python 3.13 or newer:

```sh
./assemble.sh --fetch assemble "$HOME/h7607a-source"
```

The output must be an absolute path that does not exist yet. A file that
differs from its pin stops the run before anything is written. Absent inputs
are listed by directory. Without `--fetch` the run stops and prints the command
that downloads the ones with URLs; with `--fetch`, `scripts/fetch.py` downloads
and verifies them inside the output. Files listed with a note, such as the ACPI
overrides, have to be created in the checkout before `build`. The output's
`src/`, `runtime/`, `packages/`, `provenance/`, `locks/`, `assets/`, `scripts/`
and `build/repo/` directories are the image builder's inputs.

Paths starting with a numbered directory such as `01-platform/` are in the
checkout; paths such as `scripts/` and `locks/` are in the assembled tree, and
commands using them run from there.

### Building the installer images

The installer images need native AArch64 Linux, either a machine or a VM, with
at least 10 GiB of RAM for the 8 GiB no-swap tmpfs, which needs Linux 6.4 or
newer. Install Python 3.13 or newer, GnuPG/GPGV, libarchive `bsdtar`,
Zstandard, tar, e2fsprogs, dosfstools, util-linux and the systemd tools. Make
sure `/var/tmp/h7607a-live-build` does not exist, then run from the checkout:

```sh
sudo ./assemble.sh --fetch build /srv/h7607a-image-source
```

This command assembles a new tree and then builds the images. If you already
assembled the complete inputs, run `sudo python3 scripts/build-live.py build`
from that directory instead.

| Phase | Work |
|-------|------|
| `prepare` | create the root image and extract the pinned ALARM rootfs |
| `live-tools` | install the locked live package set |
| `provision` | install the signed hardware packages |
| `embed` | copy in the source and repository, apply the live policy, generate the initramfs and EFI boot files |
| `seal` | unmount the image filesystems, detach loop devices and record image hashes |

The ESP and root images are sparse, so they fit the 8 GiB tmpfs although their
combined size is larger.

The images, `state.json` and `build-manifest.json` with each image's size and
SHA-256 stay in the tmpfs at `/var/tmp/h7607a-live-build`. Export or write them
before rebooting, which discards the workspace. From the assembled directory:

```sh
sudo python3 scripts/build-live.py status
sudo python3 scripts/build-live.py export --image ESP --manifest-sha256 HASH > "$HOME/h7607a-ESP.img"
sudo python3 scripts/build-live.py export --image root --manifest-sha256 HASH > "$HOME/h7607a-root.img"
```

Replace `HASH` with the `manifest_sha256` field from `status`. Redirect the
images to persistent storage outside the tmpfs, with space for both images.
The USB writer can also stream them directly from the VM.

The live root drops the source image's accounts, shell history, network
credentials, SSH host keys, random seed and machine ID, and removes the upstream
`alarm` account. Root logs in on tty1 without a password, on the live system
only, and the live root is persistent, so Wi-Fi credentials entered there stay
on the stick. It runs NetworkManager, systemd-resolved, timesyncd and the N1X
power services, with SSH, Bluetooth, other network managers and wait-online
units disabled, and uses `C.UTF-8`, the UK keymap and the Terminus console
font. None of this carries over to the installed system, which gets the locale,
keymap, accounts and services chosen in Archinstall. Target commands run in
private mount and PID namespaces through `arch-chroot`.

### Cleaning up a failed build

The workspace holds the ext4 and ESP images on loop devices, the root mounted
at `root/` with the ESP and a read-only bind of the source below it, and a
tmpfs underneath everything. To start over:

```sh
sudo umount -R /var/tmp/h7607a-live-build/root
sudo losetup -j /var/tmp/h7607a-live-build/root.img
sudo losetup -j /var/tmp/h7607a-live-build/ESP.img
sudo losetup -d /dev/loopN    # each device the two commands above list
sudo umount /var/tmp/h7607a-live-build
sudo rmdir /var/tmp/h7607a-live-build
```

## Writing the USB

`scripts/write-usb.py` runs on macOS against an `h7607a-build` Lima VM with the
assembled source mounted at `/mnt/h7607a-source`. It accepts only the
maintainer's 61.5 GB USB stick, whose GPT, raw wheel carrier and `P16BACKUP`
partition it verifies and leaves untouched, and it writes only the ESP and the
Linux root partition, then reads both back and ejects the stick. Without
`--write` it unmounts the stick's volumes and verifies them without writing.

Neither image goes onto a whole disk: each holds one filesystem and no
partition table. The ESP image is 805,306,368 bytes
(FAT32, label `N1XESP`) and the root image is 9,930,014,720 bytes (ext4, label
`N1XALARM`); both carry fixed UUIDs that the boot configuration expects.

## Installing

The boot sequence is UEFI => systemd-boot => EDK2 Shell => Linux, which loads
the early ACPI archive followed by the initramfs.

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

Archinstall installs a fresh system rather than copying the live root. Only
the hardware packages are pinned; everything else comes from the ALARM
repositories at installation time.

Before the target initramfs is built, the installer downloads the pinned ASUS
audio driver package from `dlcdnets.asus.com` and installs the three Cirrus
tuning files from it in `/usr/lib/firmware/cirrus/`. If the download fails, the
installation continues with untuned speakers and prints how to add the files
later.

The "Development tools" menu defaults to Python, C/C++, the PyTorch CUDA
environment and GPU containers, and needs an ordinary user account. The PyTorch
environment reads its wheels from a raw carrier region that exists only on the
maintainer's USB; without it, the installer downloads the wheels inside the
target. A failing development profile does not stop the installation; for
PyTorch and the CUDA SDK it prints the command that completes it after the
first boot.

## Development environments

The PyTorch environment holds 31 pinned Python 3.14 wheels, including PyTorch
2.14.1+cu132 and Triton 3.8.0. The cuSPARSELt 0.8.1 wheel is retagged from
`manylinux2014_sbsa` to `manylinux2014_aarch64` with its RECORD updated; its
libraries are unchanged. It is a virtual environment on the system's Python
3.14 interpreter:

```sh
source ~/.local/share/h7607a/venvs/torch/bin/activate
```

The optional CUDA 13.4.3 SDK comes from 28 locked SBSA archives, including
`nvcc` 13.4.92, and needs about 20 GiB during installation. It skips the
driver, Nsight GUIs, Orin and NPU libraries and GPUDirect modules. Use it from a
separate shell so its library paths stay out of the PyTorch environment:

```sh
source ~/.local/opt/cuda-13.4.3/env.sh
```

Wheels come from PyPI and the
[PyTorch CUDA 13.2 index](https://download.pytorch.org/whl/cu132), SDK archives
from NVIDIA's SBSA redistributables, and each is checked against the byte count
and SHA-256 in its lock file.

The container profile installs rootless Podman, `crun`, the NVIDIA container
toolkit with the absolute `/usr/bin/ldconfig` hook and CDI with specification
refresh, and assigns non-overlapping subordinate UID and GID ranges.
`h7607a-container run IMAGE COMMAND...` runs an ARM64 image with direct CDI
injection, `keep-id`, `keep-groups` and `no-new-privileges`; plain `podman`
keeps working.

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

- PyTorch: the environment is built for Python 3.14, and the system Python is
  not held, so a Python minor upgrade breaks it until the lock is updated.
- UCM: `h7607a-hardware` owns the `mt8901-soundwir` UCM files. If
  `alsa-ucm-conf` ever ships the same paths, its upgrade stops with a file
  conflict until `h7607a-hardware` drops them.
- Cirrus tuning: the three files in `/usr/lib/firmware/cirrus/` belong to no
  package, so pacman neither tracks nor removes them.

## Maintainer builds

### Rebuilding packages and binaries

`scripts/build-packages.py` rebuilds the custom packages with `makepkg` in a
native Arch Linux ARM build root and publishes the signed repository in
`build/repo`. Run the build step (`--build-only`) as an unprivileged user that
cannot read the signing home, and sign separately with
`--assemble-only --signing-home DIR`, so package build code never sees the key.
The signing step accepts only packages that the build step recorded in
`build-record.json` with matching sizes and SHA-256 values.

This rebuilds packages from their recipes and pinned inputs. The kernel
recipe packages a precompiled `h7607a-kernel-runtime.tar.zst`, and the hardware
recipes include prebuilt modules. To change those binaries, first follow the
build scripts in their `provenance/` directories, update the affected artifact,
recipe and lock pins, and then rebuild the package repository and images.
The [GPL source table](#gpl-source) locates the corresponding sources and
scripts.

### Signing with your own key

Build the ACPI overrides in the checkout before following this workflow.

1. Create a key in a private GnuPG home and export its public key to
   `07-build/packages/payload/assets/keys/h7607a-build.gpg`.
2. In `07-build/packages/payload/locks/packages.json`, set `fingerprint`,
   `bytes` and `sha256` for the new file in both `signing_keys.local` and the
   `local-key` artifact row.
3. In `07-build/packages/component.yaml`, update `bytes` and `sha256` for both
   `payload/assets/keys/h7607a-build.gpg` and `payload/locks/packages.json`,
   then that component's hash in `manifest.yaml`.
4. Run `./assemble.sh --fetch assemble DIR`, where `DIR` is an absolute path
   that does not exist. Enter `DIR` and move the downloaded `build/repo` aside;
   the signing step requires that destination to be absent.
5. In a native Arch Linux ARM build root, run
   `python3 scripts/build-packages.py --build-only` as an unprivileged user
   without access to the signing home. The assembled tree and build output
   must be writable by that user.
6. With access to the same assembled tree and build output, run
   `python3 scripts/build-packages.py --assemble-only --signing-home /absolute/private/gnupg`
   as the signing user. Keep that GnuPG home outside the assembled tree.
7. In the assembled tree, run `python3 scripts/release.py seal`, then
   `sudo python3 scripts/build-live.py build`.

### Publishing the inputs

`publish-inputs.sh STAGE [SEARCH...]` collects every pinned file whose last URL
is on the release, checks it against its pin and links or copies it into
`STAGE` under its SHA-256 name. It looks in the checkout and in each `SEARCH`
directory, including a Git LFS object store, and lists the files it found
nowhere. Any file that differs from its pin stops it; otherwise it prints the
`gh release create --draft` and `gh release upload` commands, and `--upload`
runs them, skipping assets the draft already holds.

The release also carries the corresponding source of every copyleft Arch Linux
ARM package in the rootfs, the live package set and `packages.json`, listed in
`07-build/locked-inputs/payload/locks/alarm-sources.json`. To rebuild it after
those pins change, run on an Arch Linux host with git, Mercurial and makepkg:

```sh
./collect-sources.py ROOTFS DIR > 07-build/locked-inputs/payload/locks/alarm-sources.json
```

`ROOTFS` is the pinned rootfs tarball. Each package version gets
`DIR/<pkgbase>-<version>.src.tar.gz` with its exact recipe, patches and
upstream sources, git and Mercurial sources archived at their pinned revision;
`SUBSTITUTES` in the script lists the few sources taken from another host. A
rerun with the same tool versions gives the same files. Update the lock's pins
in `component.yaml` and `manifest.yaml`, then pass `DIR` to `publish-inputs.sh`
as a `SEARCH` directory.

## GPL source

| Binary | Corresponding source |
|--------|----------------------|
| `linux-n1x-nvidia-bin` kernel and modules | Ubuntu `linux-nvidia-7.0` 7.0.0-1022.23~24.04.1 (orig and diff URLs with SHA-256 in `01-platform/kernel/component.yaml`, also on the `inputs-1` release), [NV-Kernels `2234cd7f`](https://github.com/NVIDIA/NV-Kernels/tree/2234cd7f8f8a9fdbceef37f82c531016aab8cf73), the ACPI allocation patch, `config-7.0.0-1022-nvidia`, `build.sh` and `package.sh` |
| Ubuntu `linux-image-unsigned`, `linux-modules` and `linux-headers` debs | the same Ubuntu source package |
| `soundwire-mtk.ko` | pristine sources, patch, `build.sh` and receipts in `02-drivers/soundwire` |
| `nvidia-modeset.ko` | open-gpu-kernel-modules `1a82fbfd` with PR 1368, `build-modeset.sh` in `02-drivers/nvidia-display` |
| `nvidia.ko`, `nvidia-uvm.ko` | `nvidia-open-dkms 615.78.08-1`, pinned in `locks/packages.json` and on the `inputs-1` release |
| `nvidia-drm.ko` | `drm-kernel-open-source.tar.gz`, `drm.patch` and `drm-build.sh` in `02-drivers/nvidia-display` |
| `hid-asus.ko` | `hid-asus.c`, `hid-ids.h`, `hid.Kbuild` and `hid-build.sh` in `02-drivers/keyboard` |
| `mt8901-acp-sdw-legacy-mach.ko` | `audio-source.c`, `audio-identity.patch`, `audio.Kbuild` and `audio-build.sh` in `02-drivers/audio` |
| `power-profiles-daemon-n1x` | the MR 236 source tarball in `03-system/power` |
| copyleft Arch Linux ARM packages in the rootfs, the live set and `packages.json` | one source bundle per package version on the `inputs-1` release, listed in `locks/alarm-sources.json` |

The three build scripts run natively on AArch64 against the pinned 7.0.0-1022
header debs and `config-7.0.0-1022-nvidia`, check their inputs against the
build receipts and compare the result with the shipped module.

## License

The scripts, Python modules and configuration in this repository are under the
MIT license in `LICENSE`. Third-party material keeps its own license: kernel
patches and vendored kernel sources are GPL-2.0, `hid-asus.c` is
GPL-2.0-or-later, power-profiles-daemon is GPL-3.0-only, the NVIDIA open kernel
modules are dual MIT/GPL-2.0, NVIDIA's userspace driver is downloaded under
NVIDIA's license, the UCM files are BSD-3-Clause from NVIDIA, the EDK2 shell is
BSD-2-Clause-Patent and the ACPI guard EFI programs are MIT. Arch Linux ARM and
Ubuntu packages keep their distributions' licenses. The Cirrus tuning firmware
belongs to ASUS and Cirrus Logic, and the ACPI tables to ASUS and NVIDIA. The
tuning is extracted from ASUS's driver package on the owner's machine during
installation, and the ACPI overrides are built from the owner's own table dump;
neither is redistributed.
