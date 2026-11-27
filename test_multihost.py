"""多主机聚合代理与 API_KEY 鉴权的离线自测。

本地直接运行（无需 Docker、不操作任何浏览器实例）：
    python test_multihost.py

安装了 fastapi + httpx 的环境（CI）会自动追加集成测试：
    pip install -r requirements.txt httpx
    python test_multihost.py
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 是否在导入 main 前就判断测试客户端可用性：mock 注入后 fastapi 会变成假模块。
try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient
    HAS_TESTCLIENT = True
except Exception:
    HAS_TESTCLIENT = False

if not HAS_TESTCLIENT:
    # 纯逻辑测试：用假模块顶替第三方依赖，只为加载 main.py 里的函数定义。
    for name in ("docker", "fastapi", "starlette", "jinja2", "websockets", "websocket"):
        sys.modules.setdefault(name, MagicMock())
    sys.modules.setdefault(
        "fastapi.responses", MagicMock()
    )
    sys.modules.setdefault("fastapi.templating", MagicMock())
    sys.modules.setdefault("starlette.concurrency", MagicMock())

# 模块导入会启动两个后台线程；测试里屏蔽，避免碰 Docker / 拉镜像。
_real_thread = threading.Thread
threading.Thread = lambda *a, **kw: SimpleNamespace(start=lambda: None)
try:
    import app.main as main
finally:
    threading.Thread = _real_thread


class FakeRequest:
    """只提供 external_base() 需要的属性。"""
    headers = {"host": "panel.example.com", "x-forwarded-proto": "https"}
    url = SimpleNamespace(scheme="http")
    client = SimpleNamespace(host="127.0.0.1")


class ParseHostsTests(unittest.TestCase):
    def test_json_format(self):
        hosts = main.parse_remote_hosts(json.dumps([
            {"id": "b", "name": "主机B", "base": "https://b.example.com/", "key": "k"},
        ]))
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["base"], "https://b.example.com")
        self.assertEqual(hosts[0]["key"], "k")

    def test_simple_format(self):
        hosts = main.parse_remote_hosts("b|主机B|https://b.example.com|k;c|C|http://127.0.0.1:8000")
        self.assertEqual([h["id"] for h in hosts], ["b", "c"])
        self.assertEqual(hosts[1]["key"], "")

    def test_rejects_unsafe_id_and_local(self):
        hosts = main.parse_remote_hosts(json.dumps([
            {"id": "../evil", "base": "https://x"},
            {"id": "local", "base": "https://x"},
            {"id": "ok", "base": "ftp://x"},
        ]))
        self.assertEqual(hosts, [])

    def test_empty(self):
        self.assertEqual(main.parse_remote_hosts(""), [])


class WebSocketSubprotocolTests(unittest.TestCase):
    def test_parse_binary(self):
        self.assertEqual(main.ws_requested_subprotocols({"sec-websocket-protocol": "binary"}), ["binary"])

    def test_parse_multiple(self):
        self.assertEqual(
            main.ws_requested_subprotocols({"sec-websocket-protocol": " binary , v1 "}),
            ["binary", "v1"],
        )

    def test_parse_missing(self):
        self.assertEqual(main.ws_requested_subprotocols({}), [])
        self.assertEqual(main.ws_requested_subprotocols(None), [])

    def test_novnc_selects_binary(self):
        # noVNC 请求 binary，代理必须回显它，否则 VNC 卡在 Connecting。
        requested = main.ws_requested_subprotocols({"sec-websocket-protocol": "binary"})
        subprotocol = "binary" if "binary" in requested else (requested[0] if requested else None)
        self.assertEqual(subprotocol, "binary")

    def test_cdp_selects_none(self):
        requested = main.ws_requested_subprotocols({})
        subprotocol = "binary" if "binary" in requested else (requested[0] if requested else None)
        self.assertIsNone(subprotocol)


class PortProxyRouteTests(unittest.TestCase):
    """端口代理必须用 {port:int}，否则 /instance/{sid}/stop 会被抢先匹配成 422。"""

    def test_all_port_proxy_routes_use_int_converter(self):
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        # 逐一检查四类端口代理路由声明，路径参数都应是 {port:int}。
        self.assertIn('"/{port:int}/json/{rest:path}"', src)
        self.assertIn('"/{port:int}/devtools/{rest:path}"', src)
        self.assertIn('"/{port:int}/{rest:path}"', src)
        self.assertIn('"/{port:int}/websockify"', src)
        # 不应再有旧的裸 {port} 路由声明（函数签名里的 port: int 不算）。
        self.assertNotIn('"/{port}/json/', src)
        self.assertNotIn('"/{port}/devtools/', src)
        self.assertNotIn('"/{port}/{rest:path}"', src)
        self.assertNotIn('"/{port}/websockify"', src)


class AddressBarJsTests(unittest.TestCase):
    """前端地址栏实时更新的关键 JS 片段必须存在，防回归。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_onmessage_handles_url(self):
        self.assertIn('msg.type === "url"', self.src)
        self.assertIn('box.value = msg.url', self.src)

    def test_focused_input_not_overwritten(self):
        self.assertIn('document.activeElement === box', self.src)

    def test_sig_includes_url(self):
        # castSig 里必须把 t.url 纳入签名，否则标题没变时地址栏不刷新。
        i = self.src.index("function castSig")
        seg = self.src[i:i + 500]
        self.assertIn("t.url", seg)

    def test_poll_interval_shortened(self):
        self.assertIn("pollCastTargets(); }, 2500)", self.src)


