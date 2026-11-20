# Clipper Engine

A GTA 6 clipper for Vice City Vibes. Watches live streams, finds hype moments, and cuts them into vertical 9:16 Shorts/Reels with captions.

Built 2026-11-19 from gtavi-claudecode (MIT). Runs on this VM, no payment, no sign-up, no cookies.

## Hard rule

Clipping is only for OUR OWN footage or a live campaign that explicitly allows that creator. Never post re-uploads of other creators' streams.

## What it does

| Feature | Where |
|---|---|
| Stream Dashboard — top Twitch/YouTube GTA 6 streams, ranked by viewers + chat heat | `dashboard.py` — real live data |
| Watchlist — your own links, pin / hide | `watchlist.json` |
| Auto-pilot — picks the best stream to scout | `autopilot.py` — suggests only, a human confirms before any recording |
| Hype Meter — chat speed + hype words + loud moments, 5 stars = clip-worthy | `clip.py hype` |
| Auto-clip on hype | `clip.py scout` → `clip.py cut --pick N` |
| Manual Clip it now / Mark Start / Mark End, up to 50 clips per stream | `clip.py manual` / `cut`, `garage.py` enforces the 50 cap |
| Cutting Room — auto-edit with captions, full-fill 9:16, hook, -14 LUFS | `clip.py cut` + `clip.py polish` |
| Exports 16:9 + 9:16 | `clip.py export` |
| Video Garage — sorted by streamer/platform/moment, approve / export / delete | `garage.py` + `garage/index.html` |
| Content filter — blocks unsafe clips before posting | `safety.py` (NudeNet) |
| Scene snap — cuts snap to real scene changes | `scenes.py` (PySceneDetect) |

## How to run

```bash
VENV=~/workspace/gta-test/gtavi-claudecode/.venv/bin/python

# 1. Stream Dashboard
$VENV dashboard.py --query "gta 6" --limit 20

# 2. Auto-pilot scout
python3 autopilot.py

# 3. Scout a source (OWN footage or campaign-allowed VOD/file only)
$VENV clip.py scout <url-or-local-file> --top 8 --min 7 --max 30

# 4. Cut the best moment (full-fill vertical 1080x1920, captions + hook)
$VENV clip.py cut jobs/<id> --pick 1 --hook "WAIT FOR IT"

# 5. Export 16:9 too
$VENV clip.py export jobs/<id>/edits/clip_1.mp4

# 6. Video Garage
python3 garage.py register jobs/<id>/edits/clip_1.mp4 --moment "best moment" --stars 5
python3 garage.py approve jobs/<id>/edits/clip_1.mp4
```

## Files

- `dashboard.py` — Stream Dashboard
- `autopilot.py` — Auto-pilot scout pick
- `clip.py` — scout / cut / manual / hype / export
- `garage.py` — Video Garage index + HTML
- `safety.py` — content filter
- `scenes.py` — scene detection
- `reframe.py` — full-fill 9:16 game-action tracking
- `ssl_fix.py` — VM cert workaround for yt-dlp / Hugging Face
- `watcher.py` — Fly watcher (scouts every 15 min, never records/cuts/posts)
- `fly.toml` / `Dockerfile` — Fly deploy (shared-cpu-1x, 512MB)
- `watchlist.json` — your own links, pinned / hidden

## Deploy

- Fly (watcher, CPU, always-on): `flyctl deploy` from this folder
- Modal (GPU cutter, on demand): `modal deploy` with the `maddukuriajaykumar05` profile
