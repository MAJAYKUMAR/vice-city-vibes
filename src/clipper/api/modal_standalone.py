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

import modal

APP_NAME = "vice-city-vibes-cutter"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "faster-whisper>=1.1",
        "yt-dlp>=2026.10.26",
        "opencv-python-headless>=4.9",
        "nudenet==3.4.2",
        "onnxruntime>=1.17",
        "numpy>=1.26",
    )
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


@app.function(image=image, gpu="T4", timeout=1200, scaledown_window=300)
def cut_clip(source, start, end, hook="", title=""):
    """Cut one clip on GPU. Returns (mp4_bytes, safety_verdict_dict).
    `source` is a URL (VOD / own footage) or a Modal Volume path.
    Kept as a plain function so it can also be called from Python
    (`modal run modal_app.py::cut_clip`) for testing."""
    import subprocess
    import tempfile
    from pathlib import Path

    start, end = float(start), float(end)
    if end - start < 3 or end - start > 60:
        raise ValueError("clip window must be 3-60s")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "source.mp4"
        # Download only the window (+5s headroom for the transcribe).
        r = subprocess.run(
            ["yt-dlp", "-f", "bestvideo[height<=1080]+bestaudio/best",
             "--download-sections", f"*{max(0.0, start - 5):.1f}-{end + 1:.1f}",
             "-o", str(src), source],
            capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not src.exists():
            # Fall back to a full download (some sources reject sections).
            r = subprocess.run(
                ["yt-dlp", "-f", "bestvideo[height<=1080]+bestaudio/best",
                 "-o", str(src), source],
                capture_output=True, text=True, timeout=900)
            if r.returncode != 0 or not src.exists():
                raise RuntimeError(f"download failed: {(r.stderr or '')[-500:]}")
        # Transcribe on GPU (faster-whisper tiny, float16 on CUDA).
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cuda", compute_type="float16")
        segs, _ = model.transcribe(str(src), word_timestamps=True,
                                   vad_filter=True)
        words = [{"w": w.word.strip(), "s": round(w.start, 2),
                  "e": round(w.end, 2)}
                 for s in segs for w in (s.words or [])]
        safety = _safety_check(src, start, end, words)
        ass = td / "clip.ass"
        _build_ass(words, start, end, hook, ass)
        out = td / "clip_fill.mp4"
        vf = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
              f"crop=1080:1920,setsar=1,fps=30,subtitles='{ass}'[vo]")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(src),
             "-filter_complex", vf, "-map", "[vo]", "-map", "0:a?",
             "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
             "-ar", "48000", "-movflags", "+faststart", str(out)],
            check=True, timeout=600)
        return out.read_bytes(), safety


@app.function(image=image, gpu="T4", timeout=1200, scaledown_window=300)
@modal.fastapi_endpoint(method="POST")
def cut(item: dict, request: "Request"):
    """POST /cut — JSON {source, start, end, hook, title}.
    Returns the mp4 bytes with X-Safety-Verdict / X-Duration-S headers.
    Auth: when the env var MODAL_SHARED_SECRET is set on the Modal app,
    the request must carry header X-Clipper-Auth with that value."""
    from fastapi import Response

    secret = os.environ.get("MODAL_SHARED_SECRET", "")
    if secret and request.headers.get("X-Clipper-Auth") != secret:
        return Response(content=b"unauthorized", status_code=401)
    source = item.get("source", "")
    start, end = float(item.get("start", 0)), float(item.get("end", 0))
    hook = item.get("hook", "")
    title = item.get("title", "")
    data, safety = cut_clip.local(source, start, end, hook, title)
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
