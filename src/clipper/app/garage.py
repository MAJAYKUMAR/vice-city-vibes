#!/usr/bin/env python3
"""Video Garage — index of every clip the engine makes.

Clips are stored in garage/clips.json (newest first) and rendered to
garage/index.html. Each entry: file, 16:9 file, streamer/channel,
platform, moment, duration, hype stars, status (new / approved /
exported), source.

Usage:
    python -m clipper.app.garage register <clip.mp4> [--moment "text"] [--stars N]
    python -m clipper.app.garage approve <clip.mp4>
    python -m clipper.app.garage export <clip.mp4>     # marks exported (file already made by clip export)
    python -m clipper.app.garage delete <clip.mp4>     # removes entry + files
    python -m clipper.app.garage list
    python -m clipper.app.garage build                  # rebuild index.html from clips.json

Cap: 50 clips per stream — register() refuses #51 and says the engine
has parked that stream (same limit as the paid app).
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from clipper.paths import repo_root

HERE = repo_root()
GARAGE = HERE / "garage"
CLIPS_JSON = GARAGE / "clips.json"
INDEX_HTML = GARAGE / "index.html"
MAX_PER_STREAM = 50


def load():
    if not CLIPS_JSON.exists():
        return []
    return json.loads(CLIPS_JSON.read_text(encoding="utf-8"))


def save(rows):
    GARAGE.mkdir(exist_ok=True)
    CLIPS_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    build(rows)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _next_edit_path(clip):
    """First free <stem>-edit<N>.mp4 next to the original — never overwrite."""
    i = 1
    while True:
        cand = clip.with_name(f"{clip.stem}-edit{i}.mp4")
        if not cand.exists():
            return cand
        i += 1


def validate_trim(start, end, duration):
    """Trim-bounds check: 0 <= start < end <= duration.

    Returns an error string, or None when the trim is valid. Never
    produce a corrupt/empty file from a bad trim — callers refuse
    anything this rejects."""
    try:
        start = float(start)
        end = float(end)
        duration = float(duration)
    except (TypeError, ValueError):
        return "start and end must be numbers (seconds)"
    if duration <= 0:
        return "could not read the clip duration — refusing to trim"
    if start < 0:
        return f"start {start:g}s is before 0"
    if end <= start:
        return f"end {end:g}s must be after start {start:g}s"
    if end > duration:
        return f"end {end:g}s is past the clip duration ({duration:g}s)"
    return None


def save_edit(clip, start, end):
    """Trim a garage clip and save it as a NEW clip (never overwrites).

    clip: path (or garage file string) of a registered clip.
    start/end: seconds within the clip, validated by validate_trim().
    Returns the new garage row. Raises ValueError on bad input and
    RuntimeError if ffmpeg fails or the output is unusable (any
    partial output is deleted)."""
    clip = Path(clip).resolve()
    if not clip.is_file():
        raise ValueError(f"clip not found: {clip}")
    rows = load()
    src_row = next((r for r in rows if r.get("file") == str(clip)), None)
    if src_row is None:
        raise ValueError(f"clip is not in the garage: {clip}")
    duration = probe(clip)
    err = validate_trim(start, end, duration)
    if err:
        raise ValueError(err)
    start, end = float(start), float(end)

    out = _next_edit_path(clip)
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", f"{start:.2f}", "-to", f"{end:.2f}",
           "-i", str(clip),
           # veryfast (not medium): the editor is interactive — a 1080x1920
           # medium encode took >2 min on this shared-CPU VM and outlasted
           # a browser request. Same crf, same output, much faster.
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        if out.exists():
            out.unlink()
        raise RuntimeError(f"ffmpeg trim failed: {r.stderr.strip()[:300]}")
    out_dur = probe(out)
    if out_dur <= 0 or abs(out_dur - (end - start)) > 0.6:
        if out.exists():
            out.unlink()
        raise RuntimeError(
            f"trim output unusable (duration {out_dur}s, expected "
            f"{end - start:.1f}s) — partial file removed")

    # Sidecar .json next to the edit, mirroring the source clip's.
    src_meta_path = clip.with_suffix(".json")
    meta = (json.loads(src_meta_path.read_text(encoding="utf-8"))
            if src_meta_path.exists() else {})
    meta.update({
        "file": str(out),
        "edited_from": str(clip),
        "trim_start": start,
        "trim_end": end,
        "edit_of_start": meta.get("start"),
        "edit_of_end": meta.get("end"),
        "caption_style": "as-burned",
    })
    out.with_suffix(".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    row = dict(src_row)
    row.update({
        "file": str(out),
        "file_16x9": "",
        "duration_s": round(out_dur, 1),
        "status": "new",
        "edited_from": str(clip),
        "trim_start": start,
        "trim_end": end,
    })
    rows = [row] + [x for x in rows if x.get("file") != row["file"]]
    save(rows)
    print(f"garage: edited {clip.name} {start:.2f}-{end:.2f}s -> "
          f"{out.name} ({out_dur:.1f}s) — {len(rows)} clips total")
    return row


def register(clip, moment="", stars=0):
    clip = Path(clip).resolve()
    meta_path = clip.with_suffix(".json")
    meta = (json.loads(meta_path.read_text(encoding="utf-8"))
            if meta_path.exists() else {})
    stream = meta.get("source_channel") or meta.get("source_title") or "unknown"
    rows = load()
    same = [r for r in rows if r.get("stream") == stream]
    if len(same) >= MAX_PER_STREAM:
        print(f"PARKED: {stream} already has {MAX_PER_STREAM} clips — engine stops at 50 per stream.")
        return None
    row = {
        "file": str(clip),
        "file_16x9": str(clip.with_name(clip.stem + "_16x9.mp4"))
                    if clip.with_name(clip.stem + "_16x9.mp4").exists() else "",
        "stream": stream,
        "platform": "YouTube" if "youtube" in str(meta.get("source_url", "")).lower() else "local / own",
        "moment": moment or meta.get("hook") or meta.get("title") or "",
        "duration_s": round(probe(clip), 1),
        "stars": stars,
        "status": "blocked" if ((meta.get("safety") or {}).get("verdict") == "BLOCK"
                                or (meta.get("copyright") or {}).get("verdict") == "BLOCK") else "new",
        "safety": (meta.get("safety") or {}).get("verdict", "unknown"),
        "copyright": (meta.get("copyright") or {}).get("verdict", "unknown"),
        "layout": meta.get("layout", ""),
        "source_url": meta.get("source_url", ""),
        "source_title": meta.get("source_title", ""),
        # Virality (workstream A): moment entries in candidates.json may
        # carry virality (int 0-100) + virality_why (string). Defensive
        # .get — rows without them simply show no score.
        "virality": meta.get("virality"),
        "virality_why": meta.get("virality_why", ""),
    }
    rows = [row] + [r for r in rows if r["file"] != row["file"]]
    save(rows)
    print(f"garage: registered {clip.name} ({row['duration_s']}s, {row['stream']}) — {len(rows)} clips total")
    return row


def build(rows=None):
    rows = load() if rows is None else rows
    GARAGE.mkdir(exist_ok=True)
    cards = ""
    for r in rows:
        stars = "★" * int(r.get("stars", 0)) + "☆" * (5 - int(r.get("stars", 0)))
        vir = r.get("virality")
        vir_line = ""
        if isinstance(vir, (int, float)):
            why = (r.get("virality_why") or "").strip()
            vir_line = f"<br>🔥 virality {vir}/100" + (f" — {why}" if why else "")
        edit_line = ""
        if r.get("edited_from"):
            edit_line = (f"<br>✂ edit of {Path(r['edited_from']).name} "
                         f"({r.get('trim_start')}–{r.get('trim_end')}s)")
        cards += (
            f'<div class="card"><video src="file://{r["file"]}" controls preload="metadata"></video>'
            f'<h3>{r["moment"] or r["source_title"] or "Clip"}</h3>'
            f'<p>{stars} · {r["duration_s"]}s · {r["status"]} · safety {r.get("safety", "?")} · copyright {r.get("copyright", "?")} · {r.get("layout", "")}{vir_line}{edit_line}<br>'
            f'{r["stream"]} · {r["platform"]}<br>'
            f'<code>{r["file"]}</code></p></div>\n')
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>GTA 6 Video Garage</title>
<style>body{{font-family:system-ui;background:#111;color:#eee;margin:20px}}
h1{{color:#ff5fa2}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}}
.card{{background:#1c1c1c;border-radius:12px;padding:12px}}video{{width:100%;border-radius:8px}}
code{{font-size:11px;word-break:break-all;color:#aaa}}</style></head><body>
<h1>🎮 GTA 6 Video Garage — {len(rows)} clips</h1>
<p>Sorted newest first. Statuses: new / approved / exported. Files are local — open this page from the VM.</p>
<div class="grid">{cards or '<p>No clips yet.</p>'}</div></body></html>"""
    INDEX_HTML.write_text(html, encoding="utf-8")
    print(f"garage: {INDEX_HTML} ({len(rows)} clips)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("register"); p.add_argument("clip"); p.add_argument("--moment", default=""); p.add_argument("--stars", type=int, default=0)
    p = sub.add_parser("approve"); p.add_argument("clip")
    p = sub.add_parser("export"); p.add_argument("clip")
    p = sub.add_parser("delete"); p.add_argument("clip")
    sub.add_parser("list"); sub.add_parser("build")
    a = ap.parse_args()
    rows = load()
    if a.cmd == "register":
        register(a.clip, a.moment, a.stars)
    elif a.cmd in ("approve", "export"):
        for r in rows:
            if r["file"] == str(Path(a.clip).resolve()):
                if a.cmd == "approve" and r.get("safety") == "BLOCK":
                    print(f"garage: REFUSED — {a.clip} is safety BLOCKED "
                          f"(never approve/post).")
                    return
                if a.cmd == "approve" and r.get("copyright") == "BLOCK":
                    print(f"garage: REFUSED — {a.clip} is copyright BLOCKED "
                          f"(never approve/post).")
                    return
                r["status"] = "approved" if a.cmd == "approve" else "exported"
        save(rows); print(f"garage: {a.cmd}d {a.clip}")
    elif a.cmd == "delete":
        for r in rows:
            if r["file"] == str(Path(a.clip).resolve()):
                for f in (r["file"], r.get("file_16x9", "")):
                    if f and Path(f).exists():
                        Path(f).unlink()
        save([r for r in rows if r["file"] != str(Path(a.clip).resolve())])
        print(f"garage: deleted {a.clip}")
    elif a.cmd == "list":
        for r in rows:
            print(f"{r['status']:9} {r['duration_s']:>5}s  {'★'*int(r.get('stars',0))}  {r['stream']} — {r['moment']} — {r['file']}")
    elif a.cmd == "build":
        build(rows)


if __name__ == "__main__":
    main()
