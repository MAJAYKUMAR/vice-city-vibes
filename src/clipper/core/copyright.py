#!/usr/bin/env python3
"""Copyright-risk check for the Clipper Engine (stage 11) — flag
copyright-muted audio, licensed-music beds, and streamer opt-outs
BEFORE a clip is approved or posted.

Why it exists: Twitch mutes VOD audio where it detects copyrighted
music, so a clip cut from a muted stretch ships with dead audio —
and a clip built on a licensed-music bed, or from a streamer who
has opted out of clipping, is a takedown waiting to happen. This
check runs at cut time in clip.py::cut_filltrack() (step 2b, right
after the safety check), mirroring safety.py: verdicts CLEAR /
REVIEW / BLOCK + a reasons list, a check_clip() function, and a
__main__ CLI. scan_vod.py does NOT run it — only the cut flow does.

What it uses (measured 2026-11-25, not assumed):
  * MEASURED anchors from the queue file: healthy posted clips have
    mean volume roughly -15 to -26 dB with "0 muted segments". The
    Twitch copyright-mute floor is -91 dB.
  * KNOWN muted cases: StableRonaldo Twitch VOD v2932442046 (5.52M
    views) — best moment at VOD 4:45:00 dropped, audio fully muted
    at -91 dB; JasonTheWeen Day 2 v2933588799 — dirt-bike crash at
    VOD 7031.5-7038.5 inside a mute block (~6733-7245s, 51
    consecutive segments at -91 dB), faster-whisper transcribed
    0 words twice.
  * GOOD clips with no speech exist and must NOT block: C25
    kai-bus-fireball (no speech, explosion audio), C27/C38 (no
    speech). So "no speech" alone never BLOCKs — at most REVIEW.
    MEASURED 2026-11-25: the sustained-bed heuristic is BROADER than
    first assumed — C25's explosion audio reads as a sustained bed
    (14/14 1s buckets in band, mean -15.7 dB) and returns REVIEW, not
    CLEAR. Steady game ambience / explosion tails can trip it too.
    That is acceptable because it only ever yields REVIEW (a worker
    look, never a lost clip), but do not claim transient-heavy audio
    reads CLEAR — it sometimes does not.
  * ffmpeg/ffprobe only (volumedetect, silencedetect, per-second
    volumedetect slices) + the faster-whisper transcript words the
    caller already has. This module NEVER transcribes, NEVER calls
    a network service, and NEVER reads an API key. There is no
    audio fingerprinting: the sustained-energy heuristic below is
    explicitly a heuristic, not a match, and it only ever yields
    REVIEW — a human looks before the clip can post.

Verdicts: CLEAR / REVIEW / BLOCK (precedence BLOCK > REVIEW > CLEAR).
  * BLOCK: mean volume <= -60 dB (muted — Twitch copyright-mute
    pattern, floor -91 dB); or >= 80% of the window silent
    (silencedetect); or the streamer is on the opt-out blocklist.
    A BLOCK verdict means: the clip may be cut for format proof,
    but it is NEVER registered as approvable in the Garage and
    NEVER posted. garage.py approve() refuses BLOCKED clips, and
    workers file BLOCK clips as Section D HELD entries in
    clip-queue.md, exactly like safety BLOCKs.
  * REVIEW: near-silent window (-60 < mean <= -45 dB) with zero
    transcribed words; or a sustained audio bed with zero speech
    (possible music — verify license); or no audio stream at all;
    or the blocklist file missing/unparseable; or the check itself
    failed. REVIEW clips need a worker look + a
    "copyright REVIEW cleared" note in the Flags/QC line before
    posting. REVIEW never blocks posting by itself.
  * CLEAR: everything else.
"""
import json
import re
import subprocess
from pathlib import Path

# Thresholds — measured anchors, see module docstring.
MUTE_MEAN_DB = -60.0      # <= this: BLOCK (Twitch mute floor is -91 dB)
NEAR_SILENT_DB = -45.0    # (-60, -45] + 0 words: REVIEW
SILENT_FRACTION_BLOCK = 0.80   # >= 80% of window silent: BLOCK
SUSTAINED_FRACTION = 0.70      # > 70% of 1s buckets within band: music bed
SUSTAINED_BAND_DB = 6.0        # buckets within +/-6 dB of the window mean
MUTE_FLOOR_DB = -91.0          # Twitch copyright-mute floor; digital
                               # silence reads as -inf from volumedetect
                               # and is clamped to this so JSON stays valid
