#!/usr/bin/env python3
"""Thin CLI shim — the real code lives in src/clipper/ (clipper.infra.modal_client).

Run as: python modal_client.py ...  (same as before)
The package entry point is: python -m clipper.infra.modal_client
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from clipper.infra.modal_client import main  # noqa: E402

if __name__ == "__main__":
    main()
