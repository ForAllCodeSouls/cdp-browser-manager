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
import threading
import unittest
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


class AuthExemptTests(unittest.TestCase):
    def test_exempt_paths(self):
        for path in ("/", "/api/health", "/api/hosts", "/7901/vnc_lite.html",
                     "/7901/app.js", "/7901/core/rfb.js"):
            self.assertTrue(main.auth_exempt(path, "GET"), path)

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
        self.patches = [
            patch.object(main, "API_KEY", ""),
            patch.object(main, "REMOTE_HOSTS", self.hosts),
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

    def test_websocket_key_guard(self):
        from starlette.websockets import WebSocketDisconnect

        with patch.object(main, "API_KEY", "local-secret"):
            client = TestClient(main.app)
            with self.assertRaises(WebSocketDisconnect) as ctx:
                with client.websocket_connect("/ws/cast/abc/def"):
                    pass
            self.assertEqual(ctx.exception.code, 4401)


if __name__ == "__main__":
    print("集成测试:", "启用" if HAS_TESTCLIENT else "跳过（本地缺少 fastapi/httpx）")
    unittest.main(verbosity=2)
