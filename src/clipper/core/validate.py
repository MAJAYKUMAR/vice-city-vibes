#!/usr/bin/env python3
"""Post-encode QC gate (pipeline upgrade ITEM 5) — export-spec validation
+ audio QC for a finished clip, run AFTER encoding and BEFORE the clip
is marked post-ready.

Structure: audio measurements live in audio_qc.py; this module owns the
export-spec checks (ffprobe + container inspection) and the combined
validate_clip() entry point that the cut paths call.

Export spec (strictest platform cap where platforms differ):
  * resolution 1080x1920, H.264 video, yuv420p, fps in {24, 25, 30, 60}
    (59.94/29.97/23.976 accepted as their nominal rates),
  * moov atom before mdat (faststart — moov first),
  * AAC audio at 48 kHz,
  * file < 300 MB (Instagram API cap, the strictest platform cap),
  * duration 7-30 s (our own rule),
  * title <= 100 chars, description <= 5,000 BYTES UTF-8 (emoji count
    as 4 bytes each — measured in bytes, not characters).

Recording (mirrors the safety verdict pattern in safety.py / clip.py):
the safety verdict is stored as a top-level "safety" dict inside the
clip's .json sidecar ({"verdict": "SAFE"/"REVIEW"/"BLOCK", ...}) and
garage.py reads it from there. QC is stored the same way, as a
top-level "qc" dict: {"verdict": "PASS"/"FAIL"/"UNAVAILABLE", "passed",
"reasons", "audio": {...}, "export": {...}}. Every measurement is
kept, pass or fail — the numbers are what later decides the two-pass
loudnorm / TP-unification question (clip.py uses TP=-1.5,
openshorts_ffmpeg_utils.py uses TP=-2.0).

Fail = the clip stays out of the queue, with a reason string. This
module never changes the queue file format and never raises for a bad
file: a missing ffmpeg/ffprobe yields verdict UNAVAILABLE (not a
crash), and a gate error caps at UNAVAILABLE with the error recorded.
"""
import json
import shutil
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from clipper.core.audio_qc import measure_audio

# --- Export spec -------------------------------------------------------
SPEC_WIDTH = 1080
SPEC_HEIGHT = 1920
SPEC_VIDEO_CODEC = "h264"
SPEC_PIX_FMT = "yuv420p"
SPEC_FPS = (24, 25, 30, 60)
SPEC_AUDIO_CODEC = "aac"
SPEC_AUDIO_SAMPLE_RATE = 48000
SPEC_MAX_BYTES = 300 * 1024 * 1024          # 300 MB, Instagram API cap
SPEC_MIN_DURATION_S = 7.0
SPEC_MAX_DURATION_S = 30.0
SPEC_MAX_TITLE_CHARS = 100
SPEC_MAX_DESCRIPTION_BYTES = 5000           # UTF-8 bytes, not chars

_FPS_TOLERANCE = 0.11   # 23.976/29.97/59.94 read as their nominal rate
_FFPROBE_TIMEOUT_S = 60


def _ffprobe_available():
    return shutil.which("ffprobe") is not None


def _ffprobe(path):
    """ffprobe JSON for a file, or None + error string."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(path)],
            capture_output=True, text=True, errors="replace",
            timeout=_FFPROBE_TIMEOUT_S)
    except FileNotFoundError:
        return None, "ffprobe not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"ffprobe timed out after {_FFPROBE_TIMEOUT_S}s"
    except Exception as e:  # noqa: BLE001 — gate must never crash the cut
        return None, f"ffprobe error: {e}"
    if r.returncode != 0:
        return None, f"ffprobe failed (rc={r.returncode}): {r.stderr.strip()[-300:]}"
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError as e:
        return None, f"ffprobe output not JSON: {e}"


def _moov_first(path):
    """True when the moov atom precedes mdat (faststart). Seeks through
    top-level MP4 boxes by size+type (headers only, so any file size is
    fine); handles 64-bit largesize (size == 1) and size == 0 (to EOF)."""
    try:
        size_on_disk = Path(path).stat().st_size
    except OSError as e:
        return None, f"cannot read file for moov check: {e}"
    order = []
    try:
        with open(path, "rb") as f:
            off = 0
            while off + 8 <= size_on_disk and len(order) < 32:
                f.seek(off)
                head = f.read(16)
                if len(head) < 8:
                    break
                size, typ = struct.unpack(">I4s", head[:8])
                typ = typ.decode("latin-1")
                hdr = 8
                if size == 1 and len(head) >= 16:  # 64-bit largesize
                    size = struct.unpack(">Q", head[8:16])[0]
                    hdr = 16
                elif size == 0:  # box runs to EOF
                    size = size_on_disk - off
                if size < hdr:
                    break
                order.append(typ)
                if "moov" in order and "mdat" in order:
                    break
                off += size
    except OSError as e:
        return None, f"cannot read file for moov check: {e}"
    if "moov" not in order or "mdat" not in order:
        return None, f"moov/mdat boxes not both found (saw: {order})"
    return order.index("moov") < order.index("mdat"), None


def _fps_of(stream):
    """Average frame rate as float, from avg_frame_rate then r_frame_rate."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = (stream or {}).get(key) or ""
        if "/" in raw:
            num, _, den = raw.partition("/")
            try:
                value = float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                continue
            if value > 0:
                return value
    return None


