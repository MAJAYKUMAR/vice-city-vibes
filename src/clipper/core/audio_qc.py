#!/usr/bin/env python3
"""Post-encode audio QC (pipeline upgrade ITEM 5) — decode-only ffmpeg
measurements of a finished clip's audio.

Why it exists: clips were marked post-ready with no check that the
encode actually met the export spec. The cut normalises with
single-pass loudnorm I=-14, but the true-peak ceiling is inconsistent
across the codebase (TP=-1.5 in clip.py vs TP=-2.0 in
openshorts_ffmpeg_utils.py, whose own measurements show TP=-1.5 lets
AAC overshoot to +0.2 dBTP). This module does NOT settle that question
— it RECORDS the measurements (integrated loudness, true peak, LRA,
sample peak, flat factor) so the two-pass loudnorm / TP-unification
decision can be made from data, not vibes.

Checks (three decode-only ffmpeg passes, ~1-2 s per clip):
  * ebur128=peak=true — integrated loudness (I), loudness range (LRA),
    true peak (TP, dBTP).
  * astats — sample Peak level (dBFS) and Flat factor. Flat-topped runs
    indicate clipping. NOTE: Flat factor 0 does NOT rule out
    single-sample clipping — a single clipped sample has no run to be
    flat. A peak at/above 0 dBFS with flat factor 0 is recorded as a
    note, not cleared.
  * volumedetect — mean/max volume (dB), cross-check for the peak.

FLAG (fail) when:
  * integrated loudness outside -15...-13 LUFS (inclusive), or
  * true peak > -1.0 dBTP, or
  * Peak level >= 0 dBFS with Flat factor > 0 ("possible clipping").

Returns a plain dict (JSON-serialisable) so it can be stored verbatim
in the clip's .json sidecar next to the safety verdict — see
validate.py / clip.py for the recording pattern. Never raises for a
bad file: measurement failures are reported in the dict.
"""
import re
import shutil
import subprocess

# Export-spec audio targets. I=-14 is the house loudnorm target
# (clip.py, openshorts_ffmpeg_utils.LOUDNORM_FILTER); the gate allows
# +/-1 LU because single-pass loudnorm + AAC encode drifts from target.
TARGET_I_LUFS = -14.0
I_MIN_LUFS = -15.0
I_MAX_LUFS = -13.0
# -1.0 dBTP: the platforms' own ceiling. Measured overshoot (see
# openshorts_ffmpeg_utils.py) is why TP=-1.5 encodes can still fail this.
TP_MAX_DBTP = -1.0

_FFMPEG_TIMEOUT_S = 120


