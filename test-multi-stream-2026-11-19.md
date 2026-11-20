# Real multi-stream test — 10 clips from 4 different streamers (2026-11-19/20)

TEST ONLY. Nothing here was posted to Facebook / Instagram / YouTube, no
campaign was joined. Clipping other creators' streams is for testing the
tool only — posting needs a live paid campaign that explicitly allows that
creator (Vice City Vibes Rule 2/11). Credit format per clip, if ever posted
under such a campaign: "All clips are from [streamer]. Go support the OG:
[link]" — per Ajay, 2026-11-19, following the Jiddyclips model.

## Why this test exists

The first real test (test-10-clips-2026-11-19.md) cut all 10 clips from ONE
12-min CaseOh VOD — same house scene, and clip_5 and clip_7 looked/sounded
almost identical. Ajay: "start your real test" — from many streams.

## Sources (dashboard 2026-11-19 23:54 UTC: 14 YouTube live + 19 Twitch live)

| Source | Streamer | VOD | Section used | Link |
|---|---|---|---|---|
| 1 | kaicenat (Twitch, 32,541 live) | v2932305259 "PLAYING GTA 6 FOR THE FIRST TIME" (34.9 h) | 02:00:00–02:12:00 | https://www.twitch.tv/kaicenat |
| 2 | stableronaldo (Twitch, 20,983 live) | v2933798318 "GTA 6 BEST POV" (8.1 h) | 01:30:00–01:42:00 | https://www.twitch.tv/stableronaldo |
| 3 | theburntpeanut (Twitch, 60,403 live) | v2934030540 "GTA 6 DAY 2 SUPER CARS" (4.7 h) | 00:20:00–00:32:00 | https://www.twitch.tv/theburntpeanut |
| 4 | zackrawrr (Twitch, 52,424 live) | v2933941381 "UNBANNED GTA 6 RELEASE STREAM" (6.0 h) | 01:00:00–01:12:00 | https://www.twitch.tv/zackrawrr |

All 12:00, 1280×720, 60 fps, ~256–282 MB each, in `test-real/multi/`.

### Sources that FAILED and were dropped

- **xqc** (Twitch, 34,721 live, VOD v2934155295, 00:30–00:42): the whole
  12 minutes is his DESKTOP — OBS settings, USB-C / capture-card
  troubleshooting, zero gameplay. Every frame checked. Dropped, no clips.
- **ishowspeed** (Twitch): his latest Twitch VODs are Fortnite, not GTA 6.
  Not used.
- **theburntpeanut, second section (02:00–02:12)**: download stuck at
  0 bytes for 12+ min (bad/flaky segment on that VOD); killed. The
  00:20 section worked on retry (first try failed with a 403 + ffmpeg
  segment error, second try at a different offset worked).
- **Scout trap, found again**: `clip.py scout` defaults to Whisper
  "small" — on this CPU it took 6+ min per 12-min file and looked hung.
  Restarted with `CLIPPER_WHISPER_MODEL=tiny`: ~2 min per source.

## The 10 clips (plus 1 BLOCKED cut = 11 cuts total)

All full-fill 9:16 filltrack, 1080×1920, 30 fps, captions + hook,
-14 LUFS, check.py PASS on all 11. Garage: 25 clips total, 6 of these
approved, BLOCK refusal verified (see below).

