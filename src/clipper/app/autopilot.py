#!/usr/bin/env python3
"""Auto-pilot scout — pick the best stream by itself, then scout it.

Reads dashboard/streams.json (run dashboard.py first), picks the top
ranked live stream that is not hidden in watchlist.json (pinned links
first), and prints the scout command to run on it. It does NOT record
or clip a stream on its own: recording someone else's live stream is
only done for OUR OWN footage or under a live paid campaign that
explicitly allows that creator — a human confirms that before any
recording starts (Vice City Vibes Rule 2/11).

Usage:
    python -m clipper.app.dashboard
    python -m clipper.app.autopilot
"""
import json
import sys
from pathlib import Path

from clipper.paths import config_dir, repo_root

HERE = repo_root()


def main():
    streams_file = HERE / "dashboard/streams.json"
    if not streams_file.exists():
        print("No dashboard/streams.json — run dashboard.py first.")
        sys.exit(1)
    data = json.loads(streams_file.read_text(encoding="utf-8"))
    wl = json.loads((config_dir() / "watchlist.json").read_text(encoding="utf-8"))
    hidden = set(wl.get("hidden", []))
    pinned = wl.get("pinned", []) + wl.get("links", [])

    ranked = [r for r in data["ranked_all"] if r["url"] not in hidden and r["channel"] not in hidden]
    if pinned:
        print("📌 Pinned / your links (scouted first):")
        for u in pinned:
            print(f"   {u}")
    if not ranked:
        print("No live streams to pick from right now.")
        return
    top = ranked[0]
    print(f"🤖 Auto-pilot pick: {top['channel']} ({top['platform']}) — "
          f"{top['viewers']} viewers — {top['title']}")
    print(f"   {top['url']}")
    print("   Next step (human confirms ownership/campaign first):")
    print(f"   python -m clipper.core.clip scout \"{top['url']}\" --top 8")
    print("   On deck:")
    for r in ranked[1:4]:
        print(f"   - {r['channel']} ({r['platform']}) {r['viewers']} viewers")


if __name__ == "__main__":
    main()
