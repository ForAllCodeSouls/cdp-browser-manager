"""官方 CDP WebSocket 自测套件。

所有用例都通过 Chrome DevTools Protocol 的标准 WebSocket 接口完成，不依赖任何
第三方自动化框架。每个用例独立 try/except，单项失败不影响其它用例；结果通过
回调函数逐条输出，便于实时写入 manager 运行日志、在面板里查看。
"""

import base64
import json
import time

import websocket


class CDPClient:
    """一个极简的 CDP WebSocket 客户端：一条连接上顺序发命令、按 id 收响应。"""

    def __init__(self, ws_url: str, timeout: float = 10.0):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._id = 0
        self.timeout = timeout
        # CDP 会在命令响应之外主动推送事件（抓屏帧、网络事件等）。
        # 以前 call() 直接丢弃事件，导致 startScreencast 的响应先到时首帧丢失；
        # 现在统一缓存，测试用例再按需取出。
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        call_id = self._id
        self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                break
            if "method" in msg:
                self.events.append(msg)
                continue
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})
        raise RuntimeError(f"CDP 命令 {method} 响应超时")

    def wait_event(self, method: str, timeout: float = 5.0) -> dict:
        """取出一个已缓存或随后到达的 CDP 事件参数，避免漏掉首帧。"""
        deadline = time.time() + timeout
        while True:
            for idx, event in enumerate(self.events):
                if event.get("method") == method:
                    return self.events.pop(idx).get("params", {})
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError(f"等待 CDP 事件 {method} 超时")
            try:
                msg = json.loads(self.ws.recv())
            except Exception as e:
                raise RuntimeError(f"等待 CDP 事件 {method} 失败: {e}")
            if "method" in msg:
                self.events.append(msg)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _eval(client: CDPClient, expression: str):
    """执行 JS 并按值返回，失败抛错。"""
    res = client.call(
        "Runtime.evaluate", {"expression": expression, "returnByValue": True}
    )
    if "exceptionDetails" in res:
        raise RuntimeError(str(res["exceptionDetails"]))
    return res.get("result", {}).get("value")


