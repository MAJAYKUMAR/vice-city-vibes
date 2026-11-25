#!/usr/bin/env python3
"""Tests for item #7 — yt-dlp hardened retries (ssl_fix.py).

Plain asserts, no pytest (pytest is not installed on this VM). Run from
the clipper-engine root:

    python3 tests/test_ytdlp_hardening.py

What it proves:
  (a) the hardened opts/args contain every flag from the research spec
      (--socket-timeout 30, --retries infinite, --fragment-retries
      infinite, --retry-sleep fragment:exp=1:20, --continue,
      --throttled-rate 100K, --download-archive archive.txt,
      --no-abort-on-error), and patch_yt_dlp() behaviour is unchanged
      (--no-check-certificates still applied exactly once, env vars
      still set, original base flags preserved);
  (b) transient failure: a local flaky server (HTTP 500 on the first
      attempts, then 200) is retried with exponential backoff and the
      download then SUCCEEDS — once for a plain file download, once for
      HLS fragments — and a rerun with the same --download-archive
      skips the completed ID without re-requesting it;
  (c) permanent failure: a 404 (and, live, a removed YouTube video)
      exits non-zero PROMPTLY — no infinite loop, no hang.

Everything runs against a local HTTP server + temp dirs — no repo
files are touched. The YouTube check in (c) needs network; if the
verdict is not a clean "video is unavailable" it is reported as SKIP
rather than failing the suite. The YoutubeDL-API check runs only when
the yt_dlp module is importable (gtavi venv); under system python3 it
prints SKIP — the CLI checks above are the ones that gate the result.
"""
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ENGINE = Path(__file__).resolve().parents[1]

# --- yt-dlp binary -------------------------------------------------------
YTDLP = shutil.which("yt-dlp") or str(Path.home() / ".local/bin" / "yt-dlp")
assert Path(YTDLP).exists(), f"yt-dlp binary not found (looked on PATH and at {YTDLP})"
# common.ytdlp_base() falls back to shutil.which("yt-dlp") when the
# yt_dlp module is absent (system python3), so make sure it is on PATH.
os.environ["PATH"] = str(Path(YTDLP).parent) + os.pathsep + os.environ.get("PATH", "")

from clipper.infra import ssl_fix  # noqa: E402

SPEC_CLI = [
    ("--socket-timeout", "30"),
    ("--retries", "infinite"),
    ("--fragment-retries", "infinite"),
    ("--retry-sleep", "fragment:exp=1:20"),
    ("--continue", None),
    ("--throttled-rate", "100K"),
    ("--download-archive", "archive.txt"),
    ("--no-abort-on-error", None),
]


def _flag_pairs(args):
    """--flag value pairs (value None for bare flags) from a flat list."""
    pairs, i = [], 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("--") and i + 1 < len(args) and not args[i + 1].startswith("--"):
            pairs.append((tok, args[i + 1]))
            i += 2
        else:
            pairs.append((tok, None))
            i += 1
    return pairs


