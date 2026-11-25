#!/usr/bin/env python3
"""Tests for src/clipper/core/copyright.py — stage 11 copyright-risk check.

Plain asserts, no network, no pytest needed (pytest is not installed
on this VM). Run from the clipper-engine root:

    python3 tests/test_copyright.py

Fixtures are synthetic and generated here with ffmpeg + stdlib wave:
digital silence, quiet/near-silent sine tones at MEASURED levels
(volumes picked from real volumedetect readings, see comments), a
steady tone (music-bed stand-in), and a transient burst over a noise
floor (explosion/crash stand-in). No live VODs, no paid services.
"""
import math
import random
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "src"))

from clipper.core import copyright as cp  # noqa: E402

WORDS = [{"w": "hello", "s": 1.0, "e": 1.5},
         {"w": "chat", "s": 2.0, "e": 2.4},
         {"w": "look", "s": 5.0, "e": 5.3}]


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _tone(path, volume, dur=10):
    """Sine tone; volumes are MEASURED (AAC volumedetect means):
    0.003 -> -71.6 dB (BLOCK band), 0.035 -> -50.2 dB (REVIEW band),
    0.2 -> -35.1 dB (healthy band, cf. posted clips -15..-26 dB)."""
    _ffmpeg("-f", "lavfi", "-i",
            f"sine=frequency=440:sample_rate=48000:duration={dur},"
            f"volume={volume}", "-t", str(dur),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path))


def _wav(path, fn, dur=10, sr=48000):
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = b"".join(struct.pack(
            "<h", int(max(-1.0, min(1.0, fn(i / sr))) * 32767))
            for i in range(sr * dur))
        w.writeframes(frames)


def _fixtures(d):
    """Build all fixtures under temp dir d; return dict of Paths."""
    f = {}
    # Digital silence (anullsrc encodes to -91 dB, the Twitch mute floor).
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(d / "silent.mp4"))
    f["silent"] = d / "silent.mp4"
    _tone(d / "quiet.mp4", 0.003)       # mean -71.6 dB
    f["quiet"] = d / "quiet.mp4"
    _tone(d / "near_silent.mp4", 0.035)  # mean -50.2 dB
    f["near_silent"] = d / "near_silent.mp4"
    _tone(d / "healthy.mp4", 0.2)       # mean -35.1 dB
    f["healthy"] = d / "healthy.mp4"
    # Steady 0.2-amplitude tone: sustained bed, mean ~-17 dB.
    _wav(d / "steady.wav",
         lambda t: 0.2 * math.sin(2 * math.pi * 440 * t))
    _ffmpeg("-i", str(d / "steady.wav"), "-c:a", "aac", str(d / "steady.m4a"))
    f["steady"] = d / "steady.m4a"
    # Transient bursts (3 x 0.4s loud sine) over a low noise floor:
    # whole-window mean ~-18 dB, energy in 3 of 10 buckets only.
    random.seed(7)

    def _transient(t):
        v = random.uniform(-0.02, 0.02)
        for b in (2.0, 5.0, 8.0):
            if b <= t < b + 0.4:
                v += 0.5 * math.sin(2 * math.pi * 880 * (t - b))
        return v
    _wav(d / "transient.wav", _transient)
    _ffmpeg("-i", str(d / "transient.wav"), "-c:a", "aac",
            str(d / "transient.m4a"))
    f["transient"] = d / "transient.m4a"
    # Video-only file: no audio stream at all.
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "video_only.mp4"))
    f["video_only"] = d / "video_only.mp4"
    return f


