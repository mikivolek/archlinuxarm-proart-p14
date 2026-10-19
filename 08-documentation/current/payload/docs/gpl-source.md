# GPL source

| Binary | Corresponding source |
|--------|----------------------|
| `linux-n1x-nvidia-bin` kernel and modules | Ubuntu `linux-nvidia-7.0` 7.0.0-1022.23~24.04.1 (orig and diff URLs with SHA-256 in `01-platform/kernel/component.yaml`, also on the `inputs-1` release), [NV-Kernels `2234cd7f`](https://github.com/NVIDIA/NV-Kernels/tree/2234cd7f8f8a9fdbceef37f82c531016aab8cf73), the ACPI allocation patch, `config-7.0.0-1022-nvidia`, `build.sh` and `package.sh` |
| Ubuntu `linux-image-unsigned`, `linux-modules` and `linux-headers` debs | the same Ubuntu source package |
| `soundwire-mtk.ko` | pristine sources, patch, `build.sh` and receipts in `02-drivers/soundwire` |
| `nvidia-modeset.ko` | open-gpu-kernel-modules `1a82fbfd` with PR 1368, `build-modeset.sh` in `02-drivers/nvidia-display` |
| `nvidia.ko`, `nvidia-uvm.ko` | `nvidia-open-dkms 615.78.08-1`, pinned in `07-build/packages/payload/locks/packages.json` and on the `inputs-1` release |
| `nvidia-drm.ko` | `drm-kernel-open-source.tar.gz`, `drm.patch` and `drm-build.sh` in `02-drivers/nvidia-display` |
| `hid-asus.ko` | `hid-asus.c`, `hid-ids.h`, `hid.Kbuild` and `hid-build.sh` in `02-drivers/keyboard` |
| `mt8901-acp-sdw-legacy-mach.ko` | `audio-source.c`, `audio-identity.patch`, `audio.Kbuild` and `audio-build.sh` in `02-drivers/audio` |
| `power-profiles-daemon-n1x` | the MR 236 source tarball in `03-system/power` |
| copyleft Arch Linux ARM packages in the rootfs, the live set and `packages.json` | one source bundle per package version on the `inputs-1` release, listed in `07-build/locked-inputs/payload/locks/alarm-sources.json` |

The three build scripts run natively on AArch64 against the pinned 7.0.0-1022
header debs and `config-7.0.0-1022-nvidia`, check their inputs against the
build receipts and compare the result with the shipped module.
