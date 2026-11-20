#!/usr/bin/env python3
"""Vice City Vibes — personal web dashboard (read-only).

Stdlib-only HTTP server. Serves one page at / and a JSON snapshot at
/api/state, built from the watcher's state — no new dependencies.

Data sources (first hit wins):
  * /data/streams.json, /data/trends.md, /data/clips.json,
    /data/watcher.log   — the Fly volume, written by the watcher
  * <repo>/dashboard/streams.json, trends.md, garage/clips.json
    — local fallback (what `python -m clipper.app.dashboard` writes)

Sections: hero, stat strip, 01 Most Wanted, 02 Stream Dashboard,
03 Auto-pilot top pick, 04 Hype Meter, 05 Cutting Room, 06 Video
Garage, Export formats, watcher status / last run, Modal cutter
status, and Fly machine status.

READ-ONLY and NO AUTH (for now): this is Ajay's personal view of his
own clipper. There are no buttons that record, cut or post anything —
posting still needs his explicit approval, per Vice City Vibes Rule 1.
Do not expose this URL publicly and do not add posting actions without
auth + approval. The page says so in its footer.

Usage:
    python -m clipper.app.web [--port 8080] [--host 0.0.0.0]
    # env: PORT, CLIPPER_HOME (repo root), MODAL_CUT_URL (optional)
"""
import html
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clipper.paths import repo_root

DATA = Path("/data")
ROOT = repo_root()
APP_NAME = "vice-city-vibes-clipper"
PUBLIC_URL = "https://vice-city-vibes-clipper.fly.dev"


def _load_json(name, fallback_dir):
    for base in (DATA, fallback_dir):
        p = Path(base) / name
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8")), p
        except Exception:
            pass
    return None, None


def _read_text(name, fallback_dir, limit=0):
    for base in (DATA, fallback_dir):
        p = Path(base) / name
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                return (text[-limit:] if limit and len(text) > limit else text), p
        except Exception:
            pass
    return "", None


