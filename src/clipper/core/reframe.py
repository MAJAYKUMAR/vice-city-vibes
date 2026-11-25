#!/usr/bin/env python3
"""Full-fill 9:16 reframing for the Clipper Engine.

The old default (gtavi cut.py "blur" layout) put a small 16:9 frame in
the middle of a 1080x1920 canvas with blurred bars top and bottom — it
looks like a laptop video, not a mobile Short. This module replaces it
with a real reframe: scale-to-cover and crop the 1080-wide window out
of the scaled frame, following the action, so the game fills the whole
phone screen. No blurred bars, no letterbox.

How the crop is chosen (borrowed from two MIT repos, credited):
  * Face / speaker tracking + dead-zone smoothing: ClipIt
    (shasshhaank/ClipIt, MIT) — reframe_clipit.py, vendored here with
    its YuNet model (core/models/face_detection_yunet_2023mar.onnx). Its
    track() returns a smoothed camera path (x = crop centre, 0..1),
    picks the active speaker by mouth movement, holds the choice for
    >=1s, and only follows meaningful moves (dead zone 0.06).
  * Layout idea: openshorts (mutonby/openshorts, MIT) — TRACK mode
    (MediaPipe/YOLOv8 face tracking) instead of its GENERAL mode
    (blurred background). We do NOT install openshorts itself: it is a
    full Docker web app (8 GB+ RAM, Gemini key, GPU recommended) and
    this VM is 6 GB, CPU-only, no Docker — the reframe idea is what we
    adopted, implemented with ClipIt's lighter tracker.

For GTA gameplay the subject is the game, not the streamer's facecam:
the facecam is a small box in a corner and a 1080-wide crop of a
1280-wide source cannot hold both the facecam and the centre action,
so the crop follows the GAME action (motion centroid in the game area,
facecam box masked out), not the speaker's face. The facecam is
dropped in the vertical cut — same trade-off OpusClip/openshorts
make in TRACK mode. The 16:9 export (clip.py export) keeps the full
frame including the facecam.

IMPORTANT LESSON (measured 2026-11-19 on clip_7): ClipIt's track()
is built for talking-head videos — it follows the SPEAKER's face.
On gameplay that pulls the crop onto the facecam and away from the
game (frame at 13s: CaseOh's face + empty floor; frame at 25s: a
dark wall). So plan_crop() uses game_track() below, NOT track()'s
face path. track() is still used for its scene-cut detection and
dead-zone smoothing helper.

The crop path is downsampled to ~1 keyframe/second and rendered with
an ffmpeg crop expression that lerps between keyframes, so the camera
glides instead of jumping. If tracking fails, it falls back to a
static centre crop.
"""
import json
import math
import sys
from pathlib import Path

from clipper.infra.ssl_fix import gtavi_scripts_dir

sys.path.insert(0, str(gtavi_scripts_dir()))

W, H = 1080, 1920


def scaled_width(sw, sh):
    """Width after scale-to-cover to height 1920 (ffmpeg scale=-2:1920)."""
    w = int(sw * H / sh)
    return w // 2 * 2  # even, like ffmpeg's -2


def game_track(src, start, end, sample_fps=2):
    """Camera path following the GAME action, not the facecam.

    Samples at sample_fps, masks out the streamer's facecam box
    (gtavi facecam.find_cam), and takes the motion centroid of the
    remaining frame. Returns (times, centres_x 0..1, scene_cuts).
    The path is smoothed with ClipIt's dead-zone _smooth so the
    camera holds still on small moves and only follows real action.
    """
    import cv2
    import numpy as np
    from clipper.core.reframe_clipit import _smooth

    # Facecam box (fractions of the frame), if the stream has one.
    cam_box = None
    try:
        sys.path.insert(0, str(gtavi_scripts_dir()))
        from facecam import find_cam
        r = find_cam(str(src), start, end)
        if r.get("mode") in ("cam", "face"):
            cam_box = r["box"]  # [x, y, w, h] fractions
    except Exception:
        cam_box = None

    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start) * 1000)
    step = max(1, int(round(fps / sample_fps)))
    ts, xs, prev, i, scenes = [], [], None, 0, []
    while True:
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
        if t > end or not cap.grab():
            break
        if i % step == 0:
            ok, f = cap.retrieve()
            if not ok:
                break
            small = cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (64, 36))
            if prev is not None:
                if cv2.absdiff(small, prev).mean() > 28:
                    scenes.append(t)  # hard scene change: framing restarts
                diff = cv2.GaussianBlur(cv2.absdiff(small, prev), (21, 21), 0)
                if cam_box is not None:
                    x0 = int(cam_box[0] * 64)
                    y0 = int(cam_box[1] * 36)
                    x1 = int((cam_box[0] + cam_box[2]) * 64)
                    y1 = int((cam_box[1] + cam_box[3]) * 36)
                    diff[y0:y1, x0:x1] = 0  # never follow the facecam
                m = cv2.moments(diff)
                if diff.max() > 12 and m["m00"] > 0:
                    xs.append(m["m10"] / m["m00"] / 64)
                else:
                    xs.append(float("nan"))
                ts.append(t)
            prev = small
        i += 1
    cap.release()
    if not ts:
        return [start, end], [0.5, 0.5], [], cam_box
    ts = np.array(ts)
    xs = np.array(xs, dtype=float)
    cuts = sorted(set(scenes))
    xs = _smooth(ts, xs, cuts, 0.5)
    return ts.tolist(), xs.tolist(), cuts, cam_box


