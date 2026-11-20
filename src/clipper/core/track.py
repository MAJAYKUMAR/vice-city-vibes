#!/usr/bin/env python3
"""Shared tracked-reframe helpers for the Clipper Engine (OpusClip parity).

One small, self-contained home for the crop math both cut paths use:

  * CPU path (clip.py cut_filltrack via reframe.py) — the full game-track
    plan lives in reframe.py; this module is the lightweight core it
    shares semantics with, NOT a replacement for it.
  * GPU path (api/modal_standalone.py, the Modal T4 cutter) — the Modal
    image has no gtavi tree, no facecam module and no reframe.py, so the
    cutter imports ONLY this file. It must stay that way: stdlib at
    import time, cv2/numpy imported lazily inside the compute functions
    (the Modal image ships opencv-python-headless + numpy; a plain
    `import track` must never fail for lack of cv2).

What "tracked" means here: a full-fill 9:16 crop (1080x1920 target).
The source is scaled to cover 1920px height, and a 1080-wide window
slides horizontally along a smoothed camera path (x keyframes, ~1/s,
linearly interpolated by ffmpeg). If tracking fails or no track is
available, callers fall back to a static centre crop — a cut must
never fail just because tracking failed.
"""
import math

W, H = 1080, 1920


def scaled_width(sw, sh):
    """Width after scale-to-cover to height 1920 (ffmpeg scale=-2:1920)."""
    w = int(sw * H / sh)
    return w // 2 * 2  # even, like ffmpeg's -2


def centre_x(sw, sh):
    """Static centre-crop x offset in the scaled frame (even)."""
    return max(0, scaled_width(sw, sh) - W) // 2 // 2 * 2


