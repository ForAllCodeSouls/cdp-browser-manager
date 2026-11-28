"""CDP 脚本注入功能的静态自检（不依赖 Docker / 运行中的实例）。

运行：python -m unittest test_inject -v
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))

MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
INDEX = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


class BuiltinScriptTests(unittest.TestCase):
    def setUp(self):
        from scripts_builtin import BUILTIN_SCRIPTS, get_script

        self.scripts = BUILTIN_SCRIPTS
        self.get_script = get_script

    def test_script_count_and_categories(self):
        self.assertGreaterEqual(len(self.scripts), 16)
        cats = {s["category"] for s in self.scripts}
        self.assertTrue({"指纹伪装", "环境模拟", "反调试", "实用片段"}.issubset(cats))

    def test_ids_unique(self):
        ids = [s["id"] for s in self.scripts]
        self.assertEqual(len(ids), len(set(ids)))

    def test_shape(self):
        for s in self.scripts:
            self.assertIn(s["kind"], ("js", "cdp"))
            self.assertTrue(s.get("name"))
            if s["kind"] == "js":
                self.assertTrue(s.get("code", "").strip(), s["id"])
            else:
                self.assertIn(".", s.get("method", ""), s["id"])
                self.assertIsInstance(s.get("params", {}), dict)

    def test_key_scripts_present(self):
        ids = {s["id"] for s in self.scripts}
        for key in (
            "stealth_core",
            "hide_automation",
            "antidebug_eval",
            "antidebug_cdp",
            "env_tz_shanghai",
            "util_hook_fetch",
        ):
            self.assertIn(key, ids)

    def test_anti_cdp_script_content(self):
        s = next(x for x in self.scripts if x["id"] == "antidebug_cdp")
        self.assertIn("webdriver", s["code"])
        self.assertIn("prepareStackTrace", s["code"])

    def test_selfcheck_script(self):
        ids = {s["id"] for s in self.scripts}
        self.assertIn("util_selfcheck", ids)
        s = self.get_script("util_selfcheck")
        self.assertIn("console.table", s["code"])
        for marker in ("navigator.webdriver", "window.chrome", "__playwright__", "cdc_"):
            self.assertIn(marker, s["code"])


class BackendRouteTests(unittest.TestCase):
    def test_routes(self):
        for route in (
            '@app.get("/api/scripts")',
            '@app.post("/api/scripts")',
            '@app.delete("/api/scripts/{script_id}")',
            '@app.post("/api/instance/{sid}/inject")',
            '@app.get("/api/instance/{sid}/injections")',
            '@app.delete("/api/instance/{sid}/injections/{injection_id}")',
            '@app.post("/api/instance/{sid}/watch")',
            '@app.get("/api/instance/{sid}/watch")',
            '@app.delete("/api/instance/{sid}/watch")',
        ):
            self.assertIn(route, MAIN, route)

    def test_command_whitelist(self):
        for cmd in (
            "Page.addScriptToEvaluateOnNewDocument",
            "Page.removeScriptToEvaluateOnNewDocument",
            "Runtime.evaluate",
            "Emulation.setTimezoneOverride",
            "Emulation.setGeolocationOverride",
        ):
            self.assertIn(cmd, MAIN, cmd)

    def test_watcher_and_cleanup(self):
        self.assertIn("Target.setDiscoverTargets", MAIN)
        self.assertIn("def inject_watch_loop", MAIN)
        stop_fn = MAIN.split("def stop_instance(sid: str) -> bool:")[1].split("\n\n\n")[0]
        self.assertIn("stop_inject_watcher(sid)", stop_fn)
        self.assertIn("INJECTIONS.pop(sid, None)", stop_fn)

    def test_storage_uses_data_dir(self):
        self.assertIn('"inject_scripts": DATA_DIR / "inject_scripts.json"', MAIN)
        self.assertIn('store_load("inject_scripts"', MAIN)

    def test_remote_script_fallback(self):
        self.assertIn('payload.get("method") and payload.get("kind") == "cdp"', MAIN)

    def test_no_list_audit_noise(self):
        block = MAIN.split("@app.get(\"/api/scripts\")")[1].split("@app.post")[0]
        self.assertNotIn("audit(", block)

    def test_docs(self):
        self.assertIn("CDP 脚本注入", README)
        self.assertIn("TLS/JA3", README)
        self.assertIn("所有运行实例", README)
        self.assertIn("运行态", README)
        self.assertIn("伪装自检", README)


class FrontendTests(unittest.TestCase):
    def test_entry(self):
        self.assertIn('onclick="openInject()"', INDEX)
        self.assertIn('id="injectMask"', INDEX)

    def test_functions(self):
        for fn in (
            "async function openInject()",
            "function renderInjectList()",
            "async function doInject()",
            "async function loadInjections()",
            "async function toggleInjectWatch()",
            "async function saveCustomScript()",
        ):
            self.assertIn(fn, INDEX, fn)

    def test_mode_options(self):
        self.assertIn('value="new_document"', INDEX)
        self.assertIn('value="immediate"', INDEX)
        self.assertIn('value="all"', INDEX)

    def test_no_native_confirm(self):
        self.assertNotIn("confirm(", INDEX)

    def test_uses_existing_helpers(self):
        for fn in ("function ask(", "function esc(", "function jsArg(", "async function api("):
            self.assertIn(fn, INDEX, fn)
        self.assertNotIn("function closeToolMenu", INDEX)

    def test_remote_script_library(self):
        self.assertIn("function injectScriptsUrl()", INDEX)
        self.assertIn("/remote/\" + encodeURIComponent(inst.host) + \"/api/scripts", INDEX)
        self.assertIn("async function loadInjectScripts()", INDEX)
        self.assertIn("loadInjectScripts()", INDEX.split("function onInjectSidChange()")[1][:300])

    def test_batch_inject(self):
        self.assertIn('id="injectHostScope"', INDEX)
        self.assertIn('value="all">所有运行中的实例（批量）', INDEX)
        body = INDEX.split("async function doInject()")[1].split("async function loadInjections()")[0]
        self.assertIn("const batch = $(\"injectHostScope\").value === \"all\"", body)
        self.assertIn("const sids = batch ? running.map(i => i.sid) : [currentSid]", body)
        self.assertIn("payload.all_targets = true", body)
        self.assertIn("批量完成", body)

    def test_watch_hint(self):
        self.assertIn("已打开的标签需手动刷新或单独注入", INDEX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
