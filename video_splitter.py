#!/usr/bin/env python3
"""
video_splitter.py — Batch-split videos into fixed-length clips (default 15 min)
ready for sharing on WhatsApp and YouTube.

Requires: Python 3.8+ and ffmpeg/ffprobe on your PATH.
  Mac:     brew install ffmpeg
  Windows: winget install Gyan.FFmpeg   (then reopen the terminal)
  Linux:   sudo apt install ffmpeg

Run with no arguments to open a simple window instead of using the command line.

Examples
  # Split everything under two folders, output both YouTube and WhatsApp versions
  python video_splitter.py "D:/Videos/2024" "D:/Videos/Family" -o "D:/Clips"

  # Only WhatsApp-friendly clips, 10-minute length, 2 files at a time
  python video_splitter.py ~/Movies -o ~/Clips --profile whatsapp --minutes 10 --jobs 2

  # See what would happen without doing anything
  python video_splitter.py ~/Movies -o ~/Clips --dry-run

  # Split for YouTube and upload as unlisted, each video's parts in a playlist
  python video_splitter.py ~/Movies -o ~/Clips --profile youtube --upload --privacy unlisted --playlist

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

YouTube upload (optional, --upload or the checkbox in the window)
  Uploads the YouTube clips to your channel, titled "<video> - Part 1 of 3".
  Uploaded clips are remembered, so re-running only uploads what's missing.
  One-time setup:
    1. pip install google-api-python-client google-auth-oauthlib
    2. In https://console.cloud.google.com create a project, then:
       - APIs & Services > Library: enable "YouTube Data API v3"
       - OAuth consent screen: choose External, and add your Google account
         as a test user
       - Credentials > Create credentials > OAuth client ID > "Desktop app",
         then download the JSON file
    3. Save that file as ~/.video_splitter/client_secret.json (or next to this
       script, or pass --yt-secrets). The window asks for it if it's missing.
    4. The first upload opens your browser so you can sign in and allow access.
  Good to know:
    - YouTube locks videos uploaded through a new, unreviewed Google project to
      Private. You can change them to Public in YouTube Studio, or apply for
      YouTube's API audit to lift the limit.
    - The free API quota allows only a few uploads per day. When it runs out,
      run the script again the next day and it continues where it stopped.
    - While the project is in "Testing" mode, Google asks you to sign in again
      about once a week.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".mts", ".m2ts", ".webm", ".3gp"}
YT_COPY_VCODECS = {"h264", "hevc"}
YT_COPY_ACODECS = {"aac", "mp3"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # stop console pop-ups on Windows

_cancel = threading.Event()  # set by the GUI's Stop button
_procs = set()               # running ffmpeg processes, so Stop can kill them


# ---------------------------------------------------------------- helpers
def app_dir():
    """Folder holding this script (or the bundled files of a packaged exe)."""
    return Path(getattr(sys, "_MEIPASS", Path(sys.argv[0]).resolve().parent))


def tool(name):
    """Use ffmpeg/ffprobe bundled next to the script or exe if present, else the one on PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = app_dir() / exe
    return str(bundled) if bundled.exists() else name


def missing_tools():
    return [t for t in ("ffmpeg", "ffprobe") if shutil.which(tool(t)) is None]


