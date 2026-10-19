#!/usr/bin/env bash
# Rebuild hid-asus.ko from hid-asus.c and hid-ids.h with the inputs, compiler
# and make command recorded in hid-build.json.
# Usage: hid-build.sh PACKAGE_DIRECTORY KERNEL_CONFIG
# PACKAGE_DIRECTORY holds the pinned linux-headers-7.0.0-1022-nvidia and
# linux-nvidia-7.0-headers-7.0.0-1022 debs (07-build/locked-inputs/payload/
# .artifacts/packages) and KERNEL_CONFIG is config-7.0.0-1022-nvidia from
# 01-platform/kernel. The build runs in /var/tmp/h7607a-hid-build, which must
# not exist.
# Run natively on aarch64 Linux, because the header package's build tools are
# arm64 binaries, with gcc 16.1.1 20260430, binutils, make, kmod, zstd, tar,
# gawk and diffutils. Nothing is installed or loaded.
set -euo pipefail
if (($# != 2)); then
    echo "Usage: $0 PACKAGE_DIRECTORY KERNEL_CONFIG" >&2
    exit 2
fi
packages=$(realpath -e -- "$1")
config=$(realpath -e -- "$2")
recipe=$(cd -- "$(dirname -- "$0")" && pwd)
shipped=$(realpath -e -- "$recipe/../../assets/hardware/modules/hid-asus.ko")
# The debug information records the build paths, so a fixed directory keeps
# rebuilds byte-identical.
work=/var/tmp/h7607a-hid-build
headers=$work/root/usr/src/linux-headers-7.0.0-1022-nvidia
image_deb=linux-headers-7.0.0-1022-nvidia_7.0.0-1022.23~24.04.1_arm64.deb
common_deb=linux-nvidia-7.0-headers-7.0.0-1022_7.0.0-1022.23~24.04.1_all.deb
# Inherited KCFLAGS, ARCH, CROSS_COMPILE and similar variables change the build.
clean=(env -i PATH=/usr/bin:/bin LC_ALL=C LANG=C)
[[ $(uname -m) == aarch64 ]] || { echo 'Run natively on aarch64' >&2; exit 1; }
[[ ! -e $work && ! -L $work ]] || { echo "$work must not exist" >&2; exit 1; }
[[ $("${clean[@]}" gcc --version | head -n 1) == 'gcc (GCC) 16.1.1 20260430' ]] ||
    { echo 'hid-build.json records gcc (GCC) 16.1.1 20260430' >&2; exit 1; }
# Source, Kbuild and header values come from hid-build.json; the debs and the
# configuration are pinned by 07-build/locked-inputs and 01-platform/kernel.
sha256sum --strict --check <<EOF
4639fa303f42679b361e342b7eae4f68bb478566cf84f61278587cc5c90230de  $recipe/hid-asus.c
4a4c05a345b8ddec1ebeb3f5d3e5a38edcc456f26886769a63278c19434000d8  $recipe/hid-ids.h
21e14c2bf7e4e4d9de3c1d5b82853972b3ba50344fc1492646487724078773eb  $recipe/hid.Kbuild
93847c99d2a49f9713c464bb76956dc27f6709c7e20296f8eec894632cac3921  $packages/$image_deb
650da497156cf3518debf2f326e161b598d9fd0b41ab9e717e4bc72b3f8f87c8  $packages/$common_deb
300b9d6316a1c772b9cb48eead848444e61d3d177daf74dee4a2007ac5e66667  $config
EOF
mkdir -p -- "$work/root" "$work/module" "$work/tmp"
for deb in "$image_deb" "$common_deb"; do
    ar p "$packages/$deb" data.tar.zst | tar --zstd -xf - -C "$work/root"
done
(cd -- "$headers" && sha256sum --strict --check) <<'EOF'
3702382acae6cb607b441f0a44c2f0929a9ed98b9d33dcf2f731b1a1d1469297  .config
f3fdeeaa2053f98aa80f3c0d8efd797bddec5d2dc112ed2232b16cfd720debaf  Makefile
ad63eccd8911085a10d7996b78dd0c40f6bcaa0b32d42986a9cce39e0101a7ba  Module.symvers
430df14bc7aec97db7a25388783c0a69f5d0630fb10b6ef2979e1ad99a779389  include/config/auto.conf
deb435ae4bf00fe209a66e9d146a4ccb03b82a826a0f83b85844cb6e127a0935  include/generated/autoconf.h
7e524323ec20f848311d0666a66207dc2fb595be38b03e55795493a54abf670a  include/generated/utsrelease.h
076eaff42468545fd1174e65c2e4104ba3030504b2a79576dae2453f9e6e7a25  scripts/module.lds
715118bc3b328b369e9a16f96f0dbf469dd0c873aaaa5e4937fbc9cc9361988f  include/config/kernel.release
730cb5034088c43d586658efd91cf05181d5b0becb42d7dbaae338f41e3cd0b8  include/generated/rustc_cfg
EOF
# The shipped kernel is built from KERNEL_CONFIG. It differs from the header
# package's .config only in Rust toolchain values that no C header reads.
rust='^CONFIG_(RUSTC_VERSION|RUSTC_LLVM_VERSION|RUST_IS_AVAILABLE|RUSTC_HAS_SLICE_AS_FLATTENED)='
diff <(grep -Ev "$rust" "$headers/.config") <(grep -Ev "$rust" "$config")
cp -- "$recipe/hid.Kbuild" "$work/module/Kbuild"
cp -- "$recipe/hid-asus.c" "$recipe/hid-ids.h" "$work/module/"
"${clean[@]}" TMPDIR="$work/tmp" make -j2 -C "$headers" M="$work/module" \
    CC=gcc HOSTCC=gcc modules 2>&1 | tee "$work/build.log"
built=$work/hid-asus.ko
cp -- "$work/module/hid-asus.ko" "$built"
modprobe --show-modversions "$built" | LC_ALL=C sort > "$work/imports.tsv"
modprobe --show-modversions "$shipped" | LC_ALL=C sort > "$work/shipped-imports.tsv"
[[ -s $work/imports.tsv ]] || { echo 'No versioned imports' >&2; exit 1; }
awk -F '\t' 'NR == FNR { crc[$2] = $1; next }
    crc[$2] != $1 { print "CRC differs from Module.symvers: " $2 > "/dev/stderr"; bad = 1 }
    END { exit bad }' "$headers/Module.symvers" "$work/imports.tsv"
diff -u "$work/shipped-imports.tsv" "$work/imports.tsv"
for field in name vermagic license depends alias import_ns parm; do
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
echo 'Imported CRCs match Module.symvers and the shipped module; nothing was installed or loaded.'
