#!/usr/bin/env python3
"""Vice City Vibes — personal web dashboard (read-only).

Stdlib-only HTTP server. Serves one page at / and a JSON snapshot at
/api/state, built from the watcher's state — no new dependencies.

Data sources (first hit wins):
  * /data/streams.json, /data/trends.md, /data/clips.json,
    /data/watcher.log   — the Fly volume, written by the watcher
  * <repo>/dashboard/streams.json, trends.md, garage/clips.json
    — local fallback (what `python -m clipper.app.dashboard` writes)

Sections: live GTA 6 streams now, auto-pilot top pick, Most Wanted
(trends by views/hour), Hype Meter per stream, Video Garage clips with
safety verdicts, watcher status / last run, Modal cutter status, and
Fly machine status.

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
        "fly": {"region": "dfw", "machine": "shared-cpu-1x @ 512MB",
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
            f'<div class="mw-main"><a href="{_esc(w.get("url"))}" target="_blank" rel="noopener">'
            f'{_esc(w.get("title"))}</a>'
            f'<span class="mw-sub">{_esc(w.get("channel"))} · {_esc(w.get("views_per_hour"))} views/h</span></div>'
            f'<span class="mw-heat">{_esc(w.get("views_per_hour"))}</span></div>')
    if not wanted_rows:
        wanted_rows = '<p class="dim">No trend data yet — watcher refreshes every 15 min.</p>'

    # ---- Live streams: ranked cards with stars + hype bar ----
    stream_cards = ""
    for i, r in enumerate(ranked, 1):
        plat = (_esc(r.get("platform")) or "").lower()
        plat_cls = "twitch" if "twitch" in plat else "youtube"
        stream_cards += (
            f'<div class="stream-row"><span class="mw-rank">{i:02d}</span>'
            f'<div class="stream-main"><span class="stream-name">{_esc(r.get("channel"))}</span>'
            f'<span class="stream-sub"><span class="plat {plat_cls}">{_esc(r.get("platform"))}</span>'
            f' · {_esc(r.get("viewers"))} watching · {_esc(r.get("title"))}</span>'
            f'<span class="stars">{stars(r["hype"])}</span></div>'
            f'<div class="hype-side"><span class="hype-num">{r["hype"]}%</span>'
            f'<span class="hype-label">hype</span>'
            f'<div class="hypebar"><i style="width:{r["hype"]}%"></i></div></div>'
            f'<a class="watch-link" href="{_esc(r.get("url"))}" target="_blank" rel="noopener">Watch ↗</a></div>')
    if not stream_cards:
        stream_cards = '<p class="dim">No live streams in the last watcher run.</p>'

    # ---- Garage clip cards ----
    clip_cards = ""
    for c in clips:
        badge = {"SAFE": "safe", "REVIEW": "review", "BLOCK": "block"}.get(
            c.get("safety"), "unknown")
        st = stars(int((c.get("stars") or 0) * 20))
        clip_cards += (
            f'<div class="clip-card"><div class="clip-top"><b>{_esc(c.get("moment") or c.get("source_title") or "Clip")}</b>'
            f'<span class="badge {badge}">{_esc(c.get("safety"))}</span></div>'
            f'<span class="stars">{st}</span>'
            f'<p>{_esc(c.get("duration_s"))}s · {_esc(c.get("status"))} · {_esc(c.get("stream"))}</p>'
            f'<div class="chips"><span class="chip">✂ Cut</span><span class="chip">CC Captions</span>'
            f'<span class="chip">9:16 Vertical</span></div></div>')
    if not clip_cards:
        clip_cards = "<p class=\"dim\">No clips registered yet.</p>"

    modal_cls = {"ok": "safe", "up": "safe"}.get(modal["state"], "review")
    log_html = "<br>".join(_esc(l) for l in log) or "No watcher log yet."
    top_hype = _hype(top.get("viewers")) if top else 0
    live_total = s["youtube_live"] + s["twitch_live"]

    css = """
