"""Shared yt-dlp SSL workaround for the Clipper Engine.

Root cause (verified 2026-11-19): this VM's egress proxy presents a
self-signed certificate in the chain. yt-dlp ships its own certifi CA
bundle and ignores the VM's egress CA bundle
(/run/hatch/egress-tls/ca-bundle.pem, also set as SSL_CERT_FILE), so
every YouTube call fails with:
  SSL: CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain
`streams.py` already tries `truststore.inject_into_ssl()`; that does not
fix yt-dlp here. `--no-check-certificates` does (verified live).

Security note: this only skips certificate verification for yt-dlp's
*public* YouTube/Twitch search + metadata calls. Never use it for a
login, a cookie, a password, or any authenticated request, and never
paste cookies/credentials into any script in this folder.

Second SSL fix (verified 2026-11-19): Hugging Face model downloads
(faster-whisper) stall the same way — huggingface_hub uses certifi and
ignores SSL_CERT_FILE, leaving .incomplete blobs. Pointing
REQUESTS_CA_BUNDLE at the VM's egress CA bundle makes the download
work (tiny model downloaded in ~2s). patch_yt_dlp() sets both vars.
"""
import os
import sys
from pathlib import Path

from clipper.paths import repo_root

EGRESS_CA = "/run/hatch/egress-tls/ca-bundle.pem"


# --- Item #7 (2026-11-25): yt-dlp hardened retry profile -----------------
# From yt-dlp's own guide (docs/pipeline-upgrade-research-20261125.md #7):
#   --socket-timeout 30 --retries infinite --fragment-retries infinite
#   --retry-sleep fragment:exp=1:20 --continue --throttled-rate 100K
#   --download-archive archive.txt --no-abort-on-error
# Retry flags fix stalls/timeouts, NOT CERTIFICATE_VERIFY_FAILED — that
# is still the --no-check-certificates workaround above; do not confuse
# the two. The archive flag is deliberately NOT in the patched
# ytdlp_base(): it needs a per-download path, so each download call site
# adds it via hardened_download_args(). Everything else rides in the
# patched base, so every ytdlp_base()/ytdlp_json() call in the repo gets
# it (metadata -J calls included — the flags are harmless there).
#
# Verified 2026-11-25 against yt-dlp 2026.11.23: the HLS fragment path
# retries each fragment through an inner HTTP downloader, whose sleep
# key is 'http', not 'fragment' — with only fragment:exp=1:20 set, 5
# failed fragments retried immediately (2.3 s total, no sleep at all).
# Adding http:exp=1:20 as well gives the measured backoff (1 s, 2 s,
# 4 s, ... capped at 20 s) on the path that actually fires for our HLS
# sources. The fragment key is kept as specified: it governs the outer
# DASH/ISM fragment loop.
SOCKET_TIMEOUT_SECS = 30
RETRY_SLEEP_FRAGMENT = "fragment:exp=1:20"
RETRY_SLEEP_HTTP = "http:exp=1:20"
THROTTLED_RATE = "100K"
THROTTLED_RATE_BYTES = 100 * 1024  # what --throttled-rate 100K means
ARCHIVE_FILENAME = "archive.txt"
DEFAULT_ESTIMATED_BYTES = 500 * 1024 * 1024  # 500 MB, disk-guard default

# CLI flags that are safe on every call (metadata -J as well as
# downloads). --download-archive is excluded here: it needs a
# per-download path (see hardened_download_args()).
HARDENED_CLI_FLAGS = (
    "--socket-timeout", "30",
    "--retries", "infinite",
    "--fragment-retries", "infinite",
    "--retry-sleep", RETRY_SLEEP_FRAGMENT,
    "--retry-sleep", RETRY_SLEEP_HTTP,
    "--continue",
    "--throttled-rate", THROTTLED_RATE,
    "--no-abort-on-error",
)


def _exp_retry_sleep(n):
    """exp=1:20 backoff, exactly as yt-dlp's own parse_sleep_func builds
    it: min(1 * 2**n, 20) seconds. yt-dlp calls the sleep function with
    n = count - 1, so the sleeps actually seen are 1 s, 2 s, 4 s, 8 s,
    16 s, then 20 s."""
    return min(1.0 * (2.0 ** int(n)), 20.0)


