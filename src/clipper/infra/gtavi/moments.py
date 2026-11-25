"""Find the best clip moments in a video or VOD.

Signals per second: loudness spikes, YouTube "most replayed" heatmap, live-chat spikes
(YouTube VOD chat replay or a TwitchDownloader chat.json) and hype words in the transcript.
Each pick also gets a 0-100 virality score (OpusClip parity) combining those signals with a
visual signal — scene-change density (PySceneDetect when installed, else a cheap frame-diff)
plus facecam presence via the existing facecam code — and a short human-readable
virality_why string. The visual component degrades gracefully to 0 when its dependencies
or the source video are unavailable; scoring never crashes over it.
Reaction signals ported from OpenClip (clipper.core.reactions) add capped
funny / highlight / clippable terms, loudness lift vs a rolling median
baseline, and sustained on-screen action as ADDITIONAL terms alongside the
chat signal — the chat/loud/hype/heat weights are unchanged.
Writes jobs/<id>/candidates.md + candidates.json.

usage: python moments.py <url-or-file> [--top 8] [--min 15] [--max 40] [--chat chat.json] [--max-minutes 90]
"""
import argparse, json, math, re, sys, wave
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from common import ffprobe, is_url, job_dir, load_json, run, save_json, tool, ytdlp_base, ytdlp_json

HYPE = re.compile(r"\b(no way|oh my god|omg|what|wait|bro|let'?s go|holy|insane|crazy|yo+|wtf|dude|look at|nah|run|go go)\b", re.I)
CHAT_HYPE = re.compile(r"(LMAO|LOL|KEKW|OMEGALUL|W+\b|CLIP|💀|😂|🔥|NO WAY|HOLY)", re.I)

# OpenClip reaction signals (clipper.core.reactions) — additive terms only.
# The import is guarded: if the module is unavailable, every weight below
# stays 0.0 and scoring is byte-for-byte the pre-reaction behaviour.
_src = Path(__file__).resolve().parents[3]  # <engine>/src
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
REACTIONS = False
W_FUNNY = W_HIGHLIGHT = W_CLIPPABLE = W_LOUD_LIFT = W_LOUD_PEAK = W_ACTION = 0.0
try:
    from clipper.core.reactions import (
        W_ACTION as _W_ACTION, W_CLIPPABLE as _W_CLIPPABLE, W_FUNNY as _W_FUNNY,
        W_HIGHLIGHT as _W_HIGHLIGHT, W_LOUD_LIFT as _W_LOUD_LIFT,
        W_LOUD_PEAK as _W_LOUD_PEAK, action_series, loudness_lift_peak,
        reaction_series,
    )
    W_FUNNY, W_HIGHLIGHT, W_CLIPPABLE = _W_FUNNY, _W_HIGHLIGHT, _W_CLIPPABLE
    W_LOUD_LIFT, W_LOUD_PEAK, W_ACTION = _W_LOUD_LIFT, _W_LOUD_PEAK, _W_ACTION
    REACTIONS = True
except Exception:
    pass


def fetch(src, d, max_min):
    info = {}
    if is_url(src):
        info = ytdlp_json(src)
        save_json(d / "info.json", {k: info.get(k) for k in ("id", "title", "uploader", "channel", "uploader_id", "webpage_url",
                                                             "duration", "view_count", "upload_date", "heatmap", "is_live", "was_live")})
        vid = d / "source.mp4"
        if not vid.exists():
            # Item #7: disk-guard pre-flight (clipper.core.disk_guard lands
            # in parallel; until then the import fails and we proceed
            # unguarded). Estimated size from the probe when known, else
            # the conservative 500 MB default.
            try:
                from clipper.core.disk_guard import preflight
                preflight(d, info.get("filesize") or info.get("filesize_approx")
                          or 500 * 1024 * 1024)
            except ImportError:
                pass
            # Item #7: hardened retry flags ride in the patched
            # ytdlp_base(); add only the per-download archive (reruns skip
            # completed IDs). Standalone runs without clipper on sys.path
            # fall back to the same literal flag.
            try:
                from clipper.infra.ssl_fix import hardened_download_args
                archive_args = hardened_download_args(d / "archive.txt")
            except ImportError:
                archive_args = ["--download-archive", str(d / "archive.txt")]
            # Item #8 (2026-11-25): prefer true 1080p60, then any 1080p,
            # then 720p. The mp4/m4a constraint is kept (same as before);
            # only the 60 fps preference is added in front. 1080p cap
            # kept: the 1440p upload trick is out of scope.
            args = ytdlp_base() + archive_args + ["-f", "bv*[height=1080][fps=60][ext=mp4]+ba[ext=m4a]/bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b", "--merge-output-format", "mp4",
                                   "-o", str(vid)]
            if (info.get("duration") or 0) > max_min * 60:
                args += ["--download-sections", f"*0-{max_min * 60}"]
            run(args + [src])
        if info.get("was_live") and not (d / "chat.json").exists():  # YouTube chat replay
            run(ytdlp_base() + ["--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", str(d / "chat"), src], check=False)
    else:
        vid = Path(src).resolve()
        save_json(d / "info.json", {"title": vid.stem, "webpage_url": str(vid)})
    return vid, info


