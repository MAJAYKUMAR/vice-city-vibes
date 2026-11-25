#!/usr/bin/env python3
"""Styled animated captions — OpusClip-parity caption styles (English only).

Burns word-by-word animated captions into a clip with ffmpeg (ASS
subtitle burn-in via libass). Three styles, each visibly distinct in
its ASS styling:

  karaoke — word-by-word highlight: the current word is coloured in a
            contrasting orange and pops slightly larger, like
            OpusClip's karaoke captions.
  pop     — bold, large, punchy: bigger type, thick outline, the active
            word scales in hard (130% -> 100%) in a contrasting red.
  minimal — small, clean, low-contrast: regular weight, smaller type,
            light-grey words, no highlight colour, no scale pop.

Public API:
    STYLE_NAMES = ["karaoke", "pop", "minimal"]
    build_ass(words, s0, e0, hook="", cap_y=1080, style="karaoke") -> str
    render_captions(input_mp4, words, style="karaoke", output_mp4=None,
                    start=None) -> str

`words` are word-level timed words as produced by the existing
transcription flow: dicts with "w"/"s"/"e" keys (transcript.json) or
"word"/"start"/"end" keys — both spellings are accepted. Timings are
absolute (source timeline); pass `start` (the clip's start offset) to
`render_captions` when re-captioning an already-cut clip, or use
`build_ass` with the clip window (s0/e0) which emits relative timings.

Language: English only, hardcoded (per Ajay 2026-11-20 scope change —
no Spanish/Portuguese, no language parameter anywhere).

Only stdlib + ffmpeg are used, so the Modal cutter can import this
module without extra dependencies.
"""
import subprocess
import tempfile
from pathlib import Path

STYLE_NAMES = ["karaoke", "pop", "minimal"]

# Play resolution matches the existing caption pipeline (1080x1920).
W, H = 1080, 1920

# Per-style ASS style definitions. Format fields:
# Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,
# OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,
# ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,
# Alignment, MarginL, MarginR, MarginV, Encoding
# Colours are ASS &HAABBGGRR (AA=alpha, 00=opaque).
_STYLES = {
    # White words, orange active word, Arial Black 92, outline 7.
    "karaoke": {
        "name": "CapKaraoke",
        "font": "Arial Black",
        "size": 92,
        "primary": "&H00FFFFFF",    # white (inactive words)
        "highlight": "&H0000E5FF",  # orange (active word)
        "outline_colour": "&H00000000",
        "back": "&H64000000",
        "bold": -1,
        "outline": 7,
        "shadow": 3,
        "uppercase": True,
        "active_scale": 112,  # active word pops to 112% then settles
    },
    # Bigger, punchier: Arial Black 110, thick outline 10, active word
    # in red, scaling in hard from 130%.
    "pop": {
        "name": "CapPop",
        "font": "Arial Black",
        "size": 110,
        "primary": "&H00FFFFFF",
        "highlight": "&H000000FF",  # red (active word)
        "outline_colour": "&H00000000",
        "back": "&H64000000",
        "bold": -1,
        "outline": 10,
        "shadow": 4,
        "uppercase": True,
        "active_scale": 130,  # active word pops in from 130%
    },
    # Small, clean, low-contrast: regular Arial 56, light-grey words,
    # thin outline 2, no highlight colour, no scale pop.
    "minimal": {
        "name": "CapMinimal",
        "font": "Arial",
        "size": 56,
        "primary": "&H00D9D9D9",    # light grey (all words)
        "highlight": "&H00D9D9D9",  # same as primary: no highlight
        "outline_colour": "&H00000000",
        "back": "&H50000000",
        "bold": 0,
        "outline": 2,
        "shadow": 1,
        "uppercase": False,  # keep original casing
        "active_scale": 100,  # no pop
    },
}

# Hook style, shared by all three styles (same as today's Hook style).
_HOOK_STYLE = ("Style: Hook,Arial Black,68,&H00000000,&H00000000,"
               "&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,"
               "70,70,400,1")


def _validate_style(style):
    if style not in STYLE_NAMES:
        raise ValueError(
            f"unknown caption style {style!r}; expected one of {STYLE_NAMES}")
    return style


def _norm_word(w):
    """Accept transcript.json spelling (w/s/e) and word/start/end."""
    if "w" in w:
        return w.get("w", ""), float(w.get("s", 0)), float(w.get("e", 0))
    return (w.get("word", ""), float(w.get("start", 0)),
            float(w.get("end", 0)))


