"""浏览器身份伪装守护。

问题：两种无头方式（chrome-headless-shell 与完整 Chrome 的 --headless=new）
都会把自己暴露成 HeadlessChrome——User-Agent、Sec-CH-UA Client Hints 里都带，
网站一眼就能看出是无头浏览器（Chrome for Testing 158 实测两者皆如此）。

解决思路（不注入页面脚本、不改页面 DOM）：
1. 启动参数层：--disable-blink-features=AutomationControlled 让
   navigator.webdriver 为 false（三种内核统一加，见 start.sh）。
2. CDP 层：连一条 browser 级 WebSocket，用 Target.setAutoAttach 自动接管
   之后新建的所有页面；对每个页面用 Emulation.setUserAgentOverride 覆盖
   UA 与 Client Hints。Emulation 域的覆盖是 target 级的，客户端用另一条
   连接访问同一页面也看到覆盖后的身份，这正是我们需要的。

为什么用有头实例的真实 UA 做模板：
   有头 Chrome for Testing 报的是缩减版 "Chrome/MAJOR.0.0.0"，brands 只有
   Not.A/Brand 与 Chromium。照抄它，老无头伪装后与有头完全一致，不会出现
   "UA 说 Chrome、brands 却说 HeadlessChrome" 的自相矛盾。

版本动态读取：从 /json/version 的 Browser 字符串解析主版本号，Chrome 升级
（脚本 / Dockerfile 改 CHROME_VERSION）后无需改本文件。

生命周期：每个实例一个守护线程，随实例创建启动、随实例停止退出；容器重启
导致 CDP 断连时自动重连。失败只记日志，绝不影响浏览器本身与面板功能。
"""

import json
import re
import threading
import time
import urllib.request

import websocket


# 真实有头 Chrome for Testing 的 UA 形态：缩减版 MAJOR.0.0.0，不带完整补丁号。
# 与线上实测值一致：Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36
#   (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36
UA_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)


