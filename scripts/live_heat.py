#!/usr/bin/env python3
"""Chat-only live heat watcher for the Vice City Vibes clipper engine.

    python scripts/live_heat.py <youtube-live-url> [more urls...] [--minutes 20]
    python scripts/live_heat.py <url> --lookback-minutes 30 [--ratio 1.5]
    python scripts/live_heat.py <vod-url> --full-stream [--ratio 1.5]

Why this exists
---------------
The old watcher (src/clipper/infra/gtavi/live.py) records 20 minutes of
video per stream and then looks for chat spikes in the recording. Through
this VM's egress proxy that is wasteful and lossy: the 2026-11-20
BurntPeanut run captured only ~4 min of video in 20 min and found 0 spikes.
Heat does not need video at all.

How it works
------------
1. HEAT comes from live chat + viewers only (no video needed). Each stream
   gets one yt-dlp `--skip-download --write-subs --sub-langs live_chat`
   tail; the spike rule is live.py's shape (10 s window, 300 s baseline,
   ratio 2, min 8 msgs, 45 s cooldown, 8 s lag) — ratio lowered from 3
   to 2 on 2026-11-20 (Ajay: 3x never fired on fast steady chats).
   ChatTail is imported from live.py, not re-implemented.
2. VIDEO is fetched only for spike windows. On a spike, the spike's
   wall-clock time is mapped to a stream-relative offset
   (offset = wall - release_timestamp, verified present on live streams)
   and ONLY that window (spike - 15 s .. spike + 20 s) is fetched from
   YouTube's DVR via yt-dlp --download-sections. The fetched file is
   verified with ffprobe (video + audio streams, > 100 KB) before it is
   reported. A failed fetch is logged and watching continues.
3. Outputs per stream go to jobs/<video-id>/live-heat/: spikes.md (same
   table format as live.py) plus spike_<n>.mp4 window files, ready for
   the Modal GPU cutter. Nothing is posted from here.

Lookback mode (--lookback-minutes N or --full-stream)
------------------------------------------------------
Before (live) or instead of (VOD) watching forward, scan the chat replay
for spikes with the same rule shape (10 s windows, 300 s weighted
baseline, tunable --ratio, default 2.0, min 8 msgs, 45 s cooldown).
The bar is only a candidate filter — the final call is whether the
moment is good, so a lower ratio in lookback mode just yields more
candidates to review. Candidates are recorded in spikes.md; video
windows are fetched only for the top candidates (cap
--max-lookback-fetch, default 8, highest rate first), oldest ones first
in the table. Multiple URLs run in parallel threads, lookback included.
Live streams: the replay is downloaded with yt-dlp --live-from-start
(chat only); ended streams (VODs): plain --write-subs live_chat, and
windows are fetched from the VOD itself, where --download-sections
works over the whole video.

Replay limits (measured 2026-11-22, do not re-litigate without new data)
------------------------------------------------------------------------
While a stream is LIVE, yt-dlp can only serve roughly the last ~10 min
of chat history, whatever you ask for: --live-from-start and
--download-sections on the chat track both return the recent window
only (verified: 2 probes, ~8 min span each) and the downloader never
exits on its own — it follows the live edge. So a live lookback covers
~10 min max, however large N is. The full history exists only once the
stream ends (VOD chat replay). VOD replay download runs at ~9x
real-time single-fragment, faster with --concurrent-fragments 4, so a
full-stream scan of a long VOD is bounded by download time (6.1 h VOD
~10 min with concurrency; 23.7 h VOD ~40 min) and disk (~1.5 MB per
minute of dense chat). --download-sections does NOT bound the chat
download on either live or VOD: the chat fragments always start from
the beginning of the available replay.

The chat tail is restarted whenever it dies or stalls (no new lines for
STALL_SECONDS, default 180 s), up to MAX_RESTARTS per stream — each
restart writes to a fresh file (chat-r1, chat-r2, ...) so the tail's
per-file byte offset can never wedge on a truncated file.

DVR window
----------
YouTube keeps ~15 min of DVR on a live stream (playlist_duration 900), so
a spike window is fetched immediately after it ends (spike + 20 s + 8 s
lag). If a fetch starts more than ~15 min after the spike, the window is
gone — the fetch fails fast and is logged, never retried.

Fetch speed through this VM's egress proxy is ~0.3-0.6x real-time (a
35 s window takes ~1-3 min), so fetches run in a background thread per
spike and never block chat watching. The watch phase itself downloads
no video at all: no ffmpeg, no .ts segments, only the tiny live-chat JSON.
"""
import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "src"))
sys.path.insert(0, str(ENGINE / "src" / "clipper" / "infra" / "gtavi"))

