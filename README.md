# Clipper Engine

A GTA 6 clipper for Vice City Vibes. Watches live streams, finds hype moments, and cuts them into vertical 9:16 Shorts/Reels with captions.

Built 2026-11-19 from gtavi-claudecode (MIT). Runs on this VM, no payment, no sign-up, no cookies.

## Hard rule

Clipping is only for OUR OWN footage or a live campaign that explicitly allows that creator. Never post re-uploads of other creators' streams.

## Layout

Layered, not flat — the code lives in `src/clipper/`, deploy configs in `deploy/`, docs in `docs/`, helpers in `scripts/`. Thin shims at the repo root (`clip.py`, `dashboard.py`, …) keep the old commands working; the canonical way to run anything is `python -m clipper.<layer>.<module>`.

```
├── README.md                  # this file
├── pyproject.toml             # package (src layout), console scripts
├── src/clipper/
│   ├── paths.py               # repo-root resolution (jobs/, dashboard/, garage/)
│   ├── app/                   # entrypoints / CLI orchestration
│   │   ├── dashboard.py       # Stream Dashboard — top live GTA 6 streams
│   │   ├── autopilot.py       # Auto-pilot — picks the best stream to scout
│   │   ├── watcher.py         # Fly watcher — scouts every 15 min, never posts
│   │   ├── web.py             # Personal web dashboard (read-only, stdlib only)
│   │   ├── run_fly.py         # Fly entrypoint — watcher + web in one machine
│   │   └── garage.py          # Video Garage — clip index + approve/export
│   ├── core/                  # domain logic
│   │   ├── clip.py            # scout / cut / manual / hype / export / polish
│   │   ├── reframe.py         # full-fill 9:16 game-action tracking
│   │   ├── reframe_clipit.py  # vendored ClipIt tracker (MIT) + YuNet model
│   │   ├── safety.py          # content filter — SAFE / REVIEW / BLOCK (NudeNet)
│   │   ├── scenes.py          # scene snap (PySceneDetect)
│   │   └── openshorts_ffmpeg_utils.py  # vendored openshorts helper (MIT)
│   ├── infra/                 # external integrations
│   │   ├── ssl_fix.py         # yt-dlp / Hugging Face cert workaround
│   │   ├── modal_client.py    # Fly → Modal caller (Rule 2/11 gate)
│   │   └── gtavi/             # vendored gtavi-claudecode scripts/ (MIT, untouched)
│   ├── api/                   # Modal apps / web endpoints
│   │   ├── modal_app.py       # GPU pipeline cutter (volume, /cut + /health)
│   │   └── modal_standalone.py# self-contained GPU cutter (streams mp4 back)
│   └── config/
│       ├── watchlist.json     # your own links, pinned / hidden
│       └── campaigns.json     # live paid campaigns that allow clipping (empty)
├── deploy/
│   ├── fly/                   # Dockerfile, fly.toml, requirements.txt
│   └── modal/                 # Modal README + TEST checklist
├── docs/                      # test reports (2026-11-19)
└── scripts/                   # shell helpers (scout/cut batches, downloads)
```

Runtime state lives at the repo root and is **not** in Git: `jobs/` (scout/cut working dirs), `dashboard/` (streams.json/.md, trends.md), `garage/` (clips.json + index.html).

Notes:
- The YuNet face model (`core/models/face_detection_yunet_2023mar.onnx`) is **not** in Git (binary). The tracker falls back to Haar cascades when it is missing — copy the model in locally if you want YuNet tracking.
- `src/clipper/infra/gtavi/` is the vendored gtavi-claudecode `scripts/` tree, upstream untouched (MIT). `ssl_fix.py` finds it there first, then falls back to the original local checkout.

## What it does

| Feature | Where |
|---|---|
| Stream Dashboard — top Twitch/YouTube GTA 6 streams, ranked by viewers + chat heat | `clipper.app.dashboard` — real live data |
| Watchlist — your own links, pin / hide | `src/clipper/config/watchlist.json` |
| Auto-pilot — picks the best stream to scout | `clipper.app.autopilot` — suggests only, a human confirms before any recording |
| Hype Meter — chat speed + hype words + loud moments, 5 stars = clip-worthy | `clipper.core.clip hype` |
| Auto-clip on hype | `clipper.core.clip scout` → `clipper.core.clip cut --pick N` |
| Manual Clip it now / Mark Start / Mark End, up to 50 clips per stream | `clipper.core.clip manual` / `cut`, `clipper.app.garage` enforces the 50 cap |
| Cutting Room — auto-edit with captions, full-fill 9:16, hook, -14 LUFS | `clipper.core.clip cut` + `clipper.core.clip polish` |
| Exports 16:9 + 9:16 | `clipper.core.clip export` |
| Video Garage — sorted by streamer/platform/moment, approve / export / delete | `clipper.app.garage` |
| Content filter — blocks unsafe clips before posting | `clipper.core.safety` (NudeNet) |
| Scene snap — cuts snap to real scene changes | `clipper.core.scenes` (PySceneDetect) |

