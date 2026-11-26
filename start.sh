#!/bin/bash
# 单实例启动脚本：Xvfb + x11vnc + websockify + Chrome for Testing
# 两份镜像各自只装一份二进制，内核由镜像内 ENV BROWSER_MODE 固定（manager 也会传入同值）：
#   full    : /usr/bin/chrome               完整浏览器，VNC 和 screencast 都能用
#   headless: /usr/bin/chrome-headless-shell 无头内核，只能 screencast（无浏览器界面）
# 用户目录两种模式（由 manager 通过 PROFILE_DIR 环境变量指定）：
#   临时模式：PROFILE_DIR=/tmp/cdp-profile        —— 容器删除即清理
#   持久模式：PROFILE_DIR=/cdp-profile（挂载卷）  —— 容器删除后数据保留
# 不使用 set -e：某个后台服务失败时不应导致浏览器永不启动。

BROWSER_MODE="${BROWSER_MODE:-full}"
if [ "$BROWSER_MODE" = "headless" ]; then
    CHROME_BIN=/usr/bin/chrome-headless-shell
else
    BROWSER_MODE=full
    CHROME_BIN=/usr/bin/chrome
fi

# 启动自检：直接打到 stdout，让 manager 的实例日志/全局日志能看到。
# 之前 chrome 报错被重定向进文件，崩溃时只剩 exit 127 无从排查。
echo "=== browser start: mode=$BROWSER_MODE bin=$CHROME_BIN ==="
if [ ! -x "$CHROME_BIN" ]; then
    echo "FATAL: 浏览器二进制不存在或不可执行: $CHROME_BIN" >&2
    ls -l "$CHROME_BIN" >&2 2>&1 || true
    exit 127
fi
if command -v ldd >/dev/null 2>&1; then
    MISSING=$(ldd "$CHROME_BIN" 2>&1 | grep -i 'not found' || true)
    if [ -n "$MISSING" ]; then
        echo "FATAL: 浏览器缺失动态库:" >&2
        echo "$MISSING" >&2
        exit 127
    fi
fi

DISPLAY_NUM=99
DISPLAY=":${DISPLAY_NUM}"
export DISPLAY

PROFILE_DIR="${PROFILE_DIR:-/tmp/cdp-profile}"
export PROFILE_DIR
mkdir -p "$PROFILE_DIR"

cleanup() {
    pkill -f "/opt/chrome-linux64/chrome" 2>/dev/null || true
    pkill -f "/opt/chrome-headless-shell-linux64/chrome-headless-shell" 2>/dev/null || true
    pkill -f "x11vnc" 2>/dev/null || true
    pkill -f "websockify" 2>/dev/null || true
    pkill -f "Xvfb" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 去 Google 化：官方 for Testing 包本无广告，这里再关掉后台联网/组件更新/同步等
# 谷歌服务请求；CDP 参数在两种内核下完全一致，外部客户端可直连同一 WS 端口。
CHROME_ARGS=(
    --user-data-dir="$PROFILE_DIR"
    --remote-debugging-port=9222
    --remote-debugging-address=0.0.0.0
    --remote-allow-origins='*'
    --no-sandbox
    --disable-dev-shm-usage
    --disable-gpu
    --no-first-run
    --no-default-browser-check
    --disable-search-engine-choice-screen
    --disable-default-apps
    --disable-background-networking
    --disable-component-update
    --disable-sync
    --disable-domain-reliability
    --metrics-recording-only
    --disable-features=Translate,OptimizationHints,MediaRouter
    --window-size=1920,1080
)

if [ "$BROWSER_MODE" = "full" ]; then
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

    CHROME_ARGS+=(--start-maximized)
fi

# 4) 浏览器（两种内核共用同一套 CDP 参数）
# 关键参数说明：
#   --user-data-dir          Chrome 136+ 起远程调试必须用非默认目录，否则 CDP 静默失效
#   --remote-debugging-*     监听 0.0.0.0，供宿主机端口映射后外部连接
#   about:blank              启动时打开空白页，不进新标签页推广内容
# 关键：不要把 chrome 输出重定向到文件。之前写进 /tmp/chromium.log 导致
# docker logs / 全局日志看不到任何报错，崩溃时只显示 exit 127 却查不到原因。
# 直接输出到 stdout/stderr，让 manager 的实例日志接口能拿到真实错误。
"$CHROME_BIN" "${CHROME_ARGS[@]}" about:blank &

# 任一关键进程退出即结束脚本，借助 restart_policy 让容器自愈重建。
# Debian bookworm 的 bash 5.x 支持 wait -n。
wait -n
