#!/usr/bin/env python3
"""Thin CLI shim — the real code lives in src/clipper/ (clipper.core.clip).

Run as: python clip.py scout|cut|manual|hype|export|polish|safety|scenes ...  (same as before)
The package entry point is: python -m clipper.core.clip
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from clipper.core.clip import main  # noqa: E402

if __name__ == "__main__":
    main()
