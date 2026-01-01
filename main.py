import shutil
import subprocess
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

URL_RE = re.compile(r"https?://\S+")


def extract_urls(text: str | None) -> list[str]:
    return URL_RE.findall(text or "")


def parse_allowlist() -> set[int]:
    raw = os.getenv("ALLOWLIST_USER_IDS", "").strip()
    if not raw:
        return set()
    allowlist: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            allowlist.add(int(part))
        except ValueError:
            continue
    return allowlist


def is_twitter_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("twitter.com") or host.endswith("x.com")


def build_ydl_opts(output_dir: Path, twitter_api: str | None) -> dict:
    ydl_opts: dict = {
        "format": os.getenv("YTDLP_FORMAT", "bestvideo*+bestaudio/best"),
        "merge_output_format": "mp4",
        "remuxvideo": "mp4",
        "outtmpl": str(output_dir / "%(title).200B.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "extractor_retries": 3,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
    }

    if Path("/usr/local/bin/ffmpeg").exists():
        ydl_opts["ffmpeg_location"] = "/usr/local/bin"

    if Path("/usr/local/bin/deno").exists():
        ydl_opts["js_runtimes"] = {"deno": {"path": "/usr/local/bin/deno"}}

    if os.getenv("YTDLP_FIX_ASPECT_RATIO", "").lower() in {"1", "true", "yes"}:
        ydl_opts["postprocessor_args"] = [
            "-bsf:v",
            "h264_metadata=sample_aspect_ratio=1/1",
        ]

    cookies_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    if twitter_api:
        ydl_opts["extractor_args"] = {"twitter": {"api": [twitter_api]}}

    user_agent = os.getenv("YTDLP_USER_AGENT")
    if user_agent:
        ydl_opts["http_headers"] = {"User-Agent": user_agent}

    return ydl_opts


def normalize_download_path(filename: str) -> Path:
    path = Path(filename)
    if path.suffix.lower() != ".mp4" and path.exists():
        mp4_path = path.with_suffix(".mp4")
        if mp4_path.exists():
            return mp4_path
    return path


def transcode_to_hevc(path: Path, output_dir: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        raise RuntimeError("ffmpeg not found for HEVC post-processing")

    output_path = output_dir / f"{path.stem}.hevc.mp4"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(path),
        "-map",
        "0",
        "-c:v",
        "libx265",
        "-pix_fmt",
        "yuv420p",
        "-x265-params",
        "lossless=1:profile=main",
        "-tag:v",
        "hvc1",
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        "-movflags",
        "+faststart",
    ]

    if os.getenv("YTDLP_FIX_ASPECT_RATIO", "").lower() in {"1", "true", "yes"}:
        cmd.extend(["-vf", "setsar=1"])

    cmd.append(str(output_path))
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output_path


def download_with_yt_dlp(url: str, output_dir: Path) -> Path:
    twitter_api_order = os.getenv(
        "YTDLP_TWITTER_API_ORDER", "graphql,legacy,syndication"
    )
    api_candidates = [
        part.strip() for part in twitter_api_order.split(",") if part.strip()
    ]
    if not api_candidates:
        api_candidates = [
            os.getenv("YTDLP_TWITTER_API") or os.getenv("TWITTER_API") or "syndication"
        ]

    last_error: Exception | None = None
    attempts = api_candidates if is_twitter_url(url) else [None]

    for api in attempts:
        try:
            ydl_opts = build_ydl_opts(output_dir, api)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
            downloaded = normalize_download_path(filename)
            return transcode_to_hevc(downloaded, output_dir)
        except Exception as exc:
            last_error = exc
            if not is_twitter_url(url):
                break

    if last_error:
        raise last_error
    raise RuntimeError("Download failed")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "Send me a URL and I will download it with yt-dlp and return the video."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    allowlist = parse_allowlist()
    user_id = message.from_user.id if message.from_user else None
    if allowlist and (user_id is None or user_id not in allowlist):
        await message.reply_text("Access denied.")
        return

    urls = extract_urls(message.text)
    if not urls:
        await message.reply_text("No URL found. Send me a message with a link.")
        return

    for url in urls:
        status = await message.reply_text(f"Downloading: {url}")
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO
        )

        try:
            with tempfile.TemporaryDirectory(prefix="yt-dlp-") as tmp_dir:
                path = download_with_yt_dlp(url, Path(tmp_dir))

                if not path.exists():
                    await status.edit_text(f"Download failed: {url}")
                    continue

                with path.open("rb") as video_file:
                    await context.bot.send_video(
                        chat_id=message.chat_id,
                        video=video_file,
                    )
                await status.delete()
        except Exception as exc:
            await status.edit_text(f"Error: {exc}")


def main() -> None:
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