def _parse_trends_md(text, limit=5):
    """Parse the trends.md markdown table into rows."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or "---" in line or "views/h" in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7 or not cells[0].isdigit():
            continue
        m = re.search(r"\[(.*?)\]\((.*?)\)", cells[6])
        rows.append({
            "rank": int(cells[0]), "views_per_hour": cells[1],
            "views": cells[2], "age_h": cells[3],
            "channel": cells[5], "title": m.group(1) if m else cells[6],
            "url": m.group(2) if m else "",
        })
        if len(rows) >= limit:
            break
    return rows


def _hype(viewers):
    """0-100 hype score from live viewers (log-ish scale)."""
    try:
        v = float(viewers or 0)
    except Exception:
        v = 0
    if v <= 0:
        return 0
    return max(1, min(100, int(12 * (v ** 0.5))))



def _thumb(r):
    """Live preview image for a stream row: Twitch live preview CDN,
    or the YouTube video thumbnail. Empty string when unresolvable."""
    url = (r.get("url") or "")
    plat = (r.get("platform") or "").lower()
    if "twitch" in plat:
        login = (r.get("channel") or "").strip().lower()
        if login:
            return f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-640x360.jpg"
        return ""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if m:
        return f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg"
    return ""

def _thumb_img(r, cls="thumb-img", alt=""):
    t = _thumb(r)
    if not t:
        return '<div class="thumb-fallback">VICE CITY VIBES</div>'
    return (f'<img class="{cls}" src="{html.escape(t, quote=True)}" '
            f'alt="{html.escape(alt or r.get("channel") or "stream", quote=True)}" loading="lazy">')

def _modal_status():
    cut_url = os.environ.get("MODAL_CUT_URL", "").strip()
    if not cut_url:
        return {"state": "not_configured",
                "note": "MODAL_CUT_URL not set as a Fly secret yet"}
    health_url = cut_url.rsplit("/cut", 1)[0] + "/health" \
        if "/cut" in cut_url else cut_url
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        return {"state": "ok" if body.get("status") == "ok" else "up",
                "url": health_url, "detail": body}
    except Exception as e:
        return {"state": "unreachable", "url": health_url, "note": str(e)[:200]}


def collect_state():
    streams, streams_path = _load_json("streams.json", ROOT / "dashboard")
    trends_text, trends_path = _read_text("trends.md", ROOT / "dashboard")
    clips, clips_path = _load_json("clips.json", ROOT / "garage")
    log_text, log_path = _read_text("watcher.log", ROOT, limit=6000)

    ranked = (streams or {}).get("ranked_all") or []
    top = ranked[0] if ranked else None
    log_lines = [l for l in log_text.splitlines() if l.strip()]
    last_run = log_lines[-1] if log_lines else ""

    def _mtime(p):
        try:
            return datetime.fromtimestamp(p.stat().st_mtime,
                                          tz=timezone.utc).isoformat() if p else None
        except Exception:
            return None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app": APP_NAME, "public_url": PUBLIC_URL,
        "fly": {"region": "dfw", "machine": "shared-cpu-1x @ 1GB",
                "state": "running — this page is served by it",
                "note": "Watcher runs in the same machine; "
                        "auto_stop_machines is off, min_machines_running=1."},
        "streams": {"checked_at": (streams or {}).get("checked_at"),
                    "source": str(streams_path) if streams_path else None,
                    "youtube_live": len((streams or {}).get("youtube_live") or []),
                    "twitch_live": len((streams or {}).get("twitch_live") or []),
                    "ranked": [{**r, "hype": _hype(r.get("viewers"))}
                               for r in ranked[:12]]},
        "top_pick": top,
        "most_wanted": _parse_trends_md(trends_text),
        "trends_source": str(trends_path) if trends_path else None,
        "garage": {"count": len(clips or []),
                   "source": str(clips_path) if clips_path else None,
                   "clips": (clips or [])[:20]},
        "watcher": {"last_log_line": last_run,
                    "log_tail": log_lines[-12:],
                    "log_source": str(log_path) if log_path else None,
                    "streams_updated": _mtime(streams_path),
                    "interval": "every 15 min"},
        "modal": _modal_status(),
    }


def _esc(s):
    return html.escape(str(s or ""), quote=True)


def render(state):
    s = state["streams"]
    ranked = s["ranked"]
    top = state["top_pick"] or {}
    wanted = state["most_wanted"]
    clips = state["garage"]["clips"]
    modal = state["modal"]
    log = state["watcher"]["log_tail"]

    def stars(h):
        filled = max(0, min(5, int(round((h or 0) / 20))))
        return "".join("★" if i < filled else "☆" for i in range(5))

    # ---- Most Wanted ranked rows (01..06, gold numbers, heat bar) ----
    wanted_rows = ""
    for i, w in enumerate(wanted, 1):
        wanted_rows += (
            f'<div class="mw-row"><span class="mw-rank">{i:02d}</span>'
            f'<a class="mw-thumb" href="{_esc(w.get("url"))}" target="_blank" rel="noopener">{_thumb_img(w)}</a>'
            f'<div class="mw-main"><a href="{_esc(w.get("url"))}" target="_blank" rel="noopener">'
            f'{_esc(w.get("title"))}</a>'
            f'<span class="mw-sub">{_esc(w.get("channel"))} · {_esc(w.get("views_per_hour"))} views/h</span></div>'
            f'<span class="mw-heat">{_esc(w.get("views_per_hour"))}</span></div>')
    if not wanted_rows:
        wanted_rows = '<p class="dim">No trend data yet — watcher refreshes every 15 min.</p>'

    # ---- Live streams: preview-image cards in a grid ----
    stream_cards = ""
    for i, r in enumerate(ranked, 1):
        plat = (_esc(r.get("platform")) or "").lower()
        plat_cls = "twitch" if "twitch" in plat else "youtube"
        stream_cards += (
            f'<div class="stream-card"><a class="thumb" href="{_esc(r.get("url"))}" target="_blank" rel="noopener">'
            f'{_thumb_img(r)}<span class="live-badge">● LIVE</span>'
            f'<span class="viewers-badge">{_esc(r.get("viewers"))} watching</span></a>'
            f'<div class="stream-body"><span class="stream-name">{_esc(r.get("channel"))}</span>'
            f'<span class="stream-title">{_esc(r.get("title"))}</span>'
            f'<span class="stream-sub"><span class="plat {plat_cls}">{_esc(r.get("platform"))}</span>'
            f' <span class="stars">{stars(r["hype"])}</span></span>'
            f'<div class="hypebar"><i style="width:{r["hype"]}%"></i></div>'
            f'<span class="hype-num">{r["hype"]}% hype</span> '
            f'<a class="watch-link" href="{_esc(r.get("url"))}" target="_blank" rel="noopener">Watch ↗</a></div></div>')
    if not stream_cards:
        stream_cards = '<p class="dim">No live streams in the last watcher run.</p>'

    # ---- Garage clip cards ----
    clip_cards = ""
    for c in clips:
        badge = {"SAFE": "safe", "REVIEW": "review", "BLOCK": "block"}.get(
            c.get("safety"), "unknown")
        st = stars(int((c.get("stars") or 0) * 20))
        clip_cards += (
            f'<div class="clip-card"><div class="clip-poster {badge}">'
            f'<span class="poster-dur">{_esc(c.get("duration_s"))}s · 9:16</span>'
            f'<span class="poster-moment">{_esc(c.get("moment") or c.get("source_title") or "Clip")}</span>'
            f'<span><span class="badge {badge}">{_esc(c.get("safety"))}</span></span></div>'
            f'<div class="clip-body"><span class="stars">{st}</span>'
            f'<p>{_esc(c.get("status"))} · {_esc(c.get("stream"))}</p>'
            f'<div class="chips"><span class="chip">✂ Cut</span><span class="chip">CC Captions</span>'
            f'<span class="chip">9:16 Vertical</span><span class="chip">16:9</span></div></div></div>')
    if not clip_cards:
        clip_cards = "<p class=\"dim\">No clips registered yet.</p>"

    # ---- Cutting Room: progress from garage clip statuses ----
    n_new = sum(1 for c in clips if (c.get("status") or "").lower() == "new")
    n_block = sum(1 for c in clips if (c.get("safety") or "").upper() == "BLOCK")
    n_safe = sum(1 for c in clips if (c.get("safety") or "").upper() == "SAFE")
    n_total = len(clips)
    cutting_html = (
        f'<div class="steps">'
        f'<div class="step done"><b>✂ {n_total}</b><span>Cut</span></div>'
        f'<div class="step done"><b>▣ {n_total}</b><span>Reframe 9:16</span></div>'
        f'<div class="step done"><b>CC {n_total}</b><span>Captions</span></div>'
        f'<div class="step"><b>{n_safe} safe</b><span>{n_new} new · {n_block} blocked</span></div>'
        f'</div>')

    modal_cls = {"ok": "safe", "up": "safe"}.get(modal["state"], "review")
    log_html = "<br>".join(_esc(l) for l in log) or "No watcher log yet."
    top_hype = _hype(top.get("viewers")) if top else 0
    live_total = s["youtube_live"] + s["twitch_live"]

    css = """
