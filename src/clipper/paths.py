"""Repo-root resolution for the Clipper Engine.

All runtime state (jobs/, dashboard/, garage/) lives at the repo root,
not next to the module that happens to read it. ``repo_root()`` finds
that root by walking up from this file for a directory containing both
``pyproject.toml`` and ``src/clipper`` (``CLIPPER_HOME`` overrides it,
and the current working directory is the last resort). This works for
a local checkout, for the Fly image (build context = repo root) and
for the Modal container (the whole engine is copied to /root/clipper).
"""
import os
from pathlib import Path


def repo_root(start=None):
    env = os.environ.get("CLIPPER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    p = Path(start or __file__).resolve()
    for parent in [p, *p.parents]:
        if (parent / "pyproject.toml").is_file() and (
            parent / "src" / "clipper"
        ).is_dir():
            return parent
    return Path.cwd()


def src_dir():
    return repo_root() / "src"


def config_dir():
    return repo_root() / "src" / "clipper" / "config"


def jobs_dir():
    return repo_root() / "jobs"


def dashboard_dir():
    return repo_root() / "dashboard"


def garage_dir():
    return repo_root() / "garage"
