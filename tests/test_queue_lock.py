#!/usr/bin/env python3
"""Tests for item #6 — public queue lock + per-worker temp dirs +
single-writer merge (src/clipper/core/queue_io.py).

Plain asserts, no network, no pytest needed (pytest is not installed
on this VM). Run from the clipper-engine root:

    python3 tests/test_queue_lock.py

Everything runs against temp copies in a TemporaryDirectory — NEVER
against the real jobs/.../clip-queue.md. The contention test spawns
two merger processes (20 worker result files each) and checks for
lost entries, duplicate entries, and duplicate C-numbers.
"""
import multiprocessing as mp
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipper.core.queue_io import (  # noqa: E402
    merge_worker_results, parse_c_numbers, queue_lock,
    worker_job_dir, worker_temp_dir,
)

SEED = (
    "# Test queue\n\n"
    "### C1. alpha-clip.mp4 — 10.0s\n- File: `a.mp4`\n\n"
    "### C2. beta-clip.mp4 — 12.0s\n- File: `b.mp4`\n\n"
)


def _write_results(base, wid, count):
    """One worker's output: 20 result files in its OWN job dir.

    Workers write placeholder headers (``### C0. ...``) — the final
    C-number is assigned only by the single queue-writer merge.
    """
    d = worker_job_dir(base, f"w{wid}")
    files = []
    for i in range(count):
        f = d / f"result-{i:02d}.md"
        f.write_text(f"### C0. test-w{wid}-{i}.mp4 — 10.0s\n"
                     f"- File: `w{wid}-{i}.mp4`\n", encoding="utf-8")
        files.append(str(f))
    return files


def _merge_worker(queue, base, wid, count):
    files = _write_results(base, wid, count)
    merge_worker_results(queue, files)


def _lock_holder(queue, ready, release):
    with queue_lock(queue):
        ready.set()
        release.wait(timeout=10)


def test_queue_lock_is_public_and_exclusive():
    with tempfile.TemporaryDirectory() as d:
        q = Path(d) / "clip-queue.md"
        q.write_text(SEED, encoding="utf-8")
        # A second process cannot take the lock (non-blocking) while
        # this process holds it, and can once it is released.
        import fcntl
        from clipper.core.queue_io import lock_path_for
        ready = mp.Event()
        release = mp.Event()
        p = mp.Process(target=_lock_holder, args=(str(q), ready, release))
        p.start()
        assert ready.wait(timeout=10), "holder never took the lock"
        try:
            fd = os.open(lock_path_for(q), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                blocked = True
            else:
                blocked = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            assert blocked, "lock was NOT exclusive while held"
        finally:
            release.set()
            p.join(timeout=10)
        assert p.exitcode == 0, "holder crashed"
        # After release the lock is takeable again.
        with queue_lock(q):
            pass
    print("PASS test_queue_lock_is_public_and_exclusive")


def test_merge_two_writers_x20():
    with tempfile.TemporaryDirectory() as d:
        q = str(Path(d) / "clip-queue.md")
        Path(q).write_text(SEED, encoding="utf-8")
        base = Path(d) / "work"
        procs = [mp.Process(target=_merge_worker, args=(q, str(base), w, 20))
                 for w in range(2)]
        t0 = time.time()
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        assert all(p.exitcode == 0 for p in procs), "merger crashed"
        headers = [ln for ln in Path(q).read_text(encoding="utf-8").splitlines()
                   if ln.startswith("### C")]
        numbers = parse_c_numbers(q)
        new = [n for n in numbers if n > 2]
        assert len(new) == 40, f"lost entries: {len(new)}/40"
        assert len(set(new)) == 40, "duplicate C-numbers"
        assert sorted(new) == list(range(3, 43)), "non-contiguous numbers"
        assert len(headers) == 42, f"expected 42 headers, got {len(headers)}"
        # Placeholder C0 headers must all have been renumbered.
        assert not any(ln.startswith("### C0") for ln in headers), \
            "placeholder C0 header survived the merge"
        print(f"PASS test_merge_two_writers_x20 "
              f"(2x20 in {time.time() - t0:.2f}s, C3..C42, no dups)")


def test_merge_dir_input_and_empty_files():
    with tempfile.TemporaryDirectory() as d:
        q = Path(d) / "clip-queue.md"
        q.write_text(SEED, encoding="utf-8")
        base = Path(d) / "work"
        wdir = worker_job_dir(base, "solo")
        (wdir / "a.md").write_text("### C0. one.mp4 — 5.0s\n", encoding="utf-8")
        (wdir / "b.md").write_text("### C0. two.mp4 — 6.0s\n", encoding="utf-8")
        (wdir / "empty.md").write_text("   \n", encoding="utf-8")
        # A whole per-worker job dir merges in one call; the empty
        # file consumes no number.
        nums = merge_worker_results(q, [wdir])
        assert nums == [3, 4], f"expected [3, 4], got {nums}"
        assert parse_c_numbers(q) == [1, 2, 3, 4]
        text = q.read_text(encoding="utf-8")
        assert "### C3. one.mp4" in text and "### C4. two.mp4" in text
    print("PASS test_merge_dir_input_and_empty_files")


def test_worker_dirs():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "work"
        d1 = worker_job_dir(base, "w1")
        d2 = worker_job_dir(base, "w2")
        assert d1 != d2 and d1.is_dir() and d2.is_dir()
        # A hostile worker id cannot escape the base dir.
        evil = worker_job_dir(base, "../../etc")
        assert evil.is_dir() and evil.parent == base, f"escaped base: {evil}"
        assert evil.resolve().is_relative_to(base.resolve()), evil
        # Temp dir is removed on exit, even on error.
        with worker_temp_dir(base, "w1") as td:
            (td / "scratch.txt").write_text("x", encoding="utf-8")
            assert td.is_dir()
            saved = td
        assert not saved.exists(), "worker temp dir was not cleaned up"
        try:
            with worker_temp_dir(base, "w1"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert not [p for p in base.iterdir() if p.name.startswith("worker-w1-")], \
            "temp dir left behind after an error"
    print("PASS test_worker_dirs")


def main():
    test_queue_lock_is_public_and_exclusive()
    test_merge_two_writers_x20()
    test_merge_dir_input_and_empty_files()
    test_worker_dirs()
    print("\n4 passed, 0 failed of 4 tests")


if __name__ == "__main__":
    main()
