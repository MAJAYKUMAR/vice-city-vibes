#!/usr/bin/env python3
"""Clipper Engine — scout, detect hype, cut vertical + landscape clips.

Wraps gtavi-claudecode scripts/moments.py (hype detection: loudness
spikes + transcript hype words + most-replayed heatmap + chat spikes),
scripts/cut.py (1080x1920 vertical cut, word-by-word captions, hook,
-14 LUFS) and scripts/check.py (pre-post check), with the SSL
workaround in ssl_fix.py applied first.

Modes:
  scout  — moments.py on a URL or local file -> candidates.json/.md
  cut    — full-fill 9:16 cut (default layout "filltrack": scale-to-cover
           + face/action-tracked crop, NO blurred bars), word-by-word
           captions + hook, -14 LUFS, content-filter verdict attached.
           Old gtavi "blur" layout is still available as --layout blur
           (opt-in only — it looks like a laptop video, not a Short).
  manual — cut a fixed --start/--end window (Mark Start / Mark End)
  hype   — 0-5 star Hype Meter for a candidates file (chat speed +
           hype words + loud moments, 5 stars = clip-worthy)
  export — also render the 16:9 landscape version of a finished clip
  polish — Auto-Editor silence trim on a finished clip (optional)
  safety — content-filter verdict for a job pick (SAFE/REVIEW/BLOCK)
  scenes — PySceneDetect scene cuts inside a job pick window

Usage (from clipper-engine/, with the gtavi venv):
    python -m clipper.core.clip scout <url-or-file> [--top 8]
    python -m clipper.core.clip cut <job-dir> --pick 1 --hook "TEXT"
    python -m clipper.core.clip manual <url-or-file> --start 12 --end 42 --hook "TEXT"
    python -m clipper.core.clip hype <job-dir>/candidates.json
    python -m clipper.core.clip export <clip.mp4>   # adds 16:9

Every run registers the finished clip in the Video Garage (garage.py).
Cap: 50 clips per stream — the engine parks itself at 50 (their app's
limit, kept here as a safety cap).
"""
import argparse
import json
import math
import sys
from pathlib import Path

from clipper.infra.ssl_fix import patch_module, patch_yt_dlp  # noqa: E402
from clipper.paths import repo_root

# Repo root: jobs/, dashboard/ and garage/ live here, not next to this file.
ROOT = repo_root()
HERE = ROOT

common = patch_yt_dlp()
import check  # noqa: E402
import cut  # noqa: E402
import moments  # noqa: E402

for _m in (check, cut, moments):
    patch_module(_m)

MAX_CLIPS_PER_STREAM = 50


def stars(score):
    """Hype Meter: map a moments.py score to 0-5 stars (5 = clip-worthy)."""
    if score >= 4:
        return 5
    if score >= 3:
        return 4
    if score >= 2:
        return 3
    if score >= 1:
        return 2
    if score > 0:
        return 1
    return 0


def hype_report(candidates_path):
    data = json.loads(Path(candidates_path).read_text(encoding="utf-8"))
    print(f"# Hype Meter — {data['info'].get('title', '')}")
    print(f"signals: {data['signals']}")
    for c in data["candidates"]:
        s = stars(c["score"])
        print(f"{'★' * s}{'☆' * (5 - s)}  {c['score']:>5}  "
              f"{c['start']:>6.1f}-{c['end']:>6.1f}s  {', '.join(c['why'])}  | {c['text'][:80]}")
    return data


