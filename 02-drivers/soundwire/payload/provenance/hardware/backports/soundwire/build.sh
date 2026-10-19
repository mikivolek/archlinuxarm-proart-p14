#!/usr/bin/env bash
# Run natively in aarch64 Linux with the exact 1022 headers and GCC16.1.1.
set -euo pipefail
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
work=/var/tmp/h7607a-soundwire-backport
headers=/usr/src/linux-headers-7.0.0-1022-nvidia
test "$(uname -m)" = aarch64
test ! -e "$work"
python3 - "$src" "$headers" <<'PY'
import hashlib, json, pathlib, sys
source, headers = map(pathlib.Path, sys.argv[1:])
receipt = json.loads((source/'source-receipt.json').read_text())
for row in receipt['source_files']:
    data = (source/'pristine'/row['path']).read_bytes()
    assert len(data) == row['bytes']
    assert hashlib.sha256(data).hexdigest() == row['sha256']
patch = (source/'0001-soundwire-close-interrupt-gate.patch').read_bytes()
assert hashlib.sha256(patch).hexdigest() == receipt['patch']['sha256']
assert hashlib.sha256((headers/'.config').read_bytes()).hexdigest() == '3702382acae6cb607b441f0a44c2f0929a9ed98b9d33dcf2f731b1a1d1469297'
assert hashlib.sha256((headers/'Module.symvers').read_bytes()).hexdigest() == 'ad63eccd8911085a10d7996b78dd0c40f6bcaa0b32d42986a9cce39e0101a7ba'
PY
mkdir -p "$work"
cp -a "$src/pristine" "$work/source"
cp -a "$src/pristine" "$work/baseline"
patch --batch --fuzz=0 -d "$work/source" -p1 < "$src/0001-soundwire-close-interrupt-gate.patch"
for tree in source baseline; do
    printf '\nccflags-y += -I$(src)/..\n' >> "$work/$tree/drivers/soundwire/mediatek/Makefile"
    make -C "$headers" M="$work/$tree/drivers/soundwire/mediatek" CC=gcc \
        CONFIG_SOUNDWIRE_MTK=m CONFIG_SOUNDWIRE_MTK_MT8901_CLK=y -j4 modules \
        > "$work/$tree-build.log" 2>&1
done
python3 "$src/check-native.py"
printf 'Built and ABI checked: %s/soundwire-mtk.ko\n' "$work"