def check_a_unit():
    print("== (a) hardened opts/args + patch_yt_dlp() unchanged ==")
    # --- CLI args contain every spec flag ---
    args = ssl_fix.hardened_cli_args()
    pairs = _flag_pairs(args)
    for flag, value in SPEC_CLI:
        assert (flag, value) in pairs, f"hardened_cli_args() missing {flag} {value}: {args}"
    # archive omitted on request (patched base must not create one in cwd)
    assert "--download-archive" not in ssl_fix.hardened_cli_args(archive_path=None)
    # download-only helper is exactly the archive flag
    assert ssl_fix.hardened_download_args("/tmp/x/archive.txt") == [
        "--download-archive", "/tmp/x/archive.txt"]
    print(f"  hardened_cli_args() = {args}")

    # --- YoutubeDL-API opts mirror the CLI flags ---
    opts = ssl_fix.hardened_ydl_opts()
    assert opts["socket_timeout"] == 30, opts
    assert opts["retries"] == float("inf"), opts
    assert opts["fragment_retries"] == float("inf"), opts
    assert opts["continuedl"] is True, opts
    assert opts["throttledratelimit"] == 100 * 1024, opts  # --throttled-rate 100K
    assert opts["ignoreerrors"] is True, opts  # --no-abort-on-error
    assert opts["nocheckcertificate"] is True, opts  # --no-check-certificates
    assert "download_archive" not in opts
    assert ssl_fix.hardened_ydl_opts("/tmp/x/a.txt")["download_archive"] == "/tmp/x/a.txt"
    # exp=1:20 sleep: yt-dlp calls the fn with n = count-1, so the
    # sleeps seen are 1, 2, 4, 8, 16, then 20s.
    for key in ("fragment", "http"):
        fn = opts["retry_sleep_functions"][key]
        got = [fn(n) for n in range(7)]
        assert got == [1.0, 2.0, 4.0, 8.0, 16.0, 20.0, 20.0], (key, got)
    print("  hardened_ydl_opts() keys/values OK; sleep fn = [1,2,4,8,16,20,20]s")

    # --- patch_yt_dlp() behaviour unchanged ---
    common = ssl_fix.patch_yt_dlp()
    base = common.ytdlp_base()
    assert base.count("--no-check-certificates") == 1, base
    assert "--no-warnings" in base and "--no-progress" in base, base  # orig() preserved
    for flag, value in SPEC_CLI:
        if flag == "--download-archive":
            continue  # archive is per-download, not in the patched base
        assert (flag, value) in _flag_pairs(base), f"patched base missing {flag} {value}: {base}"
    assert "--download-archive" not in base, base
    assert os.environ.get("REQUESTS_CA_BUNDLE") == ssl_fix.EGRESS_CA
    assert os.environ.get("SSL_CERT_FILE") == ssl_fix.EGRESS_CA
    print(f"  patched ytdlp_base() = {base}")

    # patch_module() still rebinds by-value imports
    import types
    fake = types.SimpleNamespace(ytdlp_base=lambda: ["orig"], ytdlp_json=lambda u: {})
    ssl_fix.patch_module(fake)
    assert fake.ytdlp_base is common.ytdlp_base, "patch_module did not rebind ytdlp_base"
    print("  patch_module() rebind OK")

    # --- download call sites are wired through the shared helpers ---
    for rel, must in [
        ("src/clipper/infra/gtavi/moments.py", None),
        ("scripts/scan_vod.py", None),
        ("scripts/live_heat.py", None),
    ]:
        src = (ENGINE / rel).read_text(encoding="utf-8")
        assert "hardened_download_args" in src, f"{rel}: no hardened_download_args"
        assert "from clipper.core.disk_guard import preflight" in src, f"{rel}: no preflight import"
        assert "preflight(" in src, f"{rel}: no preflight() call"
    print("  moments.py / scan_vod.py / live_heat.py: archive flag + preflight() present")
    # a real download command built the production way carries everything
    with tempfile.TemporaryDirectory() as td:
        arch = str(Path(td) / "archive.txt")
        cmd = common.ytdlp_base() + ssl_fix.hardened_download_args(arch)
        for flag, value in SPEC_CLI:
            want = arch if flag == "--download-archive" else value
            assert (flag, want) in _flag_pairs(cmd), f"download cmd missing {flag} {want}"
    print("  production download command carries every spec flag")
    print("  (a) PASS")


# --- flaky local server ----------------------------------------------------
PAYLOAD = b"X" * 200_000


def make_server(root, fail_video=(), fail_frag=0):
    """fail_video: request numbers (counting per-path GETs to /video.mp4)
    that answer 500 before succeeding. Which numbers are the download
    attempts depends on the caller: a single yt-dlp process extracts
    (#1) then downloads (#2, #3, ...), so fail {2, 3} flakes the
    download; moments.fetch() probes with -J first (#1) and the download
    process re-extracts (#2), so its download attempts are #3, #4, ...
    and fail {3, 4} flakes the download. (An extraction-phase 500 is
    fatal immediately — --retries does not cover it — so the extraction
    requests must succeed; that is pre-existing yt-dlp behaviour, not
    something the item #7 flags change.)
    fail_frag: /playlist2.ts attempts that answer 500. /missing.mp4
    always 404s."""
    counts = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/octet-stream"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def do_GET(self):
            p = self.path
            counts[p] = counts.get(p, 0) + 1
            n = counts[p]
            if p == "/missing.mp4":
                return self._send(404, b"not found")
            if p == "/video.mp4":
                if n in set(fail_video):
                    return self._send(500, b"boom")
                return self._send(200, PAYLOAD, "video/mp4")
            if p == "/playlist.m3u8":
                return self._send(200, (root / "playlist.m3u8").read_bytes(),
                                  "application/vnd.apple.mpegurl")
            if p.startswith("/playlist") and p.endswith(".ts"):
                if p == "/playlist2.ts" and n <= fail_frag:
                    return self._send(500, b"boom")
                return self._send(200, (root / p.lstrip("/")).read_bytes(), "video/MP2T")
            return self._send(404, b"nope")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, counts


def make_hls_fixtures(d):
    """6 x 1 s MPEG-TS segments + playlist, via ffmpeg testsrc."""
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi",
                        "-i", "testsrc=duration=6:size=64x64:rate=10",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10",
                        "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
                        str(d / "playlist.m3u8")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"ffmpeg fixture build failed: {r.stderr[-300:]}"
    assert (d / "playlist.m3u8").exists()
    assert len(list(d.glob("playlist*.ts"))) == 6
    return True