def test_verdict_precedence(fx, tmp):
    # BLOCK beats REVIEW: muted audio + blocklist hit still BLOCKs.
    bl = tmp / "bl.yaml"
    bl.write_text("blocklist:\n"
                  "- name: TestStreamer\n  handle: teststreamer\n"
                  "  reason: test opt-out\n  evidence: test only\n",
                  encoding="utf-8")
    old = cp.BLOCKLIST_PATH
    cp.BLOCKLIST_PATH = bl
    try:
        r = cp.check_clip(fx["silent"], 0, 10, [], streamer="TestStreamer")
        assert r["verdict"] == "BLOCK", r
        assert r["blocklist_hit"] == "TestStreamer", r
        # REVIEW-level signal alone: steady tone, no speech.
        r = cp.check_clip(fx["steady"], 0, 10, [], streamer="NobodyHere")
        assert r["verdict"] == "REVIEW", r
        # CLEAR: healthy audio + speech.
        r = cp.check_clip(fx["healthy"], 0, 10, WORDS, streamer="NobodyHere")
        assert r["verdict"] == "CLEAR", r
        assert r["reasons"] == [], r
    finally:
        cp.BLOCKLIST_PATH = old


def test_blocklist_empty_by_default(fx):
    # The shipped blocklist is empty: no streamer may false-hit.
    entries, err = cp._load_blocklist()
    assert err is None, err
    assert entries == [], entries
    for name in ("IShowSpeed", "KaiCenat", "StableRonaldo", "JasonTheWeen"):
        r = cp.check_clip(fx["healthy"], 0, 10, WORDS, streamer=name)
        assert r["blocklist_hit"] is None, (name, r)
        assert not any("opt-out" in x for x in r["reasons"]), (name, r)


def test_muted_thresholds(fx):
    # Digital silence reads at the -91 dB Twitch mute floor -> BLOCK.
    r = cp.check_clip(fx["silent"], 0, 10, [])
    assert r["verdict"] == "BLOCK", r
    assert r["mean_db"] is not None and r["mean_db"] <= -60, r
    assert r["muted_fraction"] >= 0.80, r
    assert any("muted" in x for x in r["reasons"]), r
    # Quiet tone (-71.6 dB) -> BLOCK even with words (mute beats speech).
    r = cp.check_clip(fx["quiet"], 0, 10, WORDS)
    assert r["verdict"] == "BLOCK", r
    assert r["mean_db"] <= -60, r
    # Near-silent (-50.2 dB) + 0 words -> REVIEW, never BLOCK/CLEAR.
    r = cp.check_clip(fx["near_silent"], 0, 10, [])
    assert r["verdict"] == "REVIEW", r
    assert -60 < r["mean_db"] <= -45, r
    assert any("near-silent" in x for x in r["reasons"]), r
    # Same near-silent audio WITH speech -> CLEAR (speech exempts it).
    r = cp.check_clip(fx["near_silent"], 0, 10, WORDS)
    assert r["verdict"] == "CLEAR", r


def test_sustained_bed_heuristic(fx):
    # Steady tone, no speech -> sustained bed -> REVIEW (not BLOCK).
    r = cp.check_clip(fx["steady"], 0, 10, [])
    assert r["sustained_bed"] is True, r
    assert r["verdict"] == "REVIEW", r
    assert any("music bed" in x for x in r["reasons"]), r
    # Transient bursts over a noise floor, no speech -> CLEAR.
    # (This is the C25 kai-bus-fireball shape: no speech, loud moments.)
    r = cp.check_clip(fx["transient"], 0, 10, [])
    assert r["sustained_bed"] is False, r
    assert r["verdict"] == "CLEAR", r
    # Steady tone WITH speech -> not a bed (buckets skipped) -> CLEAR.
    r = cp.check_clip(fx["steady"], 0, 10, WORDS)
    assert r["sustained_bed"] is False, r
    assert r["verdict"] == "CLEAR", r


def test_no_audio_stream(fx):
    r = cp.check_clip(fx["video_only"], 0, 10, WORDS)
    assert r["verdict"] == "REVIEW", r
    assert r["verdict"] != "CLEAR", r
    assert r["mean_db"] is None, r
    assert any("no audio stream" in x for x in r["reasons"]), r