def audio_rms(vid, d):
    wav = d / "audio.wav"
    if not wav.exists():
        run([tool("ffmpeg"), "-y", "-v", "error", "-i", str(vid), "-ac", "1", "-ar", "16000", str(wav)])
    with wave.open(str(wav)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    n = len(x) // 16000
    rms = np.sqrt((x[: n * 16000].reshape(n, 16000) ** 2).mean(1) + 1e-9)
    return wav, 20 * np.log10(rms)


def transcribe(wav, d):
    out = d / "transcript.json"
    if out.exists():
        return load_json(out)
    from faster_whisper import WhisperModel
    try:
        import ctranslate2
        cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda = False
    with wave.open(str(wav)) as w:  # pass samples directly (16 kHz mono), no extra decoder needed
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768

    def run_on(device):
        model = WhisperModel("small", device=device, compute_type="float16" if device == "cuda" else "int8")
        segs, _ = model.transcribe(pcm, word_timestamps=True, vad_filter=True)
        return [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)} for s in segs for w in (s.words or [])]
    try:
        words = run_on("cuda" if cuda else "cpu")
    except RuntimeError as e:  # GPU present but CUDA libraries missing: fall back to CPU
        print(f"GPU transcription unavailable ({str(e)[:80]}), using CPU")
        words = run_on("cpu")
    save_json(out, words)
    return words


def chat_series(d, n, chat_arg, info):
    """Messages per second from YouTube live_chat json lines or a TwitchDownloader chat.json."""
    c = np.zeros(n)
    yt = list(d.glob("chat*.live_chat.json"))
    if yt:
        for line in yt[0].read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                a = json.loads(line)["replayChatItemAction"]
                t = int(a["videoOffsetTimeMsec"]) / 1000
                txt = json.dumps(a["actions"], ensure_ascii=False)
            except Exception:
                continue
            if 0 <= t < n:
                c[int(t)] += 2 if CHAT_HYPE.search(txt) else 1
    elif chat_arg:
        js = load_json(chat_arg)
        for m in js.get("comments", []):
            t = m.get("content_offset_seconds", 0)
            body = (m.get("message") or {}).get("body", "")
            if 0 <= t < n:
                c[int(t)] += 2 if CHAT_HYPE.search(body) else 1
    return c


def z(a):
    a = np.asarray(a, dtype=float)
    if a.std() < 1e-9:
        return np.zeros_like(a)
    return (a - np.median(a)) / (a.std() + 1e-9)


def smooth(a, k):
    k = max(1, int(k))
    return np.convolve(a, np.ones(k) / k, mode="same")


# --- Virality: visual signal + 0-100 score (OpusClip parity) -----------------
# Visual component per second: scene-change density (PySceneDetect when
# installed, else a cheap frame-diff) + facecam presence (existing
# facecam.find_cam). Every visual helper is best-effort: a missing
# dependency, a missing model, or an unreadable video yields visual = 0
# and scoring continues from the other signals — never crash scoring.

VISUAL_WEIGHT = 1.0  # same scale as the raw-score weights (loud 1.0, hype 0.8, heat 2.0, chat 1.5)

