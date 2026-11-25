"""Reaction / moment-ranking signals ported from OpenClip into the clipper engine.

Ported by reading (never executed, built, installed, imported, or copied
from) /tmp/open-clip-editor (MIT licensed). Each function cites its source
file in its docstring. These are ADDITIONAL score terms: they sit alongside
the existing chat-spike ratio in scripts/scan_vod.py and the loud/hype/heat/
chat score in src/clipper/infra/gtavi/moments.py — they never replace the
chat signal, and every term is capped so no single cue dominates.

Sources
-------
- server/moments.mjs  — cue regexes, cueSums() prefix sums, categoryScores()
  formulas, loudness() ratio-to-median / peak-vs-p90.
- server/signals.mjs  — gameKeeps(): silent sustained-action ranges to keep.
- workers/signals.py  — per-second on-screen action from keyframe change
  (32x18 grey frames, 7 s median, lift over a 5-min rolling baseline, 0-3).
  NOTE: that file uses PyAV, which is NOT installed in this environment
  (``import av`` -> ModuleNotFoundError, checked 2026-11-25), so the same
  math is implemented here with ffmpeg tiny-frame extraction instead.
- workers/hotspots.py — SURVEYED ONLY for the loudness-baseline idea
  (per-second dB vs a rolling 5-min median, lift clipped 0-18/6, peak vs
  p90 clipped 0-12/6). Its choose() region picker was NOT ported.

Usage (moments.py): reaction_series() + loudness_lift_peak() + action_series()
Usage (scan_vod.py): window_reaction_bonus() per candidate window — see the
  hook note in that function; scan_vod.py itself was NOT edited here because
  another build was actively editing it (mtime 2026-11-25 20:46 UTC).
"""
import math
import re
import subprocess

import numpy as np

# ---------------------------------------------------------------------------
# Cue regexes — port of server/moments.mjs (laugh / strong / weak / clipCue /
# hook). Tested against a single word, except strong/clipCue which are also
# tested against the "previous word + word" pair, exactly as cueSums() does.
# ---------------------------------------------------------------------------
LAUGH = re.compile(
    r"\b(?:ha(?:ha)+h?|hah|lol|lmao|lmfao|hehe+|laugh\w*|i'?m dead|dying)\b|\(laugh",
    re.I,
)
STRONG = re.compile(
    r"\b(?:let'?s go+|no way|oh my god|oh my gosh|omg|insane|holy|we did it|"
    r"unbelievable|clutch|what the)\b",
    re.I,
)
WEAK = re.compile(r"\b(?:crazy|wow|bro|dude|yo+|wait|yes+|hell no|nah)\b", re.I)
CLIP_CUE = re.compile(r"\b(?:clip (?:that|it|this)|clipped|that'?s a clip)\b", re.I)
HOOK = re.compile(
    r"\b(?:how|why|stop|mistake|secret|first|never|always|what|here|because|guess|watch)\b",
    re.I,
)

# "hell no" inside WEAK can never match a single word (the source tests weak
# on one word only), so it is inert here too — faithfully preserved, not fixed.
# Likewise "what the" / "no way" only ever match via the word-pair test.

# Weights used when wiring these into moments.py's raw score. Each term is a
# 0..1 normalised component, so the largest single addition is 0.5 and the
# theoretical all-max total is ~2.6 — comparable to the existing loud (1.0)
# and chat (1.5) terms, and no one cue can dominate the Hype Meter stars().
W_FUNNY = 0.5
W_HIGHLIGHT = 0.5
W_CLIPPABLE = 0.3
W_LOUD_LIFT = 0.5
W_LOUD_PEAK = 0.3
W_ACTION = 0.5

ACTION_KEEP = 2.5  # server/signals.mjs: sustained action >= 2.5 counts as a keep


def _word_text(w):
    return (w.get("w") or w.get("text") or "") if isinstance(w, dict) else str(w)


def _word_start(w):
    return float(w.get("s", w.get("start", 0.0))) if isinstance(w, dict) else 0.0


def _word_end(w):
    return float(w.get("e", w.get("end", _word_start(w)))) if isinstance(w, dict) else 0.0