def run(cmd):
    """Like subprocess.run(capture_output=True), but killable via stop()."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, encoding="utf-8", errors="replace",
                         creationflags=NO_WINDOW)
    _procs.add(p)
    try:
        out, err = p.communicate()
    finally:
        _procs.discard(p)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def stop():
    _cancel.set()
    for p in list(_procs):
        p.kill()


def probe(path: Path) -> dict:
    """Return duration (s), video codec, audio codec, height."""
    cmd = [tool("ffprobe"), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    out = run(cmd)
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


def find_videos(inputs, warn=print):
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
            warn(f"WARNING: skipping {inp} (not found or not a video)")
            continue
        for f, root in files:
            if f not in seen:
                seen.add(f)
                yield f, root


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- youtube upload
CONFIG_DIR = Path.home() / ".video_splitter"
YT_TOKEN = CONFIG_DIR / "youtube_token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube"]  # upload videos + manage playlists
YT_PACKAGES = "google-api-python-client google-auth-oauthlib"
YT_QUOTA_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"}
YT_RETRY_STATUS = {500, 502, 503, 504}


class Stopped(Exception):
    pass


_yt = {"service": None, "blocked": None}
_yt_lock = threading.Lock()  # one upload at a time; the API client isn't thread-safe


def yt_packages_missing():
    import importlib.util
    return any(importlib.util.find_spec(m) is None for m in ("googleapiclient", "google_auth_oauthlib"))


def yt_install_hint():
    py = f'"{sys.executable}" -m pip install'
    return f"{py} --upgrade pip && {py} {YT_PACKAGES}"  # old pips pick incompatible versions


def find_client_secrets(explicit=None):
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    return next((p for p in (CONFIG_DIR / "client_secret.json", app_dir() / "client_secret.json")
                 if p.is_file()), None)


def youtube_login(secrets):
    """Return a YouTube API client. Opens the browser to sign in when there's no saved sign-in."""
    if yt_packages_missing():
        raise RuntimeError("YouTube upload needs extra Python packages. Install them with:\n  "
                           + yt_install_hint())
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if YT_TOKEN.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(YT_TOKEN), YT_SCOPES)
        except ValueError:
            creds = None
    if creds and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:  # revoked, or expired (Testing-mode projects expire after 7 days)
            creds = None
    if not creds or not creds.valid:
        if secrets is None:
            raise RuntimeError("No YouTube client_secret.json found. See 'YouTube upload' at the "
                               "top of video_splitter.py for the one-time setup.")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), YT_SCOPES)
            creds = flow.run_local_server(port=0, timeout_seconds=300)
        except Exception as e:
            raise RuntimeError(f"YouTube sign-in did not finish: {e}")
        if not creds or not creds.valid:
            raise RuntimeError("YouTube sign-in did not finish (timed out after 5 minutes).")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    YT_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    YT_TOKEN.chmod(0o600)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _error_info(e):
    try:
        err = json.loads(e.content.decode("utf-8", "replace"))["error"]
        return (err.get("errors") or [{}])[0].get("reason", ""), err.get("message", "")
    except Exception:
        return "", ""


def _api_call(fn):
    """Run one YouTube request, retrying network hiccups and YouTube server errors."""
    import httplib2
    from googleapiclient.errors import HttpError
    for attempt in range(8):
        try:
            return fn()
        except HttpError as e:
            reason, message = _error_info(e)
            if reason in YT_QUOTA_REASONS:
                _yt["blocked"] = ("YouTube's daily upload quota is used up. Run again tomorrow to "
                                  "upload the rest; clips already uploaded are skipped.")
                raise RuntimeError(_yt["blocked"])
            if e.resp.status not in YT_RETRY_STATUS or attempt == 7:
                raise RuntimeError(f"YouTube error {e.resp.status} {reason}: {message}".strip())
        except (OSError, httplib2.HttpLib2Error):
            if attempt == 7:
                raise
        if _cancel.wait(min(2 ** attempt, 60)):
            raise Stopped()


