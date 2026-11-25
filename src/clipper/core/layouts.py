#!/usr/bin/env python3
"""Facecam layout presets for the Clipper Engine (1080x1920 Shorts).

Port of OpenClip's cut-layout geometry (MIT licensed,
/tmp/open-clip-editor):
  * web/src/cutlayout.mjs — ``PRESETS.shorts`` panel geometry
    (boxes are [x, y, w, h] shares), ``boxOf`` / ``refit`` /
    ``coverSource`` / ``pixelAspect`` / ``applyPreset`` /
    ``defaultLayout``. Ported exactly; the ``video`` (16:9)
    presets are included for completeness but the Shorts presets
    are the target.
  * web/src/logic.mjs (via server/stack.mjs ``stackPlan``) —
    ``stackPanels``: the cam panel height derived from the cam
    box aspect, clamped to [.25, .55] of the frame height
    (STACK_SPLIT_MIN/MAX); ``defaultGameBox``; the Glow Card
    ``CARD`` geometry + ``cardCamBox`` (geometry only — the
    rounded-mask / glowing-frame PNG overlays do not exist in
    this engine, so the card renders as plain panels, not as a
    glow card; see the note on ``CARD``).
  * presets/edits.json — the ``cam-split`` preset (framing
    ``stack``, captionPosition ``middle``, captionReveal
    ``word``) is the cam-top stacked layout; the ``glow-card``
    preset (framing ``stack``, style ``comic-word``,
    captionPosition ``seam``, captionReveal ``phrase``) is the
    CARD geometry above. Their caption styles need Anton /
    Luckiest Guy / Work Sans, which are NOT installed here
    (fc-list: DejaVu + Noto only), so no caption styling was
    ported — only the layout geometry. Caption positions stay
    governed by our own safe-zone fix (``captions.build_ass``
    ``cap_y=1080``); ``caption_y_in_game_panel`` proves that
    y=1080 still lands inside the gameplay panel for every
    preset below.

SURVEY ONLY, deliberately not built (per task): the Cam Split
reaction zooms (stack.mjs OPEN_ZOOM=1.2, REACTION=1.5) and the
caption styles in server/captions.mjs + presets/captions.json.

These presets sit NEXT TO the existing ``reframe.fill_filter``
(full-fill tracked crop). They do not replace it: fill is still
the default; a layout preset is chosen per clip when the source
has a facecam worth keeping on screen.

Wiring (pending — reframe.py is actively edited by the
max-quality build, see HOOKS.md in this directory):
``clip.run_cut`` should accept ``layout="cam-top"`` /
``"game-top"`` / ``"cam-corner"`` and call ``layout_filter``
instead of ``reframe.fill_filter`` for those values, with the
cam box from ``reframe.plan_crop``'s ``cam_box`` (facecam
detection) or a manual box.
"""
import math

W, H = 1080, 1920

OUTPUT = {"shorts": (1080, 1920), "video": (1920, 1080)}

# cutlayout.mjs PRESETS — exact values, boxes are [x, y, w, h] shares.
PRESETS = {
    "shorts": [
        {"id": "cam-top", "name": "Camera on top",
         "camOut": [0, 0, 1, 0.34], "gameOut": [0, 0.34, 1, 0.66],
         "camOnTop": True},
        {"id": "game-top", "name": "Gameplay on top",
         "camOut": [0, 0.66, 1, 0.34], "gameOut": [0, 0, 1, 0.66],
         "camOnTop": True},
        {"id": "cam-corner", "name": "Camera in a corner",
         "camOut": [0.05, 0.04, 0.44, 0.17], "gameOut": [0, 0, 1, 1],
         "camOnTop": True},
    ],
    "video": [
        {"id": "cam-corner", "name": "Camera in a corner",
         "camOut": [0.70, 0.05, 0.27, 0.27], "gameOut": [0, 0, 1, 1],
         "camOnTop": True},
        {"id": "side", "name": "Side by side",
         "camOut": [0, 0, 0.36, 1], "gameOut": [0.36, 0, 0.64, 1],
         "camOnTop": True},
        {"id": "cam-big", "name": "Big camera, small game",
         "camOut": [0, 0, 1, 1], "gameOut": [0.62, 0.05, 0.34, 0.34],
         "camOnTop": False},
    ],
}

# logic.mjs: cam panel height is clamped to [.25, .55] of the frame.
STACK_SPLIT_MIN = 0.25
STACK_SPLIT_MAX = 0.55

