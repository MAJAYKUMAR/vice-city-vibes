#!/usr/bin/env python3
"""Clipper Engine watcher — runs on Fly.io, scouts only, never posts.

Loop (every 15 minutes):
  1. dashboard.py — top live GTA 6 streams (YouTube + Twitch) + trends
  2. autopilot.py — picks the top ranked stream and prints the scout
     command a human runs after confirming ownership / campaign
  3. Copy dashboard/streams.json, streams.md, trends.md to /data
     (the Fly volume, so scouting state survives restarts)

Hard rule (Vice City Vibes Rule 2/11): this watcher NEVER records a
stream, NEVER cuts a clip, and NEVER posts anything on its own.
Recording/clipping is only for OUR OWN footage or a live paid campaign
that explicitly allows that creator, and a human confirms that first.

Usage:
    python -m clipper.app.watcher            # loop forever (Fly CMD)
    python -m clipper.app.watcher --once     # one cycle, then exit (local test)
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from clipper.paths import repo_root

HERE = repo_root()
DATA = Path("/data")
INTERVAL_SECONDS = 15 * 60


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} watcher: {msg}"
    print(line, flush=True)
    try:
        if DATA.is_dir():
            with open(DATA / "watcher.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def run_cycle():
    log("cycle start")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HERE / "src") + os.pathsep + env.get("PYTHONPATH", "")
    for script in ("dashboard", "autopilot"):
        try:
            r = subprocess.run(
                [sys.executable, "-m", f"clipper.app.{script}"],
                cwd=HERE, env=env, capture_output=True, text=True, timeout=600,
            )
            out = (r.stdout or "") + (r.stderr or "")
            log(f"{script} exit={r.returncode} :: " + " | ".join(
                l for l in out.splitlines() if l.strip())[:1500])
        except Exception as e:  # keep the loop alive
            log(f"{script} FAILED: {e}")
    # Optional Modal eligibility check (opt-in only, default OFF):
    # if CLIPPER_MODAL_CHECK=1, ask modal_client.py whether the top
    # autopilot pick is even cuttable (own footage / live campaign).
    # This NEVER cuts — a cut needs a human-confirmed moment window
    # (start/end), which scouting does not have. Actual cuts go
    # through `modal_client.py from-autopilot --start S --end E`.
    if os.environ.get("CLIPPER_MODAL_CHECK", "0") == "1":
        try:
            import json as _json
            streams = _json.loads(
                (HERE / "dashboard" / "streams.json").read_text(encoding="utf-8"))
            top = (streams.get("ranked_all") or [{}])[0]
            r = subprocess.run(
                [sys.executable, "-m", "clipper.infra.modal_client",
                 "check-allowed", "--source", top.get("url", ""),
                 "--channel", top.get("channel", "")],
                cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
            log("modal eligibility for top pick "
                f"({top.get('channel', '?')}): "
                + " ".join((r.stdout or "").splitlines())[:300])
        except Exception as e:
            log(f"modal eligibility check FAILED: {e}")
    try:
        if DATA.is_dir():
            for name in ("streams.json", "streams.md", "trends.md"):
                src = HERE / "dashboard" / name
                if src.exists():
                    shutil.copy2(src, DATA / name)
            log("dashboard outputs copied to /data")
    except OSError as e:
        log(f"copy to /data failed: {e}")
    log("cycle done — scouting only, nothing recorded, clipped or posted")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=INTERVAL_SECONDS)
    a = ap.parse_args()
    log(f"starting (interval={a.interval}s, once={a.once})")
    while True:
        run_cycle()
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
