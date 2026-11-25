#!/bin/bash
# 单实例启动脚本：Xvfb + x11vnc + websockify + Chrome for Testing
# 用户目录两种模式（由 manager 通过 PROFILE_DIR 环境变量指定）：
#   临时模式：PROFILE_DIR=/tmp/cdp-profile        —— 容器删除即清理
#   持久模式：PROFILE_DIR=/cdp-profile（挂载卷）  —— 容器删除后数据保留
# 不使用 set -e：某个后台服务失败时不应导致 chromium 永不启动。

DISPLAY_NUM=99
DISPLAY=":${DISPLAY_NUM}"
export DISPLAY

PROFILE_DIR="${PROFILE_DIR:-/tmp/cdp-profile}"
export PROFILE_DIR
mkdir -p "$PROFILE_DIR"

cleanup() {
    pkill -f "/opt/chrome-linux64/chrome" 2>/dev/null || true
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

# 4) Chrome for Testing
# 关键参数说明：
#   --user-data-dir          Chrome 136+ 起远程调试必须用非默认目录，否则 CDP 静默失效
#   --remote-debugging-*     监听 0.0.0.0，供宿主机端口映射后外部连接
#   --no-first-run 等        去掉首次运行引导 / 搜索引擎选择 / 推广页面
#   about:blank              启动时打开空白页，不进新标签页推广内容
/usr/bin/chrome \
    --user-data-dir="$PROFILE_DIR" \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    --remote-allow-origins='*' \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --no-first-run \
    --no-default-browser-check \
    --disable-search-engine-choice-screen \
    --disable-default-apps \
    --disable-features=Translate,OptimizationHints,MediaRouter \
    --window-size=1920,1080 \
    --start-maximized \
    about:blank \
    >/tmp/chromium.log 2>&1 &

# 任一关键进程退出即结束脚本，借助 restart_policy 让容器自愈重建。
# Debian bookworm 的 bash 5.x 支持 wait -n。
wait -n
