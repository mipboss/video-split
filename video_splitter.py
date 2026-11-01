#!/usr/bin/env python3
"""
video_splitter.py — Batch-split videos into fixed-length clips (default 15 min)
ready for sharing on WhatsApp and YouTube.

Requires: Python 3.8+ and ffmpeg/ffprobe on your PATH.
  Mac:     brew install ffmpeg
  Windows: winget install Gyan.FFmpeg   (then reopen the terminal)
  Linux:   sudo apt install ffmpeg

Examples
  # Split everything under two folders, output both YouTube and WhatsApp versions
  python video_splitter.py "D:/Videos/2024" "D:/Videos/Family" -o "D:/Clips"

  # Only WhatsApp-friendly clips, 10-minute length, 2 files at a time
  python video_splitter.py ~/Movies -o ~/Clips --profile whatsapp --minutes 10 --jobs 2

  # See what would happen without doing anything
  python video_splitter.py ~/Movies -o ~/Clips --dry-run

Profiles
  youtube   Keeps original quality. Streams are copied (no re-encode) when the
            source is already H.264/H.265 + AAC, which is very fast. Cuts land on
            the nearest keyframe, so clips may be a few seconds off 15:00.
            Use --exact to re-encode for frame-accurate 15:00 cuts.
  whatsapp  Re-encodes to H.264/AAC MP4, max 720p, "fast start" so it plays
            while downloading. Much smaller files that WhatsApp accepts and
            plays on any phone. Clips are cut exactly at 15:00.
            Use --wa-height 1080 to keep full HD (bigger files).
  both      (default) produces both, in separate sub-folders.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".mts", ".m2ts", ".webm", ".3gp"}
YT_COPY_VCODECS = {"h264", "hevc"}
YT_COPY_ACODECS = {"aac", "mp3"}


# ---------------------------------------------------------------- helpers
def check_ffmpeg():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        sys.exit(f"ERROR: {', '.join(missing)} not found on PATH. Install ffmpeg first "
                 "(see the top of this script for instructions).")


def probe(path: Path) -> dict:
    """Return duration (s), video codec, audio codec, height."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    data = json.loads(out.stdout)
    v = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RuntimeError("no video stream")
    return {
        "duration": float(data["format"].get("duration", 0) or 0),
        "vcodec": v.get("codec_name", ""),
        "acodec": a.get("codec_name", "") if a else "",
        "has_audio": a is not None,
        "height": int(v.get("height", 0) or 0),
    }


