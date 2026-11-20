#!/usr/bin/env python3
"""Fly -> Modal client for the Clipper Engine (Vice City Vibes).

When watcher.py / autopilot.py picks a stream, this client asks the
Modal GPU cutter (modal_app.py, app "vice-city-vibes-cutter") to cut
one clip, downloads the finished mp4, and registers it in the Video
Garage (garage.py). It never posts anything — posting is a separate,
human-approved step.

Call flow:
    clipper.app.dashboard   -> dashboard/streams.json (ranked live)
    clipper.app.autopilot   -> top pick (channel / platform / url)
    modal_client.py         -> cut_allowed() gate (below)
                            -> POST {source, start, end, hook, title}
                               to $MODAL_CUT_URL (Modal /cut endpoint)
                            -> stream mp4 to jobs/modal-<id>/edits/
                               clip_<n>_fill.mp4 + .json sidecar
                               (safety verdict from X-Safety-Verdict)
                            -> garage.py register <clip> --stars N
    (a human then reviews the Garage entry; BLOCK clips are never
    approved — clipper.app.garage approve() refuses them.)

THE GATE (Vice City Vibes Rule 2/11 — non-negotiable):
    A cut is allowed ONLY if the source is OUR OWN footage or a live
    paid campaign that explicitly allows that creator:
      * the source URL / channel is in watchlist.json `pinned` or
        `links` (own streams), OR
      * campaigns.json lists that creator/channel with allowed=true
        and not expired.
    Everything else is REFUSED, loudly, before any request is sent.
    There is no override flag. If you need a new creator, add them
    to campaigns.json (with the campaign name + expiry) first.

Config — env vars only, never a token/secret in a file:
    MODAL_CUT_URL       required — the Modal /cut web endpoint URL
                        (printed by `modal deploy modal_app.py`).
                        Example: https://<workspace>--vice-city-vibes-
                        cutter-cut.modal.run
    MODAL_HEALTH_URL    optional — the Modal /health URL (defaults to
                        MODAL_CUT_URL with /cut replaced by /health).
    MODAL_SHARED_SECRET optional — sent as header X-Clipper-Auth;
                        must match MODAL_SHARED_SECRET on the Modal app.
    MODAL_TIMEOUT_S     optional, default 1200 (a GPU cut takes minutes).
    CLIPPER_CAMPAIGNS   optional, default campaigns.json in this folder.

Usage (from clipper-engine/, stdlib only — no extra pip packages):
    python -m clipper.infra.modal_client health
    python -m clipper.infra.modal_client check-allowed --source <url> --channel <name>
    python -m clipper.infra.modal_client cut --source <url> --start 10 --end 30 \
        --hook "WAIT FOR IT" --title "Beach fight" --stars 5 [--dry-run]
    python -m clipper.infra.modal_client from-autopilot --start 10 --end 30 \
        --hook "WAIT FOR IT" [--dry-run]
        # uses dashboard/streams.json top pick as the source; still
        # gated by cut_allowed() — refuses unless own/campaign.

Do NOT deploy from here — this file only CALLS the Modal endpoint.
Deploy the cutter with: modal deploy modal_app.py
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from clipper.paths import config_dir, repo_root

HERE = repo_root()
DEFAULT_TIMEOUT = 1200


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "clip"


def cut_allowed(source, channel="", campaigns_file=None):
    """The Rule 2/11 gate. Returns (allowed: bool, reason: str).

    Allowed only when the source/channel is in watchlist pinned/links
    (own footage) or in campaigns.json with allowed=true, unexpired.
    """
    source = (source or "").strip()
    channel = (channel or "").strip().lower()
    wl = _load_json(config_dir() / "watchlist.json", {})
    own = [str(u) for u in (wl.get("pinned", []) + wl.get("links", []))]
    if source and source in own:
        return True, "own footage (watchlist pinned/links)"
    if channel and any(channel in str(u).lower() for u in own):
        return True, "own footage (watchlist channel match)"
    camps = _load_json(campaigns_file or config_dir() / "campaigns.json",
                       {"campaigns": []})
    today = datetime.now(timezone.utc).date().isoformat()
    for c in camps.get("campaigns", []):
        if not c.get("allowed"):
            continue
        exp = str(c.get("expires", "9999-12-31"))
        if exp < today:
            continue
        c_channel = str(c.get("channel", "")).lower()
        c_url = str(c.get("url", ""))
        if (c_channel and c_channel == channel) or \
                (c_url and c_url == source) or \
                (c_channel and c_channel in source.lower()):
            return True, (f"live paid campaign '{c.get('name', '?')}' "
                          f"allows {c.get('channel', c_url)} until {exp}")
    return False, ("REFUSED — not own footage and no live paid campaign "
                   "allows this creator. Add it to campaigns.json first "
                   "(Rule 2/11).")


def pick_from_dashboard():
    """Top autopilot pick from dashboard/streams.json, or None."""
    data = _load_json(HERE / "dashboard" / "streams.json", {})
    ranked = data.get("ranked_all") or []
    if not ranked:
        return None
    top = ranked[0]
    return {"channel": top.get("channel", ""), "platform": top.get("platform", ""),
            "url": top.get("url", ""), "viewers": top.get("viewers"),
            "title": top.get("title", "")}


def _headers():
    h = {"Content-Type": "application/json"}
    secret = os.environ.get("MODAL_SHARED_SECRET", "")
    if secret:
        h["X-Clipper-Auth"] = secret
    return h


def _endpoint(name="cut"):
    url = os.environ.get("MODAL_CUT_URL", "").strip()
    if not url:
        raise SystemExit(
            "MODAL_CUT_URL is not set — deploy modal_app.py first "
            "(`modal deploy modal_app.py`) and set MODAL_CUT_URL to "
            "the printed /cut URL. No token/secret belongs in a file.")
    if name == "health":
        return os.environ.get("MODAL_HEALTH_URL") or url.rsplit("/cut", 1)[0] + "/health"
    return url


def health_check(timeout=30):
    req = urllib.request.Request(_endpoint("health"), headers=_headers(),
                                 method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    print(f"modal health: {body}")
    return body


def request_cut(source, start, end, hook="", title="", stars=0,
                timeout=None, dry_run=False, caption_style=None,
                track=None):
    """POST a cut to Modal, download the mp4, register in the Garage.
    Returns (clip_path, safety_verdict) or (None, reason) on refusal.
    caption_style (karaoke/pop/minimal, English only) and track (crop
    keyframes from the CPU path) are optional and forwarded to /cut;
    both default to None, which keeps today's behaviour."""
    start, end = float(start), float(end)
    if end - start < 3 or end - start > 60:
        return None, "clip window must be 3-60s"
    if dry_run:
        url = os.environ.get("MODAL_CUT_URL", "") or "<MODAL_CUT_URL not set yet>"
        print(f"dry-run: would POST {source} {start:.1f}-{end:.1f}s "
              f"hook={hook!r} to {url}")
        return None, "dry-run"
    timeout = int(timeout or os.environ.get("MODAL_TIMEOUT_S", DEFAULT_TIMEOUT))
    job_id = f"modal-{_slug(title or hook or 'clip')}-{int(time.time())}"
    out_dir = HERE / "jobs" / job_id / "edits"
    out_dir.mkdir(parents=True, exist_ok=True)
    clip = out_dir / "clip_1_fill.mp4"
    payload_obj = {"source": source, "start": start, "end": end,
                   "hook": hook, "title": title}
    if track is not None:
        payload_obj["track"] = track
    if caption_style is not None:
        payload_obj["caption_style"] = caption_style
    payload = json.dumps(payload_obj).encode("utf-8")
    req = urllib.request.Request(_endpoint(), data=payload,
                                 headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            verdict = r.headers.get("X-Safety-Verdict", "unknown")
            duration = r.headers.get("X-Duration-S", str(end - start))
            with open(clip, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        return None, f"Modal /cut HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}"
    except Exception as e:
        return None, f"Modal /cut failed: {e}"
    if clip.stat().st_size < 10_000:
        return None, f"Modal returned a suspiciously small file ({clip.stat().st_size} bytes)"
    meta = {"file": str(clip), "start": start, "end": end, "layout": "filltrack",
            "hook": hook, "title": title, "safety": {"verdict": verdict},
            "source_url": source, "duration_s": duration,
            "caption_style": caption_style, "track": track,
            "modal_endpoint": _endpoint().split("//", 1)[-1].split("/", 1)[0],
            "job_id": job_id}
    clip.with_suffix(".json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"modal cut: {clip} ({clip.stat().st_size // 1024} KB, "
          f"safety={verdict})")
    if verdict == "BLOCK":
        print("SAFETY BLOCK — clip saved for format proof only, "
              "NEVER approve/post (garage.py will refuse it).")
    # Register in the Garage (garage.py refuses BLOCK on approve, not here).
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HERE / "src") + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-m", "clipper.app.garage", "register",
                        str(clip), "--moment", title or hook, "--stars", str(stars)],
                       cwd=HERE, env=env, capture_output=True, text=True)
    print((r.stdout or "") + (r.stderr or ""))
    return clip, verdict


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    p = sub.add_parser("check-allowed")
    p.add_argument("--source", required=True); p.add_argument("--channel", default="")
    p = sub.add_parser("cut")
    p.add_argument("--source", required=True)
    p.add_argument("--channel", default="")
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--end", type=float, required=True)
    p.add_argument("--hook", default=""); p.add_argument("--title", default="")
    p.add_argument("--stars", type=int, default=0)
    p.add_argument("--caption-style", default=None,
                   choices=["karaoke", "pop", "minimal"])
    p.add_argument("--track", default=None,
                   help="crop keyframes JSON [[t,x],...] or @path/to/keyframes.json")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("from-autopilot")
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--end", type=float, required=True)
    p.add_argument("--hook", default=""); p.add_argument("--title", default="")
    p.add_argument("--stars", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "health":
        health_check()
    elif a.cmd == "check-allowed":
        ok, reason = cut_allowed(a.source, a.channel)
        print(f"{'ALLOWED' if ok else 'REFUSED'}: {reason}")
        sys.exit(0 if ok else 2)
    elif a.cmd == "cut":
        ok, reason = cut_allowed(a.source, a.channel)
        print(f"gate: {reason}")
        if not ok:
            sys.exit(2)
        track = None
        if a.track:
            track_src = (open(a.track[1:]).read() if a.track.startswith("@")
                         else a.track)
            track = json.loads(track_src)
        clip, verdict = request_cut(a.source, a.start, a.end, a.hook,
                                    a.title, a.stars, dry_run=a.dry_run,
                                    caption_style=a.caption_style, track=track)
        if a.dry_run:
            print("dry-run done — no request sent, nothing cut.")
            return
        if clip is None:
            print(f"cut failed: {verdict}")
            sys.exit(1)
        print(f"done: {clip} safety={verdict}")
    elif a.cmd == "from-autopilot":
        pick = pick_from_dashboard()
        if not pick:
            print("no dashboard pick — run dashboard.py first.")
            sys.exit(1)
        print(f"autopilot pick: {pick['channel']} ({pick['platform']}) "
              f"{pick.get('viewers')} viewers — {pick['url']}")
        ok, reason = cut_allowed(pick["url"], pick["channel"])
        print(f"gate: {reason}")
        if not ok:
            sys.exit(2)
        clip, verdict = request_cut(pick["url"], a.start, a.end, a.hook,
                                    a.title or pick["title"], a.stars,
                                    dry_run=a.dry_run)
        if a.dry_run:
            print("dry-run done — no request sent, nothing cut.")
            return
        if clip is None:
            print(f"cut failed: {verdict}")
            sys.exit(1)
        print(f"done: {clip} safety={verdict}")


if __name__ == "__main__":
    main()
