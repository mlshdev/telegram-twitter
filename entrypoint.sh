#!/usr/bin/env sh
set -eu

PUID="${PUID:-10001}"
PGID="${PGID:-10001}"

if command -v groupadd >/dev/null 2>&1; then
  groupadd -g "$PGID" appgroup >/dev/null 2>&1 || true
elif command -v addgroup >/dev/null 2>&1; then
  addgroup -g "$PGID" appgroup >/dev/null 2>&1 || true
fi

if command -v useradd >/dev/null 2>&1; then
  useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin appuser >/dev/null 2>&1 || true
elif command -v adduser >/dev/null 2>&1; then
  adduser -D -H -u "$PUID" -G appgroup appuser >/dev/null 2>&1 || true
fi

chown -R "$PUID:$PGID" /workspace

USER_NAME=""
if id -u appuser >/dev/null 2>&1; then
  USER_NAME="appuser"
fi

if command -v setpriv >/dev/null 2>&1; then
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
elif command -v gosu >/dev/null 2>&1; then
  exec gosu "$PUID:$PGID" "$@"
elif [ -n "$USER_NAME" ] && command -v su >/dev/null 2>&1; then
  exec su -s /bin/sh -c "$*" "$USER_NAME"
else
  exec "$@"
fi