def run_ytdlp(url, out, archive, extra=()):
    from clipper.infra.ssl_fix import hardened_download_args  # noqa: E402
    import common  # noqa: E402  (patched by check_a_unit via ssl_fix)
    cmd = (common.ytdlp_base() + hardened_download_args(archive)
           + ["-o", str(out), url, *extra])
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return r, time.time() - t0


def check_b_transient(tmp):
    print("== (b) transient failure: retry with backoff, then SUCCEED ==")
    # --- plain file download: 2 x HTTP 500, then 200 ---
    srv, counts = make_server(tmp, fail_video={2, 3})
    port = srv.server_address[1]
    out = tmp / "video.mp4"
    archive = tmp / "archive.txt"
    r, el = run_ytdlp(f"http://127.0.0.1:{port}/video.mp4", out, archive)
    assert r.returncode == 0, f"flaky http download failed rc={r.returncode}: {(r.stderr or '')[-400:]}"
    assert counts["/video.mp4"] == 4, f"expected 4 requests (1 probe + 2 failed + 1 ok), got {counts}"
    assert out.exists() and out.read_bytes() == PAYLOAD, "downloaded bytes != served payload"
    # sleeps for the 2 http retries are 1s + 2s (exp=1:20, n=count-1)
    assert el >= 3.0, f"no backoff observed: elapsed {el:.1f}s for 2 retries"
    assert archive.exists() and archive.read_text().strip(), "archive.txt not written"
    print(f"  http 500x2 -> OK: rc=0 elapsed={el:.1f}s requests={counts['/video.mp4']} "
          f"bytes={out.stat().st_size} archive={archive.read_text().strip()!r}")
    # --- rerun with the same archive: completed ID is skipped ---
    # yt-dlp still makes ONE extraction probe, then sees the ID in the
    # archive and skips the download (no second download request).
    before = dict(counts)
    size_before = out.stat().st_size
    r2, el2 = run_ytdlp(f"http://127.0.0.1:{port}/video.mp4", out, archive)
    assert r2.returncode == 0, f"archive rerun failed rc={r2.returncode}"
    assert counts["/video.mp4"] == before["/video.mp4"] + 1, (
        f"archive rerun should add exactly the extraction probe: {before} -> {counts}")
    assert out.stat().st_size == size_before, "archive rerun rewrote the file"
    assert "has already been recorded in the archive" in (r2.stdout or ""), (
        f"no archive-skip message; stdout tail: {(r2.stdout or '')[-300:]}")
    print(f"  archive rerun: rc=0 elapsed={el2:.1f}s, +1 probe request only, "
          f"'has already been recorded in the archive' (ID skipped)")
    srv.shutdown()

    # --- HLS fragments: segment 2 fails 2 x HTTP 500, then 200 ---
    if not make_hls_fixtures(tmp):
        print("  SKIP hls fragment scenario: ffmpeg not found")
        return
    srv, counts = make_server(tmp, fail_frag=2)
    port = srv.server_address[1]
    out = tmp / "hls.ts"
    archive = tmp / "archive-hls.txt"
    r, el = run_ytdlp(f"http://127.0.0.1:{port}/playlist.m3u8", out, archive)
    assert r.returncode == 0, f"flaky hls download failed rc={r.returncode}: {(r.stderr or '')[-400:]}"
    assert counts["/playlist2.ts"] == 3, f"expected 3 requests for seg 2, got {counts}"
    assert out.exists() and out.stat().st_size > 0, "hls output missing/empty"
    assert el >= 3.0, f"no backoff observed on fragments: elapsed {el:.1f}s for 2 retries"
    print(f"  hls frag 500x2 -> OK: rc=0 elapsed={el:.1f}s seg2_requests={counts['/playlist2.ts']} "
          f"size={out.stat().st_size}")
    srv.shutdown()
    print("  (b) PASS")


