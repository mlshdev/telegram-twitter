#!/bin/bash
set -e

# Telegram Bot API server configuration
# Required for local API server
TELEGRAM_API_ID="${TELEGRAM_API_ID:-}"
TELEGRAM_API_HASH="${TELEGRAM_API_HASH:-}"

# Server network configuration
# Priority: TELEGRAM_HTTP_PORT > TELEGRAM_LOCAL_API_PORT (backward compat) > 8081 (default)
TELEGRAM_HTTP_PORT="${TELEGRAM_HTTP_PORT:-${TELEGRAM_LOCAL_API_PORT:-8081}}"
TELEGRAM_HTTP_IP_ADDRESS="${TELEGRAM_HTTP_IP_ADDRESS:-}"

# Statistics endpoint (set to enable, uses port 8082)
TELEGRAM_STAT="${TELEGRAM_STAT:-}"

# Bot filtering: "<remainder>/<modulo>" - Allow only bots with 'bot_user_id % modulo == remainder'
TELEGRAM_FILTER="${TELEGRAM_FILTER:-}"

# Webhook configuration
TELEGRAM_MAX_WEBHOOK_CONNECTIONS="${TELEGRAM_MAX_WEBHOOK_CONNECTIONS:-}"

# Logging configuration
TELEGRAM_VERBOSITY="${TELEGRAM_VERBOSITY:-2}"
TELEGRAM_LOG_FILE="${TELEGRAM_LOG_FILE:-}"

# Connection limits
TELEGRAM_MAX_CONNECTIONS="${TELEGRAM_MAX_CONNECTIONS:-}"

# HTTP proxy for outgoing webhook requests (format: http://host:port)
TELEGRAM_PROXY="${TELEGRAM_PROXY:-}"

# Allow local requests (enables local mode for file operations)
TELEGRAM_LOCAL="${TELEGRAM_LOCAL:-1}"

# Function to cleanup child processes on exit
cleanup() {
    echo "Shutting down..."
    if [ -n "$BOT_API_PID" ]; then
        kill -TERM "$BOT_API_PID" 2>/dev/null || true
        wait "$BOT_API_PID" 2>/dev/null || true
    fi
    if [ -n "$PYTHON_PID" ]; then
        kill -TERM "$PYTHON_PID" 2>/dev/null || true
        wait "$PYTHON_PID" 2>/dev/null || true
    fi
    exit 0
}

trap cleanup SIGTERM SIGINT

# Check if we should start the local Telegram Bot API server
if [ -n "$TELEGRAM_API_ID" ] && [ -n "$TELEGRAM_API_HASH" ]; then
    echo "Starting local Telegram Bot API server..."
    echo "Port: $TELEGRAM_HTTP_PORT"
    
    # Build command arguments
    CMD_ARGS=(
        "--api-id=$TELEGRAM_API_ID"
        "--api-hash=$TELEGRAM_API_HASH"
        "--http-port=$TELEGRAM_HTTP_PORT"
        "--dir=/var/lib/telegram-bot-api"
        "--temp-dir=/tmp"
        "--verbosity=$TELEGRAM_VERBOSITY"
    )
    
    # Add optional arguments based on environment variables
    [ "$TELEGRAM_LOCAL" != "0" ] && CMD_ARGS+=("--local")
    [ -n "$TELEGRAM_STAT" ] && CMD_ARGS+=("--http-stat-port=8082")
    
    # Validate TELEGRAM_FILTER format: must be "<remainder>/<modulo>" where both are integers and modulo > 0
    if [ -n "$TELEGRAM_FILTER" ]; then
        if [[ "$TELEGRAM_FILTER" =~ ^(0|[1-9][0-9]*)/[1-9][0-9]*$ ]]; then
            CMD_ARGS+=("--filter=$TELEGRAM_FILTER")
        else
            echo "WARNING: TELEGRAM_FILTER='$TELEGRAM_FILTER' is invalid. Expected format: '<remainder>/<modulo>' (e.g., '0/2'). Ignoring."
        fi
    fi
    [ -n "$TELEGRAM_MAX_WEBHOOK_CONNECTIONS" ] && CMD_ARGS+=("--max-webhook-connections=$TELEGRAM_MAX_WEBHOOK_CONNECTIONS")
    [ -n "$TELEGRAM_LOG_FILE" ] && CMD_ARGS+=("--log=$TELEGRAM_LOG_FILE")
    [ -n "$TELEGRAM_MAX_CONNECTIONS" ] && CMD_ARGS+=("--max-connections=$TELEGRAM_MAX_CONNECTIONS")
    [ -n "$TELEGRAM_PROXY" ] && CMD_ARGS+=("--proxy=$TELEGRAM_PROXY")
    [ -n "$TELEGRAM_HTTP_IP_ADDRESS" ] && CMD_ARGS+=("--http-ip-address=$TELEGRAM_HTTP_IP_ADDRESS")
    
    echo "Command: telegram-bot-api ${CMD_ARGS[*]}"
    
    # Start telegram-bot-api server in background
    telegram-bot-api "${CMD_ARGS[@]}" &
    BOT_API_PID=$!
    
    # Wait for the API server to be ready
    echo "Waiting for Telegram Bot API server to start..."
    for i in $(seq 1 30); do
        # Check if the process is still running
        if ! kill -0 "$BOT_API_PID" 2>/dev/null; then
            echo "ERROR: Telegram Bot API server process exited unexpectedly"
            exit 1
        fi
        if nc -z localhost "$TELEGRAM_HTTP_PORT" 2>/dev/null; then
            echo "Telegram Bot API server is ready!"
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "ERROR: Telegram Bot API server failed to start"
            exit 1
        fi
        sleep 1
    done
    
    # Set the local API URL for the Python bot
    export TELEGRAM_LOCAL_API_URL="http://localhost:${TELEGRAM_HTTP_PORT}"
else
    echo "TELEGRAM_API_ID or TELEGRAM_API_HASH not set, using public Telegram API"
    echo "Note: File uploads will be limited to 50MB"
fi

# Start the Python bot
echo "Starting Python bot..."
python -u main.py &
PYTHON_PID=$!

# Wait for the Python process
wait "$PYTHON_PID"