| # | Clip file | Source | Window (in 12-min file) | Dur. | Score | Safety | Hook |
|---|---|---|---|---|---|---|---|
| 1 | `jobs/kaicenat-12min-a98c77a5/edits/clip_1_fill.mp4` | kaicenat | 1:02–1:21 | 19.2 s | 3.48 | SAFE | BEACH FIGHT |
| 2 | `jobs/kaicenat-12min-a98c77a5/edits/clip_4_fill.mp4` | kaicenat | 4:32–5:02 | 30.0 s | 3.95 | REVIEW | WHY ARE YOU CHASING ME |
| 3 | `jobs/kaicenat-12min-a98c77a5/edits/clip_5_fill.mp4` | kaicenat | 7:27–7:46 | 18.8 s | 2.68 | SAFE | DEAD BODIES |
| 4 | `jobs/kaicenat-12min-a98c77a5/edits/clip_8_fill.mp4` | kaicenat | 11:15–11:35 | 20.5 s | 1.62 | SAFE | NEW LOOK |
| 5 | `jobs/stableronaldo-12min-a4b9e1e3/edits/clip_5_fill.mp4` | stableronaldo | 8:06–8:33 | 26.7 s | 2.36 | SAFE | MEET AT THE FUN FAIR |
| 6 | `jobs/stableronaldo-12min-a4b9e1e3/edits/clip_6_fill.mp4` | stableronaldo | 9:15–9:40 | 24.3 s | 2.78 | SAFE | TAKE OFF WITH THE MONEY |
| 7 | `jobs/stableronaldo-12min-a4b9e1e3/edits/clip_7_fill.mp4` | stableronaldo | 10:36–11:06 | 30.0 s | 4.54 | SAFE | CREEPY PLACE |
| 8 | `jobs/burntpeanut-12min-5143c054/edits/clip_3_fill.mp4` | theburntpeanut | 2:32–2:48 | 15.3 s | 3.77 | SAFE | MISSION CAR |
| 9 | `jobs/burntpeanut-12min-5143c054/edits/clip_4_fill.mp4` | theburntpeanut | 4:54–5:16 | 21.8 s | 2.74 | SAFE | MISSION DONE |
| 10 | `jobs/zackrawrr-12min-36966931/edits/clip_8_fill.mp4` | zackrawrr | 10:35–11:04 | 29.8 s | 2.28 | SAFE | PLAYING TODAY |
| — | `jobs/zackrawrr-12min-36966931/edits/clip_2_fill.mp4` | zackrawrr | 1:54–2:21 | 27.2 s | 4.27 | **BLOCK** | TAKE A SHOWER |

### Per-clip notes (source + why picked + safety)

1. **kaicenat clip_1, beach fight** — Jason confronts NPCs on the beach,
   "I don't want to fight you, bro". Outdoor, bright, faces + sand + sea
   all in frame. Best clip of the ten. Credit: "All clips are from
   kaicenat. Go support the OG: https://www.twitch.tv/kaicenat".
2. **kaicenat clip_4, NPC chase — REVIEW** — NPC chases Jason down the
   street, "Why are you chasing me, bro?" Safety REVIEW = elevated skin
   0.63 only (beach, shirtless NPCs), NO hard NudeNet hits. Human check
   before posting. Captions carry heavy profanity ("fuck", "racist") —
   that is the streamer's own speech, not ours; it is in the burned
   captions.
3. **kaicenat clip_5, dead bodies** — "What do I do with these dead
   bodies?" + cops. Truck on the road, clear visual change.
4. **kaicenat clip_8, new look — WEAK** — barbershop/clothing change.
   Mid-frame is the in-game MAP screen, not gameplay. Cut is fine,
   moment is weak. Replacement cut for the BLOCKED zackrawrr clip.
5. **stableronaldo clip_5, fun fair** — tow truck + car on the road,
   "old fun fair across from Mo's". Clear, bright, captions render.
6. **stableronaldo clip_6, take off with the money** — street driving,
   "I'm just gonna take off with the money". Good.
7. **stableronaldo clip_7, creepy place** — "She's creepy… exactly the
   kind of place". Street + sunset, captions render. Good.
8. **theburntpeanut clip_3, mission car — OK/dark** — night, body on the
   ground, mission talk. Short (15.3 s), dark, no caption word on the
   checked frame (word-by-word gap). Usable, not great.
9. **theburntpeanut clip_4, mission done — OK/dark** — night garage,
   "successful mission, smoke in my car". Dark, talky, no visual payoff.
10. **zackrawrr clip_8, playing today — WEAK** — mid-frame is his
    X/Twitter react screen, not gameplay. The engine scored his talking,
    not a game moment. Weakest of the ten.
- **zackrawrr clip_2, shower — BLOCKED, NEVER POST.** Jason showering
  in-game, NudeNet BUTTOCKS 0.26–0.35 on 3 frames + keyword "shower".
  Cut for format proof only. `garage.py approve()` **refused it
  (verified)**. This is the second shower-nudity catch in two tests
  (CaseOh clip_10 was the first) — GTA 6 has shower scenes, and the
  loudest-moment scorer keeps finding them. The filter works; the
  scorer still needs a "shower/bathroom" down-weight.

### Deliberately NOT cut

- **kaicenat pick 7 (score 9.11, highest of all four sources)** — a
  profanity/racial rant ("dumb bitch", "racist at me"). Highest score,
  worst clip. Skipped on judgement, not on score.
