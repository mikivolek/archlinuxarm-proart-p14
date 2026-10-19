#!/usr/bin/env bash
# Stage a completed build as a normal kernel/modules/generated-headers overlay.
# Usage: package.sh WORK DEST; DEST must not exist. Also emits DEST.tar.zst.
# The PKGBUILD installs the pinned Ubuntu header packages before this overlay.
set -euo pipefail
umask 022
[[ $# == 2 ]] || { echo "Usage: $0 WORK DEST" >&2; exit 2; }
jobs=${JOBS:-$(nproc)}
[[ $jobs =~ ^[1-9][0-9]*$ ]]
work=$(realpath -e -- "$1")
dest=$(realpath -m -- "$2")
[[ ! -e $dest && ! -L $dest && ! -e $dest.tar.zst ]]
src=$work/linux
build=$work/build
krel=7.0.0-1022-nvidia
[[ $(cat "$build/include/config/kernel.release") == "$krel" ]]
for file in arch/arm64/boot/Image .config Module.symvers System.map modules.order \
    modules.builtin modules.builtin.modinfo certs/signing_key.pem certs/signing_key.x509; do
    [[ -s $build/$file ]]
done
mkdir -p -- "$dest"
export KBUILD_BUILD_USER=h7607a KBUILD_BUILD_HOST=builder
export KBUILD_BUILD_TIMESTAMP='2026-10-19 00:00:00 UTC'
unset KBUILD_GENDWARFKSYMS_STABLE
# Standard kbuild installs, strips, then signs modules using the key trusted by
# this Image. Keep their BTF sections; compression happens after signing.
# Also handle a completed source build unpacked directly from Ubuntu's diff.
chmod 755 "$src/debian/scripts/sign-module"
make -C "$src" O="$build" -j"$jobs" ARCH=arm64 CC=aarch64-linux-gnu-gcc-13 \
    HOSTCC=aarch64-linux-gnu-gcc-13 KERNELRELEASE="$krel" \
    KBUILD_BUILD_VERSION='23~24.04.1' CONFIG_DEBUG_SECTION_MISMATCH=y \
    CFLAGS_MODULE=-DPKG_ABI=1022 INSTALL_MOD_PATH="$dest" INSTALL_MOD_STRIP=1 \
    DEPMOD=true modules_install
mod=$dest/lib/modules/$krel
rm -f -- "$mod/build" "$mod/source"
expected_modules=$(wc -l < "$build/modules.order")
installed_modules=$(find "$mod/kernel" -type f -name '*.ko' -printf '.' | wc -c)
[[ $installed_modules == "$expected_modules" && $installed_modules -gt 0 ]]
# kmod's PKCS#7 sig_key field contains the signing certificate's serial.
# Check every non-staging module; Ubuntu intentionally excludes staging drivers
# from signing unless they occur in its signature-inclusion list.
cert_serial=$(openssl x509 -inform DER -in "$build/certs/signing_key.x509" \
    -noout -serial | sed 's/^serial=//; s/^0*//' | tr 'A-F' 'a-f')
[[ -n $cert_serial ]]
module_count=$(find "$mod/kernel" -type f -name '*.ko' \
    ! -path '*/drivers/staging/*' -printf '.' | wc -c)
[[ $module_count -gt 0 ]]
find "$mod/kernel" -type f -name '*.ko' ! -path '*/drivers/staging/*' -print0 |
    xargs -0 -r -n 128 modinfo -F sig_key |
    awk -v serial="$cert_serial" -v count="$module_count" '
        { gsub(/:/, ""); key = tolower($0); sub(/^0*/, "", key);
          if (key != serial) { print "Module signing key differs" > "/dev/stderr"; exit 1 } }
        END { if (NR != count) { print "Module signature count differs" > "/dev/stderr"; exit 1 } }'
openssl x509 -inform DER -in "$build/certs/signing_key.x509" \
    -noout -subject -sha256 -fingerprint
find "$mod/kernel" -type f -name '*.ko' -exec zstd -q -3 --rm -- {} +
depmod -b "$dest" -F "$build/System.map" "$krel"
mkdir -p -- "$dest/usr/lib"
mv -- "$dest/lib/modules" "$dest/usr/lib/modules"
rmdir -- "$dest/lib"
mod=$dest/usr/lib/modules/$krel
install -Dm644 "$build/arch/arm64/boot/Image" "$dest/boot/vmlinuz-linux-n1x"
install -m644 "$build/arch/arm64/boot/Image" "$mod/vmlinuz"
install -m644 "$build/.config" "$mod/config"
install -m644 "$build/System.map" "$mod/System.map"
printf '%s\n' linux-n1x-nvidia-bin > "$mod/pkgbase"
headers=$dest/usr/src/linux-headers-$krel
common=$dest/usr/src/linux-nvidia-7.0-headers-7.0.0-1022
mkdir -p -- "$headers/include" "$headers/arch/arm64/include"
cp -a -- "$build/include/config" "$build/include/generated" "$headers/include/"
cp -a -- "$build/arch/arm64/include/generated" "$headers/arch/arm64/include/"
# Build command dependency files contain host paths and are not header inputs.
find "$headers" -type f -name '*.cmd' -delete
install -m644 "$build/.config" "$headers/.config"
install -m644 "$build/Module.symvers" "$headers/Module.symvers"
install -Dm644 "$src/include/linux/acpi.h" "$common/include/linux/acpi.h"
# Future external modules need the matching BTF base, rather than the old one.
objcopy --strip-debug "$build/vmlinux" "$headers/vmlinux"
chmod 644 "$headers/vmlinux"
# The private signing key remains exclusively in the build directory.
[[ -z $(find "$dest" -type f -name '*.pem' -print -quit) ]]
tar --sort=name --mtime='2026-10-19 00:00:00 UTC' --owner=0 --group=0 \
    --numeric-owner -C "$dest" -cf - boot usr | zstd -q -3 -o "$dest.tar.zst"
sha256sum "$dest.tar.zst"