def browser_major(cdp_base: str) -> str:
    """从 CDP /json/version 读取主版本号，如 HeadlessChrome/158.0.8127.88 -> 158。"""
    with urllib.request.urlopen(cdp_base.rstrip("/") + "/json/version", timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    browser = str(data.get("Browser", ""))
    match = re.search(r"Chrome/(\d+)", browser)
    if not match:
        raise RuntimeError(f"无法从 Browser 字段解析版本: {browser!r}")
    return match.group(1)


class StealthGuard:
    """给单个浏览器实例的所有页面统一覆盖身份的后台守护。"""

    def __init__(self, sid: str, cdp_base: str, log):
        self.sid = sid
        self.cdp_base = cdp_base.rstrip("/")
        self.log = log
        self._stop = threading.Event()
        self._thread = None
        # CDP 命令响应与事件混在同一条 WebSocket 上，命令等待期间收到的事件
        # 必须缓存下来，否则会漏掉新标签的 attachedToTarget（实测确实会漏）。
        self._events: list[dict] = []
        # 已打过补丁的页面 target，避免重复 attach（实测重复 attach 会让
        # userAgentData.brands 变空，且无谓占用 session）。
        self._patched: set[str] = set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"stealth-{self.sid[:8]}"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---------- 内部实现 ----------

    def _ws_url(self) -> str:
        """取 browser 级 WebSocket 地址（Target.autoAttach 只能在 browser 会话上开）。"""
        with urllib.request.urlopen(self.cdp_base + "/json/version", timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        url = str(data.get("webSocketDebuggerUrl", ""))
        if not url:
            raise RuntimeError("/json/version 未返回 webSocketDebuggerUrl")
        # 容器内 CDP 绑在 127.0.0.1:9223，socat 转发到 0.0.0.0:9222；
        # webSocketDebuggerUrl 里可能是 127.0.0.1，换成实际可访问的地址。
        host = self.cdp_base.replace("http://", "").replace("https://", "")
        parsed = re.match(r"ws://[^/]+(/.*)$", url)
        if parsed:
            return "ws://" + host + parsed.group(1)
        return url

    def _run(self):
        self.log.info("[身份伪装] 守护启动 sid=%s", self.sid[:8])
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            # 每次（重）连接都是全新的 CDP 会话，之前的 session 已失效，清空去重集合。
            self._patched = set()
            self._events = []
            try:
                major = browser_major(self.cdp_base)
                ua = UA_TEMPLATE.format(major=major)
                ws = websocket.create_connection(
                    self._ws_url(), timeout=10, suppress_origin=True
                )
                # flatten=true 时事件里直接带 sessionId，命令也带 sessionId 即可
                # 下发到对应页面，无需自己维护多级 target 会话。
                self._call(ws, "Target.setAutoAttach", {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": False,
                    "flatten": True,
                })
                backoff = 1.0
                self.log.info(
                    "[身份伪装] 已接管浏览器 sid=%s Chrome %s，UA=%s",
                    self.sid[:8], major, ua,
                )
                # 给已存在的页面补一次覆盖（autoAttach 只覆盖之后新建的）。
                self._patch_existing(ws, ua, major)
                # 长连接读事件；新标签出现时立即打补丁。
                while not self._stop.is_set():
                    # 先处理命令等待期间缓存下来的事件（如 setAutoAttach 时的现有页面）。
                    pending = self._events
                    self._events = []
                    for msg in pending:
                        self._handle_event(ws, msg, ua, major)
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        # CDP 在无事件时不会主动推送，10 秒读超时是预期的空闲，
                        # 不是断连。若当断连处理，就会每 11 秒重建一次会话、
                        # 清空 _patched 并对所有页面重新 attach，既刷屏又会
                        # 触发重复 attach 导致 Client Hints brands 变空。
                        continue
                    except Exception:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("method"):
                        self._handle_event(ws, msg, ua, major)
            except Exception as e:
                if not self._stop.is_set():
                    self.log.warning(
                        "[身份伪装] sid=%s 临时失败，%s 秒后重试: %s",
                        self.sid[:8], backoff, e,
                    )
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)
        self.log.info("[身份伪装] 守护退出 sid=%s", self.sid[:8])

    def _handle_event(self, ws, msg: dict, ua: str, major: str):
        """处理 browser 会话推来的事件：新页面 attach 时立即覆盖身份。"""
        if msg.get("method") != "Target.attachedToTarget":
            return
        params = msg.get("params", {})
        info = params.get("targetInfo", {})
        if info.get("type") != "page":
            return
        tid = str(info.get("targetId", ""))
        if tid and tid in self._patched:
            return
        try:
            self._apply(ws, params.get("sessionId", ""), ua, major)
            if tid:
                self._patched.add(tid)
        except Exception as e:
            self.log.debug("[身份伪装] 新页面打补丁失败: %s", e)

    def _patch_existing(self, ws, ua: str, major: str):
        # 关键：覆盖是绑在 attach 出来的 session 上的——一旦 detach，覆盖立刻撤销。
        # 所以对已有页面只 attach、不 detach，让 session 随守护长连接一直存活。
        try:
            with urllib.request.urlopen(
                self.cdp_base + "/json/list", timeout=5
            ) as resp:
                targets = json.loads(resp.read().decode("utf-8", "replace"))
            pages = [t for t in targets if t.get("type") == "page"]
            if not pages:
                return
            for page in pages:
                tid = page.get("id", "")
                if not tid or tid in self._patched:
                    continue
                try:
                    res = self._call(ws, "Target.attachToTarget", {
                        "targetId": tid, "flatten": True,
                    })
                    session_id = res.get("sessionId", "")
                    if session_id:
                        self._apply(ws, session_id, ua, major)
                        self._patched.add(tid)
                except Exception as e:
                    self.log.debug("[身份伪装] 补打页面 %s 失败: %s", tid[:8], e)
        except Exception as e:
            self.log.debug("[身份伪装] 列举已有页面失败: %s", e)

    def _apply(self, ws, session_id: str, ua: str, major: str):
        """对一个页面会话下发身份覆盖。任何单项失败都不抛出，避免中断守护循环。"""
        if not session_id:
            return
        metadata = {
            "brands": [
                {"brand": "Not.A/Brand", "version": "24"},
                {"brand": "Chromium", "version": str(major)},
            ],
            "fullVersion": f"{major}.0.0.0",
            "platform": "Linux",
            "platformVersion": "",
            "architecture": "x86_64",
            "model": "",
            "mobile": False,
        }
        try:
            self._call(ws, "Emulation.setUserAgentOverride", {
                "userAgent": ua,
                "platform": "Linux",
                "userAgentMetadata": metadata,
            }, session_id=session_id)
            self.log.info("[身份伪装] 页面已覆盖身份 sid=%s", self.sid[:8])
        except Exception as e:
            self.log.warning("[身份伪装] 覆盖页面身份失败: %s", e)

    _next_id = 0
    _id_lock = threading.Lock()

    @classmethod
    def _new_id(cls) -> int:
        with cls._id_lock:
            cls._next_id += 1
            return cls._next_id

    def _call(self, ws, method: str, params: dict, session_id: str = "") -> dict:
        """在 browser 级 WebSocket 上发命令并等响应；session_id 存在则转发到页面。

        等待响应期间收到的 CDP 事件先入 self._events，由主循环消费，绝不丢弃。
        """
        call_id = self._new_id()
        frame = {"id": call_id, "method": method, "params": params or {}}
        if session_id:
            frame["sessionId"] = session_id
        ws.send(json.dumps(frame))
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                # 同样把空闲读超时视为正常等待命令响应，不要炸到外层
                # 触发无谓重连（重连会撤销已下发的身份覆盖）。
                continue
            if msg.get("method"):
                self._events.append(msg)
                continue
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})
        raise RuntimeError(f"CDP 命令 {method} 响应超时")
