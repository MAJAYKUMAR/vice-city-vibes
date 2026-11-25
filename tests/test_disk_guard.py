#!/usr/bin/env python3
"""Tests for item #9 — disk guard (src/clipper/core/disk_guard.py)
and its hooks in src/clipper/core/clip.py.

Plain asserts, no network, no pytest needed (pytest is not installed
on this VM). Run from the clipper-engine root:

    python3 tests/test_disk_guard.py

Everything runs against temp dirs — NEVER against the real jobs/
tree. The low-disk case is proven twice: once with a monkeypatched
``shutil.disk_usage`` (exact message check) and once for real, with
an absurd ``needed_bytes`` against the actual disk.
"""
import json
import os
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipper.core.disk_guard import (  # noqa: E402
    DiskSpaceError, cleanup_stale_parts, estimate_for_clip,
    preflight, startup_cleanup,
)

Usage = namedtuple("Usage", ["total", "used", "free"])


def test_estimate_for_clip():
    assert estimate_for_clip(100, 50) == 200, "source + 2x clip"
    assert estimate_for_clip(0, 0) == 0
    assert estimate_for_clip(1_000_000_000, 45_000_000) == 1_090_000_000
    try:
        estimate_for_clip(-1, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("negative size did not raise ValueError")
    print("PASS test_estimate_for_clip")


def test_preflight_monkeypatched():
    with tempfile.TemporaryDirectory() as d:
        # Low disk: free below need -> raises, with the clear message.
        with mock.patch("clipper.core.disk_guard.shutil.disk_usage",
                        return_value=Usage(10000, 9000, 1000)):
            try:
                preflight(d, 2000, what="cut pick 1 (source.mp4)")
            except DiskSpaceError as e:
                msg = str(e)
                assert "refusing cut pick 1 (source.mp4)" in msg, msg
                assert "need 2000 bytes" in msg, msg
                assert "have 1000 bytes free" in msg, msg
            else:
                raise AssertionError("low disk did not raise DiskSpaceError")
        # Sufficient disk: passes and returns the free-bytes figure.
        with mock.patch("clipper.core.disk_guard.shutil.disk_usage",
                        return_value=Usage(10000, 1000, 9000)):
            assert preflight(d, 2000) == 9000
            assert preflight(d, 9000) == 9000  # exactly enough passes
        # Default `what` falls back to the path.
        with mock.patch("clipper.core.disk_guard.shutil.disk_usage",
                        return_value=Usage(10000, 9000, 1000)):
            try:
                preflight(d, 2000)
            except DiskSpaceError as e:
                assert f"refusing {d}" in str(e), str(e)
            else:
                raise AssertionError("low disk did not raise DiskSpaceError")
    print("PASS test_preflight_monkeypatched")


def test_preflight_real_disk_absurd_need():
    # Real run, no mocking: 1 exabyte can never fit on this VM.
    with tempfile.TemporaryDirectory() as d:
        try:
            preflight(d, 10 ** 18, what="cut pick 1 (source.mp4)")
        except DiskSpaceError as e:
            assert "refusing cut pick 1 (source.mp4)" in str(e), str(e)
            assert "need 1000000000000000000 bytes" in str(e), str(e)
            assert "have" in str(e) and "bytes free" in str(e), str(e)
        else:
            raise AssertionError("absurd need did not refuse on real disk")
        # And a trivial need passes on the real disk, returning free bytes.
        free = preflight(d, 1024)
        assert isinstance(free, int) and free >= 1024, free
    print("PASS test_preflight_real_disk_absurd_need")


def test_preflight_missing_output_dir():
    # The output dir often does not exist yet (jobs/<id>/edits) —
    # the guard measures the nearest existing ancestor instead.
    with tempfile.TemporaryDirectory() as d:
        missing = Path(d) / "jobs" / "new-id" / "edits"
        assert not missing.exists()
        assert preflight(missing, 1024) >= 1024
    print("PASS test_preflight_missing_output_dir")


def _touch(path, age_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def test_cleanup_stale_parts():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        stale = root / "a.part"
        fresh = root / "b.part"
        nested_stale = root / "sub" / "c.part"
        keep = root / "keep.mp4"
        _touch(stale, age_seconds=90000)       # > 24 h: stale
        _touch(fresh, age_seconds=60)          # fresh: in-flight, keep
        _touch(nested_stale, age_seconds=90000)
        _touch(keep, age_seconds=90000)        # old but not *.part: keep
        deleted = cleanup_stale_parts(root, older_than_seconds=86400)
        assert set(deleted) == {stale, nested_stale}, f"deleted={deleted}"
        assert not stale.exists() and not nested_stale.exists()
        assert fresh.exists(), "fresh .part was deleted"
        assert keep.exists(), "non-.part file was deleted"
        # Second run finds nothing left to delete.
        assert cleanup_stale_parts(root, older_than_seconds=86400) == []
        # Missing directory is a no-op, not a crash.
        assert cleanup_stale_parts(root / "nope", 86400) == []
        # startup_cleanup wrapper uses the same rule (24 h default).
        _touch(root / "d.part", age_seconds=90000)
        assert startup_cleanup(root) == [root / "d.part"]
    print("PASS test_cleanup_stale_parts")


def test_clip_cut_preflight_hook():
    """The cut-path hook refuses before encoding when disk is short.

    Uses a sparse source file (truncate: huge logical size, ~0 bytes
    on disk) so the guard trips without a multi-GB fixture. The hook
    is the top-of-function call in cut_filltrack/cut_portrait.
    """
    from clipper.core.clip import _disk_preflight_for_cut
    with tempfile.TemporaryDirectory() as d:
        job = Path(d) / "job1"
        job.mkdir()
        src = Path(d) / "source.mp4"
        with open(src, "wb") as f:
            f.truncate(10 ** 12)  # 1 TB logical, sparse on disk
        (job / "candidates.json").write_text(json.dumps({
            "source": str(src),
            "candidates": [{"n": 1, "start": 0.0, "end": 20.0}],
        }), encoding="utf-8")
        try:
            _disk_preflight_for_cut(job, 1, start=0.0, end=20.0)
        except DiskSpaceError as e:
            assert "refusing cut pick 1" in str(e), str(e)
        else:
            raise AssertionError("hook did not refuse the 1 TB source")
        # A small real source passes the hook.
        small = Path(d) / "small.mp4"
        small.write_bytes(b"x" * 1024)
        (job / "candidates.json").write_text(json.dumps({
            "source": str(small),
            "candidates": [{"n": 1, "start": 0.0, "end": 20.0}],
        }), encoding="utf-8")
        _disk_preflight_for_cut(job, 1, start=0.0, end=20.0)
        # A job with no readable source is skipped, not crashed —
        # the cut fails naturally further down, as before.
        (job / "candidates.json").unlink()
        _disk_preflight_for_cut(job, 1, start=0.0, end=20.0)
    print("PASS test_clip_cut_preflight_hook")


def main():
    test_estimate_for_clip()
    test_preflight_monkeypatched()
    test_preflight_real_disk_absurd_need()
    test_preflight_missing_output_dir()
    test_cleanup_stale_parts()
    test_clip_cut_preflight_hook()
    print("\n6 passed, 0 failed of 6 tests")


if __name__ == "__main__":
    main()
