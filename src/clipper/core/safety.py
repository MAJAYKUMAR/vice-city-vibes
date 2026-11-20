#!/usr/bin/env python3
"""Content filter for the Clipper Engine — flag unsafe moments BEFORE
any clip is approved or posted.

Why it exists: the 10-clip test (2026-11-19) found the engine's top
moment, clip_10 (score 5.11, "CHILL OUT CHAT"), is Jason in a shower
— bare back / buttocks on screen. The hype meter finds the LOUDEST
moment, not the safest one. A clip like that must never auto-post.

What it uses (measured 2026-11-19, not assumed):
  * PRIMARY — NudeNet detector (nudenet 3.4.2, ONNX 320n model,
    pip-installed into the gtavi venv, runs on CPU via onnxruntime).
    Measured 2026-11-19 on the three test clips: clip_10 (shower)
    -> BUTTOCKS_EXPOSED 0.39 on 4 frames + MALE_GENITALIA_EXPOSED
    0.25; clip_7 -> nothing; clip_5 (Jason shirtless, shorts on) ->
    MALE_BREAST_EXPOSED 0.34 + BUTTOCKS_EXPOSED 0.39 on 1 frame
    each — FALSE positives on a bare male back (visually verified:
    his shorts are on). So a single buttocks/breast hit does NOT
    block: BLOCK needs genitalia/anus at >= 0.20, or buttocks/breast
    on >= 3 sampled frames, or a >= 0.50 score. Anything less is
    REVIEW — a human looks before it can post.
  * SECONDARY — skin-tone fraction (YCbCr) + transcript keywords
    (shower/bath/naked/nude/butt/strip...). MEASURED: skin fraction
    alone CANNOT tell a bare body from GTA 6's warm indoor world —
    clip_5 (a normal indoor room) scored skin 0.48, higher than the
    shower clip's 0.37, and blob shape/position didn't separate them
    either. So skin NEVER blocks on its own; it only adds REVIEW.
  * SECONDARY — skin-tone fraction (YCbCr) + transcript keywords
    (shower/bath/naked/nude/butt/strip...). MEASURED: skin fraction
    alone CANNOT tell a bare body from GTA 6's warm indoor world —
    clip_5 (a normal indoor room) scored skin 0.48, higher than the
    shower clip's 0.37, and blob shape/position didn't separate them
    either. So skin NEVER blocks on its own; it only adds REVIEW.
  * If the NudeNet model cannot load, the verdict caps at REVIEW
    (never SAFE) and the failure is recorded.

Verdicts: SAFE / REVIEW / BLOCK.
  * BLOCK: genitalia/anus hit >= 0.20, or buttocks/breast hits on
    >= 3 sampled frames, or any hard hit >= 0.50, or keyword hit +
    skin > 0.50.
  * REVIEW: any hard hit at all, or skin > 0.50, or keyword hit.
  * SAFE: everything else.
A BLOCK verdict means: the clip may be cut for format testing, but
it is NEVER registered as approvable in the Garage and NEVER posted.
garage.py approve() refuses BLOCKED clips. REVIEW clips need a human
look before approval. Only SAFE clips can auto-post.
"""
import json
import re
import tempfile
from pathlib import Path

KEYWORDS = re.compile(
    r"\b(shower|bath|bathtub|naked|nude|nudity|butt|buttocks|strip|stripping|"
    r"undress|topless|showerhead)\b", re.I)

# NudeNet 3.4.2 class names (verified live 2026-11-19 — they are
# "<PART>_EXPOSED", e.g. BUTTOCKS_EXPOSED, MALE_GENITALIA_EXPOSED).
# A hit in a hard class blocks. FACE / FEET / ARMPITS are not blockers.
HARD_CLASSES = {
    "BUTTOCKS_EXPOSED", "BREAST_EXPOSED", "FEMALE_BREAST_EXPOSED",
    "MALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
}


def _is_hard(cls):
    return cls in HARD_CLASSES or "GENITALIA" in cls or cls.startswith("EXPOSED_")
BLOCK_SCORE = 0.20
REVIEW_SCORE = 0.10

