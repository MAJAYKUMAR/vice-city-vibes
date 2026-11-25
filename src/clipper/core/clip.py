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
  copyright — copyright-risk verdict for a job pick (CLEAR/REVIEW/BLOCK)
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
from clipper.core import captions as _captions  # noqa: E402  (styled captions; stdlib + ffmpeg only)

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

# ITEM 4 — payoff front-load: the candidate window's highest-signal
# moment lands at ~PEAK_TARGET_OFFSET s into the output clip instead
# of mid-clip (feed data: payoff in the first 2 s held 77.5% of
# viewers; a slow open held 24.4%). The slide never changes the clip
# duration and never crosses a scene cut (see frontload_window).
PEAK_TARGET_OFFSET = 0.5


def _startup_disk_cleanup():
    """ITEM 9 — delete stale ``*.part`` files older than one run.

    Best-effort startup cleanup of the jobs tree (crashed yt-dlp
    runs leave partial downloads in ``jobs/<video-id>/...``). Never
    crashes the run: any failure is swallowed, and only files older
    than 24 h are removed, so an in-flight download is never touched.
    """
    try:
        from clipper.core.disk_guard import startup_cleanup
        deleted = startup_cleanup(HERE / "jobs")
        if deleted:
            print(f"disk guard: removed {len(deleted)} stale .part file(s)")
    except Exception:
        pass


def _disk_preflight_for_cut(job, pick, start=None, end=None):
    """ITEM 9 — pre-flight disk guard for a cut (top of cut_* only).

    Estimates the need as source size + 2x clip size
    (``disk_guard.estimate_for_clip``; clip size from the window
    duration at the 12 Mbps master ceiling) and refuses — raising
    ``DiskSpaceError`` — when free space on the output filesystem is
    below it, before any scene snap / encode work starts. If the
    source cannot be read yet (missing candidates/source), the guard
    is skipped and the cut fails naturally further down, as before.
    """
    from clipper.core.disk_guard import (
        estimate_clip_bytes, estimate_for_clip, preflight,
    )
    import os as _os

    job = Path(job)
    try:
        c = json.loads((job / "candidates.json").read_text(encoding="utf-8"))
        src = c["source"]
        if start is None or end is None:
            p = next(x for x in c["candidates"] if x["n"] == pick)
            start = p["start"] if start is None else start
            end = p["end"] if end is None else end
        source_bytes = _os.path.getsize(src)
        clip_bytes = estimate_clip_bytes(float(end) - float(start))
    except Exception:
        return
    needed = estimate_for_clip(source_bytes, clip_bytes)
    preflight(job / "edits", needed,
              what=f"cut pick {pick} ({Path(src).name})")


def _loudness_peak(src, s0, e0):
    """Loudness peak (absolute seconds) inside [s0, e0], or None.

    Fallback when a candidate carries no recorded "peak" (pipeline
    picks are hand-written, e.g. jobs/top-gta6-20261122). Mono 16 kHz
    decode + RMS per 0.1 s bin — the same recipe as moments.audio_rms.
    Best-effort: any failure returns None and the slide is skipped.
    """
    import subprocess
    import tempfile
    import wave as _wave

    try:
        import numpy as np
    except Exception:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            wav = str(Path(td) / "win.wav")
            subprocess.run([common.tool("ffmpeg"), "-y", "-v", "error",
                            "-ss", f"{s0:.2f}", "-to", f"{e0:.2f}",
                            "-i", str(src), "-ac", "1", "-ar", "16000", wav],
                           check=True)
            with _wave.open(wav) as w:
                x = np.frombuffer(w.readframes(w.getnframes()),
                                  dtype=np.int16).astype(np.float32) / 32768
        if len(x) < 1600:  # < 0.1 s of audio: no meaningful peak
            return None
        b = 1600  # 0.1 s bins
        n = len(x) // b
        rms = np.sqrt((x[:n * b].reshape(n, b) ** 2).mean(1) + 1e-9)
        return round(s0 + (int(np.argmax(rms)) + 0.5) * 0.1, 2)
    except Exception:
        return None


