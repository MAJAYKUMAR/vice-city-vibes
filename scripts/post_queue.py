#!/usr/bin/env python3
"""Post queue — upload queued clips to YouTube (Shorts) via the official API.

Reads garage/queue.json — the queue file SHARED with the garage editor
(workstream D). Each entry is:

    {"id": str, "clip_path": str, "title": str, "caption": str,
     "destination": "youtube", "status": "queued|posted|failed",
     "created": ISO timestamp, "youtube_video_id": str|null}

Optional extras an entry may carry (never required): "thumbnail"
(path to a jpg/png), "privacy" ("private"|"unlisted"|"public"),
"tags" (list of str), "category_id" (str, YouTube category id).

Scope: YouTube ONLY. There is deliberately NO Instagram / Facebook
posting here — those go through Meta Business Suite manually.

Usage:
    python3 scripts/post_queue.py                  # dry run (default)
    python3 scripts/post_queue.py --dry-run        # same, explicit
    python3 scripts/post_queue.py --live           # real upload
    python3 scripts/post_queue.py --live --id <entry-id>
    python3 scripts/post_queue.py --live --privacy unlisted
    python3 scripts/post_queue.py --queue garage/queue.json

DRY-RUN (the default, and the behaviour with no --live flag) never
contacts YouTube or any network. It prints exactly what WOULD be
uploaded and validates each clip file exists and is a readable video
(ffprobe when available, else a file-size/header sanity check).

--live uploads via the official YouTube Data API v3 (videos.insert).
It refuses to run when OAuth credentials are absent — see
docs/youtube-upload-oauth.md for setup. On success it writes back
youtube_video_id and sets status "posted"; on failure it sets status
"failed" and records the error. The queue file is always rewritten
atomically (temp file + rename) and is never deleted or corrupted.
Dry-run NEVER modifies the queue file.

Auto-posting is OFF by default: nothing uploads unless --live is
passed explicitly. Credential values are never printed, logged,
or stored by this script.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUEUE = REPO_ROOT / "garage" / "queue.json"

# OAuth files live OUTSIDE the repo. Never put them under REPO_ROOT.
CONFIG_DIR = Path.home() / ".config" / "vice-city-vibes"
CLIENT_SECRETS = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "token.json"

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

# YouTube category ids: 20 = Gaming, 24 = Entertainment, 22 = People & Blogs.
DEFAULT_CATEGORY_ID = "20"  # Gaming — GTA 6 clips
DEFAULT_TAGS = ["gta6", "gtavi", "grandtheftauto6", "vicecity", "gta6clips",
                "gaming", "shorts"]
MAX_TITLE_CHARS = 100

# Default AI disclosure appended to the description. YouTube's
# videos.insert has NO field for the altered/synthetic-content setting,
# so the disclosure lives in the description text AND the uploader must
# confirm the "Altered content" setting in YouTube Studio after the
# upload (the script prints a reminder). Ajay: check this wording
# matches what the channel already uses on manual uploads, and pass
# --ai-disclosure to override it per run if it ever changes.
DEFAULT_AI_DISCLOSURE = (
    "AI-edited clip: this Short was cut and captioned with AI tools "
    "from a livestream moment."
)


def log(msg):
    print(msg, flush=True)


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_clip_path(clip_path):
    """Resolve a queue clip path: absolute as-is, else relative to the
    repo root first, then the current working directory."""
    p = Path(clip_path)
    if p.is_absolute():
        return p
    for base in (REPO_ROOT, Path.cwd()):
        cand = base / p
        if cand.exists():
            return cand
    return REPO_ROOT / p  # for a clear "not found" message


def validate_clip(path):
    """Check the file exists and is a readable video.

    Returns (ok, detail_str). Uses ffprobe when available; otherwise a
    stdlib-only size + container-header sanity check. Never touches the
    network.
    """
    if not path.exists():
        return False, f"file not found: {path}"
    if not path.is_file():
        return False, f"not a file: {path}"
    size = path.stat().st_size
    if size == 0:
        return False, f"file is empty (0 bytes): {path}"
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height",
                 "-show_entries", "format=duration",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as e:
            return False, f"ffprobe failed to run: {e}"
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            return False, f"ffprobe output unreadable for {path}: {r.stderr.strip()}"
        streams = data.get("streams") or []
        if not streams:
            return False, f"no video stream found (ffprobe): {path}"
        s = streams[0]
        dur = (data.get("format") or {}).get("duration", "?")
        return True, (f"video OK: {s.get('codec_name')} "
                      f"{s.get('width')}x{s.get('height')}, "
                      f"{dur}s, {size / 1_000_000:.1f} MB")
    # Fallback: no ffprobe — header sanity check only.
    with open(path, "rb") as f:
        head = f.read(16)
    if len(head) < 8 or b"ftyp" not in head[:12]:
        return False, (f"not a recognisable MP4 (no ftyp header, "
                       f"{size} bytes): {path}")
    return True, f"basic check OK: {size / 1_000_000:.1f} MB, MP4 header present (ffprobe not available)"


def build_upload_metadata(entry, privacy, ai_disclosure):
    """Build the title / description / tags / category for an entry.

    Returns (metadata_dict, notes_list). Title is capped at 100 chars,
    the YouTube limit; over-long titles are truncated and noted.
    """
    title = (entry.get("title") or "").strip()
    notes = []
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
        notes.append(f"title truncated to {MAX_TITLE_CHARS} chars (YouTube limit)")
    caption = (entry.get("caption") or "").strip()
    parts = [caption] if caption else []
    if ai_disclosure:
        parts.append(ai_disclosure)
    description = "\n\n".join(parts)
    tags = entry.get("tags") or list(DEFAULT_TAGS)
    category_id = str(entry.get("category_id") or DEFAULT_CATEGORY_ID)
    entry_privacy = entry.get("privacy") or privacy
    metadata = {
        "title": title,
        "description": description,
        "tags": tags,
        "category_id": category_id,
        "privacy": entry_privacy,
        # This channel's clips are not made for kids.
        "made_for_kids": False,
    }
    return metadata, notes


def load_queue(queue_path):
    if not queue_path.exists():
        return None
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"ERROR: cannot read queue file {queue_path}: {e}")
    if not isinstance(data, list):
        raise SystemExit(f"ERROR: queue file {queue_path} is not a JSON list")
    return data


def save_queue_atomic(queue_path, entries):
    """Rewrite the queue file atomically (temp file in the same dir +
    rename) so a crash can never leave a half-written queue."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(queue_path.parent),
                               prefix=queue_path.name + ".tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, queue_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def print_would_upload(entry, clip, validation, metadata, notes):
    log(f"\n--- WOULD UPLOAD [{entry.get('id')}] ---")
    log(f"  clip:     {clip}")
    log(f"  validate: {validation}")
    log(f"  title:    {metadata['title']!r} ({len(metadata['title'])}/{MAX_TITLE_CHARS} chars)")
    log(f"  caption:  {(entry.get('caption') or '')!r}")
    log(f"  description (as uploaded):")
    for line in metadata["description"].splitlines() or ["(empty)"]:
        log(f"    {line}")
    log(f"  privacy:  {metadata['privacy']}")
    log(f"  category: {metadata['category_id']} (20=Gaming)")
    log(f"  tags:     {', '.join(metadata['tags'])}")
    thumb = entry.get("thumbnail")
    if thumb:
        tp = resolve_clip_path(thumb)
        log(f"  thumbnail: {tp} ({'exists' if tp.exists() else 'MISSING — will be skipped'})")
    else:
        log("  thumbnail: none (YouTube will auto-pick a frame)")
    log("  made for kids: No")
    log("  altered/synthetic disclosure: in description; confirm the "
        "'Altered content' setting in YouTube Studio after upload")
    for n in notes:
        log(f"  NOTE: {n}")