- **stableronaldo picks 1–4** — strip-club / naked NPC scenes
  ("I'm gonna be naked doing this", "sin right through my underwear").
  Not cut at all — the safety risk was visible before cutting.
- **theburntpeanut picks 1, 5–8** — mostly the in-game MAP screen
  ("Lose the cops") with the streamer talking over it. No visual payoff.
- **xqc, all 8 candidates** — desktop/settings, zero gameplay.

## Visual QC (mid-frame of every cut, `qc/multi/final-sheet1.jpg` + `final-sheet2.jpg`)

- Full-fill 9:16 confirmed visually on all 11 — no blurred bars, no
  letterbox, gameplay fills the phone screen. Captions + hook render on
  9 of 11 checked frames (gaps on burntpeanut clip_3 and zackrawrr
  clip_8 are normal word-by-word caption gaps / a react frame).
- Facecam: present in a corner on kaicenat/stableronaldo/zackrawrr
  clips; the game-track crop keeps the GAME centred, not the facecam.
- No garbled-text failures, no hands/controls errors (no driving
  close-ups in this set to fail Rule 9b on).

## Honest verdict

- **Engineering: 11/11 cut, 11/11 check.py PASS, 0 technical failures.**
  Scout → hype → cut → check → Garage works across 4 different
  streamers, 2 platforms' VODs (all Twitch here — YouTube live is still
  bot-gated without a login, which we do not use).
- **Variety: FIXED vs the last test.** 4 streamers, 4 different games
  states (beach fight, street chase, truck/driving, night mission,
  clothing shop), day + night, indoor + outdoor. No two clips look
  alike the way clip_5/clip_7 did. The "same clip" problem is gone.
- **As viral moments: 6/10 genuinely good, 4/10 weak.** Good: kai1,
  kai4, kai5, stable5, stable6, stable7. Weak: kai8 (map), burnt3
  (dark, no payoff), burnt4 (dark garage talk), zack8 (react screen,
  not gameplay). **Still no crash / chase / fail payoff in any clip** —
  random 12-min slices of launch-day marathon streams are mostly story
  dialogue, map pauses, setup, and reacts. The engine finds the
  loudest talky moment, not a visual payoff, because the payoff often
  isn't in the slice. To get crashes/chases, the scout needs to target
  them (e.g. pick VOD sections around a known crash, or score visual
  motion, not just transcript loudness).
- **Safety: filter caught 1 BLOCK (shower) and flagged 1 REVIEW
  (beach skin).** Both correct calls. The repeated shower pattern is
  now a known scorer weakness, not a filter failure.

## Timings (this VM, CPU only)

| Step | Time |
|---|---|
| Dashboard (live + trends) | ~60 s |
| VOD section downloads (4 × 12 min, parallel) | ~5 min each batch |
| Scout / transcribe, tiny model (4 sources) | ~2 min each |
| Cut 11 clips (filltrack + captions + hook + safety) | ~100–170 s each, ~30 min total |
| check.py × 11 + QC frames + Garage | ~5 min |
| **Total, incl. failed xqc/burntpeanut2 attempts** | **~1.5 h wall time** |

Cuts dominate, same as the first test. Transcription is not the
bottleneck once the tiny model is used (the "small" default cost ~30
wasted minutes — set `CLIPPER_WHISPER_MODEL=tiny`).

## Files

- Report: `test-multi-stream-2026-11-19.md` (this file)
- Sources: `test-real/multi/{kaicenat,xqc,stableronaldo,burntpeanut,zackrawrr}-12min.mp4`
  (xqc kept for the record: desktop only, no clips)
- Jobs: `jobs/{kaicenat-12min-a98c77a5, stableronaldo-12min-a4b9e1e3,
  burntpeanut-12min-5143c054, zackrawrr-12min-36966931}/` —
  `candidates.md`, `transcript.json`, `edits/clip_*_fill.mp4` +
  `.ass` + `.json` (safety verdict inside each `.json`)
- QC: `qc/multi/sheet2-{burntpeanut,kaicenat,xqc,stableronaldo,zackrawrr}.jpg`
  (source frames), `qc/multi/final-sheet1.jpg`, `final-sheet2.jpg`
  (mid-frame of every cut), `qc/multi/final-{name}.jpg`
- Garage: `garage/clips.json` + `garage/index.html` (25 clips total)
