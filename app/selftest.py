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
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})
        raise RuntimeError(f"CDP 命令 {method} 响应超时")

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

        # 6) Page.captureScreenshot：截图
        try:
            shot = client.call("Page.captureScreenshot", {"format": "png"})
            raw = base64.b64decode(shot.get("data", ""))
            record("Page.captureScreenshot 截图", len(raw) > 1000, f"{len(raw)} 字节 PNG")
        except Exception as e:
            record("Page.captureScreenshot 截图", False, str(e))

        # 7) Page.startScreencast：连续抓屏帧
        try:
            client.call(
                "Page.startScreencast", {"format": "jpeg", "quality": 60, "everyNthFrame": 1}
            )
            frames = 0
            deadline = time.time() + 5
            while time.time() < deadline and frames < 2:
                try:
                    msg = json.loads(client.ws.recv())
                except Exception:
                    break
                if msg.get("method") == "Page.screencastFrame":
                    params = msg.get("params", {})
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