# logic.mjs CARD — Glow Card geometry, pixels of the 1080x1920 frame.
# The rounded mask + glowing frame are PNG overlays in OpenClip
# (assets/overlays/cam-card-*.png); they do not exist here, so CARD
# is ported as geometry only. captionY=672 is where glow-card
# captions sit ("seam" — right under the card).
CARD = {"cam": {"x": 100, "y": 48, "w": 880, "h": 544},
        "game": {"y": 530, "h": 1210}, "captionY": 672}


def _clamp(v, lo, hi):
    return min(hi, max(lo, v))


def _r4(v):
    return round(v, 4)


def round_box(box):
    """cutlayout.mjs ``roundBox``."""
    return [_r4(v) for v in box]


def pixel_aspect(box, w, h):
    """Pixel aspect (width / height) of a box inside a w x h frame."""
    return (box[2] * w) / (box[3] * h)


def box_of(aspect, sw, sh, scale=1.0, cx=0.5, cy=0.5):
    """Largest box of `aspect` fitting the source, scaled, centred
    on (cx, cy), kept inside — cutlayout.mjs ``boxOf``."""
    w, h = 1.0, (sw / aspect) / sh
    if h > 1:
        h, w = 1.0, (sh * aspect) / sw
    w *= scale
    h *= scale
    return round_box([_clamp(cx - w / 2, 0, 1 - w),
                      _clamp(cy - h / 2, 0, 1 - h), w, h])


def refit(box, aspect, sw, sh):
    """Re-shape a source box to `aspect`, keeping its centre and
    about the same area, inside the frame — cutlayout.mjs ``refit``."""
    area = box[2] * sw * box[3] * sh
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    wp = math.sqrt(area * aspect)
    hp = math.sqrt(area / aspect)
    shrink = min(1, sw / wp, sh / hp)
    wp *= shrink
    hp *= shrink
    w, h = wp / sw, hp / sh
    return round_box([_clamp(cx - w / 2, 0, 1 - w),
                      _clamp(cy - h / 2, 0, 1 - h), w, h])


def cover_source(sx, sy, sw, sh, dw, dh):
    """Where cover-scaling puts a source rectangle into a panel:
    the centred part of the source with the panel's shape —
    cutlayout.mjs ``coverSource``. All args in pixels; returns
    [x, y, w, h] in source pixels."""
    pa = dw / dh
    if sw / sh > pa:
        n = sh * pa
        return [sx + (sw - n) / 2, sy, n, sh]
    n = sw / pa
    return [sx, sy + (sh - n) / 2, sw, n]


def cover_rect(box, panel_aspect, source_aspect=16 / 9):
    """The part of a box that fills a panel of `panel_aspect`
    (cover scaling crops the box's longer side, centred) —
    logic.mjs ``coverRect``. Boxes are shares."""
    x, y, w, h = box
    a = (w * source_aspect) / h
    if a > panel_aspect:
        nw = w * panel_aspect / a
        return [x + (w - nw) / 2, y, nw, h]
    nh = h * a / panel_aspect
    return [x, y + (h - nh) / 2, w, nh]


def get_preset(preset_id, fmt="shorts"):
    """A PRESETS entry by id (ValueError on unknown id)."""
    for p in PRESETS[fmt]:
        if p["id"] == preset_id:
            return dict(p)
    raise ValueError(f"unknown layout preset: {preset_id!r} "
                     f"(format {fmt!r})")


def apply_preset(layout, preset, fmt, sw, sh):
    """A preset applied to a layout: new panels, and each source
    box re-shaped to its panel (keeping where it was) —
    cutlayout.mjs ``applyPreset``."""
    width, height = OUTPUT[fmt]
    out = dict(layout)
    out["camOut"] = list(preset["camOut"])
    out["gameOut"] = list(preset["gameOut"])
    out["camOnTop"] = preset["camOnTop"]
    out["cam"] = refit(layout["cam"],
                       pixel_aspect(preset["camOut"], width, height), sw, sh)
    out["game"] = refit(layout["game"],
                        pixel_aspect(preset["gameOut"], width, height), sw, sh)
    return out