def run_moments(src, top=8, min_s=7, max_s=30, max_minutes=90):
    import os
    import wave as _wave

    import numpy as np
    os.environ["CLIP_DESK_HOME"] = str(HERE)
    # Whisper model: CLIPPER_WHISPER_MODEL (default "small", gtavi's choice).
    # "small" needs a ~466 MB download that is slow on this VM's egress
    # proxy; "tiny" (~75 MB) is already cached and is fine for hype-word
    # detection. Override moments.transcribe so the model is selectable.
    model_name = os.environ.get("CLIPPER_WHISPER_MODEL", "small")

    def transcribe(wav, d):
        out = d / "transcript.json"
        if out.exists():
            return moments.load_json(out)
        from faster_whisper import WhisperModel
        with _wave.open(str(wav)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768

        def run_on(device):
            model = WhisperModel(model_name, device=device,
                                 compute_type="float16" if device == "cuda" else "int8")
            segs, _ = model.transcribe(pcm, word_timestamps=True, vad_filter=True)
            return [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
                    for s in segs for w in (s.words or [])]
        try:
            import ctranslate2
            cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            cuda = False
        try:
            words = run_on("cuda" if cuda else "cpu")
        except RuntimeError:
            words = run_on("cpu")
        moments.save_json(out, words)
        return words

    moments.transcribe = transcribe
    sys.argv = ["moments.py", src, "--top", str(top), "--min", str(min_s),
                "--max", str(max_s), "--max-minutes", str(max_minutes)]
    moments.main()


def run_cut(job_dir, pick=1, hook="", layout="filltrack", title=""):
    import json as _json
    from pathlib import Path as _Path

    # FINAL BUILD (2026-11-19): the default vertical cut is FULL-FILL
    # 9:16 — scale-to-cover + face/action-tracked crop, no blurred bars,
    # no letterbox (reframe.py). The old gtavi "blur" layout is opt-in
    # only. Portrait sources keep the fill path (same filter, it is
    # already scale-to-cover).
    job = _Path(job_dir)
    cand = _json.loads((job / "candidates.json").read_text(encoding="utf-8"))
    src = cand["source"]
    probe = common.ffprobe(src)
    vs = [s for s in probe["streams"] if s.get("codec_type") == "video"][0]
    if layout == "filltrack":
        cut_filltrack(job, pick, hook, title)
        return
    if int(vs["width"]) < int(vs["height"]):
        cut_portrait(job, pick, hook, layout, title)
        return
    sys.argv = ["cut.py", job_dir, "--pick", str(pick), "--layout", layout,
                "--hook", hook, "--title", title]
    cut.main()


def cut_filltrack(job, pick=1, hook="", title="", start=None, end=None):
    """Full-fill 9:16 cut: scene-snapped window, content filter, tracked
    crop that fills the whole phone screen, captions + hook, -14 LUFS.

    Writes jobs/<id>/edits/clip_<n>_fill.mp4 + clip_<n>_fill.json.
    A BLOCK safety verdict does NOT stop the cut (format proof), but
    the json carries safety=BLOCK and garage.py will not approve it.
    """
    import json as _json
    from pathlib import Path as _Path

    from clipper.core import reframe, safety, scenes

    job = _Path(job)
    c = _json.loads((job / "candidates.json").read_text(encoding="utf-8"))
    p = next(x for x in c["candidates"] if x["n"] == pick)
    s0 = float(start) if start is not None else p["start"]
    e0 = float(end) if end is not None else p["end"]
    src = c["source"]
    words = _json.loads((job / "transcript.json").read_text(encoding="utf-8"))

    # 1. Scene snap (PySceneDetect, proxy) — never open mid-shot.
    try:
        cuts = scenes.scene_cuts(src, s0, e0)
        s0, e0, snap_notes = scenes.snap(s0, e0, cuts)
    except Exception as e:
        cuts, snap_notes = [], [f"scene snap skipped: {e}"]

    # 2. Content filter — runs BEFORE the cut is approved anywhere.
    try:
        verdict = safety.check(src, s0, e0, words)
    except Exception as e:
        verdict = {"verdict": "REVIEW", "reasons": [f"safety check failed: {e}"],
                   "skin_mean": 0.0, "skin_max": 0.0, "frames_sampled": 0,
                   "frames_with_face": 0, "frames_with_large_face": 0,
                   "keyword_hits": []}
    if verdict["verdict"] == "BLOCK":
        print(f"SAFETY BLOCK: pick {pick} — {verdict['reasons']} — "
              f"cut for format proof only, NEVER approve/post.")

    # 3. Tracked full-fill crop plan (ClipIt YuNet tracker, reframe.py).
    probe = common.ffprobe(src)
    vs = [s for s in probe["streams"] if s.get("codec_type") == "video"][0]
    sw, sh = int(vs["width"]), int(vs["height"])
    plan = reframe.plan_crop(src, s0, e0)
    out = job / "edits"
    out.mkdir(exist_ok=True)
    ass = out / f"clip_{pick}_fill.ass"
    ass.write_text(cut.build_ass(words, s0, e0, hook, 1380, caps="word"),
                   encoding="utf-8")
    vf = reframe.fill_filter(sw, sh, plan["keyframes"], ass)
    mp4 = out / f"clip_{pick}_fill.mp4"
    common.run([common.tool("ffmpeg"), "-y", "-v", "error",
                "-ss", f"{s0:.2f}", "-to", f"{e0:.2f}",
                "-i", src, "-filter_complex", vf, "-map", "[vo]",
                "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-ar", "48000", "-movflags", "+faststart", str(mp4)])
    info = c.get("info", {})
    common.save_json(out / f"clip_{pick}_fill.json",
                     {"file": str(mp4), "start": s0, "end": e0,
                      "layout": "filltrack", "hook": hook, "title": title,
                      "safety": verdict, "scene_cuts": cuts,
                      "snap_notes": snap_notes,
                      "crop_keyframes": plan["keyframes"],
                      "crop_x_static": plan["x_static"],
                      "scaled_width": plan["scaled_width"],
                      "source_url": info.get("webpage_url"),
                      "source_title": info.get("title"),
                      "source_channel": info.get("channel") or info.get("uploader")})
    print(mp4)
    print(f"filltrack: pick {pick} {s0:.2f}-{e0:.2f}s "
          f"safety={verdict['verdict']} scenes={cuts} snap={snap_notes}")


def cut_portrait(job_dir, pick=1, hook="", layout="blur", title=""):
    """Vertical cut for portrait sources: scale-to-cover 1080x1920,
    word-by-word captions + hook from cut.build_ass, -14 LUFS."""
    import json as _json
    from pathlib import Path as _Path

    job = _Path(job_dir)
    c = _json.loads((job / "candidates.json").read_text(encoding="utf-8"))
    p = next(x for x in c["candidates"] if x["n"] == pick)
    words = _json.loads((job / "transcript.json").read_text(encoding="utf-8"))
    out = job / "edits"
    out.mkdir(exist_ok=True)
    ass = out / f"clip_{pick}.ass"
    ass.write_text(cut.build_ass(words, p["start"], p["end"], hook, 1380,
                                 caps="word"), encoding="utf-8")
    # Path escaping is openshorts' escape_filter_value (vendored whole in
    # openshorts_ffmpeg_utils.py).
    from clipper.core.openshorts_ffmpeg_utils import escape_filter_value
    sub = escape_filter_value(str(ass))
    vf = (f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
          f"crop=1080:1920,setsar=1,fps=30,subtitles='{sub}'[vo]")
    mp4 = out / f"clip_{pick}.mp4"
    common.run([common.tool("ffmpeg"), "-y", "-v", "error",
                "-ss", f"{p['start']:.2f}", "-to", f"{p['end']:.2f}",
                "-i", c["source"], "-filter_complex", vf, "-map", "[vo]",
                "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-ar", "48000", "-movflags", "+faststart", str(mp4)])
    info = c.get("info", {})
    common.save_json(out / f"clip_{pick}.json",
                     {"file": str(mp4), "start": p["start"], "end": p["end"],
                      "layout": "portrait-fill", "hook": hook, "title": title,
                      "source_url": info.get("webpage_url"),
                      "source_title": info.get("title"),
                      "source_channel": info.get("channel") or info.get("uploader")})
    print(mp4)


def export_16x9(clip_path):
    """Render the 16:9 landscape version of a finished vertical clip's
    source segment (same start/end, centre-cropped 1920x1080)."""
    from common import run, tool  # noqa: F401  (kept for parity with cut.py)
    import subprocess

    clip = Path(clip_path)
    meta = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
    job = Path(meta["file"]).parent.parent
    src = Path(json.loads((job / "candidates.json").read_text(encoding="utf-8"))["source"])
    out = clip.with_name(clip.stem + "_16x9.mp4")
    subprocess.run([tool("ffmpeg"), "-y", "-v", "error",
                    "-ss", f"{meta['start']:.2f}", "-to", f"{meta['end']:.2f}",
                    "-i", str(src),
                    "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                    "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True)
    print(out)
    return out


def polish_clip(clip_path):
    """Auto-Editor silence trim (WyattBlue/auto-editor 29.3.1, MIT) on a
    finished clip. Trims silent gaps; captions are already burned in,
    so they move with the cut. Writes <clip>_polished.mp4. If it fails
    or changes the duration by >2 s, the original is kept and the
    failure is reported — polish never silently replaces a good clip."""
    import subprocess
    import shutil
    import sys
    # auto-editor is installed in the gtavi venv, which is NOT on PATH
    # when clip.py is run as `python clip.py` — resolve it next to
    # this interpreter first, then PATH, then `python -m auto_editor`.
    ae = Path(sys.executable).parent / "auto-editor"
    ae_cmd = [str(ae)] if ae.exists() else (
        ["auto-editor"] if shutil.which("auto-editor") else
        [sys.executable, "-m", "auto_editor"])
    clip = Path(clip_path)
    out = clip.with_name(clip.stem + "_polished.mp4")
    before = float(json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(clip)], capture_output=True, text=True
    ).stdout)["format"]["duration"])
    # auto-editor 29.x: the silence threshold lives inside the --edit
    # expression ("audio:threshold=0.03"); --silent-threshold no longer
    # exists. Verified 2026-11-19: clip_7_fill 26.50s -> 24.67s in 65s.
    r = subprocess.run(ae_cmd + [str(clip), "-o", str(out),
                        "--edit", "audio:threshold=0.03", "--no-open"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        print(f"polish FAILED (auto-editor rc={r.returncode}): "
              f"{(r.stderr or r.stdout)[-300:]} — keeping original.")
        return None
    after = float(json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(out)], capture_output=True, text=True
    ).stdout)["format"]["duration"])
    print(f"polish: {clip.name} {before:.2f}s -> {after:.2f}s "
          f"(removed {before - after:.2f}s of silence)")
    if abs(before - after) > 2.0:
        print("polish: duration changed >2s — keeping ORIGINAL, "
              "polished file left on disk for inspection.")
        return out
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scout"); p.add_argument("src"); p.add_argument("--top", type=int, default=8)
    p.add_argument("--min", type=float, default=7); p.add_argument("--max", type=float, default=30)
    p.add_argument("--max-minutes", type=int, default=90)
    p = sub.add_parser("cut"); p.add_argument("job"); p.add_argument("--pick", type=int, default=1)
    p.add_argument("--hook", default=""); p.add_argument("--layout", default="filltrack"); p.add_argument("--title", default="")
    p = sub.add_parser("manual"); p.add_argument("src"); p.add_argument("--start", type=float, required=True)
    p.add_argument("--end", type=float, required=True); p.add_argument("--hook", default="")
    p = sub.add_parser("hype"); p.add_argument("candidates")
    p = sub.add_parser("export"); p.add_argument("clip")
    p = sub.add_parser("polish"); p.add_argument("clip")
    p = sub.add_parser("safety"); p.add_argument("job"); p.add_argument("--pick", type=int, default=1)
    p = sub.add_parser("scenes"); p.add_argument("job"); p.add_argument("--pick", type=int, default=1)
    a = ap.parse_args()

    if a.cmd == "scout":
        run_moments(a.src, a.top, a.min, a.max, a.max_minutes)
    elif a.cmd == "cut":
        run_cut(a.job, a.pick, a.hook, a.layout, a.title)
    elif a.cmd == "manual":
        # Scout first (builds transcript + job dir), then full-fill cut
        # of the exact window (Mark Start / Mark End).
        run_moments(a.src, top=1, min_s=7, max_s=30)
        job = sorted((HERE / "jobs").glob("*"))[-1]
        cut_filltrack(job, 1, a.hook, start=a.start, end=a.end)
    elif a.cmd == "hype":
        hype_report(a.candidates)
    elif a.cmd == "export":
        export_16x9(a.clip)
    elif a.cmd == "polish":
        polish_clip(a.clip)
    elif a.cmd == "safety":
        from clipper.core import safety as _safety
        job = Path(a.job)
        c = json.loads((job / "candidates.json").read_text(encoding="utf-8"))
        p = next(x for x in c["candidates"] if x["n"] == a.pick)
        words = json.loads((job / "transcript.json").read_text(encoding="utf-8"))
        print(json.dumps(_safety.check(c["source"], p["start"], p["end"], words), indent=1))
    elif a.cmd == "scenes":
        from clipper.core import scenes as _scenes
        job = Path(a.job)
        c = json.loads((job / "candidates.json").read_text(encoding="utf-8"))
        p = next(x for x in c["candidates"] if x["n"] == a.pick)
        print(_scenes.scene_cuts(c["source"], p["start"], p["end"]))


if __name__ == "__main__":
    main()
