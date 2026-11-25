#!/usr/bin/env python3
"""Vice City Vibes — Modal GPU cutter (vice-city-vibes-cutter).

Deploy (from a machine where Modal's gRPC connects — this VM's egress
proxy blocks Modal verify, see README Deploy section):
    modal deploy modal_app.py --profile maddukuriajaykumar05   # root shim -> this file

What it is: the GPU port of clip.py cut_filltrack, self-contained in
this one file so Modal needs no vendored gtavi tree. Same output spec:
full-fill vertical 1080x1920, word-by-word captions + hook, -14 LUFS,
libx264 CRF 18, content-filter verdict attached as a response header.
The finished clip is streamed straight back in the HTTP response —
Modal functions are ephemeral, so nothing is stored on Modal.

Endpoints (after deploy, Modal prints the URLs):
    POST /cut    — body JSON {source, start, end, hook, title}
                   returns the mp4 bytes, headers X-Safety-Verdict,
                   X-Duration-S, X-Title. Safety BLOCK is still
                   returned (format proof) — the caller must never
                   approve/post a BLOCK clip.
                   Optional `track` (alias `crop_keyframes`): crop
                   keyframes [[t_rel, x_px], ...] from the CPU path
                   (clip.py crop_keyframes JSON) — the cut follows them
                   instead of computing its own track. Without it the
                   cutter computes a lightweight motion track on the
                   downloaded section; if that fails it falls back to
                   a static centre crop. A cut never fails for tracking.
    GET  /health — {"status": "ok", "app": "vice-city-vibes-cutter"}

Auth: if the env var MODAL_SHARED_SECRET is set on the Modal app,
the /cut endpoint requires header X-Clipper-Auth with that value.
If it is unset the endpoint is open — set it before going live.
No token/secret is written in this file.

Hard rule (Vice City Vibes Rule 2/11): this cutter only cuts what the
caller sends it. The caller (modal_client.py on Fly) is the gate —
it only sends OUR OWN footage or a live paid campaign that explicitly
allows that creator. This file performs no scouting, recording, or
posting of its own.
"""
import os
import re

import modal
from fastapi import Request  # must be a real type: FastAPI injects it; a string annotation breaks /cut with 422

APP_NAME = "vice-city-vibes-cutter"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "faster-whisper==1.0.3",  # 1.0.3 does NOT pass metadata_errors to av.open (added in 1.1.0); compatible with the older PyAV actually installed at runtime
        "yt-dlp>=2026.10.26",
        "opencv-python-headless>=4.9",
        "nudenet==3.4.2",
        "onnxruntime>=1.17",
        "numpy>=1.26",
        "fastapi[standard]>=0.110",
        "pydantic>=2.0",
        # CUDA libs: debian_slim + pip install ships no cuBLAS, so
        # faster-whisper on CUDA died with "libcublas.so.12 is not
        # found" even in a real T4 worker (2026-11-20, call
        # fc-01M7VP40NWCJAT2RX7DFY8YRJH). The wheels ship the libs,
        # but pip does NOT put them on the loader path — that was the
        # real bug (the wheel alone changed nothing). LD_LIBRARY_PATH
        # below points at the wheel lib dirs, and cuDNN + the CUDA
        # runtime wheels are included (ctranslate2 needs all three).
        "nvidia-cublas-cu12",
        "nvidia-cudnn-cu12",
        "nvidia-cuda-runtime-cu12",
    )
    .env({
        "LD_LIBRARY_PATH": "/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:"
                           "/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:"
                           "/usr/local/lib/python3.12/site-packages/nvidia/cuda_runtime/lib",
        # Silence the unauthenticated-HF-Hub warning path; the tiny
        # model still downloads without a token.
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    })
)

app = modal.App(APP_NAME)

# NudeNet hard classes — same thresholds as safety.py (measured
# 2026-11-19): BLOCK on genitalia/anus >= 0.20, buttocks/breast on
# >= 3 sampled frames, or any hard hit >= 0.50. Anything less is REVIEW.
HARD_CLASSES = {
    "BUTTOCKS_EXPOSED", "BREAST_EXPOSED", "FEMALE_BREAST_EXPOSED",
    "MALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
}
KEYWORDS = ("shower", "bath", "naked", "nude", "butt", "strip", "undress")