def check_export_spec(path, title=None, description=None):
    """Export-spec checks via ffprobe (+ moov scan). Returns a dict with
    passed, reasons, checks (per-check pass/measured/expected) and
    measurements. Title/description fall back to the file's format tags;
    when neither is supplied the check is recorded as 'skipped', not a
    fail — the cut paths only know the title, the description is written
    later at packaging time."""
    path = Path(path)
    result = {"passed": False, "reasons": [], "checks": {}, "measurements": {}}
    probe, error = _ffprobe(path)
    if probe is None:
        result["reasons"].append(f"export-spec check unavailable: {error}")
        result["checks"]["ffprobe"] = {"passed": False, "measured": None,
                                       "expected": "readable file", "error": error}
        return result

    streams = probe.get("streams", [])
    fmt = probe.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    tags = fmt.get("tags", {}) or {}
    if title is None:
        title = tags.get("title")
    if description is None:
        description = tags.get("description")

    def _check(name, passed, measured, expected, skipped=False):
        result["checks"][name] = {"passed": bool(passed), "measured": measured,
                                  "expected": expected,
                                  **({"status": "skipped"} if skipped else {})}
        if not passed and not skipped:
            result["reasons"].append(f"{name}: measured {measured}, expected {expected}")

    # --- Video ---
    if video is None:
        _check("video_stream", False, None, "one video stream")
    else:
        w, h = video.get("width"), video.get("height")
        _check("resolution", (w, h) == (SPEC_WIDTH, SPEC_HEIGHT),
               f"{w}x{h}", f"{SPEC_WIDTH}x{SPEC_HEIGHT}")
        _check("video_codec", video.get("codec_name") == SPEC_VIDEO_CODEC,
               video.get("codec_name"), SPEC_VIDEO_CODEC)
        _check("pix_fmt", video.get("pix_fmt") == SPEC_PIX_FMT,
               video.get("pix_fmt"), SPEC_PIX_FMT)
        fps = _fps_of(video)
        fps_ok = fps is not None and any(
            abs(fps - allowed) <= _FPS_TOLERANCE for allowed in SPEC_FPS)
        _check("fps", fps_ok, fps, f"one of {list(SPEC_FPS)}")
    # --- Container / faststart ---
    moov_first, moov_err = _moov_first(path)
    if moov_err:
        _check("faststart", False, None, "moov atom before mdat")
        result["reasons"][-1] += f" ({moov_err})"
    else:
        _check("faststart", moov_first, "moov first" if moov_first else "mdat first",
               "moov atom before mdat (faststart)")
    # --- Audio ---
    if audio is None:
        _check("audio_stream", False, None, "one AAC audio stream at 48 kHz")
    else:
        _check("audio_codec", audio.get("codec_name") == SPEC_AUDIO_CODEC,
               audio.get("codec_name"), SPEC_AUDIO_CODEC)
        try:
            sr = int(audio.get("sample_rate") or 0)
        except (TypeError, ValueError):
            sr = None
        _check("audio_sample_rate", sr == SPEC_AUDIO_SAMPLE_RATE,
               sr, SPEC_AUDIO_SAMPLE_RATE)
    # --- File / duration ---
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    _check("file_size", size is not None and size < SPEC_MAX_BYTES,
           f"{size} bytes" if size is not None else None, "< 300 MB")
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = None
    dur_ok = duration is not None and SPEC_MIN_DURATION_S <= duration <= SPEC_MAX_DURATION_S
    _check("duration", dur_ok,
           f"{duration:.2f}s" if duration is not None else None,
           f"{SPEC_MIN_DURATION_S:.0f}-{SPEC_MAX_DURATION_S:.0f}s")
    # --- Title / description (bytes for description: emoji = 4 bytes) ---
    if title is None:
        _check("title_length", True, None, f"<= {SPEC_MAX_TITLE_CHARS} chars",
               skipped=True)
    else:
        _check("title_length", len(title) <= SPEC_MAX_TITLE_CHARS,
               f"{len(title)} chars", f"<= {SPEC_MAX_TITLE_CHARS} chars")
    if description is None:
        _check("description_bytes", True, None,
               f"<= {SPEC_MAX_DESCRIPTION_BYTES} UTF-8 bytes", skipped=True)
    else:
        n_bytes = len(description.encode("utf-8"))
        _check("description_bytes", n_bytes <= SPEC_MAX_DESCRIPTION_BYTES,
               f"{n_bytes} bytes ({len(description)} chars)",
               f"<= {SPEC_MAX_DESCRIPTION_BYTES} UTF-8 bytes")

    result["measurements"] = {
        "resolution": result["checks"].get("resolution", {}).get("measured"),
        "video_codec": result["checks"].get("video_codec", {}).get("measured"),
        "pix_fmt": result["checks"].get("pix_fmt", {}).get("measured"),
        "fps": result["checks"].get("fps", {}).get("measured"),
        "faststart": result["checks"].get("faststart", {}).get("measured"),
        "audio_codec": result["checks"].get("audio_codec", {}).get("measured"),
        "audio_sample_rate": result["checks"].get("audio_sample_rate", {}).get("measured"),
        "file_size_bytes": size,
        "duration_s": duration,
        "title_chars": len(title) if title is not None else None,
        "description_bytes": (len(description.encode("utf-8"))
                              if description is not None else None),
    }
    result["passed"] = not result["reasons"]
    return result


