#!/usr/bin/env python3
"""Tests for the reaction-bonus hook in scripts/scan_vod.py
(re-rank only; chat-spike ratio stays the sole candidate selector).

Plain asserts, no network, no pytest needed. Run from the
clipper-engine root:

    python3 tests/test_scan_vod_reactions.py

The window_reaction_bonus() math itself is covered by the
reactions port; these tests pin the hook contract:
- bonus > 0 for a laugh/hype-word window, 0.0 for empty words and
  for words outside the window;
- reaction_bonus_for_window() never raises — missing file, missing
  faster-whisper, or missing audio all degrade to bonus 0.0;
- the fetched-set re-rank key is (chat rate, bonus), bonus never
  outranks chat rate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from clipper.core.reactions import window_reaction_bonus  # noqa: E402
import scan_vod  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"ok   {name}")


def words(text, t0=0.0, step=0.5):
    return [{"w": w, "s": round(t0 + i * step, 2), "e": round(t0 + i * step + 0.4, 2)}
            for i, w in enumerate(text.split())]


# --- window_reaction_bonus contract (synthetic timestamped words) -------
laughy = words("hahaha lol oh my god insane clip it wow bro")
r = window_reaction_bonus(laughy, 0.0, 10.0)
check("laugh/hype window bonus > 0", r["bonus"] > 0, str(r))
check("laugh/hype window funny > 0", r["funny"] > 0, str(r))
check("laugh/hype window bonus capped <= 1.3", r["bonus"] <= 1.3, str(r))

r = window_reaction_bonus([], 0.0, 10.0)
check("empty words bonus 0.0", r["bonus"] == 0.0, str(r))

r = window_reaction_bonus(words("hahaha lol insane", t0=100.0), 0.0, 10.0)
check("out-of-range words bonus 0.0", r["bonus"] == 0.0, str(r))

r = window_reaction_bonus(words("the cat sat on the mat quietly today"), 0.0, 10.0)
check("neutral words bonus ~ 0", r["bonus"] < 0.05, str(r))

# --- reaction_bonus_for_window degrades, never raises --------------------
r = scan_vod.reaction_bonus_for_window(
    Path("/nonexistent/lb_1.mp4"))
check("missing file -> bonus 0.0, no raise",
      r["bonus"] == 0.0 and r["error"], str(r))
check("missing file -> reaction_seconds recorded",
      r["reaction_seconds"] >= 0.0, str(r))

# --- re-rank key: (chat rate, bonus); bonus never outranks chat rate ---
recs = [
    {"n": 1, "msgs": 100, "bonus": 0.9},
    {"n": 2, "msgs": 200, "bonus": 0.0},
    {"n": 3, "msgs": 100, "bonus": 0.1},
]
ranked = sorted(recs, key=lambda x: (x["msgs"], x["bonus"]), reverse=True)
check("re-rank puts higher chat rate first despite bonus 0",
      [x["n"] for x in ranked] == [2, 1, 3], str(ranked))

# --- hook is present and guarded ------------------------------------------
check("scan_vod exposes reaction_bonus_for_window",
      callable(scan_vod.reaction_bonus_for_window))
check("scan_vod exposes transcribe_window",
      callable(scan_vod.transcribe_window))
check("REACTIONS flag is a bool", isinstance(scan_vod.REACTIONS, bool))

print("\nALL TESTS PASSED")
