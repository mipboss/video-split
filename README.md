# Video Splitter

**[⬇ Download VideoSplitter.exe for Windows](https://github.com/mipboss/video-split/releases/latest/download/VideoSplitter.exe)**
(no installation needed)

Splits videos into clips ready to share on WhatsApp and YouTube. You can add
single videos or whole folders, and it works through them in one go.

- **YouTube clips** have a fixed length (15 minutes by default) and keep the
  original quality.
- **WhatsApp clips** are re-encoded to a small MP4 (720p by default) and are
  as long as WhatsApp's file size limit allows.

## Download (Windows)

Download `VideoSplitter.exe` from the latest release on the
[Releases](../../releases) page and double-click it. Nothing else needs to be
installed: Python and ffmpeg are built in.

The first time, Windows may say "Windows protected your PC" because the app
isn't signed. Click **More info → Run anyway**.

## Using the window

1. **Add videos…** or **Add folder…** (folders are searched inside too).
2. Choose where to **Save clips to**.
3. Pick what to make clips for, and set the options (see below).
4. Click **Start**. When it's done, click **Open clips folder**.

Videos that are already done are skipped next time, so you can stop at any
point and carry on later.

## WhatsApp clip size

WhatsApp limits how big a video can be, and the limit depends on how you send
it. Choose the matching option under **WhatsApp max file size**:

| Option | How you send it in WhatsApp | What the recipient gets |
|---|---|---|
| **2 GB – send as Document** (default) | 📎 → **Document** | The file exactly as made. They download it, then play it. |
| **64 MB – send as video** | 📎 → **Gallery** / Photos & videos | Plays straight away in the chat, but WhatsApp recompresses it. |

Each video is split into **as few clips as fit under the limit, all about the
same size**, so there's never a tiny last piece. A video that already fits
stays in one piece.

As a rough guide at the default 720p:

- **2 GB:** about 2–4 hours per clip, so most videos aren't split at all.
- **64 MB:** about 4–8 minutes per clip.

Busy, fast-moving footage takes more space, so its clips are shorter. Lowering
**WhatsApp max height** to 480 gives longer clips; 1080 gives shorter ones.

While a video is being processed, you need free disk space for about twice
the size of its WhatsApp version.

## Command line

Run `video_splitter.py` with no arguments to open the window, or pass options:

```
python video_splitter.py "D:\Videos" -o "D:\Clips"
python video_splitter.py "D:\Videos" -o "D:\Clips" --profile whatsapp --wa-limit-mb 64
python video_splitter.py "D:\Videos" -o "D:\Clips" --profile youtube --minutes 10
```

| Option | What it does |
|---|---|
| `--profile both\|youtube\|whatsapp` | Which clips to make (default `both`) |
| `--wa-limit-mb N` | WhatsApp max file size per clip, in MB. `2000` = 2 GB (default), `64` for sending as a normal video |
| `--wa-height N` | WhatsApp max height: `480`, `720` (default) or `1080`. Never upscales |
| `--wa-crf N` | WhatsApp quality, 18 (best, biggest) to 32 (smallest). Default 26 |
| `--minutes N` | YouTube clip length in minutes (default 15) |
| `--dry-run` | Show the plan without converting. The WhatsApp clip count is only an estimate, because the real size is known after encoding |

Run `python video_splitter.py --help` for all options, including YouTube
upload.

Running from source needs Python 3.8+ and ffmpeg (`winget install
Gyan.FFmpeg` on Windows, `brew install ffmpeg` on Mac). Uploading to YouTube
needs a one-time setup, described at the top of `video_splitter.py`.