# Tracked reframe (OpusClip parity, workstream B): the GPU cutter no
# longer centre-crops. clipper.core.track holds the shared crop math
# (pure functions + a lightweight motion tracker, stdlib-only at import
# time). If Modal ever deploys this file WITHOUT the sibling module
# (this file was historically self-contained), the import below fails
# and the small local fallbacks take over: caller-supplied tracks and
# the centre crop still work, only in-cutter track computation is lost
# (it then falls back to centre — never a failed cut).
try:
    from clipper.core.track import (  # noqa: F401
        build_fill_filter as _tracked_filter,
        centre_x as _centre_x,
        crop_x_expr as _crop_x_expr,
        normalize_keyframes as _normalize_keyframes,
        plan_crop as _plan_crop,
        scaled_width as _scaled_width,
    )
    _TRACK_AVAILABLE = True
except Exception:
    _TRACK_AVAILABLE = False
    _W, _H = 1080, 1920

    def _scaled_width(sw, sh):
        w = int(sw * _H / sh)
        return w // 2 * 2

    def _centre_x(sw, sh):
        return max(0, _scaled_width(sw, sh) - _W) // 2 // 2 * 2

    def _crop_x_expr(keyframes):
        keys = sorted(keyframes)
        if len(keys) == 1:
            return str(keys[0][1])
        expr = str(keys[-1][1])
        for i in range(len(keys) - 2, -1, -1):
            t0, x0 = keys[i]
            t1, x1 = keys[i + 1]
            if t1 <= t0:
                continue
            seg = f"({x0}+({x1}-{x0})*(t-{t0})/({t1}-{t0}))"
            expr = f"if(lt(t,{t1}),{seg},{expr})"
        return expr

    def _normalize_keyframes(keyframes, duration, sw, sh):
        import math as _math
        if keyframes is None:
            return None
        if isinstance(keyframes, dict):
            if "keyframes" in keyframes:
                keyframes = keyframes["keyframes"]
            elif "x" in keyframes:
                keyframes = [(0.0, keyframes["x"])]
            else:
                return None
        if isinstance(keyframes, (int, float)):
            keyframes = [(0.0, keyframes)]
        max_x = max(0, _scaled_width(sw, sh) - _W)
        keys = []
        try:
            for k in keyframes:
                t, x = (float(k["t"]), float(k["x"])) if isinstance(k, dict) else (float(k[0]), float(k[1]))
                if not (_math.isfinite(t) and _math.isfinite(x)):
                    continue
                keys.append((round(max(0.0, min(float(duration), t)), 2),
                             int(max(0, min(max_x, x)) // 2 * 2)))
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        if not keys:
            return None
        keys.sort()
        if keys[0][0] > 0:
            keys.insert(0, (0.0, keys[0][1]))
        if keys[-1][0] < float(duration) - 0.5:
            keys.append((round(float(duration), 2), keys[-1][1]))
        return keys

    def _tracked_filter(sw, sh, keyframes, ass_path):
        x_expr = _crop_x_expr(keyframes).replace(",", "\\,")
        sub = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        return (f"[0:v]scale={_scaled_width(sw, sh)}:{_H},crop={_W}:{_H}:{x_expr}:0,"
                f"setsar=1,fps=30,subtitles='{sub}'[vo]")

    _plan_crop = None  # in-cutter tracking unavailable without track.py


# Styled captions (workstream C, coordinator wiring): clipper.core.captions
# is stdlib + ffmpeg only, so it imports cleanly on Modal — but keep the
# same fallback pattern as the track import: if the sibling module ever
# fails to ship, styled captions degrade to the plain _build_ass below
# and the cut still succeeds. English only (Ajay, 2026-11-20).
try:
    from clipper.core.captions import (  # noqa: F401
        STYLE_NAMES as _CAPTION_STYLES,
        build_ass as _styled_build_ass,
    )
    _CAPTIONS_AVAILABLE = True
except Exception:
    _CAPTIONS_AVAILABLE = False
    _CAPTION_STYLES = ["karaoke", "pop", "minimal"]


def _build_ass(words, start, end, hook, path):
    """Minimal word-by-word ASS captions + hook, same layout as
    gtavi cut.build_ass (hook top, words bottom-centre)."""
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080",
             "PlayResY: 1920", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
             "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
             "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding",
             "Style: Hook,Arial,72,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
             "1,0,0,0,100,100,0,0,1,3,0,8,40,40,120,1",
             "Style: Word,Arial,64,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
             "1,0,0,0,100,100,0,0,1,3,0,2,40,40,200,1",
             "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
             "Effect, Text"]

    def _ts(t):
        t = max(0.0, t)
        h, m = int(t // 3600), int((t % 3600) // 60)
        return f"{h}:{m:02d}:{t % 60:05.2f}"

    if hook:
        lines.append(f"Dialogue: 0,{_ts(0)},{_ts(2.0)},Hook,,0,0,0,,{hook}")
    for w in words:
        s, e = float(w.get("s", 0)), float(w.get("e", 0))
        if e < start or s > end:
            continue
        text = (w.get("w") or "").strip().replace("{", "").replace("}", "")
        if text:
            lines.append(
                f"Dialogue: 0,{_ts(s - start)},{_ts(e - start)},Word,,0,0,0,,{text}")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_ass(words, start, end, hook, path, style=None):
    """Write the .ass caption file. style=None keeps today's plain word
    captions (_build_ass); a style name (karaoke/pop/minimal, English
    only) uses the styled builder from clipper.core.captions. An unknown
    style raises ValueError (caller error — fail fast, don't silently
    ship the wrong look); a missing captions module falls back to plain
    captions and the cut still succeeds."""
    if style is None:
        _build_ass(words, start, end, hook, path)
        return
    if not _CAPTIONS_AVAILABLE:
        print("[cut] caption style requested but clipper.core.captions "
              "unavailable — plain captions", flush=True)
        _build_ass(words, start, end, hook, path)
        return
    if style not in _CAPTION_STYLES:
        raise ValueError(
            f"unknown caption style {style!r}; expected one of "
            f"{_CAPTION_STYLES}")
    path.write_text(
        _styled_build_ass(words, start, end, hook=hook, style=style),
        encoding="utf-8")
    print(f"[cut] captions style={style}", flush=True)


def _safety_check(video_path, start, end, words):
    """NudeNet + keyword check on the window. Returns a verdict dict
    (SAFE / REVIEW / BLOCK), same rules as safety.py."""
    import cv2

    verdict = {"verdict": "SAFE", "hard_hits": [], "keyword_hits": [],
               "reasons": [], "nudenet_available": False}
    text = " ".join(w.get("w", "") for w in words
                    if start <= w.get("s", 0) < end).lower()
    verdict["keyword_hits"] = sorted({k for k in KEYWORDS if k in text})
    try:
        from nudenet import NudeDetector
        det = NudeDetector()
        verdict["nudenet_available"] = True
    except Exception as e:
        verdict["verdict"] = "REVIEW"
        verdict["reasons"].append(f"nudenet unavailable ({e}) — capped at REVIEW")
        return verdict
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(round(fps / 2)))  # sample ~2 fps
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start) * 1000)
    i, hard_frames = 0, set()
    while True:
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
        if t > end or not cap.grab():
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            tmp = f"/tmp/modal-safety-{i}.jpg"
            cv2.imwrite(tmp, frame)
            try:
                for d in det.detect(tmp):
                    cls = d["class"]
                    if cls in HARD_CLASSES or "GENITALIA" in cls:
                        verdict["hard_hits"].append(
                            {"class": cls, "score": round(float(d["score"]), 2),
                             "t": round(t - start, 1)})
                        hard_frames.add(round(t - start, 1))
            except Exception:
                pass
        i += 1
    cap.release()
    best = max((h["score"] for h in verdict["hard_hits"]), default=0.0)
    genital = any("GENITALIA" in h["class"] or "ANUS" in h["class"]
                  for h in verdict["hard_hits"])
    if (genital and best >= 0.20) or len(hard_frames) >= 3 or best >= 0.50:
        verdict["verdict"] = "BLOCK"
        verdict["reasons"].append(f"nudenet hard-class hits {verdict['hard_hits'][:3]}")
    elif verdict["hard_hits"] or verdict["keyword_hits"]:
        verdict["verdict"] = "REVIEW"
        verdict["reasons"].append("hard hit or keyword in window")
    return verdict


