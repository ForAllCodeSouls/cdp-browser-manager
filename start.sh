#!/bin/bash
# 单实例启动脚本：Xvfb + x11vnc + websockify + Chrome for Testing
# 三种启动方式，由 BROWSER_MODE 决定（manager 传入，镜像 ENV 为默认值）：
#   full        : /usr/bin/chrome               完整浏览器 + Xvfb/VNC，有界面
#   headless    : /usr/bin/chrome-headless-shell 老无头壳，无界面，仅抓屏；身份由 manager CDP 补丁伪装
#   headless-new: /usr/bin/chrome --headless=new 完整 Chrome 的新无头模式，无界面，仅抓屏，身份由 manager CDP 补丁伪装
# 注意：headless-new 用的是 full 镜像里的完整 Chrome，不需要额外镜像；无 VNC/noVNC。
# 用户目录两种模式（由 manager 通过 PROFILE_DIR 环境变量指定）：
#   临时模式：PROFILE_DIR=/tmp/cdp-profile        —— 容器删除即清理
#   持久模式：PROFILE_DIR=/cdp-profile（挂载卷）  —— 容器删除后数据保留
# 不使用 set -e：某个后台服务失败时不应导致浏览器永不启动。

BROWSER_MODE="${BROWSER_MODE:-full}"
if [ "$BROWSER_MODE" = "headless" ]; then
    CHROME_BIN=/usr/bin/chrome-headless-shell
elif [ "$BROWSER_MODE" = "headless-new" ]; then
    # 完整 Chrome 的新无头模式：身份与有头一致，不用 CDP 补丁。
    CHROME_BIN=/usr/bin/chrome
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
    pkill -f "socat" 2>/dev/null || true
    pkill -f "x11vnc" 2>/dev/null || true
    pkill -f "websockify" 2>/dev/null || true
    pkill -f "Xvfb" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# CDP 暴露：Chrome 136+ 强制把 DevTools 绑在 127.0.0.1，--remote-debugging-address
# 在 148+ 已被完全忽略，容器外（manager 内网 IP / 宿主机端口映射）连不上。
# 解法：Chrome 只监听容器内的 9223，由 socat 在 0.0.0.0:9222 做透明转发。
# 对 manager / 外部客户端仍是 9222，完全无感。
CDP_LISTEN_PORT=9223
CDP_PUBLIC_PORT=9222
if ! command -v socat >/dev/null 2>&1; then
    echo "FATAL: 缺少 socat，无法把 CDP 从 127.0.0.1 转发到容器网卡" >&2
    exit 127
fi

# 去 Google 化：官方 for Testing 包本无广告，这里再关掉后台联网/组件更新/同步等
# 谷歌服务请求；CDP 参数在两种内核下完全一致，外部客户端可直连同一 WS 端口。
CHROME_ARGS=(
    --user-data-dir="$PROFILE_DIR"
    --remote-debugging-port="$CDP_LISTEN_PORT"
    --remote-debugging-address=127.0.0.1
    --remote-allow-origins='*'
    --no-sandbox
    --disable-dev-shm-usage
    --disable-gpu
    --no-first-run
    --no-default-browser-check
    # 隐藏自动化身份：让 navigator.webdriver = false（有头实测本来就是 false，
    # 老无头实测为 true）。这是标准启动参数，不被页面 JS 察觉，无需注入脚本。
    --disable-blink-features=AutomationControlled
    --disable-search-engine-choice-screen
    --disable-default-apps
    --disable-background-networking
    --disable-component-update
    --disable-sync
    --disable-domain-reliability
    --metrics-recording-only
    # Chrome for Testing 启动时会显示 "only for automated testing" 提示条：
    # Chromium 的 infobar_utils.cc 在 --test-type=gpu 时会跳过它；
    # --disable-infobars 对新版已不完全生效，这里作为旧构建的兜底保留。
    --test-type=gpu
    --disable-infobars
    --disable-features=Translate,OptimizationHints,MediaRouter
    --window-size=1920,1080
)

# --no-sandbox 会触发 "unsupported command-line flag" 警告条，Chrome 76+ 起
# 只能用管理策略关闭（--disable-infobars 已失效）。策略在镜像构建时写入。
if [ -f /etc/opt/chrome/policies/managed/cdp_browser_manager.json ]; then
    echo "managed policy: flag security warnings disabled"
fi

if [ "$BROWSER_MODE" = "headless-new" ]; then
    # Chrome 112+ 的新无头：与有头共用同一套渲染实现，无需 Xvfb/VNC。
    # 注意实测 Chrome for Testing 158 的新无头仍报 HeadlessChrome，身份仍需
    # manager 的 CDP 守护伪装（见 app/stealth.py），这里只负责启动方式。
    CHROME_ARGS+=(--headless=new)
fi

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
#   --remote-debugging-port  内部端口 9223，只绑 loopback（新版 Chrome 强制如此）
#   about:blank              启动时打开空白页，不进新标签页推广内容
# 关键：不要把 chrome 输出重定向到文件。之前写进 /tmp/chromium.log 导致
# docker logs / 全局日志看不到任何报错，崩溃时只显示 exit 127 却查不到原因。
# 直接输出到 stdout/stderr，让 manager 的实例日志接口能拿到真实错误。
"$CHROME_BIN" "${CHROME_ARGS[@]}" about:blank &

# 5) CDP 端口转发：0.0.0.0:9222 -> 127.0.0.1:9223。
# 等 Chrome 起一会儿再连，socat 会在上游未就绪时自动重试，不会因短暂失败退出。
socat TCP-LISTEN:"$CDP_PUBLIC_PORT",fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:"$CDP_LISTEN_PORT" &
echo "CDP forward: 0.0.0.0:${CDP_PUBLIC_PORT} -> 127.0.0.1:${CDP_LISTEN_PORT}"

# 任一关键进程退出即结束脚本，借助 restart_policy 让容器自愈重建。
# Debian bookworm 的 bash 5.x 支持 wait -n。
wait -n
