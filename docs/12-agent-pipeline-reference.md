# 12-Agent Clipping Pipeline — reference post (found online, 2026-11-25)

Ajay found this post online and shared it as a design reference for our web
clipper engine — NOT a LinkedIn draft, NOT to be posted anywhere.

## Original text (as found)

"I'm building a GTA 6 clipping engine powered by 12 AI agents. Not another AI
video editor. Not a simple n8n workflow. This is a dedicated clipping
infrastructure designed to turn raw GTA 6 gameplay, streams, and recordings
into a complete short-form content pipeline. 12 specialized agents work
together: Footage analysis and scene detection; Viral moment and reaction
identification; Highlight scoring and moment selection; Smart clip extraction;
AI-powered reframing and cropping; Hook and caption generation; Subtitle
synchronization; Motion and visual enhancement; Audio optimization; Quality
control; Copyright risk detection; Final export and content preparation. The
entire system is being built with Claude powering the intelligence and n8n
coordinating the workflows, agents, and execution pipeline. I'm also
integrating a copyright risk detection and compliance layer to identify
potentially problematic footage and reduce copyright-related risks before clips
move through the publishing pipeline. The goal? Take hours of raw GTA 6
footage and turn it into a structured pipeline capable of producing
platform-ready content with minimal human intervention. 12 agents. One
infrastructure. An entire clipping pipeline. Still building."

## Mapping to our clipper-engine (assessed 2026-11-25)

| # | Stage in post | Our status |
|---|---|---|
| 1 | Footage analysis / scene detection | BUILT — PySceneDetect + time-spread visual sampling in scan workers |
| 2 | Viral moment / reaction ID | PARTIAL — chat-heat signal proved weak (0 spikes in 4 forward runs; spikes track reactions/menus, not action); visual sampling finds most picks |
| 3 | Highlight scoring / selection | BUILT — heat-ratio scoring (scan_vod.py 10s windows); visual picks by worker judgment |
| 4 | Smart clip extraction | BUILT — ffmpeg cuts, Modal GPU cutter on demand, CPU fallback |
| 5 | Reframing / cropping | BUILT — 9:16 reframe; 40/60 facecam test running as A/B |
| 6 | Hook / caption generation | PARTIAL — titles + captions written per clip by workers; no dedicated hook scoring |
| 7 | Subtitle synchronization | BUILT — faster-whisper word-level karaoke captions |
| 8 | Motion / visual enhancement | BASIC — 720p→1080p Lanczos upscale + unsharp, CRF 17 |
| 9 | Audio optimization | GAP — audio is passthrough; no loudness normalization. We DO detect fully-muted audio (StableRonaldo VOD dropped for copyright-muted audio) |
| 10 | Quality control | BUILT — Rule 9b visual realism check + NudeNet safety + QC gates |
| 11 | Copyright risk detection | BUILT 2026-11-25 — src/clipper/core/copyright.py, hooked into cut_filltrack() step 2b (right after the safety check). Verdicts CLEAR / REVIEW / BLOCK; BLOCK on muted audio (mean <= -60 dB, Twitch mute floor -91 dB), >= 80% silent window, or streamer opt-out blocklist hit; REVIEW on near-silent + no speech, sustained music bed + no speech, no audio stream, or missing/unparseable blocklist. Garage refuses copyright-BLOCK clips. Fingerprint hook present but DISABLED (no network, no keys). |
| 12 | Final export / content prep | BUILT — post-packages (exact title/caption/thumbnail), clip-queue.md, hourly queue-post cron |

## Architecture notes

- **n8n: not needed.** Our orchestration is already Muse coordinator agents +
  worker swarms (64 workers in the big refill), cron jobs, the Fly always-on
  watcher, and the Modal GPU cutter. Adding n8n would be a rewrite for no gain.
  The post's "12 agents" framing validates what we built — we just run it as
  agent swarms, not 12 fixed agents.
- **The one genuinely new idea is #11, copyright risk detection.** A real layer
  could flag, at scan time: (a) copyright-muted audio segments, (b) licensed
  music in the clip's audio, (c) streamers with known clip opt-outs / takedown
  history. Currently only (a) is caught, and only as a whole-VOD drop.
- **Audio optimization (#9)** is a small win: loudness normalization across
  clips would make the feed experience consistent.

## Next step (built 2026-11-25)

The copyright-risk check (stage #11) is BUILT: `src/clipper/core/copyright.py`
(`check_clip()`, verdicts CLEAR / REVIEW / BLOCK), hooked into
`src/clipper/core/clip.py::cut_filltrack()` step 2b — right after the safety
check, before the crop plan — with the verdict stored in the clip JSON as
`"copyright"` next to `"safety"`, a `copyright` CLI subcommand mirroring
`safety`, `copyright` + `copyright_reasons` in `scripts/cut_vod_pick.py`
cut-summary.json, and garage.py refusing copyright-BLOCK clips on
register/approve. Note the hook is at CUT time, not scan time:
scan_vod.py runs no safety/copyright checks — only the cut flow does — so
muted/blocked audio is still discovered after cutting, as format proof.
Verdict semantics: BLOCK = muted audio (mean <= -60 dB, floor -91 dB),
>= 80% silent window, or streamer opt-out blocklist hit — never approve,
never post; REVIEW = near-silent + no speech, sustained music bed + no
speech, no audio stream, or missing/unparseable blocklist — needs a worker
look before posting; CLEAR = everything else. The streamer opt-out
blocklist (`src/clipper/data/streamer_blocklist.yaml`) starts EMPTY — only
Ajay-confirmed entries with cited evidence go in.

### Copyright check — queue convention

Copyright-BLOCK clips are filed as Section D HELD entries in clip-queue.md
exactly like safety BLOCKs — e.g. `### D15. name.mp4 — 23.0s — HELD
(copyright BLOCK — audio muted, mean -91 dB)` — and are never posted: the
queue-post cron only ever picks C-numbered entries from Section C.
Copyright-REVIEW clips need a worker look + a `copyright REVIEW cleared`
note in the Flags/QC line before posting. clip-queue.md and package.md are
written by worker agents, not by Python code — no code edits the queue.

Follow-up (not done here): Modal parity — `src/clipper/api/modal_standalone.py`
and `modal_app.py` (deployed Modal code) do not run this check yet; the
copyright check needs porting there before GPU-cut clips get the same gate.