def _probe_size(video_path):
    """Source width/height via ffprobe (defaults 1280x720 on failure)."""
    import json
    import subprocess

    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json",
             str(video_path)],
            capture_output=True, text=True, timeout=30)
        s = json.loads(r.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:
        return 1280, 720


def _check_download(video_path, source):
    """Fail fast on an empty/invalid download (found 2026-11-20, Modal
    logs): yt-dlp --download-sections exits 0 but writes only a 261-byte
    file for Twitch VOD v2934030540 — same empty-section failure mode as
    Nov 19. Without this check the empty file reaches faster-whisper,
    which crashes with IndexError (no audio stream) after minutes of
    wasted GPU time (4 failed cuts today). Requires a video stream AND
    an audio stream, and a file over ~100KB; otherwise raises
    RuntimeError immediately, in seconds."""
    import subprocess

    size = video_path.stat().st_size if video_path.exists() else 0
    if size <= 100 * 1024:
        raise RuntimeError(
            f"download produced an empty/invalid file for {source} "
            f"(size={size}B)")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, timeout=30)
    kinds = set(probe.stdout.split())
    if "video" not in kinds or "audio" not in kinds:
        raise RuntimeError(
            f"download produced an empty/invalid file for {source} "
            f"(size={size}B, streams={sorted(kinds)})")