class CueSums:
    """Prefix sums of reaction cues per word — port of cueSums() in
    server/moments.mjs. Any word range [a, b) is then scored in O(1) via
    range(kind, a, b). ``words`` are our transcript dicts ({"w","s","e"}) or
    OpenClip-style dicts ({"text","start","end"}); both are accepted."""

    KINDS = ("laugh", "strong", "weak", "clip", "exclaim")

    # Faithful quirk, preserved not fixed: the source tests strong/clip on
    # the word AND on the "prev + word" pair per word (OR), so a word after
    # a strong word can itself count — e.g. "insane no way" scores strong=3
    # ("insane", pair "was insane", pair "insane no"). Verified in tests.

    def __init__(self, words):
        self.words = list(words or [])
        self.sums = {k: [0] for k in self.KINDS}
        for i, w in enumerate(self.words):
            t = _word_text(w)
            prev = _word_text(self.words[i - 1]) if i else ""
            pair = f"{prev} {t}"
            self.sums["laugh"].append(self.sums["laugh"][i] + (1 if LAUGH.search(t) else 0))
            self.sums["strong"].append(
                self.sums["strong"][i] + (1 if (STRONG.search(t) or STRONG.search(pair)) else 0)
            )
            self.sums["weak"].append(self.sums["weak"][i] + (1 if WEAK.search(t) else 0))
            self.sums["clip"].append(self.sums["clip"][i] + (1 if CLIP_CUE.search(pair) else 0))
            self.sums["exclaim"].append(
                self.sums["exclaim"][i] + (1 if re.search(r"[!?]$", t) else 0)
            )

    def range(self, kind, a, b):
        """Cue count for words[a:b] (b exclusive, like the source's [a, b))."""
        s = self.sums[kind]
        a = max(0, min(a, len(s) - 1))
        b = max(0, min(b, len(s) - 1))
        return s[b] - s[a]

    def word_index_at(self, t):
        """First word index with start >= t (binary search over starts)."""
        import bisect

        starts = [ _word_start(w) for w in self.words ]
        return bisect.bisect_left(starts, t)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def category_scores(cues, i, j, ratio, peak, opening, density):
    """Category scores — exact port of categoryScores() in server/moments.mjs.

    ``i``/``j`` are word indices, INCLUSIVE on both ends (the source calls
    range(kind, i, j+1)). ``ratio``/``peak`` come from loudness(), ``opening``
    is the window's opening text, ``density`` its word density. Caps
    (min(3, ...), min(4, ...), min(1, ...), clamp(ratio-1, 0, 2)) are the
    source's own: no cue dominates its category.
    """
    return {
        "funny": 22 * min(3, cues.range("laugh", i, j + 1))
        + 5 * min(4, cues.range("weak", i, j + 1))
        + 8 * _clamp(ratio - 1, 0, 2),
        "highlight": 14 * min(3, cues.range("strong", i, j + 1))
        + 18 * _clamp(peak - 1, 0, 1.5)
        + 10 * _clamp(ratio - 1, 0, 2)
        + 3 * min(4, cues.range("exclaim", i, j + 1)),
        "clippable": 30 * min(1, cues.range("clip", i, j + 1))
        + (14 if HOOK.search(opening or "") else 0)
        + (8 if "?" in (opening or "") else 0)
        + min(12, density * 4),
    }


# ---------------------------------------------------------------------------
# Loudness — server/moments.mjs loudness() + workers/hotspots.py score_seconds()
# ---------------------------------------------------------------------------
def rolling_median(x, window=301):
    """Rolling median with a 5-minute window — port of rolling_median() in
    workers/hotspots.py (median of every 5th step, interpolated; a short
    series just gets its global median)."""
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return x
    if len(x) < window:
        return np.full(len(x), float(np.median(x)))
    half = window // 2
    pad = np.pad(x, (half, half), mode="edge")
    step = 5
    centers = np.arange(0, len(x), step)
    med = np.array([np.median(pad[c : c + window]) for c in centers])
    return np.interp(np.arange(len(x)), centers, med)


def loudness_lift_peak(db):
    """Per-second loudness lift vs the rolling 5-min median baseline, from
    the dB series moments.py already computes (audio_rms) — the
    workers/hotspots.py idea, no new audio decode needed.

    lift = clip(dB - rolling_median(dB), 0, 18) / 6   (0..3)
    peak = clip(dB - p90(dB), 0, 12) / 6              (0..2)
    Returns (lift, peak) arrays; both all-zero when the series is empty.
    """
    db = np.asarray(db, dtype=float)
    if len(db) == 0:
        return np.zeros(0), np.zeros(0)
    lift = np.clip(db - rolling_median(db), 0, 18) / 6.0
    peak = np.clip(db - np.percentile(db, 90), 0, 12) / 6.0
    return lift, peak


