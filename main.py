import shutil
import subprocess
import os
import re
import sys
import tempfile
import logging
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

# Configure logging with highest verbosity
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Set all loggers to DEBUG
logger = logging.getLogger(__name__)
logging.getLogger("telegram").setLevel(logging.DEBUG)
logging.getLogger("telegram.ext").setLevel(logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.DEBUG)
logging.getLogger("httpcore").setLevel(logging.DEBUG)
logging.getLogger("yt_dlp").setLevel(logging.DEBUG)

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
    logger.debug(f"Building yt-dlp options for output_dir={output_dir}, twitter_api={twitter_api}")
    ydl_opts: dict = {
        "format": os.getenv("YTDLP_FORMAT", "bestvideo*+bestaudio/best"),
        "merge_output_format": "mp4",
        "remuxvideo": "mp4",
        "outtmpl": str(output_dir / "%(title).200B.%(ext)s"),
        "quiet": False,
        "no_warnings": False,
        "verbose": True,
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
    logger.debug(f"Starting HEVC transcode for {path}")
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
    logger.debug(f"Running ffmpeg command: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    logger.debug(f"ffmpeg stdout: {result.stdout}")
    logger.debug(f"ffmpeg stderr: {result.stderr}")
    logger.info(f"HEVC transcode complete: {output_path}")
    return output_path


def download_with_yt_dlp(url: str, output_dir: Path) -> Path:
    logger.info(f"Starting download for URL: {url}")
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

    logger.debug(f"API candidates: {api_candidates}")
    last_error: Exception | None = None
    attempts = api_candidates if is_twitter_url(url) else [None]
    logger.debug(f"Is Twitter URL: {is_twitter_url(url)}, attempts: {attempts}")

    for api in attempts:
        try:
            logger.debug(f"Attempting download with API: {api}")
            ydl_opts = build_ydl_opts(output_dir, api)
            logger.debug(f"yt-dlp options: {ydl_opts}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                logger.debug(f"Extracted info: {info}")
                filename = ydl.prepare_filename(info)
                logger.debug(f"Prepared filename: {filename}")
            downloaded = normalize_download_path(filename)
            logger.info(f"Downloaded file: {downloaded}")
            return transcode_to_hevc(downloaded, output_dir)
        except Exception as exc:
            logger.error(f"Download attempt failed with API {api}: {exc}", exc_info=True)
            last_error = exc
            if not is_twitter_url(url):
                break

    if last_error:
        raise last_error
    raise RuntimeError("Download failed")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.debug(f"Received /start command from update: {update}")
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "Send me a URL and I will download it with yt-dlp and return the video."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.debug(f"Received message update: {update}")
    message = update.message
    if not message:
        logger.warning("No message in update")
        return

    allowlist = parse_allowlist()
    user_id = message.from_user.id if message.from_user else None
    logger.debug(f"User ID: {user_id}, Allowlist: {allowlist}")
    if allowlist and (user_id is None or user_id not in allowlist):
        logger.warning(f"Access denied for user_id={user_id}")
        await message.reply_text("Access denied.")
        return

    urls = extract_urls(message.text)
    logger.debug(f"Extracted URLs: {urls}")
    if not urls:
        await message.reply_text("No URL found. Send me a message with a link.")
        return

    for url in urls:
        logger.info(f"Processing URL: {url}")
        status = await message.reply_text(f"Downloading: {url}")
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO
        )

        try:
            with tempfile.TemporaryDirectory(prefix="yt-dlp-") as tmp_dir:
                logger.debug(f"Created temp directory: {tmp_dir}")
                path = download_with_yt_dlp(url, Path(tmp_dir))

                if not path.exists():
                    logger.error(f"Downloaded file does not exist: {path}")
                    await status.edit_text(f"Download failed: {url}")
                    continue

                logger.info(f"Sending video: {path} (size: {path.stat().st_size} bytes)")
                with path.open("rb") as video_file:
                    await context.bot.send_video(
                        chat_id=message.chat_id,
                        video=video_file,
                    )
                await status.delete()
                logger.info(f"Successfully sent video for URL: {url}")
        except Exception as exc:
            logger.error(f"Error processing URL {url}: {exc}", exc_info=True)
            await status.edit_text(f"Error: {exc}")


def main() -> None:
    load_dotenv()
    logger.info("Starting Telegram Twitter Bot")
    logger.debug(f"Environment: YTDLP_COOKIES_FILE={os.getenv('YTDLP_COOKIES_FILE')}")
    logger.debug(f"Environment: YTDLP_TWITTER_API={os.getenv('YTDLP_TWITTER_API')}")
    logger.debug(f"Environment: YTDLP_TWITTER_API_ORDER={os.getenv('YTDLP_TWITTER_API_ORDER')}")
    logger.debug(f"Environment: ALLOWLIST_USER_IDS={os.getenv('ALLOWLIST_USER_IDS')}")

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN is not set")
        raise RuntimeError("BOT_TOKEN is not set")

    logger.debug("Building application")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting polling")
    app.run_polling()


if __name__ == "__main__":
    main()
