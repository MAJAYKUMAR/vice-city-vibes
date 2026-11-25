# Hook instructions — wiring teaser.py + layouts.py into clip.py / reframe.py

Status 2026-11-25 ~21:00 UTC: the modules are written, unit-tested
(44 checks, `tests/test_teaser_layouts.py`) and ffmpeg-verified
(see /tmp/teaser_layout_verify.py — test encodes only, in
/tmp/teaser_test/). They are NOT yet wired into the cut path:
`clip.py` and `reframe.py` were both last modified 20:44 UTC today
by the max-quality build (CRF 17 + 60 fps), so per the conflict
rule no hook edits were made. Wire these when that build lands.

## 1. Teaser-open in `src/clipper/core/clip.py`

`cut_filltrack()` already computes `peak` (recorded scan peak, else
loudness peak) and runs `frontload_window()` at step 1b. The teaser
is an OPTION alongside that slide — never the default, never both
at once (front-load slides the window; the teaser repeats the
payoff up front).

Hook point — in `cut_filltrack`, right after the front-load block
(the `print(f"front-load: ...")` line), add:

```python
# Teaser-open (clipper.core.teaser, port of OpenClip edl.mjs):
# opt-in only, mutually exclusive with the front-load slide above.
teaser_on = <new flag, e.g. a cut_filltrack(teaser=False) parameter
             threaded from run_cut()/CLI --teaser>
segments = None
if teaser_on:
    from clipper.core import teaser as _teaser
    segments = _teaser.teaser_segments(s0, e0, peak=peak)
    # segments = [{"kind": "cold", "start": a, "end": b},
    #             {"kind": "main", "start": s0, "end": e0}]
```

Encode change — replace the single-input ffmpeg call in
`cut_filltrack` (the `common.run([... "-ss", f"{s0:.2f}", "-to",
f"{e0:.2f}", "-i", src, "-filter_complex", vf, ...])` block) with
a two-input concat when `segments` has a cold segment:

```python
# inputs: -ss <cold.start> -to <cold.end> -i src
#         -ss <main.start> -to <main.end> -i src
# filter: [0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[cutv][cuta]
#         then the existing vf chain on [cutv] (reframe.fill_filter
#         is written for [0:v] — retarget its first filter to [cutv]),
#         plus teaser.teaser_flash_filter(cold_duration) at the
#         teaser->main boundary (visual flash only; we have no SFX
#         track, so the rewind sound from OpenClip render.mjs is
#         deliberately omitted).
# captions: build the .ass from main_segments(segments) only —
# the teaser shows the hook text as a label, never word captions
# (edl.mjs retimeWords: captions follow the main pass only).
```

Also record in the clip json: `"teaser": segments` and
`"teaser_seconds": teaser.teaser_duration(segments)`.
Auto-enable rule (OpenClip pipeline.mjs): enable the teaser when
the payoff is late — `teaser.payoff_is_late(s0, peak)` (>= 25 s
in) — but only as an explicit opt-in decision by Ajay, not
automatic; the pipeline rule is provided, not wired to auto-fire.

CLI: add `--teaser` to the `cut` and `manual` subparsers and
thread it through `run_cut` -> `cut_filltrack`.

## 2. Layout presets in `src/clipper/core/reframe.py` / `clip.py`

`layouts.layout_filter(preset_id, src_w, src_h, cam_box, game_box,
ass_path, fps)` is a drop-in replacement for
`reframe.fill_filter(...)` — same `[vo]` output contract, same
lanczos + unsharp chain, same subtitles handling.

Hook point — in `clip.run_cut`, next to the `layout == "filltrack"`
dispatch:

```python
if layout in ("cam-top", "game-top", "cam-corner"):
    # cam_box: reframe.plan_crop(src, s0, e0)["cam_box"] (facecam
    # detection) when present, else None (layouts falls back to the
    # defaultLayout top-left box, reshaped to the panel).
    # game_box: None (full frame, reshaped to the panel).
    cut_filltrack(..., layout=layout)  # with layout_filter used
    # in place of reframe.fill_filter inside cut_filltrack.
```

Cleanest: inside `cut_filltrack`, replace the single line
`vf = reframe.fill_filter(sw, sh, plan["keyframes"], ass, fps=fps)`
with:

```python
if layout in ("cam-top", "game-top", "cam-corner"):
    from clipper.core import layouts as _layouts
    vf = _layouts.layout_filter(layout, sw, sh,
                                cam_box=plan.get("cam_box"),
                                game_box=None, ass_path=ass, fps=fps)
else:
    vf = reframe.fill_filter(sw, sh, plan["keyframes"], ass, fps=fps)
```

This needs `layout` threaded into `cut_filltrack` (it currently
takes no layout argument; `run_cut` dispatches before calling it).
Record `"layout": layout` in the clip json (already done for
filltrack) and `"cam_box": plan.get("cam_box")`.

Caption safe zone: `cap_y=1080` is inside the game panel for all
three presets (proven in the unit tests via
`layouts.caption_y_in_game_panel`); no caption change needed.
The cam-corner cam panel (54,76,476,326) sits clear of the
caption line and of the bottom watermark zone.

## 3. Deliberately NOT wired (surveyed and skipped)

- Cam Split reaction zooms (stack.mjs OPEN_ZOOM=1.2,
  REACTION=1.5): surveyed only, per task.
- Glow Card rounded mask / glowing frame PNGs: geometry ported
  (`layouts.CARD`, `card_cam_box`); the overlay art does not exist
  here, so a "glow-card" render would be plain panels.
- Caption styles (server/captions.mjs, presets/captions.json):
  surveyed and skipped — Anton / Barlow Condensed / Montserrat /
  Luckiest Guy / Work Sans are all absent (fc-list: DejaVu + Noto
  only). No font-free piece mapped cleanly onto
  `src/clipper/core/captions.py`.
