#!/bin/bash
# 单实例启动脚本：Xvfb + x11vnc + websockify + chromium
# 不使用 set -e：某个后台服务启动失败时不应导致脚本提前退出、chromium 永不启动

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

# 1) 虚拟显示。残留 lock 会导致新容器起不来，先清理。
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true
Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
# 显式等待 X socket 就绪（最多 15 秒），替代固定 sleep
for _ in $(seq 1 30); do
    [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
    sleep 0.5
done

# 2) VNC 服务端
x11vnc -display "$DISPLAY" -rfbport 5900 -forever -shared -nopw -quiet >/tmp/x11vnc.log 2>&1 &
sleep 1

# 3) noVNC websocket 桥
websockify :6080 127.0.0.1:5900 --web=/usr/share/novnc >/tmp/websockify.log 2>&1 &
sleep 1

# 4) Chromium
# 关键：Chrome 136+ 起，若使用默认 user-data-dir，--remote-debugging-port 会被静默忽略，
# 表现为端口不监听 / TCP 通但无 HTTP 响应。必须指定一个非默认目录 CDP 才真正生效。
PROFILE_DIR=/tmp/cdp-profile
mkdir -p "$PROFILE_DIR"
chromium \
    --user-data-dir="$PROFILE_DIR" \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    --remote-allow-origins='*' \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-infobars \
    --no-first-run \
    --window-size=1920,1080 \
    --start-maximized \
    >/tmp/chromium.log 2>&1 &

# 任一关键进程退出即结束脚本，借助 restart_policy 让容器自愈重建。
# Debian bookworm 的 bash 5.x 支持 wait -n。
wait -n
