#!/usr/bin/env bash
set -euo pipefail

: "${INTERNXT_EMAIL:?INTERNXT_EMAIL is required}"
: "${INTERNXT_PASSWORD:?INTERNXT_PASSWORD is required}"

PORT="${WEBDAV_PORT:-3005}"
HOST="${WEBDAV_HOST:-0.0.0.0}"

# Build login args
LOGIN_ARGS=(login --non-interactive --email "$INTERNXT_EMAIL" --password "$INTERNXT_PASSWORD")

# If a TOTP secret is set, use --tfa-secret for automatic 2FA code generation.
# If a static 2FA code is set (e.g. for one-shot use), pass it directly.
if [ -n "${INTERNXT_TFA_SECRET:-}" ]; then
    LOGIN_ARGS+=(--tfa-secret "$INTERNXT_TFA_SECRET")
elif [ -n "${INTERNXT_TFA:-}" ]; then
    LOGIN_ARGS+=(--tfa "$INTERNXT_TFA")
fi

echo "Logging in as ${INTERNXT_EMAIL}..."
python cli.py "${LOGIN_ARGS[@]}"

echo "Starting WebDAV server on ${HOST}:${PORT}..."
exec python cli.py webdav-start --port "$PORT"
