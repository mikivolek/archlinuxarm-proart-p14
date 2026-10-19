# ASUS ProArt P16 (H7607BA) Arch Linux ARM source

This is the assembled source tree of the Arch Linux ARM installer for the ASUS
ProArt P16 H7607BA. The live installer carries a copy of it in
`/opt/h7607a-arch`.

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
| `docs/` | the documentation below |

Generate build outputs outside this tree. Private signing keys never belong in
it, in the live filesystem or on an installed system.

## Documentation

- [Limits and known issues](docs/limits.md)
- [Hardware support](docs/hardware.md)
- [Building the ACPI overrides](docs/acpi-overrides.md)
- [Assembling the inputs](docs/assembling.md)
- [Building and writing the installer media](docs/media-build.md)
- [Installing](docs/installing.md)
- [Development environments](docs/development.md)
- [Maintainer builds](docs/maintainer.md)
- [GPL source](docs/gpl-source.md)