SILENCE_NOISE_DB = -50.0       # silencedetect noise floor
MIN_BUCKETS = 3                # need >= 3 one-second buckets to judge a bed

BLOCKLIST_PATH = (Path(__file__).resolve().parents[1]
                  / "data" / "streamer_blocklist.yaml")

# Fingerprint hook — DISABLED. A future service (e.g. ACRCloud) can
# be wired here; while disabled nothing is called, no network is
# used, and no key is read. See fingerprint_check().
FINGERPRINT_ENABLED = False


def fingerprint_check(src, start, end):
    """Disabled audio-fingerprint hook (future: ACRCloud or similar).

    Returns None while FINGERPRINT_ENABLED is False — the caller
    treats None as "no fingerprint signal". If someone flips the
    flag on without wiring a provider, this raises a clear
    "not configured" error instead of silently pretending to check.
    Never call a network service from this module while disabled.
    """
    if not FINGERPRINT_ENABLED:
        return None
    raise RuntimeError(
        "fingerprinting enabled but no provider configured — wire an "
        "ACRCloud (or similar) client into fingerprint_check() first")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _parse_blocklist_text(text):
    """Minimal blocklist parser (fallback when PyYAML is unavailable).

    Understands only the documented schema: a top-level list of
    "- key: value" entries, or a "blocklist:" key holding such a
    list. Anything else raises ValueError so the caller can REVIEW
    instead of silently trusting a misparsed file.
    """
    entries, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "blocklist:":
            continue
        if line.startswith("- "):
            if cur is not None:
                entries.append(cur)
            cur = {}
            line = line[2:].strip()
            if not line:
                continue
        if cur is None:
            raise ValueError(f"unexpected line in blocklist: {raw!r}")
        if ":" not in line:
            raise ValueError(f"unexpected line in blocklist: {raw!r}")
        k, v = line.split(":", 1)
        cur[k.strip()] = v.strip().strip("\"'")
    if cur is not None:
        entries.append(cur)
    return entries


def _parse_blocklist(text):
    try:
        import yaml
    except ImportError:
        return _parse_blocklist_text(text)
    data = yaml.safe_load(text)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        entries = data.get("blocklist", [])
        if entries is None:
            return []
        if not isinstance(entries, list):
            raise ValueError("blocklist: key must hold a list")
        return entries
    raise ValueError("blocklist file must hold a list or a blocklist: key")


_blocklist_cache = {"path": None, "mtime": None, "entries": None,
                    "error": None}


def _load_blocklist():
    """Load (and cache by mtime) the streamer opt-out blocklist.

    Returns (entries, error): entries is a list of dicts, error is a
    string when the file is missing/unparseable (never raises).
    """
    import os
    path = Path(os.environ.get("CLIPPER_BLOCKLIST", BLOCKLIST_PATH))
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        return [], f"blocklist missing ({e})"
    if (_blocklist_cache["path"] == str(path)
            and _blocklist_cache["mtime"] == mtime
            and _blocklist_cache["entries"] is not None):
        return _blocklist_cache["entries"], _blocklist_cache["error"]
    try:
        entries = _parse_blocklist(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("blocklist did not parse to a list")
        entries = [e for e in entries if isinstance(e, dict)]
    except Exception as e:
        _blocklist_cache.update(path=str(path), mtime=mtime,
                                entries=[], error=f"blocklist unparseable: {e}")
        return [], _blocklist_cache["error"]
    _blocklist_cache.update(path=str(path), mtime=mtime,
                            entries=entries, error=None)
    return entries, None


def _blocklist_hit(streamer, entries):
    """Match a streamer name/handle against blocklist entries.

    Normalised (lowercased, non-alphanumerics stripped) equality
    against an entry's name/handle, or a substring match when the
    handle is >= 4 chars (e.g. handle "ishowspeed" vs channel
    "IShowSpeed"). Returns the matching entry dict, or None.
    """
    n = _norm(streamer)
    if not n:
        return None
    for e in entries:
        for key in ("name", "handle"):
            m = _norm(e.get(key))
            if m and (m == n or (len(m) >= 4 and (m in n or n in m))):
                return e
    return None


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)