def frontload_window(s0, e0, peak, cuts, src_dur, target=PEAK_TARGET_OFFSET):
    """Slide [s0, e0] so `peak` lands at ~`target` s into the clip.

    Returns (new_s0, new_e0, notes). Duration is unchanged. Two hard
    guards, both of which leave the window untouched:
    - scene: if a cut falls between the old and new start, the peak is
      in a different scene than the window start — sliding would break
      the scene snap (the clip would open mid-shot in another scene).
    - end of source: if the slide would run past `src_dur`, it is
      clamped to the latest start that keeps the full duration (the
      peak then lands later than `target`, noted); if even that is no
      later than the old start, the window is left alone.
    A peak already within the first `target` s needs no slide. A peak
    sitting just before the window start (word-snapping can push s0
    past it) slides the window earlier to pull it in.
    """
    notes = []
    dur = e0 - s0
    if peak is None:
        return s0, e0, ["payoff front-load skipped: no peak timestamp"]
    try:
        peak = float(peak)
    except (TypeError, ValueError):
        return s0, e0, ["payoff front-load skipped: unparsable peak timestamp"]
    if peak < s0 - 2.0 or peak > e0 + 2.0:
        return s0, e0, [f"payoff front-load skipped: peak {peak:.2f}s "
                        f"outside window {s0:.2f}-{e0:.2f}s"]
    new_s0 = peak - target
    if s0 <= peak <= s0 + target:
        return s0, e0, [f"payoff front-load: peak {peak:.2f}s already at "
                        f"{peak - s0:.2f}s into window — no slide"]
    if new_s0 < 0:
        return s0, e0, [f"payoff front-load skipped: slide to {new_s0:.2f}s "
                        f"would start before 0"]
    if new_s0 > s0 and src_dur is not None:
        # Forward slide: keep the full duration inside the source.
        if new_s0 + dur > src_dur:
            clamped = src_dur - dur
            if clamped <= s0:
                return s0, e0, [f"payoff front-load skipped: slide to "
                                f"{new_s0:.2f}s would run past end of "
                                f"source ({src_dur:.2f}s)"]
            notes.append(f"payoff front-load clamped to source end: "
                         f"start {new_s0:.2f} -> {clamped:.2f}s")
            new_s0 = clamped
    lo, hi = min(s0, new_s0), max(s0, new_s0)
    # Strict bounds: a cut exactly AT the new start is fine — the clip
    # then opens on a scene boundary, which is the snap ideal.
    if any(lo < t < hi for t in (cuts or [])):
        return s0, e0, [f"payoff front-load skipped: scene cut inside "
                        f"({lo:.2f}, {hi:.2f}) — peak is in a different "
                        f"scene than the window start"]
    notes.append(f"payoff front-load: start slid {s0:.2f} -> {new_s0:.2f}s "
                 f"so peak {peak:.2f}s lands at {peak - new_s0:.2f}s")
    return new_s0, new_s0 + dur, notes


def source_fps(probe, default=30):
    """Source fps from an ffprobe dict, capped at 60, never upsampled.

    Prefers ``avg_frame_rate`` (the real rate) over ``r_frame_rate``
    (often the container max). Rounds to the nearest whole frame rate
    (29.97 -> 30, 59.94 -> 60) and clamps to [1, 60]. Returns `default`
    when the probe is missing or unparseable.
    """
    try:
        vs = [s for s in probe["streams"]
              if s.get("codec_type") == "video"][0]
        for key in ("avg_frame_rate", "r_frame_rate"):
            num, _, den = (vs.get(key) or "0/0").partition("/")
            num, den = float(num or 0), float(den or 0)
            if num > 0 and den > 0:
                return max(1, min(60, int(round(num / den))))
    except Exception:
        pass
    return default


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
    _startup_disk_cleanup()  # ITEM 9 — stale *.part cleanup on startup
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


