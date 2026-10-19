#!/usr/bin/env python3
"""Check retained module ABI against rebuilt stock exports before packaging."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile


def sections(blob):
    assert blob[:6] == b'\x7fELF\x02\x01', 'Expected ELF64 little endian'
    h = struct.unpack_from('<16sHHIQQQIHHHHHH', blob)
    assert h[2] == 183, 'Expected AArch64 module'
    rows = [struct.unpack_from('<IIQQQQIIQQ', blob, h[6] + i * h[11]) for i in range(h[12])]
    strings = blob[rows[h[13]][4]:rows[h[13]][4] + rows[h[13]][5]]
    return {strings[r[0]:strings.index(b'\0', r[0])].decode(): blob[r[4]:r[4] + r[5]] for r in rows}


def imports(data):
    table = sections(data)
    legacy, extended = {}, {}
    if '__versions' in table:
        raw = table['__versions']; assert len(raw) % 64 == 0
        for i in range(0, len(raw), 64):
            crc, name = struct.unpack_from('<Q56s', raw, i)
            name = name.split(b'\0', 1)[0].decode()
            assert name and name not in legacy
            legacy[name] = crc
    if '__version_ext_names' in table or '__version_ext_crcs' in table:
        assert '__version_ext_names' in table and '__version_ext_crcs' in table, \
            'Extended version names and CRCs must both be present'
        names, raw = table['__version_ext_names'], table['__version_ext_crcs']
        assert len(raw) % 4 == 0, 'Extended version CRC is truncated'
        # Like kernel/module/version.c, count u32 CRCs, not NUL bytes.
        # modpost emits a C string containing explicit per-name terminators;
        # its implicit final terminator is padding, not another symbol.
        offset = 0
        for i in range(len(raw) // 4):
            end = names.find(b'\0', offset)
            assert end > offset, 'Extended version name is missing or unterminated'
            name = names[offset:end].decode()
            assert name not in extended, 'Duplicate extended version name'
            extended[name] = struct.unpack_from('<I', raw, 4 * i)[0]
            offset = end + 1
        assert not names[offset:].strip(b'\0'), 'Extended version names exceed CRC count'
    if legacy and extended:
        # modpost omits names >=56 bytes from the legacy format. The loader
        # prefers the complete extended table; shared entries must still agree.
        assert all(extended.get(name) == crc for name, crc in legacy.items()), \
            'Legacy and extended version tables disagree'
    assert legacy or extended, 'Module has no versioned imports'
    assert not ({'.BTF', '.BTF.base'} & table.keys()), 'Retained module needs matching BTF rebuild'
    return extended or legacy


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('symvers', type=Path)
    ap.add_argument('modules', nargs='+', type=Path)
    args = ap.parse_args()
    spec = importlib.util.spec_from_file_location('module_exports', Path(__file__).resolve().parents[1] / 'nvidia/module_exports.py')
    api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
    providers = {}
    for line in args.symvers.read_text().splitlines():
        crc, name, owner, *_ = line.split()
        assert name not in providers or providers[name] == int(crc, 16), 'Conflicting stock export'
        providers[name] = int(crc, 16)
    assert providers, 'Empty rebuilt kernel export table'
    modules = []
    with tempfile.TemporaryDirectory(prefix='h7607a-abi-') as temporary:
        for index, path in enumerate(args.modules):
            raw = subprocess.check_output(['zstd', '-q', '-dc', str(path)]) if path.suffix == '.zst' else path.read_bytes()
            target = Path(temporary) / f'{index}.ko'; target.write_bytes(raw)
            required = imports(raw)
            try:
                exported = api.exports(target)
            except AssertionError as exc:
                if str(exc) != 'No exported CRCs':
                    raise
                exported = {}
            for name, row in exported.items():
                crc = int(row['crc'], 16)
                assert name not in providers or providers[name] == crc, 'Conflicting retained module export: ' + name
                providers[name] = crc
            modules.append((path, raw, required))
        result = []
        for path, raw, required in modules:
            mismatch = {name: {'module': hex(crc), 'rebuilt': hex(providers[name]) if name in providers else None}
                        for name, crc in required.items() if providers.get(name) != crc}
            assert not mismatch, f'{path.name}: incompatible imported symbols: {mismatch}'
            result.append({'module': path.name, 'uncompressed_sha256': hashlib.sha256(raw).hexdigest(),
                           'imports_checked': len(required), 'all_import_crcs_match': True,
                           'has_coupled_btf': False})
    print(json.dumps({'rebuilt_symvers_sha256': hashlib.sha256(args.symvers.read_bytes()).hexdigest(),
                      'modules': result, 'all_passed': True}, indent=2))


if __name__ == '__main__':
    main()
