#!/usr/bin/env python3
"""Vice City Vibes — Modal GPU cutter.

Runs the SAME cut pipeline as clip.py on a Modal GPU (T4 by default,
L4 if you flip GPU below): faster-whisper scout -> hype candidates ->
scene-snapped, full-fill 9:16 cut with word-by-word captions + hook,
-14 LUFS, NudeNet safety filter. Output is a 1080x1920 mp4, stored in
the `vice-city-vibes-clips` Modal Volume so the Fly watcher (or
anything else) can fetch it.

What this file is NOT:
  * It does NOT post anything. It cuts one clip and returns metadata.
    Posting stays with the Fly watcher / the 3-hour cron, and only
    SAFE clips may auto-post (Vice City Vibes Rule 14: only good
    clips, always; Rule 2/11: only OUR OWN footage or a live campaign
    that explicitly allows that creator).
  * It holds NO token or secret. Auth comes from the Modal CLI profile
    on the machine that deploys it (`modal profile activate
    maddukuriajaykumar05`), never from a value in this file.

Deploy (do NOT run from here unless Modal connects from this machine):
    cd ~/workspace/gta-test/clipper-engine
    ~/workspace/gta-test/clipper-engine/modal-venv/bin/modal deploy src/clipper/api/modal_app.py

Test one cut after deploy:
    modal run modal_app.py --source-url "<twitch/youtube VOD or mp4 URL>" \
        --hook "WAIT FOR IT"

Fly watcher calls it over HTTPS:
    POST https://<workspace>--vice-city-vibes-cutter.modal.run/cut
    {"source_url": "...", "pick": 1, "hook": "WAIT FOR IT"}
    -> {"job_id": ..., "file": "/data/<job>/clip_1_fill.mp4",
        "safety": {...}, "do_not_post": false, ...}
    GET  /health                         -> {"ok": true}
    GET  /download/<job_id>/<file>       -> the finished mp4

GPU note: T4 is the cheapest GPU that still cuts fast (transcribe +
reframe + encode are 5-10x faster than the CPU VM). L4 is ~2x the
T4 price for ~1.5x speed — flip GPU = "L4" below only if T4 is the
bottleneck. Whisper "small" is used on GPU (clip.py default); the
first run on a cold container downloads the Whisper model (~466 MB)
and the NudeNet model once, then they are cached for the life of
that container.
"""
from pathlib import Path

import modal

# --- GPU choice -----------------------------------------------------------
# T4 = cheapest useful GPU for this job. Change to "L4" for more speed.
GPU = "T4"
# Whisper model on GPU: "small" (clip.py default). "tiny" is faster
# but worse at hype-word detection.
WHISPER_MODEL = "small"

APP_NAME = "vice-city-vibes-clipper"
VOLUME_NAME = "vice-city-vibes-clips"
VOLUME_MOUNT = "/data"
# Inside the container the clipper code lives here (add_local_dir below).
CLIPPER_DIR = "/root/clipper"
# gtavi-claudecode scripts/ are vendored at clipper-engine/src/clipper/infra/gtavi/
# (upstream repo untouched, MIT). ssl_fix.py and reframe.py expect them at
# $HOME/workspace/gta-test/gtavi-claudecode/scripts and at
# /root/gtavi-claudecode/scripts — _setup_paths() symlinks both to the
# vendored copy before any clipper module is imported.
GTAVI_SCRIPTS = f"{CLIPPER_DIR}/src/clipper/infra/gtavi"

# Pinned to the versions already verified in the gtavi venv (2026-11-19),
# plus fastapi/pydantic for the web endpoint.
PIP_PACKAGES = [
    "faster-whisper>=1.1",
    "numpy>=1.26",
    "requests>=2.31",
    "yt-dlp[default]>=2026.10.26",
    "deep-translator>=1.11",
    "edge-tts>=6.1",
    "opencv-python-headless>=4.9",
    "nudenet==3.4.2",
    "onnxruntime>=1.17",
    "scenedetect==0.7.3",
    "auto-editor==29.3.1",
    "fastapi>=0.110",
    "pydantic>=2.0",
]

from clipper.paths import repo_root as _repo_root

