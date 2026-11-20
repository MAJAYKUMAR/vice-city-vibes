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

EGRESS_CA = "/run/hatch/egress-tls/ca-bundle.pem"

GTAVI = Path.home() / "workspace/gta-test/gtavi-claudecode"
SCRIPTS = GTAVI / "scripts"
VENV_PY = GTAVI / ".venv/bin/python"


def patch_yt_dlp():
    """Patch every gtavi module's yt-dlp entry points to skip cert checks.

    Call BEFORE importing streams/trends/moments/cut, then re-patch the
    names those modules imported by value.
    """
    os.environ.setdefault("REQUESTS_CA_BUNDLE", EGRESS_CA)
    os.environ.setdefault("SSL_CERT_FILE", EGRESS_CA)
    sys.path.insert(0, str(SCRIPTS))
    import common  # noqa: F401  (imported for its module object)

    orig = common.ytdlp_base

    def patched():
        return orig() + ["--no-check-certificates"]

    common.ytdlp_base = patched
    return common


def patch_module(mod):
    """Rebind a module's by-value imports of ytdlp_base / ytdlp_json."""
    import common

    if hasattr(mod, "ytdlp_base"):
        mod.ytdlp_base = common.ytdlp_base
    if hasattr(mod, "ytdlp_json"):
        mod.ytdlp_json = common.ytdlp_json
