"""Generate and materialize the installer source manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import stat

TREES = ('assets', 'docs', 'installer', 'locks', 'packages', 'provenance',
         'runtime', 'scripts', 'src')
FILES = ('README.md', '.gitignore')
MANIFEST = 'release.json'


def source_files(root):
    root = Path(root)
    files = [root / name for name in FILES if (root / name).is_file()]
    for name in TREES:
        files.extend(path for path in (root / name).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts
                     and path.suffix not in ('.pyc', '.pyo'))
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def seal(root):
    root = Path(root)
    rows = []
    for path in source_files(root):
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        rows.append({'path': path.relative_to(root).as_posix(),
                     'bytes': path.stat().st_size, 'sha256': digest,
                     'mode': stat.S_IMODE(path.stat().st_mode)})
    value = {'schema': 'h7607a-source-release-v1', 'files': rows,
             'excluded': ['build/', '.work/', '.artifacts/', '.git/', '__pycache__/']}
    data = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()
    (root / MANIFEST).write_bytes(data)
    return {'files': len(rows), 'bytes': sum(row['bytes'] for row in rows),
            'manifest_sha256': hashlib.sha256(data).hexdigest()}


def snapshot(root, destination):
    root, destination = Path(root), Path(destination)
    data = (root / MANIFEST).read_bytes()
    value = json.loads(data)
    destination.mkdir()
    for row in value['files']:
        path = destination / row['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / row['path'], path)
        path.chmod(row['mode'])
    (destination / MANIFEST).write_bytes(data)
    return {'files': len(value['files']), 'destination': str(destination)}