def _resolve_crop(src, clip_start, clip_end, supplied_track):
    """Pick the crop keyframes for one cut window.

    `clip_start`/`clip_end` are RELATIVE to the downloaded section
    file (the same relative times the transcribe/safety/encode steps
    use); returned keyframe times are relative to the CLIP start, so
    ffmpeg filter time t=0 is the first keyframe. Priority:
      1. caller-supplied track (CPU path crop_keyframes) if it validates;
      2. lightweight in-cutter motion track (track.plan_crop, ~2 fps on
         64x36 frames — cheap on GPU time);
      3. static centre crop — NEVER raises, never fails the cut.
    Returns (keyframes [(t_rel, x_px), ...], mode str, src_w, src_h).
    """
    import math

    duration = clip_end - clip_start
    sw, sh = _probe_size(src)
    centre = [(0.0, _centre_x(sw, sh))]
    if supplied_track is not None:
        try:
            keys = _normalize_keyframes(supplied_track, duration, sw, sh)
        except Exception:
            keys = None
        if keys:
            print(f"[cut] crop=tracked-supplied {len(keys)} keyframes "
                  f"x {keys[0][1]}..{max(x for _, x in keys)} "
                  f"(centre would be {centre[0][1]})", flush=True)
            return keys, "tracked-supplied", sw, sh
        print("[cut] crop: supplied track unusable — computing track",
              flush=True)
    if _TRACK_AVAILABLE and _plan_crop is not None:
        try:
            plan = _plan_crop(src, clip_start, clip_end, sample_fps=2)
            keys = plan["keyframes"]
            if keys and any(math.isfinite(x) for _, x in keys):
                print(f"[cut] crop=tracked-computed {len(keys)} keyframes "
                      f"x {keys[0][1]}..{max(x for _, x in keys)} "
                      f"(centre would be {centre[0][1]})", flush=True)
                return keys, "tracked-computed", sw, sh
        except Exception as e:
            print(f"[cut] crop: track computation failed ({e}) — centre fallback",
                  flush=True)
    else:
        print("[cut] crop: in-cutter tracking unavailable — centre fallback",
              flush=True)
    print(f"[cut] crop=centre x={centre[0][1]}", flush=True)
    return centre, "centre", sw, sh


