#!/usr/bin/env python3
"""Thin CLI shim — the real code lives in src/clipper/ (clipper.app.watcher).

Run as: python watcher.py [--once]  (same as before)
The package entry point is: python -m clipper.app.watcher
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from clipper.app.watcher import main  # noqa: E402

if __name__ == "__main__":
    main()
