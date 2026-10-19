# Installing

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
repositories at installation time. Audio, Bluetooth and networking packages
come from Archinstall's own options, and every installation gets the
[device policy and `h7607a-hardware`](hardware.md#device-policy). The
"Development tools" menu installs the
[development environments](development.md).

Before the target initramfs is built, the installer downloads the pinned ASUS
audio driver package from `dlcdnets.asus.com` and installs the three Cirrus
tuning files from it in `/usr/lib/firmware/cirrus/`. If the download fails, the
installation continues with untuned speakers and prints how to add the files
later.

Target commands run in private mount and PID namespaces through `arch-chroot`
and log to `/var/log/h7607a/`. The installer never reads or changes the USB's
backup partition.

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
