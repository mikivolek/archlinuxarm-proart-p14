#!/usr/bin/env python3
"""Generate a source manifest or materialize its source tree."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from h7607a import release

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('action', choices=['seal', 'snapshot'])
parser.add_argument('--destination', type=Path)
args = parser.parse_args()
if args.action == 'snapshot':
    if args.destination is None:
        parser.error('snapshot requires --destination')
    result = release.snapshot(ROOT, args.destination)
else:
    result = release.seal(ROOT)
print(json.dumps(result, indent=2))
