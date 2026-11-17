#!/data/data/com.termux/files/usr/bin/bash

ROOT="${AMIKI_ROOT:-/data/data/com.termux/files/home/astra_core}"
cd "$ROOT" || exit 1
export AMIKI_ROOT="$ROOT"
export ANDROID_API_LEVEL=29

# Start the optional local cloud control plane without blocking the core API.
CLOUD_ROOT="${AMIKI_CLOUD_ROOT:-$HOME/amiki-cloud}"
if [ -f "$CLOUD_ROOT/scripts/start_cloud.sh" ]; then
    if ! AMIKI_CLOUD_ROOT="$CLOUD_ROOT" bash "$CLOUD_ROOT/scripts/start_cloud.sh"; then
        printf 'Amiki Cloud startup failed; continuing with the core API.\n' \
            >> /data/data/com.termux/files/home/amiki-api.log
    fi
fi

# Share the private Cloud token with the backend proxy; Android never receives it.
TOKEN_FILE="${AMIKI_CLOUD_TOKEN_FILE:-$HOME/.config/amiki-cloud/api-token}"
if [ -s "$TOKEN_FILE" ]; then
    export AMIKI_CLOUD_API_TOKEN="$(cat "$TOKEN_FILE")"
fi

# Load the narrow ALE owner-review token from a private file. An explicitly
# exported environment value takes precedence; the secret is never logged.
OWNER_REVIEW_TOKEN_FILE="${AMIKI_OWNER_REVIEW_TOKEN_FILE:-$HOME/.config/amiki/owner-review-token}"
if [ -z "${AMIKI_OWNER_REVIEW_TOKEN:-}" ] && [ -s "$OWNER_REVIEW_TOKEN_FILE" ]; then
    export AMIKI_OWNER_REVIEW_TOKEN="$(tr -d '\r\n' < "$OWNER_REVIEW_TOKEN_FILE")"
fi

# Load Cipher's least-privilege read-only service token from a private file.
# Cipher and Amiki use the same file; the token is never sent to Android or logged.
CIPHER_READ_TOKEN_FILE="${AMIKI_CIPHER_READ_TOKEN_FILE:-$HOME/.config/amiki/cipher-read-token}"
if [ -z "${AMIKI_READ_TOKEN:-}" ] && [ -s "$CIPHER_READ_TOKEN_FILE" ]; then
    export AMIKI_READ_TOKEN="$(tr -d '\r\n' < "$CIPHER_READ_TOKEN_FILE")"
fi

# Load the dedicated Android/Core API bearer token. This credential is distinct
# from the ALE owner-review, Cloud service, and Cipher service tokens.
API_TOKEN_FILE="${AMIKI_API_TOKEN_FILE:-$HOME/.config/amiki/api-token}"
if [ -z "${AMIKI_API_TOKEN:-}" ] && [ -s "$API_TOKEN_FILE" ]; then
    export AMIKI_API_TOKEN="$(tr -d '\r\n' < "$API_TOKEN_FILE")"
fi

if curl -sf http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
    exit 0
fi

nohup .venv/bin/python -m uvicorn interfaces.api.server.app:app \
    --host 127.0.0.1 \
    --port 8000 \
    >> /data/data/com.termux/files/home/amiki-api.log 2>&1 &

exit 0
