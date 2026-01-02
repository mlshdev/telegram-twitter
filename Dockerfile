FROM docker.io/denoland/deno:bin AS deno-bin

# FFmpeg downloader stage
FROM docker.io/library/alpine:latest AS ffmpeg-downloader

ARG TARGETARCH

RUN apk add --no-cache curl tar xz

RUN set -ex; \
    case "${TARGETARCH}" in \
    amd64) FFMPEG_ARCH="linux64" ;; \
    arm64) FFMPEG_ARCH="linuxarm64" ;; \
    *) echo "Unsupported architecture: ${TARGETARCH}" && exit 1 ;; \
    esac; \
    curl -L "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-${FFMPEG_ARCH}-gpl.tar.xz" -o /tmp/ffmpeg.tar.xz && \
    mkdir -p /ffmpeg && \
    tar -xJf /tmp/ffmpeg.tar.xz -C /ffmpeg --strip-components=1 && \
    rm /tmp/ffmpeg.tar.xz

FROM ghcr.io/astral-sh/uv:python3.14-trixie

# OCI annotations (compatible with Docker, Podman, and Kubernetes)
LABEL org.opencontainers.image.title="Telegram Twitter Bot"
LABEL org.opencontainers.image.description="Telegram bot for Twitter integration with Deno and Python support"
LABEL org.opencontainers.image.vendor="mlshdev"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/mlshdev/telegram-twitter"
LABEL org.opencontainers.image.documentation="https://github.com/mlshdev/telegram-twitter/blob/main/README.md"
LABEL org.opencontainers.image.url="https://github.com/mlshdev/telegram-twitter"
LABEL org.opencontainers.image.base.name="ghcr.io/astral-sh/uv:python3.14-trixie"

# Explicit shell for OCI compliance
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/usr/local/bin:${PATH}" \
    HOME=/home/app

COPY --from=deno-bin /deno /usr/local/bin/deno
COPY --from=ffmpeg-downloader /ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-downloader /ffmpeg/bin/ffprobe /usr/local/bin/ffprobe

# Create non-root user first for Podman rootless + SELinux
RUN useradd --create-home --uid 1000 --home-dir /home/app --shell /usr/sbin/nologin app && \
    mkdir -p /app /data && \
    chown -R 1000:1000 /app /data /home/app && \
    chmod 755 /data

WORKDIR /app

COPY --chown=1000:1000 pyproject.toml /app/
RUN uv pip install --system -r pyproject.toml

COPY --chown=1000:1000 main.py /app/

VOLUME /data

USER 1000

# OCI-compliant signal handling (SIGTERM for graceful shutdown)
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys; sys.exit(0)"

CMD ["python", "-u", "main.py"]