def _ts(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(s):
    return (s.replace("\\", "").replace("{", "(").replace("}", ")")
             .replace("\n", " "))


def _style_line(spec):
    return ("Style: {name},{font},{size},{primary},{highlight},"
            "{outline_colour},{back},{bold},0,0,0,100,100,0,0,1,"
            "{outline},{shadow},5,60,60,0,1".format(**spec))


def build_ass(words, s0, e0, hook="", cap_y=1080, style="karaoke"):
    """Build an ASS subtitle string for one clip window.

    Same grouping as the existing cut.build_ass (groups of up to 3
    words, broken on punctuation or a >0.6 s pause) and the same
    per-word timing, so a styled caption lands on exactly the same
    words at exactly the same times — only the look changes.
    Timings are relative to s0. `hook` renders the top hook line.
    """
    _validate_style(style)
    spec = _STYLES[style]
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
; Caption style: {style} (English only)

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{_style_line(spec)}
{_HOOK_STYLE}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ws = []
    for w in words:
        text, s, e = _norm_word(w)
        if s0 <= s < e0 and text:
            ws.append({"w": text, "s": s, "e": e})
    # Groups of up to 3 words, break on punctuation or pauses.
    groups, cur = [], []
    for w in ws:
        if cur and (len(cur) == 3 or w["s"] - cur[-1]["e"] > 0.6
                    or cur[-1]["w"][-1:] in ".?!,"):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    ev = []
    for gi, g in enumerate(groups):
        g_end = (groups[gi + 1][0]["s"] if gi + 1 < len(groups)
                 else g[-1]["e"] + 0.4)
        g_end = min(g_end, g[-1]["e"] + 0.8)
        for k, w in enumerate(g):
            a = w["s"] - s0
            b = (g[k + 1]["s"] - s0) if k + 1 < len(g) else g_end - s0
            if b <= a:
                continue
            parts = []
            for j, x in enumerate(g):
                t = _esc(x["w"].upper() if spec["uppercase"] else x["w"])
                if j == k:
                    parts.append("{\\c" + spec["highlight"] + "&}" + t
                                 + "{\\c" + spec["primary"] + "&}")
                else:
                    parts.append(t)
            # Active-word pop: scale in from active_scale -> 100% over
            # the first 90 ms. Minimal has active_scale 100 = no pop.
            scale = spec["active_scale"]
            pop = (f"{{\\fscx{scale}\\fscy{scale}"
                   f"\\t(0,90,\\fscx100\\fscy100)}}" if scale != 100
                   else "")
            ev.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},{spec['name']},,0,0,0,,"
                      f"{{\\pos({W // 2},{cap_y})}}{pop}{' '.join(parts)}")
    if hook:
        ev.append(f"Dialogue: 1,{_ts(0)},{_ts(2.6)},Hook,,0,0,0,,"
                  f"{{\\fad(0,180)\\fscx92\\fscy92\\t(0,120,\\fscx100\\fscy100)}}"
                  f"{_esc(hook)}")
    return hdr + "\n".join(ev) + "\n"


def _escape_subtitles_path(path):
    """Escape a file path for the ffmpeg subtitles filter value."""
    return (str(path).replace("\\", "\\\\").replace("'", "\\'")
            .replace(":", "\\:").replace("[", "\\[").replace("]", "\\]"))


def render_captions(input_mp4, words, style="karaoke", output_mp4=None,
                    start=None):
    """Burn styled word captions into a video with ffmpeg (ASS burn-in).

    input_mp4: source clip. words: word-level timed words (absolute
    source-timeline timings, as in transcript.json). style: one of
    STYLE_NAMES. output_mp4: destination path; defaults to
    <input stem>_<style>.mp4 next to the input. start: the clip's start
    offset on the source timeline — word timings are shifted by it so
    captions line up on an already-cut clip; when omitted, words are
    used as-is (use it whenever the input is a cut, not the raw source).

    Returns the output path (str). Audio is copied untouched; video is
    re-encoded libx264 (crf 20, yuv420p, +faststart).
    Raises ValueError for an unknown style, FileNotFoundError for a
    missing input, RuntimeError if ffmpeg fails.
    """
    _validate_style(style)
    src = Path(input_mp4)
    if not src.exists():
        raise FileNotFoundError(f"input clip not found: {src}")
    if output_mp4 is None:
        out = src.with_name(f"{src.stem}_{style}.mp4")
    else:
        out = Path(output_mp4)

    words = list(words)
    if start is None:
        # No offset given: assume words are already relative to the clip.
        s0, e0 = 0.0, float("inf")
    else:
        s0, e0 = float(start), float("inf")
        # Window end: last word end (captions only need the window).
        ends = [_norm_word(w)[2] for w in words]
        if ends:
            e0 = max(ends)

    ass_text = build_ass(words, s0, e0, hook="", cap_y=1080, style=style)
    with tempfile.TemporaryDirectory(prefix="captions_") as tmp:
        ass_path = Path(tmp) / f"captions_{style}.ass"
        ass_path.write_text(ass_text, encoding="utf-8")
        sub = _escape_subtitles_path(ass_path)
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-i", str(src),
               "-vf", f"subtitles='{sub}'",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-pix_fmt", "yuv420p", "-c:a", "copy",
               "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"ffmpeg caption burn-in failed (rc={r.returncode}): "
                f"{(r.stderr or r.stdout)[-500:]}")
    return str(out)