*{box-sizing:border-box}
/* Design system — tokens + component patterns reused from proven MIT kits:
   - Tabler (tabler/tabler, MIT): plain-HTML dashboard kit — card, badge,
     progress-bar and ranked-list patterns; dark-mode-first, one CSS file.
   - shadcn/ui dashboard-01 blocks (shadcn-ui/ui, MIT): card / stat-card /
     badge / progress tokens — radius scale, muted foreground, ring on hover.
   Evaluated and NOT used: shadcn dashboard templates and TailAdmin — both
   are React/Next.js and need a build step; this server is stdlib-only
   Python with no build step, so the kits are applied as CDN-free inline
   design tokens (same values, no external dependency, page still renders
   if any CDN is unreachable). No paid library, no auth, no pricing. */
:root{
--bg:#07030f;--panel:#150a26;--panel2:#1e1030;--line:#3d2358;--gold:#ffd166;
--pink:#ff2e88;--orange:#ff5e3a;--violet:#c9a7e8;--dim:#9d7fc4;--ink:#f5eefc;
/* shadcn-style tokens: radius scale + semantic surfaces */
--radius-sm:6px;--radius-md:10px;--radius-lg:16px;--radius-xl:18px;
--card:#150a26;--card-hover:#1e1030;--muted:#9d7fc4;--muted-fg:#c9a7e8;
--primary:#ff2e88;--ring:rgba(123,47,247,.45);
/* Tabler-style tokens: spacing scale + border */
--sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:20px;--sp-6:24px;
--border:1px solid #3d2358}
html{scroll-behavior:smooth}
body{margin:0;font-family:'Segoe UI',system-ui,-apple-system,Roboto,Arial,sans-serif;
background:radial-gradient(1200px 500px at 50% -10%,#3b0a4e 0%,transparent 60%),var(--bg);color:var(--ink);line-height:1.45}
a{color:#7dd3fc;text-decoration:none} a:hover{text-decoration:underline}
/* --- hero --- */
.hero{position:relative;overflow:hidden;text-align:center;padding:26px 20px 0;
background:linear-gradient(180deg,#12041f 0%,#3b0a4e 42%,#7a1e63 68%,#ff5e3a 100%)}
.hero .sun{width:min(420px,80vw);height:200px;margin:6px auto -110px;border-radius:210px 210px 0 0;
background:repeating-linear-gradient(180deg,#ffd166 0 16px,transparent 16px 26px,#ff9a3d 26px 42px,transparent 42px 52px,#ff2e88 52px 70px,transparent 70px 80px);
opacity:.95;box-shadow:0 0 110px rgba(255,94,58,.55)}
.hero-inner{position:relative;z-index:2}
.script{font-family:'Segoe Script','Brush Script MT',cursive;font-size:28px;color:#ff7ad9;text-shadow:0 0 18px #ff2e88;margin:0}
.hero h1{margin:4px 0 0;font-size:clamp(38px,7vw,64px);line-height:.95;letter-spacing:2px;font-weight:900;text-transform:uppercase;
background:linear-gradient(90deg,#fff 10%,#ffd166 40%,#ff5e3a 65%,#ff2e88 90%);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero .tag{margin:12px auto 0;font-size:clamp(18px,3vw,26px);font-weight:800;color:#fff;letter-spacing:.5px}
.hero .tag b{color:var(--gold)}
.hero .sub{margin:8px 0 20px;color:#ffe9f5;font-size:15px}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;max-width:1180px;margin:0 auto 16px;position:relative;z-index:2}
.brand{font-weight:900;letter-spacing:3px;font-size:15px;color:#fff}
.brand b{color:var(--gold)}
.pill{display:inline-block;padding:5px 12px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase}
.pill.live{background:var(--pink);color:#fff;box-shadow:0 0 14px rgba(255,46,136,.7)}
.pill.onair{background:#1a0b2e;border:1px solid #ff9a3d;color:var(--gold)}
main{max-width:1180px;margin:0 auto;padding:22px 18px 10px}
.secnum{font-size:13px;font-weight:800;letter-spacing:3px;color:var(--gold);text-transform:uppercase}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:-64px 0 18px;position:relative;z-index:3}
@media(max-width:760px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:rgba(23,11,40,.92);backdrop-filter:blur(6px);border:1px solid #4a1e6e;border-radius:16px;padding:18px 12px;text-align:center;box-shadow:0 10px 34px rgba(0,0,0,.5)}
.stat b{display:block;font-size:36px;color:var(--gold);line-height:1}
.stat span{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:var(--violet)}
.card{background:linear-gradient(180deg,#180c2c,#130a22);border:1px solid var(--line);border-radius:18px;padding:20px 22px;margin-bottom:20px;box-shadow:0 10px 34px rgba(0,0,0,.35)}
.card h2{margin:2px 0 4px;font-size:clamp(24px,3.4vw,32px);letter-spacing:1px;text-transform:uppercase;font-weight:900}
.card h2 .grad{background:linear-gradient(90deg,#ffd166,#ff5e3a,#ff2e88);-webkit-background-clip:text;background-clip:text;color:transparent}
.card .lede{margin:0 0 14px;color:var(--violet);font-size:14px}
.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:18px} @media(max-width:820px){.grid{grid-template-columns:1fr}}
.mw-row{display:flex;align-items:center;gap:14px;padding:12px 4px;border-bottom:1px solid var(--line)}
.mw-row:last-child{border-bottom:0}
.mw-rank{font-size:32px;font-weight:900;color:var(--gold);min-width:46px;font-variant-numeric:tabular-nums}
.mw-main{flex:1;min-width:0}
.mw-main a{display:block;color:#fff;font-weight:700;font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mw-sub{display:block;color:var(--violet);font-size:13px;margin-top:2px}
.mw-heat{font-size:20px;font-weight:900;color:#ff7ab8;white-space:nowrap}
.heatbar{height:6px;background:#2a1745;border-radius:3px;overflow:hidden;margin-top:5px}
.heatbar i{display:block;height:100%;background:linear-gradient(90deg,#ff2e88,#ffb03d,#ffd166)}
.stream-row{display:flex;align-items:center;gap:14px;padding:14px 4px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.stream-row:last-child{border-bottom:0}
.stream-main{flex:1;min-width:220px}
.stream-name{display:block;font-size:21px;font-weight:900;letter-spacing:1px;text-transform:uppercase;color:#fff}
.stream-sub{display:block;color:var(--violet);font-size:13px;margin-top:2px}
.plat{font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:1px;padding:2px 8px;border-radius:999px}
.plat.twitch{background:#6441a5;color:#fff} .plat.youtube{background:#c00;color:#fff}
.stars{color:var(--gold);letter-spacing:3px;font-size:16px}
.hype-side{text-align:right;min-width:110px}
.hype-num{font-size:22px;font-weight:900;color:#fff}
.hype-label{display:block;font-size:11px;color:var(--violet);text-transform:uppercase;letter-spacing:1px}
.hypebar{height:8px;background:#2a1745;border-radius:4px;overflow:hidden;margin-top:4px}
.hypebar i{display:block;height:100%;background:linear-gradient(90deg,#ff2e88,#ffb03d,#ffd166)}
.watch-link{font-weight:700;white-space:nowrap}
.pick-name{font-size:32px;font-weight:900;text-transform:uppercase;letter-spacing:1px;margin:4px 0}
.pick-meta{color:var(--violet);font-size:14px}
.rec{display:inline-block;background:#dc2626;color:#fff;font-weight:800;font-size:12px;letter-spacing:2px;padding:3px 10px;border-radius:6px}
.garage-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.clip-card{background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:14px;transition:transform .15s ease,border-color .15s ease}
.clip-card:hover{transform:translateY(-2px);border-color:#7b2ff7}
.clip-top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.clip-card p{margin:6px 0 8px;color:var(--violet);font-size:13px}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap}
.safe{background:#14532d;color:#bbf7d0} .review{background:#713f12;color:#fde68a}
.block{background:#7f1d1d;color:#fecaca} .unknown{background:#374151;color:#e5e7eb}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:#0a0514;border:1px solid #4a1e6e;border-radius:6px;padding:3px 8px;font-size:11px;font-weight:700;letter-spacing:.5px;color:#ffe9f5}
.dim{color:var(--dim)}
.log{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#d8b4fe;white-space:pre-wrap;word-break:break-word}
.status-row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
.status-pill{background:#2a1745;border:1px solid #7b2ff7;border-radius:999px;padding:5px 13px;font-size:13px}
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:6px}
@media(max-width:760px){.steps{grid-template-columns:repeat(2,1fr)}}
.step{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px;text-align:center}
.step b{display:block;font-size:24px;color:var(--gold)}
.step span{font-size:12px;letter-spacing:1px;text-transform:uppercase;color:var(--violet)}
.step.done{border-color:#14532d} .step.done b{color:#4ade80}
.fmt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.fmt{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px;text-align:center}
.fmt b{display:block;font-size:20px}
.fmt span{color:var(--violet);font-size:13px}
.howto{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(max-width:760px){.howto{grid-template-columns:1fr}}
.howto div{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px}
.howto b{color:var(--gold)}
footer{color:var(--dim);font-size:13px;text-align:center;padding:12px 20px 34px}
/* --- premium layer: motion, glass, depth --- */
@keyframes pulseDot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.8)}}
@keyframes shimmer{0%{background-position:0% 0}100%{background-position:200% 0}}
@keyframes sunGlow{0%,100%{box-shadow:0 0 90px rgba(255,94,58,.55)}50%{box-shadow:0 0 150px rgba(255,46,136,.75)}}
@keyframes gradShift{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
.hero .sun{animation:sunGlow 5s ease-in-out infinite}
.hero h1{background-size:200% auto;animation:gradShift 7s ease infinite}
.pill.live::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:#fff;margin-right:6px;animation:pulseDot 1.4s ease-in-out infinite}
.card{transition:border-color .2s ease,box-shadow .2s ease}
.card:hover{border-color:#6b3aa8;box-shadow:0 14px 44px rgba(123,47,247,.22)}
.stat{position:relative;overflow:hidden}
.stat::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#ff2e88,#ffb03d,#ffd166)}
.stat:hover{transform:translateY(-2px)}
.stat{transition:transform .18s ease}
.mw-row{border-radius:12px;transition:background .15s ease}
.mw-row:hover{background:rgba(123,47,247,.14)}
.mw-rank{background:linear-gradient(180deg,rgba(255,209,102,.16),transparent);border-radius:12px;display:flex;align-items:center;justify-content:center;height:52px}
.stream-row{border-radius:12px;transition:background .15s ease}
.stream-row:hover{background:rgba(123,47,247,.12)}
.hypebar i,.heatbar i{background-size:200% 100%;animation:shimmer 2.6s linear infinite}
.secnum{display:flex;align-items:center;gap:10px}
.secnum::after{content:"";flex:1;height:1px;background:linear-gradient(90deg,#7b2ff7,transparent)}
/* gallery garage */
.clip-poster{border-radius:12px;padding:14px 12px 12px;min-height:118px;display:flex;flex-direction:column;justify-content:space-between;gap:8px;
background:linear-gradient(135deg,#3b0a4e,#7a1e63 55%,#ff5e3a);position:relative;overflow:hidden}
.clip-poster::after{content:"";position:absolute;right:-30px;top:-30px;width:110px;height:110px;border-radius:50%;
background:radial-gradient(circle,rgba(255,209,102,.55),transparent 70%)}
.clip-poster.review{background:linear-gradient(135deg,#3a2a05,#713f12 60%,#b45309)}
.clip-poster.block{background:linear-gradient(135deg,#2a0505,#7f1d1d 60%,#450a0a)}
.poster-moment{font-size:19px;font-weight:900;text-transform:uppercase;letter-spacing:.5px;line-height:1.15;color:#fff;text-shadow:0 2px 12px rgba(0,0,0,.45)}
.poster-dur{align-self:flex-start;background:rgba(0,0,0,.45);border-radius:999px;padding:2px 9px;font-size:11px;font-weight:800;letter-spacing:1px}
.clip-body{padding:10px 2px 0}
.pick-name{text-shadow:0 0 30px rgba(255,46,136,.35)}


/* --- rich stream previews: real thumbnails, not text rows --- */
.stream-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.stream-card{background:var(--panel2);border:1px solid var(--line);border-radius:16px;overflow:hidden;transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
.stream-card:hover{transform:translateY(-3px);border-color:#7b2ff7;box-shadow:0 16px 40px rgba(123,47,247,.28)}
.thumb{position:relative;display:block;aspect-ratio:16/9;background:#0a0514;overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .3s ease}
.stream-card:hover .thumb img,.pick-thumb:hover img{transform:scale(1.05)}
.thumb-fallback{display:flex;align-items:center;justify-content:center;height:100%;min-height:120px;font-weight:900;letter-spacing:2px;color:#ff7ad9;background:linear-gradient(135deg,#3b0a4e,#7a1e63 55%,#ff5e3a)}
.live-badge{position:absolute;top:10px;left:10px;background:#dc2626;color:#fff;font-weight:800;font-size:11px;letter-spacing:1.5px;padding:3px 9px;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.5)}
.viewers-badge{position:absolute;bottom:10px;left:10px;background:rgba(0,0,0,.65);color:#fff;font-weight:700;font-size:12px;padding:3px 9px;border-radius:999px}
.stream-body{padding:14px 16px 16px}
.stream-title{display:block;color:#e9d5ff;font-size:14px;line-height:1.35;margin:4px 0 8px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:38px}
.pick-thumb{border-radius:14px;margin-bottom:12px}
.pick-thumb img{border-radius:14px}
.mw-thumb{flex:0 0 160px;display:block;border-radius:10px;overflow:hidden;aspect-ratio:16/9;background:#0a0514}
.mw-thumb img{width:100%;height:100%;object-fit:cover;display:block}
@media(max-width:640px){.mw-thumb{flex-basis:110px}}

"""

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>Vice City Vibes — Clipper Dashboard</title>
<style>{css}</style></head><body>
<div class="hero">
<div class="topbar"><span class="brand">🌴 VICE CITY <b>VIBES</b> — CLIPPER DASHBOARD</span>
<span><span class="pill live">● Live now</span> <span class="pill onair">On Air</span></span></div>
<div class="hero-inner">
<p class="script">your clips, live from Vice City</p>
<h1>Vice City Vibes</h1>
<p class="tag">One live stream. <b>50 moments.</b> Zero editing.</p>
<p class="sub">The personal view of the GTA 6 clipper — it watches the top live streams, catches the moment chat goes crazy, and parks the clip in the Garage. Personal use only · {PUBLIC_URL} · updated {_esc(state["generated_at"])}</p>
</div>
<div class="sun"></div>
</div>
<main>
<div class="stats">
<div class="stat"><b>{live_total}</b><span>Live now</span></div>
<div class="stat"><b>{state["garage"]["count"]}</b><span>Clips parked</span></div>
<div class="stat"><b>{top_hype}%</b><span>Top pick hype</span></div>
<div class="stat"><b>15 min</b><span>Watcher cycle</span></div>
</div>

<div class="howto">
<div><b>01 · Watch</b><p class="lede">The watcher scans Twitch + YouTube every 15 min for the busiest GTA 6 streams.</p></div>
<div><b>02 · Clip</b><p class="lede">Good moments are cut full-fill 9:16, with captions and a hook — CPU now, GPU when it is live.</p></div>
<div><b>03 · Post</b><p class="lede">Only good clips, only with Ajay's approval — 1 per hour, YouTube capped at 10/day.</p></div>
</div>
<br>

<div class="grid">
<div class="card"><span class="secnum">03 · Auto-pilot</span><h2>🤖 Auto-pilot <span class="grad">top pick</span></h2>
<p class="lede">The scout picks the busiest stream — the engine watches it. <span class="rec">● REC</span></p>
{_esc(top.get("channel")) and f'<a class="thumb pick-thumb" href="{_esc(top.get("url"))}" target="_blank" rel="noopener">{_thumb_img(top)}<span class="live-badge">● LIVE</span><span class="viewers-badge">{top.get("viewers") or ""} watching</span></a><div class="pick-name">{_esc(top.get("channel"))}</div><span class="stars">{stars(top_hype)}</span> <b>{top_hype}% hype</b><p class="pick-meta">{_esc(top.get("platform"))} · {top.get("viewers") or ""} viewers</p><p>{_esc(top.get("title"))}</p><p><a href="{_esc(top.get("url"))}" target="_blank" rel="noopener">Watch the stream ↗</a></p>' or '<p class="dim">No live pick right now.</p>'}
<p class="pick-meta">Checked: {_esc(s.get("checked_at"))} · {s["youtube_live"]} YouTube live · {s["twitch_live"]} Twitch live</p></div>
<div class="card"><span class="secnum">Status</span><h2>⚡ <span class="grad">Status</span></h2>
<p class="lede">Fly watcher + GPU cutter, at a glance.</p>
<div class="status-row">
<span class="status-pill">Fly: {_esc(state["fly"]["state"])}</span>
<span class="status-pill">Watcher: every 15 min</span>
<span class="status-pill">Modal cutter: {_esc(modal["state"])}</span>
</div>
<p class="pick-meta">Fly machine: {_esc(state["fly"]["machine"])} · region {_esc(state["fly"]["region"])}</p>
<p class="pick-meta">Modal: {_esc(modal.get("url") or modal.get("note") or "")}</p>
<p class="pick-meta">Last watcher line: {_esc(state["watcher"]["last_log_line"])}</p></div>
</div>

<div class="card"><span class="secnum">01 · Most Wanted</span><h2>🔥 Most <span class="grad">Wanted</span></h2>
<p class="lede">Scanning Twitch and YouTube for the busiest chats — ranked by heat.</p>
{wanted_rows}</div>

<div class="card"><span class="secnum">02 · Stream Dashboard</span><h2>📺 Stream <span class="grad">Dashboard</span></h2>
<p class="lede">Top GTA 6 live streams from Twitch and YouTube in one place — ranked by viewers and hype.</p>
<div class="stream-grid">{stream_cards}</div>
<p class="dim">Scouting only — clipping needs own footage or a live paid campaign that allows that creator (Rule 2/11).</p></div>

<div class="card"><span class="secnum">05 · Cutting Room</span><h2>✂️ Cutting <span class="grad">Room</span></h2>
<p class="lede">Every clip is cut, reframed and captioned here — watch it get edited.</p>
{cutting_html}</div>

<div class="card"><span class="secnum">06 · Video Garage</span><h2>🎬 Video <span class="grad">Garage</span> — {_esc(state["garage"]["count"])} clips</h2>
<p class="lede">All your clips land in one garage — sorted by streamer, platform and moment. Preview the moment, check its safety verdict.</p>
<div class="garage-grid">{clip_cards}</div>
<p class="dim">Blocked clips are never approved or posted. Safety: SAFE / REVIEW / BLOCK.</p></div>

<div class="card"><span class="secnum">Export</span><h2>📤 Every clip, <span class="grad">2 formats</span></h2>
<p class="lede">Ready to post on YouTube, Instagram and Facebook — vertical first, widescreen when needed.</p>
<div class="fmt-grid">
<div class="fmt"><b>9:16</b><span>YouTube Shorts</span></div>
<div class="fmt"><b>9:16</b><span>Instagram Reels</span></div>
<div class="fmt"><b>9:16</b><span>Facebook Reels</span></div>
<div class="fmt"><b>16:9</b><span>YouTube / X landscape</span></div>
</div></div>

<div class="card"><span class="secnum">Watcher log</span><h2>📋 Watcher <span class="grad">log</span></h2>
<div class="log">{log_html}</div></div>
</main>
<footer>Read-only personal dashboard — no recording, cutting or posting from here.
Posting still needs Ajay's approval of the exact clip (Vice City Vibes Rule 1).
No login yet — keep this URL private.</footer>
</body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # No caching: the dashboard must always show the latest release.
        # (Ajay kept seeing the old dashboard because browsers cached it.)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # Strip any query string (e.g. /?v=2 cache-buster) before matching.
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                self._send(200, render(collect_state()), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, f"Dashboard error: {html.escape(str(e))}",
                           "text/plain; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(collect_state(), ensure_ascii=False, indent=1),
                       "application/json")
        elif path == "/api/health":
            self._send(200, json.dumps({"status": "ok", "app": APP_NAME}),
                       "application/json")
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8080")))
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"web dashboard: http://{a.host}:{a.port}/ (data: /data, fallback {ROOT})",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
