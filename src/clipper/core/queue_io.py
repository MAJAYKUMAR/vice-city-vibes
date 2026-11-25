#!/usr/bin/env python3
"""Atomic, locked writes to the clip queue (and other queue markdown files).

Why it exists: the queue file (jobs/top-gta6-20261122/clip-queue.md) once
suffered a numbering collision (C75-C79 assigned twice) because C-numbers
were allocated from in-memory state and entries were appended with plain
``open(path, "a")``. Two writers could read the same max, pick the same
next number, and interleave partial lines. The hourly posting cron reads
this file, so the format is frozen — entries stay ``### C<number>. ...``
headers with POSTED markers — but the *write path* is now:

  * every write goes through a temp file in the SAME directory + fsync +
    ``os.replace`` (atomic on POSIX: readers see old-complete or
    new-complete, never partial), and
  * every read-allocate-write sequence holds an exclusive ``fcntl.flock``
    on a lockfile next to the queue (``<queue>.lock``), so concurrent
    writers serialise.

C-number allocation: ``allocate_c_number()`` parses ``### C(\\d+)`` headers
from the ON-DISK file under the lock and returns max(existing) + 1. There
is deliberately NO in-memory counter and NO persisted counter file:
max+1-under-lock is the simpler robust option, because the queue file
itself is the single source of truth — a separate counter file could
drift from the file (crash between counter bump and append, manual edits
to the queue, entries removed) and would need its own recovery logic.
max+1-under-lock cannot drift: it re-derives from the file every time,
and the lock makes the derive-and-append pair atomic. It only fails if
the queue file itself is lost or hand-edited to remove the highest
headers — in which case a counter file would be wrong too, and the
human-visible file is the thing to fix.

Usage — the race-free way to add an entry (allocate + append under ONE
lock hold; do NOT call allocate_c_number() and then atomic_append()
separately from two processes — the number is only reserved inside the
combined call)::

    from clipper.core.queue_io import append_entry

    n = append_entry(queue_path,
                     lambda n: f"### C{n}. my-clip.mp4 — 12.0s\\n"
                               f"- File: `...`\\n")

Lower-level pieces (also importable) for callers that need them::

    from clipper.core.queue_io import atomic_write, atomic_append, allocate_c_number

    atomic_write(path, content)        # temp + fsync + os.replace
    atomic_append(path, text)          # read-modify-write under the lock
    allocate_c_number(queue_path)      # max on-disk C-number + 1, under lock

Lockfile: ``<queue_path>.lock`` (e.g. ``clip-queue.md.lock``), created on
demand next to the queue. It is never deleted — deleting a lockfile while
another process holds it open would break mutual exclusion. The lock is
advisory and process-wide: it protects writers that use this module, not
a stray ``open(path, "a")`` elsewhere (see the remaining-writers list in
the item report — those still need converting).

Crash behaviour: a kill mid-write can only leave a stray temp file
(``.clip-queue.md.tmp-XXXXXX``) in the queue directory; the target is
untouched because ``os.replace`` either happened (complete new file) or
did not (complete old file). Stray temps are harmless and can be deleted.
"""
import fcntl
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "atomic_write",
    "atomic_append",
    "allocate_c_number",
    "append_entry",
    "parse_c_numbers",
    "lock_path_for",
    "queue_lock",
    "merge_worker_results",
    "worker_job_dir",
    "worker_temp_dir",
]

# Queue entries are "### C<number>. <clip>.mp4 — ..." headers; a few
# entries in the wild are bare "### C<number>" with no trailing text,
# so the number is matched with a word boundary, not a literal ".".
C_HEADER_RE = re.compile(r"^###\s+C(\d+)\b", re.MULTILINE)


def lock_path_for(path):
    """Lockfile path for a queue file: the queue path + ``.lock``."""
    return Path(path).with_suffix(Path(path).suffix + ".lock")


@contextmanager
def queue_lock(path):
    """Public queue lock: hold an exclusive flock on ``<path>.lock``.

    Blocking: a second writer waits instead of failing. The lock file
    is opened (created if missing) and closed on exit, releasing the
    lock. The file itself is never deleted (see module docstring).
    This is the same lock ``atomic_append`` / ``append_entry`` /
    ``allocate_c_number`` already held internally — use it when a
    caller needs to hold the lock across several queue operations
    (e.g. :func:`merge_worker_results`).
    """
    lock_path = lock_path_for(path)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# Private alias, kept for existing callers: identical to queue_lock.
_queue_lock = queue_lock


