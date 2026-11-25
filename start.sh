#!/bin/bash
set -e

DISPLAY_NUM=99
DISPLAY=":${DISPLAY_NUM}"
export DISPLAY

cleanup() {
    pkill -f "chromium" 2>/dev/null || true
    pkill -f "x11vnc" 2>/dev/null || true
    pkill -f "websockify" 2>/dev/null || true
    pkill -f "Xvfb" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

Xvfb "$DISPLAY" -screen 0 1920x1080x24 >/dev/null 2>&1 &
sleep 1

x11vnc -display "$DISPLAY" -rfbport 5900 -forever -shared -nopw -quiet >/dev/null 2>&1 &
sleep 1

websockify :6080 127.0.0.1:5900 --web=/usr/share/novnc >/dev/null 2>&1 &
sleep 1

chromium \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    --remote-allow-origins='*' \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-infobars \
    --window-size=1920,1080 \
    --start-maximized \
    >/dev/null 2>&1 &

wait