SKIN_CB = (77, 127)
SKIN_CR = (133, 173)

_detector = None
_detector_error = None


def _get_detector():
    global _detector, _detector_error
    if _detector is None and _detector_error is None:
        try:
            from nudenet import NudeDetector
            _detector = NudeDetector()
        except Exception as e:
            _detector_error = str(e)
    return _detector


def skin_fraction(frame_bgr):
    import cv2
    import numpy as np
    ycrcb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2YCrCb)
    lo = np.array([0, SKIN_CR[0], SKIN_CB[0]], dtype=np.uint8)
    hi = np.array([255, SKIN_CR[1], SKIN_CB[1]], dtype=np.uint8)
    mask = cv2.inRange(ycrcb, lo, hi)
    return float(mask.mean() / 255.0)


def check(src, start, end, transcript_words=None, sample_fps=2):
    """Check a source window. Returns a verdict dict."""
    import cv2

    det = _get_detector()
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    sw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    sh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    step = max(1, int(round(fps / sample_fps)))
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start) * 1000)
    skins, hard_hits, n = [], [], 0
    i = 0
    with tempfile.TemporaryDirectory() as td:
        while True:
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if t > end or not cap.grab():
                break
            if i % step == 0:
                ok, f = cap.retrieve()
                if not ok:
                    break
                n += 1
                cw = int(sh * 9 / 16)
                x0 = max(0, (sw - cw) // 2)
                skins.append(skin_fraction(f[:, x0:x0 + cw]))
                if det is not None:
                    fp = str(Path(td) / f"f{n}.jpg")
                    cv2.imwrite(fp, f)
                    try:
                        for d in det.detect(fp):
                            if _is_hard(d["class"]):
                                hard_hits.append(
                                    {"class": d["class"],
                                     "score": round(float(d["score"]), 2),
                                     "t": round(t - start, 1)})
                    except Exception:
                        pass
            i += 1
    cap.release()
    mean_skin = sum(skins) / len(skins) if skins else 0.0
    max_skin = max(skins) if skins else 0.0

    kw_hits = []
    if transcript_words:
        text = " ".join(w.get("w", "") for w in transcript_words
                        if start <= w.get("s", 0) < end)
        kw_hits = sorted(set(KEYWORDS.findall(text.lower())))

    best_hard = max((h["score"] for h in hard_hits), default=0.0)
    n_hard_frames = len({h["t"] for h in hard_hits})
    genital = any("GENITALIA" in h["class"] or "ANUS" in h["class"]
                  for h in hard_hits)
    reasons = []
    if hard_hits:
        reasons.append(f"nudenet hard-class hits {hard_hits[:3]}"
                       f"{' (+more)' if len(hard_hits) > 3 else ''}")
    if mean_skin > 0.50:
        reasons.append(f"elevated skin {mean_skin:.2f} (weak signal only)")
    if kw_hits:
        reasons.append(f"keywords {kw_hits}")

    if ((genital and best_hard >= BLOCK_SCORE)
            or n_hard_frames >= 3
            or best_hard >= 0.50
            or (kw_hits and mean_skin > 0.50)):
        verdict = "BLOCK"
    elif hard_hits or mean_skin > 0.50 or kw_hits:
        verdict = "REVIEW"
    else:
        verdict = "SAFE"
    if det is None and verdict == "SAFE":
        verdict = "REVIEW"
        reasons.append(f"nudenet unavailable ({_detector_error}) — capped at REVIEW")
    return {
        "verdict": verdict,
        "nudenet_available": det is not None,
        "hard_hits": hard_hits,
        "best_hard_score": round(best_hard, 2),
        "skin_mean": round(mean_skin, 3),
        "skin_max": round(max_skin, 3),
        "frames_sampled": n,
        "keyword_hits": kw_hits,
        "reasons": reasons,
    }


if __name__ == "__main__":
    import sys
    src, s0, e0 = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    words = None
    if len(sys.argv) > 4 and Path(sys.argv[4]).exists():
        words = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
    print(json.dumps(check(src, s0, e0, words), indent=1))