def run_all(cdp_base: str, emit) -> dict:
    """在给定实例上跑完整套 CDP WebSocket 用例。

    cdp_base: manager 容器内可达的 CDP 根地址，如 http://172.x.x.x:9222
    emit:     回调，签名为 emit(level, name, detail)，用于实时输出日志
    返回 {"passed": int, "failed": int, "cases": [...]}
    """
    import urllib.request

    cases = []
    passed = failed = 0

    def record(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
        else:
            failed += 1
        cases.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
        emit("PASS" if ok else "FAIL", name, detail)

    # 0) HTTP 探测：/json/version 与 /json/list
    try:
        with urllib.request.urlopen(cdp_base + "/json/version", timeout=8) as r:
            ver = json.loads(r.read().decode("utf-8", "replace"))
        browser = ver.get("Browser", "")
        record("HTTP /json/version", bool(browser), browser)
    except Exception as e:
        record("HTTP /json/version", False, f"{type(e).__name__}: {e}")

    page = None
    try:
        with urllib.request.urlopen(cdp_base + "/json/list", timeout=8) as r:
            targets = json.loads(r.read().decode("utf-8", "replace"))
        pages = [t for t in targets if t.get("type") == "page"]
        page = pages[0] if pages else None
        record("HTTP /json/list 列出页面", bool(page), f"共 {len(pages)} 个页面")
    except Exception as e:
        record("HTTP /json/list 列出页面", False, f"{type(e).__name__}: {e}")

    if not page:
        emit("FAIL", "后续 WebSocket 用例", "没有可用页面目标，跳过")
        return {"passed": passed, "failed": failed + 1, "cases": cases}

    ws_url = cdp_base.replace("http://", "ws://") + "/devtools/page/" + page["id"]
    client = None
    try:
        client = CDPClient(ws_url)
        record("WebSocket 握手连接", True, ws_url.split("/devtools")[0])

        # 1) Runtime.evaluate：JS 计算
        try:
            val = _eval(client, "21*2")
            record("Runtime.evaluate JS 计算", val == 42, f"21*2={val}")
        except Exception as e:
            record("Runtime.evaluate JS 计算", False, str(e))

        # 2) Page.navigate：导航到 data URL
        try:
            client.call("Page.enable")
            client.call("Page.navigate", {"url": "data:text/html,<h1 id=t>CDPTEST</h1><input id=i>"})
            time.sleep(0.6)
            text = _eval(client, "document.getElementById('t').textContent")
            record("Page.navigate 导航并读 DOM", text == "CDPTEST", f"h1={text}")
        except Exception as e:
            record("Page.navigate 导航并读 DOM", False, str(e))

        # 2a) Runtime.evaluate：获取完整页面源码
        try:
            html = _eval(client, "document.documentElement.outerHTML") or ""
            ok = "CDPTEST" in html and "<html" in html.lower()
            record(
                "Runtime.evaluate 获取页面源码",
                ok,
                f"HTML {len(html)} 字符，含测试标记={'CDPTEST' in html}",
            )
        except Exception as e:
            record("Runtime.evaluate 获取页面源码", False, str(e))

        # 2b) DOM.getDocument / querySelector / getOuterHTML：通过 CDP 读取节点源码
        try:
            client.call("DOM.enable")
            root_id = client.call("DOM.getDocument", {"depth": 1}).get("root", {}).get("nodeId")
            node_id = client.call(
                "DOM.querySelector", {"nodeId": root_id, "selector": "#t"}
            ).get("nodeId")
            outer = client.call("DOM.getOuterHTML", {"nodeId": node_id}).get("outerHTML", "")
            record(
                "DOM.getOuterHTML 读取节点源码",
                "CDPTEST" in outer,
                f"{len(outer)} 字符: {outer[:60]}",
            )
        except Exception as e:
            record("DOM.getOuterHTML 读取节点源码", False, str(e))

        # 2c) Page.getResourceTree：读取页面资源树
        try:
            frame = client.call("Page.getResourceTree").get("frameTree", {}).get("frame", {})
            url = frame.get("url", "")
            record("Page.getResourceTree 读取资源树", bool(url), f"frame url={url[:80]}")
        except Exception as e:
            record("Page.getResourceTree 读取资源树", False, str(e))

        # 3) Runtime.evaluate：表单读写
        try:
            _eval(client, "document.getElementById('i').value='abc123'")
            val = _eval(client, "document.getElementById('i').value")
            record("表单输入与读回", val == "abc123", f"value={val}")
        except Exception as e:
            record("表单输入与读回", False, str(e))

        # 4) Input.dispatchMouseEvent：真实鼠标点击
        try:
            _eval(
                client,
                "window.__clicked=false;"
                "var e=document.getElementById('t');"
                "e.onclick=function(){window.__clicked=true};",
            )
            pos = _eval(
                client,
                "(function(){var r=document.getElementById('t').getBoundingClientRect();"
                "return {x:r.x+r.width/2,y:r.y+r.height/2}})()",
            )
            for typ in ("mousePressed", "mouseReleased"):
                client.call(
                    "Input.dispatchMouseEvent",
                    {"type": typ, "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1},
                )
            clicked = _eval(client, "window.__clicked")
            record("Input.dispatchMouseEvent 点击", clicked is True, f"clicked={clicked}")
        except Exception as e:
            record("Input.dispatchMouseEvent 点击", False, str(e))

        # 5) Input.dispatchKeyEvent：真实键盘输入
        try:
            _eval(client, "document.getElementById('i').focus();document.getElementById('i').value=''")
            for ch in "XYZ":
                client.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "key": ch})
                client.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
            val = _eval(client, "document.getElementById('i').value")
            record("Input.dispatchKeyEvent 键盘输入", val == "XYZ", f"value={val}")
        except Exception as e:
            record("Input.dispatchKeyEvent 键盘输入", False, str(e))

        # 5a) Network.setCookie / getCookies：Cookie 写入并读回
        cookie_url = "https://cdp-selftest.invalid/"
        try:
            client.call("Network.enable")
            client.call(
                "Network.setCookie",
                {
                    "name": "cdp_selftest",
                    "value": "cookie-ok",
                    "url": cookie_url,
                    "path": "/",
                    "secure": True,
                },
            )
            cookies = client.call("Network.getCookies", {"urls": [cookie_url]}).get("cookies", [])
            hit = next((c for c in cookies if c.get("name") == "cdp_selftest"), None)
            value = hit.get("value") if hit else None
            record("Network.setCookie/getCookies Cookie 读写", value == "cookie-ok", f"value={value}")
        except Exception as e:
            record("Network.setCookie/getCookies Cookie 读写", False, str(e))

        # 5b) Network.getAllCookies：列出全部 Cookie
        try:
            cookies = client.call("Network.getAllCookies").get("cookies", [])
            hit = next((c for c in cookies if c.get("name") == "cdp_selftest"), None)
            record(
                "Network.getAllCookies 列出 Cookie",
                hit is not None,
                f"共 {len(cookies)} 个 Cookie",
            )
        except Exception as e:
            record("Network.getAllCookies 列出 Cookie", False, str(e))

        # 5c) Network.deleteCookies：清理测试 Cookie，避免污染持久化目录
        try:
            client.call(
                "Network.deleteCookies",
                {"name": "cdp_selftest", "url": cookie_url, "path": "/"},
            )
            left = client.call("Network.getCookies", {"urls": [cookie_url]}).get("cookies", [])
            count = sum(1 for c in left if c.get("name") == "cdp_selftest")
            record("Network.deleteCookies 清理 Cookie", count == 0, f"剩余测试 Cookie {count} 个")
        except Exception as e:
            record("Network.deleteCookies 清理 Cookie", False, str(e))

        # 6) Page.captureScreenshot：截图（同时校验 PNG 文件头）
        try:
            shot = client.call("Page.captureScreenshot", {"format": "png"})
            raw = base64.b64decode(shot.get("data", ""))
            png_ok = raw.startswith(b"\x89PNG\r\n\x1a\n")
            record(
                "Page.captureScreenshot 截图",
                png_ok and len(raw) > 1000,
                f"{len(raw)} 字节，PNG 签名={'OK' if png_ok else '错误'}",
            )
        except Exception as e:
            record("Page.captureScreenshot 截图", False, str(e))

        # 7) Page.startScreencast：连续抓屏帧（用事件缓存，避免漏掉首帧）
        try:
            client.call(
                "Page.startScreencast", {"format": "jpeg", "quality": 60, "everyNthFrame": 1}
            )
            frames = 0
            deadline = time.time() + 5
            while time.time() < deadline and frames < 2:
                try:
                    params = client.wait_event(
                        "Page.screencastFrame", timeout=max(0.1, deadline - time.time())
                    )
                except RuntimeError:
                    if frames:
                        break
                    raise
                sid = params.get("sessionId", "")
                if sid:
                    try:
                        client.call("Page.screencastFrameAck", {"sessionId": sid})
                    except Exception:
                        pass
                frames += 1
            client.call("Page.stopScreencast")
            record("Page.startScreencast 抓屏帧", frames >= 1, f"收到 {frames} 帧")
        except Exception as e:
            record("Page.startScreencast 抓屏帧", False, str(e))

        # 8) Target.createTarget / closeTarget：多标签页管理
        new_tid = None
        try:
            res = client.call("Target.createTarget", {"url": "about:blank"})
            new_tid = res.get("targetId", "")
            record("Target.createTarget 新建标签", bool(new_tid), new_tid)
        except Exception as e:
            record("Target.createTarget 新建标签", False, str(e))
        if new_tid:
            try:
                client.call("Target.closeTarget", {"targetId": new_tid})
                record("Target.closeTarget 关闭标签", True, new_tid)
            except Exception as e:
                record("Target.closeTarget 关闭标签", False, str(e))

        # 9) Runtime.evaluate：用户代理读取
        try:
            ua = _eval(client, "navigator.userAgent")
            record("Runtime.evaluate 读取 UA", bool(ua), str(ua)[:80])
        except Exception as e:
            record("Runtime.evaluate 读取 UA", False, str(e))

    except Exception as e:
        record("WebSocket 会话", False, f"{type(e).__name__}: {e}")
    finally:
        if client is not None:
            client.close()

    return {"passed": passed, "failed": failed, "cases": cases}
