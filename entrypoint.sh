#!/bin/bash
set -e

# Start the FastAPI server with uvicorn
echo "Starting Video Download API server..."

# Configuration
HOST="${API_HOST:-0.0.0.0}"
PORT="${API_PORT:-8000}"
WORKERS="${API_WORKERS:-1}"
LOG_LEVEL="${API_LOG_LEVEL:-info}"

exec uvicorn main:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers "$WORKERS" \
    --log-level "$LOG_LEVEL" \
    --proxy-headers \
    --forwarded-allow-ips='*'
