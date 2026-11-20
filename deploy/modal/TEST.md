# Modal GPU cutter — test + deploy checklist

Vice City Vibes Clipper Engine. GPU cutting on Modal (on demand), Fly watcher
(CPU, always-on) finds the moments. This file is the test plan and the deploy
checklist only — it does not deploy anything, and it contains no token or
secret. Never put a token/secret in any file in this folder.

## Status (2026-11-19, checked)

- **App code EXISTS now.** Written 2026-11-19 ~19:35 CST at
  `src/clipper/api/modal_app.py` (see "Expected interface" below — this file was
  updated to match it). It imports the clipper pipeline (`clip.py` /
  `reframe.py` / `safety.py` / vendored gtavi scripts), loads offline
  (`py_compile` OK, `import modal_app` OK, app `vice-city-vibes-clipper`,
  GPU T4, Whisper small). It has NOT been run or deployed yet — the
  `modal run` / `modal deploy` commands below are still untested.
- **Profile:** `maddukuriajaykumar05` is the active profile and the token
  is saved in `~/.modal.toml` (verified via `modal profile list`,
  2026-11-19 ~19:24 CST). The list shows workspace **"Unknown (profile
  misconfigured)"** — the token is saved locally, but Modal's server
  could not be reached from this VM to resolve the workspace.
- **gRPC warning (verified, not a guess):** `modal token set --verify`
  failed on this VM with `Error: Could not connect to the Modal server`
  after ~60 s. A control test with dummy credentials gave the identical
  error, while plain HTTPS to the same host is reachable (returns 415 —
  Modal is gRPC-only). So this is a **gRPC transport failure through
  this VM's egress proxy, not a wrong-token error**. The token was saved
  with `--no-verify` instead. **Consequence: a `modal deploy` / `modal
  run` from this VM may fail the same way. If it does, deploy from a
  machine where Modal connects (e.g. Ajay's laptop, where
  `python3 -m modal setup` browser auth also works), do not keep
  retrying the same failing command here.**
- **Credits (reported 2026-11-19, re-verify before a big run):**
  $30.00 credits left / $0.00 used, plus $42.50 workspace usage left —
  $72.50 free total. Plenty for testing and a good run of real clips.
