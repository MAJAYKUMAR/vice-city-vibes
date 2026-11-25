# Pipeline Upgrade Research — Vice City Vibes clipping engine (2026-11-25)

Research-only pass (no code edited). Four domain researchers read the current
state first (rules file, 12-stage reference, `clip.py` / `captions.py` /
`safety.py`, queue file head) and grounded every claim in a real source.
Ranked by expected impact vs effort. Hype is labeled as hype.

## Corrections the researchers found in our own docs

These matter — the 12-stage reference doc is wrong in three places:

1. **Audio is NOT passthrough.** Every cut path already normalizes:
   single-pass `loudnorm I=-14` in `clip.py` (`cut_filltrack`, `cut_portrait`,
   `export_16x9`) and `modal_standalone.py`. The real gaps are (a) no
   post-encode verification, (b) TP ceiling inconsistent across paths
   (−1.5 in `clip.py`/Modal vs −2.0 in `openshorts_ffmpeg_utils.py`),
   (c) no conditional cleanup. The −14 LUFS target itself is already right.
2. **The real-clip path does NOT do Lanczos + unsharp.** `reframe.py`
   `fill_filter()` passes no `flags=` at all — inside `-filter_complex`
   ffmpeg defaults to **bilinear**, the softest scaler. The rules file §5
   ("ALWAYS upscale with Lanczos + unsharp, CRF 17") describes the AI-clip
   path, not what real clips get. Same for CRF: filltrack is CRF 18,
   portrait/export CRF 20 — one step softer than the stated standard.
3. **Per-segment muted-audio detection ALREADY exists for Twitch.**
   `pipeline/*/fetch_segments.py` parses the HLS playlist and counts
   `-muted` segments per 10 s window. It is only logged, never used as a
   gate. The whole-VOD drop (StableRonaldo) was the old behaviour.

Other useful facts the researchers surfaced: `moments.py` already computes
loudness spikes, transcript hype words, most-replayed heatmap and chat spikes
(a hook score can reuse these); `polish_clip()` (auto-editor silence trim)
already exists, just unwired; `openshorts_ffmpeg_utils.py` already has NVENC
tiers for the Modal GPU path; `cut_filltrack()` already scene-snaps via
PySceneDetect and face-tracks via YuNet in `reframe.py`.

## Master ranked table (deduplicated across domains)

