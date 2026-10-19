# Limits and known issues

Only firmware H7607BA.300 is supported, and the collection has been tested on
one machine. The [ACPI overrides](acpi-overrides.md) match only that firmware,
and the installer checks the board and BIOS version only once the live system
is up, so do not update the BIOS after installing.

Camera, microphone capture, touchscreen and stylus, fingerprint login and the
NPU are excluded.

## Known issues

- PyTorch: the environment is built for Python 3.14, and the system Python is
  not held, so a Python minor upgrade breaks it until the lock is updated.
- UCM: `h7607a-hardware` owns the `mt8901-soundwir` UCM files. If
  `alsa-ucm-conf` ever ships the same paths, its upgrade stops with a file
  conflict until `h7607a-hardware` drops them.
- Cirrus tuning: the three files in `/usr/lib/firmware/cirrus/` belong to no
  package, so pacman neither tracks nor removes them.
