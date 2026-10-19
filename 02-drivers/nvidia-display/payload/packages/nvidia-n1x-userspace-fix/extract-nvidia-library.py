"""Extract the ARM RMAPI library and license without executing NVIDIA's .run.

Usage: python extract-nvidia-library.py DRIVER.run OUTPUT_DIRECTORY
The calling PKGBUILD verifies SHA256 first. This also checks NVIDIA's embedded
download checksum and the Zstandard stream before accepting the extraction.
"""
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tarfile

source, output = Path(sys.argv[1]), Path(sys.argv[2])
output.mkdir(parents=True, exist_ok=True)
with source.open('rb') as handle:
    header = handle.read(20000).decode('utf-8', 'replace')
skip = int(re.search(r'^skip=(\d+)$', header, re.M)[1])
expected_md5 = re.search(r'^MD5=([0-9a-f]{32})$', header, re.M)[1]
with source.open('rb') as handle:
    for _ in range(3):
        handle.readline()
    actual_md5 = hashlib.file_digest(handle, 'md5').hexdigest()
if expected_md5 != actual_md5:
    raise SystemExit('NVIDIA embedded download checksum failed')
with source.open('rb') as handle:
    for _ in range(skip - 1):
        handle.readline()
    offset = handle.tell()
found = set()
with source.open('rb', buffering=0) as handle:
    handle.seek(offset)
    process = subprocess.Popen(['zstd', '-dc'], stdin=handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                name = Path(member.name).name
                if member.isfile() and name not in found and member.name.removeprefix('./') in {'libnvidia-rmapi-tegra.so.615.78.08', 'LICENSE'}:
                    if member.size > 64 * 1024 * 1024:
                        raise RuntimeError('Unexpected artifact size')
                    (output / name).write_bytes(archive.extractfile(member).read())
                    found.add(name)
        errors = process.stderr.read().decode()
        if process.wait() != 0:
            raise RuntimeError(errors)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
if found != {'libnvidia-rmapi-tegra.so.615.78.08', 'LICENSE'}:
    raise SystemExit(f'Missing NVIDIA source artifacts: {found}')
library = (output / 'libnvidia-rmapi-tegra.so.615.78.08').read_bytes()
if library[:6] != b'\x7fELF\x02\x01' or int.from_bytes(library[18:20], 'little') != 183:
    raise SystemExit('Expected a 64-bit little-endian AArch64 ELF library')
print('PASS: extracted AArch64 RMAPI library and license; installer was not executed')