_CLIPPER_ENGINE = str(_repo_root())

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libgomp1")
    .pip_install(*PIP_PACKAGES)
    .env({"CLIPPER_WHISPER_MODEL": WHISPER_MODEL, "PYTHONUNBUFFERED": "1"})
    # The whole clipper-engine folder, minus generated / huge stuff.
    # jobs/, test-real/ and clips/ hold GBs of video and are never
    # needed inside the cutter; modal-venv / .venv / __pycache__ are
    # local-only. models/ (YuNet face model) IS included.
    .add_local_dir(
        _CLIPPER_ENGINE,
        remote_path=CLIPPER_DIR,
        ignore=[
            "jobs",
            "test-real",
            "clips",
            "modal-venv",
            "deploy/fly/bin",
            "deploy/fly/tmp",
            "garage",
            "dashboard",
            "__pycache__",
            ".venv",
            "clipit-eval",
            "openshorts-eval",
            ".git",
        ],
    )
)

app = modal.App(APP_NAME, image=image)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


def _setup_paths():
    """Make the vendored gtavi scripts visible where clip.py expects them.

    Must run BEFORE importing clip/reframe/safety/scenes. Creates:
      /root/workspace/gta-test/gtavi-claudecode/scripts  (ssl_fix.py)
      /root/gtavi-claudecode/scripts                     (reframe.py)
    both -> /root/clipper/src/clipper/infra/gtavi (vendored, MIT).
    """
    import os
    import sys

    for target in (
        Path("/root/workspace/gta-test/gtavi-claudecode/scripts"),
        Path("/root/gtavi-claudecode/scripts"),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            continue
        try:
            target.symlink_to(GTAVI_SCRIPTS, target_is_directory=True)
        except OSError:
            pass
    os.environ.setdefault("CLIP_DESK_HOME", CLIPPER_DIR)
    for p in (CLIPPER_DIR, f"{CLIPPER_DIR}/src", GTAVI_SCRIPTS):
        if p not in sys.path:
            sys.path.insert(0, p)


def _load_clip():
    """Import the clipper pipeline inside the container (after paths)."""
    _setup_paths()
    import importlib

    return importlib.import_module("clipper.core.clip")


def _run_cut(source_url, pick, hook, top, min_s, max_s, start, end, polish):
    """Scout + full-fill cut. Shared by the GPU function and the web app.

    Returns the metadata dict (same shape as clip_N_fill.json plus
    volume path and do_not_post). Raises on failure.
    """
    import json
    import shutil
    from pathlib import Path as _Path

    clip = _load_clip()
    import common  # noqa: F401  (imported via clip's ssl_fix patch)

    # 1. Scout: faster-whisper transcribe (GPU if available) + hype
    #    candidates. Writes jobs/<id>/{candidates,transcript}.json.
    clip.run_moments(source_url, top=top, min_s=min_s, max_s=max_s)
    job = common.job_dir(source_url)
    cand = json.loads((job / "candidates.json").read_text(encoding="utf-8"))
    if not cand["candidates"]:
        raise RuntimeError(f"scout found no candidates for {source_url}")

    # 2. Pick: explicit --pick, else the top-scored candidate.
    if pick is not None:
        chosen = next((c for c in cand["candidates"] if c["n"] == pick), None)
        if chosen is None:
            raise ValueError(f"pick {pick} not in candidates "
                             f"({[c['n'] for c in cand['candidates']]})")
    else:
        chosen = max(cand["candidates"], key=lambda c: c["score"])
    n = chosen["n"]

    # 3. Cut: scene snap + safety filter + tracked full-fill 9:16,
    #    captions + hook, -14 LUFS. Writes edits/clip_<n>_fill.mp4/.json.
    #    A BLOCK safety verdict does NOT stop the cut (format proof),
    #    but the json carries safety=BLOCK and it must never post.
    if start is not None and end is not None:
        clip.cut_filltrack(job, n, hook, start=start, end=end)
    else:
        clip.cut_filltrack(job, n, hook)

    mp4 = job / "edits" / f"clip_{n}_fill.mp4"
    meta = json.loads(
        (job / "edits" / f"clip_{n}_fill.json").read_text(encoding="utf-8"))

    # 4. Optional polish: Auto-Editor silence trim (never replaces a
    #    good clip if it changes duration by >2s — see clip.py).
    final_mp4 = mp4
    if polish:
        polished = clip.polish_clip(str(mp4))
        if polished is not None:
            final_mp4 = _Path(polished)

    # 5. Persist to the shared Volume so Fly / anything else can fetch it.
    job_id = job.name
    dest_dir = _Path(VOLUME_MOUNT) / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_mp4 = dest_dir / final_mp4.name
    shutil.copy2(final_mp4, dest_mp4)
    dest_json = dest_dir / f"clip_{n}_fill.json"
    shutil.copy2(job / "edits" / f"clip_{n}_fill.json", dest_json)
    volume.commit()

    safety = meta.get("safety", {})
    return {
        "job_id": job_id,
        "file": str(dest_mp4),
        "meta_file": str(dest_json),
        "download": f"/download/{job_id}/{dest_mp4.name}",
        "pick": n,
        "start": meta.get("start"),
        "end": meta.get("end"),
        "duration": round((meta.get("end", 0) - meta.get("start", 0)), 2),
        "score": chosen.get("score"),
        "hook": hook,
        "title": meta.get("title", ""),
        "source_title": (cand.get("info") or {}).get("title", ""),
        "safety": safety,
        "safety_verdict": safety.get("verdict", "UNKNOWN"),
        # Rule 14: only good clips, always. BLOCK = never post.
        "do_not_post": safety.get("verdict") == "BLOCK",
        "scene_cuts": meta.get("scene_cuts", []),
    }


@app.function(
    gpu=GPU,
    cpu=4,
    memory=8192,
    timeout=1800,
    volumes={VOLUME_MOUNT: volume},
)
def cut_clip(
    source_url: str,
    pick: int | None = None,
    hook: str = "",
    top: int = 8,
    min_s: float = 7,
    max_s: float = 30,
    start: float | None = None,
    end: float | None = None,
    polish: bool = False,
) -> dict:
    """GPU cutter: scout + cut one clip from a source URL.

    source_url: YouTube/Twitch VOD URL or a direct mp4 URL. (A local
    file path from the caller's machine does NOT work remotely — upload
    it first, or pass a URL. clip.py's local-file mode is CPU/VM only.)
    pick: candidate number from the scout (default: top-scored).
    hook: hook text burned at the top of the clip.
    start/end: manual window (Mark Start / Mark End) — scout still
    runs first so the transcript exists for captions.
    polish: Auto-Editor silence trim after the cut.
    Returns metadata dict; the mp4 lives in the Volume at file=.
    """
    return _run_cut(source_url, pick, hook, top, min_s, max_s,
                    start, end, polish)


@app.function(timeout=1800, volumes={VOLUME_MOUNT: volume})
@modal.asgi_app()
def web():
    """HTTP stub so the Fly watcher can call the GPU cutter.

    POST /cut   {"source_url": ..., "pick": 1, "hook": "...",
                 "start": 12, "end": 42, "polish": false}
                -> metadata dict from cut_clip (see above)
    GET  /health
    GET  /download/<job_id>/<file>  -> the finished mp4
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse
    from pydantic import BaseModel

    class CutRequest(BaseModel):
        source_url: str
        pick: int | None = None
        hook: str = ""
        top: int = 8
        min_s: float = 7
        max_s: float = 30
        start: float | None = None
        end: float | None = None
        polish: bool = False

    api = FastAPI(title="vice-city-vibes-clipper")

    @api.get("/health")
    def health():
        return {"ok": True, "app": APP_NAME, "gpu": GPU}

    @api.post("/cut")
    def cut(req: CutRequest):
        # Spawns the GPU function and waits for it (up to the 1800s
        # timeout on this function). The GPU container does the work.
        result = cut_clip.remote(
            req.source_url,
            pick=req.pick,
            hook=req.hook,
            top=req.top,
            min_s=req.min_s,
            max_s=req.max_s,
            start=req.start,
            end=req.end,
            polish=req.polish,
        )
        return result

    @api.get("/download/{job_id}/{filename}")
    def download(job_id: str, filename: str):
        path = Path(VOLUME_MOUNT) / job_id / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="clip not found")
        return FileResponse(path, media_type="video/mp4",
                            filename=filename)

    return api


@app.local_entrypoint()
def main(
    source_url: str,
    pick: int | None = None,
    hook: str = "",
    top: int = 8,
    start: float | None = None,
    end: float | None = None,
    polish: bool = False,
):
    """Manual test from the deploy machine: modal run modal_app.py
    --source-url <url> [--pick 1] [--hook "WAIT FOR IT"]."""
    import json

    result = cut_clip.remote(
        source_url, pick=pick, hook=hook, top=top,
        start=start, end=end, polish=polish,
    )
    print(json.dumps(result, indent=1, ensure_ascii=False))