from clipper.infra import ssl_fix  # noqa: E402
from clipper.paths import jobs_dir  # noqa: E402

common = ssl_fix.patch_yt_dlp()  # --no-check-certificates for yt-dlp (egress proxy)
import live as live_mod  # noqa: E402  (ChatTail + spike constants; no side effects on import)

ssl_fix.patch_module(live_mod)

from common import save_json, ytdlp_base, ytdlp_json  # noqa: E402  (patched via common)

# Spike rule: same shape as live.py, ratio lowered 3 -> 2 (Ajay, 2026-11-20).
WINDOW = 10          # seconds per heat window
BASELINE = 300       # seconds of weighted chat history for the baseline
RATIO = 2.0          # spike = rate >= RATIO * baseline rate
MIN_MSGS = 8         # raw messages per window to count as a spike
COOLDOWN = 45        # seconds between spikes on one stream
LAG = 8              # seconds to shift the clip later (stream delay + chat reaction)
STALL_SECONDS = 180  # no new chat lines for this long => tail stalled, restart it
MAX_RESTARTS = 10    # cap on chat-tail restarts per stream
BEFORE = 15          # seconds before the spike to include in the fetched window
AFTER = 20           # seconds after the spike to include in the fetched window
MIN_CLIP_BYTES = 100 * 1024
# Item #8 (2026-11-25): prefer true 1080p60, then any 1080p, then 720p.
# live HLS offers video-only + audio-only; no combined format.
# 1080p cap kept: the 1440p upload trick is out of scope.
FORMAT = "bv*[height=1080][fps=60]+ba/bv*[height=1080]+ba/bv*[height<=720]+ba/b"
REPLAY_CONCURRENT_FRAGMENTS = 4  # parallel fragment requests (egress proxy is the bottleneck)
REPLAY_POLL_SECS = 10            # how often to check replay catch-up
REPLAY_DOWNLOAD_TIMEOUT = 600    # max seconds downloading a chat replay
LOOKBACK_MAX_FETCH = 8           # cap on video windows fetched in the lookback phase
LOOKBACK_DVR_SECS = 900          # live DVR window; older live candidates can't be fetched


def now():
    return time.time()