def default_layout(fmt, sw=1920, sh=1080):
    """Starting layout for a shape: the first preset, the camera in
    the top-left corner and the gameplay centred and as big as fits
    — cutlayout.mjs ``defaultLayout``."""
    preset = PRESETS[fmt][0]
    width, height = OUTPUT[fmt]
    cam_aspect = pixel_aspect(preset["camOut"], width, height)
    game_aspect = pixel_aspect(preset["gameOut"], width, height)
    cam = box_of(cam_aspect, sw, sh, scale=0.26, cx=0.14, cy=0.16)
    return {"cam": cam, "game": box_of(game_aspect, sw, sh),
            "camOut": list(preset["camOut"]),
            "gameOut": list(preset["gameOut"]),
            "camOnTop": True, "camHidden": False}


def stack_panels(cam_box, split=None, source_aspect=16 / 9):
    """Cam panel height from the cam box aspect, clamped —
    logic.mjs ``stackPanels`` (via stack.mjs ``stackPlan``).

    share = split (clamped to [.25, .55]) or, by default,
    1080 / camAspect / 1920 — i.e. the height a full-width panel
    needs to show the cam box undistorted. top is rounded to an
    even pixel count; bottom is the rest of the 1920 frame.
    Returns {"top": px, "bottom": px}.
    """
    cam_aspect = ((cam_box[2] * source_aspect) / cam_box[3]
                  if cam_box else 16 / 9)
    share = (_clamp(split, STACK_SPLIT_MIN, STACK_SPLIT_MAX)
             if isinstance(split, (int, float))
             else _clamp(1080 / cam_aspect / 1920,
                         STACK_SPLIT_MIN, STACK_SPLIT_MAX))
    top = max(2, round(1920 * share / 2) * 2)
    return {"top": top, "bottom": 1920 - top}


def default_game_box(bottom, source_aspect=16 / 9):
    """Default gameplay crop: full height, centred, shaped like
    the bottom panel — logic.mjs ``defaultGameBox``."""
    w = min(1, (1080 / bottom) / source_aspect)
    return [round((1 - w) / 2, 4), 0, round(w, 4), 1]


def card_cam_box(box, face=None, source_aspect=16 / 9):
    """The part of the cam box shaped like the Glow Card
    (880x544) — logic.mjs ``cardCamBox``. A cam taller than the
    card loses height, not the head: the face sits ~45% down (or
    the crop keeps the upper part when no face is known)."""
    x, y, w, h = box
    target = CARD["cam"]["w"] / CARD["cam"]["h"]
    aspect = w * source_aspect / h
    if abs(aspect - target) < 0.01:
        return [x, y, w, h]
    if aspect < target:
        nh = w * source_aspect / target
        ny = (_clamp(face[1] + face[3] / 2 - nh * 0.45, y, y + h - nh)
              if face else y + (h - nh) * 0.2)
        return [round(v, 5) for v in (x, ny, w, nh)]
    nw = h * target / source_aspect
    nx = (_clamp(face[0] + face[2] / 2 - nw / 2, x, x + w - nw)
          if face else x + (w - nw) / 2)
    return [round(v, 5) for v in (nx, y, nw, h)]


def panel_pixels(preset_id, out_w=W, out_h=H):
    """Panel rects in output pixels: {"cam": (x, y, w, h),
    "game": (x, y, w, h)}. Widths/heights rounded to even pixels
    (yuv420p); stacked panels sum exactly to the frame height."""
    p = get_preset(preset_id)
    def px(box):
        x = int(round(box[0] * out_w / 2) * 2)
        y = int(round(box[1] * out_h / 2) * 2)
        w = int(round(box[2] * out_w / 2) * 2)
        h = int(round(box[3] * out_h / 2) * 2)
        return (x, y, w, h)
    cam = px(p["camOut"])
    game = px(p["gameOut"])
    # Stacked presets must tile the frame exactly, no 2px seam/gap.
    if p["camOut"][2] >= 0.99 and p["gameOut"][2] >= 0.99:
        if p["camOut"][1] == 0:  # cam on top
            cam = (0, 0, out_w, cam[3])
            game = (0, cam[3], out_w, out_h - cam[3])
        else:  # game on top
            game = (0, 0, out_w, game[3])
            cam = (0, game[3], out_w, out_h - game[3])
    return {"cam": cam, "game": game}


