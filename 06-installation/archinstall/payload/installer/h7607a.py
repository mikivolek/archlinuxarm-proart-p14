#!/usr/bin/env python3
"""Start the pinned, normally interactive Arch Linux ARM installer."""
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from h7607a.archinstall_adapter import main

if __name__ == "__main__":
    raise SystemExit(main(REPO))