def _has_audio_stream(src):
    r = _run(["ffprobe", "-v", "error", "-show_entries",
              "stream=codec_type", "-of", "json", str(src)], timeout=60)
    try:
        streams = json.loads(r.stdout).get("streams", [])
    except Exception:
        return None  # probe failed — caller REVIEWs
    return any(s.get("codec_type") == "audio" for s in streams)


def _mean_max_db(src, start, end):
    """volumedetect over [start, end). Returns (mean_db, max_db) or
    (None, None) when ffmpeg cannot measure the window."""
    r = _run(["ffmpeg", "-v", "info", "-ss", f"{start:.2f}",
              "-to", f"{end:.2f}", "-i", str(src),
              "-af", "volumedetect", "-vn", "-sn", "-dn",
              "-f", "null", "-"], timeout=120)
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?|-inf)\s*dB", r.stderr)
    x = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?|-inf)\s*dB", r.stderr)
    if not m:
        return None, None

    def _db(s):
        try:
            v = float(s)
        except ValueError:
            return MUTE_FLOOR_DB
        if v != v or v in (float("inf"), float("-inf")):
            return MUTE_FLOOR_DB
        return round(v, 1)
    return _db(m.group(1)), (_db(x.group(1)) if x else None)


def _silence(src, start, end):
    """silencedetect over [start, end). Returns (segments, fraction):
    number of silence intervals and the fraction of the window they
    cover. A trailing silence with no silence_end is closed at the
    window end."""
    r = _run(["ffmpeg", "-v", "info", "-ss", f"{start:.2f}",
              "-to", f"{end:.2f}", "-i", str(src),
              "-af", f"silencedetect=noise={SILENCE_NOISE_DB:.0f}dB:d=0.5",
              "-vn", "-sn", "-dn", "-f", "null", "-"], timeout=120)
    total, segments, open_start = 0.0, 0, None
    for line in r.stderr.splitlines():
        ms = re.search(r"silence_start:\s*([\d.]+)", line)
        me = re.search(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", line)
        if ms:
            open_start = float(ms.group(1))
        if me:
            total += float(me.group(2))
            segments += 1
            open_start = None
    if open_start is not None:
        total += max(0.0, end - max(open_start, start))
        segments += 1
    window = max(0.0, end - start)
    return segments, (round(total / window, 3) if window else 0.0)


def _second_buckets(src, start, end):
    """Per-second mean dB buckets over the window (volumedetect per
    1s slice). Digital silence in a bucket reads as the -91 dB mute
    floor. Returns a list of floats (only successful slices)."""
    import math
    buckets = []
    n = max(1, math.ceil(end - start))
    for i in range(n):
        s, e = start + i, min(start + i + 1, end)
        if e - s < 0.2:
            continue
        try:
            mean, _ = _mean_max_db(src, s, e)
        except Exception:
            continue
        if mean is not None:
            buckets.append(mean)
    return buckets


def check_clip(src, start, end, transcript_words=None, streamer=None):
    """Check a source window for copyright risk. Returns a verdict dict
    with verdict (CLEAR/REVIEW/BLOCK), mean_db, max_db,
    muted_segments, muted_fraction, word_count, sustained_bed,
    blocklist_hit and reasons. Never raises for a bad file or a bad
    blocklist — those cap at REVIEW (or BLOCK on a blocklist hit).
    Does NOT transcribe: transcript_words are the caller's words."""
    start, end = float(start), float(end)
    words = transcript_words or []
    word_count = sum(1 for w in words
                     if start <= w.get("s", 0) < end)
    reasons = []
    block = False
    review = False

    # Fingerprint hook (disabled — returns None; see fingerprint_check).
    try:
        fp = fingerprint_check(src, start, end)
    except Exception as e:
        fp = None
        review = True
        reasons.append(f"fingerprint hook misconfigured: {e}")
    if fp is not None:
        review = True
        reasons.append(f"fingerprint signal: {fp}")

    # Streamer opt-out blocklist.
    entries, blocklist_error = _load_blocklist()
    hit = _blocklist_hit(streamer, entries)
    if hit is not None:
        block = True
        reasons.append(
            f"streamer opt-out: {hit.get('name') or hit.get('handle')} "
            f"({hit.get('reason', 'no reason recorded')})"
            + (f" — evidence: {hit.get('evidence')}"
               if hit.get("evidence") else ""))
    elif blocklist_error:
        review = True
        reasons.append(f"{blocklist_error} — could not verify opt-outs")

    # Audio probe.
    mean_db, max_db = None, None
    muted_segments, muted_fraction = 0, 0.0
    sustained_bed = False
    has_audio = None
    try:
        has_audio = _has_audio_stream(src)
    except Exception as e:
        has_audio = None
        reasons.append(f"audio probe failed: {e}")
    if has_audio is None:
        review = True
        if not any("probe failed" in r for r in reasons):
            reasons.append("audio probe failed — could not verify audio")
    elif not has_audio:
        review = True
        reasons.append("no audio stream — cannot verify copyright audio")
    else:
        try:
            mean_db, max_db = _mean_max_db(src, start, end)
        except Exception as e:
            mean_db, max_db = None, None
            review = True
            reasons.append(f"volumedetect failed: {e}")
        if mean_db is None:
            review = True
            reasons.append("volumedetect gave no reading — could not "
                           "verify audio")
        else:
            try:
                muted_segments, muted_fraction = _silence(src, start, end)
            except Exception as e:
                review = True
                reasons.append(f"silencedetect failed: {e}")
            if mean_db <= MUTE_MEAN_DB:
                block = True
                reasons.append(
                    f"audio muted — Twitch copyright-mute pattern "
                    f"(mean {mean_db} dB, floor {MUTE_FLOOR_DB:.0f} dB; "
                    f"healthy posted clips measure -15 to -26 dB)")
            elif mean_db <= NEAR_SILENT_DB and word_count == 0:
                review = True
                reasons.append(
                    f"near-silent window, no speech — verify audio "
                    f"(mean {mean_db} dB)")
            if muted_fraction >= SILENT_FRACTION_BLOCK:
                block = True
                reasons.append(
                    f"{muted_fraction:.0%} of window silent — "
                    f"copyright-mute pattern")
            # Sustained-bed heuristic: only meaningful with no speech
            # and audible (not muted) audio, and only a REVIEW (never
            # a BLOCK) — it is a heuristic, not fingerprinting.
            # NOTE (measured 2026-11-25): this is broader than
            # "music only" — explosion audio with a sustained tail /
            # steady game ambience can also read as a bed (C25
            # kai-bus-fireball: 14/14 buckets in band -> REVIEW).
            # That costs a worker look, never a lost clip.
            if word_count == 0 and mean_db > MUTE_MEAN_DB:
                buckets = _second_buckets(src, start, end)
                if len(buckets) >= MIN_BUCKETS:
                    in_band = sum(1 for b in buckets
                                  if abs(b - mean_db) <= SUSTAINED_BAND_DB)
                    if in_band / len(buckets) > SUSTAINED_FRACTION:
                        sustained_bed = True
                        review = True
                        reasons.append(
                            "possible music bed — verify license "
                            f"(sustained audio energy, no speech: "
                            f"{in_band}/{len(buckets)} 1s buckets within "
                            f"+/-{SUSTAINED_BAND_DB:.0f} dB of the window "
                            f"mean; heuristic, not fingerprinting)")
    verdict = "BLOCK" if block else ("REVIEW" if review else "CLEAR")
    return {
        "verdict": verdict,
        "mean_db": mean_db,
        "max_db": max_db,
        "muted_segments": muted_segments,
        "muted_fraction": muted_fraction,
        "word_count": word_count,
        "sustained_bed": sustained_bed,
        "blocklist_hit": (hit.get("name") or hit.get("handle")
                          if hit else None),
        "reasons": reasons,
    }


if __name__ == "__main__":
    import sys
    src, s0, e0 = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    words = None
    if len(sys.argv) > 4 and Path(sys.argv[4]).exists():
        words = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
    streamer = sys.argv[5] if len(sys.argv) > 5 else None
    print(json.dumps(check_clip(src, s0, e0, words, streamer=streamer),
                     indent=1))