# ITEM 4 — payoff front-load at scan time: a candidate window opens
# PEAK_LEAD seconds before its peak, so the peak already sits in the
# first 2 s of the window (was 0.55 * max — peak mid-clip, which the
# feed data punishes: slow opens held 24.4% vs 77.5% for payoff in
# the first 2 s). The cut-time slide in core/clip.py then only has to
# nudge it to ~0.5 s — a small slide that rarely crosses a scene cut.
PEAK_LEAD = 2.0


def _p99norm(a):
    """0..1 per-second component: value / 99th percentile of the video, clipped.
    The video's top ~1% of seconds on each signal score ~1.0. Robust to a
    single-second outlier (e.g. one hype-word burst) dragging the scale."""
    a = np.asarray(a, dtype=float)
    scale = float(np.percentile(a, 99)) if len(a) else 0.0
    if scale < 1e-9:
        return np.zeros_like(a)
    return np.clip(a / scale, 0, 1)


def _density_from_cuts(cuts, n, window=5):
    """Scene-cut times (absolute seconds) -> per-second cut density."""
    b = np.zeros(n)
    for t in cuts:
        i = int(t)
        if 0 <= i < n:
            b[i] += 1
    return smooth(b, window)


def _scenedetect_cuts(vid, dur):
    """Scene-cut times via PySceneDetect on a 640-wide / 15 fps proxy (the
    scenes.py pattern). Raises ImportError when scenedetect is absent."""
    import subprocess
    import tempfile
    from scenedetect import ContentDetector, detect
    with tempfile.TemporaryDirectory() as td:
        proxy = str(Path(td) / "proxy.mp4")
        subprocess.run([tool("ffmpeg"), "-y", "-v", "error", "-i", str(vid),
                        "-vf", "scale=640:-2,fps=15", "-an", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "28", proxy], check=True)
        scenes = detect(proxy, ContentDetector(threshold=27.0), show_progress=False)
        return [round(s[0].get_seconds(), 2) for s in scenes]


def _cv2_framediff_cuts(vid, n, dur, sample_fps=2.0):
    """Cheap OpenCV frame-diff (the reframe.py pattern: 64x36 gray frames,
    mean absdiff; > 28 counts as a hard cut). Seeks at sample_fps so a long
    video stays cheap. Raises ImportError when cv2 is absent."""
    import cv2
    cap = cv2.VideoCapture(str(vid))
    if not cap.isOpened():
        raise RuntimeError("cv2 could not open video")
    cuts, prev, t = [], None, 0.0
    while t < dur:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, f = cap.read()
        if not ok or f is None:
            break
        small = cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (64, 36))
        if prev is not None and cv2.absdiff(small, prev).mean() > 28:
            cuts.append(round(t, 2))
        prev = small
        t += 1.0 / sample_fps
    cap.release()
    return cuts


def _ffmpeg_framediff_cuts(vid, n, dur):
    """Dependency-free frame-diff: ffmpeg decodes 1 fps 160x90 gray frames to a
    pipe; numpy scores the mean absdiff between consecutive frames. The cut
    threshold is adaptive (median + 3 robust sigmas, floor 20). Needs only
    ffmpeg + numpy, both already required. Returns [] on failure."""
    import subprocess
    r = subprocess.run([tool("ffmpeg"), "-v", "error", "-i", str(vid),
                        "-vf", "scale=160:90,fps=1", "-f", "rawvideo",
                        "-pix_fmt", "gray", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0 or not r.stdout:
        return []
    w, h = 160, 90
    nframes = len(r.stdout) // (w * h)
    if nframes < 2:
        return []
    frames = np.frombuffer(r.stdout[:nframes * w * h], dtype=np.uint8).reshape(nframes, h, w).astype(np.float32)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))  # diff[i] = change into second i+1
    med = float(np.median(diff))
    mad = float(np.median(np.abs(diff - med)))
    thr = max(20.0, med + 3 * 1.4826 * mad)
    return [round(float(i + 1), 2) for i in range(len(diff)) if diff[i] > thr]


