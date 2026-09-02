#!/bin/sh
# SurfShark Ultra-Lite launcher
# Starts the local status panel (if not running) and opens it in the browser.
DIR="$(dirname "$(readlink -f "$0")")"
PORT="${SURFSHARK_WEBUI_PORT:-8777}"
if ! curl -s --max-time 1 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1; then
    (setsid python3 "$DIR/app.py" >/dev/null 2>&1 &)
    sleep 1
fi
xdg-open "http://127.0.0.1:$PORT" >/dev/null 2>&1 &
exit 0
