# Hardware support

| Area | What is used |
|------|--------------|
| Kernel | `linux-n1x-nvidia-bin 7.0.0.1022.23-3`, kernel release `7.0.0-1022-nvidia`, built from [NVIDIA/NV-Kernels `2234cd7f`](https://github.com/NVIDIA/NV-Kernels/tree/2234cd7f8f8a9fdbceef37f82c531016aab8cf73) with the ACPI override allocation patch |
| Boot | UEFI => systemd-boot => the pinned EDK2 shell => Linux, which loads an early ACPI archive with the SSPM DSDT and ASUS SSDT overrides built from the machine's own tables, followed by the mkinitcpio image |
| Display | `nvidia-n1x-modules 615.78.08-4` from [open-gpu-kernel-modules `1a82fbfd`](https://github.com/NVIDIA/open-gpu-kernel-modules/tree/1a82fbfda8f321b056eb3568850f9316c5e465c5) with the DRM Normal-NC framebuffer mapping patch and [PR 1368](https://github.com/NVIDIA/open-gpu-kernel-modules/pull/1368), plus `nvidia-utils 615.78.08` and the Tegra RM API library |
| Keyboard | patched `hid-asus` loaded before udev; panel and keyboard backlight through `h7607a-brightness` and logind; Fn-lock left alone |
| Touchpad | device-specific PixArt pressure and palm quirk for libinput |
| Audio | MediaTek machine driver, the SoundWire interrupt-gate backport, Cirrus CS35L56 tuning firmware added during [installation](installing.md), and playback-only UCM derived from [alsa-ucm-conf PR 862](https://github.com/alsa-project/alsa-ucm-conf/pull/862) for the speakers and headphone jack |
| Power | CPPC initialization and `power-profiles-daemon-n1x 0.30.nvidia3001c25-2` from [power-profiles-daemon MR 236](https://gitlab.freedesktop.org/upower/power-profiles-daemon/-/merge_requests/236) (commit `3001c25a`); no `asusd` or `asusctl` |
| Wi-Fi and Bluetooth | MT7925 through `linux-firmware-mediatek` |

The internal panel was tested at 3840x2400, 120 Hz, with scale 2.

Every boot uses this kernel command line:

```text
rw rootwait console=tty0 acpi=nospcr thermal.tzp=100 mem_sleep_default=s2idle initcall_blacklist=simpledrm_platform_driver_init fbcon=font:TER16x32 log_buf_len=4M loglevel=3
```

## Device policy

Both the live and installed systems get the device policy. It blacklists
`uvcvideo`, has logind ignore idle, lid and
suspend-key events, and enables the CPPC initialization and adapted
`power-profiles-daemon` units. The `h7607a-hardware` package, also on every
installation, carries the UCM, the touchpad quirk, `h7607a-brightness`,
`h7607a-audio` for playback volume and a WirePlumber policy without camera
monitors, capture nodes or Bluetooth headset capture roles.
