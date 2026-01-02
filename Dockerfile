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

# Telegram Bot API server builder stage
FROM docker.io/library/alpine:latest AS telegram-bot-api-builder

RUN apk add --no-cache \
    alpine-sdk \
    linux-headers \
    git \
    zlib-dev \
    openssl-dev \
    cmake \
    gperf

WORKDIR /build

# Clone and build telegram-bot-api with TDLib
RUN git clone --recursive --depth 1 https://github.com/tdlib/telegram-bot-api.git && \
    cd telegram-bot-api && \
    mkdir build && \
    cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/usr/local .. && \
    cmake --build . --target install -j $(nproc) && \
    strip /usr/local/bin/telegram-bot-api

FROM ghcr.io/astral-sh/uv:python3.14-trixie

# OCI annotations (compatible with Docker, Podman, and Kubernetes)
LABEL org.opencontainers.image.title="Telegram Twitter Bot"
LABEL org.opencontainers.image.description="Telegram bot for Twitter integration with Deno, Python and local Telegram Bot API support"
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
COPY --from=telegram-bot-api-builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

# Install runtime dependencies for telegram-bot-api
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    zlib1g \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user first for Podman rootless + SELinux
RUN useradd --create-home --uid 1000 --home-dir /home/app --shell /usr/sbin/nologin app && \
    mkdir -p /app /data /var/lib/telegram-bot-api && \
    chown -R 1000:1000 /app /data /home/app /var/lib/telegram-bot-api && \
    chmod 755 /data /var/lib/telegram-bot-api

WORKDIR /app

COPY --chown=1000:1000 pyproject.toml /app/
RUN uv pip install --system -r pyproject.toml

COPY --chown=1000:1000 main.py /app/
COPY --chown=1000:1000 entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

VOLUME /data
VOLUME /var/lib/telegram-bot-api

USER 1000

# Expose Telegram Bot API server port
EXPOSE 8081

# OCI-compliant signal handling (SIGTERM for graceful shutdown)
STOPSIGNAL SIGTERM

# Healthcheck: check if local API server is running (if enabled) or if we can reach public Telegram API
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD bash -c 'if [ -n "$TELEGRAM_API_ID" ] && [ -n "$TELEGRAM_API_HASH" ]; then nc -z localhost ${TELEGRAM_LOCAL_API_PORT:-8081}; else python -c "import socket; s=socket.socket(); s.settimeout(5); s.connect((\"api.telegram.org\", 443)); s.close()"; fi'

ENTRYPOINT ["/app/entrypoint.sh"]