def worker_job_dir(base_dir, worker_id):
    """Per-worker job dir: ``<base_dir>/worker-<worker_id>/``.

    Each worker writes ONLY here (its own result files, temp files,
    partial downloads) — never directly to the shared queue. The dir
    is created if missing and returned as a ``Path``. ``worker_id``
    should be unique per worker (pid, job id, or a worker name); it
    is sanitised to ``[A-Za-z0-9_.-]`` so it can never escape the base.
    """
    safe = "".join(ch if (ch.isalnum() or ch in "_.-") else "_"
                   for ch in str(worker_id)) or "worker"
    d = Path(base_dir) / f"worker-{safe}"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def worker_temp_dir(base_dir, worker_id=None):
    """Per-worker temp dir under ``base_dir``, removed on exit.

    Yields a fresh empty directory (``<base_dir>/worker-<id>-XXXXXX``)
    for one worker's scratch work; the whole tree is deleted when the
    block exits, even on error. ``worker_id`` (when given) is only a
    name prefix — uniqueness comes from the random suffix.
    """
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    prefix = f"worker-{worker_id}-" if worker_id is not None else "worker-"
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _fsync_dir(directory):
    """Best-effort fsync of a directory so a rename survives a crash."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path, content):
    """Write ``content`` to ``path`` atomically.

    Writes to a temp file in the SAME directory as ``path``, fsyncs it,
    then ``os.replace``s it over ``path``. Readers (and a crash) only
    ever see the complete old file or the complete new file — never a
    partial write. A crash mid-write leaves at most a stray temp file
    (``.<name>.tmp-XXXXXX``) next to the target; the target is untouched.
    ``content`` is a str; the file is written UTF-8 with ``\\n`` endings.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-",
                                    dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _fsync_dir(str(parent))


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def atomic_append(path, text):
    """Append ``text`` to ``path`` without interleaving or lost entries.

    Read-modify-write under the queue lock, then ``atomic_write``: two
    concurrent appends serialise, so neither overwrites the other's
    entry and no partial line can appear. If the existing content is
    non-empty and does not end with a newline, one is inserted first so
    the appended text always starts on its own line. Creates the file
    if it does not exist yet.
    """
    path = Path(path)
    with _queue_lock(path):
        current = _read_text(path)
        if current and not current.endswith("\n"):
            current += "\n"
        atomic_write(path, current + text)


def parse_c_numbers(path):
    """All C-numbers found in ``### C<number>`` headers of ``path``.

    Read-only helper (no lock — callers that need a consistent snapshot
    should hold the lock themselves or use allocate_c_number()).
    Returns a sorted list of ints; empty when the file has no headers
    (or does not exist yet).
    """
    return sorted(int(m) for m in C_HEADER_RE.findall(_read_text(path)))


def allocate_c_number(queue_path):
    """Next free C-number for ``queue_path``: max on-disk C-number + 1.

    Parses the headers from the ON-DISK file under the lock — never from
    in-memory state. Returns 1 when the file has no C headers yet.
    NOTE: the number is only *reserved* if the caller appends its entry
    before releasing the lock. Calling this and then appending in a
    separate step from another process can still collide — use
    ``append_entry()`` for the race-free allocate-and-append pair.
    """
    queue_path = Path(queue_path)
    with _queue_lock(queue_path):
        numbers = parse_c_numbers(queue_path)
        return (max(numbers) + 1) if numbers else 1


def _append_entry_locked(queue_path, make_entry):
    """Allocate + append assuming the queue lock is ALREADY held.

    The shared body of :func:`append_entry` and
    :func:`merge_worker_results`. It must NOT re-take the lock: a
    second flock on a separately-opened fd for the same lockfile
    would block against the lock this process already holds.
    """
    numbers = parse_c_numbers(queue_path)
    n = (max(numbers) + 1) if numbers else 1
    text = make_entry(n)
    current = _read_text(queue_path)
    if current and not current.endswith("\n"):
        current += "\n"
    atomic_write(queue_path, current + text)
    return n


def append_entry(queue_path, make_entry):
    """Allocate a C-number and append its entry, atomically, under ONE lock.

    ``make_entry`` is called with the allocated number and must return
    the entry text (typically starting with ``### C{n}. ...``). The
    allocate + append happen inside a single lock hold, so two
    concurrent callers can never receive the same number or lose each
    other's entry. Returns the allocated C-number.
    """
    queue_path = Path(queue_path)
    with _queue_lock(queue_path):
        return _append_entry_locked(queue_path, make_entry)


def _expand_result_files(result_files):
    """Normalise ``merge_worker_results`` inputs to a sorted file list.

    Each item may be a result file or a per-worker job dir; a dir
    expands to its ``*.md`` files (sorted, recursive), so a worker
    that wrote several entries into its own dir merges in one call.
    """
    files = []
    for item in result_files:
        p = Path(item)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.md")))
        else:
            files.append(p)
    return files


def merge_worker_results(queue_path, result_files):
    """Merge per-worker result files into the queue, under ONE lock.

    The single queue-writer path (item #6): workers write only to
    their own job dirs (see :func:`worker_job_dir`); this function
    reads each worker's result file(s) and appends them to the queue,
    allocating a fresh C-number per file. The WHOLE merge — every
    file — happens inside a single :func:`queue_lock` hold, via the
    same allocate-and-append body :func:`append_entry` uses
    (:func:`_append_entry_locked`), so concurrent merges serialise
    with zero loss and zero duplicate C-numbers.

    Each result file is one queue entry. Its first ``### C<number>``
    header (workers write a placeholder such as ``### C0. ...``) is
    renumbered to the allocated number; a file with no C header is
    appended unchanged and still consumes (and returns) its number.
    Empty files are skipped and consume no number. Returns the list
    of allocated C-numbers, in merge order. A missing result file
    raises FileNotFoundError — the merge aborts rather than silently
    dropping a worker's entry (the lock is released, the queue is
    untouched for the not-yet-merged files).
    """
    queue_path = Path(queue_path)
    files = _expand_result_files(result_files)
    allocated = []
    with queue_lock(queue_path):
        for f in files:
            text = Path(f).read_text(encoding="utf-8")
            if not text.strip():
                continue

            def _make_entry(n, text=text):
                return C_HEADER_RE.sub(f"### C{n}", text, count=1)

            allocated.append(_append_entry_locked(queue_path, _make_entry))
    return allocated