class QualityToggleJsTests(unittest.TestCase):
    """画质切换是详情栏的全局快捷按钮，不在设置弹窗里。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_no_quality_dropdown_in_settings(self):
        # 设置弹窗里不应再有画质下拉（id=setQuality）。
        self.assertNotIn('id="setQuality"', self.src)
        self.assertNotIn('$("setQuality")', self.src)

    def test_detail_has_global_quality_seg(self):
        self.assertIn("qseg", self.src)
        self.assertIn("QUALITY_LABELS", self.src)
        # 三档都渲染成按钮，点击调用全局 setQuality。
        self.assertIn("['low', 'medium', 'high'].map", self.src)
        self.assertIn("setQuality(\\'' + k + '\\')", self.src)

    def test_set_quality_persists_and_reconnects(self):
        i = self.src.index("function setQuality")
        seg = self.src[i:i + 500]
        self.assertIn("localStorage.setItem(SETTINGS_KEY", seg)
        self.assertIn("resetFrameState()", seg)
        self.assertIn("render()", seg)

    def test_quality_hint_rendered_after_seg(self):
        # 详情栏在切换按钮后有按「模式+画质」给出的建议文字。
        self.assertIn("qualityHint(mode, settings.quality)", self.src)
        self.assertIn('class="qhint"', self.src)
        i = self.src.index("function qualityHint")
        seg = self.src[i:i + 700]
        for q in ("low", "medium", "high"):
            self.assertIn(q + ":", seg)
        self.assertIn("cast:", seg)
        self.assertIn("vnc:", seg)

    def test_reset_frame_state_keeps_tid_by_default(self):
        # 切画质/模式/保存设置重连时保留当前标签，只有新建/停止才显式清空。
        i = self.src.index("function resetFrameState")
        seg = self.src[i:i + 300]
        self.assertIn("clearTid = false", seg)
        self.assertIn("if (clearTid) castPreferredTid", seg)
        # 三处销毁实例的场景传 true
        self.assertEqual(self.src.count("resetFrameState(true)"), 3)


class NavigationUrlTests(unittest.TestCase):
    """CDP 导航事件提取主 frame URL，过滤 iframe 噪声。"""

    def test_frame_navigated_main(self):
        state = {}
        url = main.navigation_url_from_cdp({
            "method": "Page.frameNavigated",
            "params": {"frame": {"id": "F1", "url": "https://example.com/a"}},
        }, state)
        self.assertEqual(url, "https://example.com/a")
        self.assertEqual(state["id"], "F1")

    def test_frame_navigated_iframe_ignored(self):
        state = {"id": "F1"}
        url = main.navigation_url_from_cdp({
            "method": "Page.frameNavigated",
            "params": {"frame": {"id": "F2", "parentId": "F1", "url": "https://ads.com/x"}},
        }, state)
        self.assertEqual(url, "")
        self.assertEqual(state["id"], "F1")

    def test_within_document_main(self):
        state = {"id": "F1"}
        url = main.navigation_url_from_cdp({
            "method": "Page.navigatedWithinDocument",
            "params": {"frameId": "F1", "url": "https://example.com/a#hash"},
        }, state)
        self.assertEqual(url, "https://example.com/a#hash")

    def test_within_document_iframe_ignored(self):
        state = {"id": "F1"}
        url = main.navigation_url_from_cdp({
            "method": "Page.navigatedWithinDocument",
            "params": {"frameId": "F2", "url": "https://ads.com/x"},
        }, state)
        self.assertEqual(url, "")

    def test_unknown_event(self):
        self.assertEqual(main.navigation_url_from_cdp({"method": "Page.loadEventFired"}, {}), "")

    def test_empty_url(self):
        url = main.navigation_url_from_cdp({
            "method": "Page.frameNavigated", "params": {"frame": {"id": "F9", "url": ""}},
        }, {})
        self.assertEqual(url, "")


class BinaryFrameForwardingTests(unittest.TestCase):
    """RFB/二进制帧转发必须用 BINARY opcode，否则 websockify 拒绝并关闭连接。"""

    def test_novnc_proxy_uses_binary_opcode(self):
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        # 在 novnc_ws_proxy 函数体内断言二进制分支显式指定了 OPCODE_BINARY。
        i = src.index("async def novnc_ws_proxy")
        seg = src[i:i + 4200]
        self.assertIn("ws_client.ABNF.OPCODE_BINARY", seg)
        self.assertIn("if is_binary", seg)

    def test_remote_proxy_uses_binary_opcode(self):
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("async def remote_ws_proxy")
        seg = src[i:i + 3200]
        self.assertIn("ws_client.ABNF.OPCODE_BINARY", seg)

    def test_cdp_devtools_ws_forwards_by_frame_type(self):
        # CDP 透传的双向都必须按帧类型处理：上游二进制响应按 bytes 转发，
        # 客户端二进制命令用 BINARY opcode 上送，文本帧保持默认 TEXT。
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("async def cdp_devtools_ws")
        seg = src[i:i + 3000]
        self.assertIn("send_bytes", seg)
        self.assertIn("ws_client.ABNF.OPCODE_BINARY", seg)
        self.assertNotIn("receive_text()", seg)

    def test_remote_query_uses_imported_urlencode(self):
        # 防回归：不用 urllib.parse.xxx 的隐式属性访问（依赖父包已加载，脆弱）。
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("urllib.parse.urlencode", src)

    def test_keepalive_caps_pending_shots(self):
        # 补帧在途请求设上限，页面卡死时不会无限堆积 captureScreenshot。
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("def request_shot()")
        seg = src[i:i + 900]
        self.assertIn("len(pending_shots) >= 2", seg)
        self.assertIn("return False", seg)
        # 只有真的发出才重置补帧计时
        self.assertIn("if request_shot():", src)


class CastQualityProfileTests(unittest.TestCase):
    """抓屏画质三档：未知档位回落均衡，参数不越界。"""

    def test_three_profiles(self):
        low = main.cast_quality_profile("low")
        medium = main.cast_quality_profile("medium")
        high = main.cast_quality_profile("high")
        # 省流档分辨率/码率最小，靠缩图省带宽，不再隔帧
        self.assertLess(low["max_width"], medium["max_width"])
        self.assertLess(medium["max_width"], high["max_width"])
        self.assertLess(low["quality"], medium["quality"])
        self.assertLessEqual(medium["quality"], high["quality"])

    def test_no_profile_drops_frames(self):
        # 三档帧率必须一致：省流只降清晰度，不隔帧，避免画面慢半拍。
        for name in ("low", "medium", "high"):
            self.assertEqual(main.cast_quality_profile(name)["every_nth"], 1, name)

    def test_unknown_falls_back_to_medium(self):
        self.assertEqual(main.cast_quality_profile("bogus"), main.cast_quality_profile("medium"))
        self.assertEqual(main.cast_quality_profile(""), main.cast_quality_profile("medium"))
        self.assertEqual(main.cast_quality_profile(None), main.cast_quality_profile("medium"))

    def test_case_insensitive(self):
        self.assertEqual(main.cast_quality_profile("LOW"), main.cast_quality_profile("low"))

    def test_returns_copy_not_shared(self):
        a = main.cast_quality_profile("low")
        a["quality"] = 999
        self.assertNotEqual(main.cast_quality_profile("low")["quality"], 999)


class AuthExemptTests(unittest.TestCase):
    def test_exempt_paths(self):
        for path in ("/", "/api/health", "/7901/vnc_lite.html",
                     "/7901/app.js", "/7901/core/rfb.js"):
            self.assertTrue(main.auth_exempt(path, "GET"), path)

    def test_hosts_list_get_exempt_but_writes_protected(self):
        # 面板添加/删除远程主机是写操作，设置 API_KEY 后绝不能免鉴权。
        self.assertTrue(main.auth_exempt("/api/hosts", "GET"))
        self.assertFalse(main.auth_exempt("/api/hosts", "POST"))
        self.assertFalse(main.auth_exempt("/api/hosts", "DELETE"))

    def test_protected_paths(self):
        for path, method in (("/api/list", "GET"), ("/instance/new", "POST"),
                             ("/9222/json/version", "GET"), ("/9222/json/list", "GET"),
                             ("/7901/websockify", "GET")):
            self.assertFalse(main.auth_exempt(path, method), path)

    def test_key_ok(self):
        with patch.object(main, "API_KEY", ""):
            self.assertTrue(main.key_ok(""))
        with patch.object(main, "API_KEY", "secret"):
            self.assertTrue(main.key_ok("secret"))
            self.assertFalse(main.key_ok(""))
            self.assertFalse(main.key_ok("wrong"))


class LinkTokenKeyTests(unittest.TestCase):
    """面板生成的接入令牌必须能当 API_KEY 用（远端主机拿令牌调用本机）。"""

    def test_token_accepted(self):
        with patch.object(main, "API_KEY", "secret"), \
                patch.object(main, "LINK_TOKENS", {"tok-abc": {"created": 1}}):
            self.assertTrue(main.key_ok("secret"))
            self.assertTrue(main.key_ok("tok-abc"))
            self.assertFalse(main.key_ok("wrong"))
            self.assertFalse(main.key_ok(""))

    def test_open_when_no_api_key(self):
        with patch.object(main, "API_KEY", ""), \
                patch.object(main, "LINK_TOKENS", {"tok-abc": {"created": 1}}):
            self.assertTrue(main.key_ok(""))


class HostStoreTests(unittest.TestCase):
    """面板添加的远程主机 / 接入令牌的持久化与合并逻辑。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cdpmgr-test-")
        self.patches = [
            patch.object(main, "UI_HOSTS_FILE", Path(self.tmp) / "remote_hosts.json"),
            patch.object(main, "LINK_TOKENS_FILE", Path(self.tmp) / "link_tokens.json"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_valid_host_entry(self):
        entry = main.valid_host_entry({"id": "b", "name": "主机B", "base": "https://b.example.com/", "key": "k"})
        self.assertEqual(entry["base"], "https://b.example.com")
        self.assertEqual(entry["key"], "k")
        for bad in ({"id": "../evil", "base": "https://x"},
                    {"id": "local", "base": "https://x"},
                    {"id": "ok", "base": "ftp://x"}):
            with self.assertRaises(ValueError):
                main.valid_host_entry(bad)

    def test_derive_host_id_sanitizes_and_dedupes(self):
        with patch.object(main, "REMOTE_HOSTS", [{"id": "127-0-0-1", "name": "x", "base": "http://x"}]):
            self.assertEqual(main._derive_host_id("http://127.0.0.1:8000"), "127-0-0-1-2")
            self.assertEqual(main._derive_host_id("https://b.example.com"), "b-example-com")

    def test_ui_hosts_roundtrip(self):
        main.UI_REMOTE_HOSTS[:] = [main.valid_host_entry(
            {"id": "b", "name": "主机B", "base": "https://b.example.com", "key": "k"})]
        self.assertTrue(main.save_ui_hosts())
        main.UI_REMOTE_HOSTS[:] = []
        main.load_ui_hosts()
        self.assertEqual(len(main.UI_REMOTE_HOSTS), 1)
        self.assertEqual(main.UI_REMOTE_HOSTS[0]["id"], "b")
        main.UI_REMOTE_HOSTS[:] = []

    def test_reload_hosts_merges_ui_over_env(self):
        env = [{"id": "b", "name": "envB", "base": "https://env-b", "key": "k1"}]
        ui = [{"id": "b", "name": "uiB", "base": "https://ui-b", "key": "k2"},
              {"id": "c", "name": "C", "base": "https://c.example.com", "key": ""}]
        with patch.object(main, "REMOTE_HOSTS_ENV", env), \
                patch.object(main, "UI_REMOTE_HOSTS", ui), \
                patch.object(main, "REMOTE_HOSTS", []):
            main.reload_hosts()
            ids = {h["id"]: h for h in main.REMOTE_HOSTS}
            self.assertEqual(set(ids), {"b", "c"})
            # 同 id 时面板配置覆盖环境变量
            self.assertEqual(ids["b"]["base"], "https://ui-b")

    def test_link_tokens_roundtrip(self):
        main.LINK_TOKENS.clear()
        main.LINK_TOKENS["tok-1"] = {"created": 123}
        self.assertTrue(main.save_link_tokens())
        main.LINK_TOKENS.clear()
        main.load_link_tokens()
        self.assertIn("tok-1", main.LINK_TOKENS)
        self.assertEqual(main.LINK_TOKENS["tok-1"]["created"], 123)
        main.LINK_TOKENS.clear()


class AddressRewriteTests(unittest.TestCase):
    def test_remote_view_uses_local_proxy(self):
        host = {"id": "B", "name": "主机B", "base": "https://b.example.com"}
        item = {
            "sid": "sid1", "debug_port": 9222, "vnc_port": 5901, "novnc_port": 7901,
            "cdp_http": "http://b.example.com:9222",
        }
        view = main.remote_view(host, item, FakeRequest())
        self.assertEqual(view["host"], "B")
        self.assertEqual(view["cdp_http_proxy"], "https://panel.example.com/remote/B/9222")
        self.assertEqual(view["novnc_url_proxy"], "https://panel.example.com/remote/B/7901")
        self.assertEqual(view["cast_ws"], "/remote/B/ws/cast/sid1/{tid}")
        # 旧字段保持不变，老项目读取方式不受影响
        self.assertEqual(view["cdp_http"], "http://b.example.com:9222")

    def test_cdp_ws_rewrite_with_prefix(self):
        raw = '{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/abc"}'
        out = main.rewrite_cdp_ws_urls(raw, FakeRequest(), 9222, prefix="/remote/B")
        self.assertIn("wss://panel.example.com/remote/B/9222/devtools/browser/abc", out)

        out_local = main.rewrite_cdp_ws_urls(raw, FakeRequest(), 9222)
        self.assertIn("wss://panel.example.com/9222/devtools/browser/abc", out_local)


@unittest.skipUnless(HAS_TESTCLIENT, "需要 fastapi + httpx，CI 环境自动运行")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class RemoteHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authed(self):
                if self.headers.get("X-API-Key") != "remote-secret":
                    self._send(401, {"detail": "bad key"})
                    return False
                return True

            def do_GET(self):
                if not self._authed():
                    return
                if self.path.startswith("/api/list"):
                    self._send(200, {
                        "instances": {
                            "remote-sid": {
                                "sid": "remote-sid", "status": "running",
                                "debug_port": 9222, "novnc_port": 7901,
                                "cdp_http": "http://b.example.com:9222",
                            }
                        },
                        "profiles": [],
                    })
                elif self.path.startswith("/9222/json/version"):
                    self._send(200, {
                        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"
                    })
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self):
                if not self._authed():
                    return
                if self.path.startswith("/instance/new"):
                    self._send(200, {"sid": "new-sid", "status": "running", "debug_port": 9224})
                else:
                    self._send(404, {"detail": "not found"})

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RemoteHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.hosts = [{"id": "B", "name": "主机B", "base": cls.base, "key": "remote-secret"}]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        main.sync_instances = lambda: None
        main.list_profiles = lambda: []
        main.INSTANCES.clear()
        self.tmp = tempfile.mkdtemp(prefix="cdpmgr-it-")
        self.patches = [
            patch.object(main, "API_KEY", ""),
            # 每个测试用独立的 REMOTE_HOSTS 副本：添加主机会原地改写列表，避免污染类状态
            patch.object(main, "REMOTE_HOSTS", list(self.hosts)),
            patch.object(main, "UI_REMOTE_HOSTS", []),
            patch.object(main, "DATA_DIR", Path(self.tmp)),
            patch.object(main, "UI_HOSTS_FILE", Path(self.tmp) / "remote_hosts.json"),
            patch.object(main, "LINK_TOKENS_FILE", Path(self.tmp) / "link_tokens.json"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_hosts_and_aggregated_list(self):
        client = TestClient(main.app)
        r = client.get("/api/hosts")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["auth_required"])
        self.assertEqual(len(r.json()["hosts"]), 2)

        r = client.get("/api/list")
        self.assertEqual(r.status_code, 200)
        view = r.json()["instances"]["remote-sid"]
        self.assertEqual(view["host"], "B")
        self.assertIn("/remote/B/9222", view["cdp_http_proxy"])

    def test_http_proxy_rewrites_cdp_ws(self):
        client = TestClient(main.app)
        r = client.get("/remote/B/9222/json/version")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/remote/B/9222/devtools/", r.json()["webSocketDebuggerUrl"])

    def test_remote_create(self):
        client = TestClient(main.app)
        r = client.post("/instance/new", json={"host": "B", "browser_mode": "headless"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sid"], "new-sid")
        self.assertIn("/remote/B/", r.json()["cdp_http_proxy"])

    def test_stop_route_not_shadowed_by_port_proxy(self):
        """回归：/instance/{sid}/stop 不能再被 /{port}/{rest} 抢走返回 422。"""
        client = TestClient(main.app)
        calls = []
        with patch.object(main, "stop_instance", side_effect=lambda sid: calls.append(sid)):
            for path in ("/instance/abc/stop", "/api/instance/abc/stop", "/instance/stop-all"):
                r = client.post(path)
                self.assertEqual(r.status_code, 200, "%s -> %s" % (path, r.text))
        self.assertIn("abc", calls)

    def test_api_key_guard(self):
        with patch.object(main, "API_KEY", "local-secret"):
            client = TestClient(main.app)
            self.assertEqual(client.get("/api/list").status_code, 401)
            self.assertEqual(
                client.get("/api/list", headers={"X-API-Key": "local-secret"}).status_code, 200
            )
            info = client.get("/api/hosts").json()
            self.assertTrue(info["auth_required"])
            self.assertTrue(info["needs_key"])
            self.assertEqual(info["hosts"], [])
            # noVNC 静态资源免鉴权（返回 404 说明已穿过中间件，未被 401 拦截）
            self.assertNotEqual(client.get("/7901/vnc_lite.html").status_code, 401)

    def test_panel_add_and_delete_host_via_link(self):
        """面板粘贴连接串（地址|令牌）添加远程主机，再从界面删除。"""
        client = TestClient(main.app)
        r = client.post("/api/hosts", json={"link": self.base + "|remote-secret"})
        self.assertEqual(r.status_code, 200, r.text)
        hid = r.json()["host"]["id"]
        self.assertTrue(hid)
        hosts = {h["id"]: h for h in client.get("/api/hosts").json()["hosts"]}
        self.assertIn(hid, hosts)
        self.assertTrue(hosts[hid]["removable"])
        # 添加后立即可聚合出远端实例（等价于环境变量配置）
        r = client.get("/api/list")
        self.assertEqual(r.status_code, 200)
        self.assertIn("remote-sid", r.json()["instances"])
        # 删除只移除接入关系
        r = client.delete("/api/hosts/" + hid)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(hid, [h["id"] for h in client.get("/api/hosts").json()["hosts"]])

    def test_panel_add_host_bad_key_rejected(self):
        client = TestClient(main.app)
        r = client.post("/api/hosts", json={"link": self.base + "|wrong-key"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("令牌", r.json()["detail"])

    def test_panel_add_host_bad_link_format(self):
        client = TestClient(main.app)
        r = client.post("/api/hosts", json={"link": "no-pipe-here"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("连接串", r.json()["detail"])

    def test_panel_add_host_unreachable(self):
        client = TestClient(main.app)
        r = client.post("/api/hosts", json={"link": "http://127.0.0.1:1|k"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("无法连接", r.json()["detail"])

    def test_panel_add_host_requires_key_when_api_key_set(self):
        with patch.object(main, "API_KEY", "local-secret"):
            client = TestClient(main.app)
            r = client.post("/api/hosts", json={"link": self.base + "|remote-secret"})
            self.assertEqual(r.status_code, 401)
            r = client.delete("/api/hosts/b")
            self.assertEqual(r.status_code, 401)

    def test_link_token_generate_list_revoke(self):
        client = TestClient(main.app)
        r = client.post("/api/link/token")
        self.assertEqual(r.status_code, 200, r.text)
        tok = r.json()["token"]
        self.assertEqual(len(tok), 48)
        # 令牌能通过本机鉴权（模拟远端面板拿令牌调用本机）
        self.assertTrue(main.key_ok(tok))
        listed = client.get("/api/link/token").json()["tokens"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["token"], tok)
        # 吊销后立即失效
        self.assertEqual(client.delete("/api/link/token?token=" + tok).status_code, 200)
        self.assertEqual(client.get("/api/link/token").json()["tokens"], [])
        with patch.object(main, "API_KEY", "secret"):
            self.assertFalse(main.key_ok(tok))
        # 吊销不存在的令牌返回 404
        self.assertEqual(client.delete("/api/link/token?token=" + tok).status_code, 404)

    def test_websocket_key_guard(self):
        from starlette.websockets import WebSocketDisconnect

        with patch.object(main, "API_KEY", "local-secret"):
            client = TestClient(main.app)
            with self.assertRaises(WebSocketDisconnect) as ctx:
                with client.websocket_connect("/ws/cast/abc/def"):
                    pass
            self.assertEqual(ctx.exception.code, 4401)

    def test_websocket_subprotocol_echoed(self):
        """回归测试：noVNC 的 binary 子协议必须被代理回显，否则 VNC 停在 Connecting。"""
        # 造一个最小 WS 应用，复用真实的 accept 逻辑验证 TestClient 能看到回显。
        from fastapi import WebSocket
        from fastapi.testclient import TestClient as TC
        from starlette.applications import Starlette
        from starlette.routing import WebSocketRoute

        async def echo(ws: WebSocket):
            requested = main.ws_requested_subprotocols(ws.headers)
            subprotocol = "binary" if "binary" in requested else (requested[0] if requested else None)
            await ws.accept(subprotocol=subprotocol)
            await ws.send_text("up")
            await ws.close()

        app = Starlette(routes=[WebSocketRoute("/7901/websockify", echo)])
        with TC(app) as client:
            with client.websocket_connect(
                "/7901/websockify", subprotocols=["binary"]
            ) as ws:
                self.assertEqual(ws.accepted_subprotocol, "binary")
                self.assertEqual(ws.receive_text(), "up")


class RemotePanelJsTests(unittest.TestCase):
    """「远程」接入面板的关键 JS 片段，防回归。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_modal_and_toolbar_button_exist(self):
        self.assertIn('id="remoteMask"', self.src)
        self.assertIn('id="btnRemote"', self.src)
        self.assertIn("function openRemote()", self.src)

    def test_add_remote_host_posts_link(self):
        i = self.src.index("async function addRemoteHost")
        seg = self.src[i:i + 800]
        self.assertIn('"/api/hosts"', seg)
        self.assertIn('"POST"', seg)
        self.assertIn("{ link: link }", seg)

    def test_delete_remote_host_calls_delete_api(self):
        i = self.src.index("async function delRemoteHost")
        seg = self.src[i:i + 700]
        self.assertIn("/api/hosts/", seg)
        self.assertIn('"DELETE"', seg)

    def test_link_token_generate_and_revoke(self):
        i = self.src.index("async function genLinkToken")
        self.assertIn('"/api/link/token"', self.src[i:i + 600])
        i = self.src.index("async function revokeLinkToken")
        seg = self.src[i:i + 700]
        self.assertIn('"/api/link/token"', seg)
        self.assertIn('"DELETE"', seg)
        # 连接串 = 地址|令牌，复制给对方
        self.assertIn("updateSelfConn", self.src)
        self.assertIn('base + "|" + latestLinkToken', self.src)

    def test_panel_refreshes_hosts_and_instances(self):
        i = self.src.index("async function addRemoteHost")
        seg = self.src[i:i + 1300]
        self.assertIn("initHosts()", seg)
        self.assertIn("load()", seg)


if __name__ == "__main__":
    print("集成测试:", "启用" if HAS_TESTCLIENT else "跳过（本地缺少 fastapi/httpx）")
    unittest.main(verbosity=2)
