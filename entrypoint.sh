#!/bin/bash
set -e

# Telegram Bot API server configuration
TELEGRAM_API_ID="${TELEGRAM_API_ID:-}"
TELEGRAM_API_HASH="${TELEGRAM_API_HASH:-}"
TELEGRAM_LOCAL_API_PORT="${TELEGRAM_LOCAL_API_PORT:-8081}"

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
    echo "API ID: $TELEGRAM_API_ID"
    echo "Port: $TELEGRAM_LOCAL_API_PORT"
    
    # Start telegram-bot-api server in background
    telegram-bot-api \
        --api-id="$TELEGRAM_API_ID" \
        --api-hash="$TELEGRAM_API_HASH" \
        --http-port="$TELEGRAM_LOCAL_API_PORT" \
        --dir=/var/lib/telegram-bot-api \
        --temp-dir=/tmp \
        --local \
        --verbosity=2 &
    BOT_API_PID=$!
    
    # Wait for the API server to be ready
    echo "Waiting for Telegram Bot API server to start..."
    for i in $(seq 1 30); do
        if nc -z localhost "$TELEGRAM_LOCAL_API_PORT" 2>/dev/null; then
            echo "Telegram Bot API server is ready!"
            break
        fi
        if [ $i -eq 30 ]; then
            echo "ERROR: Telegram Bot API server failed to start"
            exit 1
        fi
        sleep 1
    done
    
    # Set the local API URL for the Python bot
    export TELEGRAM_LOCAL_API_URL="http://localhost:${TELEGRAM_LOCAL_API_PORT}"
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