- **Fly side (for context, not this file's job):** watcher files
  (`deploy/fly/fly.toml`, `deploy/fly/Dockerfile`, `src/clipper/app/watcher.py`) are ready in
  `clipper-engine/`; Fly deploy was in progress separately.

## What the Modal cutter must do

Same pipeline as the CPU `clip.py`, just on GPU. It must NOT change the
output spec — a GPU cut must be indistinguishable in format from a CPU
cut, so a clip can be posted from either path.

- Input: a scouted job dir (`jobs/<id>/` with `candidates.json`,
  `transcript.json`) — scout/transcribe stays on CPU or GPU, whichever
  the app wires; the cut itself is the GPU part.
- Cut: full-fill 9:16 **filltrack** (scale-to-cover + game-action tracked
  crop, `reframe.py`). **No blurred bars / no letterbox** — the old
  gtavi "blur" layout (small 16:9 frame + blurred bars) is opt-in only
  (`--layout blur`) and is a FAIL for a Short.
- Captions: word-by-word burned in + hook box, same as gtavi `cut.py`
  (`build_ass`). Audio: -14 LUFS loudnorm.
- Safety: `safety.py` verdict recorded in the clip's `.json` —
  SAFE / REVIEW / BLOCK (see below). A BLOCK clip must never be
  approvable (`garage.py approve()` refuses it — verified on CPU).
- 16:9 export: `clip.py export` — 1920×1080 landscape version.
- Never records, never posts, never joins a campaign on its own
  (Vice City Vibes Rule 2/11). Test sources are the local VOD files
  already in `test-real/` — never a live stream capture for a test.

## Expected interface

The app should mirror `clip.py` exactly, so the same job dir works on
both paths:

The written app is `src/clipper/api/modal_app.py` (not `app.py`). It takes a
source **URL**, not a local job dir — a remote GPU function cannot see
the caller's local files, so the Fly watcher passes a VOD URL:

```text
modal run src/clipper/api/modal_app.py --source-url "<twitch/youtube VOD or mp4 URL>" --pick 1 --hook "BEACH FIGHT"
modal run src/clipper/api/modal_app.py --source-url "<url>" --start 120 --end 139.2 --hook "BEACH FIGHT"   # manual window
```

- `cut_clip` (GPU function) = scout + cut, returns metadata dict +
  mp4 in the `vice-city-vibes-clips` Volume. No separate `safety` /
  `export` entrypoints: safety runs inside every cut (`cut_filltrack`,
  verdict in the clip `.json`) and the 16:9 export is a CPU
  `clip.py export` step, not a GPU stage.
- `web` (FastAPI via `@modal.asgi_app`) = `POST /cut`, `GET /health`,
  `GET /download/<job_id>/<file>` — the stub the Fly watcher calls.
- For parity tests, pick numbers are per-scout-run and will NOT match
  the CPU job's pick numbers. Use `--start/--end` with the CPU clip's
  source window (from its `clip_N_fill.json`: `start`/`end`) to cut
  the exact same window on GPU.

## CPU baseline (measured, from the two test reports — the bar GPU must beat)

Source for both baselines: a 12-min, 1280×720, 60 fps VOD section.

### Test 1 — single source, 10 clips (test-10-clips-2026-11-19.md)

CaseOh VOD v2932628190, 01:00–01:12. Total **~38 min** source → 10
finished vertical clips + 10 landscape exports, CPU only:

| Step | CPU time |
|---|---|
| Dashboard (live + trends) | ~54 s |
| VOD section download | ~2 min 20 s |
| Scout / transcribe, faster-whisper **tiny** (1,441 words) | ~59 s |
| Hype Meter (12 candidates) | <1 s |
| Cut 10 clips (vertical + captions + hook) | ~17 min (**~100 s/clip**) |
| check.py on all 10 | ~2 s total |
| 16:9 exports × 10 | ~16 min (**~96 s/clip**) |

### Test 2 — multi-stream, 10 clips from 4 streamers (test-multi-stream-2026-11-19.md)

kaicenat / stableronaldo / theburntpeanut / zackrawrr, 12 min each.
Total **~1.5 h wall time** including failed attempts (xqc desktop-only
source, a stuck burntpeanut download). Cuts again dominate:

| Step | CPU time |
|---|---|
| Dashboard | ~60 s |
| VOD downloads (4 × 12 min, parallel) | ~5 min per batch |
| Scout / transcribe, tiny (4 sources) | ~2 min each |
| Cut 11 clips (filltrack + captions + hook + safety) | **100–170 s each, ~30 min total** |
| check.py × 11 + QC frames + Garage | ~5 min |

### Traps the baseline already found (do not re-learn them on GPU)

- **Whisper model:** `clip.py scout` defaults to model "small" — on CPU
  that took **6+ min per 12-min file** and looked hung. Always set
  `CLIPPER_WHISPER_MODEL=tiny` on CPU (~59 s–2 min per source). On GPU
  the app deliberately uses "small" (clip.py default, `WHISPER_MODEL`
  in `modal_app.py`): GPU transcribe is fast, and "small" is better at
  hype-word detection. If GPU transcribe is ever slow, flip it to
  "tiny" — do not leave "small" in place unmeasured.
- **Live capture is the weak link, not cutting:** YouTube bot-gates
  yt-dlp without a login (never use cookies/logins), Twitch HLS URLs
  expire mid-record (~4 min). VOD download works. A Modal test must
  use a local VOD file / job dir, not a live capture.
- **Moment quality ≠ cut quality:** the scorer finds the loudest
  transcript moment, not a visual payoff. On slow story dialogue it
  finds talky moments. GPU speed does not fix that — judge clips on
  the same honest scale as the CPU tests (Test 1: ~4/10 genuinely
  good; Test 2: 6/10 genuinely good, 4/10 weak).

## How to test the Modal GPU cut locally (`modal run`)

Run from `clipper-engine/`, with the Modal venv's `modal` CLI.
Do this in order — stop at the first failing step and record the exact
error before moving on.

### Step 0 — pre-flight (do not skip)

```bash
~/workspace/gta-test/clipper-engine/modal-venv/bin/modal profile list
# Expect: • maddukuriajaykumar05 active. Workspace may still show
# "Unknown (profile misconfigured)" — see the gRPC warning above.
# If the profile is NOT listed, the token is not saved: re-run
# `modal token set ... --no-verify` with the saved ID + secret pair
# (never paste either value into a file or chat log).
```

### Step 1 — smoke test: 1 known-good clip

Use the best CPU clip as the reference — kaicenat beach fight,
job `jobs/kaicenat-12min-a98c77a5`, pick 1, hook `BEACH FIGHT`
(19.2 s, SAFE, ★★★★★ on CPU).

```bash
time ~/workspace/gta-test/clipper-engine/modal-venv/bin/modal run src/clipper/api/modal_app.py \
  --source-url "https://www.twitch.tv/videos/2932305259" --start <cpu-clip-start> --end <cpu-clip-end> --hook "BEACH FIGHT"
# start/end = the CPU clip's source window from
# jobs/kaicenat-12min-a98c77a5/edits/clip_1_fill.json (do not guess them).
# The VOD URL above is kaicenat v2932305259; the CPU test used its
# 02:00-02:12 section, so the manual window must fall inside it.
```

Pass = a file is produced in `jobs/kaicenat-12min-a98c77a5/edits/`
(or the app's stated output dir) **and** it matches the expected
outputs below. Record the wall time — that single number is the
headline result of this step.

### Step 2 — parity check: GPU cut vs the CPU cut of the same pick

Compare the GPU file against the CPU reference
`jobs/kaicenat-12min-a98c77a5/edits/clip_1_fill.mp4` (19.2 s):

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate,duration \
  -of default=noprint_wrappers=1 <gpu-clip.mp4>
ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of default=noprint_wrappers=1 <gpu-clip.mp4>
# Extract frames at 1s / 5s / 10s and look at them:
ffmpeg -y -v error -i <gpu-clip.mp4> -vf "select='eq(n\,30)+eq(n\,150)+eq(n\,300)'" -vsync 0 /tmp/modal-parity-%d.jpg
```

Pass = all of: 1080×1920, 30 fps, has audio, duration within ±1 s
of the CPU clip, gameplay fills the whole frame (no blurred bars,
no letterbox), hook + word-by-word captions visible, no garbled
text, no Rule 9b failure (hands/controls/physics — n/a on this clip,
but check the frame anyway). Any format difference = FAIL, even if
the file "plays fine".

### Step 3 — safety check: the shower clip must BLOCK

Run the safety step on the known-BLOCK pick — zackrawrr clip_2
(job `jobs/zackrawrr-12min-36966931`, pick 2, hook `TAKE A SHOWER`,
27.2 s). CPU verdict: **BLOCK** (NudeNet BUTTOCKS 0.26–0.35 on
3 frames + "shower" keyword). The CaseOh equivalent is clip_10
(job `jobs/caseoh-vod-12min-481864bc`, pick 10 — also BLOCK).

```bash
~/workspace/gta-test/clipper-engine/modal-venv/bin/modal run src/clipper/api/modal_app.py \
  --source-url "https://www.twitch.tv/videos/2933941381" --start <cpu-clip-start> --end <cpu-clip-end> --hook "TAKE A SHOWER"
# start/end = zackrawrr clip_2's source window from its clip_2_fill.json.
# There is no separate safety entrypoint — safety runs inside the cut
# and the verdict is in the returned metadata + clip .json.
```

Pass = verdict **BLOCK** in the clip `.json`, and
`garage.py approve()` refuses the file (verify that refusal, don't
assume it). A SAFE verdict here = the safety step is broken on GPU,
stop and fix it before any other test. REVIEW on this clip is also
a FAIL — it must be BLOCK.

### Step 4 — 10-clip batch timing (the number that decides if GPU is worth it)

Re-cut the 10 Test-1 picks on GPU (CaseOh job
`jobs/caseoh-vod-12min-481864bc`, picks 1, 3, 4, 5, 7, 8, 9, 10, 11, 12
— the 10 that were cut on CPU) and time the whole batch, cuts only
(exclude download/scout — those are not the GPU question).

```bash
time (for p in 1 3 4 5 7 8 9 10 11 12; do
  ~/workspace/gta-test/clipper-engine/modal-venv/bin/modal run src/clipper/api/modal_app.py \
    --source-url "https://www.twitch.tv/videos/2932628190" --pick $p --hook "TEST"
done)
# NOTE: --pick numbers here are the GPU scout's own candidates, not the
# CPU job's pick numbers — the windows will differ from the CPU batch.
# For a like-for-like batch, re-cut each CPU pick by its source window
# (--start/--end from each clip_N_fill.json) instead.
```

Then run `check.py` on all 10 GPU clips and record PASS/FAIL per clip.
Pass = 10/10 check.py PASS **and** the batch is clearly faster than
the CPU ~17 min for the same 10 cuts. See "Timing expectations" —
record the actual numbers, do not claim a speedup you did not measure.

## Expected outputs (every GPU cut, every time)

| Check | Expected |
|---|---|
| Resolution | **1080×1920** (ffprobe width=1080, height=1920) |
| Frame rate | 30 fps |
| Layout | Full-fill filltrack — gameplay fills the whole phone screen, **no blurred bars, no letterbox** |
| Duration | 7–30 s (the pick window), within ±1 s of the CPU cut of the same pick |
| Audio | Present, -14 LUFS loudnorm |
| Captions | Word-by-word burned in, hook box at top, no garbled text |
| check.py | **PASS** on all checks (gtavi `check.py`) |
| Safety verdict | In the clip `.json`: SAFE / REVIEW / BLOCK (see thresholds below) |
| 16:9 export (if run) | 1920×1080, same captions/hook |
| Garage | Clip registers; `approve()` accepts SAFE, refuses BLOCK, REVIEW needs a human look |

## Safety verdicts (from `safety.py` — same thresholds on GPU as CPU)

- **BLOCK:** genitalia/anus hit ≥ 0.20, or buttocks/breast hits on ≥ 3
  frames, or any hard hit ≥ 0.50, or keyword + skin > 0.50.
  Cut for format proof only. `garage.py approve()` **refuses** it.
  **Never post.** Known BLOCKs to test against: CaseOh clip_10
  (shower, BUTTOCKS 0.39 ×4 frames + GENITALIA 0.25), zackrawrr
  clip_2 (shower, BUTTOCKS 0.26–0.35 ×3 frames + "shower").
- **REVIEW:** any hard hit at all, or skin > 0.50, or keyword hit.
  Human look required before posting. Known REVIEW: kaicenat clip_4
  (NPC chase — beach skin 0.63 only, no hard hits; captions carry the
  streamer's profanity) and CaseOh clip_5 (Jason shirtless, shorts on
  — NudeNet false-positive on a bare male back). Skin alone never
  blocks — it only adds REVIEW.
- **SAFE:** everything else. Only SAFE clips can auto-post.
- If the NudeNet model cannot load on the GPU image, the verdict caps
  at REVIEW (never SAFE) and the failure must be recorded — a run
  that silently reports SAFE with no model loaded is a FAIL.

## Timing expectations vs CPU (targets to measure, not promises)

CPU is ~100 s per cut and ~96 s per 16:9 export — both plain FFmpeg
re-encodes, and they dominate every run (~38 min for 10 clips +
landscapes in Test 1; ~30 min of cuts in Test 2). Transcription is
already fast on CPU with the tiny model (~59 s–2 min per 12-min
source) and is **not** the bottleneck; do not judge GPU on
transcription time.

| Stage (10-clip batch) | CPU (measured) | GPU target to verify |
|---|---|---|
| Scout / transcribe, 12-min source, tiny | ~59 s–2 min | Measure — expect similar or faster; if slower, note why |
| Cut 10 clips | ~17 min (~100 s/clip) | **Target: well under 100 s/clip, batch under ~10 min.** Record actuals |
| 16:9 exports × 10 | ~16 min (~96 s/clip) | **Target: well under 96 s/clip.** Record actuals |
| check.py × 10 | ~2 s total | ~2 s (not a GPU stage) |
| **Total, 10 clips + landscapes** | **~38 min** | **Target: under ~15 min.** Record actuals; if it is not clearly faster, say so plainly — GPU is only worth it if the measured batch time proves it |

Things GPU does **not** speed up (do not count them in the comparison):
VOD download (~2–5 min), dashboard (~60 s), and — if live capture is
ever used — the record time itself, which is real-time by definition.

## Deploy checklist

Do NOT deploy until every box is checked. If any box fails, stop and
record the exact error — do not deploy around it.

- [x] **App code exists:** `src/clipper/api/modal_app.py` is written, imports
  the clipper pipeline (`clip.py` / `reframe.py` / `safety.py` /
  vendored gtavi scripts), and loads locally (`py_compile` + import
  OK, 2026-11-19). The underlying code path (`clip.py`) was
  smoke-tested on CPU; the Modal wrapper itself has NOT run yet.
- [ ] **Deploy command (from `clipper-engine/modal/`):**
  `modal deploy src/clipper/api/modal_app.py` — record the deploy URL / app ID it
  returns. (Not `modal/app.py` — that filename does not exist.)
- [ ] **Test Steps 1–3 pass on `modal run`:** 1 known-good clip
  matches the CPU reference, parity check passes, shower clip BLOCKs.
- [ ] **Step 4 batch timed and recorded:** actual GPU batch time
  written into this file's test log (below) — not just "it felt faster".
- [ ] **Profile active:** `modal profile list` shows
  `• maddukuriajaykumar05`. (Verified 2026-11-19: active, workspace
  "Unknown (profile misconfigured)".)
- [ ] **Token saved:** `~/.modal.toml` exists with the
  `maddukuriajaykumar05` profile. No token/secret in any repo file,
  log, or chat message.
- [ ] **gRPC connect check:** run one `modal run` of a trivial
  function first. If it fails with `Could not connect to the Modal
  server`, **do not deploy from this VM** — deploy from a machine
  where Modal connects (Ajay's laptop, via `python3 -m modal setup`
  browser auth if needed), and record that here.
- [ ] **Credits check:** confirm the free credits still cover the
  deploy + a test batch ($72.50 free total reported 2026-11-19).
  A deploy that burns paid credits without Ajay's word is a stop.
- [ ] **Verify against the deployed app:** after deploy, run a
  `modal run` against the *deployed* app, not just the local file.
- [ ] **Fly handshake (if wired):** the Fly watcher calls Modal only
  via its API, on demand, when a moment is found. Watcher stays
  scout-only — it never records, cuts, or posts by itself (Rule 2/11).
- [ ] **No secrets committed:** `git status` / GitHub push of
  `modal/` contains no token, secret, `.modal.toml`, or credential.
  (Repo: `MAJAYKUMAR/vice-city-vibes`.)
- [ ] **Ajay's word:** Modal deploy is a new external deployment —
  report the test results (Steps 1–4 numbers) to Ajay and get his
  go-ahead before `modal deploy`, same as any other deploy.

## Test log

| Date | Step | Result | Time | Notes |
|---|---|---|---|---|
| 2026-11-19 | CPU Test 1 (baseline) | 10/10 PASS | ~38 min total | CaseOh, see test-10-clips-2026-11-19.md |
| 2026-11-19 | CPU Test 2 (baseline) | 11/11 cut, 11/11 PASS | ~1.5 h wall | 4 streamers, see test-multi-stream-2026-11-19.md |
| 2026-11-19 | App written | `src/clipper/api/modal_app.py` loads offline | — | py_compile + import OK; not run/deployed yet |
| — | Modal Step 1 (smoke) | _not run_ | — | |
| — | Modal Step 2 (parity) | _not run_ | — | |
| — | Modal Step 3 (safety) | _not run_ | — | |
| — | Modal Step 4 (batch) | _not run_ | — | |
| — | `modal deploy` | _not run_ | — | Blocked on gRPC check above |
