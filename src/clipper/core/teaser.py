#!/usr/bin/env python3
"""Cold-open teaser cut variant for the Clipper Engine.

Port of OpenClip's teaser-open technique (MIT licensed, /tmp/open-clip-editor):
  * server/edl.mjs — ``payoffTime()`` and the cold-open block of
    ``buildClipEdl()``: the teaser window math and the payoff
    resolution order (coldOpenAt > first replay > peakAt > 65%
    through the clip). Captions follow the main pass only; the
    teaser shows a label instead (edl.mjs ``retimeWords``).
  * server/pipeline.mjs — the auto-enable rule: cold open when the
    payoff comes late (>= 25 s of played time in).
  * server/render.mjs — the teaser is marked with a flash + rewind
    sound. We have NO SFX track, so the marker here is a VISUAL
    FLASH ONLY (``teaser_flash_filter``); the rewind sound is
    deliberately omitted, not approximated.

This is an OPTION, never the default: it sits alongside the
existing slide-based front-load (``clip.PEAK_TARGET_OFFSET`` /
``clip.frontload_window``), which slides the window so the peak
lands ~0.5 s in. The teaser instead REPEATS the payoff up front
(teaser segment, then the full main clip from the setup) — the
clip duration grows by the teaser length (default 1.6 s).

One convention difference from edl.mjs, documented so nobody
"fixes" it: edl.mjs takes ``clip.peakAt`` as an offset in seconds
from ``clip.start`` (``s + clip.peakAt``). Our engine records the
candidate peak as ABSOLUTE source seconds (``candidates.json``
``peak``), so every function here takes absolute source times.

Wiring (pending — clip.py is actively edited by the max-quality
build, see HOOKS.md in this directory): ``clip.cut_filltrack``
should call ``teaser_segments()`` after the front-load step when
``teaser=True`` is passed, prepend the cold segment to the encode,
and add ``teaser_flash_filter()`` at the teaser/main boundary.
"""
import math

# edl.mjs: S = coldOpenSeconds, clamped to [1, 3], default 1.6.
DEFAULT_COLD_OPEN_SECONDS = 1.6
MIN_COLD_OPEN_SECONDS = 1.0
MAX_COLD_OPEN_SECONDS = 3.0
# edl.mjs: a teaser shorter than 0.5 s is not prepended.
MIN_TEASER_SECONDS = 0.5
# edl.mjs: the teaser ends S*0.4 s after the payoff.
TEASER_AFTER_PAYOFF = 0.4
# pipeline.mjs: auto-enable when the payoff is >= 25 s in.
LATE_PAYOFF_SECONDS = 25.0
# render.mjs: flash duration at the teaser -> main boundary.
FLASH_SECONDS = 0.22


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _clamp_seconds(value, default=DEFAULT_COLD_OPEN_SECONDS):
    """edl.mjs ``num(v, 1, 3, 1.6)``: clamp S to [1, 3], default 1.6."""
    if not _finite(value):
        return default
    return min(MAX_COLD_OPEN_SECONDS, max(MIN_COLD_OPEN_SECONDS, float(value)))


def payoff_time(start, end, peak=None, cold_open_at=None, replay_at=None):
    """Where the cold-open teaser points — edl.mjs ``payoffTime()``.

    Resolution order (exact port):
      1. explicit ``cold_open_at`` (contract override),
      2. first replay ``replay_at``,
      3. recorded peak (absolute source seconds),
      4. 65% through the clip.
    Clamped into [start, end]. All times are absolute source seconds.
    """
    if _finite(cold_open_at):
        p = float(cold_open_at)
    elif _finite(replay_at):
        p = float(replay_at)
    elif _finite(peak):
        p = float(peak)
    else:
        p = start + 0.65 * (end - start)
    return min(end, max(start, p))


def cold_open_window(start, end, payoff, seconds=DEFAULT_COLD_OPEN_SECONDS):
    """The teaser window [a, b] — edl.mjs ``buildClipEdl`` cold-open block.

    b = min(end, p + S*0.4); a = max(start, b - S); b = min(end, a + S).
    Returns ``(a, b)`` or ``None`` when the window is < 0.5 s
    (edl.mjs drops such teasers instead of prepending them).
    """
    s = _clamp_seconds(seconds)
    b = min(end, payoff + s * TEASER_AFTER_PAYOFF)
    a = max(start, b - s)
    b = min(end, a + s)
    if b - a >= MIN_TEASER_SECONDS:
        return (round(a, 3), round(b, 3))
    return None


def payoff_is_late(start, peak, played_up_to_peak=None):
    """pipeline.mjs auto-enable rule: cold open when the payoff is late.

    OpenClip measures the *played* length of the words up to the
    peak and enables the teaser at >= 25 s. When that played length
    is available pass it as ``played_up_to_peak``; otherwise the raw
    offset ``peak - start`` is used as the approximation (it over-
    counts dead air the pause trim would have removed, so it can
    only enable early, never miss a late payoff by much).
    """
    if _finite(played_up_to_peak):
        return played_up_to_peak >= LATE_PAYOFF_SECONDS
    if not _finite(peak):
        return False
    return (float(peak) - float(start)) >= LATE_PAYOFF_SECONDS


def teaser_segments(start, end, peak=None, cold_open_at=None,
                    replay_at=None, seconds=DEFAULT_COLD_OPEN_SECONDS):
    """The edit as segments: optional cold teaser + the full main pass.

    Returns a list of ``{"kind": ..., "start": ..., "end": ...}``
    dicts, teaser first when it survives the >= 0.5 s rule. The main
    segment is always the whole [start, end] — the teaser REPEATS
    the payoff, it does not cut it out of the main clip (edl.mjs
    ``list.unshift({kind:'cold', ...})`` in front of the main list).
    Returns ``[]`` only when end <= start.
    """
    if not (_finite(start) and _finite(end)) or end <= start:
        return []
    p = payoff_time(start, end, peak=peak, cold_open_at=cold_open_at,
                    replay_at=replay_at)
    window = cold_open_window(start, end, p, seconds=seconds)
    segments = []
    if window is not None:
        segments.append({"kind": "cold", "start": window[0], "end": window[1]})
    segments.append({"kind": "main", "start": float(start), "end": float(end)})
    return segments


def main_segments(segments):
    """Segments captions follow — edl.mjs ``retimeWords``: main only.

    The teaser repeats words already spoken at the payoff; captions
    are built from the main pass only and the teaser shows a label
    instead (hook text), never burned-in word captions.
    """
    return [s for s in segments if s.get("kind") in (None, "main")]


def teaser_duration(segments):
    """Extra playback seconds the teaser adds (0 when no teaser)."""
    return sum(s["end"] - s["start"] for s in segments
               if s.get("kind") == "cold")


def teaser_flash_filter(teaser_end, duration=FLASH_SECONDS):
    """Visual flash marking the teaser -> main jump — render.mjs.

    OpenClip marks the jump with a flash AND a rewind sound. We have
    no SFX track, so this is a VISUAL FLASH ONLY: a short brightness
    lift at the boundary. Returns an ffmpeg video-filter fragment to
    append to the output chain (after framing, before subtitles), or
    ``""`` when there is no teaser boundary.
    """
    if not _finite(teaser_end):
        return ""
    t0 = max(0.0, float(teaser_end))
    t1 = t0 + duration
    return (f"eq=brightness=0.30:enable='between(t,{t0:.3f},{t1:.3f})'")
