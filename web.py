#!/usr/bin/env python3
"""Thin CLI shim — the real code lives in src/clipper/ (clipper.app.web).

Run as: python web.py [--port 8080]  (same as before)
The package entry point is: python -m clipper.app.web
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from clipper.app.web import main  # noqa: E402

if __name__ == "__main__":
    main()