def _ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def _run_ffmpeg(args):
    """Run ffmpeg, return (returncode, stderr text). Never raises."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "info", "-nostdin", *args],
            capture_output=True, text=True, errors="replace",
            timeout=_FFMPEG_TIMEOUT_S)
        return r.returncode, r.stderr or ""
    except FileNotFoundError:
        return -1, "ffmpeg not found"
    except subprocess.TimeoutExpired:
        return -1, f"ffmpeg timed out after {_FFMPEG_TIMEOUT_S}s"
    except Exception as e:  # noqa: BLE001 — gate must never crash the cut
        return -1, f"ffmpeg error: {e}"


def _num(text):
    """Parse an ffmpeg number, including '-inf'."""
    if text is None:
        return None
    text = text.strip().lower()
    if text in ("-inf", "-infinity"):
        return float("-inf")
    if text in ("inf", "+inf", "infinity"):
        return float("inf")
    try:
        return float(text)
    except ValueError:
        return None


def _measure_ebur128(path):
    """(integrated LUFS, LRA LU, true peak dBTP) or (None, None, None)
    plus an error string. framelog=quiet suppresses the per-0.1s lines;
    the Summary block is still printed."""
    rc, err = _run_ffmpeg(["-i", str(path), "-vn",
                           "-af", "ebur128=peak=true:framelog=quiet",
                           "-f", "null", "-"])
    if rc != 0:
        return None, None, None, f"ebur128 pass failed (rc={rc}): {err.strip()[-300:]}"
    summary = err.split("Summary:", 1)
    if len(summary) < 2:
        return None, None, None, "ebur128 Summary block not found in ffmpeg output"
    block = summary[1]
    m_i = re.search(r"\bI:\s+(-?\d+(?:\.\d+)?|-inf)\s+LUFS", block)
    m_lra = re.search(r"\bLRA:\s+(-?\d+(?:\.\d+)?|-inf)\s+LU", block)
    m_tp = re.search(r"\bPeak:\s+(-?\d+(?:\.\d+)?|-inf)\s+dBFS", block)
    if not (m_i and m_tp):
        return None, None, None, "could not parse ebur128 Summary (I/Peak missing)"
    return (_num(m_i.group(1)),
            _num(m_lra.group(1)) if m_lra else None,
            _num(m_tp.group(1)), None)


def _measure_astats(path):
    """(peak level dBFS, flat factor, rms level dBFS) — max across
    channels + the Overall section — plus an error string."""
    rc, err = _run_ffmpeg(["-i", str(path), "-vn",
                           "-af", "astats=metadata=1:reset=0",
                           "-f", "null", "-"])
    if rc != 0:
        return None, None, None, f"astats pass failed (rc={rc}): {err.strip()[-300:]}"
    peaks = [_num(v) for v in re.findall(r"Peak level dB:\s+(-?\d+(?:\.\d+)?|-inf)", err)]
    flats = [_num(v) for v in re.findall(r"Flat factor:\s+(-?\d+(?:\.\d+)?|-inf)", err)]
    rmss = [_num(v) for v in re.findall(r"RMS level dB:\s+(-?\d+(?:\.\d+)?|-inf)", err)]
    peaks = [p for p in peaks if p is not None]
    flats = [f for f in flats if f is not None]
    rmss = [r for r in rmss if r is not None]
    if not peaks:
        return None, None, None, "could not parse astats output (no Peak level)"
    return max(peaks), (max(flats) if flats else 0.0), (max(rmss) if rmss else None), None


def _measure_volumedetect(path):
    """(mean volume dB, max volume dB) plus an error string."""
    rc, err = _run_ffmpeg(["-i", str(path), "-vn",
                           "-af", "volumedetect",
                           "-f", "null", "-"])
    if rc != 0:
        return None, None, f"volumedetect pass failed (rc={rc}): {err.strip()[-300:]}"
    m_mean = re.search(r"mean_volume:\s+(-?\d+(?:\.\d+)?|-inf)\s+dB", err)
    m_max = re.search(r"max_volume:\s+(-?\d+(?:\.\d+)?|-inf)\s+dB", err)
    if not (m_mean and m_max):
        return None, None, "could not parse volumedetect output"
    return _num(m_mean.group(1)), _num(m_max.group(1)), None


def measure_audio(path):
    """Measure a finished clip's audio. Returns a JSON-serialisable dict.

    Keys: available, measured_I, measured_TP, lra, peak_level_db,
    flat_factor, rms_level_db, mean_volume_db, max_volume_db,
    clipping_suspected, passed, reasons, notes, errors.
    Always returns a dict; never raises.
    """
    result = {
        "available": _ffmpeg_available(),
        "measured_I": None, "measured_TP": None, "lra": None,
        "peak_level_db": None, "flat_factor": None, "rms_level_db": None,
        "mean_volume_db": None, "max_volume_db": None,
        "clipping_suspected": False,
        "passed": False, "reasons": [], "notes": [], "errors": [],
    }
    if not result["available"]:
        result["errors"].append("ffmpeg not found on PATH — audio QC unavailable")
        return result
    try:
        loud_i, lra, tp, e1 = _measure_ebur128(path)
        peak, flat, rms, e2 = _measure_astats(path)
        mean_vol, max_vol, e3 = _measure_volumedetect(path)
    except Exception as e:  # noqa: BLE001 — gate must never crash the cut
        result["errors"].append(f"audio measurement crashed: {e}")
        return result
    for e in (e1, e2, e3):
        if e:
            result["errors"].append(e)
    result.update({"measured_I": loud_i, "measured_TP": tp, "lra": lra,
                   "peak_level_db": peak, "flat_factor": flat,
                   "rms_level_db": rms,
                   "mean_volume_db": mean_vol, "max_volume_db": max_vol})
    if result["errors"]:
        result["reasons"].append(
            "audio measurement incomplete: " + "; ".join(result["errors"]))
        return result

    # --- Flags (see module docstring). Measurements are recorded
    # regardless of pass/fail — the numbers are the point. ---
    if loud_i is None or not (I_MIN_LUFS <= loud_i <= I_MAX_LUFS):
        result["reasons"].append(
            f"integrated loudness {loud_i} LUFS outside "
            f"{I_MIN_LUFS}...{I_MAX_LUFS} LUFS (target {TARGET_I_LUFS})")
    if tp is None or tp > TP_MAX_DBTP:
        result["reasons"].append(
            f"true peak {tp} dBTP above ceiling {TP_MAX_DBTP} dBTP")
    if peak is not None and flat is not None and peak >= 0.0 and flat > 0:
        result["clipping_suspected"] = True
        result["reasons"].append(
            f"possible clipping: Peak level {peak} dB with "
            f"Flat factor {flat} (flat-topped runs at full scale)")
    elif peak is not None and peak >= 0.0:
        # Flat factor 0 does NOT rule out single-sample clipping.
        result["notes"].append(
            f"peak at/above full scale ({peak} dB) but Flat factor 0 — "
            "single-sample clipping NOT ruled out")
    result["passed"] = not result["reasons"]
    return result


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(measure_audio(sys.argv[1]), indent=1))
