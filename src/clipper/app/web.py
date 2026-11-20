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
    rows = ""
    for r in s["ranked"]:
        rows += (f'<tr><td>{_esc(r.get("platform"))}</td>'
                 f'<td><a href="{_esc(r.get("url"))}" target="_blank" rel="noopener">'
                 f'{_esc(r.get("channel"))}</a></td>'
                 f'<td>{_esc(r.get("title"))}</td>'
                 f'<td class="num">{_esc(r.get("viewers"))}</td>'
                 f'<td><div class="hype"><i style="width:{r["hype"]}%"></i></div>'
                 f'{r["hype"]}</td></tr>')
    top = state["top_pick"] or {}
    wanted = "".join(
        f'<li><a href="{_esc(w.get("url"))}" target="_blank" rel="noopener">'
        f'{_esc(w.get("title"))}</a> — {_esc(w.get("channel"))} · '
        f'{_esc(w.get("views_per_hour"))} views/h</li>'
        for w in state["most_wanted"]) or "<li>No trend data yet — watcher refreshes every 15 min.</li>"
    clips = ""
    for c in state["garage"]["clips"]:
        badge = {"SAFE": "safe", "REVIEW": "review", "BLOCK": "block"}.get(
            c.get("safety"), "unknown")
        stars = "★" * int(c.get("stars") or 0)
        clips += (f'<div class="clip"><b>{_esc(c.get("moment") or c.get("source_title") or "Clip")}</b>'
                  f'<span class="badge {badge}">{_esc(c.get("safety"))}</span>'
                  f'<p>{stars} · {_esc(c.get("duration_s"))}s · {_esc(c.get("status"))} · '
                  f'{_esc(c.get("stream"))}</p></div>')
    log = "<br>".join(_esc(l) for l in state["watcher"]["log_tail"]) or "No watcher log yet."
    modal = state["modal"]
    modal_cls = {"ok": "safe", "up": "safe"}.get(modal["state"], "review")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vice City Vibes — Clipper Dashboard</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:#12081f;color:#f5eefc}}
header{{background:linear-gradient(135deg,#ff2e88 0%,#7b2ff7 45%,#ff9a3d 100%);padding:28px 20px 20px}}
header h1{{margin:0;font-size:32px;letter-spacing:1px;text-shadow:0 2px 12px rgba(0,0,0,.5)}}
header p{{margin:6px 0 0;opacity:.92}}
main{{max-width:1080px;margin:0 auto;padding:20px}}
.card{{background:#1e1030;border:1px solid #3d2358;border-radius:14px;padding:16px 18px;margin-bottom:16px}}
.card h2{{margin:0 0 10px;color:#ff7ab8;font-size:18px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} @media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
table{{width:100%;border-collapse:collapse;font-size:14px}} td,th{{padding:7px 6px;border-bottom:1px solid #3d2358;text-align:left;vertical-align:top}}
th{{color:#c9a7e8}} .num{{text-align:right}} a{{color:#7dd3fc}}
.hype{{height:8px;background:#3d2358;border-radius:4px;overflow:hidden;margin-bottom:2px}}
.hype i{{display:block;height:100%;background:linear-gradient(90deg,#ff2e88,#ffb03d)}}
.badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;margin-left:8px}}
.safe{{background:#14532d;color:#bbf7d0}} .review{{background:#713f12;color:#fde68a}}
.block{{background:#7f1d1d;color:#fecaca}} .unknown{{background:#374151;color:#e5e7eb}}
.clip{{border-top:1px solid #3d2358;padding:10px 0}} .clip p{{margin:4px 0 0;color:#c9a7e8;font-size:13px}}
.pill{{display:inline-block;background:#2a1745;border:1px solid #7b2ff7;border-radius:999px;padding:4px 12px;margin:2px 6px 2px 0;font-size:13px}}
.log{{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#d8b4fe;white-space:pre-wrap;word-break:break-word}}
footer{{color:#9d7fc4;font-size:13px;text-align:center;padding:10px 20px 30px}}
</style></head><body>
<header><h1>🌴 VICE CITY VIBES — Clipper Dashboard</h1>
<p>Personal view of the GTA 6 clipper · {PUBLIC_URL} · generated {_esc(state["generated_at"])}</p></header>
<main>
<div class="grid">
<div class="card"><h2>🤖 Auto-pilot top pick</h2>
{_esc(top.get("channel")) + " · " + _esc(top.get("platform")) + " · " + str(top.get("viewers") or "") + " viewers" if top else "No live pick right now."}
<p>{_esc(top.get("title"))}</p>
{f'<p><a href="{_esc(top.get("url"))}" target="_blank" rel="noopener">Watch the stream ↗</a></p>' if top.get("url") else ""}
<p>Streams checked: {_esc(s.get("checked_at"))} · {s["youtube_live"]} YouTube live · {s["twitch_live"]} Twitch live</p></div>
<div class="card"><h2>⚡ Status</h2>
<span class="pill">Fly: {_esc(state["fly"]["state"])}</span>
<span class="pill">Watcher: every 15 min</span>
<span class="pill badge {modal_cls}">Modal cutter: {_esc(modal["state"])}</span>
<p>Fly machine: {_esc(state["fly"]["machine"])} · region {_esc(state["fly"]["region"])}</p>
<p>Modal: {_esc(modal.get("url") or modal.get("note") or "")}</p>
<p>Last watcher line: {_esc(state["watcher"]["last_log_line"])}</p></div>
</div>
<div class="card"><h2>🔥 Live GTA 6 streams now</h2>
<table><tr><th>Platform</th><th>Channel</th><th>Title</th><th>Viewers</th><th>Hype</th></tr>
{rows or '<tr><td colspan="5">No live streams in the last watcher run.</td></tr>'}</table>
<p>Scouting only — clipping needs own footage or a live paid campaign that allows that creator (Rule 2/11).</p></div>
<div class="card"><h2>🎯 Most Wanted — trending GTA 6 videos</h2><ol>{wanted}</ol></div>
<div class="card"><h2>🎬 Video Garage — {_esc(state["garage"]["count"])} clips</h2>
{clips or "<p>No clips registered yet.</p>"}
<p>Blocked clips are never approved or posted. Safety: SAFE / REVIEW / BLOCK.</p></div>
<div class="card"><h2>📋 Watcher log (last lines)</h2><div class="log">{log}</div></div>
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