def _face_presence(vid):
    """Facecam presence for the whole video via the existing facecam code.
    Returns (mode, confidence 0..1). Only attempted when cv2 imports AND the
    YuNet model file is already cached — scoring must never trigger a model
    download. Any failure -> ("none", 0.0)."""
    try:
        import cv2  # noqa: F401  (facecam needs it; absence means no face signal)
    except Exception:
        return "none", 0.0
    try:
        # Same path facecam.model_path() reads; if it is not cached, skip
        # rather than downloading a model during scoring.
        model = Path.home() / ".cache" / "gtavi-claudecode" / "models" / "face_detection_yunet_2023mar.onnx"
        if not model.exists() or model.stat().st_size < 100_000:
            return "none", 0.0
        from facecam import find_cam
        r = find_cam(str(vid))
        if r.get("mode") in ("cam", "face"):
            return r["mode"], float(r.get("confidence") or 0.0)
    except Exception:
        pass
    return "none", 0.0


def visual_series(vid, n, dur):
    """Per-second visual component (0..1) + scene component + metadata.

    Scene-change density from PySceneDetect (videos up to 30 min, when
    installed), else the cv2 frame-diff, else the ffmpeg+numpy frame-diff.
    Facecam presence (whole-video, via facecam.find_cam) adds a flat bonus.
    Returns (visual_01, scene_01, meta); visual_01 is all zeros and
    meta["method"] is None when the video is unavailable or every method
    failed — scoring then uses only the other signals.
    """
    meta = {"method": None, "cuts": 0, "face_mode": "none", "face_confidence": 0.0}
    zeros = np.zeros(n)
    if not vid or not Path(vid).exists():
        return zeros, zeros, meta
    cuts, method = None, None
    try:
        if dur <= 1800:  # full-video proxy detect gets slow beyond ~30 min; frame-diff instead
            cuts = _scenedetect_cuts(vid, dur)
            method = "scenedetect"
    except Exception:
        cuts = None
    if cuts is None:
        try:
            cuts = _cv2_framediff_cuts(vid, n, dur)
            method = "cv2-framediff"
        except Exception:
            cuts = None
    if cuts is None:
        try:
            cuts = _ffmpeg_framediff_cuts(vid, n, dur)
            method = "ffmpeg-framediff"
        except Exception:
            cuts = None
    if cuts is None:
        return zeros, zeros, meta
    meta["method"] = method
    meta["cuts"] = len(cuts)
    scene_n = _p99norm(_density_from_cuts(cuts, n)) if cuts else zeros
    face_mode, face_conf = _face_presence(vid)
    meta["face_mode"] = face_mode
    meta["face_confidence"] = round(face_conf, 2)
    visual = np.clip(0.7 * scene_n + 0.3 * face_conf, 0, 1)
    return visual, scene_n, meta


def _hype_words(text, limit=2):
    """Distinct hype words actually present in a pick's text, in order."""
    seen = []
    for m in HYPE.finditer(text):
        w = m.group(0).strip().lower()
        if w and w not in seen:
            seen.append(w)
    return seen[:limit]


