import asyncio
import shutil
import subprocess
import os
import re
import sys
import tempfile
import logging
import signal
import json
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

# Configure logging with highest verbosity
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)

# Set all loggers to DEBUG
logger = logging.getLogger(__name__)
logging.getLogger("aiogram").setLevel(logging.DEBUG)
logging.getLogger("aiohttp").setLevel(logging.DEBUG)
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
    # Handle www. prefix and various Twitter/X domains
    host = host.removeprefix("www.")
    return host in ("twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com")


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

    cookies_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookies_file and Path(cookies_file).exists():
        # Copy cookies to temp dir so yt-dlp can write updates (SELinux/rootless friendly)
        temp_cookies = output_dir / "cookies.txt"
        shutil.copy2(cookies_file, temp_cookies)
        ydl_opts["cookiefile"] = str(temp_cookies)
        logger.debug(f"Copied cookies from {cookies_file} to {temp_cookies}")

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


def get_video_info(path: Path) -> dict:
    """Get video stream info using ffprobe."""
    ffprobe = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
    if not Path(ffprobe).exists():
        raise RuntimeError("ffprobe not found")
    
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,sample_aspect_ratio,display_aspect_ratio",
        "-of", "json",
        str(path),
    ]
    
    logger.debug(f"Running ffprobe: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    if result.returncode != 0:
        logger.error(f"ffprobe failed: {result.stderr}")
        return {}
    
    try:
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        logger.debug(f"Video info: {stream}")
        return stream
    except (json.JSONDecodeError, IndexError) as e:
        logger.error(f"Failed to parse ffprobe output: {e}")
        return {}


def transcode_to_hevc(path: Path, output_dir: Path) -> Path:
    """Lightweight remux with aspect ratio fix - minimal CPU usage."""
    logger.debug(f"Starting video processing for {path}")
    ffmpeg = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        raise RuntimeError("ffmpeg not found")

    # Verify input file exists and is readable
    if not path.exists():
        raise RuntimeError(f"Input file does not exist: {path}")
    
    input_size = path.stat().st_size
    logger.debug(f"Input file size: {input_size} bytes")
    
    if input_size == 0:
        raise RuntimeError(f"Input file is empty: {path}")

    # Probe video to check for SAR issues
    video_info = get_video_info(path)
    width = video_info.get("width", 0)
    height = video_info.get("height", 0)
    sar = video_info.get("sample_aspect_ratio", "1:1")
    dar = video_info.get("display_aspect_ratio", "")
    
    logger.info(f"Input video: {width}x{height}, SAR={sar}, DAR={dar}")

    output_path = output_dir / f"{path.stem}.fixed.mp4"
    
    # Check if SAR needs fixing
    needs_sar_fix = False
    if sar and sar not in ("1:1", "N/A", "0:1", ""):
        try:
            sar_parts = sar.split(":")
            if len(sar_parts) == 2:
                sar_num, sar_den = int(sar_parts[0]), int(sar_parts[1])
                if sar_den > 0 and sar_num != sar_den:
                    needs_sar_fix = True
                    logger.warning(f"Non-square SAR detected ({sar})")
        except (ValueError, ZeroDivisionError):
            pass
    
    if needs_sar_fix:
        # Only re-encode if SAR is wrong - use fast preset
        logger.info("Re-encoding with SAR correction (using ultrafast preset)")
        cmd = [
            ffmpeg, "-y", "-i", str(path),
            "-vf", "scale='trunc(iw*sar/2)*2:trunc(ih/2)*2',setsar=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
    else:
        # No SAR issue - just remux (copy streams, no re-encoding)
        logger.info("SAR is correct, remuxing only (no re-encoding)")
        cmd = [
            ffmpeg, "-y", "-i", str(path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

    logger.debug(f"Running ffmpeg command: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
        logger.debug(f"ffmpeg stderr: {result.stderr}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out after 5 minutes")
    except subprocess.CalledProcessError as e:
        logger.error(f"ffmpeg failed: {e.stderr}")
        raise RuntimeError(f"ffmpeg failed: {e.stderr[:500]}")
    
    if not output_path.exists():
        raise RuntimeError(f"ffmpeg did not produce output file")
    
    logger.info(f"Processing complete: {output_path} ({output_path.stat().st_size} bytes)")
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


# Global bot instance (set in main)
bot: Bot | None = None


async def start_handler(message: Message) -> None:
    logger.debug(f"Received /start command from message: {message}")
    await message.answer(
        "Send me a URL and I will download it with yt-dlp and return the video."
    )


async def handle_message(message: Message) -> None:
    logger.debug(f"Received message: {message}")
    if not message.text:
        logger.warning("No text in message")
        return

    allowlist = parse_allowlist()
    user_id = message.from_user.id if message.from_user else None
    logger.debug(f"User ID: {user_id}, Allowlist: {allowlist}")
    if allowlist and (user_id is None or user_id not in allowlist):
        logger.warning(f"Access denied for user_id={user_id}")
        await message.answer("Access denied.")
        return

    urls = extract_urls(message.text)
    logger.debug(f"Extracted URLs: {urls}")
    if not urls:
        await message.answer("No URL found. Send me a message with a link.")
        return

    for url in urls:
        logger.info(f"Processing URL: {url}")
        status = await message.answer(f"Downloading: {url}")

        try:
            await message.bot.send_chat_action(
                chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO
            )

            with tempfile.TemporaryDirectory(prefix="yt-dlp-") as tmp_dir:
                logger.debug(f"Created temp directory: {tmp_dir}")
                path = download_with_yt_dlp(url, Path(tmp_dir))

                if not path.exists():
                    logger.error(f"Downloaded file does not exist: {path}")
                    await status.edit_text(f"Download failed: {url}")
                    continue

                file_size = path.stat().st_size
                logger.info(f"Sending video: {path} (size: {file_size} bytes)")

                # With local Bot API server, we can send files up to 2000MB
                # Check if using local API (no limit) or public API (50MB limit)
                local_api_url = os.getenv("TELEGRAM_LOCAL_API_URL")
                max_size = 2000 * 1024 * 1024 if local_api_url else 50 * 1024 * 1024
                limit_text = "2000MB" if local_api_url else "50MB"

                if file_size > max_size:
                    logger.warning(f"File too large for Telegram: {file_size} bytes")
                    await status.edit_text(
                        f"Video too large ({file_size // 1024 // 1024}MB > {limit_text} limit)"
                    )
                    continue

                # Use FSInputFile for aiogram
                video_file = FSInputFile(path)
                await message.bot.send_video(
                    chat_id=message.chat.id,
                    video=video_file,
                )
                await status.delete()
                logger.info(f"Successfully sent video for URL: {url}")
        except Exception as exc:
            logger.error(f"Error processing URL {url}: {exc}", exc_info=True)
            try:
                await status.edit_text(f"Error: {exc}")
            except Exception as edit_exc:
                logger.error(f"Failed to edit status message: {edit_exc}")


async def main() -> None:
    load_dotenv()
    logger.info("Starting Telegram Twitter Bot (aiogram)")
    logger.debug(f"Environment: YTDLP_COOKIES_FILE={os.getenv('YTDLP_COOKIES_FILE')}")
    logger.debug(f"Environment: YTDLP_TWITTER_API={os.getenv('YTDLP_TWITTER_API')}")
    logger.debug(f"Environment: YTDLP_TWITTER_API_ORDER={os.getenv('YTDLP_TWITTER_API_ORDER')}")
    logger.debug(f"Environment: ALLOWLIST_USER_IDS={os.getenv('ALLOWLIST_USER_IDS')}")
    logger.debug(f"Environment: TELEGRAM_LOCAL_API_URL={os.getenv('TELEGRAM_LOCAL_API_URL')}")

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN is not set")
        raise RuntimeError("BOT_TOKEN is not set")

    # Configure session for local API server if specified
    local_api_url = os.getenv("TELEGRAM_LOCAL_API_URL")
    session = None

    if local_api_url:
        logger.info(f"Using local Telegram Bot API server: {local_api_url}")
        # Create custom API server configuration for local server
        api_server = TelegramAPIServer.from_base(local_api_url, is_local=True)
        session = AiohttpSession(api=api_server)

    logger.debug("Building bot instance")
    global bot
    bot = Bot(token=token, session=session)

    # Create dispatcher
    dp = Dispatcher()

    # Register handlers
    dp.message.register(start_handler, Command("start"))
    dp.message.register(handle_message, F.text & ~F.text.startswith("/"))

    # Graceful shutdown handling
    loop = asyncio.get_event_loop()

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        loop.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("Starting polling")
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
