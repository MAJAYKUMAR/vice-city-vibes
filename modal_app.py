#!/usr/bin/env python3
"""Thin Modal shim — the real cutter lives in src/clipper/api/modal_standalone.py.

Deploy exactly as before: modal deploy modal_app.py --profile maddukuriajaykumar05
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from clipper.api.modal_standalone import app  # noqa: E402,F401  (Modal deploy looks for `app`)