def loudness_ratio_peak(db, a, b):
    """Ratio-to-stream-median and peak-vs-p90 over seconds [a, b) — port of
    loudness().at(a, b) in server/moments.mjs. The source works on linear
    0.5 s RMS bins; our series is per-second dB, so it is converted back to
    linear RMS (10**(dB/20)) first — same ratio/peak math, adapted units.
    Returns {"ratio": float, "peak": float}; 1.0/1.0 when unavailable."""
    db = np.asarray(db, dtype=float)
    if len(db) == 0:
        return {"ratio": 1.0, "peak": 1.0}
    rms = 10.0 ** (db / 20.0)
    median = float(np.median(rms))
    p90 = float(np.percentile(rms, 90))
    i = max(0, min(int(a), len(rms) - 1))
    j = max(i + 1, min(int(math.ceil(b)), len(rms)))
    seg = rms[i:j]
    return {
        "ratio": float(seg.mean() / median) if median > 0 else 1.0,
        "peak": float(seg.max() / p90) if p90 > 0 else 1.0,
    }


# ---------------------------------------------------------------------------
# Per-second reaction series (for moments.py) + per-window bonus (scan_vod.py)
# ---------------------------------------------------------------------------
def _p99norm(a):
    a = np.asarray(a, dtype=float)
    scale = float(np.percentile(a, 99)) if len(a) else 0.0
    if scale < 1e-9:
        return np.zeros_like(a)
    return np.clip(a / scale, 0, 1)


def reaction_series(words, n, db=None, window_before=2.0, window_after=8.0):
    """Per-second funny / highlight / clippable arrays over ``n`` seconds.

    For each second t, the cue window is words in [t-before, t+after]
    (10 s, scored in O(1) from CueSums), the loudness ratio/peak come from
    loudness_ratio_peak(db) over the same window, ``opening`` is the first
    ~40 chars of window text and ``density`` is words-per-second in it.
    Returns a dict of raw arrays plus 0..1 p99-normalised arrays
    ("funny_n", "highlight_n", "clippable_n"), mirroring moments.py's
    _p99norm() convention. All zeros when there are no words.
    """
    cues = CueSums(words)
    funny = np.zeros(n)
    highlight = np.zeros(n)
    clippable = np.zeros(n)
    if not cues.words:
        return {"funny": funny, "highlight": highlight, "clippable": clippable,
                "funny_n": funny, "highlight_n": highlight, "clippable_n": clippable,
                "cues": cues}
    for t in range(n):
        i = cues.word_index_at(t - window_before)
        j = cues.word_index_at(t + window_after) - 1
        if j < i:
            continue
        lp = loudness_ratio_peak(db, t - window_before, t + window_after) if db is not None else {"ratio": 1.0, "peak": 1.0}
        text = " ".join(_word_text(w) for w in cues.words[i : j + 1])
        density = (j - i + 1) / max(1.0, window_before + window_after)
        s = category_scores(cues, i, j, lp["ratio"], lp["peak"], text[:40], density)
        funny[t], highlight[t], clippable[t] = s["funny"], s["highlight"], s["clippable"]
    return {"funny": funny, "highlight": highlight, "clippable": clippable,
            "funny_n": _p99norm(funny), "highlight_n": _p99norm(highlight),
            "clippable_n": _p99norm(clippable), "cues": cues}


def window_reaction_bonus(words, t0, t1, db=None):
    """Combined capped reaction bonus for one candidate window [t0, t1) —
    the hook scan_vod.py can call per spike window (NOT wired in here; see
    the report for the exact insertion point). Returns the raw category
    scores plus "bonus", the sum of the normalised components weighted by
    W_FUNNY/W_HIGHLIGHT/W_CLIPPABLE, capped at 1.3 so a laugh alone can
    never outrank the chat-spike ratio that selected the window.
    """
    cues = CueSums(words)
    if not cues.words:
        return {"funny": 0.0, "highlight": 0.0, "clippable": 0.0, "bonus": 0.0}
    i = cues.word_index_at(t0)
    j = cues.word_index_at(t1) - 1
    if j < i:
        return {"funny": 0.0, "highlight": 0.0, "clippable": 0.0, "bonus": 0.0}
    lp = loudness_ratio_peak(db, t0, t1) if db is not None else {"ratio": 1.0, "peak": 1.0}
    text = " ".join(_word_text(w) for w in cues.words[i : j + 1])
    density = (j - i + 1) / max(1.0, t1 - t0)
    s = category_scores(cues, i, j, lp["ratio"], lp["peak"], text[:40], density)
    # Normalise by the category maxima reachable under the source's caps
    # (funny 66+20+16=102, highlight 42+27+20+12=101, clippable 30+14+8+12=64).
    bonus = min(1.3,
                W_FUNNY * s["funny"] / 102.0
                + W_HIGHLIGHT * s["highlight"] / 101.0
                + W_CLIPPABLE * s["clippable"] / 64.0)
    return {"funny": s["funny"], "highlight": s["highlight"],
            "clippable": s["clippable"], "bonus": round(float(bonus), 3)}


