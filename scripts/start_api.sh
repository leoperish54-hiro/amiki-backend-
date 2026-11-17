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

# Owner authentication is UOSA-only. No legacy owner bearer is loaded here.
unset AMIKI_OWNER_REVIEW_TOKEN AMIKI_OWNER_REVIEW_TOKEN_FILE
# Initial device setup uses a short-lived one-time code generated explicitly by
# scripts/create_uosa_setup_code.py; recovery uses one-time recovery codes.

# Load Cipher's least-privilege read-only service token from a private file.
# Cipher and Amiki use the same file; the token is never sent to Android or logged.
CIPHER_READ_TOKEN_FILE="${AMIKI_CIPHER_READ_TOKEN_FILE:-$HOME/.config/amiki/cipher-read-token}"
if [ -z "${AMIKI_READ_TOKEN:-}" ] && [ -s "$CIPHER_READ_TOKEN_FILE" ]; then
    export AMIKI_READ_TOKEN="$(tr -d '\r\n' < "$CIPHER_READ_TOKEN_FILE")"
fi

API_PYTHON="${AMIKI_API_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$API_PYTHON" ]; then
    printf 'Amiki API Python interpreter not found: %s\\n' "$API_PYTHON" >&2
    exit 1
fi

# Termux must use its package-managed cryptography build. A pip-built native
# extension can fail to link against Termux's libpython on Android.
if ! "$API_PYTHON" -c 'from cryptography.hazmat.primitives.asymmetric import ec' >/dev/null 2>&1; then
    printf 'UOSA crypto dependency is unavailable to %s. On Termux run: pkg install python-cryptography; ensure .venv was created with --system-site-packages; install interfaces/api/requirements-termux.txt (not the pip cryptography extra).\\n' "$API_PYTHON" >&2
    exit 1
fi

if curl -sf http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
    exit 0
fi

nohup "$API_PYTHON" -m uvicorn interfaces.api.server.app:app \
    --host 127.0.0.1 \
    --port 8000 \
    >> /data/data/com.termux/files/home/amiki-api.log 2>&1 &

exit 0
