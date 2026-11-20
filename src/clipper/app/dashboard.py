#!/usr/bin/env python3
"""Stream Dashboard — list top live GTA 6 streams right now.

Reuses gtavi-claudecode scripts/streams.py (YouTube live search via
yt-dlp + Twitch via the keyless public GQL in scripts/twitch.py) and
scripts/trends.py (recent GTA 6 videos ranked by views/hour), with the
SSL workaround in ssl_fix.py applied first.

Usage (run from clipper-engine/ with the gtavi venv):
    python -m clipper.app.dashboard [--query "gta 6"] [--limit 20]

Writes:
    dashboard/streams.json  — ranked live streams, YouTube + Twitch
    dashboard/streams.md    — same, human-readable
    dashboard/trends.md     — recent videos ranked by views/hour
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from clipper.infra.ssl_fix import patch_module, patch_yt_dlp  # noqa: E402
from clipper.paths import repo_root

# Repo root: dashboard/ outputs live here.
HERE = repo_root()

common = patch_yt_dlp()
import streams  # noqa: E402
import trends  # noqa: E402

patch_module(streams)
patch_module(trends)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="gta 6")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args()

    out_dir = HERE / "dashboard"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")

    # --- Live streams (streams.py) ---
    yt = streams.youtube(a.query, a.limit)
    tw = streams.twitch(a.query)
    tw = sorted(tw, key=lambda r: r["viewers"] or -1, reverse=True)[: a.limit] if tw else []
    yt = sorted(yt, key=lambda r: r["viewers"] or -1, reverse=True)
    all_rows = sorted(yt + tw, key=lambda r: r["viewers"] or -1, reverse=True)

    data = {
        "checked_at": stamp,
        "query": a.query,
        "youtube_live": yt,
        "twitch_live": tw,
        "ranked_all": all_rows,
        "note": "Viewer counts are exactly what each platform returned at run time. "
                "These streams are for SCOUTING ONLY — do not clip or post them "
                "unless a live paid campaign explicitly allows that creator.",
    }
    (out_dir / "streams.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    md = [f"# Live GTA 6 streams — {stamp}", "", f"Query: `{a.query}`", ""]
    for name, rows in (("YouTube", yt), ("Twitch", tw)):
        md += [f"## {name}", ""]
        if not rows:
            md += ["No live streams found.", ""]
            continue
        md += ["| # | Channel | Title | Viewers | Live for | URL |", "|---|---|---|---|---|---|"]
        for n, r in enumerate(rows, 1):
            v = "" if r["viewers"] is None else r["viewers"]
            md.append(f"| {n} | {r['channel']} | {r['title'].replace('|', '/')} | {v} | {r['live_for']} | {r['url']} |")
        md.append("")
    md += ["---", "Scouting only. Clipping is for OUR OWN footage or a live paid campaign "
           "that explicitly allows that creator (Vice City Vibes Rule 2/11)."]
    (out_dir / "streams.md").write_text("\n".join(md), encoding="utf-8")

    # --- Trend radar (trends.py) ---
    sys.argv = ["trends.py", "--query", a.query, "--hours", str(a.hours),
                "--limit", "30", "--out", str(out_dir / "trends.md")]
    try:
        trends.main()
    except SystemExit as e:
        print(f"trends.py exited {e.code} — streams data above is still valid", file=sys.stderr)

    print(f"dashboard: {len(yt)} YouTube live, {len(tw)} Twitch live, "
          f"{len(all_rows)} total -> {out_dir}/streams.json + streams.md + trends.md")
    for r in all_rows[:10]:
        print(f"  {r['platform']:8} {str(r['viewers'] or ''):>7}  {r['channel'][:28]:28} {r['title'][:60]}")


if __name__ == "__main__":
    main()