@app.function(image=image, gpu="T4", timeout=1200, scaledown_window=300, max_containers=1)
def cut_clip(source, start, end, hook="", title="", track=None,
             caption_style=None):
    """Cut one clip on GPU. Returns (mp4_bytes, safety_verdict_dict).
    `source` is a URL (VOD / own footage) or a Modal Volume path.
    `track` is optional crop keyframes [[t_rel, x_px], ...] (clip.py
    crop_keyframes JSON, times relative to the clip start) — when
    supplied and valid the cut follows them; otherwise the cutter
    computes a lightweight motion track on the downloaded section and
    falls back to a static centre crop if that fails. Kept as a plain
    function so it can also be called from Python
    (`modal run modal_app.py::cut_clip`) for testing.
    `caption_style` is optional (karaoke/pop/minimal, English only) —
    None keeps today's plain word captions; a style name renders the
    styled captions from clipper.core.captions."""
    import subprocess
    import tempfile
    from pathlib import Path

    start, end = float(start), float(end)
    if end - start < 3 or end - start > 60:
        raise ValueError("clip window must be 3-60s")
    # Normalize bare IDs to full URLs before yt-dlp sees them. The
    # caller once sent "https://youtu.be/v2932305259" — a Twitch VOD id
    # wrapped as a YouTube short link — and yt-dlp failed with
    # "[youtube] v2932305259: This video is unavailable". A "v" + 10
    # digits is ALWAYS a Twitch VOD id, never a YouTube video, wherever
    # it appears (bare, youtu.be, or youtube.com URL).
    source = str(source).strip()
    m = re.search(r"v(\d{10})\b", source)
    if m:
        source = f"https://www.twitch.tv/videos/{m.group(1)}"
    elif not source.startswith("http"):
        m = re.fullmatch(r"v?(\d+)", source)
        if m:
            source = f"https://www.twitch.tv/videos/{m.group(1)}"
        elif re.fullmatch(r"[A-Za-z0-9_-]{11}", source):
            source = f"https://www.youtube.com/watch?v={source}"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "source.mp4"
        # Download only the window (+5s headroom for the transcribe).
        # BUG (found 2026-11-20, proved locally): the section file is
        # only ~25s long starting at section_start, but EVERYTHING
        # downstream used ABSOLUTE VOD timestamps against it — ffmpeg
        # `-ss 7262 -to 7281` on a 25s file seeks past EOF and exits 0
        # producing a 261-byte empty MP4, whisper word times are
        # relative to the section, and the safety check samples frames
        # at 7262s that don't exist (always SAFE). All downstream
        # times must be RELATIVE to section_start.
        section_start = max(0.0, start - 5)
        offset = start - section_start          # 5.0 normally
        rel_end = end - section_start           # offset + clip length
        duration = end - start
        r = subprocess.run(
            # Item #8 (2026-11-25): prefer true 1080p60, then any 1080p,
            # then 720p. This window IS the pick fetch — the clip is cut
            # from it. 1080p cap kept: the 1440p upload trick is out of scope.
            ["yt-dlp", "-f", "bestvideo[height=1080][fps=60]+bestaudio/bestvideo[height=1080]+bestaudio/bestvideo[height<=720]+bestaudio/best",
             "--download-sections", f"*{section_start:.1f}-{end + 1:.1f}",
             "-o", str(src), source],
            capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not src.exists():
            # Fall back to a full download (some sources reject sections).
            r = subprocess.run(
                ["yt-dlp", "-f", "bestvideo[height=1080][fps=60]+bestaudio/bestvideo[height=1080]+bestaudio/bestvideo[height<=720]+bestaudio/best",
                 "-o", str(src), source],
                capture_output=True, text=True, timeout=900)
            if r.returncode != 0 or not src.exists():
                raise RuntimeError(f"download failed: {(r.stderr or '')[-500:]}")
            # Full file: absolute timestamps are correct again.
            section_start, offset, rel_end = 0.0, start, end
        # Guard (2026-11-20): yt-dlp can exit 0 while writing a 261-byte
        # empty section file (VOD v2934030540, 4 failed cuts today). Fail
        # fast here, in seconds, instead of feeding the empty file to
        # faster-whisper and crashing with IndexError after minutes of
        # GPU time. Covers BOTH the section path and the full-download
        # fallback above — both converge here.
        _check_download(src, source)
        # Transcribe on the GPU: CUDA + float16. The earlier CPU
        # fallback was only because libcublas wasn't on the loader
        # path — fixed above with LD_LIBRARY_PATH, so CUDA is tried
        # first and a failure now raises loudly instead of silently
        # falling back to CPU.
        from faster_whisper import WhisperModel
        print(f"[cut] source={src} size={src.stat().st_size}B", flush=True)
        model = WhisperModel("tiny", device="cuda", compute_type="float16")
        segs, _ = model.transcribe(str(src), word_timestamps=True,
                                   vad_filter=True)
        words = [{"w": w.word.strip(), "s": round(w.start, 2),
                  "e": round(w.end, 2)}
                 for s in segs for w in (s.words or [])]
        print(f"[cut] transcribed {len(words)} words (cuda)", flush=True)
        safety = _safety_check(src, offset, rel_end, words)
        ass = td / "clip.ass"
        _write_ass(words, offset, rel_end, hook, ass, caption_style)
        out = td / "clip_fill.mp4"
        # Tracked full-fill 9:16 crop (workstream B): follow the
        # caller-supplied track, else compute a lightweight motion
        # track on the section, else static centre crop — never fail
        # the cut for tracking. Keyframe times are relative to the
        # clip start, matching ffmpeg filter time (t=0 at -ss offset).
        keyframes, crop_mode, sw, sh = _resolve_crop(
            src, offset, rel_end, track)
        vf = _tracked_filter(sw, sh, keyframes, ass)
        # Encode on the GPU (NVENC) when the T4 exposes it; libx264
        # (CPU) only as a fallback. NVENC is what makes the GPU cut
        # fast — libx264 on a GPU container is just CPU encode.
        enc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True)
        use_nvenc = "h264_nvenc" in enc.stdout
        print(f"[cut] encoder={'h264_nvenc (GPU)' if use_nvenc else 'libx264 (CPU fallback)'}",
              flush=True)
        v_args = (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19"]
                  if use_nvenc else
                  ["-c:v", "libx264", "-preset", "medium", "-crf", "18"])
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-ss", f"{offset:.2f}", "-t", f"{duration:.2f}", "-i", str(src),
             "-filter_complex", vf, "-map", "[vo]", "-map", "0:a?",
             "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
             *v_args,
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
             "-ar", "48000", "-movflags", "+faststart", str(out)],
            check=True, timeout=600)
        print(f"[cut] encoded {out.stat().st_size}B", flush=True)
        # Fail loudly on an empty encode: ffmpeg exits 0 even when a
        # seek past EOF produces a stream-less file (the 261-byte
        # false-200 of 2026-11-20). Never return that as a clip.
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(out)],
            capture_output=True, text=True)
        if "video" not in probe.stdout:
            raise RuntimeError(
                f"encode produced no video stream (out={out.stat().st_size}B, "
                f"offset={offset:.2f}, duration={duration:.2f})")
        return out.read_bytes(), safety


