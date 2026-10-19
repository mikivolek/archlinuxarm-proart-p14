# Building the ACPI overrides

No license allows redistributing the ASUS and NVIDIA ACPI tables or anything
derived from them, so the overrides are built from a dump of your own P16 with
firmware H7607BA.300. Nothing is flashed to the firmware. The dump stick only
collects the original tables and is separate from the installer USB.

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
