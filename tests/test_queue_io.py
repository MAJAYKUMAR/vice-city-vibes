#!/usr/bin/env python3
"""Tests for src/clipper/core/queue_io.py — atomic queue writes +
idempotent C-numbers.

Plain asserts, no network, no pytest needed (pytest is not installed
on this VM). Run from the clipper-engine root:

    python3 tests/test_queue_io.py

Everything runs against temp copies in a TemporaryDirectory — NEVER
against the real jobs/.../clip-queue.md. The concurrent test spawns
two writer processes (20 allocations each) and checks for lost entries,
duplicate entries, and duplicate C-numbers; the crash test SIGKILLs a
writer mid-append and checks the target only ever holds complete
old/new content.
"""
import multiprocessing as mp
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipper.core.queue_io import (  # noqa: E402
    allocate_c_number, append_entry, atomic_append, atomic_write,
    parse_c_numbers,
)

HEADER_RE = re.compile(r"^### C\d+\. .+\.mp4 — .+$")
BARE_HEADER_RE = re.compile(r"^### C\d+$")

SEED = (
    "# Test queue\n\n"
    "### C1. alpha-clip.mp4 — 10.0s\n- File: `a.mp4`\n\n"
    "### C2. beta-clip.mp4 — 12.0s\n- File: `b.mp4`\n\n"
)


def _headers(path):
    text = Path(path).read_text(encoding="utf-8")
    headers = [ln for ln in text.splitlines() if ln.startswith("### C")]
    bad = [ln for ln in headers
           if not (HEADER_RE.match(ln) or BARE_HEADER_RE.match(ln))]
    return headers, bad


def _worker(queue, wid, count):
    for i in range(count):
        append_entry(queue, lambda n, wid=wid, i=i:
                     f"### C{n}. test-w{wid}-{i}.mp4 — 10.0s\n"
                     f"- File: `w{wid}-{i}.mp4`\n")


def test_allocate_from_disk():
    with tempfile.TemporaryDirectory() as d:
        q = Path(d) / "clip-queue.md"
        assert allocate_c_number(q) == 1, "empty/missing file -> C1"
        q.write_text(SEED, encoding="utf-8")
        assert parse_c_numbers(q) == [1, 2]
        assert allocate_c_number(q) == 3, "max on disk is C2 -> C3"
        # Bare headers (no ". name" suffix) count too.
        atomic_append(q, "### C9\n")
        assert allocate_c_number(q) == 10
    print("PASS test_allocate_from_disk")


def test_atomic_write_and_append():
    with tempfile.TemporaryDirectory() as d:
        q = Path(d) / "clip-queue.md"
        atomic_write(q, SEED)
        assert q.read_text(encoding="utf-8") == SEED
        atomic_append(q, "### C3. gamma-clip.mp4 — 9.0s\n")
        assert parse_c_numbers(q) == [1, 2, 3]
        # No trailing newline on existing content: append still starts
        # on its own line.
        q.write_text(SEED.rstrip("\n"), encoding="utf-8")
        atomic_append(q, "### C3. gamma-clip.mp4 — 9.0s\n")
        _, bad = _headers(q)
        assert not bad, f"malformed headers: {bad}"
        # No stray temp files left behind.
        assert not [p for p in Path(d).iterdir() if ".tmp-" in p.name]
    print("PASS test_atomic_write_and_append")


def test_concurrent_allocations():
    with tempfile.TemporaryDirectory() as d:
        q = str(Path(d) / "clip-queue.md")
        Path(q).write_text(SEED, encoding="utf-8")
        procs = [mp.Process(target=_worker, args=(q, w, 20))
                 for w in range(2)]
        t0 = time.time()
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        assert all(p.exitcode == 0 for p in procs), "worker crashed"
        headers, bad = _headers(q)
        numbers = parse_c_numbers(q)
        new = [n for n in numbers if n > 2]
        assert len(new) == 40, f"lost entries: {len(new)}/40"
        assert len(set(new)) == 40, "duplicate C-numbers"
        assert sorted(new) == list(range(3, 43)), "non-contiguous numbers"
        assert not bad, f"malformed headers: {bad}"
        assert len(headers) == 42, f"expected 42 headers, got {len(headers)}"
        print(f"PASS test_concurrent_allocations "
              f"(2x20 in {time.time() - t0:.2f}s, C3..C42, no dups)")


def _crash_writer(queue):
    import itertools
    big = "x" * 200_000  # ~200KB per entry -> write takes measurable ms
    for i in itertools.count():
        atomic_append(queue,
                      f"### C{9000 + i}. crash-{i}.mp4 — 10.0s\n{big}\n")


def test_crash_mid_write():
    with tempfile.TemporaryDirectory() as d:
        q = Path(d) / "clip-queue.md"
        q.write_text(SEED, encoding="utf-8")
        p = subprocess.Popen([sys.executable, __file__, "crashwriter",
                              str(q)])
        time.sleep(0.5)
        p.send_signal(signal.SIGKILL)
        p.wait()
        after = q.read_text(encoding="utf-8")
        assert after.startswith(SEED), "original content damaged by kill"
        assert after.endswith("\n"), "torn last line"
        _, bad = _headers(q)
        assert not bad, f"malformed headers after kill: {bad}"
        n = append_entry(q, lambda n: f"### C{n}. after-crash.mp4 — 5.0s\n")
        assert n == max(parse_c_numbers(q)), "numbering did not continue"
    print("PASS test_crash_mid_write")


def main():
    test_allocate_from_disk()
    test_atomic_write_and_append()
    test_concurrent_allocations()
    test_crash_mid_write()
    print("\n4 passed, 0 failed of 4 tests")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "crashwriter":
        _crash_writer(sys.argv[2])
    else:
        main()
