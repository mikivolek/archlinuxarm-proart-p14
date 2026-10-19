#!/usr/bin/env python3
"""Run inside the exact-header native build root; never installs a module."""
import hashlib
import json
import pathlib
import re
import shutil
import subprocess

ROOT = pathlib.Path('/var/tmp/h7607a-soundwire-backport')
HEAD = pathlib.Path('/usr/src/linux-headers-7.0.0-1022-nvidia')
REL = pathlib.Path('drivers/soundwire/mediatek')
BUILT = ROOT / 'source' / REL / 'soundwire-mtk.ko'
STOCK = pathlib.Path('/usr/lib/modules/7.0.0-1022-nvidia/kernel/drivers/soundwire/mediatek/soundwire-mtk.ko.zst')

def run(*args, **kw):
    return subprocess.check_output(args, text=True, **kw).strip()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def info(path):
    fields = ('name', 'vermagic', 'srcversion', 'depends', 'alias', 'parm')
    return {f: run('modinfo', '-F', f, str(path)) for f in fields}

def versions(path):
    return {v.split()[1]: v.split()[0].lower() for v in run('modprobe', '--show-modversions', str(path)).splitlines()}

stock_info = info(STOCK)
patched_info = info(BUILT)
baseline_info = info(ROOT / 'baseline' / REL / 'soundwire-mtk.ko')
assert stock_info['name'] == patched_info['name'] == 'soundwire_mtk'
assert stock_info['vermagic'] == patched_info['vermagic']
assert stock_info['depends'] == patched_info['depends']
assert stock_info['alias'] == patched_info['alias']
assert stock_info['parm'] == patched_info['parm']
sv = versions(STOCK)
pv = versions(BUILT)
symvers = {line.split()[1]: line.split()[0].lower() for line in (HEAD / 'Module.symvers').read_text().splitlines()}
assert all(symvers.get(symbol) == crc for symbol, crc in pv.items())
assert all(pv[symbol] == sv[symbol] for symbol in pv.keys() & sv.keys())

# External Kbuild's './object.o' and bare local-header paths cause sumversion.c
# to omit local headers. Recreate only the dependency path layout used by an
# in-tree build, then let the unmodified kernel modpost compute its checksum.
# This is independent source provenance evidence, not the shipped module.
normal = ROOT / 'baseline-in-tree-srcversion'
shutil.copytree(ROOT / 'baseline', normal, dirs_exist_ok=True)
module = normal / REL
for p in module.glob('.*.o.cmd'):
    lines = []
    for line in p.read_text().splitlines():
        if line.startswith('source_'):
            key, value = line.split(' := ', 1)
            line = key + ' := ' + str(REL / value)
        elif re.fullmatch(r'\s+[\w.-]+\.[ch] \\', line):
            line = '  ' + str(REL / line.strip().removesuffix(' \\')) + ' \\'
        lines.append(line)
    p.write_text('\n'.join(lines) + '\n')
(module / 'soundwire-mtk.mod').write_text(''.join(str(REL / p.name) + '\n' for p in (module / 'soundwire-mtk.mod').read_text().splitlines() for p in [pathlib.Path(p)]))
(normal / 'modules.order').write_text(str(REL / 'soundwire-mtk.o') + '\n')
run(str(HEAD / 'scripts/mod/modpost'), '-M', '-m', '-b', '-x', '-a', '-o', 'Module.symvers', '-n', '-T', 'modules.order', '-i', str(HEAD / 'Module.symvers'), '-e', cwd=normal)
computed = re.search(r'MODULE_INFO\(srcversion, "([A-F0-9]+)"\)', (module / 'soundwire-mtk.mod.c').read_text()).group(1)

output = ROOT / 'soundwire-mtk.ko'
shutil.copy2(BUILT, output)
subprocess.run(['strip', '--strip-debug', str(output)], check=True)
assert info(output) == patched_info
assert versions(output) == pv
receipt = {
    'native_build': True,
    'kernel_release': '7.0.0-1022-nvidia',
    'compiler': run('gcc', '--version').splitlines()[0],
    'stock': stock_info,
    'patched': patched_info,
    'baseline_external': baseline_info,
    'baseline_intree_dependency_srcversion': computed,
    'baseline_intree_matches_stock': computed == stock_info['srcversion'],
    'abi': {
        'imported_symbol_count': len(pv),
        'all_import_crcs_match_exact_header_symvers': True,
        'all_shared_import_crcs_match_stock': True,
        'added_imports': sorted(pv.keys() - sv.keys()),
        'removed_imports': sorted(sv.keys() - pv.keys()),
        'exact_header_config_sha256': sha(HEAD / '.config'),
        'exact_header_symvers_sha256': sha(HEAD / 'Module.symvers'),
    },
    'artifact': {'path': str(output), 'sha256': sha(output), 'bytes': output.stat().st_size},
    'physical_tested': False,
}
(ROOT / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))
assert computed == stock_info['srcversion'], 'Pristine source checksum still differs after in-tree dependency normalization'
