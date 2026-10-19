#!/usr/bin/env bash
# Build the pinned Ubuntu/NVIDIA kernel with the ACPI allocation patch.
# Usage: build.sh WORK [JOBS]; optional SOURCE_CACHE contains the three inputs.
# Run natively in Ubuntu 24.04 with gcc-13, make, bc, bison, flex, libssl-dev,
# libelf-dev, libdw-dev, zlib1g-dev, pkg-config, pahole, curl, patch, kmod,
# xz-utils, zstd and gawk.
# The kernel generates a private module-signing key in WORK/build/certs.
set -euo pipefail
umask 022
[[ $# -ge 1 && $# -le 2 ]] || { echo "Usage: $0 WORK [JOBS]" >&2; exit 2; }
[[ $(uname -s) == Linux && $(uname -m) == aarch64 ]]
recipe=$(cd -- "$(dirname -- "$0")" && pwd)
work=$(realpath -m -- "$1")
jobs=${2:-$(nproc)}
[[ $jobs =~ ^[1-9][0-9]*$ && ! -e $work && ! -L $work ]]
cc=aarch64-linux-gnu-gcc-13
[[ $($cc -dumpfullversion) == 13.3.0 ]]
[[ $($cc --version | head -n 1) == 'aarch64-linux-gnu-gcc-13 (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0' ]]
linker=$($cc -print-prog-name=ld)
[[ $($linker --version | head -n 1) == 'GNU ld (GNU Binutils for Ubuntu) 2.42' ]]
[[ $(pahole --version) == v1.25 ]]
for command in make bc bison flex openssl pkg-config curl patch gzip xz zstd gawk \
    tar dpkg-deb sha256sum; do
    command -v "$command" >/dev/null
done
mkdir -p -- "$work/inputs" "$work/linux" "$work/build"
base=https://ports.ubuntu.com/ubuntu-ports/pool/main/l/linux-nvidia-7.0
orig=linux-nvidia-7.0_7.0.0.orig.tar.gz
delta=linux-nvidia-7.0_7.0.0-1022.23~24.04.1.diff.gz
modules=linux-modules-7.0.0-1022-nvidia_7.0.0-1022.23~24.04.1_arm64.deb
for name in "$orig" "$delta" "$modules"; do
    if [[ -n ${SOURCE_CACHE:-} && -f $SOURCE_CACHE/$name ]]; then
        cp --reflink=auto -- "$SOURCE_CACHE/$name" "$work/inputs/$name"
    else
        curl --fail --location --retry 3 --proto '=https' \
            --output "$work/inputs/$name" "$base/$name"
    fi
done
cp -- "$recipe/0001-arm64-bound-acpi-override-allocation.patch" "$work/inputs/allocator.patch"
cat > "$work/inputs/SHA256SUMS" <<'EOF'
c6343795c8c7e4feff55f5b309365c8e0eaf49cd6abcb412e32315313f149536  linux-nvidia-7.0_7.0.0.orig.tar.gz
6dbf6661592f7b6053253eea6b5411e21974627d9b76d4d3e105653c243d7718  linux-nvidia-7.0_7.0.0-1022.23~24.04.1.diff.gz
3cb8bac5ed8ba7385f652240f9154691153f5ae86a3313d324f1e28c89aec126  linux-modules-7.0.0-1022-nvidia_7.0.0-1022.23~24.04.1_arm64.deb
0ababbaed19c7108e8564916fac1b8fecc44010e7f3e4b419c34d01c66049e19  allocator.patch
EOF
(cd -- "$work/inputs" && sha256sum --check SHA256SUMS)
dpkg-query -W > "$work/build-environment.tsv"
tar -xzf "$work/inputs/$orig" --strip-components=1 -C "$work/linux"
gzip -dc "$work/inputs/$delta" | patch --batch --fuzz=0 -p1 -d "$work/linux"
patch --batch --fuzz=0 -p1 -d "$work/linux" < "$work/inputs/allocator.patch"
# Debian's source diff cannot preserve executable modes for newly added files.
chmod 755 "$work/linux/debian/scripts/sign-module"
dpkg-deb --fsys-tarfile "$work/inputs/$modules" |
    tar -xOf - ./boot/config-7.0.0-1022-nvidia > "$work/original.config"
cp -- "$work/original.config" "$work/build/.config"
export KBUILD_BUILD_USER=h7607a KBUILD_BUILD_HOST=builder
export KBUILD_BUILD_TIMESTAMP='2026-10-19 00:00:00 UTC'
unset KBUILD_GENDWARFKSYMS_STABLE
make_args=(-C "$work/linux" O="$work/build" ARCH=arm64
    CC="$cc" HOSTCC="$cc" KERNELRELEASE=7.0.0-1022-nvidia)
make "${make_args[@]}" olddefconfig
# These four capability keys change when the builder lacks Rust. The original
# kernel does not enable CONFIG_RUST; every other configuration value must match.
for config in original.config build/.config; do
    sed -E '/^CONFIG_(RUSTC_VERSION|RUSTC_LLVM_VERSION|RUST_IS_AVAILABLE|RUSTC_HAS_SLICE_AS_FLATTENED)=/d' \
        "$work/$config" > "$work/$(basename "$config").normalized"
done
diff -u "$work/original.config.normalized" "$work/.config.normalized"
! grep -qx 'CONFIG_RUST=y' "$work/build/.config"
make "${make_args[@]}" -j"$jobs" KBUILD_BUILD_VERSION='23~24.04.1' \
    CONFIG_DEBUG_SECTION_MISMATCH=y CFLAGS_MODULE=-DPKG_ABI=1022 Image modules
sha256sum "$work/build/arch/arm64/boot/Image" "$work/build/.config" \
    "$work/build/Module.symvers" > "$work/build-outputs.sha256"
printf 'Built Image and matching modules in %s/build\n' "$work"
