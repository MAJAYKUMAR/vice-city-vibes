#!/usr/bin/env python3
"""Offline VOD chat-spike scan for the Vice City Vibes clipper engine.

    python scripts/scan_vod.py <vod-url> [more urls...] [--ratio 1.5]
        [--max-fetch 8] [--replay-dir jobs/<vid>/live-heat/replay]

Why this exists
---------------
live_heat.py --full-stream downloads the whole chat replay and then
scans it. That run was SIGTERMed mid-download (2026-11-22), leaving
partial .part chat files (~70-75 min of each Speed VOD, ~150K
messages, 0 bad lines). This script scans whatever chat is already
on disk — no re-download — with the exact same spike rule shape as
live_heat (10 s windows, 300 s weighted baseline, tunable ratio,
min 8 msgs, 45 s cooldown), then fetches the top candidate windows
from the VOD with yt-dlp --download-sections.

It reuses live.py's ChatTail for parsing (same HYPE weighting,
NOISE filter, top-words) so results are comparable to live_heat.

Outputs per VOD go to jobs/<video-id>/live-heat/: rows appended to
spikes.md (same table format as live_heat, plus a reaction-bonus
column), lb_<n>.mp4 window files, and scan_vod.json with the full
candidate list plus a "ranked_fetched" re-rank of the fetched set.

Reaction bonus (OpenClip port, clipper.core.reactions)
------------------------------------------------------
After a window is fetched, its lb_<n>.mp4 is transcribed with
faster-whisper (tiny, CPU, word timestamps — the same recipe as
scripts/cut_vod_pick.py) and its per-second loudness measured from
the same decoded PCM; window_reaction_bonus() then scores the
transcript cues in that window. The bonus is RE-RANK ONLY: the
chat-spike ratio stays the sole selector of candidates and of
fetch_idx, the bonus never filters and never changes what is
fetched. Any failure (no faster-whisper, no transcript, no audio,
reactions import error) degrades to bonus 0.0 and logs it — the
scan never crashes over it. Measured cost (2026-11-25, this VM,
1.5 CPU): ~9 s wall for a 22 s window cold (3 s model load +
5 s transcribe + 0.5 s decode), ~6 s warm with the model cached
(~0.25 s per second of window); the model is cached across
windows in one run, so the first window pays the load once.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "src"))
sys.path.insert(0, str(ENGINE / "src" / "clipper" / "infra" / "gtavi"))

from clipper.infra import ssl_fix  # noqa: E402
from clipper.paths import jobs_dir  # noqa: E402

# OpenClip reaction bonus — guarded like moments.py: if the module is
# unavailable, every bonus is 0.0 and scoring/selection is unchanged.
try:
    from clipper.core.reactions import window_reaction_bonus
    REACTIONS = True
except Exception:  # noqa: BLE001
    window_reaction_bonus = None
    REACTIONS = False

common = ssl_fix.patch_yt_dlp()  # --no-check-certificates for yt-dlp (egress proxy)
import live as live_mod  # noqa: E402  (ChatTail + spike constants; no side effects on import)

ssl_fix.patch_module(live_mod)

from common import ytdlp_base, ytdlp_json  # noqa: E402  (patched via common)

WINDOW = 10          # seconds per heat window
BASELINE = 300       # seconds of weighted chat history for the baseline
RATIO = 1.5          # spike = rate >= RATIO * baseline rate (candidate filter)
MIN_MSGS = 8         # raw messages per window to count as a spike
COOLDOWN = 45        # seconds between spikes on one stream
BEFORE = 15          # seconds before the spike to include in the fetched window
AFTER = 20           # seconds after the spike to include in the fetched window
MIN_CLIP_BYTES = 100 * 1024
# Reaction bonus (see module docstring): faster-whisper tiny on CPU is
# the default — small was not measured because tiny already costs
# ~0.25 s per second of window warm; pass --reaction-model small only
# if a run can afford roughly 2-3x that. --no-reaction skips it.
REACTION_MODEL_DEFAULT = "tiny"
_WHISPER_MODEL = {}  # model name -> loaded WhisperModel (cached per run)
# Item #8 (2026-11-25): prefer true 1080p60, then any 1080p, then 720p.
# These lb_* windows ARE the cut source (cut_vod_pick.py cuts from them),
# not 480p triage — triage stays 480p in the per-job pipeline scripts.
# 1080p cap kept: the 1440p upload trick is out of scope.
FORMAT = "bv*[height=1080][fps=60]+ba/bv*[height=1080]+ba/bv*[height<=720]+ba/b"
MAX_FETCH = 8


def fmt_offset(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def verify_clip(path):
    """ffprobe guard: needs a video stream AND an audio stream, >100 KB,
    duration > 0. Never raises."""
    try:
        if not path.exists() or path.stat().st_size <= MIN_CLIP_BYTES:
            return False, f"file missing or <= {MIN_CLIP_BYTES // 1024}KB"
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return False, "ffprobe not found"
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,width,height",
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, f"ffprobe failed: {(r.stderr or '').strip()[-200:]}"
        info = json.loads(r.stdout)
        types = {s.get("codec_type") for s in info.get("streams", [])}
        if "video" not in types or "audio" not in types:
            return False, f"missing stream(s): {sorted(types)}"
        dur = float(info.get("format", {}).get("duration") or 0)
        if dur <= 0:
            return False, "zero duration"
        v = next(s for s in info["streams"] if s.get("codec_type") == "video")
        return True, f"{v.get('width')}x{v.get('height')} {dur:.1f}s {path.stat().st_size / 1e6:.1f}MB"
    except Exception as exc:  # noqa: BLE001
        return False, f"verify error: {exc}"


def transcribe_window(path, model_name=REACTION_MODEL_DEFAULT):
    """Word-level transcript + per-second loudness (dB) of a fetched
    window file. Timings are relative to the window file itself.

    Same recipe as scripts/cut_vod_pick.py: ffmpeg decodes to 16 kHz
    mono PCM (faster-whisper 1.2.1's av-based decode is broken against
    the venv's old PyAV, so the array path is used), then tiny/small
    on CPU int8 with word timestamps + VAD. The dB series is the
    moments.audio_rms() math over the same PCM — no second decode.
    The model is cached across windows in one run (first window pays
    the ~3 s load once). Raises on failure — the caller degrades."""
    import numpy as np

    wav = Path(tempfile.mktemp(suffix=".wav"))
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                        str(wav)], check=True, timeout=120)
        with wave.open(str(wav)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()),
                                dtype=np.int16).astype(np.float32) / 32768
    finally:
        wav.unlink(missing_ok=True)
    n = len(pcm) // 16000
    if n == 0:
        raise ValueError("no audio samples decoded")
    rms = np.sqrt((pcm[: n * 16000].reshape(n, 16000) ** 2).mean(1) + 1e-9)
    db = 20 * np.log10(rms)
    from faster_whisper import WhisperModel
    if model_name not in _WHISPER_MODEL:
        _WHISPER_MODEL[model_name] = WhisperModel(
            model_name, device="cpu", compute_type="int8")
    model = _WHISPER_MODEL[model_name]
    segs, _ = model.transcribe(pcm, language="en",
                               word_timestamps=True, vad_filter=True)
    words = [{"w": w.word.strip(), "s": round(w.start, 2),
              "e": round(w.end, 2)}
             for s in segs for w in (s.words or []) if w.word.strip()]
    return words, db


def reaction_bonus_for_window(path, model_name=REACTION_MODEL_DEFAULT):
    """Re-rank-only reaction bonus for one fetched window file.

    Transcribes the window, measures its loudness, and scores
    reactions.window_reaction_bonus() over the whole window
    (t0=0, t1=window seconds — transcript timings are relative to
    the file). NEVER raises and NEVER filters: any failure (no
    faster-whisper installed, reactions module missing, no
    transcript, no audio) returns bonus 0.0 with the reason in
    "error" and the scan continues. Returns {"funny",
    "highlight", "clippable", "bonus", "words", "seconds",
    "reaction_seconds", "error"}."""
    t_start = time.time()
    zero = {"funny": 0.0, "highlight": 0.0, "clippable": 0.0, "bonus": 0.0,
            "words": 0, "seconds": 0.0, "reaction_seconds": 0.0,
            "error": None}
    if not REACTIONS:
        zero["error"] = "reactions module unavailable"
        return zero
    try:
        words, db = transcribe_window(path, model_name)
        res = window_reaction_bonus(words, 0.0, float(len(db)), db=db)
    except Exception as exc:  # noqa: BLE001
        zero["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        zero["reaction_seconds"] = round(time.time() - t_start, 1)
        print(f"  reaction bonus for {Path(path).name}: FAILED "
              f"({zero['error']}) — bonus 0.0", flush=True)
        return zero
    out = {"funny": res["funny"], "highlight": res["highlight"],
           "clippable": res["clippable"], "bonus": res["bonus"],
           "words": len(words), "seconds": float(len(db)),
           "reaction_seconds": round(time.time() - t_start, 1),
           "error": None}
    print(f"  reaction bonus for {Path(path).name}: bonus={out['bonus']:.3f} "
          f"(funny={out['funny']:.1f} highlight={out['highlight']:.1f} "
          f"clippable={out['clippable']:.1f}, {out['words']} words, "
          f"{out['reaction_seconds']}s)", flush=True)
    return out


def scan_replay(tail, t_from, t_to, ratio, t_base):
    """Same rule shape as live_heat._scan_replay: 10 s windows stepping
    2 s, 300 s weighted baseline, tunable ratio, min 8 msgs, 45 s
    cooldown. Returns [(wall, raw, words)] in time order."""
    out = []
    last_spike = -1e18
    e = int(t_from) + WINDOW + 30
    t_to = int(t_to)
    while e <= t_to:
        raw = tail.total(tail.raw, e - WINDOW + 1, e)
        w = tail.total(tail.wgt, e - WINDOW + 1, e)
        nb = min(BASELINE, max(1, e - int(t_base) - WINDOW))
        bw = tail.total(tail.wgt, e - WINDOW - nb + 1, e - WINDOW) / nb
        if (raw >= MIN_MSGS and w / WINDOW >= ratio * max(bw, 0.02)
                and e - last_spike >= COOLDOWN):
            last_spike = e
            out.append((float(e), raw, tail.top_words(e - WINDOW + 1, e)))
        e += 2
    return out


def fetch_window(url, ld, num, spike_wall, words, rate, stream_start):
    """Download one spike window from the VOD and verify it. Retries
    once (the section fetch failed transiently once on 2026-11-22
    with an ffmpeg error, then worked on retry)."""
    out = ld / f"lb_{num}.mp4"
    s = int(spike_wall - BEFORE - stream_start)
    e = int(spike_wall + AFTER - stream_start)
    if s < 0:
        s = 0
    if e <= s:
        print(f"  lb #{num}: bad window {s}-{e}s, skipping")
        return None, f"bad window {s}-{e}s"
    # Item #7: disk-guard pre-flight (clipper.core.disk_guard lands in
    # parallel; until then the import fails and we proceed unguarded).
    # Window size is unknown up front, so the conservative 500 MB default.
    try:
        from clipper.core.disk_guard import preflight
        preflight(ld, 500 * 1024 * 1024)
    except ImportError:
        pass
    for attempt in (1, 2):
        print(f"  lb #{num}: fetching VOD *{s}-{e}s "
              f"(offset {fmt_offset(s)}-{fmt_offset(e)}) "
              f"attempt {attempt} ...", flush=True)
        # Item #7: hardened retry flags ride in the patched ytdlp_base();
        # add only the per-download archive (reruns skip completed IDs).
        cmd = (ytdlp_base() + ssl_fix.hardened_download_args(ld / "archive.txt")
               + ["--download-sections", f"*{s}-{e}",
                  "-f", FORMAT, "--merge-output-format", "mp4",
                  "-o", str(out), url])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            print(f"  lb #{num}: fetch TIMED OUT after 600s")
            continue
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            print(f"  lb #{num}: yt-dlp fetch FAILED rc={r.returncode}: "
                  f"{err[-1][:200] if err else 'no output'}")
            continue
        ok, detail = verify_clip(out)
        if ok:
            print(f"  lb #{num}: VERIFIED {out.name} ({detail})")
            return out, detail
        print(f"  lb #{num}: file FAILED verification: {detail}")
    return None, "fetch failed after 2 attempts"


def scan_vod(url, ratio, max_fetch, reaction=True,
             reaction_model=REACTION_MODEL_DEFAULT):
    try:
        info = ytdlp_json(url)
    except Exception as exc:  # noqa: BLE001
        print(f"could not read stream info for {url}: {exc}")
        return None
    vid = info.get("id") or "unknown"
    title = info.get("title", "")
    dur = info.get("duration")
    stream_start = info.get("release_timestamp")
    ld = jobs_dir() / vid / "live-heat"
    rdir = ld / "replay"
    print(f"[{vid}] {title[:80]} | duration {fmt_offset(dur) if dur else '?'}")
    if not stream_start:
        print(f"[{vid}] WARNING: no release_timestamp; cannot map offsets")
        return None
    if not rdir.exists() or not list(rdir.glob("chat.live_chat.json*")):
        print(f"[{vid}] no chat replay on disk at {rdir} — nothing to scan")
        return None
    # Parse the (possibly partial) chat replay with live.py's ChatTail.
    # start=0 accepts every replayed message (no history-backlog drop).
    tail = live_mod.ChatTail(rdir, 0)
    tail.poll()
    if not tail.raw:
        print(f"[{vid}] no chat messages parsed — skipping")
        return None
    first_ts = min(tail.raw)
    last_ts = max(tail.raw)
    total = sum(tail.raw.values())
    print(f"[{vid}] chat on disk covers "
          f"{datetime.fromtimestamp(first_ts, timezone.utc).strftime('%H:%M')}–"
          f"{datetime.fromtimestamp(last_ts, timezone.utc).strftime('%H:%M')} UTC "
          f"({len(tail.raw)} chat-seconds, {total} msgs) "
          f"= {fmt_offset(last_ts - first_ts)} of stream")
    cands = scan_replay(tail, first_ts, last_ts, ratio, first_ts)
    print(f"[{vid}] scanned {fmt_offset(last_ts - first_ts)} of chat at "
          f"ratio {ratio}: {len(cands)} candidate spike(s)")
    # Fetch the top candidates by rate (cap); record the rest with
    # offsets so they can be cut from the VOD later. The chat-spike
    # ratio is the SOLE selector here — the reaction bonus (below)
    # only re-ranks the fetched set's presentation order afterwards.
    spikes_md = ld / "spikes.md"
    _spikes_header = (
        f"| UTC time | offset | msgs/{WINDOW}s | top words | file | bonus |\n"
        "|---|---|---|---|---|---|\n")
    if not spikes_md.exists():
        spikes_md.write_text(_spikes_header, encoding="utf-8")
    elif "bonus" not in spikes_md.read_text(encoding="utf-8",
                                            errors="ignore").splitlines()[0]:
        # Older runs wrote a 5-column header; upgrade it in place so
        # new rows line up. Earlier rows keep 5 cells (markdown-tolerant).
        lines = spikes_md.read_text(encoding="utf-8").splitlines()
        lines[0] = _spikes_header.splitlines()[0]
        lines[1] = _spikes_header.splitlines()[1]
        spikes_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fetch_idx = {i for i, _ in sorted(enumerate(cands),
                                      key=lambda p: p[1][1], reverse=True)
                 [:max_fetch]}
    results = []
    with open(spikes_md, "a", encoding="utf-8") as fh:
        for i, (wall, raw, words) in enumerate(cands):
            num = i + 1
            iso = datetime.fromtimestamp(wall, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            off = fmt_offset(wall - stream_start)
            rec = {"n": num, "utc": iso, "offset": off,
                   "offset_s": int(wall - stream_start),
                   "msgs": raw, "words": words, "file": None,
                   "bonus": 0.0, "reaction": None}
            if i not in fetch_idx:
                fh.write(f"| {iso} | {off} | {raw} | {', '.join(words)} | "
                         f"CANDIDATE (scan_vod, over fetch cap — cut from VOD) | — |\n")
                rec["file"] = "CANDIDATE (over fetch cap)"
            else:
                out, detail = fetch_window(url, ld, num, wall, words, raw,
                                           stream_start)
                if out is not None:
                    # Reaction bonus hook: inputs exist only now (fetched
                    # window file -> transcript + loudness). Re-rank only.
                    rb = (reaction_bonus_for_window(out, reaction_model)
                          if reaction else
                          {"funny": 0.0, "highlight": 0.0, "clippable": 0.0,
                           "bonus": 0.0, "words": 0, "seconds": 0.0,
                           "reaction_seconds": 0.0, "error": "disabled"})
                    rec["reaction"] = rb
                    rec["bonus"] = rb["bonus"]
                    fh.write(f"| {iso} | {off} | {raw} | {', '.join(words)} | "
                             f"{out.name} | {rb['bonus']:.3f} |\n")
                    rec["file"] = out.name
                    rec["verify"] = detail
                else:
                    fh.write(f"| {iso} | {off} | {raw} | {', '.join(words)} | "
                             f"FETCH FAILED ({detail}) | — |\n")
                    rec["file"] = f"FETCH FAILED ({detail})"
            results.append(rec)
    fetched = sum(1 for r in results if r["file"] and r["file"].endswith(".mp4"))
    # Re-rank ONLY the fetched set's presentation order, by (chat rate,
    # reaction bonus). Candidate selection and fetch order above are
    # untouched; "candidates" stays in scan (time) order.
    ranked_fetched = [
        {"n": r["n"], "file": r["file"], "offset": r["offset"],
         "msgs": r["msgs"], "bonus": r["bonus"],
         "reaction_seconds": (r.get("reaction") or {}).get("reaction_seconds", 0.0)}
        for r in sorted(
            (r for r in results if r["file"] and r["file"].endswith(".mp4")),
            key=lambda r: (r["msgs"], r["bonus"]), reverse=True)]
    with open(spikes_md, "a", encoding="utf-8") as fh:
        fh.write("\n**Re-ranked fetched windows (chat rate, then reaction bonus):**\n\n")
        fh.write("| rank | file | msgs/10s | bonus |\n|---|---|---|---|\n")
        for k, r in enumerate(ranked_fetched, 1):
            fh.write(f"| {k} | {r['file']} | {r['msgs']} | {r['bonus']:.3f} |\n")
    (ld / "scan_vod.json").write_text(json.dumps({
        "url": url, "video_id": vid, "title": title, "duration": dur,
        "stream_start": stream_start, "ratio": ratio,
        "coverage": [first_ts, last_ts], "total_msgs": total,
        "candidates": results, "ranked_fetched": ranked_fetched,
    }, indent=1), encoding="utf-8")
    print(f"[{vid}] done: {len(cands)} candidate(s), {fetched} fetched. "
          f"Ranked fetched: "
          + ", ".join(f"#{r['n']} {r['file']} (msgs={r['msgs']}, "
                      f"bonus={r['bonus']:.3f})" for r in ranked_fetched)
          + f". See {spikes_md}")
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("urls", nargs="+", help="YouTube VOD URLs to scan")
    ap.add_argument("--ratio", type=float, default=RATIO,
                    help="spike bar: rate >= RATIO * baseline (default 1.5)")
    ap.add_argument("--max-fetch", type=int, default=MAX_FETCH,
                    help="max video windows to fetch per VOD (highest rate first)")
    ap.add_argument("--no-reaction", action="store_true",
                    help="skip the per-window reaction bonus (re-rank only; "
                    "selection is unchanged either way)")
    ap.add_argument("--reaction-model", default=REACTION_MODEL_DEFAULT,
                    choices=["tiny", "small"],
                    help="faster-whisper model for the reaction bonus "
                    "(default tiny; small is ~2-3x slower, unmeasured here)")
    a = ap.parse_args()
    for u in a.urls:
        scan_vod(u, a.ratio, a.max_fetch, reaction=not a.no_reaction,
                 reaction_model=a.reaction_model)


if __name__ == "__main__":
    main()
