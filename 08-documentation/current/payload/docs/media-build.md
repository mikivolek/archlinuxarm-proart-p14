# Building and writing the installer media

This builds the ESP and live root partition images from the bundled prebuilt
hardware packages and writes them to the installer USB. It compiles nothing
and produces no whole-disk image; [Maintainer builds](maintainer.md) covers
rebuilding the packages and binaries.

## Inputs and host

- The assembled source tree, the signed package repository in `build/repo/` and
  the locked artifacts. `locks/live-base.json` pins the ALARM rootfs and its
  detached signature, and `locks/live-tools.json` the live package set.
- Native AArch64 Linux 6.4 or newer (the workspace is a `noswap` tmpfs), on a
  machine or in a VM, with Python 3.13 or newer, GnuPG/GPGV, libarchive
  `bsdtar`, Zstandard, tar, e2fsprogs, dosfstools, util-linux and the systemd
  tools.
- At least 10 GiB of RAM for the 8 GiB tmpfs and the host processes.
- Keep the source outside `/var/tmp/h7607a-live-build`, and make sure that
  workspace does not exist before you start.

## Building

From the checkout, this assembles a new tree and builds the images in one run:

```sh
sudo ./assemble.sh --fetch build /srv/h7607a-image-source
```

From a tree you already [assembled](assembling.md) with complete inputs, run:

```sh
python3 scripts/release.py seal
sudo python3 scripts/build-live.py build
```

`build` runs these phases in order; each can also be run on its own:

| Phase | Work |
|-------|------|
| `prepare` | create the root image and extract the pinned ALARM rootfs |
| `live-tools` | install the locked live package set |
| `provision` | install the signed hardware packages |
| `embed` | copy in the source and repository, apply the live policy, generate the initramfs and EFI boot files |
| `seal` | unmount the image filesystems, detach loop devices and record image hashes |

The images are sparse, so they fit the 8 GiB tmpfs although their combined
size is larger. `/var/tmp/h7607a-live-build/build-manifest.json` records each
image's size and SHA-256. The images, `state.json` and the manifest live only
in the tmpfs, so a reboot discards them.

## The live system

The live root drops the source image's accounts, shell history, network
credentials, SSH host keys, random seed and machine ID, and removes the upstream
`alarm` account. Root logs in on tty1 without a password, on the live system
only, and the live root is persistent, so Wi-Fi credentials entered there stay
on the stick. It runs NetworkManager, systemd-resolved, timesyncd and the N1X
power services, with SSH, Bluetooth, other network managers and wait-online
units disabled, uses `C.UTF-8`, the UK keymap and the Terminus console font,
and caps its persistent journal at 128 MiB. None of this carries over to the
installed system, which gets the locale, keymap, accounts and services chosen
in Archinstall.

## Exporting

From the assembled directory:

```sh
sudo python3 scripts/build-live.py status
sudo python3 scripts/build-live.py export --image ESP --manifest-sha256 HASH > "$HOME/h7607a-ESP.img"
sudo python3 scripts/build-live.py export --image root --manifest-sha256 HASH > "$HOME/h7607a-root.img"
```

`HASH` is the `manifest_sha256` value from `status`. Export to persistent
storage outside the tmpfs, with space for both images, before rebooting. The
USB writer can also stream the images directly from the VM.

## Cleaning up a failed build

A failed build leaves its mounts and loop devices in place, and there is no
teardown command. The workspace holds the ext4 and ESP images on loop devices,
the root mounted at `root/` with the ESP and a read-only bind of the source
below it, and a tmpfs underneath everything. To start over:

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
maintainer's 61.5 GB USB stick, described in `locks/usb-layout.json`, and
verifies its GPT, raw Python wheel carrier and `P16BACKUP` partition without
changing them. It streams both images from the VM into the ESP and root
partitions, reads them back and ejects the stick. Pass
`--write --device /dev/diskN --build-manifest FILE --build-sha256 HASH` to
write; without `--write` it unmounts the stick's volumes and verifies them
without writing.

Neither image goes onto a whole disk: each holds one filesystem and no
partition table, and the writer replaces only the two existing filesystem
partitions, so it does not prepare a blank stick. The ESP image is 805,306,368
bytes (FAT32, label `N1XESP`) and the root image 9,930,014,720 bytes (ext4,
label `N1XALARM`); both carry fixed UUIDs that the boot configuration expects.