def virality_why(p, loud_v, hype_words, heat_v, chat_v, w_chat, scene_v, face_conf,
                 funny_v=0.0, highlight_v=0.0, action_v=0.0):
    """Short human-readable reason for a virality score, e.g.
    "loud peak + hype words 'insane' + face on camera"."""
    parts = []
    if loud_v > 1:
        parts.append("loud peak")
    if hype_words:
        parts.append("hype words " + ", ".join(f"'{w}'" for w in hype_words))
    if funny_v >= 22:
        parts.append("laugh moment")
    if highlight_v >= 14:
        parts.append("strong reaction")
    if action_v > 0.5:
        parts.append("sustained action")
    if heat_v > 0.5:
        parts.append("most replayed")
    if w_chat and chat_v > 1:
        parts.append("chat spike")
    if scene_v > 0.34:
        parts.append("fast cuts")
    elif scene_v > 0.15:
        parts.append("scene change")
    if face_conf > 0:
        parts.append("face on camera")
    return " + ".join(parts) if parts else "overall energy"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--min", type=float, default=15); ap.add_argument("--max", type=float, default=40)
    ap.add_argument("--chat"); ap.add_argument("--max-minutes", type=int, default=90)
    a = ap.parse_args()
    d = job_dir(a.src)
    vid, info = fetch(a.src, d, a.max_minutes)
    dur = float(ffprobe(vid)["format"]["duration"])
    wav, db = audio_rms(vid, d)
    n = len(db)
    words = transcribe(wav, d)
    # signals
    loud = smooth(np.clip(z(db - smooth(db, min(30, n))), 0, None), 3)          # jumps above the local level
    hype = np.zeros(n)
    for w in words:
        if HYPE.search(w["w"]) and int(w["s"]) < n:
            hype[int(w["s"])] += 1
    hype = smooth(hype, 5)
    heat = np.zeros(n)
    for h in (info.get("heatmap") or []):
        heat[int(h["start_time"]):int(math.ceil(h["end_time"]))] = h["value"]
    chat = chat_series(d, n, a.chat, info)
    chatz = smooth(np.clip(z(smooth(chat, 10)), 0, None), 3)
    w_heat = 2.0 if heat.any() else 0
    w_chat = 1.5 if chat.any() else 0
    # OpenClip reaction terms (additive; loud/hype/heat/chat weights above
    # are untouched). All components are 0..1 p99-normalised, each weight
    # <= 0.5, so no single cue can dominate. Degrades to zeros when the
    # reactions module is unavailable or any step fails.
    funny = highlight = clippable = np.zeros(n)
    funny_n = highlight_n = clippable_n = np.zeros(n)
    lift_n = peak_n = action_n = np.zeros(n)
    action_meta = {"method": None, "max": 0.0}
    if REACTIONS:
        try:
            rs = reaction_series(words, n, db)
            funny, highlight, clippable = rs["funny"], rs["highlight"], rs["clippable"]
            funny_n, highlight_n, clippable_n = rs["funny_n"], rs["highlight_n"], rs["clippable_n"]
            lift, peak = loudness_lift_peak(db)
            lift_n, peak_n = _p99norm(lift), _p99norm(peak)
        except Exception:
            pass
        try:
            act = action_series(vid, n) if dur <= 1800 else None
            if act is not None:
                action_n = _p99norm(act)
                action_meta = {"method": "ffmpeg-framediff-32x18", "max": round(float(act.max()), 2)}
        except Exception:
            pass
    score = 1.0 * loud + 0.8 * z(hype).clip(0) + w_heat * z(heat).clip(0) + w_chat * chatz \
        + W_FUNNY * funny_n + W_HIGHLIGHT * highlight_n + W_CLIPPABLE * clippable_n \
        + W_LOUD_LIFT * lift_n + W_LOUD_PEAK * peak_n + W_ACTION * action_n
    # Visual signal (scene-change density + facecam); degrades to 0, never crashes scoring.
    # The raw `score` above is unchanged, so the 0-5 star Hype Meter in core/clip.py
    # (stars() over score) behaves exactly as before.
    try:
        visual, scene_n, visual_meta = visual_series(vid, n, dur)
    except Exception:
        visual, scene_n, visual_meta = np.zeros(n), np.zeros(n), {"method": None, "cuts": 0, "face_mode": "none", "face_confidence": 0.0}
    visual_available = bool(visual_meta.get("method")) and bool(visual.any())
    w_visual = VISUAL_WEIGHT if visual_available else 0.0
    # 0-100 virality: the same relative weights as the raw score, each signal
    # normalised to 0..1 by its own 99th percentile, renormalised over the
    # signals actually present (a missing signal never caps the score — its
    # weight is redistributed). The weighted mean is square-root stretched so
    # the scale reads like OpusClip's: one maxed signal ~55-60, two strong
    # signals ~80, every signal maxed = 100. Order is preserved.
    loud_n = _p99norm(loud)
    hype_n = _p99norm(z(hype).clip(0))
    heat_n = _p99norm(z(heat).clip(0)) if w_heat else np.zeros(n)
    chat_n = _p99norm(chatz) if w_chat else np.zeros(n)
    vis_n = np.clip(visual, 0, 1) if w_visual else np.zeros(n)
    w_total = 1.0 + 0.8 + w_heat + w_chat + w_visual \
        + W_FUNNY + W_HIGHLIGHT + W_CLIPPABLE + W_LOUD_LIFT + W_LOUD_PEAK + W_ACTION
    virality = np.sqrt((1.0 * loud_n + 0.8 * hype_n + w_heat * heat_n + w_chat * chat_n + w_visual * vis_n
                        + W_FUNNY * funny_n + W_HIGHLIGHT * highlight_n + W_CLIPPABLE * clippable_n
                        + W_LOUD_LIFT * lift_n + W_LOUD_PEAK * peak_n + W_ACTION * action_n) / w_total) * 100
    # pick non-overlapping windows around peaks
    starts = [x["s"] for x in words]
    picks, used = [], np.zeros(n, bool)
    order = np.argsort(-score)
    for p in order:
        if len(picks) >= a.top or score[p] <= 0:
            break
        s0 = max(0, p - PEAK_LEAD); e0 = min(dur, s0 + a.max)  # ITEM 4: peak ~2 s into the window, not mid-clip
        # snap start to the first word after s0 and end to a word end, keep length in [min, max]
        ws = [x for x in words if x["s"] >= s0 and x["e"] <= e0]
        if ws:
            s0 = max(0, ws[0]["s"] - 0.3); e0 = min(dur, ws[-1]["e"] + 0.6)
        if e0 - s0 < a.min:
            e0 = min(dur, s0 + a.min)
        e0 = min(e0, s0 + a.max)
        if used[int(s0):int(e0)].any():
            continue
        used[int(s0):int(e0)] = True
        text = " ".join(x["w"] for x in words if s0 <= x["s"] < e0)
        why = []
        if loud[p] > 1: why.append("loudness spike")
        if hype[p] > 0: why.append("hype words")
        if funny[p] >= 22: why.append("laugh moment")
        if highlight[p] >= 14: why.append("strong reaction")
        if clippable[p] >= 30: why.append("clip cue")
        if action_n[p] > 0.5: why.append("sustained action")
        if heat[p] > 0.5: why.append(f"most replayed {heat[p]:.2f}")
        if w_chat and chatz[p] > 1: why.append("chat spike")
        v = int(round(float(np.clip(virality[p], 0, 100))))
        vwhy = virality_why(p, loud[p], _hype_words(text), heat[p], chatz[p], w_chat,
                            scene_n[p], visual_meta.get("face_confidence") or 0.0,
                            funny[p], highlight[p], action_n[p])
        picks.append({"n": len(picks) + 1, "start": round(s0, 2), "end": round(e0, 2), "peak": int(p),
                      "score": round(float(score[p]), 2), "why": why or ["overall energy"], "text": text[:400],
                      "virality": v, "virality_why": vwhy})
    picks.sort(key=lambda x: x["start"])
    for i, x in enumerate(picks, 1):
        x["n"] = i
    meta = load_json(d / "info.json")
    save_json(d / "candidates.json", {"source": str(vid), "info": meta, "duration": dur, "candidates": picks,
                                      "signals": {"heatmap": bool(w_heat), "chat": bool(w_chat), "words": len(words),
                                                  "visual": visual_meta.get("method") or False,
                                                  "visual_cuts": visual_meta.get("cuts", 0),
                                                  "reactions": bool(REACTIONS),
                                                  "action": action_meta.get("method") or False,
                                                  "action_max": action_meta.get("max", 0.0),
                                                  "face": visual_meta.get("face_mode", "none")}})
    fmt = lambda t: f"{int(t // 60)}:{int(t % 60):02d}"
    if visual_meta.get("method"):
        visual_line = (f"visual: {visual_meta['method']} ({visual_meta['cuts']} scene cuts"
                       + (f", facecam {visual_meta['face_mode']} {visual_meta['face_confidence']}" if visual_meta.get("face_confidence") else ", no facecam") + ")")
    else:
        visual_line = "visual: unavailable (scored from audio/transcript only)"
    md = [f"# Candidates: {meta.get('title', '')}", f"source: {meta.get('webpage_url', '')}",
          f"signals: loudness, transcript ({len(words)} words)" + (", most replayed" if w_heat else "") + (", chat" if w_chat else "") + (", reactions" if REACTIONS else ""),
          visual_line, ""]
    if action_meta.get("method"):
        md.insert(4, f"action: {action_meta['method']} (max {action_meta['max']})")
    for x in picks:
        md += [f"## {x['n']}. {fmt(x['start'])}-{fmt(x['end'])} ({x['end'] - x['start']:.0f}s) score {x['score']}",
               f"why: {', '.join(x['why'])}", f"virality: {x['virality']}/100 — {x['virality_why']}", f"> {x['text']}", ""]
    (d / "candidates.md").write_text("\n".join(md), encoding="utf-8")
    print(f"job: {d}\n{len(picks)} candidates -> {d / 'candidates.md'}")


if __name__ == "__main__":
    main()