| # | Technique | Improves | Effort | Impact | Plugs into |
|---|---|---|---|---|---|
| 1 | Scaler fix: explicit Lanczos + light unsharp/cas in filltrack | Sharpness of every real clip (fixes a real bug: bilinear today) | S | M | `reframe.py:fill_filter()`, `clip.py:cut_portrait()`, `export_16x9()` |
| 2 | Caption + hook safe-zone fix: cap_y 1380→~1080, hook MarginV 170→~400 | Muted-viewer legibility (current positions sit under YouTube's UI overlay) | S | M | `captions.py:build_ass()`, `_HOOK_STYLE` |
| 3 | Atomic queue writes + idempotent C-number allocation | Never another C75–C79 numbering collision (already happened once) | S | L | New `core/queue_io.py`; all writers of `clip-queue.md` / `candidates.json` |
| 4 | Payoff-front-load the clip window (peak at t≈0.5 s) | Swiped-away rate — the #1 feed gate; our own data proves it (iguana 77.5% stayed vs golden hour 24.4%) | S | L | `clip.py:cut_filltrack()` start-offset; `moments.py` candidate ranking |
| 5 | Post-encode QC gate: ebur128/astats audio check + export-spec validation (ffprobe) | Catches bad loudness, clipping, wrong res/fps, missing faststart, oversize files before posting | S | M | New `core/audio_qc.py` + `core/validate.py`, called at end of cut paths |
| 6 | Queue file lock + per-worker temp dirs | 19–64 parallel workers can't race on shared files | S | M | `core/queue_io.py` (flock); worker spawn code |
| 7 | yt-dlp hardened profile: bounded retries, exp backoff, resume, archive | Attacks the most repeated failure (SSL/timeout stalls; trends.py failed every cycle) | S | M | `infra/ssl_fix.py:patch_yt_dlp`, `moments.py`, scan workers |
| 8 | Fetch best source: 1440p cap for YouTube, keep 1080p for Twitch, prefer 60 fps | Native pixels beat any upscaler; 60 fps keeps motion smooth | S | M | `scripts/scan_vod.py:57`, `scripts/live_heat.py:122` (branch on URL) |
| 9 | Pre-flight disk-space guard + stale `*.part` cleanup | Tiny near-full VM can't fill mid-cut and corrupt downstream work | S | M | New `infra/disk_guard.py`; top of `run_moments` / scan workers / `cut_filltrack` |
| 10 | Encode unification: CRF 17–18 everywhere, NVENC on Modal, bitrate caps | Consistent clean master for platform transcoders; no more CRF 20 outliers | S | M | `clip.py` cut paths; `openshorts_ffmpeg_utils.py` NVENC tiers |
| 11 | First-2-seconds hook score (audio + visual + transcript) | Repeatable clip ranking / start-offset choice instead of worker gut-feel | M | M | New `core/hook_score.py`; reuses `moments.py` signals + `reframe.py` YuNet |
| 12 | Per-segment muted-audio rescue (shift window to clean span; YouTube silence×transcript check) | Recovers clips from partially-muted VODs instead of dropping whole VODs | M | M | `fetch_segments.py` (expose `muted_segments`); scan/triage path |
| 13 | Two-pass loudnorm (measure, then apply) — VERIFY FIRST on VCV clips | Loudness consistency across clips in the feed | S | M | `clip.py` `-af` chains; centralize via `openshorts_ffmpeg_utils.py:LOUDNORM_FILTER` |
| 14 | Duplicate detection: exact key + SHA-256, then pHash on 3 frames | Stops the same moment being cut twice from different VODs/scans | M | M | New `core/dedup.py`; SQLite/JSON index of queued + posted |
| 15 | Resume checkpoints per job (`state.json`: scouted→scanned→cut→queued→posted) | Refill that dies at clip 47/100 resumes at 48, not from scratch | M | M | Per-job `state.json`, atomic writes via #3's helper |
| 16 | Auto-draft titles/hooks from Whisper transcript (templates first) | Removes per-clip hand-writing bottleneck; grounds titles in what's actually said | S | M | New `core/titles.py` on `transcript.json` |
| 17 | Conditional light cleanup: `highpass=f=80` + `afftdn`, only when measured floor warrants it | Removes rumble/hiss on noisy streamer audio without mangling game music | S | M (conditional) | `clip.py` `-af` chains, gated by #5's measurement |
| 18 | Face-track smoothing + active-speaker pick for 40/60 reframes | Face stays in frame without jitter in the A/B split format | M | M (40/60 only) | `core/reframe.py:plan_crop()` |
| 19 | Sentence-case captions + accent-one-word; A/B karaoke vs pop on our own numbers | Readability; settles which existing style retains better | S | M | `captions.py:_STYLES`; `--caption-style` passthrough |
| 20 | Edge-silence trim: wire existing `polish_clip()` as opt-in flag | Hook lands in first 1–2 s; no dead tail | S | S–M | `clip.py:main()` + `polish_clip()` (after burn-in) |
| 21 | Gentle `acompressor` + presence EQ (2–4 kHz lift) before loudnorm | Speech cuts through game audio on phone speakers | S | S–M | `clip.py` `-af` chains; needs phone-speaker A/B test |
| 22 | Unify TP ceiling (−1.5 vs −2.0), decided by #5's measurements | Consistency; prevents rare AAC inter-sample clipping | S | S | Single constant in `openshorts_ffmpeg_utils.py:LOUDNORM_FILTER` |
| 23 | Scene-snap hardening: min scene length 2 s, prefer `detect-adaptive` for gameplay | No snap lands on a 0.3 s micro-cut | S | S | `core/scenes.py:snap()` |
| 24 | Light pre-clean `hqdn3d` + `deband` on ≤720p / low-bitrate sources only | Upscale doesn't amplify macroblocking and banding | S | M on bad sources / S on clean 1080p | `reframe.py:fill_filter()`, gated on source height/bitrate |
| 25 | Real-ESRGAN opt-in Modal post-pass for ≤720p sources — BENCHMARK FIRST | Only technique that *adds* apparent detail; but per-frame GAN shimmers on motion | M | M | New optional stage after reframe cut, before caption burn-in; gate on source ≤720p |
| 26 | Seamless-loop trim where start≈end frame | APV above 100% on looping clips | M | M (narrow) | `clip.py:cut_filltrack()` / `polish_clip()` |
| 27 | Local Ollama title drafting (`llama3.2:3b` on Modal T4) | 3 title/hook variants per clip, zero API cost | M | M | Phase 2 of #16; needs Ollama install on Modal image |
| 28 | BasicVSR++ video upscaler (temporally correct) — experiment only | Fixes Real-ESRGAN's flicker with cross-frame memory | L | M | Same insertion point as #25; after #25 is benchmarked |
| 29 | Spline36 vs Lanczos A/B (+ zscale caution: red-shift bug in 4:2:0) | Marginally less ringing than Lanczos | S | S | `reframe.py:fill_filter()` flag swap; same A/B session as #1 |
| 30 | TikTok "Content under review" — process rule, not code | Stops misreading a normal moderation hold as a failed post (and re-uploading) | S | S–M | Posting-run checklist / rules file addendum; wait 45 min, escalate after 24–48 h |
| 31 | Concurrency cap by resource (max 2–3 CPU-heavy ffmpeg/Whisper jobs on VM) | Prevents the pile-ups that already hang posts | M | M | Worker-spawn code; checkpoints (#15) make a slower refill safe |
| 32 | 1440×2560 vertical master for YouTube Shorts only — test first | Maybe forces VP9/AV1 tier; evidence for Shorts is contradictory | M | S (uncertain) | `clip.py:cut_filltrack()` `--master-1440` flag; A/B one clip first |
| 33 | H.265/AV1 upload master (YouTube only) — test, not migration | Slightly cleaner source for YouTube's transcoder | M | S | `openshorts_ffmpeg_utils.py` HEVC NVENC tier |


---

## Domain detail

### 1. Video quality / resolution

- **#1 scaler fix (S/M).** Evidence: ffmpeg scaler docs list `lanczos`
  (windowed sinc) vs default `bicubic`
  (https://ffmpeg.org/ffmpeg-scaler.html). A measured CPU comparison (Big
  Buck Bunny, PSNR/SSIM vs ground truth) found Lanczos 28.77 dB / SSIM 0.735
  beat bicubic 28.70 / 0.730 and both Real-ESRGAN models on fidelity
  (https://huggingface.co/blog/Jacob123222/ai-video-upscaling-with-real-esrgan-and-ffmpeg-a-m).
  Guides converge on `scale=…:flags=lanczos,unsharp=5:5:1.0:5:5:0.5` /
  `cas=0.5` (https://wavespeed.ai/blog/posts/blog-how-to-upscale-enhance-video-quality-ffmpeg/;
  https://www.aiarty.com/ai-video-enhancer/ffmpeg-upscale-video.htm). Cost
  ~2–3% encode time. Watch for Lanczos ringing on HUD text.
- **#8 fetch best source (S/M).** `FORMAT = "bv*[height<=1080]+ba/b"` is a
  hard ceiling in `scan_vod.py:57` and `live_heat.py:122`. Twitch caps
  ingestion at 1080p60 (6,000 kbps non-Partner, 8,500 Partner; 1440p only via
  Enhanced Broadcasting HEVC) — keep 1080p for Twitch. YouTube VODs can be
  1440p/4K; run `yt-dlp -F` first and only pay the ~2× download cost when
  the format exists.
- **#3 preserve 60 fps (S/M).** `reframe.py` forces `fps=30`, halving motion
  from 60 fps sources. All platforms accept 23–60 fps; YouTube's ladder is
  12 Mbps @ 48–60 fps vs 8 Mbps @ 30 fps. Captions are time-based (`.ass`),
  so no caption change — verify karaoke timing on one 60 fps clip. Use the
  Modal NVENC path for 60 fps cuts, not libx264-medium on the tiny VM.
- **#24 pre-clean (S).** "Upscale a 320×240 clip 4× and every 8×8 block
  becomes a 32×32 tile… the fix has to happen before upscaling"
  (https://dev.to/thekillsquad007/why-upscaling-old-low-res-video-makes-it-look-worse-and-the-ffmpeg-filters-that-fix-it-4nnf).
  Light `hqdn3d=1.5:1.0:3:2,deband` only when source ≤720p or low bitrate.
  Never `nlmeans`/`bm3d` in the queue path (too slow). Over-denoising smears
  game texture into wax — light settings + A/B frames mandatory.
- **#10 encode hygiene (S).** Delivered medians measured Oct 2026: 2.1 Mbps
  (Reels), 1.1 Mbps (TikTok), 5.7 Mbps (Shorts) — bits above the ingest
  ceiling buy nothing, bits below starve the transcoder. Targets converge on
  1080×1920 H.264, 8–12 Mbps, AAC 128–256k. CRF 17 vs 18 is ~12% more bits
  for no visible difference on a phone after re-encode — don't chase CRF
  below 17. NVENC measured: `h264_nvenc p6 cq19` = 35 Mbps / VMAF 99.96 at
  ~107 fps.
- **#25 Real-ESRGAN (M/M, benchmark first).** Models: `RealESRGAN_x4plus`
  (general), `x4plus_anime_6B` (anime/line art only — do NOT use on game
  footage), `RealESRGAN_x2plus` (2×, less hallucination),
  `realesr-general-x4v3` (1.2M params, fast, denoise control; recommended
  default). Our upscale factors are 1.78–2.67×, so 2× models are right; 4×
  then downscale wastes compute. Honest limits: Lanczos *beat* both
  Real-ESRGAN models on PSNR/SSIM in the benchmark above (GAN invents
  plausible-but-wrong texture); per-frame Real-ESRGAN flickers/shimmers on
  motion (no temporal memory). **No T4 benchmark found.** RTX 4090 TensorRT:
  10.4 fps @720p; RTX 3080 Ti PyTorch: 2.85 fps — T4 estimate ~1–3 fps,
  i.e. ~2–7 min per 10–30 s clip on Modal. Acceptable on-demand, impossible
  as a bulk step. Gate to ≤720p sources; QC every output for shimmer on fast
  pans; fall back to #1 if it shimmers.
- **#28 BasicVSR++ (L/M, experiment only).** Bidirectional recurrent
  propagation + flow-guided alignment locks texture across 30+ frames —
  the right architecture for video; Real-ESRGAN is the wrong architecture
  used on video. 7.32M params vs Real-ESRGAN's 16.7M. Requires BasicSR +
  VSR inference script in the Modal image. Only after #25 is benchmarked.
- **#32 1440p Shorts trick (M/S uncertain).** Long-form: 1080p upload →
  ~97.3 VMAF vs 1440p → ~99.95 (independent 2026 VMAF testing). For Shorts
  specifically, sources contradict each other — one says playback tops out
  at 1080p ("no reason to render above that"), another says Shorts now get
  the better codec tier regardless. Only a live A/B upload settles it.
- **#33 H.265/AV1 (M/S).** Best-scoring masters in one 2026 test were
  H.265/AV1 ~60 Mbps 10-bit, but YouTube's official page recommends H.264
  High; "a smaller HEVC or AV1 upload does not come out looking better on
  the other side" once the platform re-encodes. Stay H.264 for delivery.

### 2. Audio

- **#5 post-encode QC gate (S/M).** After the encode, decode-only passes:
  `ebur128=peak=true` (integrated LUFS, LRA, true peak) + `astats` (Peak
  level, Flat factor — flat-topped runs indicate clipping) +
  `volumedetect`. Flag output I outside −15…−13 LUFS, TP > −1.0 dBTP, or
  Peak level 0 dB with Flat factor > 0. Log numbers into the clip's `.json`
  sidecar (same place the safety verdict lives). Cost ~1–2 s per clip.
  Note: Flat factor 0 does *not* rule out single-sample clipping — report
  as "possible clipping".
- **#13 two-pass loudnorm (S/M, verify first).** Measure pass
  (`print_format=json`, `-f null -`, decode-only, cheap) feeding
  measured_I/TP/LRA/thresh/offset back with `linear=true`. Measured:
  single-pass missed a −16 target by ~0.4 LU; two-pass by ~0.21
  (GronkAgent, ffmpeg 7.1.5, Nov 2026). **Critical gotcha:** linear mode
  silently falls back to dynamic when the gain would push true peak past
  the TP ceiling or target LRA < source LRA — on loud, peaky game audio
  two-pass often buys nothing. Our own codebase already measured this
  (openshorts utils: two-pass linear +0.4, still clipping). Adopt only if
  #5's data shows it tightens the spread on VCV clips.
- **Target honesty:** −14 LUFS is convergent practice, not a published spec
  — YouTube publishes no required number; TikTok and Instagram publish no
  target at all. And normalization helps in one direction only: YouTube
  turns louder uploads down to ~−14 but does **not** turn quiet ones up —
  a quiet clip stays quiet in the feed. The win is floor-raising quiet
  clips and consistency, not loudness per se.
- **#12 muted rescue (M/M).** Twitch: `fetch_segments.py` already returns
  `muted_segments` per 10 s segment — promote from logged number to gate:
  shift the candidate window to the nearest fully-clean span, drop only if
  no clean span covers the moment. YouTube: no `-muted` filenames, so
  detect with `silencedetect=noise=-50dB:d=5` cross-checked against the
  transcript — long digital silence (≥8–10 s) + video still moving + zero
  transcript words = likely copyright mute. Never silence alone (menu
  pauses, loading screens and dead air are also silent). Twitch mutes
  matching segments only; there is no tool that restores audio to an
  already-muted VOD — but unmuted spans are fully usable.
- **#17 conditional cleanup (S).** `highpass=f=80,afftdn=nr=10:nf=<measured
  floor>:tn=1` before loudnorm, only if the measured noise floor is high
  (silent gaps > −50 dB). `nf` set to the *measured* floor, not guessed —
  that is what separates clean from watery. A/B listen on a phone speaker
  before enabling by default.
- **#20 polish opt-in (S).** `polish_clip()` exists and is safe (cuts A+V
  together after captions are burned in; keeps the original if duration
  changes >2 s). Wire as `--polish` flag. **Never** put `silenceremove`
  inside the cut's `-af` chain: it shortens audio without shortening
  video, desyncing A/V and breaking caption timing. Any timing-changing
  audio step must happen BEFORE transcription.
- **#21 compressor + EQ (S).** `acompressor=threshold=-20dB:ratio=3:
  attack=15:release=250:knee=6,equalizer=f=3000:t=q:w=1:g=2` before
  loudnorm. A/B on 3–5 real clips first; if explosions/SFX pump audibly,
  drop the compressor and keep only the EQ, or drop both.
- **#22 unify TP (S).** In-repo measurement (14 clips): TP=−1.5 → peak
  +0.2 dBTP, 1 clipping; TP=−2.0 → −0.3 dBTP, 0 clipping. Decide from #5's
  data on ~10 VCV clips; either way delete the duplicated literal strings.
- **Short-clip trap:** EBU R128 integrated loudness is less stable on very
  short clips; one batch pipeline recommends falling back to peak
  normalization for clips under ~10 s. Our clips are 7–30 s — verify output
  loudness rather than assuming it.
- **Do NOT build:** sidechain music ducking — needs two separate inputs
  (music bus + voice key); our VOD audio is a single pre-mixed stream.
  The only way to get stems is source separation (e.g. Demucs, GPU ML) —
  outside the ffmpeg/light-CPU constraint. `arnndn` — needs an external
  `.rnnn` model file, trained on speech so it mangles game music/SFX, and
  breaks downstream silence detection. `dynaudnorm` — not a loudness tool;
  measured to *widen* loudness range (9.8 → 15.1 LU). `alimiter` after
  loudnorm — already tested in-repo, measured worse (+0.7 dBTP). Clipping
  repair — clipping baked into the source cannot be repaired by lowering
  level afterwards; flag-and-drop is the honest handling.

### 3. Captions / hooks / retention

- **#4 payoff-front-load (S/L).** YouTube Blog, Jan 2025 Shorts deep dive
  (Shorts product lead Todd Sherman on seed-cohort testing); creator Jenny
  Hoyos: "viewers decide in about one second whether to keep watching or
  swipe past"; ScaleLab, 3,000+ channels: "hook in the first 1–2 seconds…
  the Shorts algorithm tests swipe-away rate before anything else". Viral
  benchmarks converge on ≥75% viewed and <12% first-1.5 s drop-off — our
  56.8% stayed is below the breakout bar. Plug-in: after scene snap in
  `cut_filltrack()`, slide `s0` so the candidate window's highest-signal
  moment (loudness peak / visual-change peak) lands at t≈0.5 s; or prefer
  windows whose peak is in the first 2 s in `moments.py`. No new deps.
- **#11 hook score (M/M).** OpusClip's API blog lists exactly this signal
  set — transcript (hook strength, narrative arc, emotional valence),
  audio (pace shifts, pitch range, background reactions, pause patterns),
  visual (gesture intensity, facial expression) — and reports scores ≥70
  predict above-median performance ~75% of the time *on talking-head
  content*. Open-source precedent: NeonClipping scores sudden loudness
  jumps vs a 2-minute local baseline, raised voices (2–4 kHz energy),
  quiet-to-loud transitions, plus OpenCV Farneback optical flow for
  impacts/camera whips. **Honest caveat:** OpusClip itself says
  music-driven/animation content "scores conservatively" — our GTA gameplay
  clips are exactly that category, so treat the score as a ranking aid,
  never a green/red light. New module `core/hook_score.py`, reusing signals
  `moments.py` already computes + `reframe.py`'s YuNet face tracker + cheap
  ffmpeg `astats`/`volumedetect` loudness delta and frame-diff motion on
  the first 2 s vs clip mean. All stdlib + ffmpeg + already-vendored
  OpenCV; heavy parts can run on the Modal T4 alongside the cut.
- **#2 safe-zone fix (S/M).** Google's own vertical-video safe-zone
  margins, measured Oct 2026: top 288 px, bottom 672 px, left 48 px,
  right 192 px clear on 1080×1920, leaving an 840×960 px safe area;
  recommended subtitle band ≈900–1,248 px from top, hook text ≈350–700 px
  from top. **Our current code violates both:** `cap_y=1380` puts caption
  centres ~130 px *below* the safe area, inside the title/channel/subscribe
  overlay; hook at `MarginV 170` sits inside the top 288 px search/menu
  strip. ~80% of social video is watched muted (Verizon Media / Publicis
  Media 2019 study of 5,600 US adults), so for most viewers the caption
  *is* the video in the first 3 s. Fix: `cap_y` 1380→~1080, hook MarginV
  170→~400; verify on one posted clip's screenshot before rolling out.
  Meta's numbers agree in spirit: 14% top / 35% bottom / 6% sides free of
  text. Measured reality: on Instagram the button column starts ~970 px
  down, above Meta's 35% line at 1,248 — text near the right edge in the
  lower middle can sit under the like button even inside Meta's safe zone.
- **#19 caption style (S/M).** Evidence is mixed — report both sides.
  *For* word-by-word: KreateFlo's A/B across 40 Shorts claims watch time
  48%→79% and swipe-away 41%→19% (vendor blog, study not independently
  verifiable). QuickReel: 1–7 words visible, 2–4 as the comfortable
  middle. Libi/Blitzcut 2026 audit: highest-converting style is
  word-by-word in *sentence case* with a *single* highlighted accent word
  per phrase; ALL CAPS is losing in 2026 and sentence case reads ~10–15%
  faster in eye-tracking work. *Against* karaoke hype: LinkLab's research
  review found "no controlled evidence that word-by-word highlighting beats
  well-timed phrase captions on retention — every 'karaoke wins' claim
  traces to a caption-tool vendor," citing Rajendran et al. 2013
  (*Perspectives: Studies in Translatology* 21(1):5–21) — phrase chunking
  produced fewer fixations and less effortful processing. Our current code
  is already at the hybrid sweet spot (3-word groups, active-word
  highlight) — do not rebuild it. Real gaps: ALL CAPS is hardcoded
  (`uppercase=True`) in karaoke/pop, *every* word gets the highlight
  treatment, and we have never measured karaoke vs pop. Fix: add
  `uppercase=False` sentence-case option + "accent word only" mode; run the
  existing A/B (alternating posts, compare stayed-to-watch after 72 h).
- **#16 auto titles (S/M).** NeonClipping's `hooks.py` pattern: local LLM
  (Ollama `llama3.2:3b`) reads each moment's transcript, scores it, writes
  title + hook, snaps to sentence boundaries — fully local, zero API cost.
  Title craft: 40–70 chars, keyword in first 5 words, front-load inside
  first 50 chars (mobile truncation ~55–60), max 2 emojis for Shorts, no
  all-caps, no vague filler ("crazy/insane/wait for it" penalised by
  YouTube's clickbait classifier). SubSub's 120,703-video dataset: Shorts
  formula = emotion + brevity + one emoji; shorter titles outperform.
  Clickbait guard: YouTube judges titles by post-click completion — a title
  that overpromises tanks retention and distribution. Phase 1 (S):
  deterministic templates from our own proven winners ("[Subject] did X…
  then Y!"), 40–70 char cap, banned-word list (REAL/LEAKED/OFFICIAL per
  Rule 4). Phase 2 (M): Ollama on the Modal T4 to draft + score 3 variants
  per clip; worker/Ajay still approves exact text (Rule 1 unchanged).
- **#26 seamless loops (M, narrow).** Shorts auto-loop, so APV can exceed
  100% — viral benchmarks put APV at 98–125% including replays, loop-replay
  rates 20–35%. Case study: a 6-second Short with 71.0% stayed, 0:09
  average view on a 6 s clip, **156.1% APV** — the opening question returns
  while the landing is still fresh. Only for clips where start≈end frame or
  continuous action (driving, chase); gag clips with a hard payoff ending
  do *not* loop naturally — do not force it.
- **#7 thumbnails (S/S).** Our own finding is **confirmed, not
  contradicted**: ShortGenius placement test (500 Shorts, 50 channels, ~1M
  impressions) — custom thumbnails with readable text gave **+85% CTR in
  YouTube Search and 0% impact in the Shorts feed**. Same split in Taja
  and YTThumbnails guides. vidIQ's 12,000-channel analysis: 6.4% vs 4.1%
  CTR on home feed for custom vs auto frames. YouTube's own A/B testing
  now picks winners by **watch time, not CTR**. And YouTube rolled out
  custom Shorts thumbnails to Partner Program creators from July 2026.
  Conclusion: keep making peak-frame custom thumbnails (cheap, already
  built; search is ~4% of traffic and growing with the Jan 2026 Shorts
  search filter), add 3–5 words of readable text for search surfaces — but
  stop treating the thumbnail as a feed lever. The opening frame *is* the
  feed thumbnail.
- **Explicitly NOT recommended:** lengthening clips to 35–48 s for an
  "algorithmic sweet spot" (that number comes from talking-head/educational
  Shorts; our best clip is 10 s); "Triple-Interrupt Hook Framework" /
  0.0–0.4 s macro-zoom prescriptions (vivid, untestable, no data); buying a
  virality-score API (paid, needs credentials, vendor admits weak
  performance on exactly our content type); rainbow/emoji karaoke, bouncing
  words, one-word-only flashes (2026 audits call these a "slop tell";
  accessibility literature shows they raise cognitive load).

### 4. Robustness + platform specs

- **2026 spec sheet (verified Nov 2026).** One 1080×1920 H.264 master
  already satisfies all four platforms on video. Differences that matter
  are text limits, length caps and covers:
  - YouTube Shorts: 1080×1920 (max 1080p for Shorts), MP4 H.264 High +
    AAC-LC 48 kHz, **8 Mbps @ 24–30 fps, 12 Mbps @ 48–60 fps** (published
    ladder), max length **3 min**, 256 GB max, title **100 chars**,
    description **5,000 bytes UTF-8** (emoji = 4 bytes each; no `<` `>`),
    thumbnail 9:16, 2160×3840 recommended, ≤50 MB desktop / 2 MB app,
    **daily custom-thumbnail limit exists**.
  - Instagram Reels: 1080×1920 (API max width 1920), MP4/MOV H.264 or
    HEVC, AAC ≤48 kHz, closed GOP, 4:2:0, moov atom first, VBR ≤25 Mbps
    (5 Mbps recommended), 15 min via API / 20 min in app (only ≤3 min
    recommended to non-followers), **300 MB via API**, caption **2,200
    chars**, hashtags **30 per API docs but Help Center says 5** (we use 4
    — safe either way, never add more without re-checking), JPEG cover
    ≤8 MB, 9:16, feed grid crops middle 1:1 square.
  - Facebook Reels: 1080×1920 recommended (min 540×960), MP4 H.264/H.265,
    AAC-LC stereo 48 kHz, closed GOP 2–5 s, no limit in app, **3–90 s via
    Reels Publishing API**, separate thumbnail ≤10 MB.
  - TikTok: 1080×1920 (min 720×1280 web), MP4/WebM, H.264 recommended,
    23–60 fps, 10 min via Content Posting API, up to 30–60 min web upload,
    **4 GB via API**, ~287 MB iOS / ~72 MB Android in-app, caption **2,200
    UTF-16 runes** via API (in-app reported 4,000), bio 80 chars, no custom
    cover via API (cover = frame pick in Studio).
  - Safe zones: YouTube ~15% top / 35% bottom / 18% right / 4% left;
    IG/FB 14% top / 35% bottom / 6% sides (Meta Ads Guide); TikTok ~top
    15% / bottom 35%. Caveat: these numbers are published for **ads**, not
    organic posts — no platform publishes an organic-post safe zone; treat
    as working guidance, verified by eyeballing one live post per format.
  - **Two gotchas our queue already trips on:** YouTube descriptions are
    capped in **bytes**, not characters — emoji-heavy captions burn 4 bytes
    per emoji; Instagram's hashtag cap is genuinely disputed (30 vs 5).
- **#3 atomic queue writes (S/L).** Write to temp file in same directory,
  `fsync`, then `os.replace` (atomic on POSIX; readers see old-complete or
  new-complete, never partial). C-numbers allocated from a persisted
  counter file (or `max(existing)+1` computed from the on-disk queue under
  a lock), never from in-memory state. Evidence: crash-only design docs
  ("Atomic state transitions… SQLite transactions, atomic file writes…
  append-only logs"; "Idempotent recovery… running recovery multiple times
  produces the same result").
- **#6 queue lock (S/M).** `fcntl.flock` on a lockfile next to the queue;
  each worker writes only to its own job dir; a single queue-writer
  process merges results.
- **#7 yt-dlp hardening (S/M).** yt-dlp's own guide:
  `--socket-timeout 30 --retries infinite --fragment-retries infinite
  --retry-sleep fragment:exp=1:20 --continue` — keep retrying the HTTP
  request and each fragment with exponential backoff (1s, 2s, 4s… capped
  at 20s), resume the partial file. `--throttled-rate 100K` re-extracts
  fresh stream URLs when throttled. `--download-archive archive.txt` +
  `--no-abort-on-error` makes reruns skip completed IDs. Caveat: datacenter
  IPs get throttled harder than residential — flags help, they don't
  guarantee full speed on this VM. Note: the recurring
  `CERTIFICATE_VERIFY_FAILED` is a separate, still-open issue — retry flags
  fix stalls/timeouts, not a broken cert chain.
- **#9 disk guard (S/M).** `shutil.disk_usage` pre-flight check before any
  download/encode; abort if free < estimated need (source size + 2× clip
  size). Download to `*.part`; delete stale `*.part` files older than one
  run on startup.
- **#5 validation gate (S/M).** New `core/validate.py`, called at the end
  of `cut_filltrack`/`cut_portrait` and before queue registration in
  `garage.py`. Checks: 1080×1920, H.264, yuv420p, fps ∈ {24,25,30,60},
  moov-at-front, AAC 48 kHz, file < 300 MB (strictest cap — Instagram
  API), duration 7–30 s (our own rule, inside every platform's cap),
  title ≤100 chars, description ≤5,000 **bytes**, TikTok/IG caption ≤2,200
  chars. Fail = clip stays out of the queue with a reason string.
- **#10 CRF/bitrate (S).** YouTube re-encodes *everything* to VP9/AV1 for
  delivery — "Viewers never see your file. Your upload is just the *source*
  for YouTube's encoder… Too low and you feed it compression artifacts,
  which it then re-compresses." Practical guidance: uploading 20–50% above
  the published numbers leaves headroom; for 1080p60, 15–18 Mbps is the
  practical sweet spot; fast complex motion (gameplay) benefits from
  1.5–2× the recommendation. "Never upload VP9" — independent 2026 VMAF
  testing shows VP9 uploads come out *measurably worse* than H.264 after
  YouTube re-encodes them.
- **#14 dedup (M/M).** Three-stage design: byte hash → 64-bit perceptual
  hash (Hamming distance) → ffmpeg MPEG-7 `signature` filter. Cheap check
  on every upload before transcoding; expensive check only on candidates
  the cheap check surfaced. Layer 1 (free, do first): exact key
  `(source_video_id, start±1s, end±1s)` + SHA-256 of the finished file
  against a small SQLite/JSON index of queued + posted clips. Layer 2
  (only for layer-1 misses sharing a source VOD): pHash on 3 sampled
  frames (start/mid/end), Hamming distance ≤5–8 = near-dup, flag for human
  review. Layer 3 (MPEG-7) is overkill below ~1,000 clips — skip.
- **#15 checkpoints (M/M).** Per-job `state.json` in `jobs/<id>/` with
  stages `scouted → scanned → cut → queued → posted`, written atomically
  (via #3's helper) after each stage; workers skip stages already marked
  complete *and* verify the output file exists and passes #5's validation
  before skipping.
- **#18 face tracking (M/M, 40/60 only).** Full virtual-camera design:
  three-level face detection fallback (MediaPipe → YuNet → Haar Cascade),
  anti-jerk and low-pass filtering, virtual camera as a damped oscillator,
  composition rules (rule of thirds, side bias, eye-level lift, face
  margin), Ken Burns fallback when the face is lost. Active-speaker
  variant: crop follows the face whose mouth is *moving* (MediaPipe Tasks
  FaceLandmarker 478-pt mesh at 5 fps), hard cuts between segments, fixed
  crop within each segment, hysteresis to prevent flicker. YuNet as primary
  is a validated choice — holds turned/profile faces far better. Keep YuNet;
  MediaPipe as fallback only if YuNet misses (don't add the dependency
  pre-emptively). Smoothing first (S); speaker-pick second (M).
- **#23 scene-snap hardening (S/S).** PySceneDetect `detect-content`
  (default, threshold 27, range 15–40) vs `detect-adaptive` (rolling-average
  ratio, better for fast camera movement / mixed pacing); `-m 2.0` minimum
  scene length to drop micro-cuts under 2 s. TransNetV2 is the documented
  fallback for dissolves/fades — **do not add**: extra model, GPU time, and
  our cuts are hard-cut gameplay.
- **#13 mezzanine (M/S today — do not build yet).** One high-quality master
  (CRF 18, never posted) that all variants derive from avoids re-decoding
  the multi-hour source per variant. But all four platforms currently take
  the *same* 1080×1920 file, so there is no second delivery rung. Build
  only when a real second format is requested (e.g. 1:1 feed version).
- **#30 TikTok review rule (S).** "Most TikTok reviews clear on their
  own… If a video is still under review after 24 to 48 hours, do not
  delete it, edit it, or repost it, because that restarts the queue."
  "Give any new video 45 minutes before you assume something is wrong."
  Deleting and reposting the exact same file usually triggers the same
  flag. Also: unlabeled AI content may be withheld — relevant because our
  real clips carry no AI label by design; the review badge on those is
  expected, not a defect. New accounts get checked harder.
- **#31 concurrency cap (M/M).** Evidence is our own incident history
  (runtime restart mid-refill, 3 re-spawns; posts hanging when runs
  overlap). Cap concurrent ffmpeg/Whisper jobs (2–3 CPU-heavy max on the
  VM; GPU work already goes to Modal). Serialized posting rule stays.

## Explicitly rejected / do not build

| Idea | Why not |
|---|---|
| 4K upscale upload ("4K trick" for VP9/AV1 treatment) | Real effect, but 4× file size + longer processing on a tiny VM for 10–30 s clips; 1080p at 12–15 Mbps gets most of the benefit |
| TransNetV2 scene detection | Only helps soft dissolves/fades; our picks are hard-cut gameplay |
| MPEG-7 signature dedup (stage 3) | Overkill below ~1,000 clips; pHash is enough at queue scale 100 |
| Per-platform video variants / mezzanine | All four platforms accept the identical 1080×1920 file today |
| n8n / new orchestration | Already assessed: "not needed… would be a rewrite for no gain" |
| VP9 or AV1 upload to YouTube | 2026 VMAF testing: VP9 uploads come out measurably worse than H.264 after re-encode |
| Sidechain music ducking | Infeasible: single pre-mixed audio track; would need stem separation (Demucs, GPU ML) |
| `arnndn` neural denoiser | Needs external model file; trained on speech, mangles game music/SFX; breaks silence detection |
| `dynaudnorm` | Not a loudness tool; measured to *widen* loudness range (9.8 → 15.1 LU) |
| `alimiter` after loudnorm | Already tested in-repo: measured worse (+0.7 dBTP) |
| Clipping repair (`adeclip`, turning down clipped audio) | Clipping baked into source cannot be repaired; flag-and-drop is honest |
| Lengthening clips to 35–48 s ("algorithmic sweet spot") | Number comes from talking-head/educational Shorts; our best clip is 10 s |
| "Triple-Interrupt Hook Framework" / 0.0–0.4 s macro-zoom prescriptions | Vivid, untestable, no data |
| Virality-score APIs (OpusClip API etc.) | Paid, needs credentials, vendor admits weak performance on music-driven/gameplay content |
| Rainbow/emoji karaoke, bouncing words, one-word-only flashes | 2026 audits call these a "slop tell"; raise cognitive load |
| SwinIR / HAT (transformer still-image upscalers) for video | Still-image models, no temporal handling (flicker); 4× upscaling hits a hard ~27–28 dB PSNR ceiling regardless of architecture |
| Topaz Video AI | $299/yr Personal / $699/yr Pro, desktop Mac/Windows app, not scriptable on Modal GPU; violates no-paid-service constraint. Keep as quality bar for comparison only |

## Suggested implementation order

1. **#1 + #2 + #3 + #4** (scaler fix, safe-zone fix, atomic queue writes,
   payoff-front-load) — all S effort, all touch every clip or prevent a
   known failure class. Do the scaler + spline A/B in one session.
2. **#6 + #7 + #9** (queue lock, yt-dlp hardening, disk guard) — one small
   robustness module + one shared constant.
3. **#5** (QC gate: audio + export-spec validation) — the enforcement
   mechanism for the spec table; generates the data that decides #13 and
   #22.
4. **#8 + #10** (1440p/60 fps fetch, encode unification) — S effort, real
   quality headroom.
5. **#13** (two-pass loudnorm) — only if #5's data shows it tightens the
   spread on VCV clips; our own in-repo measurement says it may not.
6. **#11 + #16** (hook score, template auto-titles) — the ranking/throughput
   layer; reuses signals that already exist.
7. **#12** (muted rescue) — M, in coordination with the copyright-check
   build (see below).
8. **#14 + #15** (dedup layer 1, then checkpoints) — grow in value as the
   queue refills repeatedly.
9. **#17 + #19 + #20 + #21** (conditional denoise, sentence-case captions,
   polish opt-in, compressor/EQ) — small, each needs its own A/B listen or
   view test.
10. **#18** (reframe smoothing + speaker pick) — M, only for the 40/60 A/B.
11. **#25 benchmark** (Real-ESRGAN on Modal T4, one 720p clip) — go/no-go
    gate before any rollout; #28, #32, #33 as experiments only after 1–10
    are live.

## Overlap with the copyright-check build (in progress, separate coordinator)

No code conflicts: this pass edited no code. Two overlaps to coordinate:

- **Muted-audio detection is being built twice unless shared.** The
  copyright module (`src/clipper/core/copyright.py`, in progress) covers
  muted-audio detection as a flag. Audio recommendation #12 (per-segment
  muted rescue) is the complementary *action*: shift the window to a clean
  span instead of only flagging. The YouTube silence×transcript check
  proposed in #12 is the same function the copyright module needs for
  YouTube sources — build it once inside the copyright module and expose
  the result so the scan/triage path can use it as a gate, not just a flag.
- **Dedup (#14) and queue-integrity work (#3, #6, #15)** touch the same
  queue files the copyright module records verdicts in (`clip-queue.md`,
  `package.md`). The copyright module should write its verdict through the
  same atomic queue helper (#3) once it exists, rather than appending to
  the markdown directly.
