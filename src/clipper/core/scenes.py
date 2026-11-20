#!/usr/bin/env python3
"""Scene cuts via PySceneDetect (Breakthrough/PySceneDetect 0.7.3, MIT)
— adopted 2026-11-19 in the final build.

Used to snap a clip's start/end to the nearest real scene boundary
(within 1.5 s) so a cut never opens mid-shot, and to report how many
scene cuts fall inside a clip window.

Speed note (measured): PySceneDetect on full-res 720p60 took 6+ min
with no output on this VM. It runs on a 640-wide / 15 fps proxy of
just the clip window instead — detection in ~5 s.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from clipper.infra.ssl_fix import patch_yt_dlp  # noqa: E402  (ffmpeg path helper)
_common = patch_yt_dlp()


def scene_cuts(src, start, end, threshold=27.0):
    """Scene-cut times (seconds, absolute) inside [start, end]."""
    from scenedetect import ContentDetector, detect
    with tempfile.TemporaryDirectory() as td:
        proxy = str(Path(td) / "proxy.mp4")
        subprocess.run([_common.tool("ffmpeg"), "-y", "-v", "error",
                        "-ss", f"{max(0.0, start - 2):.2f}",
                        "-to", f"{end + 2:.2f}", "-i", str(src),
                        "-vf", "scale=640:-2,fps=15",
                        "-an", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "28", proxy], check=True)
        scenes = detect(proxy, ContentDetector(threshold=threshold),
                        show_progress=False)
        base = max(0.0, start - 2)
        cuts = []
        for s in scenes:
            t = base + s[0].get_seconds()
            if start - 0.1 <= t <= end + 0.1:
                cuts.append(round(t, 2))
        return cuts


def snap(start, end, cuts, max_shift=1.5, min_dur=7.0):
    """Snap start/end to the nearest scene cut within max_shift seconds.
    Never shortens the clip below min_dur. Returns (start, end, notes)."""
    notes = []
    ns, ne = start, end
    if cuts:
        cs = min(cuts, key=lambda c: abs(c - start))
        if abs(cs - start) <= max_shift and (end - cs) >= min_dur:
            ns = cs
            notes.append(f"start snapped {start:.2f} -> {cs:.2f} (scene cut)")
        ce = min(cuts, key=lambda c: abs(c - end))
        if abs(ce - end) <= max_shift and (ce - ns) >= min_dur:
            ne = ce
            notes.append(f"end snapped {end:.2f} -> {ce:.2f} (scene cut)")
    return ns, ne, notes


if __name__ == "__main__":
    src, s0, e0 = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    cuts = scene_cuts(src, s0, e0)
    print(cuts)
    print(snap(s0, e0, cuts))