def check_live_credentials():
    """Refuse --live cleanly when OAuth credentials are absent.

    Returns an error string, or None when the client-secrets file is
    present. No network access happens here."""
    if not CLIENT_SECRETS.exists():
        return (
            "REFUSED --live: YouTube OAuth credentials not found.\n"
            f"  Expected client secrets at: {CLIENT_SECRETS}\n"
            "  Set up OAuth first — follow docs/youtube-upload-oauth.md\n"
            "  (Google Cloud project -> enable YouTube Data API v3 ->\n"
            "   OAuth 2.0 client (Desktop app) -> download client_secret.json\n"
            "   to that path, outside the repo). Nothing was uploaded."
        )
    return None


def upload_live(entry, clip, metadata):
    """Upload one clip via the official YouTube Data API v3.

    Google API libraries are imported LAZILY here — and only here — so
    a missing googleapiclient install never breaks dry-run mode.
    Returns (video_id, error_str_or_None). Never prints credentials.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        return None, (
            "Google API libraries not installed "
            f"({e}). Install them with: "
            "pip install google-api-python-client google-auth-oauthlib "
            "google-auth-httplib2 — then re-run. Nothing was uploaded."
        )

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE),
                                                      [YOUTUBE_UPLOAD_SCOPE])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # refresh token: no browser needed
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRETS), [YOUTUBE_UPLOAD_SCOPE])
            # First run: opens the browser for Google sign-in + consent
            # to the channel's Google account. Token is then saved below.
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": metadata["category_id"],
        },
        "status": {
            "privacyStatus": metadata["privacy"],
            "selfDeclaredMadeForKids": metadata["made_for_kids"],
        },
    }
    media = MediaFileUpload(str(clip), mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response.get("id")
    if not video_id:
        return None, f"YouTube API returned no video id: {response!r:.200}"

    thumb = entry.get("thumbnail")
    if thumb:
        tp = resolve_clip_path(thumb)
        if tp.exists():
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(tp), resumable=True)).execute()
        else:
            log(f"  WARNING: thumbnail missing, skipped: {tp}")
    return video_id, None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Upload queued clips to YouTube (Shorts). "
                    "Dry-run is the default; --live uploads for real.")
    ap.add_argument("--queue", default=str(DEFAULT_QUEUE),
                    help="path to garage/queue.json (default: %(default)s)")
    ap.add_argument("--live", action="store_true",
                    help="actually upload to YouTube (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry-run (same as the default)")
    ap.add_argument("--id", dest="only_id", default=None,
                    help="process only the queue entry with this id")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N queued entries")
    ap.add_argument("--privacy", default="private",
                    choices=["private", "unlisted", "public"],
                    help="privacy for uploads (default: private — "
                         "say --privacy public explicitly to go public)")
    ap.add_argument("--ai-disclosure", default=DEFAULT_AI_DISCLOSURE,
                    help="AI/synthetic-content disclosure line appended to "
                         "the description (empty string to omit)")
    args = ap.parse_args(argv)

    live = args.live and not args.dry_run
    queue_path = Path(args.queue)
    entries = load_queue(queue_path)
    if entries is None:
        log(f"No queue file at {queue_path} — nothing to post.")
        return 0
    if not entries:
        log(f"Queue is empty ({queue_path}) — nothing to post.")
        return 0

    if live:
        refusal = check_live_credentials()
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
    else:
        log("DRY-RUN — nothing will be uploaded, no network contact, "
            "queue file will not be modified.")

    selected = []
    for e in entries:
        if e.get("destination") != "youtube":
            continue  # YouTube only; other destinations are not ours
        if e.get("status") != "queued":
            continue
        if args.only_id and e.get("id") != args.only_id:
            continue
        selected.append(e)
        if args.limit and len(selected) >= args.limit:
            break

    skipped = len(entries) - len(selected)
    if not selected:
        log(f"No queued YouTube entries to process "
            f"({len(entries)} total, {skipped} skipped/not queued).")
        return 0

    failures = 0
    for entry in selected:
        clip = resolve_clip_path(entry.get("clip_path") or "")
        ok, validation = validate_clip(clip)
        metadata, notes = build_upload_metadata(entry, args.privacy,
                                                args.ai_disclosure)
        if not entry.get("title"):
            ok, validation = False, "title is empty — set a title before posting"
        if not live:
            if ok:
                print_would_upload(entry, clip, validation, metadata, notes)
            else:
                log(f"\n--- WOULD UPLOAD [{entry.get('id')}] — VALIDATION FAILED ---")
                log(f"  clip:  {clip}")
                log(f"  error: {validation}")
                log("  (dry-run: queue file left unchanged)")
                failures += 1
            continue

        # --- live path ---
        if not ok:
            entry["status"] = "failed"
            entry["error"] = validation
            log(f"FAILED [{entry.get('id')}]: {validation}")
            failures += 1
            save_queue_atomic(queue_path, entries)
            continue
        print_would_upload(entry, clip, validation, metadata, notes)
        log(f"Uploading [{entry.get('id')}] to YouTube...")
        try:
            video_id, err = upload_live(entry, clip, metadata)
        except Exception as e:  # API / network / auth failure
            video_id, err = None, f"{type(e).__name__}: {e}"
        if err:
            entry["status"] = "failed"
            entry["error"] = err
            log(f"FAILED [{entry.get('id')}]: {err}")
            failures += 1
        else:
            entry["status"] = "posted"
            entry["youtube_video_id"] = video_id
            entry.pop("error", None)
            log(f"POSTED [{entry.get('id')}] — "
                f"https://www.youtube.com/watch?v={video_id}")
            log("  Reminder: confirm the 'Altered content' setting for this "
                "video in YouTube Studio (videos.insert has no field for it).")
        save_queue_atomic(queue_path, entries)

    done = len(selected) - failures
    if live:
        log(f"\nLive run done: {done} posted, {failures} failed, "
            f"{skipped} skipped. Queue saved to {queue_path}.")
    else:
        log(f"\nDry-run done: {done} would upload, {failures} failed "
            f"validation, {skipped} skipped. Queue file not modified.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