def check_moments_fetch(home_tmp):
    """End-to-end through the real moments.fetch() call site: the
    disk-guard pre-flight runs against the real disk_guard module, the
    download rides the patched ytdlp_base(), and the archive lands in
    the job dir. The job dir is on /home (65 GB free); /tmp is a 512 MB
    tmpfs, so the 500 MB default pre-flight would be marginal there."""
    print("== (moments) fetch() end-to-end through the real call site ==")
    try:
        import moments  # noqa: E402  (gtavi dir is on sys.path after patch)
    except ImportError as exc:
        print(f"  SKIP: cannot import moments: {exc}")
        return
    srv, counts = make_server(home_tmp, fail_video={3, 4})
    port = srv.server_address[1]
    d = home_tmp / "job-moments"
    d.mkdir(exist_ok=True)
    t0 = time.time()
    vid, info = moments.fetch(f"http://127.0.0.1:{port}/video.mp4", d, 90)
    el = time.time() - t0
    assert vid == d / "source.mp4", vid
    assert vid.exists() and vid.read_bytes() == PAYLOAD, "moments.fetch bytes != payload"
    assert counts["/video.mp4"] == 5, (
        f"expected 5 requests (probe + download-extract + 2 failed + 1 ok), got {counts}")
    assert el >= 3.0, f"no backoff observed in moments.fetch: elapsed {el:.1f}s"
    assert (d / "archive.txt").exists() and (d / "archive.txt").read_text().strip()
    assert (d / "info.json").exists()
    print(f"  moments.fetch flaky 500x2 -> OK: elapsed={el:.1f}s requests={counts['/video.mp4']} "
          f"bytes={vid.stat().st_size} archive present (preflight passed against real disk_guard)")
    srv.shutdown()
    print("  (moments) PASS")


def check_c_permanent(tmp):
    print("== (c) permanent failure: non-zero exit, PROMPTLY, no hang ==")
    srv, counts = make_server(tmp)
    port = srv.server_address[1]
    out = tmp / "missing.mp4"
    r, el = run_ytdlp(f"http://127.0.0.1:{port}/missing.mp4", out, tmp / "archive-404.txt")
    assert r.returncode != 0, f"404 download unexpectedly succeeded rc={r.returncode}"
    assert el < 30, f"404 download took too long: {el:.1f}s"
    assert counts["/missing.mp4"] == 1, f"404 was retried?! requests={counts}"
    assert not out.exists(), "404 download left an output file"
    print(f"  local 404: rc={r.returncode} elapsed={el:.1f}s requests={counts['/missing.mp4']} "
          f"(no retry loop, no hang)")
    srv.shutdown()

    # Live YouTube check: a removed/unavailable video ID. Informational
    # unless yt-dlp gives a clean "unavailable" verdict.
    import common  # noqa: E402
    from clipper.infra.ssl_fix import hardened_download_args  # noqa: E402
    cmd = (common.ytdlp_base() + hardened_download_args(tmp / "archive-yt.txt")
           + ["--skip-download", "-J", "https://www.youtube.com/watch?v=xxxxxxxxxxx"])
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        el = time.time() - t0
        tail = ((r.stderr or "") + (r.stdout or ""))[-300:]
        if "This video is unavailable" in tail:
            assert r.returncode != 0, "unavailable YouTube video exited 0"
            assert el < 60, f"unavailable YouTube video took too long: {el:.1f}s"
            print(f"  youtube bogus ID: rc={r.returncode} elapsed={el:.1f}s "
                  f"('This video is unavailable', no hang)")
        else:
            print(f"  SKIP youtube check (no clean verdict, rc={r.returncode} "
                  f"elapsed={el:.1f}s): {tail.strip().splitlines()[-1][:160] if tail.strip() else ''}")
    except subprocess.TimeoutExpired:
        print("  SKIP youtube check: timed out after 120s (network-dependent)")
    print("  (c) PASS")


def check_api_if_available(tmp):
    print("== (api) YoutubeDL with hardened_ydl_opts() ==")
    try:
        import yt_dlp  # noqa: E402
    except ImportError:
        print("  SKIP: yt_dlp module not importable under this python "
              "(run with the gtavi venv python to cover the API path)")
        return
    print(f"  yt_dlp {yt_dlp.version.__version__} available")
    srv, counts = make_server(tmp, fail_video={2, 3})
    port = srv.server_address[1]
    out = tmp / "api.mp4"
    opts = ssl_fix.hardened_ydl_opts(archive_path=tmp / "archive-api.txt")
    opts.update({"outtmpl": str(out), "quiet": True, "no_warnings": True})
    t0 = time.time()
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"http://127.0.0.1:{port}/video.mp4"])
    el = time.time() - t0
    assert counts["/video.mp4"] == 4, f"API path: expected 4 requests, got {counts}"
    assert out.exists() and out.read_bytes() == PAYLOAD, "API path: bytes != payload"
    assert el >= 3.0, f"API path: no backoff observed, elapsed {el:.1f}s"
    print(f"  api flaky http 500x2 -> OK: elapsed={el:.1f}s requests={counts['/video.mp4']} "
          f"bytes={out.stat().st_size}")
    srv.shutdown()
    print("  (api) PASS")


def main():
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="ytdlp-hardening-") as td:
        tmp = Path(td)
        check_a_unit()
        check_b_transient(tmp)
        check_c_permanent(tmp)
        check_api_if_available(tmp)
    with tempfile.TemporaryDirectory(prefix="ytdlp-hardening-", dir=str(Path.home())) as td:
        check_moments_fetch(Path(td))
    print(f"\nALL CHECKS PASSED in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