def validate_clip(clip_path, title=None, description=None,
                  sidecar_path=None, write_sidecar=True):
    """Post-encode QC gate for one finished clip.

    Runs audio QC (audio_qc.measure_audio) + export-spec checks, and —
    unless write_sidecar=False — records the full result as the top-level
    "qc" key in the clip's .json sidecar (same pattern as the "safety"
    verdict in clip.py), preserving every other key already there.

    Returns {"verdict": "PASS"/"FAIL"/"UNAVAILABLE", "passed": bool,
    "available": bool, "reasons": [...], "notes": [...], "audio": {...},
    "export": {...}, "clip": str, "checked_at": ISO-8601 UTC}.
    Never raises. A FAIL means: clip stays out of the queue.
    """
    clip = Path(clip_path)
    result = {
        "verdict": "FAIL", "passed": False, "available": True,
        "reasons": [], "notes": [],
        "audio": {}, "export": {},
        "clip": str(clip),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not clip.exists():
        result["reasons"].append(f"file not found: {clip}")
        result["available"] = False
        return result
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
        result.update({"verdict": "UNAVAILABLE", "available": False})
        result["reasons"].append(
            f"QC unavailable — missing on PATH: {', '.join(missing)}")
        result["audio"] = {"available": shutil.which("ffmpeg") is not None}
        result["export"] = {"passed": False,
                            "reasons": result["reasons"][:], "checks": {},
                            "measurements": {}}
        return result
    try:
        audio = measure_audio(clip)
        export = check_export_spec(clip, title=title, description=description)
    except Exception as e:  # noqa: BLE001 — gate must never crash the cut
        result.update({"verdict": "UNAVAILABLE", "available": False})
        result["reasons"].append(f"QC gate error: {e}")
        return result
    result["audio"] = audio
    result["export"] = export
    result["reasons"] = list(audio.get("reasons", [])) + list(export.get("reasons", []))
    result["notes"] = list(audio.get("notes", []))
    if audio.get("errors"):
        result["notes"].append(
            "audio measurement errors: " + "; ".join(audio["errors"]))
    # Tooling failure (ffprobe could not read the file at all) is not a
    # bad clip: cap at UNAVAILABLE, don't fail it. A measurable defect
    # (bad audio, wrong spec) stays FAIL.
    ffprobe_check = export.get("checks", {}).get("ffprobe", {})
    if ffprobe_check and not ffprobe_check.get("passed", True):
        result.update({"verdict": "UNAVAILABLE", "available": False,
                       "passed": False})
        return _record(result, clip, sidecar_path, write_sidecar)
    result["passed"] = bool(audio.get("passed") and export.get("passed"))
    result["verdict"] = "PASS" if result["passed"] else "FAIL"
    return _record(result, clip, sidecar_path, write_sidecar)


def _record(result, clip, sidecar_path, write_sidecar):
    """Write result as the top-level "qc" key of the sidecar .json.
    Never raises; a write failure is recorded in notes."""
    if not write_sidecar:
        return result
    sidecar = Path(sidecar_path) if sidecar_path else clip.with_suffix(".json")
    try:
        data = {}
        if sidecar.exists():
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        data["qc"] = result
        sidecar.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — recording must never fail the gate
        result["notes"].append(f"could not write sidecar {sidecar}: {e}")
    return result


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Post-encode QC gate for one clip.")
    ap.add_argument("clip")
    ap.add_argument("--title", default=None)
    ap.add_argument("--description", default=None)
    ap.add_argument("--description-file", default=None)
    ap.add_argument("--sidecar", default=None,
                    help="sidecar .json to record into (default: <clip>.json)")
    ap.add_argument("--no-write-sidecar", action="store_true")
    a = ap.parse_args()
    desc = a.description
    if a.description_file:
        desc = Path(a.description_file).read_text(encoding="utf-8")
    res = validate_clip(a.clip, title=a.title, description=desc,
                        sidecar_path=a.sidecar,
                        write_sidecar=not a.no_write_sidecar)
    print(json.dumps(res, indent=1))
    sys.exit(0 if res["verdict"] == "PASS" else
             2 if res["verdict"] == "UNAVAILABLE" else 1)