def test_blocklist_missing_and_malformed(fx, tmp):
    old = cp.BLOCKLIST_PATH
    try:
        # Missing file -> REVIEW, never a crash, never CLEAR.
        cp.BLOCKLIST_PATH = tmp / "does-not-exist.yaml"
        r = cp.check_clip(fx["healthy"], 0, 10, WORDS, streamer="Anyone")
        assert r["verdict"] == "REVIEW", r
        assert any("blocklist missing" in x for x in r["reasons"]), r
        # Malformed YAML -> REVIEW, never a crash.
        bad = tmp / "bad.yaml"
        bad.write_text("blocklist: [unclosed\n  - : : :\n", encoding="utf-8")
        cp.BLOCKLIST_PATH = bad
        r = cp.check_clip(fx["healthy"], 0, 10, WORDS, streamer="Anyone")
        assert r["verdict"] == "REVIEW", r
        assert any("blocklist unparseable" in x for x in r["reasons"]), r
        # Valid entry, matching streamer -> BLOCK with the name recorded.
        good = tmp / "good.yaml"
        good.write_text("blocklist:\n"
                        "- name: NoClipsPlz\n  handle: noclipsplz\n"
                        "  platform: Twitch\n"
                        "  reason: asked for no clips\n"
                        "  evidence: https://example.invalid/proof\n"
                        "  date_added: '2026-11-25'\n  added_by: Ajay\n",
                        encoding="utf-8")
        cp.BLOCKLIST_PATH = good
        r = cp.check_clip(fx["healthy"], 0, 10, WORDS, streamer="noclipsplz")
        assert r["verdict"] == "BLOCK", r
        assert r["blocklist_hit"] == "NoClipsPlz", r
        assert any("opt-out" in x for x in r["reasons"]), r
        # Same file, different streamer -> no hit -> CLEAR.
        r = cp.check_clip(fx["healthy"], 0, 10, WORDS, streamer="OtherGuy")
        assert r["verdict"] == "CLEAR", r
        assert r["blocklist_hit"] is None, r
    finally:
        cp.BLOCKLIST_PATH = old


def test_fingerprint_hook_disabled():
    assert cp.FINGERPRINT_ENABLED is False
    assert cp.fingerprint_check("whatever.mp4", 0, 10) is None


def test_missing_source_never_clear():
    r = cp.check_clip("/tmp/does-not-exist-xyz.mp4", 0, 10, WORDS)
    assert r["verdict"] == "REVIEW", r
    assert r["mean_db"] is None, r


def test_result_shape(fx):
    r = cp.check_clip(fx["healthy"], 0, 10, WORDS, streamer="Speed")
    for key in ("verdict", "mean_db", "max_db", "muted_segments",
                "muted_fraction", "word_count", "sustained_bed",
                "blocklist_hit", "reasons"):
        assert key in r, f"missing key {key} in {r}"
    assert r["word_count"] == 3, r
    assert r["verdict"] in ("CLEAR", "REVIEW", "BLOCK"), r
    assert isinstance(r["reasons"], list), r


def main():
    tests = [test_verdict_precedence, test_blocklist_empty_by_default,
             test_muted_thresholds, test_sustained_bed_heuristic,
             test_no_audio_stream, test_blocklist_missing_and_malformed,
             test_fingerprint_hook_disabled, test_missing_source_never_clear,
             test_result_shape]
    passed, failed = 0, []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "fx").mkdir(exist_ok=True)
        fx = _fixtures(tmp / "fx")
        for t in tests:
            try:
                if t.__code__.co_argcount == 2:
                    t(fx, tmp)
                elif t.__code__.co_argcount == 1:
                    t(fx)
                else:
                    t()
                passed += 1
                print(f"PASS {t.__name__}")
            except Exception as e:
                failed.append(t.__name__)
                print(f"FAIL {t.__name__}: {e!r}")
    print(f"\n{passed} passed, {len(failed)} failed "
          f"of {len(tests)} tests")
    if failed:
        print("failed:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