def upload_clips(dest_dir, name, args, emit, activity):
    """Upload clips in dest_dir that aren't on YouTube yet; progress is kept in .uploaded.json."""
    if _yt["blocked"]:
        raise RuntimeError(_yt["blocked"])
    from googleapiclient.http import MediaFileUpload

    yt = _yt["service"]
    clips = sorted(dest_dir.glob(f"{name}_part*.mp4"))
    if not clips:
        raise RuntimeError("no clips found to upload")
    state_file = dest_dir / ".uploaded.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    ids = state.setdefault("videos", {})
    in_playlist = state.setdefault("in_playlist", [])

    def save():
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    title = name.replace("<", "").replace(">", "").strip() or "Video"  # YouTube rejects < >
    n, new = len(clips), 0
    for i, clip in enumerate(clips, 1):
        if clip.name in ids:
            continue
        if _cancel.is_set():
            raise Stopped()
        suffix = f" - Part {i} of {n}" if n > 1 else ""
        body = {"snippet": {"title": title[:100 - len(suffix)] + suffix, "categoryId": "22"},
                "status": {"privacyStatus": args.privacy}}
        media = MediaFileUpload(str(clip), mimetype="video/mp4", chunksize=8 * 1024 * 1024,
                                resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            if _cancel.is_set():
                raise Stopped()
            status, resp = _api_call(req.next_chunk)
            if status and activity:
                activity(f"Uploading {clip.name}: {int(status.progress() * 100)}%")
        ids[clip.name] = resp["id"]
        save()
        new += 1
        emit(f"    uploaded {clip.name} -> https://youtu.be/{resp['id']}")

    msg = f"{new} clip(s) uploaded as {args.privacy}"
    if n - new:
        msg += f", {n - new} already on YouTube"
    if args.playlist and n > 1:
        if not state.get("playlist"):
            resp = _api_call(yt.playlists().insert(part="snippet,status", body={
                "snippet": {"title": title[:150]},
                "status": {"privacyStatus": args.privacy}}).execute)
            state["playlist"] = resp["id"]
            save()
        for clip in clips:
            vid = ids[clip.name]
            if vid not in in_playlist:
                _api_call(yt.playlistItems().insert(part="snippet", body={"snippet": {
                    "playlistId": state["playlist"],
                    "resourceId": {"kind": "youtube#video", "videoId": vid}}}).execute)
                in_playlist.append(vid)
                save()
        msg += f", playlist https://www.youtube.com/playlist?list={state['playlist']}"
    return msg


# ---------------------------------------------------------------- core
def build_cmd(src, info, out_pattern, profile, seg_sec, exact, crf_wa, crf_yt, wa_height):
    base = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
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


def process(src, root, out_dir, profiles, args, emit=print, activity=None):
    rel = src.relative_to(root)
    label = str(rel)
    if _cancel.is_set():
        return label, False, "stopped"
    try:
        info = probe(src)
    except Exception as e:
        return label, False, f"cannot read: {e}"

    n_parts = max(1, -(-int(info["duration"]) // args.seg_sec))
    results = []
    yt_dir = None
    for profile in profiles:
        sub = out_dir / profile if len(profiles) > 1 else out_dir
        dest_dir = sub / rel.parent / src.stem
        if profile == "youtube":
            yt_dir = dest_dir
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

        if _cancel.is_set():
            return label, False, "stopped"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for old in dest_dir.glob(f"{src.stem}_part*.mp4"):
            old.unlink()
        t0 = time.time()
        r = run(cmd)
        if _cancel.is_set():
            return label, False, f"{profile}: stopped (partial clips left, will redo next time)"
        if r.returncode != 0:
            return label, False, f"{profile} failed: {r.stderr.strip()[-400:]}"

        clips = sorted(dest_dir.glob(f"{src.stem}_part*.mp4"))
        big = [c for c in clips if c.stat().st_size > 2 * 1024**3]
        done_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        msg = f"{profile}: {len(clips)} clip(s) in {time.time() - t0:.0f}s [{mode}]"
        if profile == "whatsapp" and big:
            msg += f"  WARNING: {len(big)} clip(s) over WhatsApp's 2 GB limit — try --wa-crf 30"
        results.append(msg)

    if args.upload and yt_dir:
        if args.dry_run:
            results.append(f"upload: would upload ~{n_parts} clip(s) to YouTube as {args.privacy}")
        else:
            prefix = f"({fmt_time(info['duration'])}) " + " | ".join(results)
            try:
                with _yt_lock:
                    results.append("upload: " + upload_clips(yt_dir, src.stem, args, emit, activity))
            except Stopped:
                return label, False, prefix + " | upload: stopped (will continue next time)"
            except Exception as e:
                return label, False, prefix + f" | upload failed: {e}"

    return label, True, f"({fmt_time(info['duration'])}) " + " | ".join(results)


# ---------------------------------------------------------------- batch
def parse_args(argv=None):
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
    yt = ap.add_argument_group("YouTube upload (one-time setup described below)")
    yt.add_argument("--upload", action="store_true", help="Upload the YouTube clips to your channel")
    yt.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private",
                    help="Visibility of uploaded clips (default private)")
    yt.add_argument("--playlist", action="store_true",
                    help="Put each video's parts into their own YouTube playlist")
    yt.add_argument("--yt-secrets", metavar="FILE",
                    help="OAuth client JSON from Google Cloud (default: "
                         "~/.video_splitter/client_secret.json or next to this script)")
    args = ap.parse_args(argv)
    if args.upload and args.profile == "whatsapp":
        ap.error("--upload uploads the YouTube clips, so use --profile youtube or both")
    args.seg_sec = int(args.minutes * 60)
    return args


def run_batch(args, emit=print, progress=None, activity=None):
    """Split every video in args.inputs. Returns (ok, fail), or None if no videos were found.
    Raises RuntimeError if YouTube upload was asked for but sign-in fails."""
    out_dir = Path(args.output).expanduser().resolve()
    profiles = ["youtube", "whatsapp"] if args.profile == "both" else [args.profile]

    videos = [(f, r) for f, r in find_videos(args.inputs, emit) if out_dir not in f.parents]
    if not videos:
        return None
    emit(f"Found {len(videos)} video(s). Output: {out_dir}  Profile: {args.profile}  "
         f"Clip length: {args.minutes:g} min\n")
    if args.upload and not args.dry_run:
        emit("Signing in to YouTube (your browser opens the first time)…")
        _yt["blocked"] = None
        _yt["service"] = youtube_login(find_client_secrets(args.yt_secrets))
        emit(f"Signed in. Clips will be uploaded as {args.privacy}.\n")

    ok = fail = 0
    log_lines = []
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = {pool.submit(process, f, r, out_dir, profiles, args, emit, activity): f
                for f, r in videos}
        for i, fut in enumerate(as_completed(futs), 1):
            label, success, msg = fut.result()
            status = "OK  " if success else "FAIL"
            line = f"[{i}/{len(videos)}] {status} {label} {msg}"
            emit(line)
            log_lines.append(line)
            ok += success
            fail += not success
            if progress:
                progress(i, len(videos))

    summary = f"\nDone in {fmt_time(time.time() - t_start)}: {ok} succeeded, {fail} failed."
    emit(summary)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        log = out_dir / f"split_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        log.write_text("\n".join(log_lines) + summary + "\n", encoding="utf-8")
        emit(f"Log saved to {log}")
    return ok, fail


# ---------------------------------------------------------------- gui
def open_folder(path):
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def gui():
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Video Splitter")
    root.minsize(640, 600)
    events = queue.Queue()  # worker thread -> GUI; Tk widgets may only be touched from here
    running = [False]

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(0, weight=1)
    frm.rowconfigure(6, weight=1)

    # --- what to split
    ttk.Label(frm, text="Videos and folders to split (folders are searched inside too)").grid(
        row=0, column=0, columnspan=2, sticky="w")
    lst_box = ttk.Frame(frm)
    lst_box.grid(row=1, column=0, sticky="nsew")
    lst_box.columnconfigure(0, weight=1)
    lst = tk.Listbox(lst_box, height=6, selectmode="extended")
    lst.grid(row=0, column=0, sticky="nsew")
    lst_scroll = ttk.Scrollbar(lst_box, orient="vertical", command=lst.yview)
    lst_scroll.grid(row=0, column=1, sticky="ns")
    lst.configure(yscrollcommand=lst_scroll.set)

    def add(path):
        if path and path not in lst.get(0, "end"):
            lst.insert("end", path)

    def add_files():
        patterns = " ".join("*" + e for e in sorted(VIDEO_EXTS))
        for f in filedialog.askopenfilenames(
                title="Choose videos", filetypes=[("Videos", patterns), ("All files", "*")]):
            add(f)

    def add_folder():
        add(filedialog.askdirectory(title="Choose a folder of videos"))

    def remove():
        for i in reversed(lst.curselection()):
            lst.delete(i)

    in_btns = ttk.Frame(frm)
    in_btns.grid(row=1, column=1, sticky="n", padx=(8, 0))
    for text, cmd in (("Add videos…", add_files), ("Add folder…", add_folder),
                      ("Remove", remove), ("Clear", lambda: lst.delete(0, "end"))):
        ttk.Button(in_btns, text=text, command=cmd).pack(fill="x", pady=(0, 4))

    # --- where to put clips
    ttk.Label(frm, text="Save clips to").grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
    out_var = tk.StringVar()
    ttk.Entry(frm, textvariable=out_var).grid(row=3, column=0, sticky="ew")

    def browse_out():
        d = filedialog.askdirectory(title="Choose where to save the clips")
        if d:
            out_var.set(d)

    ttk.Button(frm, text="Browse…", command=browse_out).grid(row=3, column=1, sticky="ew", padx=(8, 0))

    # --- options
    opts = ttk.LabelFrame(frm, text="Options", padding=8)
    opts.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
    prof_var = tk.StringVar(value="both")
    ttk.Label(opts, text="Make clips for:").grid(row=0, column=0, sticky="w")
    for col, (text, val) in enumerate((("YouTube + WhatsApp", "both"), ("YouTube only", "youtube"),
                                       ("WhatsApp only", "whatsapp")), 1):
        ttk.Radiobutton(opts, text=text, value=val, variable=prof_var).grid(
            row=0, column=col, sticky="w", padx=(8, 0))
    min_var = tk.StringVar(value="15")
    ttk.Label(opts, text="Clip length (minutes):").grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Spinbox(opts, from_=1, to=240, width=6, textvariable=min_var).grid(
        row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
    h_var = tk.StringVar(value="720")
    ttk.Label(opts, text="WhatsApp max height:").grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Combobox(opts, values=["480", "720", "1080"], width=6, state="readonly",
                 textvariable=h_var).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
    up_var = tk.BooleanVar(value=False)
    up_chk = ttk.Checkbutton(opts, text="Upload YouTube clips to my channel", variable=up_var)
    up_chk.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
    priv_var = tk.StringVar(value="private")
    priv_box = ttk.Combobox(opts, values=["private", "unlisted", "public"], width=9,
                            state="readonly", textvariable=priv_var)
    priv_box.grid(row=3, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
    pl_var = tk.BooleanVar(value=True)
    pl_chk = ttk.Checkbutton(opts, text="Parts in a playlist", variable=pl_var)
    pl_chk.grid(row=3, column=3, sticky="w", padx=(8, 0), pady=(8, 0))

    def sync_upload(*_):
        can = prof_var.get() != "whatsapp"
        up_chk.configure(state="normal" if can else "disabled")
        on = can and up_var.get()
        priv_box.configure(state="readonly" if on else "disabled")
        pl_chk.configure(state="normal" if on else "disabled")

    prof_var.trace_add("write", sync_upload)
    up_var.trace_add("write", sync_upload)
    sync_upload()

    # --- run controls
    ctl = ttk.Frame(frm)
    ctl.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 4))
    ctl.columnconfigure(1, weight=1)
    go_btn = ttk.Button(ctl, text="Start")
    go_btn.grid(row=0, column=0)
    bar = ttk.Progressbar(ctl, mode="indeterminate")
    bar.grid(row=0, column=1, sticky="ew", padx=8)
    status = ttk.Label(ctl, text="Ready")
    status.grid(row=0, column=2)
    open_btn = ttk.Button(ctl, text="Open clips folder", state="disabled",
                          command=lambda: open_folder(Path(out_var.get()).expanduser()))
    open_btn.grid(row=0, column=3, padx=(8, 0))

    # --- log
    log_box = ttk.Frame(frm)
    log_box.grid(row=6, column=0, columnspan=2, sticky="nsew")
    log_box.columnconfigure(0, weight=1)
    log_box.rowconfigure(0, weight=1)
    log = tk.Text(log_box, height=10, wrap="word", state="disabled")
    log.grid(row=0, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=log.yview)
    log_scroll.grid(row=0, column=1, sticky="ns")
    log.configure(yscrollcommand=log_scroll.set)

    def write(text):
        log.configure(state="normal")
        log.insert("end", text + "\n")
        log.see("end")
        log.configure(state="disabled")

    def set_running(on):
        running[0] = on
        go_btn.configure(text="Stop" if on else "Start")
        if on:
            bar.start(15)
        else:
            bar.stop()

    def start():
        if running[0]:
            if messagebox.askyesno("Stop", "Stop splitting? Unfinished videos will be redone next time."):
                status.configure(text="Stopping…")
                stop()
            return
        inputs = list(lst.get(0, "end"))
        out = out_var.get().strip()
        if not inputs:
            messagebox.showwarning("Nothing to split", "Add some videos or a folder first.")
            return
        if not out:
            messagebox.showwarning("No destination", "Choose where to save the clips.")
            return
        try:
            minutes = float(min_var.get())
            if minutes <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Clip length", "Clip length must be a number of minutes above 0.")
            return
        missing = missing_tools()
        if missing:
            messagebox.showerror("ffmpeg missing", f"{', '.join(missing)} not found. Install ffmpeg, "
                                 "or put ffmpeg and ffprobe next to this program.")
            return

        extra = []
        if up_var.get() and prof_var.get() != "whatsapp":
            if yt_packages_missing():
                messagebox.showerror("YouTube upload", "Uploading needs extra Python packages. "
                                     "Run this in Terminal, then try again:\n\n" + yt_install_hint())
                return
            secrets = find_client_secrets()
            if secrets is None and not YT_TOKEN.exists():
                messagebox.showinfo("YouTube upload", "Choose the client_secret JSON file you "
                                    "downloaded from Google Cloud (see the setup steps at the top "
                                    "of video_splitter.py).")
                chosen = filedialog.askopenfilename(title="Choose client_secret JSON",
                                                    filetypes=[("JSON", "*.json")])
                if not chosen:
                    return
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                secrets = CONFIG_DIR / "client_secret.json"
                shutil.copyfile(chosen, secrets)
            extra = ["--upload", "--privacy", priv_var.get()] + (["--playlist"] if pl_var.get() else [])
            if secrets:
                extra += ["--yt-secrets", str(secrets)]

        args = parse_args(["-o", out, "--profile", prof_var.get(), "--minutes", str(minutes),
                           "--wa-height", h_var.get()] + extra + ["--"] + inputs)
        _cancel.clear()
        set_running(True)
        open_btn.configure(state="disabled")
        status.configure(text="Working…")
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")

        def work():
            try:
                result = run_batch(args, emit=lambda s: events.put(("log", s)),
                                   progress=lambda i, n: events.put(("status", f"{i} of {n} videos done")),
                                   activity=lambda s: events.put(("status", s)))
            except Exception as e:
                events.put(("log", f"ERROR: {e}"))
                result = (0, 1)
            events.put(("done", result))

        threading.Thread(target=work, daemon=True).start()

    go_btn.configure(command=start)

    def finish(result):
        set_running(False)
        if result is None:
            status.configure(text="Ready")
            messagebox.showwarning("No videos", "No video files were found in what you added.")
            return
        ok, fail = result
        open_btn.configure(state="normal")
        if _cancel.is_set():
            status.configure(text="Stopped")
        elif fail:
            status.configure(text=f"Done, {fail} failed")
            messagebox.showwarning("Finished with errors", f"{ok} succeeded, {fail} failed. See the log.")
        else:
            status.configure(text="Done")
            messagebox.showinfo("Finished", f"All {ok} video(s) split.")

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "log":
                    write(value)
                elif kind == "status":
                    status.configure(text=value)
                else:
                    finish(value)
        except queue.Empty:
            pass
        root.after(100, poll)

    def on_close():
        if running[0]:
            if not messagebox.askyesno("Quit", "Splitting is still running. Stop it and quit?"):
                return
            stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    poll()
    root.mainloop()


# ---------------------------------------------------------------- main
def main():
    if len(sys.argv) == 1:
        gui()
        return
    args = parse_args()
    missing = missing_tools()
    if missing:
        sys.exit(f"ERROR: {', '.join(missing)} not found on PATH. Install ffmpeg first "
                 "(see the top of this script for instructions).")
    try:
        result = run_batch(args, emit=lambda s: print(s, flush=True))
    except RuntimeError as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        stop()
        sys.exit("\nStopped.")
    if result is None:
        sys.exit("No video files found.")
    sys.exit(1 if result[1] else 0)


if __name__ == "__main__":
    main()
