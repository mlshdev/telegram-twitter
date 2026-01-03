"""
Video Download API Server

A FastAPI-based API server that downloads videos from various platforms
using yt-dlp and returns them in Apple QuickTime friendly format.

For use with iOS apps via "Share with App" feature.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import yt_dlp
from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, HttpUrl

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================


def get_auth_tokens() -> set[str]:
    """Parse AUTH_TOKENS from environment variable (comma-separated)."""
    raw = os.getenv("AUTH_TOKENS", "").strip()
    if not raw:
        return set()
    return {token.strip() for token in raw.split(",") if token.strip()}


# ============================================================================
# Security
# ============================================================================

api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


async def verify_token(
    api_key: Annotated[str | None, Security(api_key_header)],
) -> str:
    """Verify the API key from the Authorization header."""
    tokens = get_auth_tokens()
    if not tokens:
        logger.warning("No AUTH_TOKENS configured - API is unprotected!")
        return "anonymous"

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    # Support "Bearer <token>" format
    token = api_key.removeprefix("Bearer ").strip()

    if token not in tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    return token


# ============================================================================
# Request/Response Models
# ============================================================================


class DownloadRequest(BaseModel):
    """Request body for video download."""

    url: HttpUrl


class ErrorResponse(BaseModel):
    """Error response model."""

    detail: str


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str


# ============================================================================
# Video Download Logic
# ============================================================================


def is_twitter_url(url: str) -> bool:
    """Check if URL is from Twitter/X."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    host = host.removeprefix("www.")
    return host in ("twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com")


def build_ydl_opts(output_dir: Path, twitter_api: str | None = None) -> dict:
    """Build yt-dlp options optimized for Apple QuickTime compatibility."""
    # Format selection for Apple QuickTime compatibility:
    # - Prefer H.264 video codec (avc1) which has native QuickTime support
    # - Prefer AAC audio codec which is natively supported
    # - MP4 container is universally compatible
    # - Avoid VP9/AV1 which require additional codecs on iOS
    format_spec = os.getenv(
        "YTDLP_FORMAT",
        # Prefer H.264 + AAC in MP4, fallback to best available
        "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/bestvideo+bestaudio/best",
    )

    ydl_opts: dict = {
        "format": format_spec,
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "%(title).200B.%(ext)s"),
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "extractor_retries": 3,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        # Postprocessor to ensure MP4 container with faststart for streaming
        "postprocessors": [
            {
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }
        ],
    }

    # FFmpeg location
    if Path("/usr/local/bin/ffmpeg").exists():
        ydl_opts["ffmpeg_location"] = "/usr/local/bin"

    # Deno for JavaScript-heavy sites
    if Path("/usr/local/bin/deno").exists():
        ydl_opts["js_runtimes"] = {"deno": {"path": "/usr/local/bin/deno"}}

    # Cookies file
    cookies_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookies_file and Path(cookies_file).exists():
        temp_cookies = output_dir / "cookies.txt"
        shutil.copy2(cookies_file, temp_cookies)
        ydl_opts["cookiefile"] = str(temp_cookies)
        logger.debug(f"Using cookies from {cookies_file}")

    # Twitter-specific API
    if twitter_api:
        ydl_opts["extractor_args"] = {"twitter": {"api": [twitter_api]}}

    # Custom User-Agent
    user_agent = os.getenv("YTDLP_USER_AGENT")
    if user_agent:
        ydl_opts["http_headers"] = {"User-Agent": user_agent}

    return ydl_opts


def get_video_info(path: Path) -> dict:
    """Get video stream info using ffprobe."""
    ffprobe = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
    if not Path(ffprobe).exists():
        logger.warning("ffprobe not found, skipping video analysis")
        return {}

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,sample_aspect_ratio,display_aspect_ratio",
        "-of",
        "json",
        str(path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"ffprobe failed: {result.stderr}")
            return {}
        data = json.loads(result.stdout)
        return data.get("streams", [{}])[0]
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        logger.error(f"ffprobe error: {e}")
        return {}


def needs_quicktime_fix(video_info: dict) -> tuple[bool, str]:
    """
    Check if video needs processing for Apple QuickTime compatibility.
    Returns (needs_fix, reason).

    QuickTime issues:
    1. Non-square SAR (Sample Aspect Ratio) - causes stretched video
    2. Non-H.264/HEVC codecs - VP9, AV1 not natively supported
    """
    codec = video_info.get("codec_name", "")
    sar = video_info.get("sample_aspect_ratio", "1:1")

    # Check codec compatibility
    quicktime_codecs = {"h264", "hevc", "avc1", "hvc1", "aac", "mp4a"}
    if codec and codec.lower() not in quicktime_codecs:
        return True, f"Incompatible codec: {codec}"

    # Check SAR
    if sar and sar not in ("1:1", "N/A", "0:1", ""):
        try:
            sar_parts = sar.split(":")
            if len(sar_parts) == 2:
                sar_num, sar_den = int(sar_parts[0]), int(sar_parts[1])
                if sar_den > 0 and sar_num != sar_den:
                    return True, f"Non-square SAR: {sar}"
        except (ValueError, ZeroDivisionError):
            pass

    return False, ""


