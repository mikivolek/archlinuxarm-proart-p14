#!/usr/bin/env bash
# Rebuild nvidia-drm.ko from drm-kernel-open-source.tar.gz and drm.patch with
# the inputs, compiler and environment recorded in drm-build.json.
# Usage: drm-build.sh DRM_PATCH PACKAGE_DIRECTORY KERNEL_CONFIG
# DRM_PATCH is drm.patch (02-drivers/nvidia-display/patches/provenance/hardware),
# PACKAGE_DIRECTORY holds the pinned linux-headers-7.0.0-1022-nvidia and
# linux-nvidia-7.0-headers-7.0.0-1022 debs (07-build/locked-inputs/payload/
# .artifacts/packages) and KERNEL_CONFIG is config-7.0.0-1022-nvidia from
# 01-platform/kernel. The build runs in /var/tmp/h7607a-drm-build, which must
# not exist.
# Run natively on aarch64 Linux, because the header package's build tools are
# arm64 binaries, with gcc 16.1.1 20260430, binutils, make, kmod, zstd, tar,
# gzip, patch, python, gawk and diffutils. Nothing is installed or loaded.
# drm-build.json does not record the make command. The one below is the
# kernel-open build of build-modeset.sh limited to nvidia-drm and without
# IGNORE_CC_MISMATCH, which drm-build.json reports as unset, so conftest warns
# that the compiler differs from the kernel's. Run at the workspace and header
# paths drm-build.json records, it reproduces that receipt's module byte for byte.
set -euo pipefail
if (($# != 3)); then
    echo "Usage: $0 DRM_PATCH PACKAGE_DIRECTORY KERNEL_CONFIG" >&2
    exit 2
fi
patch_file=$(realpath -e -- "$1")
packages=$(realpath -e -- "$2")
config=$(realpath -e -- "$3")
recipe=$(cd -- "$(dirname -- "$0")" && pwd)
modeset=$(realpath -e -- "$recipe/../../assets/build-inputs/nvidia-modeset.ko.zst")
shipped=$(realpath -e -- "$recipe/../../assets/hardware/modules/nvidia-drm.ko")
# The debug information records the build paths, so a fixed directory keeps
# rebuilds byte-identical.
work=/var/tmp/h7607a-drm-build
headers=$work/root/usr/src/linux-headers-7.0.0-1022-nvidia
image_deb=linux-headers-7.0.0-1022-nvidia_7.0.0-1022.23~24.04.1_arm64.deb
common_deb=linux-nvidia-7.0-headers-7.0.0-1022_7.0.0-1022.23~24.04.1_all.deb
# Inherited KCFLAGS, ARCH, CROSS_COMPILE and similar variables change the build.
clean=(env -i PATH=/usr/bin:/bin LC_ALL=C LANG=C)
[[ $(uname -m) == aarch64 ]] || { echo 'Run natively on aarch64' >&2; exit 1; }
[[ ! -e $work && ! -L $work ]] || { echo "$work must not exist" >&2; exit 1; }
[[ $("${clean[@]}" gcc --version | head -n 1) == 'gcc (GCC) 16.1.1 20260430' ]] ||
    { echo 'drm-build.json records gcc (GCC) 16.1.1 20260430' >&2; exit 1; }
# The NVIDIA Kbuild adds this Tegra tree's headers and symbols when it exists.
[[ ! -e /usr/src/nvidia/nvidia-public ]] ||
    { echo '/usr/src/nvidia/nvidia-public would change the build' >&2; exit 1; }
# Patch, manifest, header and nvidia-modeset values come from drm-build.json,
# the module_exports.py value from native-build-receipt.json; the archive, debs
# and configuration are pinned by component.yaml files.
sha256sum --strict --check <<EOF
2cae1578e998735229b0b26056bb33ad9fad11eab2b925bcff00ecb22939c2e7  $patch_file
7f687f195288b9b252b6037abf228c983ee698846490f1268eab809b59258916  $recipe/drm-kernel-open-source.tar.gz
79ee35083684ed95cedb9b209ba4453558cc522918c188264610cee1056b8ce2  $recipe/drm-source-manifest.json
f390076f7cec4a8fc7079dff8fddd7607b2d82a33b97219767d59cf98ea2475e  $modeset
206949fd128faa50e39460a4457cafcb1efcaee34109ff340b41f36d5fed893c  $recipe/backports/nvidia/module_exports.py
93847c99d2a49f9713c464bb76956dc27f6709c7e20296f8eec894632cac3921  $packages/$image_deb
650da497156cf3518debf2f326e161b598d9fd0b41ab9e717e4bc72b3f8f87c8  $packages/$common_deb
300b9d6316a1c772b9cb48eead848444e61d3d177daf74dee4a2007ac5e66667  $config
EOF
mkdir -p -- "$work/root" "$work/kernel-open" "$work/tmp"
for deb in "$image_deb" "$common_deb"; do
    ar p "$packages/$deb" data.tar.zst | tar --zstd -xf - -C "$work/root"
done
(cd -- "$headers" && sha256sum --strict --check) <<'EOF'
3702382acae6cb607b441f0a44c2f0929a9ed98b9d33dcf2f731b1a1d1469297  .config
ad63eccd8911085a10d7996b78dd0c40f6bcaa0b32d42986a9cce39e0101a7ba  Module.symvers
f3fdeeaa2053f98aa80f3c0d8efd797bddec5d2dc112ed2232b16cfd720debaf  Makefile
430df14bc7aec97db7a25388783c0a69f5d0630fb10b6ef2979e1ad99a779389  include/config/auto.conf
deb435ae4bf00fe209a66e9d146a4ccb03b82a826a0f83b85844cb6e127a0935  include/generated/autoconf.h
7e524323ec20f848311d0666a66207dc2fb595be38b03e55795493a54abf670a  include/generated/utsrelease.h
076eaff42468545fd1174e65c2e4104ba3030504b2a79576dae2453f9e6e7a25  scripts/module.lds
51a59cedb5d5e7973caea9c2cb72462e2d4920f7b78d3e420a72f70af4d47274  include/generated/compile.h
EOF
# The shipped kernel is built from KERNEL_CONFIG. It differs from the header
# package's .config only in Rust toolchain values that no C header reads.
rust='^CONFIG_(RUSTC_VERSION|RUSTC_LLVM_VERSION|RUST_IS_AVAILABLE|RUSTC_HAS_SLICE_AS_FLATTENED)='
diff <(grep -Ev "$rust" "$headers/.config") <(grep -Ev "$rust" "$config")
tar -xzf "$recipe/drm-kernel-open-source.tar.gz" -C "$work/kernel-open"
python3 -I - "$work/kernel-open" "$recipe/drm-source-manifest.json" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
files = json.loads(pathlib.Path(sys.argv[2]).read_text())['files']
found = {str(path.relative_to(root)) for path in root.rglob('*') if not path.is_dir()}
assert found == files.keys(), sorted(found ^ files.keys())
for name, digest in sorted(files.items()):
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
print(f'{len(files)} source files match drm-source-manifest.json')
PY
patch --batch --fuzz=0 -p1 -d "$work" < "$patch_file"
zstd -q -dc "$modeset" > "$work/nvidia-modeset.ko"
# modpost copies the third column into depends, so it must name nvidia-modeset.
python3 -I "$recipe/backports/nvidia/module_exports.py" "$work/nvidia-modeset.ko" --symvers |
    awk 'BEGIN { FS = OFS = "\t" } { $3 = "nvidia-modeset"; print }' > "$work/nvidia-modeset.symvers"
"${clean[@]}" TMPDIR="$work/tmp" make -j"$(nproc)" -C "$work/kernel-open" \
    NV_KERNEL_MODULES=nvidia-drm SYSSRC="$headers" KERNEL_UNAME=7.0.0-1022-nvidia \
    KBUILD_EXTRA_SYMBOLS="$work/nvidia-modeset.symvers" CC=gcc LD=ld modules 2>&1 |
    tee "$work/build.log"
built=$work/nvidia-drm.ko
cp -- "$work/kernel-open/nvidia-drm.ko" "$built"
modprobe --show-modversions "$built" | LC_ALL=C sort > "$work/imports.tsv"
modprobe --show-modversions "$shipped" | LC_ALL=C sort > "$work/shipped-imports.tsv"
[[ -s $work/imports.tsv ]] || { echo 'No versioned imports' >&2; exit 1; }
cat "$headers/Module.symvers" "$work/nvidia-modeset.symvers" |
    awk -F '\t' 'NR == FNR { crc[$2] = $1; next }
        crc[$2] != $1 { print "CRC differs from Module.symvers: " $2 > "/dev/stderr"; bad = 1 }
        END { exit bad }' - "$work/imports.tsv"
diff -u "$work/shipped-imports.tsv" "$work/imports.tsv"
for field in name version vermagic license depends alias import_ns parm; do
    [[ $(modinfo -F "$field" "$built") == "$(modinfo -F "$field" "$shipped")" ]] ||
        { echo "modinfo $field differs from the shipped module" >&2; exit 1; }
done
sha=$(sha256sum < "$built")
shipped_sha=$(sha256sum < "$shipped")
srcversion=$(modinfo -F srcversion "$built")
shipped_srcversion=$(modinfo -F srcversion "$shipped")
printf '%s\n  sha256      %s\n  srcversion  %s\n' "$built" "${sha%% *}" "$srcversion"
if [[ $sha == "$shipped_sha" ]]; then
    echo 'The sha256 matches the shipped module.'
else
    printf 'The sha256 differs from the shipped module (%s). Byte identity needs the same toolchain and the same build paths, which the debug information records.\n' "${shipped_sha%% *}"
fi
if [[ $srcversion == "$shipped_srcversion" ]]; then
    echo 'The srcversion matches the shipped module.'
else
    printf 'The srcversion differs from the shipped module (%s).\n' "$shipped_srcversion"
fi
echo 'Imported CRCs match Module.symvers, nvidia-modeset and the shipped module; nothing was installed or loaded.'