*{box-sizing:border-box}
body{margin:0;font-family:'Arial Narrow',Impact,'Segoe UI',system-ui,sans-serif;background:#0a0514;color:#f5eefc}
a{color:#7dd3fc;text-decoration:none} a:hover{text-decoration:underline}
/* --- hero: synthwave sunset, pure CSS --- */
.hero{position:relative;overflow:hidden;background:linear-gradient(180deg,#12041f 0%,#3b0a4e 45%,#7a1e63 70%,#ff5e3a 100%);padding:34px 20px 0;text-align:center}
.hero .sun{width:340px;height:170px;margin:0 auto -90px;border-radius:170px 170px 0 0;
background:repeating-linear-gradient(180deg,#ffd166 0 14px,transparent 14px 22px,#ff9a3d 22px 36px,transparent 36px 44px,#ff2e88 44px 60px,transparent 60px 68px);
opacity:.95;box-shadow:0 0 90px rgba(255,94,58,.55)}
.hero-inner{position:relative;z-index:2}
.script{font-family:'Segoe Script','Brush Script MT',cursive;font-size:26px;color:#ff7ad9;text-shadow:0 0 18px #ff2e88;margin:0}
.hero h1{margin:2px 0 0;font-size:56px;line-height:.95;letter-spacing:2px;font-weight:900;text-transform:uppercase;
background:linear-gradient(90deg,#fff 10%,#ffd166 40%,#ff5e3a 65%,#ff2e88 90%);-webkit-background-clip:text;background-clip:text;color:transparent;
text-shadow:0 4px 24px rgba(0,0,0,.35)}
.hero .sub{margin:10px 0 18px;color:#ffe9f5;font-size:16px;letter-spacing:.5px}
.topbar{display:flex;justify-content:space-between;align-items:center;max-width:1180px;margin:0 auto 14px;padding:0 4px;position:relative;z-index:2}
.brand{font-weight:900;letter-spacing:3px;font-size:15px;color:#fff}
.brand b{color:#ffd166}
.pill{display:inline-block;padding:5px 12px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase}
.pill.live{background:#ff2e88;color:#fff;box-shadow:0 0 14px rgba(255,46,136,.7)}
.pill.onair{background:#1a0b2e;border:1px solid #ff9a3d;color:#ffd166}
main{max-width:1180px;margin:0 auto;padding:22px 18px 10px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:-58px 0 18px;position:relative;z-index:3}
@media(max-width:760px){.stats{grid-template-columns:repeat(2,1fr)}.hero h1{font-size:38px}}
.stat{background:#170b28;border:1px solid #4a1e6e;border-radius:14px;padding:16px;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.45)}
.stat b{display:block;font-size:34px;color:#ffd166;line-height:1}
.stat span{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#c9a7e8}
.card{background:#150a26;border:1px solid #3d2358;border-radius:16px;padding:18px 20px;margin-bottom:18px;box-shadow:0 8px 30px rgba(0,0,0,.35)}
.card h2{margin:0 0 4px;font-size:30px;letter-spacing:1px;text-transform:uppercase;font-weight:900}
.card h2 .grad{background:linear-gradient(90deg,#ffd166,#ff5e3a,#ff2e88);-webkit-background-clip:text;background-clip:text;color:transparent}
.card .lede{margin:0 0 14px;color:#c9a7e8;font-size:14px}
.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:18px} @media(max-width:820px){.grid{grid-template-columns:1fr}}
/* Most Wanted rows */
.mw-row{display:flex;align-items:center;gap:14px;padding:12px 4px;border-bottom:1px solid #3d2358}
.mw-rank{font-size:30px;font-weight:900;color:#ffd166;min-width:44px}
.mw-main{flex:1;min-width:0}
.mw-main a{display:block;color:#fff;font-weight:700;font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mw-sub{display:block;color:#c9a7e8;font-size:13px;margin-top:2px}
.mw-heat{font-size:20px;font-weight:900;color:#ff7ab8}
/* stream rows */
.stream-row{display:flex;align-items:center;gap:14px;padding:14px 4px;border-bottom:1px solid #3d2358}
.stream-main{flex:1;min-width:0}
.stream-name{display:block;font-size:22px;font-weight:900;letter-spacing:1px;text-transform:uppercase;color:#fff}
.stream-sub{display:block;color:#c9a7e8;font-size:13px;margin-top:2px}
.plat{font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:1px;padding:2px 8px;border-radius:999px}
.plat.twitch{background:#6441a5;color:#fff} .plat.youtube{background:#c00;color:#fff}
.stars{color:#ffd166;letter-spacing:3px;font-size:16px}
.hype-side{text-align:right;min-width:110px}
.hype-num{font-size:22px;font-weight:900;color:#fff}
.hype-label{display:block;font-size:11px;color:#c9a7e8;text-transform:uppercase;letter-spacing:1px}
.hypebar{height:8px;background:#3d2358;border-radius:4px;overflow:hidden;margin-top:4px}
.hypebar i{display:block;height:100%;background:linear-gradient(90deg,#ff2e88,#ffb03d,#ffd166)}
.watch-link{font-weight:700;white-space:nowrap}
/* top pick */
.pick-name{font-size:34px;font-weight:900;text-transform:uppercase;letter-spacing:1px;margin:4px 0}
.pick-meta{color:#c9a7e8;font-size:14px}
/* garage */
.garage-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.clip-card{background:#1e1030;border:1px solid #3d2358;border-radius:12px;padding:14px}
.clip-top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.clip-card p{margin:6px 0 8px;color:#c9a7e8;font-size:13px}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap}
.safe{background:#14532d;color:#bbf7d0} .review{background:#713f12;color:#fde68a}
.block{background:#7f1d1d;color:#fecaca} .unknown{background:#374151;color:#e5e7eb}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:#0a0514;border:1px solid #4a1e6e;border-radius:6px;padding:3px 8px;font-size:11px;font-weight:700;letter-spacing:.5px;color:#ffe9f5}
.dim{color:#9d7fc4}
.log{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#d8b4fe;white-space:pre-wrap;word-break:break-word}
.status-row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
.status-pill{background:#2a1745;border:1px solid #7b2ff7;border-radius:999px;padding:5px 13px;font-size:13px}
footer{color:#9d7fc4;font-size:13px;text-align:center;padding:12px 20px 34px}
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
<p class="sub">Personal view of the GTA 6 clipper · {PUBLIC_URL} · updated {_esc(state["generated_at"])}</p>
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

<div class="grid">
<div class="card"><h2>🤖 Auto-pilot <span class="grad">top pick</span></h2>
<p class="lede">The scout picks the busiest stream — the engine watches it.</p>
{_esc(top.get("channel")) and f'<div class="pick-name">{_esc(top.get("channel"))}</div><span class="stars">{stars(top_hype)}</span> <b>{top_hype}% hype</b><p class="pick-meta">{_esc(top.get("platform"))} · {top.get("viewers") or ""} viewers</p><p>{_esc(top.get("title"))}</p><p><a href="{_esc(top.get("url"))}" target="_blank" rel="noopener">Watch the stream ↗</a></p>' or '<p class="dim">No live pick right now.</p>'}
<p class="pick-meta">Checked: {_esc(s.get("checked_at"))} · {s["youtube_live"]} YouTube live · {s["twitch_live"]} Twitch live</p></div>
<div class="card"><h2>⚡ <span class="grad">Status</span></h2>
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

<div class="card"><h2>🔥 Most <span class="grad">Wanted</span></h2>
<p class="lede">The busiest GTA 6 streams, ranked by heat.</p>
{wanted_rows}</div>

<div class="card"><h2>📺 Stream <span class="grad">Dashboard</span></h2>
<p class="lede">Top GTA 6 live streams from Twitch and YouTube in one place — ranked by viewers and hype.</p>
{stream_cards}
<p class="dim">Scouting only — clipping needs own footage or a live paid campaign that allows that creator (Rule 2/11).</p></div>

<div class="card"><h2>🎬 Video <span class="grad">Garage</span> — {_esc(state["garage"]["count"])} clips</h2>
<p class="lede">Every clip the engine cut — sorted by moment, with its safety verdict.</p>
<div class="garage-grid">{clip_cards}</div>
<p class="dim">Blocked clips are never approved or posted. Safety: SAFE / REVIEW / BLOCK.</p></div>

<div class="card"><h2>📋 Watcher <span class="grad">log</span></h2>
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
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                self._send(200, render(collect_state()), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, f"Dashboard error: {html.escape(str(e))}",
                           "text/plain; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, json.dumps(collect_state(), ensure_ascii=False, indent=1),
                       "application/json")
        elif self.path == "/api/health":
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