def hardened_cli_args(archive_path=ARCHIVE_FILENAME):
    """Full hardened CLI flag list (item #7 + the http-sleep note above).

    archive_path: where --download-archive records completed IDs, so
    reruns skip them. Pass None to omit the flag (what the patched
    ytdlp_base() does — metadata calls must not create an archive file
    in whatever the cwd happens to be).
    """
    args = list(HARDENED_CLI_FLAGS)
    if archive_path is not None:
        args += ["--download-archive", str(archive_path)]
    return args


def hardened_download_args(archive_path):
    """Download-only flags for a real download call site.

    The retry/socket/throttled flags already ride in the patched
    ytdlp_base(); this adds only the per-download archive, so a rerun
    skips IDs that already completed.
    """
    return ["--download-archive", str(archive_path)]


def hardened_ydl_opts(archive_path=None):
    """YoutubeDL-API equivalent of hardened_cli_args().

    Key mapping (verified against yt-dlp 2026.10.26 source):
    --retries/--fragment-retries "infinite" -> float("inf");
    --retry-sleep fragment:/http:exp=1:20 -> retry_sleep_functions
    (yt-dlp's own exp parse: min(1 * 2**n, 20)); --continue ->
    continuedl; --throttled-rate 100K -> throttledratelimit (bytes/s);
    --no-abort-on-error -> ignoreerrors (its compat alias);
    --no-check-certificates -> nocheckcertificate (same egress-proxy
    behaviour as patch_yt_dlp()). No YoutubeDL-API call site exists in
    the repo today (all calls go through the ytdlp_base() CLI funnel);
    this is the shared dict for any future one.
    """
    opts = {
        "socket_timeout": SOCKET_TIMEOUT_SECS,
        "retries": float("inf"),
        "fragment_retries": float("inf"),
        "retry_sleep_functions": {
            "fragment": _exp_retry_sleep,
            "http": _exp_retry_sleep,
        },
        "continuedl": True,
        "throttledratelimit": THROTTLED_RATE_BYTES,
        "ignoreerrors": True,
        "nocheckcertificate": True,
    }
    if archive_path is not None:
        opts["download_archive"] = str(archive_path)
    return opts


def gtavi_scripts_dir():
    """Where the vendored gtavi-claudecode scripts/ live.

    Order: $CLIPPER_GTAVI_SCRIPTS, then the vendored copy in this
    repo (src/clipper/infra/gtavi), then the legacy spots — the old
    fly/gtavi-scripts copy and the original local checkout. The
    first directory containing common.py wins."""
    import os as _os
    root = repo_root()
    cands = [
        Path(_os.environ["CLIPPER_GTAVI_SCRIPTS"])
        if _os.environ.get("CLIPPER_GTAVI_SCRIPTS") else None,
        root / "src" / "clipper" / "infra" / "gtavi",
        root / "fly" / "gtavi-scripts",
        Path.home() / "workspace" / "gta-test" / "gtavi-claudecode" / "scripts",
    ]
    for d in cands:
        if d is not None and (d / "common.py").is_file():
            return d
    return cands[-1]

GTAVI = Path.home() / "workspace/gta-test/gtavi-claudecode"
SCRIPTS = gtavi_scripts_dir()
VENV_PY = GTAVI / ".venv/bin/python"


def patch_yt_dlp():
    """Patch every gtavi module's yt-dlp entry points to skip cert checks.

    Call BEFORE importing streams/trends/moments/cut, then re-patch the
    names those modules imported by value.

    Besides --no-check-certificates (unchanged behaviour), the patched
    ytdlp_base() now also carries the item #7 hardened retry profile
    (hardened_cli_args(archive_path=None) — every flag except
    --download-archive, which needs a per-download path and is added by
    each download call site via hardened_download_args()).
    """
    os.environ.setdefault("REQUESTS_CA_BUNDLE", EGRESS_CA)
    os.environ.setdefault("SSL_CERT_FILE", EGRESS_CA)
    sys.path.insert(0, str(SCRIPTS))
    import common  # noqa: F401  (imported for its module object)

    orig = common.ytdlp_base

    def patched():
        return orig() + ["--no-check-certificates"] + hardened_cli_args(archive_path=None)

    common.ytdlp_base = patched
    return common


def patch_module(mod):
    """Rebind a module's by-value imports of ytdlp_base / ytdlp_json."""
    import common

    if hasattr(mod, "ytdlp_base"):
        mod.ytdlp_base = common.ytdlp_base
    if hasattr(mod, "ytdlp_json"):
        mod.ytdlp_json = common.ytdlp_json