def process_for_quicktime(path: Path, output_dir: Path) -> Path:
    """
    Process video for Apple QuickTime compatibility with minimal ffmpeg usage.
    Only re-encodes if absolutely necessary.
    """
    ffmpeg = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        logger.warning("ffmpeg not found, returning original file")
        return path

    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Invalid input file: {path}")

    video_info = get_video_info(path)
    needs_fix, reason = needs_quicktime_fix(video_info)

    output_path = output_dir / f"{path.stem}.qt.mp4"

    if needs_fix:
        logger.info(f"Processing required: {reason}")
        # Re-encode with H.264 for maximum compatibility
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-vf",
            "scale='trunc(iw*sar/2)*2:trunc(ih/2)*2',setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-brand",
            "mp42",
            str(output_path),
        ]
    else:
        # Just remux with faststart for streaming (no re-encoding)
        logger.info("Remuxing for streaming optimization (no re-encoding)")
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-brand",
            "mp42",
            str(output_path),
        ]

    try:
        result = subprocess.run(
            cmd, check=True, capture_output=True, text=True, timeout=600
        )
        logger.debug(f"ffmpeg output: {result.stderr[-500:] if result.stderr else ''}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Video processing timed out (10 minutes)")
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr[-500:] if e.stderr else str(e)
        raise RuntimeError(f"Video processing failed: {error_msg}")

    if not output_path.exists():
        raise RuntimeError("Video processing produced no output")

    logger.info(f"Processed video: {output_path.stat().st_size} bytes")
    return output_path


def normalize_download_path(filename: str) -> Path:
    """Normalize downloaded file path, handling extension changes."""
    path = Path(filename)
    if path.suffix.lower() != ".mp4" and path.exists():
        mp4_path = path.with_suffix(".mp4")
        if mp4_path.exists():
            return mp4_path
    return path


def download_video(url: str, output_dir: Path) -> Path:
    """
    Download video using yt-dlp and process for Apple QuickTime.
    Returns path to the processed video file.
    """
    logger.info(f"Starting download: {url}")

    # Twitter API fallback order
    twitter_api_order = os.getenv(
        "YTDLP_TWITTER_API_ORDER", "graphql,legacy,syndication"
    )
    api_candidates = [p.strip() for p in twitter_api_order.split(",") if p.strip()]
    if not api_candidates:
        api_candidates = [os.getenv("YTDLP_TWITTER_API", "syndication")]

    # For non-Twitter URLs, only try once without special API
    attempts = api_candidates if is_twitter_url(url) else [None]
    last_error: Exception | None = None

    for api in attempts:
        try:
            ydl_opts = build_ydl_opts(output_dir, api)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

            downloaded = normalize_download_path(filename)
            if not downloaded.exists():
                raise RuntimeError(f"Downloaded file not found: {downloaded}")

            logger.info(f"Downloaded: {downloaded} ({downloaded.stat().st_size} bytes)")
            return process_for_quicktime(downloaded, output_dir)

        except Exception as exc:
            logger.error(f"Download failed (api={api}): {exc}")
            last_error = exc
            if not is_twitter_url(url):
                break

    if last_error:
        raise last_error
    raise RuntimeError("Download failed with unknown error")


# ============================================================================
# FastAPI Application
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("Video Download API starting up")
    tokens = get_auth_tokens()
    if tokens:
        logger.info(f"Loaded {len(tokens)} auth token(s)")
    else:
        logger.warning("No AUTH_TOKENS configured - API is unprotected!")
    yield
    logger.info("Video Download API shutting down")


app = FastAPI(
    title="Video Download API",
    description="Download videos from various platforms in Apple QuickTime friendly format",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Health check endpoint for container orchestration."""
    return HealthResponse(status="healthy", version="1.0.0")


@app.post(
    "/download",
    response_class=FileResponse,
    responses={
        200: {
            "description": "Video file download",
            "content": {"video/mp4": {}},
        },
        400: {"model": ErrorResponse, "description": "Invalid request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Download failed"},
    },
    tags=["Download"],
)
async def download_endpoint(
    request: DownloadRequest,
    token: Annotated[str, Depends(verify_token)],
):
    """
    Download a video from the provided URL.

    The video will be processed for Apple QuickTime compatibility:
    - H.264 video codec (if conversion needed)
    - AAC audio codec
    - MP4 container with faststart for streaming
    - Corrected aspect ratio

    **Authentication**: Requires `Authorization: Bearer <token>` header.
    """
    url = str(request.url)
    logger.info(f"Download request: {url}")

    # Create temporary directory for download
    tmp_dir = tempfile.mkdtemp(prefix="ytdlp-api-")
    tmp_path = Path(tmp_dir)

    try:
        video_path = download_video(url, tmp_path)

        if not video_path.exists():
            shutil.rmtree(tmp_path, ignore_errors=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Download completed but video file not found",
            )

        # Return file with appropriate headers for iOS
        # Use background task to clean up temp directory after response is sent
        filename = video_path.name

        def cleanup_temp_dir():
            shutil.rmtree(tmp_path, ignore_errors=True)
            logger.debug(f"Cleaned up temp directory: {tmp_path}")

        from starlette.background import BackgroundTask

        return FileResponse(
            path=video_path,
            media_type="video/mp4",
            filename=filename,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(cleanup_temp_dir),
        )

    except HTTPException:
        # Re-raise HTTP exceptions as-is
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(tmp_path, ignore_errors=True)
        error_msg = str(e)
        if "Private video" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Video is private or unavailable",
            )
        elif "Video unavailable" in error_msg or "not available" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Video not found or unavailable",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Download error: {error_msg[:200]}",
            )
    except Exception as e:
        shutil.rmtree(tmp_path, ignore_errors=True)
        logger.exception(f"Download failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Download failed: {str(e)[:200]}",
        )
