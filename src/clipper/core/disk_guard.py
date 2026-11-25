#!/usr/bin/env python3
"""Pre-flight disk-space guard + stale ``*.part`` cleanup (item #9).

Why it exists: this VM is small and near-full, and a cut that runs out
of disk mid-encode corrupts downstream work (a half-written clip, a
queue entry pointing at it). So before any download/encode the caller
checks free space against the estimated need — source size + 2x the
clip size — and aborts instead of starting. Downloads go to ``*.part``
files; a stale one (older than one run) is deleted on startup so a
crashed run's partial downloads do not accumulate.

API (frozen — a second worker imports these names, do not rename)::

    from clipper.core.disk_guard import (
        DiskSpaceError, preflight, estimate_for_clip, cleanup_stale_parts,
    )

    needed = estimate_for_clip(source_bytes, clip_bytes)  # source + 2x clip
    free = preflight(output_dir, needed)  # raises DiskSpaceError if short
    deleted = cleanup_stale_parts(jobs_dir, older_than_seconds=86400)

``preflight`` returns the free-bytes figure when there is room. The
error message is meant to be read by a human in a log: ``"refusing
<what>: need X, have Y free"``.
"""
import os
import shutil
import time
from pathlib import Path

__all__ = [
    "DiskSpaceError",
    "preflight",
    "estimate_for_clip",
    "cleanup_stale_parts",
    "startup_cleanup",
]

# Clip-size estimate used by the cut-path pre-flight in clip.py: the
# 1080x1920 master is capped at ~12 Mbps (YouTube's published ladder
# for 48-60 fps; 8 Mbps at 24-30 fps), so 12 Mbps is the conservative
# per-second figure. Kept here so every caller estimates the same way.
CLIP_BYTES_PER_SECOND = 1_500_000  # 12 Mbps


class DiskSpaceError(OSError):
    """Raised by :func:`preflight` when free disk is below the need.

    Subclasses OSError so callers that already catch OS errors still
    catch it; the message carries the refusal in plain language.
    """


def estimate_for_clip(source_bytes, clip_bytes):
    """Estimated disk need for a cut: source size + 2x clip size.

    The 2x clip factor covers the encode output plus its temp/partial
    file (and, for the scout path, the downloaded source alongside the
    finished clip). Both inputs must be non-negative ints.
    """
    source_bytes = int(source_bytes)
    clip_bytes = int(clip_bytes)
    if source_bytes < 0 or clip_bytes < 0:
        raise ValueError("estimate_for_clip: sizes must be non-negative")
    return source_bytes + 2 * clip_bytes


def estimate_clip_bytes(duration_seconds):
    """Clip-size estimate (bytes) for a clip of ``duration_seconds``.

    Duration x :data:`CLIP_BYTES_PER_SECOND` (12 Mbps ceiling).
    """
    return int(float(duration_seconds) * CLIP_BYTES_PER_SECOND)


def _existing_ancestor(path):
    """Nearest existing ancestor of ``path`` (itself if it exists).

    ``shutil.disk_usage`` needs an existing path, but the pre-flight
    target is often an output directory that does not exist yet
    (e.g. ``jobs/<id>/edits``). Walking up to the nearest existing
    ancestor measures the same filesystem.
    """
    p = Path(path)
    while not p.exists():
        parent = p.parent
        if parent == p:  # reached the filesystem root without a hit
            return p
        p = parent
    return p


def preflight(path, needed_bytes, what=None):
    """Abort (raise :class:`DiskSpaceError`) if free space < need.

    ``path`` is the directory the output will land in (or the source
    file — the nearest existing ancestor is measured). ``needed_bytes``
    is the estimated need, typically from :func:`estimate_for_clip`.
    ``what`` names the refused operation in the message (defaults to
    the path); the message is always::

        refusing <what>: need <N> bytes, have <free> bytes free

    Returns the free-bytes figure when there is room.
    """
    needed_bytes = int(needed_bytes)
    if needed_bytes < 0:
        raise ValueError("preflight: needed_bytes must be non-negative")
    target = _existing_ancestor(path)
    usage = shutil.disk_usage(str(target))
    free = usage.free
    if free < needed_bytes:
        label = str(what) if what is not None else str(path)
        raise DiskSpaceError(
            f"refusing {label}: need {needed_bytes} bytes, "
            f"have {free} bytes free (on {target})"
        )
    return free


def cleanup_stale_parts(directory, older_than_seconds, recursive=True):
    """Delete ``*.part`` files older than ``older_than_seconds``.

    ``directory`` is the tree downloads land in (typically the jobs
    dir — ``*.part`` files from a crashed yt-dlp run sit in
    ``jobs/<video-id>/...``). Only regular files matching ``*.part``
    whose mtime is older than ``now - older_than_seconds`` are
    removed; fresh partial downloads (an in-flight run's) are kept.
    Returns the list of deleted paths (``pathlib.Path`` objects,
    sorted). A missing directory returns ``[]``; a file that
    disappears mid-cleanup is skipped, never fatal.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    cutoff = time.time() - float(older_than_seconds)
    pattern = "**/*.part" if recursive else "*.part"
    deleted = []
    for part in sorted(directory.glob(pattern)):
        try:
            if not part.is_file():
                continue
            if part.stat().st_mtime < cutoff:
                part.unlink()
                deleted.append(part)
        except OSError:
            continue
    return deleted


def startup_cleanup(directory, older_than_seconds=86400):
    """Startup stale-part cleanup: ``*.part`` older than one run.

    Thin wrapper over :func:`cleanup_stale_parts` with the default
    threshold (24 h — longer than any single scan/cut run, so an
    in-flight download is never deleted). Called best-effort from
    startup paths (``clip.main`` / ``clip.run_moments``); returns the
    list deleted. Callers should wrap it in try/except — it must
    never crash a run.
    """
    return cleanup_stale_parts(directory, older_than_seconds)