# ---------------------------------------------------------------------------
# On-screen action — port of workers/signals.py action() / keyframes()
# ---------------------------------------------------------------------------
def _local_median(x, window=7):
    """Exact centred rolling median for short windows — port of local_median()
    in workers/signals.py."""
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return x
    half = window // 2
    pad = np.pad(x, (half, half), mode="edge")
    return np.median(
        np.lib.stride_tricks.sliding_window_view(pad, window), axis=1
    )


def action_series(video, seconds, ffmpeg="ffmpeg"):
    """Per-second on-screen action (0..3) — port of workers/signals.py.

    PyAV is absent here, so frames come from ffmpeg instead: 1 fps 32x18
    grey frames piped as raw video (the same dependency-free pattern
    moments.py's _ffmpeg_framediff_cuts already uses). NOTE: ``-skip_frame
    nokey`` was tried and REJECTED on this file/ffmpeg — it returned 835
    frames for a 24 s clip (i.e. it did not skip), and with fps=1 the
    filter then duplicated keyframes, collapsing the signal to ~0; the
    plain 1 fps sampling gives the honest per-second change. The source
    spreads keyframe change over the seconds between keyframes; with
    1 fps sampling each diff already lands on its own second, which is
    the same thing for these files (this clip has only 4 keyframes in
    24 s, so keyframe-only would be far coarser anyway).
    mean abs diff after removing brightness (fades don't count), zeroed next
    to a flat frame (std < 10, e.g. black/white loading screens), then the
    7 s median, lifted over the 5-min rolling baseline floored at the
    stream median, clipped 0-3:  clip(2 * (motion / base - 1), 0, 3).
    Returns None when the video is unreadable — scoring must never crash
    over it (same degrade-to-zero contract as moments.py visual_series).
    Measured cost (2026-11-25, this VM): ~1.2 s wall for a 24 s 1080x1920
    clip warm (~3 s per minute of video); the very first cold run took
    ~45-56 s, almost certainly CPU contention from parallel builds on this
    1.5-CPU VM, so treat the warm figure as the steady-state cost.
    """
    import os

    if not video or not os.path.exists(str(video)) or seconds <= 0:
        return None
    try:
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(video),
             "-vf", "scale=32:18,fps=1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900,
        )
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    w, h = 32, 18
    nframes = len(r.stdout) // (w * h)
    if nframes < 2:
        return None
    frames = np.frombuffer(r.stdout[: nframes * w * h], dtype=np.uint8) \
        .reshape(nframes, h, w).astype(np.float32)
    # diff[i] = change into second i+1; fade removal + flat-frame zeroing
    # mirror the source exactly (std < 10 -> 0).
    motion = np.zeros(seconds)
    for i in range(len(frames) - 1):
        t = i + 1
        if t >= seconds:
            break
        g, last = frames[i + 1], frames[i]
        if min(g.std(), last.std()) < 10:
            motion[t] = 0.0
        else:
            motion[t] = float(np.abs((g - g.mean()) - (last - last.mean())).mean())
    motion = _local_median(motion, 7)
    base = np.maximum(rolling_median(motion), np.median(motion))
    return np.clip(2 * (motion / np.maximum(base, 1e-3) - 1), 0, 3)


def game_keeps(action, words, start, end, max_ranges=6):
    """Silent sustained-action ranges to keep — port of gameKeeps() in
    server/signals.mjs. Sustained on-screen action >= ACTION_KEEP (2.5)
    with no word within 0.75 s; at most ``max_ranges`` ranges, each with
    0.6 s before and 1 s after. Returns [{"start":..., "end":...}]."""
    if action is None or len(action) == 0:
        return []
    words = list(words or [])

    def quiet(t):
        return not any(_word_end(w) > t - 0.75 and _word_start(w) < t + 0.75 for w in words)

    def on(t):
        return (action[t] if 0 <= t < len(action) else 0) >= ACTION_KEEP and quiet(t)

    out, a = [], None
    last = int(math.floor(end))
    for t in range(int(math.ceil(start)), last + 1):
        yes = on(t)
        if yes and a is None:
            a = t
        if (not yes or t == last) and a is not None:
            b = t if yes else t - 1
            out.append({"start": round(max(start, a - 0.6), 2),
                        "end": round(min(end, b + 1), 2)})
            a = None
    return out[:max_ranges]
