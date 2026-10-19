# MediaTek SoundWire interrupt gate repair

This isolated build changes only `soundwire-mtk.ko` for `7.0.0-1022-nvidia`.
It preserves the separately qualified `mt8901-acp-sdw-legacy` machine driver,
speaker identity, firmware and userspace audio configuration.

Source: NVIDIA `NV-Kernels` commit `2234cd7f8f8a9fdbceef37f82c531016aab8cf73`.
The fourteen exact source files, immutable URLs and SHA256 values are in
`source-receipt.json`. The repair is Ryan Hughes's October 10 downstream
patch, SHA256
`0251766c52add0dfb792616dd93244aa242f406dd7ff8b20eec043b3fe50fa47`. This is
attributed external source, not handwritten code or a merged mainline backport.

The driver previously closed its interrupt gate after draining its IRQ
handler and status work. The work could reenable slave interrupts while the
driver was disabling them, causing an attachment interrupt storm and a boot
hang. The patch closes the global gate first, retains IRQ synchronization
and work cancellation, masks slave interrupts even while processing is
disabled, and rate limits slave-status messages. SoundWire commands use
register polling, so work completion does not require the closed gate.

Run `build.sh` inside an aarch64 Linux environment with the exact 1022
headers, installed stock module, GCC 16.1.1, binutils, make, Python, patch
and kmod. It verifies input/header hashes and builds only six source
objects. It never installs or loads a module. Native build warnings are
recorded: compiler differs from the original Ubuntu GCC13 build; an
existing clock helper is unused; optional BTF cannot be generated without
vmlinux/pahole. No warnings were hidden.

`check-native.py` checks all 89 imported symbol CRCs against both the exact
kernel `Module.symvers` and stock module. Names, dependency set, modalias
and module parameters match. It checks the stripped artifact again.

An external Kbuild rebuild initially produced a different pristine
`srcversion` because `sumversion.c` selects local dependencies by their
directory paths: external `./object.o` plus bare local headers omit the
headers from the calculation. Recreating the in-tree dependency path
layout and invoking the unmodified kernel `modpost` reproduces the stock
checksum **B0EEB455101BC5E89D2E1B0** exactly. This establishes that the
source baseline matches the shipped module, rather than assuming the
different checksum is harmless. This normalization is audit-only and does
not modify the shipped artifact.

`build-receipt.json` records the exact artifact identity and checks.
The patched `srcversion` is **7588DC9A1BF7B049DA48615**. Compilation and ABI
checks passed. Physical boot/audio behavior with this new module remains
unverified; the patch repairs a credible race and does not claim all audio
or suspend behavior is proven. The exact pinned NVIDIA source and three
live NVIDIA branch tips examined still lacked this downstream repair.