## How to run

Run from the repo root, with the gtavi venv. `PYTHONPATH=src` is needed for the `-m` form; the root shims (`python clip.py …`) set it for you.

```bash
export PYTHONPATH=$PWD/src
V=~/workspace/gta-test/gtavi-claudecode/.venv/bin/python

# 1. Stream Dashboard
$V -m clipper.app.dashboard --query "gta 6" --limit 20

# 2. Auto-pilot scout
python -m clipper.app.autopilot

# 3. Scout a source (OWN footage or campaign-allowed VOD/file only)
$V -m clipper.core.clip scout <url-or-local-file> --top 8 --min 7 --max 30

# 4. Cut the best moment (full-fill vertical 1080x1920, captions + hook)
$V -m clipper.core.clip cut jobs/<id> --pick 1 --hook "WAIT FOR IT"

# 5. Export 16:9 too
$V -m clipper.core.clip export jobs/<id>/edits/clip_1.mp4

# 6. Video Garage
python -m clipper.app.garage register jobs/<id>/edits/clip_1.mp4 --moment "best moment" --stars 5
python -m clipper.app.garage approve jobs/<id>/edits/clip_1.mp4
```

Or install it once (`pip install -e .`) and use the console scripts: `clipper-dashboard`, `clipper-clip`, `clipper-garage`, `clipper-watcher`, `clipper-autopilot`, `clipper-modal-client`.

## Web dashboard (personal view, not for sale)

`https://vice-city-vibes-clipper.fly.dev` — a single-page, read-only
dashboard served by the same Fly machine as the watcher
(`src/clipper/app/web.py`, stdlib only, no new dependencies; entrypoint
`src/clipper/app/run_fly.py` runs watcher + web together, and restarts
the watcher if it exits, so adding the web service never stops scouting).
Neon Vice City styling (pink/purple sunset gradient, no Rockstar logo,
no Lucia/Jason likeness). Shows: live GTA 6 streams now with a Hype
Meter, auto-pilot top pick, Most Wanted trends, Video Garage clips with
safety verdicts, watcher status/last run, Modal cutter status, Fly
machine status. Data comes from the `/data` volume (watcher.log,
streams.json, trends.md, clips.json) with a local repo fallback.

**Read-only, no auth yet:** there are no buttons that record, cut or
post anything — posting still needs Ajay's approval of the exact clip
(Rule 1). Keep the URL private; do not share it publicly, and do not
add posting actions without auth + approval.

Local test: `PYTHONPATH=src python -m clipper.app.web --port 8080`,
then open http://localhost:8080/ and /api/state.

## Fly → Modal (GPU cutting)

1. Deploy the cutter: `modal deploy src/clipper/api/modal_app.py --profile maddukuriajaykumar05`
2. Set the endpoint on Fly: `flyctl secrets set MODAL_CUT_URL=<printed /cut URL>`
3. Cut one allowed clip: `python -m clipper.infra.modal_client cut --source <url> --start 10 --end 30 --hook "WAIT FOR IT"`
   — refused unless the source is own footage (watchlist) or a live paid campaign in `src/clipper/config/campaigns.json`.
   The finished clip downloads to `jobs/modal-<id>/edits/clip_1_fill.mp4` and is registered in the Garage.
   Or from the autopilot pick: `python -m clipper.infra.modal_client from-autopilot --start 10 --end 30 --hook "…"`

## Deploy

- Fly (watcher, CPU, always-on): from the repo root — `flyctl deploy --config deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile`
- Modal (GPU cutter, on demand): `modal deploy src/clipper/api/modal_app.py` with the `maddukuriajaykumar05` profile (self-contained cutter: `modal deploy modal_app.py`, the root shim)

### GitHub Actions — the reliable deploy route

`.github/workflows/deploy-fly.yml` deploys the watcher to Fly on every push
to `main` that touches `deploy/fly/**`, `src/**`, or the workflow itself, and
can also be run manually (Actions > Deploy watcher to Fly > Run workflow).
It also builds and pushes the image to
`ghcr.io/MAJAYKUMAR/vice-city-vibes-clipper:<commit-sha>` (plus `:latest`),
so the image is available even if the Fly step is skipped. This is the
reliable route because deploying directly from the dev VM kept failing
(Fly token scope / Depot builder hangs).

Required secret, set once before the workflow can deploy:
**Settings > Secrets and variables > Actions > `FLY_API_TOKEN`** — a Fly
Personal Access Token (fly.io dashboard > account > Tokens). Without it,
the Fly step fails; the GHCR image step still runs. Never put a token value
in the workflow file or any file in the repo.
