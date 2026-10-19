# Building the installer images

This builds the ESP and root partition images from the bundled prebuilt
hardware packages; it compiles nothing and produces no whole-disk image.

## Inputs and host

- The assembled source tree, the signed package repository in `build/repo/` and
  the locked artifacts. `locks/live-base.json` pins the ALARM rootfs and its
  detached signature, and `locks/live-tools.json` the live package set.
- Native AArch64 Linux 6.4 or newer (the workspace is a `noswap` tmpfs), Python
  3.13 or newer, GnuPG/GPGV, libarchive `bsdtar`, Zstandard, tar, e2fsprogs,
  dosfstools, util-linux and the systemd tools.
- At least 10 GiB of RAM for the 8 GiB tmpfs and the host processes.
- Keep the source outside `/var/tmp/h7607a-live-build`, and make sure that
  workspace does not exist before you start.

## Building

Run these commands from the assembled source directory:

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

The builder removes the base image's accounts and credentials. The images are
sparse, so they fit the 8 GiB tmpfs although their combined size is larger.

`/var/tmp/h7607a-live-build/build-manifest.json` records each image's size and
SHA-256. The images, `state.json` and the manifest live only in the tmpfs, so a
reboot discards them. A failed build leaves its mounts and loop devices in
place; there is no teardown command.

## Exporting

```sh
sudo python3 scripts/build-live.py status
sudo python3 scripts/build-live.py export --image ESP --manifest-sha256 HASH > "$HOME/h7607a-ESP.img"
sudo python3 scripts/build-live.py export --image root --manifest-sha256 HASH > "$HOME/h7607a-root.img"
```

`HASH` is the `manifest_sha256` value from `status`. The root image is
9,930,014,720 bytes and the ESP image 805,306,368 bytes. Export to persistent
storage outside the tmpfs, with space for both images, before rebooting.

## Writing the USB

`scripts/write-usb.py` runs on macOS against an `h7607a-build` Lima VM with this
source mounted at `/mnt/h7607a-source`. It accepts only the maintainer's USB
stick (`locks/usb-layout.json`), verifies its GPT, raw Python wheel carrier and
`P16BACKUP` partition without changing them, streams both images from the VM
into the ESP and root partitions, reads them back and ejects the stick. Pass
`--write --device /dev/diskN --build-manifest FILE --build-sha256 HASH` to
write; without `--write` it only verifies.

Neither image contains a GPT partition table or should be written to an entire
disk. The writer replaces only the two existing filesystem partitions and
does not prepare a blank stick.

## Installing the target

Check the supported P16 model and H7607BA.300 BIOS version in firmware setup
before booting the `n1x p16 installer` entry with Secure Boot disabled. The live
kernel loads the ACPI overrides before Archinstall can check the board and BIOS.
Connect with `nmtui` or Ethernet and run `archinstall`. Archinstall creates
the target filesystems and installs packages with pacstrap; the target root
does not derive from the live root image.