@app.function(image=image, gpu="T4", timeout=1200, scaledown_window=300, max_containers=1)
@modal.fastapi_endpoint(method="POST")
def cut(item: dict, request: Request):
    """POST /cut — JSON {source, start, end, hook, title}.
    Optional `track` (alias `crop_keyframes`): crop keyframes
    [[t_rel, x_px], ...] from the CPU path — the cut follows them;
    without it the cutter computes its own lightweight motion track,
    falling back to centre crop. Optional `caption_style`
    (karaoke/pop/minimal, English only): None keeps today's plain word
    captions; a style name renders styled captions. All fields except
    source/start/end are optional; adding `track`/`caption_style`
    changes nothing for old callers.
    Returns the mp4 bytes with X-Safety-Verdict / X-Duration-S headers.
    Auth: when the env var MODAL_SHARED_SECRET is set on the Modal app,
    the request must carry header X-Clipper-Auth with that value.
    NOTE: `request` MUST be the real fastapi.Request type (imported at
    module level). A string annotation "Request" makes FastAPI treat it
    as a required query parameter -> 422 on every call (found 2026-11-20)."""
    from fastapi import Response

    secret = os.environ.get("MODAL_SHARED_SECRET", "")
    if secret and request.headers.get("X-Clipper-Auth") != secret:
        return Response(content=b"unauthorized", status_code=401)
    source = item.get("source", "")
    start, end = float(item.get("start", 0)), float(item.get("end", 0))
    hook = item.get("hook", "")
    title = item.get("title", "")
    track = item.get("track", item.get("crop_keyframes"))
    caption_style = item.get("caption_style")
    data, safety = cut_clip.remote(source, start, end, hook, title, track,
                                   caption_style)
    return Response(
        content=data, media_type="video/mp4",
        headers={
            "X-Safety-Verdict": safety["verdict"],
            "X-Duration-S": f"{end - start:.2f}",
            "X-Title": title or hook or "clip",
            "Content-Disposition": 'attachment; filename="clip_fill.mp4"',
        })


@app.function(image=image)
@modal.fastapi_endpoint(method="GET")
def health():
    return {"status": "ok", "app": APP_NAME}


if __name__ == "__main__":
    print("Deploy with:  modal deploy modal_app.py --profile maddukuriajaykumar05")
    print("Then set MODAL_CUT_URL to the printed /cut URL and run:")
    print("  python -m clipper.infra.modal_client health")
    print("  python -m clipper.infra.modal_client cut --source <url> --start 10 --end 30 --hook 'WAIT FOR IT'")