def run_cut(job_dir, pick=1, hook="", layout="filltrack", title="",
            caption_style=None, teaser=False):
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
        cut_filltrack(job, pick, hook, title, caption_style=caption_style,
                      teaser=teaser)
        return
    if layout in ("cam-top", "game-top", "cam-corner"):
        # Facecam layout presets (clipper.core.layouts, port of
        # OpenClip cutlayout.mjs): same cut_filltrack path, the
        # layout filter replaces reframe.fill_filter inside it.
        # cam_box comes from reframe.plan_crop inside cut_filltrack.
        cut_filltrack(job, pick, hook, title, caption_style=caption_style,
                      layout=layout, teaser=teaser)
        return
    if int(vs["width"]) < int(vs["height"]):
        cut_portrait(job, pick, hook, layout, title,
                     caption_style=caption_style)
        return
    sys.argv = ["cut.py", job_dir, "--pick", str(pick), "--layout", layout,
                "--hook", hook, "--title", title]
    cut.main()


def cut_filltrack(job, pick=1, hook="", title="", start=None, end=None,
                  caption_style=None, layout="filltrack", teaser=False):
    """Full-fill 9:16 cut: scene-snapped window, content filter, tracked
    crop that fills the whole phone screen, captions + hook, -14 LUFS.

    Writes jobs/<id>/edits/clip_<n>_fill.mp4 + clip_<n>_fill.json.
    A BLOCK safety verdict does NOT stop the cut (format proof), but
    the json carries safety=BLOCK and garage.py will not approve it.
    Same for a BLOCK copyright verdict (step 2b): cut for format
    proof only, json carries copyright=BLOCK, garage.py refuses it.

    caption_style: None (default) keeps today's plain word captions
    from cut.build_ass exactly; "karaoke"/"pop"/"minimal" burn the
    styled captions from clipper.core.captions instead.
    layout: "filltrack" (default) or one of "cam-top"/"game-top"/
    "cam-corner" (clipper.core.layouts facecam presets, same
    1080x1920 output, caption safe zone cap_y=1080 intact).
    teaser: opt-in cold-open teaser (clipper.core.teaser, port of
    OpenClip edl.mjs) — repeats the payoff up front, then the full
    main clip. NEVER the default, mutually exclusive with the
    front-load slide: when on, frontload_window is skipped entirely.
    """
    _disk_preflight_for_cut(job, pick, start, end)  # ITEM 9 — disk guard
    import json as _json
    from pathlib import Path as _Path

    from clipper.core import copyright as copyright_check, reframe, safety, scenes

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

    # 1b. Payoff front-load (ITEM 4) — slide the window so the
    # candidate's peak moment lands at ~0.5 s in the output clip
    # instead of mid-clip. Peak: the scan's recorded peak when the
    # candidate has one, else the loudness peak inside this window.
    # Never slides across a scene cut and never changes the duration
    # (frontload_window). Runs BEFORE the safety / copyright checks
    # so they sample the final window. The probe is hoisted here for
    # the end-of-source clamp and reused by step 3.
    try:
        probe = common.ffprobe(src)
        src_dur = float(probe["format"]["duration"])
    except Exception:
        probe, src_dur = None, None
    peak = p.get("peak")
    if peak is None:
        peak = _loudness_peak(src, s0, e0)
        peak_source = "loudness peak (candidate has no recorded peak)"
    else:
        peak_source = "recorded scan peak"
    # Teaser-open (clipper.core.teaser, port of OpenClip edl.mjs):
    # opt-in only, mutually exclusive with the front-load slide —
    # the teaser repeats the payoff up front, the slide moves the
    # window, never both at once. When on, the slide is skipped
    # entirely and the edit becomes [cold teaser] + [full main pass].
    # payoff_is_late() is provided, NOT wired to auto-fire — Ajay
    # decides per clip.
    segments = None
    if teaser:
        from clipper.core import teaser as _teaser
        segments = _teaser.teaser_segments(s0, e0, peak=peak)
        frontload_notes = ["payoff front-load skipped: teaser-open "
                           "enabled (mutually exclusive)"]
        print(f"teaser: pick {pick} peak={peak} ({peak_source}) — "
              f"segments={segments} "
              f"(+{_teaser.teaser_duration(segments):.2f}s)")
    else:
        s0, e0, frontload_notes = frontload_window(s0, e0, peak, cuts, src_dur)
    print(f"front-load: pick {pick} peak={peak} ({peak_source}) — "
          f"{'; '.join(frontload_notes)}")

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

    # 2b. Copyright-risk check (stage 11) — same fail-closed pattern:
    # on exception it caps at REVIEW, never CLEAR, never a crash.
    # A BLOCK verdict does NOT stop the cut (format proof), but the
    # json carries copyright=BLOCK and garage.py will not approve it.
    streamer = ((c.get("info") or {}).get("channel")
                or (c.get("info") or {}).get("uploader"))
    try:
        copyright_verdict = copyright_check.check_clip(
            src, s0, e0, words, streamer=streamer)
    except Exception as e:
        copyright_verdict = {"verdict": "REVIEW",
                             "reasons": [f"copyright check failed: {e}"],
                             "mean_db": None, "max_db": None,
                             "muted_segments": 0, "muted_fraction": 0.0,
                             "word_count": 0, "sustained_bed": False,
                             "blocklist_hit": None}
    if copyright_verdict["verdict"] == "BLOCK":
        print(f"COPYRIGHT BLOCK: pick {pick} — "
              f"{copyright_verdict['reasons']} — "
              f"cut for format proof only, NEVER approve/post.")

    # 3. Tracked full-fill crop plan (ClipIt YuNet tracker, reframe.py).
    # probe was hoisted to step 1b (end-of-source clamp); re-probe only
    # if that failed.
    if probe is None:
        probe = common.ffprobe(src)
    vs = [s for s in probe["streams"] if s.get("codec_type") == "video"][0]
    sw, sh = int(vs["width"]), int(vs["height"])
    plan = reframe.plan_crop(src, s0, e0)
    out = job / "edits"
    out.mkdir(exist_ok=True)
    ass = out / f"clip_{pick}_fill.ass"
    # Captions follow the main pass only (teaser.main_segments /
    # edl.mjs retimeWords): the teaser shows the hook text as a label,
    # never word captions. The main segment is the whole [s0, e0]
    # window, so the .ass build is unchanged when there is no teaser.
    cap_s0, cap_e0 = s0, e0
    if segments:
        from clipper.core import teaser as _teaser
        _main_segs = _teaser.main_segments(segments)
        if _main_segs:
            cap_s0, cap_e0 = _main_segs[0]["start"], _main_segs[0]["end"]
    if caption_style is None:
        # Default: today's plain word captions, byte-identical path.
        ass.write_text(cut.build_ass(words, cap_s0, cap_e0, hook, 1080,
                                     caps="word"),
                       encoding="utf-8")
    else:
        _captions._validate_style(caption_style)
        ass.write_text(_captions.build_ass(words, cap_s0, cap_e0, hook, 1080,
                                           style=caption_style),
                       encoding="utf-8")
    # Source fps preserved, capped at 60 (never upsampled); 30 when the
    # probe failed — the fps= filter below then keeps the source rate.
    fps = source_fps(probe)
    if layout in ("cam-top", "game-top", "cam-corner"):
        # Facecam layout preset (clipper.core.layouts): drop-in
        # replacement for reframe.fill_filter — same [vo] contract,
        # same lanczos + unsharp chain, same subtitles handling.
        # cam_box: reframe.plan_crop's facecam detection when present,
        # else None (layouts falls back to the defaultLayout box,
        # reshaped to the panel). game_box: None (full frame).
        from clipper.core import layouts as _layouts
        vf = _layouts.layout_filter(layout, sw, sh,
                                    cam_box=plan.get("cam_box"),
                                    game_box=None, ass_path=ass, fps=fps)
    else:
        vf = reframe.fill_filter(sw, sh, plan["keyframes"], ass, fps=fps)
    mp4 = out / f"clip_{pick}_fill.mp4"
    cold = None
    if segments:
        cold = next((s for s in segments if s.get("kind") == "cold"), None)
    if cold is not None:
        # Teaser encode: two-input concat — cold teaser, then the
        # full main pass. The vf chain is written for [0:v]; retarget
        # its first filter to the concat output [cutv]. Audio comes
        # from the concat output [cuta] — and because [cuta] is a
        # filtergraph output, the -af loudnorm used by the single-
        # input path cannot apply to it (ffmpeg: simple + complex
        # filtering cannot mix on one stream), so loudnorm runs
        # INSIDE the filtergraph here, as [aout].
        from clipper.core import teaser as _teaser
        cold_dur = cold["end"] - cold["start"]
        vf = vf.replace("[0:v]", "[cutv]", 1)
        flash = _teaser.teaser_flash_filter(cold_dur)
        if flash:
            vf = vf.replace(",subtitles=", f",{flash},subtitles=", 1)
        vf = (vf + ";[cuta]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
        common.run([common.tool("ffmpeg"), "-y", "-v", "error",
                    "-ss", f"{cold['start']:.2f}", "-to", f"{cold['end']:.2f}",
                    "-i", src,
                    "-ss", f"{s0:.2f}", "-to", f"{e0:.2f}",
                    "-i", src,
                    "-filter_complex",
                    "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[cutv][cuta];" + vf,
                    "-map", "[vo]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "17",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-ar", "48000", "-movflags", "+faststart", str(mp4)])
    else:
        common.run([common.tool("ffmpeg"), "-y", "-v", "error",
                    "-ss", f"{s0:.2f}", "-to", f"{e0:.2f}",
                    "-i", src, "-filter_complex", vf, "-map", "[vo]",
                    "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "17",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-ar", "48000", "-movflags", "+faststart", str(mp4)])
    # 4. Post-encode QC gate (ITEM 5) — runs AFTER encoding, BEFORE the
    # clip is marked post-ready. Never crashes the cut: any gate error
    # caps at UNAVAILABLE. A FAIL means the clip stays out of the queue.
    try:
        from clipper.core import validate as _validate
        qc_result = _validate.validate_clip(mp4, title=title, write_sidecar=False)
    except Exception as e:  # noqa: BLE001 — gate must never crash the cut
        qc_result = {"verdict": "UNAVAILABLE", "passed": False, "available": False,
                     "reasons": [f"qc gate error: {e}"], "notes": [],
                     "audio": {}, "export": {}}
    if not qc_result.get("passed"):
        print(f"QC {qc_result.get('verdict')}: pick {pick} — "
              f"{qc_result.get('reasons')} — clip stays out of the queue.")
    info = c.get("info", {})
    clip_json = {"file": str(mp4), "start": s0, "end": e0,
                 "layout": layout, "hook": hook, "title": title,
                 "caption_style": caption_style,
                 "source_fps": fps,
                 "safety": verdict, "copyright": copyright_verdict,
                 "qc": qc_result,
                 "peak": peak, "peak_source": peak_source,
                 "frontload_notes": frontload_notes,
                 "scene_cuts": cuts,
                 "snap_notes": snap_notes,
                 "crop_keyframes": plan["keyframes"],
                 "crop_x_static": plan["x_static"],
                 "scaled_width": plan["scaled_width"],
                 "cam_box": plan.get("cam_box"),
                 "source_url": info.get("webpage_url"),
                 "source_title": info.get("title"),
                 "source_channel": info.get("channel") or info.get("uploader")}
    if segments is not None:
        # Teaser was on: record the edit (cold + main) and the extra
        # seconds it adds. Absent entirely when the teaser is off.
        from clipper.core import teaser as _teaser
        clip_json["teaser"] = segments
        clip_json["teaser_seconds"] = _teaser.teaser_duration(segments)
    common.save_json(out / f"clip_{pick}_fill.json", clip_json)
    print(mp4)
    print(f"filltrack: pick {pick} {s0:.2f}-{e0:.2f}s fps={fps} "
          f"layout={layout} "
          f"teaser={'on ' + str(segments) if segments is not None else 'off'} "
          f"safety={verdict['verdict']} "
          f"copyright={copyright_verdict['verdict']} "
          f"qc={qc_result.get('verdict')} "
          f"peak={peak} "
          f"scenes={cuts} snap={snap_notes} frontload={frontload_notes}")


def cut_portrait(job_dir, pick=1, hook="", layout="blur", title="",
                 caption_style=None):
    """Vertical cut for portrait sources: scale-to-cover 1080x1920,
    word-by-word captions + hook from cut.build_ass, -14 LUFS.

    caption_style: None (default) keeps today's plain word captions;
    "karaoke"/"pop"/"minimal" use clipper.core.captions instead."""
    _disk_preflight_for_cut(job_dir, pick)  # ITEM 9 — disk guard
    import json as _json
    from pathlib import Path as _Path

    job = _Path(job_dir)
    c = _json.loads((job / "candidates.json").read_text(encoding="utf-8"))
    p = next(x for x in c["candidates"] if x["n"] == pick)
    words = _json.loads((job / "transcript.json").read_text(encoding="utf-8"))
    out = job / "edits"
    out.mkdir(exist_ok=True)
    ass = out / f"clip_{pick}.ass"
    if caption_style is None:
        # Default: today's plain word captions, byte-identical path.
        ass.write_text(cut.build_ass(words, p["start"], p["end"], hook, 1080,
                                     caps="word"), encoding="utf-8")
    else:
        _captions._validate_style(caption_style)
        ass.write_text(_captions.build_ass(words, p["start"], p["end"], hook,
                                           1080, style=caption_style),
                       encoding="utf-8")
    # Path escaping is openshorts' escape_filter_value (vendored whole in
    # openshorts_ffmpeg_utils.py).
    from clipper.core.openshorts_ffmpeg_utils import escape_filter_value
    sub = escape_filter_value(str(ass))
    # Source fps preserved, capped at 60 (never upsampled); 30 when the
    # probe fails — same rule as cut_filltrack.
    try:
        fps = source_fps(common.ffprobe(c["source"]))
    except Exception:
        fps = 30
    # Scaler (ITEM 1 fix, same as reframe.fill_filter): lanczos + a
    # light unsharp after the scale — the default bilinear scale
    # softens the 1080x1920 upscale.
    vf = (f"[0:v]scale=1080:1920:flags=lanczos:force_original_aspect_ratio=increase,"
          f"unsharp=5:5:1.0:5:5:0.5,crop=1080:1920,setsar=1,fps={fps},subtitles='{sub}'[vo]")
    mp4 = out / f"clip_{pick}.mp4"
    common.run([common.tool("ffmpeg"), "-y", "-v", "error",
                "-ss", f"{p['start']:.2f}", "-to", f"{p['end']:.2f}",
                "-i", c["source"], "-filter_complex", vf, "-map", "[vo]",
                "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:v", "libx264", "-preset", "medium", "-crf", "17",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-ar", "48000", "-movflags", "+faststart", str(mp4)])
    # Post-encode QC gate (ITEM 5) — same as cut_filltrack step 4: runs
    # AFTER encoding, BEFORE the clip is marked post-ready; never crashes
    # the cut, a FAIL means the clip stays out of the queue.
    try:
        from clipper.core import validate as _validate
        qc_result = _validate.validate_clip(mp4, title=title, write_sidecar=False)
    except Exception as e:  # noqa: BLE001 — gate must never crash the cut
        qc_result = {"verdict": "UNAVAILABLE", "passed": False, "available": False,
                     "reasons": [f"qc gate error: {e}"], "notes": [],
                     "audio": {}, "export": {}}
    if not qc_result.get("passed"):
        print(f"QC {qc_result.get('verdict')}: pick {pick} — "
              f"{qc_result.get('reasons')} — clip stays out of the queue.")
    info = c.get("info", {})
    common.save_json(out / f"clip_{pick}.json",
                     {"file": str(mp4), "start": p["start"], "end": p["end"],
                      "layout": "portrait-fill", "hook": hook, "title": title,
                      "caption_style": caption_style,
                      "source_fps": fps,
                      "qc": qc_result,
                      "source_url": info.get("webpage_url"),
                      "source_title": info.get("title"),
                      "source_channel": info.get("channel") or info.get("uploader")})
    print(mp4)
    print(f"portrait-fill: pick {pick} fps={fps} qc={qc_result.get('verdict')}")


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
    # Source fps preserved, capped at 60 (never upsampled); 30 when the
    # probe fails — same rule as the vertical cuts. CRF 17 unifies this
    # derivative with the posted vertical clip (was a CRF 20 outlier).
    try:
        fps = source_fps(common.ffprobe(str(src)))
    except Exception:
        fps = 30
    subprocess.run([tool("ffmpeg"), "-y", "-v", "error",
                    "-ss", f"{meta['start']:.2f}", "-to", f"{meta['end']:.2f}",
                    "-i", str(src),
                    "-vf", f"scale=1920:1080:flags=lanczos:force_original_aspect_ratio=increase,unsharp=5:5:1.0:5:5:0.5,crop=1920:1080,fps={fps}",
                    "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "17",
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
    p.add_argument("--teaser", action="store_true",
                   help="cold-open teaser: repeat the payoff up front, then the full clip (opt-in; skips the front-load slide)")
    p.add_argument("--caption-style", choices=_captions.STYLE_NAMES, default=None,
                   help="styled captions (karaoke/pop/minimal); omit for today's plain word captions")
    p = sub.add_parser("manual"); p.add_argument("src"); p.add_argument("--start", type=float, required=True)
    p.add_argument("--end", type=float, required=True); p.add_argument("--hook", default="")
    p.add_argument("--teaser", action="store_true",
                   help="cold-open teaser: repeat the payoff up front, then the full clip (opt-in; skips the front-load slide)")
    p.add_argument("--caption-style", choices=_captions.STYLE_NAMES, default=None,
                   help="styled captions (karaoke/pop/minimal); omit for today's plain word captions")
    p = sub.add_parser("hype"); p.add_argument("candidates")
    p = sub.add_parser("export"); p.add_argument("clip")
    p = sub.add_parser("polish"); p.add_argument("clip")
    p = sub.add_parser("safety"); p.add_argument("job"); p.add_argument("--pick", type=int, default=1)
    p = sub.add_parser("copyright"); p.add_argument("job"); p.add_argument("--pick", type=int, default=1)
    p = sub.add_parser("scenes"); p.add_argument("job"); p.add_argument("--pick", type=int, default=1)
    a = ap.parse_args()

    _startup_disk_cleanup()  # ITEM 9 — stale *.part cleanup on startup
    if a.cmd == "scout":
        run_moments(a.src, a.top, a.min, a.max, a.max_minutes)
    elif a.cmd == "cut":
        run_cut(a.job, a.pick, a.hook, a.layout, a.title, a.caption_style,
                teaser=a.teaser)
    elif a.cmd == "manual":
        # Scout first (builds transcript + job dir), then full-fill cut
        # of the exact window (Mark Start / Mark End).
        run_moments(a.src, top=1, min_s=7, max_s=30)
        job = sorted((HERE / "jobs").glob("*"))[-1]
        cut_filltrack(job, 1, a.hook, start=a.start, end=a.end,
                      caption_style=a.caption_style, teaser=a.teaser)
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
    elif a.cmd == "copyright":
        from clipper.core import copyright as _copyright
        job = Path(a.job)
        c = json.loads((job / "candidates.json").read_text(encoding="utf-8"))
        p = next(x for x in c["candidates"] if x["n"] == a.pick)
        words = json.loads((job / "transcript.json").read_text(encoding="utf-8"))
        streamer = ((c.get("info") or {}).get("channel")
                    or (c.get("info") or {}).get("uploader"))
        print(json.dumps(_copyright.check_clip(
            c["source"], p["start"], p["end"], words, streamer=streamer), indent=1))
    elif a.cmd == "scenes":
        from clipper.core import scenes as _scenes
        job = Path(a.job)
        c = json.loads((job / "candidates.json").read_text(encoding="utf-8"))
        p = next(x for x in c["candidates"] if x["n"] == a.pick)
        print(_scenes.scene_cuts(c["source"], p["start"], p["end"]))


if __name__ == "__main__":
    main()
