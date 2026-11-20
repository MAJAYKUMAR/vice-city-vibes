# Modal GPU cutter — Vice City Vibes

`src/clipper/api/modal_app.py` runs the same cut pipeline as `clipper.core.clip` on a Modal
GPU, on demand. The Fly watcher scouts live streams (CPU, always-on);
when it finds a moment, it calls this cutter over HTTPS instead of
cutting on the slow CPU VM.

## What it does

1. **Scout** — faster-whisper transcribe (GPU if available) + hype
   candidates, same as `clip.py scout` (loudness spikes, hype words,
   most-replayed heatmap, chat spikes).
2. **Cut** — scene-snapped, full-fill 9:16 cut (`clipper.core.reframe` game-action
   tracking, no blurred bars), word-by-word captions + hook, -14 LUFS.
3. **Safety** — NudeNet filter (`clipper.core.safety`): SAFE / REVIEW / BLOCK.
   A BLOCK verdict is returned with `"do_not_post": true` — it must
   never post. Only good clips post (Rule 14, always).
4. **Store** — finished 1080×1920 mp4 + `.json` metadata in the
   `vice-city-vibes-clips` Modal Volume (`/data/<job_id>/`), so Fly
   or anything else can fetch it.

## Files

- `src/clipper/api/modal_app.py` — the whole app: GPU function `cut_clip`, HTTP
  stub `web` (FastAPI via `@modal.asgi_app`), `modal run` entrypoint.
  No token or secret anywhere in this file — auth is the Modal CLI
  profile on the deploy machine.

## Deploy

```bash
cd ~/workspace/gta-test/clipper-engine
~/workspace/gta-test/clipper-engine/modal-venv/bin/modal deploy src/clipper/api/modal_app.py
```

Deploy needs a working Modal profile on that machine
(`modal profile activate maddukuriajaykumar05`). Known issue on this
VM (2026-11-19): `modal token set --verify` fails with "Could not
connect to the Modal server" (gRPC through the VM egress proxy);
the profile was saved with `--no-verify` and shows as active, but
its workspace reads as "Unknown (profile misconfigured)". If
`modal deploy` fails the same way here, deploy from a machine where
Modal connects (e.g. Ajay's laptop) — the code here does not change.

After a successful deploy Modal prints the web URL, shaped like:
`https://<workspace>--vice-city-vibes-clipper.modal.run`

## Use it

Manual test (from the deploy machine):

```bash
modal run src/clipper/api/modal_app.py --source-url "<twitch/youtube VOD or mp4 URL>" \
    --hook "WAIT FOR IT"
```

From Python (same machine / any machine with the profile):

```python
import modal
cut = modal.Function.from_name("vice-city-vibes-clipper", "cut_clip")
result = cut.remote("https://www.twitch.tv/videos/...", hook="WAIT FOR IT")
print(result["file"], result["safety_verdict"], result["do_not_post"])
```

From Fly (or curl) over HTTPS:

```bash
curl -X POST https://<workspace>--vice-city-vibes-clipper.modal.run/cut \
  -H "Content-Type: application/json" \
  -d '{"source_url": "https://www.twitch.tv/videos/...", "pick": 1, "hook": "WAIT FOR IT"}'
# -> {"job_id": "...", "file": "/data/<job>/clip_1_fill.mp4", "safety_verdict": "SAFE", "do_not_post": false, ...}

curl https://<workspace>--vice-city-vibes-clipper.modal.run/health
curl -o clip.mp4 https://<workspace>--vice-city-vibes-clipper.modal.run/download/<job_id>/clip_1_fill.mp4
```

Request fields (`POST /cut`): `source_url` (required — YouTube/Twitch
VOD URL or direct mp4 URL; a local file path from the caller's machine
does NOT work remotely), `pick` (candidate number, default: top-scored),
`hook`, `top` (default 8), `min_s`/`max_s` (default 7/30), `start`/`end`
(manual Mark Start / Mark End window — scout still runs first for the
transcript), `polish` (default false — Auto-Editor silence trim).

## GPU / cost

- Default GPU is **T4** (cheapest useful GPU; set `GPU = "L4"` at the
  top of `modal_app.py` for more speed at ~2x the price).
- Whisper model is **small** on GPU (clipper.core.clip default); the first run
  on a cold container downloads the Whisper model (~466 MB) and the
  NudeNet model once, then they are cached for that container's life.
- Containers spin up on demand and shut down after — no always-on
  cost. Modal free credits on file: $72.50 total ($30 credits + $42.50
  workspace usage, per Ajay 2026-11-19).
- Timeout per cut: 1800s. A 12-min source took ~38 min end-to-end on
  CPU; on GPU the transcribe + reframe + encode stages are 5–10x
  faster — the record time is what never changes.