def find_videos(inputs):
    seen = set()
    for inp in inputs:
        p = Path(inp).expanduser().resolve()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            files = [(p, p.parent)]
        elif p.is_dir():
            files = [(f, p) for f in sorted(p.rglob("*"))
                     if f.is_file() and f.suffix.lower() in VIDEO_EXTS
                     and not f.name.startswith("._")]
        else:
            print(f"WARNING: skipping {inp} (not found or not a video)")
            continue
        for f, root in files:
            if f not in seen:
                seen.add(f)
                yield f, root


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- core
def build_cmd(src, info, out_pattern, profile, seg_sec, exact, crf_wa, crf_yt, wa_height):
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(src), "-map", "0:v:0"]
    if info["has_audio"]:
        base += ["-map", "0:a:0"]

    if profile == "youtube":
        can_copy = (info["vcodec"] in YT_COPY_VCODECS and
                    (not info["has_audio"] or info["acodec"] in YT_COPY_ACODECS))
        if can_copy and not exact:
            codec = ["-c", "copy"]
            mode = "copy (no quality loss)"
        else:
            codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf_yt),
                     "-pix_fmt", "yuv420p",
                     "-force_key_frames", f"expr:gte(t,n_forced*{seg_sec})",
                     "-c:a", "aac", "-b:a", "192k"]
            mode = f"re-encode H.264 CRF {crf_yt}"
    else:  # whatsapp
        scale = f"scale=-2:'min({wa_height},ih)'"
        level = "4.0" if wa_height <= 720 else "4.2"  # 4.2 covers 1080p at 60 fps
        codec = ["-vf", scale, "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", str(crf_wa), "-profile:v", "high", "-level", level,
                 "-pix_fmt", "yuv420p",
                 "-force_key_frames", f"expr:gte(t,n_forced*{seg_sec})",
                 "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
        mode = f"re-encode {min(wa_height, info['height'] or wa_height)}p H.264 CRF {crf_wa}"

    seg = ["-f", "segment", "-segment_time", str(seg_sec),
           "-segment_time_delta", "0.05", "-reset_timestamps", "1", "-segment_format", "mp4",
           "-segment_format_options", "movflags=+faststart",
           "-segment_start_number", "1", str(out_pattern)]
    return base + codec + seg, mode


def process(src, root, out_dir, profiles, args):
    rel = src.relative_to(root)
    label = str(rel)
    try:
        info = probe(src)
    except Exception as e:
        return label, False, f"cannot read: {e}"

    n_parts = max(1, -(-int(info["duration"]) // args.seg_sec))
    results = []
    for profile in profiles:
        sub = out_dir / profile if len(profiles) > 1 else out_dir
        dest_dir = sub / rel.parent / src.stem
        done_marker = dest_dir / ".done"
        if done_marker.exists() and not args.overwrite:
            results.append(f"{profile}: already done, skipped")
            continue

        pattern = dest_dir / f"{src.stem}_part%02d.mp4"
        cmd, mode = build_cmd(src, info, pattern, profile, args.seg_sec,
                              args.exact, args.wa_crf, args.yt_crf, args.wa_height)
        if args.dry_run:
            results.append(f"{profile}: would create ~{n_parts} clip(s) in {dest_dir} [{mode}]")
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        for old in dest_dir.glob(f"{src.stem}_part*.mp4"):
            old.unlink()
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return label, False, f"{profile} failed: {r.stderr.strip()[-400:]}"

        clips = sorted(dest_dir.glob(f"{src.stem}_part*.mp4"))
        big = [c for c in clips if c.stat().st_size > 2 * 1024**3]
        done_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        msg = f"{profile}: {len(clips)} clip(s) in {time.time() - t0:.0f}s [{mode}]"
        if profile == "whatsapp" and big:
            msg += f"  WARNING: {len(big)} clip(s) over WhatsApp's 2 GB limit — try --wa-crf 30"
        results.append(msg)

    return label, True, f"({fmt_time(info['duration'])}) " + " | ".join(results)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Batch-split videos into 15-minute clips for WhatsApp and YouTube.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Examples")[1])
    ap.add_argument("inputs", nargs="+", help="Video files and/or folders (searched recursively)")
    ap.add_argument("-o", "--output", required=True, help="Folder to write clips into")
    ap.add_argument("--profile", choices=["both", "youtube", "whatsapp"], default="both")
    ap.add_argument("--minutes", type=float, default=15, help="Clip length in minutes (default 15)")
    ap.add_argument("--jobs", type=int, default=1,
                    help="Videos to process at once (default 1; re-encoding already uses all CPU cores)")
    ap.add_argument("--exact", action="store_true",
                    help="YouTube profile: re-encode so cuts are exactly on the minute mark")
    ap.add_argument("--wa-crf", type=int, default=26,
                    help="WhatsApp quality, 18 (best/biggest) to 32 (smallest). Default 26")
    ap.add_argument("--wa-height", type=int, default=720,
                    help="WhatsApp max height in pixels, e.g. 480, 720, 1080. Never upscales. Default 720")
    ap.add_argument("--yt-crf", type=int, default=18,
                    help="YouTube quality when re-encoding, lower = better. Default 18")
    ap.add_argument("--overwrite", action="store_true", help="Redo videos already processed")
    ap.add_argument("--dry-run", action="store_true", help="Show the plan without converting")
    args = ap.parse_args()
    args.seg_sec = int(args.minutes * 60)

    check_ffmpeg()
    out_dir = Path(args.output).expanduser().resolve()
    profiles = ["youtube", "whatsapp"] if args.profile == "both" else [args.profile]

    videos = [(f, r) for f, r in find_videos(args.inputs) if out_dir not in f.parents]
    if not videos:
        sys.exit("No video files found.")
    print(f"Found {len(videos)} video(s). Output: {out_dir}  Profile: {args.profile}  "
          f"Clip length: {args.minutes:g} min\n")

    ok = fail = 0
    log_lines = []
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = {pool.submit(process, f, r, out_dir, profiles, args): f for f, r in videos}
        for i, fut in enumerate(as_completed(futs), 1):
            label, success, msg = fut.result()
            status = "OK  " if success else "FAIL"
            line = f"[{i}/{len(videos)}] {status} {label} {msg}"
            print(line, flush=True)
            log_lines.append(line)
            ok += success
            fail += not success

    summary = f"\nDone in {fmt_time(time.time() - t_start)}: {ok} succeeded, {fail} failed."
    print(summary)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        log = out_dir / f"split_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        log.write_text("\n".join(log_lines) + summary + "\n", encoding="utf-8")
        print(f"Log saved to {log}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