def caption_y_in_game_panel(preset_id, cap_y=1080, out_h=H):
    """True when our caption line (cap_y, default 1080) lands
    inside the gameplay panel — i.e. the preset keeps our caption
    safe zone intact. For cam-corner the game panel is the whole
    frame, so this is trivially true."""
    panels = panel_pixels(preset_id, out_h=out_h)
    _, gy, _, gh = panels["game"]
    return gy <= cap_y < gy + gh


def _even(n):
    return max(2, int(round(n / 2) * 2))


def _panel_filter(box_shares, src_w, src_h, panel_w, panel_h, label):
    """Crop a source box (shares) to the panel shape (coverSource
    math), scale-to-cover the panel, return the filter fragment
    producing [label]."""
    x, y, w, h = box_shares
    sx, sy, sw, sh = x * src_w, y * src_h, w * src_w, h * src_h
    cx, cy, cw, ch = cover_source(sx, sy, sw, sh, panel_w, panel_h)
    cx, cy = _even(cx), _even(cy)
    cw, ch = _even(cw), _even(ch)
    # Clamp the even-rounded crop back inside the frame.
    cw = min(cw, _even(src_w - cx))
    ch = min(ch, _even(src_h - cy))
    return (f"crop={cw}:{ch}:{cx}:{cy},"
            f"scale={panel_w}:{panel_h}:flags=lanczos:"
            f"force_original_aspect_ratio=increase,"
            f"unsharp=5:5:1.0:5:5:0.5,crop={panel_w}:{panel_h},"
            f"setsar=1[{label}]")


def layout_filter(preset_id, src_w, src_h, cam_box=None, game_box=None,
                  ass_path=None, fps=30):
    """Full ffmpeg video filter for a facecam layout preset:
    [0:v] ... [vo]. Mirrors ``reframe.fill_filter``'s chain
    (lanczos scale + light unsharp, source fps preserved by the
    caller, burned .ass captions) but composites the two panels
    instead of a single tracked crop.

    cam_box / game_box are source boxes in shares; when omitted
    they come from ``default_layout`` reshaped to the preset
    (``apply_preset``). The game box defaults to the full frame;
    the cam box defaults to the top-left defaultLayout box.
    """
    preset = get_preset(preset_id)
    panels = panel_pixels(preset_id)
    if cam_box is None or game_box is None:
        base = default_layout("shorts", src_w, src_h)
        applied = apply_preset(base, preset, "shorts", src_w, src_h)
        cam_box = cam_box if cam_box is not None else applied["cam"]
        game_box = game_box if game_box is not None else applied["game"]
    # Re-shape each source box to its panel (applyPreset does this
    # for the defaults; do it for caller-supplied boxes too so the
    # panel is never stretched).
    cam_box = refit(cam_box, pixel_aspect(preset["camOut"], W, H),
                    src_w, src_h)
    game_box = refit(game_box, pixel_aspect(preset["gameOut"], W, H),
                     src_w, src_h)
    (cx, cy, cw, ch) = panels["cam"]
    (gx, gy, gw, gh) = panels["game"]
    filters = []
    if preset_id == "cam-corner":
        filters.append(f"[0:v]split=2[gamesrc][camsrc]")
        filters.append("[gamesrc]" + _panel_filter(
            game_box, src_w, src_h, W, H, "gamepanel"))
        filters.append("[camsrc]" + _panel_filter(
            cam_box, src_w, src_h, cw, ch, "campanel"))
        filters.append(f"[gamepanel][campanel]overlay={cx}:{cy},"
                       f"setsar=1,fps={fps}[stacked]")
    else:
        top_first = preset["camOut"][1] == 0  # cam-top vs game-top
        filters.append(f"[0:v]split=2[camsrc][gamesrc]")
        filters.append("[camsrc]" + _panel_filter(
            cam_box, src_w, src_h, cw, ch, "campanel"))
        filters.append("[gamesrc]" + _panel_filter(
            game_box, src_w, src_h, gw, gh, "gamepanel"))
        first, second = ("campanel", "gamepanel") if top_first \
            else ("gamepanel", "campanel")
        filters.append(f"[{first}][{second}]vstack=inputs=2,"
                       f"setsar=1,fps={fps}[stacked]")
    tail = "[stacked]"
    if ass_path is not None:
        from clipper.core.openshorts_ffmpeg_utils import escape_filter_value
        sub = escape_filter_value(str(ass_path))
        tail = f"[stacked]subtitles='{sub}'[vo]"
    else:
        tail = "[stacked]null[vo]"
    filters.append(tail)
    return ";".join(filters)
