#!/usr/bin/env python3
"""Fetch or verify exact artifacts listed by repository lock files."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from h7607a.artifacts import ArtifactError, fetch, load_lock, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("locks", nargs="+", type=Path)
    parser.add_argument("--cache", action="append", default=[], type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    for lock in args.locks:
        for row in load_lock(lock)["artifacts"]:
            if args.verify_only:
                verify(ROOT, row)
            else:
                fetch(ROOT, row, cache_dirs=args.cache, offline=args.offline)
            print(f"verified {row['path']}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ArtifactError, OSError, ValueError) as exc:
        sys.exit(str(exc))