def plan_crop(src, start, end, sample_fps=2):
    """Return the crop plan for a source window.

    Uses game_track() — motion centroid in the game area with the
    facecam masked out — NOT the speaker-face path (see module
    docstring: face tracking pulls the crop off the game).

    Returns dict: keyframes [(t_rel, x_px)], x_static, scenes,
    source size, scaled width. x_px is the ffmpeg crop x offset in
    the scaled frame.
    """
    probe_src = str(src)
    ts, xs, cuts, cam_box = game_track(probe_src, start, end,
                                       sample_fps=sample_fps)
    # Source size via cv2.
    import cv2
    cap = cv2.VideoCapture(probe_src)
    sw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    sh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    cap.release()
    sw_scaled = scaled_width(sw, sh)
    max_x = max(0, sw_scaled - W)

    def to_px(cx):
        # cx is the crop CENTRE as a fraction of the scaled width.
        x = cx * sw_scaled - W / 2
        return int(max(0, min(max_x, x)) // 2 * 2)

    # Downsample the smoothed path to ~1 keyframe per second.
    keys = []
    last_t = -10
    for t, cx in zip(ts, xs):
        rel = t - start
        if rel < 0:
            continue
        if not keys or rel - last_t >= 1.0:
            keys.append((round(rel, 2), to_px(cx)))
            last_t = rel
    if not keys:
        keys = [(0.0, max_x // 2 // 2 * 2)]
    # Anchor the path at t=0 (first motion sample needs a previous
    # frame, so the first keyframe can be ~0.5s in — without this the
    # crop expression extrapolates before the first keyframe).
    if keys[0][0] > 0:
        keys.insert(0, (0.0, keys[0][1]))
    # Always end on the last tracked centre.
    end_x = to_px(xs[-1])
    if keys[-1][0] < (end - start) - 0.5:
        keys.append((round(end - start, 2), end_x))
    xs_px = [to_px(c) for c in xs]
    x_static = sorted(xs_px)[len(xs_px) // 2]
    return {
        "keyframes": keys,
        "x_static": x_static,
        "scaled_width": sw_scaled,
        "max_x": max_x,
        "source": [sw, sh],
        "samples": len(ts),
        "scenes_track": cuts,
        "cam_box": cam_box,
        "duration": round(end - start, 2),
    }


def crop_x_expr(keyframes):
    """ffmpeg crop x expression: lerp between 1-second keyframes.

    keyframes: [(t_rel, x_px), ...] sorted by t. Between keyframes the
    camera glides linearly; before the first / after the last it holds.
    """
    keys = sorted(keyframes)
    if len(keys) == 1:
        return str(keys[0][1])
    # Build from the end: expr = if(lt(t,T1), lerp0, if(lt(t,T2), lerp1, ... Xn))
    expr = str(keys[-1][1])
    for i in range(len(keys) - 2, -1, -1):
        t0, x0 = keys[i]
        t1, x1 = keys[i + 1]
        if t1 <= t0:
            continue
        # lerp from x0 at t0 to x1 at t1, clamped to the segment
        seg = f"({x0}+({x1}-{x0})*(t-{t0})/({t1}-{t0}))"
        expr = f"if(lt(t,{t1}),{seg},{expr})"
    return expr


def fill_filter(src_w, src_h, keyframes, ass_path, fps=30):
    """Full ffmpeg video filter for a full-fill 9:16 cut with captions.

    scale-to-cover 1080x1920, crop with the tracked x path, source fps
    preserved (capped at 60 by the caller — never upsampled), burned
    word-by-word captions from the .ass file. `fps` defaults to 30 for
    callers that did not probe the source.
    """
    sw_scaled = scaled_width(src_w, src_h)
    # ffmpeg's filter-graph parser splits on commas, so the commas
    # inside the if()/lt() crop expression must be escaped as \,
    # or ffmpeg reads "1.19)" as a filter name.
    x_expr = crop_x_expr(keyframes).replace(",", "\\,")
    # Path escaping is openshorts' escape_filter_value (vendored whole in
    # openshorts_ffmpeg_utils.py) — same ":" handling as the old hand
    # escape, plus apostrophes.
    from clipper.core.openshorts_ffmpeg_utils import escape_filter_value
    sub = escape_filter_value(str(ass_path))
    return (f"[0:v]scale={sw_scaled}:{H}:flags=lanczos,"
            f"unsharp=5:5:1.0:5:5:0.5,crop={W}:{H}:{x_expr}:0,"
            f"setsar=1,fps={fps},subtitles='{sub}'[vo]")


if __name__ == "__main__":
    import sys as _s
    plan = plan_crop(_s.argv[1], float(_s.argv[2]), float(_s.argv[3]))
    print(json.dumps(plan, indent=1))