def to_px(cx, sw_scaled):
    """Crop-centre fraction (0..1) -> even crop x offset, clamped."""
    max_x = max(0, sw_scaled - W)
    x = cx * sw_scaled - W / 2
    return int(max(0, min(max_x, x)) // 2 * 2)


def normalize_keyframes(keyframes, duration, sw, sh):
    """Validate/normalise caller-supplied crop keyframes.

    Accepts the shapes callers actually send:
      * [[t_rel, x_px], ...]            (clip.py crop_keyframes JSON)
      * [{"t": t_rel, "x": x_px}, ...]
      * {"keyframes": [...]}             (wrapped)
      * a single number                  (static x for the whole clip)
      * {"x": x_px}                      (static x)
    Returns [(t_rel, x_px), ...] sorted by t, anchored at t=0, ending
    at `duration`, x clamped to [0, scaled_width - 1080] and even —
    or None if the input is unusable (caller must fall back to centre).
    """
    if keyframes is None:
        return None
    if isinstance(keyframes, dict):
        if "keyframes" in keyframes:
            keyframes = keyframes["keyframes"]
        elif "x" in keyframes:
            keyframes = [(0.0, keyframes["x"])]
        else:
            return None
    if isinstance(keyframes, (int, float)):
        keyframes = [(0.0, keyframes)]
    sw_scaled = scaled_width(sw, sh)
    max_x = max(0, sw_scaled - W)
    keys = []
    try:
        for k in keyframes:
            if isinstance(k, dict):
                t, x = float(k["t"]), float(k["x"])
            else:
                t, x = float(k[0]), float(k[1])
            if not (math.isfinite(t) and math.isfinite(x)):
                continue
            t = max(0.0, min(float(duration), t))
            x = int(max(0, min(max_x, x)) // 2 * 2)
            keys.append((round(t, 2), x))
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not keys:
        return None
    keys.sort()
    # Drop duplicate times (keep the last), anchor at t=0, end at duration.
    dedup = []
    for t, x in keys:
        if dedup and dedup[-1][0] == t:
            dedup[-1] = (t, x)
        else:
            dedup.append((t, x))
    keys = dedup
    if keys[0][0] > 0:
        keys.insert(0, (0.0, keys[0][1]))
    if keys[-1][0] < float(duration) - 0.5:
        keys.append((round(float(duration), 2), keys[-1][1]))
    return keys


def crop_x_expr(keyframes):
    """ffmpeg crop x expression: lerp between ~1-second keyframes.

    keyframes: [(t_rel, x_px), ...] sorted by t. Between keyframes the
    camera glides linearly; before the first / after the last it holds.
    Same construction as reframe.crop_x_expr.
    """
    keys = sorted(keyframes)
    if len(keys) == 1:
        return str(keys[0][1])
    expr = str(keys[-1][1])
    for i in range(len(keys) - 2, -1, -1):
        t0, x0 = keys[i]
        t1, x1 = keys[i + 1]
        if t1 <= t0:
            continue
        seg = f"({x0}+({x1}-{x0})*(t-{t0})/({t1}-{t0}))"
        expr = f"if(lt(t,{t1}),{seg},{expr})"
    return expr


def _escape_filter_value(path):
    """Escape a file path for use inside an ffmpeg filter value.

    Same ":" handling as the vendored openshorts escape_filter_value,
    plus backslashes and apostrophes. Kept local so this module has
    no dependency on openshorts_ffmpeg_utils (unavailable on Modal).
    """
    s = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return s


def build_fill_filter(sw, sh, keyframes, ass_path):
    """Full ffmpeg video filter for a full-fill 9:16 cut with captions.

    scale-to-cover 1080x1920, crop with the tracked x path, fps 30,
    burned word-by-word captions from the .ass file. `keyframes` must
    already be normalised (see normalize_keyframes); a single
    [(0.0, x)] keyframe is a static crop (centre fallback).
    """
    sw_scaled = scaled_width(sw, sh)
    # ffmpeg's filter-graph parser splits on commas, so the commas
    # inside the if()/lt() crop expression must be escaped as \,
    # or ffmpeg reads "1.19)" as a filter name.
    x_expr = crop_x_expr(keyframes).replace(",", "\\,")
    sub = _escape_filter_value(str(ass_path))
    return (f"[0:v]scale={sw_scaled}:{H},crop={W}:{H}:{x_expr}:0,"
            f"setsar=1,fps=30,subtitles='{sub}'[vo]")


def _smooth(ts, v, cuts, default):
    """Fill gaps, then smooth per shot with a dead zone so the camera
    holds still on small moves and only follows real action. Vendored
    from reframe_clipit._smooth (same 0.06 dead zone, 0.3 ease) minus
    the face-room clamp, which only applies to face tracking."""
    import numpy as np

    if np.all(np.isnan(v)):
        return np.full_like(ts, default, dtype=float)
    good = ~np.isnan(v)
    idx = np.arange(len(v))
    v = np.interp(idx, idx[good], v[good])
    out = np.empty_like(v)
    bounds = [0] + [int(np.searchsorted(ts, c)) for c in sorted(cuts)] + [len(v)]
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b <= a:
            continue
        seg = v[a:b]
        med = np.convolve(np.pad(seg, 3, mode="edge"), np.ones(7) / 7, mode="valid")
        cam = med[0]
        for k in range(len(med)):
            if abs(med[k] - cam) > 0.06:  # dead-zone: only meaningful moves
                cam += (med[k] - cam) * 0.3  # ease toward target
            out[a + k] = cam
    return out


def compute_track(src, start, end, sample_fps=2):
    """Lightweight game-action camera path for a source window.

    Same idea as reframe.game_track but without the gtavi facecam
    dependency (the Modal image has no gtavi tree, so no facecam box
    is masked out — the crop may occasionally follow facecam motion;
    caller-supplied tracks from the CPU path do mask it). Samples at
    `sample_fps` on 64x36 grayscale frames, takes the motion centroid
    of the frame difference, smooths with the ClipIt dead-zone
    smoother. Cheap on GPU time: ~2 fps x 64x36 for a <=60s window.

    Returns (times, centres_x 0..1, scene_cuts). Raises on failure —
    callers must catch and fall back to centre crop.
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start) * 1000)
    step = max(1, int(round(fps / sample_fps)))
    ts, xs, prev, i, scenes = [], [], None, 0, []
    try:
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
                    m = cv2.moments(diff)
                    if diff.max() > 12 and m["m00"] > 0:
                        xs.append(m["m10"] / m["m00"] / 64)
                    else:
                        xs.append(float("nan"))
                    ts.append(t)
                prev = small
            i += 1
    finally:
        cap.release()
    if not ts:
        return [start, end], [0.5, 0.5], []
    ts = np.array(ts)
    xs = np.array(xs, dtype=float)
    cuts = sorted(set(scenes))
    xs = _smooth(ts, xs, cuts, 0.5)
    return ts.tolist(), xs.tolist(), cuts


def plan_crop(src, start, end, sample_fps=2):
    """Return the crop plan for a source window (GPU-cutter edition).

    Mirrors reframe.plan_crop's return shape (keyframes, x_static,
    scaled_width, max_x, source, samples, scenes_track, duration) but
    runs compute_track() above instead of reframe.game_track(), so it
    works with no gtavi imports. Keyframe times are RELATIVE to `start`.
    Raises on failure — callers must catch and fall back to centre.
    """
    import cv2

    ts, xs, cuts = compute_track(src, start, end, sample_fps=sample_fps)
    cap = cv2.VideoCapture(str(src))
    sw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    sh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    cap.release()
    sw_scaled = scaled_width(sw, sh)
    max_x = max(0, sw_scaled - W)

    def to_px_fn(cx):
        return to_px(cx, sw_scaled)

    keys = []
    last_t = -10
    for t, cx in zip(ts, xs):
        rel = t - start
        if rel < 0:
            continue
        if not keys or rel - last_t >= 1.0:
            keys.append((round(rel, 2), to_px_fn(cx)))
            last_t = rel
    if not keys:
        keys = [(0.0, max_x // 2 // 2 * 2)]
    if keys[0][0] > 0:
        keys.insert(0, (0.0, keys[0][1]))
    end_x = to_px_fn(xs[-1])
    if keys[-1][0] < (end - start) - 0.5:
        keys.append((round(end - start, 2), end_x))
    xs_px = [to_px_fn(c) for c in xs]
    x_static = sorted(xs_px)[len(xs_px) // 2]
    return {
        "keyframes": keys,
        "x_static": x_static,
        "scaled_width": sw_scaled,
        "max_x": max_x,
        "source": [sw, sh],
        "samples": len(ts),
        "scenes_track": cuts,
        "duration": round(end - start, 2),
    }
