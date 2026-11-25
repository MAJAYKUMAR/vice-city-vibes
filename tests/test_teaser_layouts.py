#!/usr/bin/env python3
"""Tests for the OpenClip port: teaser-open cut variant
(src/clipper/core/teaser.py, port of server/edl.mjs +
server/pipeline.mjs + server/render.mjs) and facecam layout
presets (src/clipper/core/layouts.py, port of
web/src/cutlayout.mjs + web/src/logic.mjs stackPanels/CARD).

Plain asserts, no network, no pytest needed. Run from the
clipper-engine root:

    python3 tests/test_teaser_layouts.py

Expected values below are computed by hand from the OpenClip
source math, not copied from the implementation under test.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipper.core import layouts, teaser  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"ok   {name}")


# ---------------------------------------------------------------- teaser
# edl.mjs cold-open block, default S=1.6, payoff p, clip [14.22, 43.19]:
#   b = min(end, p + 1.6*0.4) = p + 0.64; a = max(start, b - 1.6).
# Case 1: payoff = recorded peak 30.0 -> b=30.64, a=29.04 (1.60 s).
w = teaser.cold_open_window(14.22, 43.19, 30.0)
check("teaser window default S=1.6", w == (29.04, 30.64), f"got {w}")

# Payoff resolution order — edl.mjs payoffTime():
# coldOpenAt > first replay > peakAt > 65% through.
check("payoff: coldOpenAt wins",
      teaser.payoff_time(14.22, 43.19, peak=30.0, cold_open_at=20.0,
                         replay_at=25.0) == 20.0)
check("payoff: replay beats peak",
      teaser.payoff_time(14.22, 43.19, peak=30.0, replay_at=25.0) == 25.0)
check("payoff: peak used when no override",
      teaser.payoff_time(14.22, 43.19, peak=30.0) == 30.0)
# 65% through [14.22, 43.19]: 14.22 + 0.65*28.97 = 33.0505.
check("payoff: 65% fallback",
      abs(teaser.payoff_time(14.22, 43.19) - 33.0505) < 1e-9,
      f"got {teaser.payoff_time(14.22, 43.19)}")
# Clamped into [start, end].
check("payoff: clamped to end",
      teaser.payoff_time(14.22, 43.19, peak=99.0) == 43.19)
check("payoff: clamped to start",
      teaser.payoff_time(14.22, 43.19, peak=1.0) == 14.22)

# b/a clamping at the clip edges.
# Payoff 0.3 s before the end: b = min(43.19, 42.89+0.64)=43.19,
# a = max(14.22, 43.19-1.6)=41.59, then b = min(43.19, 41.59+1.6)=43.19.
w = teaser.cold_open_window(14.22, 43.19, 42.89)
check("teaser window clamped at end", w == (41.59, 43.19), f"got {w}")
# Payoff 0.2 s after the start: b = min(43.19, 14.42+0.64)=15.06,
# a = max(14.22, 15.06-1.6)=14.22, b = min(43.19, 14.22+1.6)=15.82.
w = teaser.cold_open_window(14.22, 43.19, 14.42)
check("teaser window clamped at start", w == (14.22, 15.82), f"got {w}")

# S clamping: edl.mjs num(v,1,3,1.6) — S=5 -> 3, S=0.2 -> 1.
# S=3, payoff 30: b=31.2, a=28.2.
check("teaser S clamped to max 3",
      teaser.cold_open_window(14.22, 43.19, 30.0, seconds=5) == (28.2, 31.2))
# S=1, payoff 30: b=30.4, a=29.4.
check("teaser S clamped to min 1",
      teaser.cold_open_window(14.22, 43.19, 30.0, seconds=0.2) == (29.4, 30.4))

# < 0.5 s rejected: a 0.4 s clip cannot hold a teaser.
check("teaser <0.5s rejected",
      teaser.cold_open_window(10.0, 10.4, 10.2) is None)
# Exactly 0.5 s is kept (>= 0.5 in edl.mjs).
check("teaser exactly 0.5s kept",
      teaser.cold_open_window(10.0, 10.5, 10.25) == (10.0, 10.5))

# Segments: teaser first, then the FULL main pass (repeat, not cut-out).
segs = teaser.teaser_segments(14.22, 43.19, peak=30.0)
check("segments: cold + full main",
      segs == [{"kind": "cold", "start": 29.04, "end": 30.64},
               {"kind": "main", "start": 14.22, "end": 43.19}],
      f"got {segs}")
check("teaser adds 1.6s",
      abs(teaser.teaser_duration(segs) - 1.6) < 1e-9)
# No teaser when the window is rejected -> main only.
check("segments: main only when window rejected",
      teaser.teaser_segments(10.0, 10.4, peak=10.2)
      == [{"kind": "main", "start": 10.0, "end": 10.4}])
# Captions follow the main pass only.
check("captions: main only",
      teaser.main_segments(segs) == [segs[1]])

# pipeline.mjs auto-enable: payoff >= 25 s in.
check("auto-enable: late payoff (>=25s)",
      teaser.payoff_is_late(14.22, 40.0) is True)
check("auto-enable: early payoff skipped",
      teaser.payoff_is_late(14.22, 30.0) is False)
check("auto-enable: played length wins over offset",
      teaser.payoff_is_late(14.22, 40.0, played_up_to_peak=10.0) is False)

# render.mjs marker: visual flash only, at the teaser boundary.
f = teaser.teaser_flash_filter(1.6)
check("flash: visual only, at boundary",
      "eq=brightness=0.30" in f and "between(t,1.600,1.820)" in f,
      f"got {f}")
check("flash: empty without boundary",
      teaser.teaser_flash_filter(None) == "")

# ---------------------------------------------------------------- layouts
# cutlayout.mjs PRESETS.shorts — exact geometry.
p = {x["id"]: x for x in layouts.PRESETS["shorts"]}
check("preset cam-top geometry",
      p["cam-top"]["camOut"] == [0, 0, 1, 0.34]
      and p["cam-top"]["gameOut"] == [0, 0.34, 1, 0.66])
check("preset game-top geometry",
      p["game-top"]["camOut"] == [0, 0.66, 1, 0.34]
      and p["game-top"]["gameOut"] == [0, 0, 1, 0.66])
check("preset cam-corner geometry",
      p["cam-corner"]["camOut"] == [0.05, 0.04, 0.44, 0.17]
      and p["cam-corner"]["gameOut"] == [0, 0, 1, 1])

# Panel pixels tile the 1080x1920 frame exactly.
panels = layouts.panel_pixels("cam-top")
check("cam-top panels tile frame",
      panels == {"cam": (0, 0, 1080, 652), "game": (0, 652, 1080, 1268)},
      f"got {panels}")
panels = layouts.panel_pixels("game-top")
check("game-top panels tile frame",
      panels == {"game": (0, 0, 1080, 1268), "cam": (0, 1268, 1080, 652)},
      f"got {panels}")
panels = layouts.panel_pixels("cam-corner")
check("cam-corner panels",
      panels == {"cam": (54, 76, 476, 326), "game": (0, 0, 1080, 1920)},
      f"got {panels}")

# Caption safe zone: cap_y=1080 lands inside the game panel for all.
for pid in ("cam-top", "game-top", "cam-corner"):
    check(f"caption y=1080 in game panel ({pid})",
          layouts.caption_y_in_game_panel(pid))

# pixelAspect: cam-top panel on 1080x1920 -> (1*1080)/(0.34*1920) = 1.6544.
check("pixel_aspect cam-top",
      abs(layouts.pixel_aspect([0, 0, 1, 0.34], 1080, 1920) - 1.6544) < 1e-3)

# boxOf: aspect 16/9 in a 1280x720 source -> full frame [0,0,1,1].
check("box_of 16:9 in 16:9 source",
      layouts.box_of(16 / 9, 1280, 720) == [0.0, 0.0, 1.0, 1.0])
# boxOf: 9:16 aspect in 1280x720 -> w=(720*9/16)/1280=0.3164, centred.
b = layouts.box_of(9 / 16, 1280, 720)
check("box_of 9:16 in 16:9 source",
      b == [0.3418, 0.0, 0.3164, 1.0], f"got {b}")

# refit: a 16:9 box reshaped to the cam-top panel aspect keeps its
# centre (0.5, 0.5) and about the same area.
r = layouts.refit([0, 0, 1, 1], layouts.pixel_aspect([0, 0, 1, 0.34], 1080, 1920),
                  1280, 720)
check("refit keeps centre",
      abs(r[0] + r[2] / 2 - 0.5) < 1e-4 and abs(r[1] + r[3] / 2 - 0.5) < 1e-4,
      f"got {r}")

# coverSource: 1280x720 source into a 1080x652 panel (aspect 1.6564):
# sw/sh=1.7778 > pa -> n = 720*1.6564 = 1192.6, x=(1280-1192.6)/2=43.7.
c = layouts.cover_source(0, 0, 1280, 720, 1080, 652)
check("cover_source crops sides",
      abs(c[0] - 43.7) < 0.6 and c[1] == 0 and abs(c[2] - 1192.6) < 0.6
      and c[3] == 720, f"got {c}")
# coverSource: 1280x720 into 1080x1920 (aspect 0.5625):
# sw/sh > pa -> n = 720*0.5625 = 405, x=(1280-405)/2=437.5.
c = layouts.cover_source(0, 0, 1280, 720, 1080, 1920)
check("cover_source full-frame 9:16",
      abs(c[0] - 437.5) < 0.6 and c[1] == 0 and abs(c[2] - 405) < 0.6
      and c[3] == 720, f"got {c}")

# applyPreset: default layout reshaped to cam-top keeps the cam box
# centred on its old centre and the game box full-frame-ish.
base = layouts.default_layout("shorts", 1280, 720)
applied = layouts.apply_preset(base, p["cam-top"], "shorts", 1280, 720)
check("apply_preset panels",
      applied["camOut"] == [0, 0, 1, 0.34]
      and applied["gameOut"] == [0, 0.34, 1, 0.66])

# stackPanels (logic.mjs): 16:9 cam box -> share = 1080/1.7778/1920
# = 0.3164 -> top = round(1920*0.3164/2)*2 = 608, bottom 1312.
sp = layouts.stack_panels([0.02, 0.02, 0.30, 0.17], source_aspect=16 / 9)
# cam aspect = (0.30*16/9)/0.17 = 3.137 -> share = 1080/3.137/1920 = 0.1793
# -> clamped to STACK_SPLIT_MIN 0.25 -> top = 480, bottom 1440.
check("stack_panels clamps to min 0.25",
      sp == {"top": 480, "bottom": 1440}, f"got {sp}")
sp = layouts.stack_panels([0, 0, 0.5, 0.5], source_aspect=16 / 9)
# cam aspect = (0.5*16/9)/0.5 = 1.7778 -> share = 1080/1.7778/1920 = 0.3164
# -> top = round(1920*0.3164/2)*2 = 608, bottom 1312.
check("stack_panels 16:9 cam",
      sp == {"top": 608, "bottom": 1312}, f"got {sp}")
sp = layouts.stack_panels([0, 0, 0.5, 0.5], split=0.9, source_aspect=16 / 9)
check("stack_panels split clamps to max 0.55",
      sp == {"top": 1056, "bottom": 864}, f"got {sp}")

# defaultGameBox: bottom 1312, 16:9 -> w = (1080/1312)/1.7778 = 0.4629.
gb = layouts.default_game_box(1312)
check("default_game_box",
      abs(gb[2] - 0.4629) < 1e-3 and abs(gb[0] - 0.2686) < 1e-3,
      f"got {gb}")

# cardCamBox (logic.mjs CARD): a 16:9 cam box reshaped to 880/544
# (1.6176): aspect 1.7778 > target -> narrower, centred: nw =
# 0.5625*1.6176/1.7778 = 0.5118, nx = (1-0.5118)/2 = 0.2441.
cc = layouts.card_cam_box([0, 0, 1, 0.5625])
check("card_cam_box narrows wide cam",
      abs(cc[2] - 0.5118) < 1e-3 and abs(cc[0] - 0.2441) < 1e-3,
      f"got {cc}")

# layout_filter builds a graph that ends in [vo] and stacks panels.
vf = layouts.layout_filter("cam-top", 1280, 720)
check("layout_filter cam-top graph",
      "vstack=inputs=2" in vf and vf.endswith("[vo]")
      and "crop=" in vf and "scale=1080:652" in vf
      and "scale=1080:1268" in vf, f"got {vf}")
vf = layouts.layout_filter("cam-corner", 1280, 720)
check("layout_filter cam-corner graph",
      "overlay=54:76" in vf and "scale=476:326" in vf
      and vf.endswith("[vo]"), f"got {vf}")
vf = layouts.layout_filter("game-top", 1280, 720)
check("layout_filter game-top graph",
      "vstack=inputs=2" in vf and "[gamepanel][campanel]" in vf,
      f"got {vf}")

print("\nALL TESTS PASSED")
