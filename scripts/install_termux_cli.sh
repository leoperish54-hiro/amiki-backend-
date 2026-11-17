#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

ROOT="${AMIKI_ROOT:-$HOME/astra_core}"
CLI="$ROOT/scripts/amiki_cli.py"

if [[ ! -f "$CLI" ]]; then
    echo "Amiki CLI not found: $CLI" >&2
    echo "Set AMIKI_ROOT to the Amiki backend repository path and retry." >&2
    exit 1
fi

if [[ -z "${PREFIX:-}" || ! -d "$PREFIX/bin" ]]; then
    echo "Termux PREFIX/bin is unavailable; run this installer inside Termux." >&2
    exit 1
fi

cat > "$PREFIX/bin/amiki" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
export AMIKI_ROOT="$ROOT"
exec python "$CLI" "\$@"
EOF
chmod 700 "$PREFIX/bin/amiki"

echo "Installed the Amiki Termux command at: $PREFIX/bin/amiki"
echo "Examples:"
echo '  amiki ask "battery status"'
echo '  amiki device-state'
echo '  amiki device-execute speak "Hello from Amiki"'
echo "Device actions still require the on-device confirmation prompt."
