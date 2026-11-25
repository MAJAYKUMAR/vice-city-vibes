#!/usr/bin/env python3
"""Cut one picked VOD moment into a post-ready vertical Short (CPU only).

Usage (from clipper-engine/, with the gtavi venv):
    python scripts/cut_vod_pick.py --window <lb window mp4> \
        --start S --end E --hook "HOOK" --title "TITLE" --name <slug> \
        --vod-url <url> --vod-offset <seconds> \
        [--caption-style karaoke] [--thumb-at FRAC] \
        [--outdir jobs/speed-vod-20261122]

Steps: transcribe the window (faster-whisper tiny, word timestamps) ->
write job candidates.json + transcript.json -> cut_filltrack
(full-fill 9:16, karaoke captions, -14 LUFS) -> watermark
("Vice City Vibes", bottom-right, low opacity, CRF 17, audio copy) ->
thumbnail (peak frame from the finished clip).

The window file is a live_heat lookback window (spike-15s..spike+20s,
landscape 1080p). --start/--end are seconds WITHIN the window file.
--vod-offset is the window's start offset in the VOD (for the package
notes); the final clip's VOD range is vod-offset+start .. vod-offset+end.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "src"))
sys.path.insert(0, str(ENGINE / "src" / "clipper" / "infra" / "gtavi"))

from clipper.infra import ssl_fix  # noqa: E402

ssl_fix.patch_yt_dlp()
from clipper.core.clip import cut_filltrack  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def ffprobe_duration(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                        "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    return float(json.loads(r.stdout)["format"]["duration"])


def transcribe(window, model_name="tiny"):
    """Word-level transcript of the window file (timings relative to it).

    Decodes to 16 kHz mono PCM with ffmpeg first (like run_moments):
    faster-whisper 1.2.1's av-based decode_audio is broken against the
    venv's old PyAV (metadata_errors kwarg), so the array path is used.
    """
    import tempfile
    import wave as _wave

    import numpy as np
    wav = Path(tempfile.mktemp(suffix=".wav"))
    try:
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(window),
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                        str(wav)], check=True)
        with _wave.open(str(wav)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()),
                                dtype=np.int16).astype(np.float32) / 32768
    finally:
        wav.unlink(missing_ok=True)
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(pcm, language="en",
                               word_timestamps=True, vad_filter=True)
    words = [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
             for s in segs for w in (s.words or []) if w.word.strip()]
    return words


def watermark(src, dst):
    """Small 'Vice City Vibes' watermark, bottom-right, low opacity.
    Same recipe as the ig-schedule watermarking (2026-11-22)."""
    vf = (f"drawtext=fontfile={FONT}:text='Vice City Vibes':fontsize=36:"
          f"fontcolor=white@0.4:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
          f"x=w-tw-24:y=h-th-24")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src),
                    "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "17", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", str(dst)], check=True)
    return dst


def thumbnail(clip, dst, frac=0.6):
    dur = ffprobe_duration(clip)
    t = max(0.5, min(dur - 0.5, dur * frac))
    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", f"{t:.2f}",
                    "-i", str(clip), "-frames:v", "1", "-q:v", "2",
                    str(dst)], check=True)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--hook", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--vod-url", required=True)
    ap.add_argument("--vod-offset", type=float, required=True)
    ap.add_argument("--caption-style", default="karaoke")
    ap.add_argument("--thumb-at", type=float, default=0.6)
    ap.add_argument("--outdir", default="jobs/speed-vod-20261122")
    a = ap.parse_args()

    window = Path(a.window).resolve()
    outdir = Path(a.outdir)
    job = outdir / a.name
    job.mkdir(parents=True, exist_ok=True)
    (job / "edits").mkdir(exist_ok=True)

    s0, e0 = float(a.start), float(a.end)
    if e0 - s0 < 7 or e0 - s0 > 30:
        print(f"WARNING: clip length {e0 - s0:.1f}s outside the 7-30s target")

    print(f"[{a.name}] transcribing {window.name} ...", flush=True)
    words = transcribe(window)
    print(f"[{a.name}] {len(words)} words transcribed", flush=True)
    (job / "transcript.json").write_text(json.dumps(words), encoding="utf-8")
    (job / "candidates.json").write_text(json.dumps({
        "source": str(window),
        "candidates": [{"n": 1, "start": s0, "end": e0, "text": a.hook}],
        "info": {"title": a.title, "webpage_url": a.vod_url,
                 "channel": "IShowSpeed"},
    }), encoding="utf-8")

    print(f"[{a.name}] cutting {s0:.1f}-{e0:.1f}s (filltrack, "
          f"{a.caption_style}) ...", flush=True)
    cut_filltrack(job, 1, a.hook, a.title, start=s0, end=e0,
                  caption_style=a.caption_style)
    cut = job / "edits" / "clip_1_fill.mp4"
    meta = json.loads((job / "edits" / "clip_1_fill.json").read_text())
    print(f"[{a.name}] safety={meta.get('safety', {}).get('verdict')} "
          f"copyright={meta.get('copyright', {}).get('verdict')} "
          f"actual={meta.get('start')}-{meta.get('end')} "
          f"crop_keyframes={len(meta.get('crop_keyframes', []))}", flush=True)

    final = outdir / f"{a.name}.mp4"
    print(f"[{a.name}] watermarking -> {final} ...", flush=True)
    watermark(cut, final)
    thumb = outdir / f"{a.name}-thumbnail.jpg"
    thumbnail(final, thumb, a.thumb_at)
    dur = ffprobe_duration(final)
    summary = {
        "name": a.name, "file": str(final), "thumbnail": str(thumb),
        "duration": round(dur, 2),
        "window": str(window),
        "window_start": s0, "window_end": e0,
        "vod_url": a.vod_url,
        "vod_start": round(a.vod_offset + s0, 1),
        "vod_end": round(a.vod_offset + e0, 1),
        "hook": a.hook, "title": a.title,
        "safety": meta.get("safety", {}).get("verdict"),
        "safety_reasons": meta.get("safety", {}).get("reasons"),
        "copyright": meta.get("copyright", {}).get("verdict"),
        "copyright_reasons": meta.get("copyright", {}).get("reasons"),
        "caption_style": a.caption_style,
    }
    (job / "cut-summary.json").write_text(json.dumps(summary, indent=1),
                                          encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