def fmt_offset(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def verify_clip(path):
    """ffprobe guard (same spirit as the Modal _check_download): needs a
    video stream AND an audio stream, >100 KB, duration > 0. Never raises."""
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
    except Exception as exc:  # noqa: BLE001 — verification must never crash the watcher
        return False, f"verify error: {exc}"


class RestartSafeTail(live_mod.ChatTail):
    """ChatTail that also follows restarted tails and survives truncation.

    live.py's poll() globs only ``chat.live_chat.json*`` and seeks to a
    stored per-file byte offset. A restarted yt-dlp writing to a NEW file
    (chat-rN.live_chat.json) would be invisible to it, and one truncating
    the SAME file would wedge it (offset past EOF, reads nothing forever).
    This subclass widens the glob to ``chat*.live_chat.json*`` and resets
    the offset when a file shrinks under it. Message-id dedupe in
    handle() prevents double-counting replayed chat.
    """

    def poll(self):
        for f in sorted(self.d.glob("chat*.live_chat.json*")):
            try:
                size = f.stat().st_size
            except OSError:
                continue
            pos = self.pos.get(f.name, 0)
            if pos > size:
                # file was truncated/recreated under us; start over
                self.pos[f.name] = 0
                self.buf[f.name] = b""
                pos = 0
            try:
                with open(f, "rb") as fh:
                    fh.seek(pos)
                    data = fh.read()
            except OSError:
                continue
            if not data:
                continue
            self.pos[f.name] = pos + len(data)
            buf = self.buf.get(f.name, b"") + data
            *lines, rest = buf.split(b"\n")
            self.buf[f.name] = rest
            for ln in lines:
                self.handle(ln)


class StreamWatcher(threading.Thread):
    """Watch one YouTube live stream's chat; fetch DVR windows for spikes."""

    def __init__(self, url, minutes, args):
        super().__init__(daemon=True, name=f"watch-{url[-11:]}")
        self.url = url
        self.minutes = minutes
        self.a = args
        self.stop_flag = threading.Event()
        self.spikes = 0
        self.fetched = 0
        self.failed = 0
        self.chat_lines = 0
        self.restarts = 0
        self.lb_spikes = 0       # lookback candidates found
        self.lb_fetched = 0      # lookback windows fetched OK
        self.lb_failed = 0       # lookback fetches failed / skipped
        self.lb_coverage = None  # (first_ts, last_ts) of replay actually scanned
        self._is_live = True
        self._last_restart_at = 0.0
        self._fetch_threads = []

    def log(self, msg):
        print(f"[{self.name}] {msg}", flush=True)

    def fetch_window(self, num, spike_wall, words, rate, stream_start, prefix="spike"):
        """Download one spike window from DVR (live) or VOD and verify it.
        Runs in its own thread so chat watching never stalls (~2 min per
        35 s window). ``prefix`` is "spike" for live spikes, "lb" for
        lookback candidates (files lb_<n>.mp4)."""
        out = self.ld / f"{prefix}_{num}.mp4"
        s = int(spike_wall - self.a.before - stream_start)
        e = int(spike_wall + self.a.after - stream_start)
        if s < 0:
            s = 0
        if e <= s:
            self.log(f"{prefix} #{num}: bad window {s}-{e}s, skipping fetch")
            self.failed += 1
            return
        # Item #7: disk-guard pre-flight (clipper.core.disk_guard lands
        # in parallel; until then the import fails and we proceed
        # unguarded). Window size is unknown up front, so the
        # conservative 500 MB default.
        try:
            from clipper.core.disk_guard import preflight
            preflight(self.ld, 500 * 1024 * 1024)
        except ImportError:
            pass
        self.log(f"{prefix} #{num}: fetching {'DVR' if self._is_live else 'VOD'} *{s}-{e}s "
                 f"(offset {fmt_offset(s)}-{fmt_offset(e)}) ...")
        # Item #7: hardened retry flags ride in the patched ytdlp_base();
        # add only the per-download archive (reruns skip completed IDs).
        cmd = (ytdlp_base() + ssl_fix.hardened_download_args(self.ld / "archive.txt")
               + ["--download-sections", f"*{s}-{e}",
                  "-f", FORMAT, "--merge-output-format", "mp4",
                  "-o", str(out), self.url])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            self.log(f"{prefix} #{num}: fetch TIMED OUT after 600s")
            self.failed += 1
            return
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            self.log(f"{prefix} #{num}: yt-dlp fetch FAILED rc={r.returncode}: "
                     f"{err[-1][:200] if err else 'no output'}")
            self.failed += 1
            return
        ok, detail = verify_clip(out)
        iso = datetime.fromtimestamp(spike_wall, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        off = fmt_offset(spike_wall - stream_start)
        if ok:
            self.fetched += 1
            self.log(f"{prefix} #{num}: VERIFIED {out.name} ({detail})")
            row = f"| {iso} | {off} | {rate} | {', '.join(words)} | {out.name} |\n"
        else:
            self.failed += 1
            self.log(f"{prefix} #{num}: file FAILED verification: {detail}")
            row = f"| {iso} | {off} | {rate} | {', '.join(words)} | FETCH FAILED ({detail}) |\n"
        with self._md_lock:
            with open(self.spikes_md, "a", encoding="utf-8") as fh:
                fh.write(row)

    def _scan_replay(self, tail, t_from, t_to, ratio, t_base):
        """Offline spike scan over already-parsed chat (lookback). Same rule
        shape as the live loop: 10 s windows, 300 s weighted baseline,
        tunable ratio, min 8 msgs, 45 s cooldown. Windows step 2 s.
        ``t_base`` is the earliest timestamp available for the baseline
        warm-up (may be earlier than ``t_from`` when only the tail of the
        replay is scanned). Returns [(wall, raw, words)] in time order."""
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

    def _lookback(self, info, stream_start):
        """Scan the chat replay for spikes before (live) or instead of (VOD)
        watching forward. Returns True if the phase ran. Live: downloads
        the replay with --live-from-start until caught up (newest message
        within 30 s of now) or --replay-timeout; only ~10 min of history
        is available while live (measured). VOD: downloads the whole
        replay (exits on its own) up to --replay-timeout. Candidates are
        written to spikes.md; the top --max-lookback-fetch by rate get
        their windows fetched (lb_<n>.mp4)."""
        a = self.a
        is_live = bool(info.get("is_live"))
        self._is_live = is_live
        n_min = a.lookback_minutes if a.lookback_minutes > 0 else None
        full = bool(a.full_stream)
        rdir = self.ld / "replay"
        rdir.mkdir(parents=True, exist_ok=True)
        cmd = (ytdlp_base() + ["--skip-download", "--write-subs",
                               "--sub-langs", "live_chat",
                               "--concurrent-fragments",
                               str(REPLAY_CONCURRENT_FRAGMENTS)]
               + (["--live-from-start"] if is_live else [])
               + ["-o", str(rdir / "chat"), self.url])
        self.log(f"lookback: downloading chat replay "
                 f"({'live' if is_live else 'VOD'}, "
                 f"{'full stream' if full else f'last {n_min} min'}) ...")
        # Item #7: disk-guard pre-flight (clipper.core.disk_guard lands
        # in parallel; until then the import fails and we proceed
        # unguarded). Replay size is unknown up front, so the
        # conservative 500 MB default.
        try:
            from clipper.core.disk_guard import preflight
            preflight(rdir, 500 * 1024 * 1024)
        except ImportError:
            pass
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            self.log(f"lookback: could not start replay download: {exc}")
            return False
        # Accept every replayed message: start=0 disables handle()'s
        # "history backlog" drop (it drops ts < start - 5).
        tail = RestartSafeTail(rdir, 0)
        deadline = now() + a.replay_timeout
        caught_up = False
        while now() < deadline:
            if proc.poll() is not None:
                caught_up = True  # VOD: replay fully downloaded
                break
            tail.poll()
            if is_live and tail.raw and max(tail.raw) >= now() - 30:
                caught_up = True  # live edge reached
                break
            time.sleep(REPLAY_POLL_SECS)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        tail.poll()
        if not tail.raw:
            self.log("lookback: no chat in replay (chat disabled, empty, "
                     "or download failed) — skipping lookback")
            return False
        first_ts = min(tail.raw)
        last_ts = max(tail.raw)
        self.lb_coverage = (first_ts, last_ts)
        if full:
            scan_from = first_ts
        elif n_min:
            scan_from = max(first_ts, last_ts - n_min * 60)
        else:
            scan_from = first_ts
        scan_to = last_ts
        self.log(f"lookback: replay covers "
                 f"{datetime.fromtimestamp(first_ts, timezone.utc).strftime('%H:%M')}–"
                 f"{datetime.fromtimestamp(last_ts, timezone.utc).strftime('%H:%M')} UTC "
                 f"({len(tail.raw)} chat-seconds, {sum(tail.raw.values())} msgs)"
                 + (" (caught up)" if caught_up else " (download capped)"))
        cands = self._scan_replay(tail, scan_from, scan_to, a.ratio, first_ts)
        self.lb_spikes = len(cands)
        self.log(f"lookback: scanned {fmt_offset(scan_to - scan_from)} of chat "
                 f"at ratio {a.ratio}: {len(cands)} candidate spike(s)")
        if not cands:
            return True
        # Fetch the top candidates by rate (cap); record the rest as
        # candidates with offsets so they can be cut from the VOD later.
        s0 = stream_start or first_ts
        fetch_idx = {i for i, _ in sorted(enumerate(cands),
                                          key=lambda p: p[1][1], reverse=True)
                     [:a.max_lookback_fetch]}
        f0, fa0 = self.fetched, self.failed
        threads = []
        for i, (wall, raw, words) in enumerate(cands):
            num = i + 1
            iso = datetime.fromtimestamp(wall, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            off = fmt_offset(wall - s0)
            if i not in fetch_idx:
                row = (f"| {iso} | {off} | {raw} | {', '.join(words)} | "
                       f"CANDIDATE (lookback, over fetch cap — cut from VOD) |\n")
                with self._md_lock:
                    with open(self.spikes_md, "a", encoding="utf-8") as fh:
                        fh.write(row)
                continue
            if is_live and now() - wall > LOOKBACK_DVR_SECS:
                row = (f"| {iso} | {off} | {raw} | {', '.join(words)} | "
                       f"CANDIDATE (lookback, DVR expired — cut from VOD) |\n")
                with self._md_lock:
                    with open(self.spikes_md, "a", encoding="utf-8") as fh:
                        fh.write(row)
                self.lb_failed += 1
                continue
            th = threading.Thread(target=self.fetch_window,
                                  args=(num, wall, words, raw, s0),
                                  kwargs={"prefix": "lb"}, daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join(timeout=300)
        done = sum(1 for th in threads if not th.is_alive())
        self.lb_fetched = self.fetched - f0
        self.lb_failed += self.failed - fa0
        self.log(f"lookback: {len(threads)} fetch(es) started, {done} finished "
                 f"within the join window (rest continue in background)")
        return True

    def run(self):
        self._md_lock = threading.Lock()
        try:
            self._run()
        except Exception:  # noqa: BLE001 — one dead stream must not kill the others
            self.log("WATCHER ERROR:\n" + traceback.format_exc())

    def _run(self):
        try:
            info = ytdlp_json(self.url)
        except Exception as exc:  # noqa: BLE001
            self.log(f"could not read stream info: {exc}")
            return
        is_live = bool(info.get("is_live"))
        self._is_live = is_live
        want_lb = bool(self.a.lookback_minutes > 0 or self.a.full_stream)
        if not is_live and not want_lb:
            self.log(f"not live (live_status={info.get('live_status')}), skipping")
            return
        vid = info.get("id") or "unknown"
        title = info.get("title", "")
        viewers = info.get("concurrent_view_count") or info.get("view_count")
        stream_start = info.get("release_timestamp")
        self.ld = jobs_dir() / vid / "live-heat"
        self.ld.mkdir(parents=True, exist_ok=True)
        self.spikes_md = self.ld / "spikes.md"
        if not self.spikes_md.exists():
            self.spikes_md.write_text(
                f"| UTC time | offset | msgs/{WINDOW}s | top words | file |\n"
                "|---|---|---|---|---|\n", encoding="utf-8")
        if is_live:
            age = int(now() - stream_start) if stream_start else None
            self.log(f"LIVE {title[:80]} | {viewers} watching | "
                     f"stream age {fmt_offset(age) if age else '?'} | out {self.ld}")
        else:
            dur = info.get("duration")
            self.log(f"VOD {title[:80]} | "
                     f"duration {fmt_offset(dur) if dur else '?'} | out {self.ld}")
        if not stream_start:
            self.log("WARNING: no release_timestamp in stream info; "
                     "offset mapping will fall back to earliest chat timestamp")
        if want_lb:
            self._lookback(info, stream_start)
        if not is_live:
            # VOD: lookback only, no forward watch.
            save_json(self.ld / "live-heat.json", {
                "url": self.url, "video_id": vid, "title": title,
                "stream_start": stream_start, "viewers": viewers,
                "mode": "vod-lookback",
                "lookback_minutes": self.a.lookback_minutes,
                "full_stream": self.a.full_stream,
                "ratio": self.a.ratio,
                "spikes": self.spikes, "fetched": self.fetched,
                "fetch_failed": self.failed, "minutes": 0,
                "chat_lines": 0,
                "lookback_spikes": self.lb_spikes,
                "lookback_fetched": self.lb_fetched,
                "lookback_failed": self.lb_failed,
                "lookback_coverage": self.lb_coverage,
            })
            self.log(f"done (VOD lookback): {self.lb_spikes} candidate(s), "
                     f"{self.lb_fetched} fetched, {self.lb_failed} failed. "
                     f"See {self.spikes_md}")
            return
        start = now()
        end_at = start + self.minutes * 60
        tail = RestartSafeTail(self.ld, start)
        chat = self._start_chat()
        if chat is None:
            self.log("could not start live-chat tail, giving up on this stream")
            return
        pending = []  # (num, spike_wall, rate, words, fetch_at)
        last_spike = 0.0
        last_heat = 0.0
        last_chat_growth = start
        last_lines = 0
        try:
            while now() < end_at and not self.stop_flag.is_set():
                time.sleep(1)
                tail.poll()
                lines = sum(tail.raw.values())
                if lines != last_lines:
                    last_lines = lines
                    last_chat_growth = now()
                t = now()
                # --- stream-end / chat-feed death handling ---
                if chat.poll() is not None:
                    if (self.restarts < MAX_RESTARTS and now() < end_at - 60
                            and now() - self._last_restart_at > 20):
                        chat = self._restart_tail(chat, "feed died")
                        if chat is None:
                            self.log("chat restart failed, finishing stream")
                            break
                        last_chat_growth = now()
                        last_lines = sum(tail.raw.values())
                    else:
                        self.log("live-chat feed ended (stream over or feed closed)")
                        break
                elif now() - last_chat_growth > self.a.stall_secs:
                    if self.restarts < MAX_RESTARTS and now() < end_at - 60:
                        self.log(f"WARNING: no new chat lines for "
                                 f"{int(self.a.stall_secs)}s (feed stalled)")
                        chat = self._restart_tail(chat, "feed stalled")
                        if chat is None:
                            self.log("chat restart failed, finishing stream")
                            break
                        last_chat_growth = now()
                        last_lines = sum(tail.raw.values())
                    else:
                        self.log(f"WARNING: no new chat lines for "
                                 f"{int(self.a.stall_secs)}s (feed stalled, "
                                 "restart cap reached)")
                        last_chat_growth = now()
                # --- periodic heat line (proves the tail is alive) ---
                if t - last_heat >= 60:
                    last_heat = t
                    e = int(t) - 3
                    raw10 = tail.total(tail.raw, e - WINDOW + 1, e)
                    nb = min(BASELINE, max(1, int(e - start) - WINDOW))
                    bw = tail.total(tail.wgt, e - WINDOW - nb + 1, e - WINDOW) / nb
                    self.log(f"[heat] {raw10} msgs/{WINDOW}s | "
                             f"baseline {bw * WINDOW:.1f} weighted | "
                             f"chat lines total {lines}")
                # --- spike rule (identical to live.py) ---
                e = int(t) - 3
                elapsed_chat = e - int(start)
                if (elapsed_chat >= WINDOW + 30 and t - last_spike >= COOLDOWN):
                    raw = tail.total(tail.raw, e - WINDOW + 1, e)
                    w = tail.total(tail.wgt, e - WINDOW + 1, e)
                    nb = min(BASELINE, elapsed_chat - WINDOW)
                    bw = tail.total(tail.wgt, e - WINDOW - nb + 1, e - WINDOW) / nb
                    rate_w = w / WINDOW
                    if raw >= MIN_MSGS and rate_w >= self.a.ratio * max(bw, 0.02):
                        self.spikes += 1
                        last_spike = t
                        words = tail.top_words(e - WINDOW + 1, e)
                        self.log(f"SPIKE #{self.spikes}: {raw} msgs/{WINDOW}s vs "
                                 f"baseline {bw * WINDOW:.1f} weighted | {words}")
                        pending.append((self.spikes, float(e), raw, words,
                                        t + self.a.after + max(LAG, 0)))
                # --- due fetches (background threads, watching continues) ---
                for p in [p for p in pending if p[4] <= t]:
                    pending.remove(p)
                    num, wall, rate, words, _ = p
                    s0 = stream_start
                    if not s0:
                        # Fallback: earliest chat timestamp ≈ stream start.
                        s0 = min(tail.raw) if tail.raw else None
                        if s0:
                            self.log(f"spike #{num}: using earliest chat ts as stream start")
                    if not s0:
                        self.log(f"spike #{num}: no stream start known, cannot map offset")
                        self.failed += 1
                        continue
                    th = threading.Thread(target=self.fetch_window,
                                          args=(num, wall, words, rate, s0),
                                          daemon=True)
                    th.start()
                    self._fetch_threads.append(th)
        finally:
            if chat is not None and chat.poll() is None:
                chat.terminate()
                try:
                    chat.wait(10)
                except subprocess.TimeoutExpired:
                    chat.kill()
            # Flush any spike whose window has fully aired.
            for p in pending:
                num, wall, rate, words, _ = p
                s0 = stream_start or (min(tail.raw) if tail.raw else None)
                if s0 and wall + self.a.after <= now():
                    th = threading.Thread(target=self.fetch_window,
                                          args=(num, wall, words, rate, s0),
                                          daemon=True)
                    th.start()
                    self._fetch_threads.append(th)
            for th in self._fetch_threads:
                th.join(timeout=240)
            self.chat_lines = sum(tail.raw.values())
            save_json(self.ld / "live-heat.json", {
                "url": self.url, "video_id": vid, "title": title,
                "stream_start": stream_start, "viewers": viewers,
                "mode": "live-watch" + ("+lookback" if want_lb else ""),
                "lookback_minutes": self.a.lookback_minutes,
                "full_stream": self.a.full_stream,
                "ratio": self.a.ratio,
                "spikes": self.spikes, "fetched": self.fetched,
                "fetch_failed": self.failed, "minutes": self.minutes,
                "chat_lines": self.chat_lines,
                "lookback_spikes": self.lb_spikes,
                "lookback_fetched": self.lb_fetched,
                "lookback_failed": self.lb_failed,
                "lookback_coverage": self.lb_coverage,
            })
        self.log(f"done: {self.spikes} spike(s), {self.fetched} fetched, "
                 f"{self.failed} failed"
                 + (f" (lookback: {self.lb_spikes} candidate(s), "
                    f"{self.lb_fetched} fetched)" if self.lb_spikes or want_lb else "")
                 + f". See {self.spikes_md}")

    def _start_chat(self, stem="chat"):
        """Start yt-dlp live-chat tail (chat only, no video). Returns the
        Popen object or None on immediate failure. ``stem`` is the output
        filename stem; restarts use a fresh stem (chat-r1, chat-r2, ...)
        so the tail's per-file byte offset starts at 0."""
        try:
            return subprocess.Popen(
                ytdlp_base() + ["--skip-download", "--write-subs",
                                "--sub-langs", "live_chat",
                                "-o", str(self.ld / stem), self.url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            self.log(f"failed to start chat tail: {exc}")
            return None

    def _restart_tail(self, chat, reason):
        """Kill the old tail (if alive) and start a fresh one on a UNIQUE
        output file (chat-rN). Returns the new Popen or None.
        The unique name is load-bearing: ChatTail tracks a byte offset per
        filename, so a restarted yt-dlp truncating the same file would
        wedge the tail forever (offset past EOF, reads nothing). A fresh
        filename starts at offset 0; message-id dedupe in handle()
        prevents double-counting if the new tail replays recent chat."""
        self.restarts += 1
        self._last_restart_at = now()
        if chat is not None and chat.poll() is None:
            try:
                chat.terminate()
                chat.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    chat.kill()
                except Exception:  # noqa: BLE001
                    pass
        backoff = min(2 ** self.restarts, 10)
        self.log(f"chat tail {reason} — restarting #{self.restarts} "
                 f"(backoff {backoff}s, output chat-r{self.restarts}) ...")
        time.sleep(backoff)
        new = self._start_chat(f"chat-r{self.restarts}")
        if new is None:
            self.log("chat restart failed to start")
        return new


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("urls", nargs="+", help="YouTube live URLs to watch")
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--before", type=float, default=BEFORE)
    ap.add_argument("--after", type=float, default=AFTER)
    ap.add_argument("--stall-secs", type=float, default=STALL_SECONDS,
                    help="restart the chat tail after this many seconds "
                         "with no new chat lines")
    ap.add_argument("--lookback-minutes", type=float, default=0,
                    help="before watching live (or instead of it, for a VOD), "
                         "scan the last N minutes of chat replay for spikes. "
                         "No cap. 0 = off. Live streams can only serve ~10 min "
                         "of history, however large N is (measured).")
    ap.add_argument("--full-stream", action="store_true",
                    help="scan the entire available chat replay (VOD: whole "
                         "stream; live: whatever history yt-dlp can serve)")
    ap.add_argument("--ratio", type=float, default=RATIO,
                    help="spike bar: rate >= RATIO * baseline (default 2.0). "
                         "A lower ratio in lookback mode yields more "
                         "candidates to review.")
    ap.add_argument("--replay-timeout", type=float, default=REPLAY_DOWNLOAD_TIMEOUT,
                    help="max seconds to spend downloading the chat replay")
    ap.add_argument("--max-lookback-fetch", type=int, default=LOOKBACK_MAX_FETCH,
                    help="max video windows to fetch in the lookback phase "
                         "(highest rate first)")
    a = ap.parse_args()
    watchers = [StreamWatcher(u, a.minutes, a) for u in a.urls]
    for w in watchers:
        w.start()
    try:
        for w in watchers:
            w.join()
    except KeyboardInterrupt:
        print("stopping...", flush=True)
        for w in watchers:
            w.stop_flag.set()
        for w in watchers:
            w.join(timeout=30)
    total_spikes = sum(w.spikes for w in watchers)
    total_fetched = sum(w.fetched for w in watchers)
    print(f"ALL DONE: {total_spikes} spike(s), {total_fetched} window(s) fetched "
          f"across {len(watchers)} stream(s)")


if __name__ == "__main__":
    main()
