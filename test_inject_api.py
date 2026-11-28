"""CDP 脚本注入的 API 层测试（FastAPI TestClient，不依赖真实浏览器实例）。

运行：python -m unittest test_inject_api -v
依赖：fastapi / httpx / docker / websocket-client / jinja2（见 requirements.txt）。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
DATA_TMP = tempfile.mkdtemp(prefix="cdp-inject-test-")
os.environ["DATA_DIR"] = DATA_TMP
os.environ.pop("API_KEY", None)
sys.path.insert(0, str(ROOT / "app"))

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


def _import_main():
    """隔离 Docker 连接与后台轮询线程，只保留路由与存储逻辑。"""
    with mock.patch("docker.from_env", return_value=mock.MagicMock()), \
            mock.patch("threading.Thread.start", lambda self: None):
        import main as appmain
    return appmain


@unittest.skipUnless(TestClient is not None, "需要安装 fastapi / httpx")
class InjectApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.appmain = _import_main()
        cls.client = TestClient(cls.appmain.app)

    def setUp(self):
        # 每个用例清空自定义脚本，避免顺序依赖。
        self.appmain.store_save("inject_scripts", {})

    def test_01_list_scripts(self):
        r = self.client.get("/api/scripts")
        self.assertEqual(r.status_code, 200)
        items = r.json()["items"]
        ids = {s["id"] for s in items}
        self.assertIn("stealth_core", ids)
        self.assertIn("util_selfcheck", ids)
        self.assertTrue(all(s["origin"] == "builtin" for s in items))

    def test_02_save_and_delete_custom_script(self):
        payload = {
            "id": "my_test", "name": "测试脚本", "category": "自定义",
            "kind": "js", "description": "test", "code": "console.log(1);",
        }
        r = self.client.post("/api/scripts", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        items = self.client.get("/api/scripts").json()["items"]
        mine = [s for s in items if s["id"] == "my_test"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["origin"], "custom")
        self.assertEqual(mine[0]["code"], "console.log(1);")

        r = self.client.delete("/api/scripts/my_test")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.delete("/api/scripts/my_test").status_code, 404)

    def test_03_builtin_script_is_readonly(self):
        r = self.client.post("/api/scripts", json={
            "id": "stealth_core", "name": "伪造", "kind": "js", "code": "1",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.delete("/api/scripts/stealth_core").status_code, 400)

    def test_04_save_validation(self):
        cases = [
            {"id": "bad id!", "name": "x", "kind": "js", "code": "1"},
            {"id": "ok_id", "name": "", "kind": "js", "code": "1"},
            {"id": "ok_id", "name": "x", "kind": "js", "code": "   "},
            {"id": "ok_id", "name": "x", "kind": "other", "code": "1"},
            {"id": "ok_id", "name": "x", "kind": "cdp", "method": "badformat"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                r = self.client.post("/api/scripts", json=payload)
                self.assertEqual(r.status_code, 400, r.text)

    def test_05_inject_requires_script_or_code(self):
        r = self.client.post("/api/instance/nonexistent/inject", json={"mode": "new_document"})
        self.assertEqual(r.status_code, 400)

    def test_06_inject_rejects_bad_mode_and_target(self):
        r = self.client.post("/api/instance/nonexistent/inject", json={
            "code": "1", "mode": "invalid",
        })
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/instance/nonexistent/inject", json={
            "code": "1", "target_id": "bad id!",
        })
        self.assertEqual(r.status_code, 400)

    def test_07_injections_empty(self):
        r = self.client.get("/api/instance/nonexistent/injections")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])
        self.assertEqual(self.client.delete("/api/instance/nonexistent/injections/abc").status_code, 404)

    def test_08_watch_validation(self):
        r = self.client.post("/api/instance/nonexistent/watch", json={"scripts": []})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/instance/nonexistent/watch", json={"scripts": ["not_exist"]})
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/instance/nonexistent/watch")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["running"])

    def test_09_watch_stop_is_idempotent(self):
        r = self.client.delete("/api/instance/nonexistent/watch")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_10_cdp_script_saved_with_params(self):
        r = self.client.post("/api/scripts", json={
            "id": "cdp_tz", "name": "设时区", "kind": "cdp",
            "method": "Emulation.setTimezoneOverride",
            "params": {"timezoneId": "Asia/Shanghai"},
        })
        self.assertEqual(r.status_code, 200, r.text)
        items = self.client.get("/api/scripts").json()["items"]
        mine = next(s for s in items if s["id"] == "cdp_tz")
        self.assertEqual(mine["method"], "Emulation.setTimezoneOverride")
        self.assertEqual(mine["params"]["timezoneId"], "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
