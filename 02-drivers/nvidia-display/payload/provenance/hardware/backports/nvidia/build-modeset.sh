#!/bin/bash
# Native AArch64 only. Builds one module in a disposable directory; never installs.
set -euo pipefail
if (($# != 6)); then
    echo "Usage: $0 NVIDIA_ARCHIVE PR1368_PATCH STOCK_NVIDIA_ZST STOCK_MODESET_ZST HEADERS BUILD_DIRECTORY" >&2
    exit 2
fi
archive=$(realpath "$1")
patch_file=$(realpath "$2")
stock_nvidia=$(realpath "$3")
stock_modeset=$(realpath "$4")
headers=$(realpath "$5")
work=$(realpath -m "$6")
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
[[ $(uname -m) == aarch64 ]]
[[ ! -e $work ]] || { echo 'Build directory must not exist' >&2; exit 1; }
printf '%s  %s\n' eac0ecd52facab28ad2963cf7e03ae8d2289d596ad590c703b5f72d65899e2bf "$archive" | sha256sum -c -
printf '%s  %s\n' 54c6946021f3e49ca4dc9744898b6f7691ab7191592f76e4abe14cb1a4c0c03e "$patch_file" | sha256sum -c -
printf '%s  %s\n' e38370575be91aa186ee7c18b8d7e81435ae7825740304c7295d7eb9f8a4b661 "$stock_nvidia" | sha256sum -c -
printf '%s  %s\n' f390076f7cec4a8fc7079dff8fddd7607b2d82a33b97219767d59cf98ea2475e "$stock_modeset" | sha256sum -c -
[[ $(cat "$headers/include/config/kernel.release") == 7.0.0-1022-nvidia ]]
mkdir -p "$work/source"
tar -xf "$archive" --strip-components=1 -C "$work/source"
cp "$script_dir/module_exports.py" "$work/"
zstd -dc "$stock_nvidia" > "$work/stock-nvidia.ko"
zstd -dc "$stock_modeset" > "$work/stock-modeset.ko"
python "$work/module_exports.py" "$work/stock-nvidia.ko" --symvers > "$work/stock-nvidia.symvers"
cd "$work/source"
patch --fuzz=0 -p1 < "$patch_file"
export LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=1791388800
export KBUILD_BUILD_TIMESTAMP='Wed Oct 7 16:00:00 UTC 2026'
export KBUILD_BUILD_USER=h7607a KBUILD_BUILD_HOST=builder
make -j3 -C src/nvidia-modeset TARGET_ARCH=aarch64 CC=gcc LD=ld \
    'DATE=date -u -d @1791388800' NV_BUILD_USER=h7607a NV_BUILD_HOST=builder
ln -s ../../src/nvidia-modeset/_out/Linux_aarch64/nv-modeset-kernel.o kernel-open/nvidia-modeset/nv-modeset-kernel.o_binary
make -j3 -C kernel-open NV_KERNEL_MODULES=nvidia-modeset SYSSRC="$headers" \
    KERNEL_UNAME=7.0.0-1022-nvidia KBUILD_EXTRA_SYMBOLS="$work/stock-nvidia.symvers" \
    CC=gcc LD=ld IGNORE_CC_MISMATCH=1 modules
candidate="$work/source/kernel-open/nvidia-modeset.ko"
python "$work/module_exports.py" "$candidate" > "$work/candidate-exports.json"
python "$work/module_exports.py" "$work/stock-modeset.ko" > "$work/stock-exports.json"
cmp "$work/stock-exports.json" "$work/candidate-exports.json"
modprobe --show-modversions "$candidate" | sort > "$work/candidate-imports.tsv"
modprobe --show-modversions "$work/stock-modeset.ko" | sort > "$work/stock-imports.tsv"
cmp "$work/stock-imports.tsv" "$work/candidate-imports.tsv"
for field in version vermagic depends license parm; do
    [[ $(modinfo -F "$field" "$candidate") == "$(modinfo -F "$field" "$work/stock-modeset.ko")" ]]
done
cp "$candidate" "$work/nvidia-modeset.ko"
zstd -T1 -19 -f "$work/nvidia-modeset.ko" -o "$work/nvidia-modeset.ko.zst"
sha256sum "$work/nvidia-modeset.ko" "$work/nvidia-modeset.ko.zst"
modinfo "$work/nvidia-modeset.ko" > "$work/modinfo.txt"
echo 'MODESET BUILD AND ABI CHECKS PASS; HARDWARE RUNTIME NOT TESTED'
