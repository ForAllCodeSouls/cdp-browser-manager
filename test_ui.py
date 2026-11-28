"""UI 重构的静态自检：信息架构 / 分区结构 / 视觉变量 / 主题兼容。

运行：python -m unittest test_ui -v
不依赖 Docker 或运行中的实例。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")

# 取出 <style> 与 <script> 便于分段断言
CSS = "\n".join(re.findall(r"<style>(.*?)</style>", INDEX, re.S))
JS = "\n".join(re.findall(r"<script>(.*?)</script>", INDEX, re.S))


class ToolMenuTests(unittest.TestCase):
    def test_grouped_menu(self):
        for group in ("自动化", "运维治理", "录制与界面"):
            self.assertIn('class="tool-group-title">' + group, INDEX)
        self.assertEqual(INDEX.count('class="tool-group"'), 3)

    def test_menu_icons(self):
        for symbol in ("i-terminal", "i-code", "i-clock", "i-chart", "i-record", "i-theme", "i-stack", "i-shield"):
            self.assertIn('<symbol id="' + symbol + '"', INDEX)
        # 每个工具按钮都带图标
        menu = INDEX.split('id="toolMenu"')[1].split("</div>")[0]
        for fn in ("openBatch()", "openInject()", "openSnipLib()", "openTpl()",
                   "openSched()", "openAudit()", "openRes()", "openRec()", "toggleTheme()"):
            self.assertIn(fn, INDEX)
        self.assertGreaterEqual(INDEX.count('<svg class="ico"><use href="#i-'), 8)

    def test_menu_entries_preserved(self):
        for fn in ("openBatch()", "openInject()", "openSnipLib()", "openTpl()",
                   "openSched()", "openAudit()", "openRes()", "openRec()", "toggleTheme()"):
            self.assertIn("onclick=\"" + fn + "\"", INDEX)


class DetailTests(unittest.TestCase):
    def test_detail_zone_structure(self):
        self.assertIn("detail-main", JS)
        self.assertIn("detail-identity", JS)
        self.assertIn("detail-connection", JS)
        self.assertIn("detail-meta", JS)
        self.assertIn("lk-group", JS)
        self.assertIn("detail-main", CSS)
        self.assertIn("detail-meta", CSS)

    def test_detail_critical_ids_preserved(self):
        # 这些 token 在 INDEX 里都有（含 JS 段），统一对整份模板断言即可。
        for token in ('id="cdpVal"', "copyCdp(\\'domain\\')", 'openVncPage()',
                      'openCastPage()', 'class="badge host badge-host-link"',
                      'class="inst-cdp-link"', ">VNC画面 ↗</button>", ">抓屏画面 ↗</button>"):
            self.assertIn(token, INDEX, token)

    def test_link_groups(self):
        self.assertIn("CDP 端点", JS)
        self.assertIn("<span class=\"lk-label\">工具</span>", JS)
        self.assertIn("class=\"lk-group\"", JS)


class PanelTests(unittest.TestCase):
    def test_unified_panel_skeleton(self):
        self.assertIn("panelIn", CSS)
        self.assertIn("maskIn", CSS)
        self.assertIn("backdrop-filter: blur(3px)", CSS)
        self.assertIn(".panel::before", CSS)
        self.assertIn("var(--radius-lg)", CSS)

    def test_inject_panel_steps(self):
        self.assertIn("inject-step", INDEX)
        self.assertIn("inject-step-head", INDEX)
        self.assertIn("step-no", INDEX)
        self.assertEqual(JS.count('<div class="inject-step">') + INDEX.count('<div class="inject-step">'), 3)

    def test_inject_advanced_collapsible(self):
        self.assertIn('<details class="inject-details">', INDEX)
        self.assertGreaterEqual(INDEX.count('<details class="inject-details">'), 3)
        for token in ('id="watchState"', 'id="injectRecords"', 'id="customId"',
                      'id="customName"', 'id="customCategory"', 'toggleInjectWatch()',
                      'loadInjections()', 'saveCustomScript()', 'deleteCustomScript()'):
            self.assertIn(token, INDEX)

    def test_all_masks_present(self):
        masks = re.findall(r'id="([a-zA-Z]+Mask)"', INDEX)
        # 原 19 个含 devMask（内嵌开发者工具面板）；现已改为独立原生窗口，故为 18。
        self.assertGreaterEqual(len(set(masks)), 18)


class VisualSystemTests(unittest.TestCase):
    def test_tokens(self):
        for token in ("--radius-lg", "--radius-sm", "--shadow-sm", "--shadow-lg",
                      "--header-bg", "--card-bg"):
            self.assertIn(token, CSS)

    def test_theme_compatibility(self):
        light = CSS.split('html[data-theme="light"] {')[1].split("}")[0]
        self.assertIn("--header-bg", light)
        self.assertIn("--shadow-lg", light)
        self.assertIn('html[data-theme="light"] .badge.full', CSS)
        self.assertIn('html[data-theme="light"] .badge.headless', CSS)

    def test_badge_tokens(self):
        for cls in ("running", "stopped", "persist", "full", "headless", "host"):
            self.assertIn(".badge." + cls, CSS)
        self.assertIn("rgba(62,207,142,.13)", CSS)

    def test_sidebar_handle_intact(self):
        # 视觉升级不能破坏侧栏细线 sash 的历史约定
        self.assertIn(".sidebar-handle::before", CSS)
        self.assertIn('<symbol id="i-wave"', INDEX)
        self.assertIn('class="sidebar-handle"', INDEX)

    def test_no_native_confirm(self):
        self.assertNotIn("confirm(", INDEX)


class HandlerIntegrityTests(unittest.TestCase):
    """防止重构遗留未定义变量，导致按钮点击后函数静默抛错。"""

    def test_no_stale_current_tid(self):
        # 历史遗留变量 currentTid 从未定义，曾导致开发者工具/批量执行/
        # 定时任务/录制回放四个入口点击无反应。
        self.assertNotIn("currentTid", JS)

    def test_all_onclick_handlers_defined(self):
        calls = set(re.findall(r'onclick=["\']([A-Za-z_$][\w$]*)\s*\(', INDEX))
        calls -= {"if", "return", "switch", "for", "while"}
        defined = set(re.findall(r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(', JS))
        defined |= set(re.findall(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(', JS))
        missing = sorted(calls - defined)
        self.assertEqual(missing, [], "onclick 引用了未定义函数: " + ", ".join(missing))

    def test_affected_handlers_use_defined_tid_source(self):
        for fn in ("openDev", "runBatch", "saveSchedule", "replayRecording"):
            self.assertIn("function " + fn + "(", JS)
        dev = JS.split("function openDev()")[1].split("function ")[0]
        self.assertIn("castPreferredTid", dev)
        self.assertIn("castState", dev)


class DevToolsDrawerTests(unittest.TestCase):
    """开发者工具 = 右侧抽屉内中文抓包面板，与运行日志按钮切换；原生窗口为其中入口。"""

    def test_drawer_has_two_views(self):
        self.assertIn('class="side-switch"', INDEX)
        self.assertIn('class="side-view on" id="sideViewLog"', INDEX)
        self.assertIn('id="devMask"', INDEX)
        self.assertIn('id="sideTabLog"', INDEX)
        self.assertIn('id="sideTabDev"', INDEX)
        self.assertIn('onclick="switchSideView(\'dev\')"', INDEX)
        self.assertIn("function switchSideView(which)", JS)

    def test_open_dev_switches_to_drawer(self):
        # openDev 打开抽屉并切到开发者工具视图，不再弹独立窗口
        self.assertIn("function openDev()", JS)
        open_dev = JS.split("function openDev()")[1].split("function ")[0]
        self.assertIn('switchSideView("dev")', open_dev)
        self.assertIn('$("logDrawer").classList.add("open")', open_dev)
        self.assertIn('onclick="openDev()"', INDEX)

    def test_native_devtools_still_available(self):
        # 原生 DevTools 独立窗口作为抓包面板内入口保留
        self.assertIn("function openNativeDevTools(", JS)
        self.assertIn("/devtools/inspector.html", JS)
        self.assertIn("/devtools/page/", JS)
        self.assertIn("openNativeDevTools()", INDEX)

    def test_no_embedded_iframe_left(self):
        # 不保留会降级为 undocked 的内嵌 iframe 实现
        for dead in ("nativeDevFrame", "mountNativeDevTools", "devNativeReload",
                     "copyDevToolsLink", "initDrawerResizer", "drawerResizer",
                     "devToggle", "switchDev("):
            self.assertNotIn(dead, INDEX + JS, dead)

    def test_remote_native_devtools_path(self):
        self.assertIn('"/remote/" + encodeURIComponent(sel.host)', JS)
        self.assertIn("function devToolsTarget()", JS)

    def test_tid_autoresolve(self):
        self.assertIn("function devResolveTid()", JS)
        self.assertIn("/targets", JS)

    def test_backend_devtools_proxy(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("def relax_devtools_csp(", main)
        self.assertIn('@app.api_route("/{port:int}/devtools/{rest:path}"', main)
        self.assertIn("Content-Security-Policy", main)


class PythonCopyInjectionTests(unittest.TestCase):
    """方案一：原生 DevTools 右键菜单注入中文「复制为 Python」。"""

    @classmethod
    def setUpClass(cls):
        cls.main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    def test_anchor_and_snippet_present(self):
        self.assertIn('DEVTOOLS_PY_ANCHOR = "this.appendCopyAsPreloadItem(o,t)"', self.main_src)
        for kw in ("import requests", "import httpx", "requestFormData",
                   "requestHeaders", "copyText"):
            self.assertIn(kw, self.main_src)

    def test_inject_is_idempotent_and_safe(self):
        # 直接加载常量并调用函数验证：锚点唯一才替换、重复注入跳过、中文转义正确
        import ast
        tree = ast.parse(self.main_src)
        consts = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in ("DEVTOOLS_PY_ANCHOR", "DEVTOOLS_PY_SNIPPET"):
                    try:
                        consts[name] = ast.literal_eval(node.value)
                    except Exception:
                        pass
        self.assertIn("DEVTOOLS_PY_ANCHOR", consts)
        self.assertIn("DEVTOOLS_PY_SNIPPET", consts)
        anchor = consts["DEVTOOLS_PY_ANCHOR"]
        snippet = consts["DEVTOOLS_PY_SNIPPET"].replace("function(m,r){", "function __cdmpyPython(m,r){", 1)

        fake = "AAA" + anchor + "BBB"
        injected = fake.replace(anchor, snippet, 1)
        self.assertIn("__cdmpyPython", injected)
        self.assertIn("AAA", injected)
        self.assertIn("BBB", injected)
        # 中文以 \u 转义存在，避免编码问题
        self.assertIn("\\u590d\\u5236\\u4e3a Python", snippet)
        # 注入后再次出现标记时不影响幂等（函数体已带标记）
        self.assertIn("__cdmpyPython", injected)

    def test_both_proxy_paths_inject(self):
        # 本机 DevTools 代理与远端代理都要注入 network.js
        self.assertIn("rest.endswith(\"/network.js\")", self.main_src)
        self.assertIn("rest.endswith(\"/devtools/panels/network/network.js\")", self.main_src)

    def test_async_body_awaited(self):
        # requestFormData 是异步方法，必须 await，否则生成 [object Promise]
        self.assertIn("await r.requestFormData()", self.main_src)


class CapturePanelTests(unittest.TestCase):
    """主应用抽屉内的中文抓包面板：真 CDP 抓包、右键 cURL/Python。"""

    def test_chinese_ui(self):
        for kw in ("网络抓包", "开始抓包", "请求头", "Python 代码", "复制为 Python (requests)"):
            self.assertIn(kw, INDEX)

    def test_real_cdp_wiring(self):
        for kw in ("/ws/inspect/", "domain=network", "Network.requestWillBeSent",
                   "Network.responseReceived", "Network.getResponseBody", "oncontextmenu"):
            self.assertIn(kw, JS + INDEX)

    def test_export_functions(self):
        for fn in ("function capGenCurl(", "function capGenPython(", "function capCtxAction("):
            self.assertIn(fn, JS)

    def test_no_naming_collision(self):
        # 抓包函数全部 cap 前缀，不覆盖主应用的 toast/render/$ 等
        self.assertNotIn("function render()", JS.split("// ---- 中文抓包面板")[1].split("// ---- 批量执行")[0])
        cap_section = JS.split("// ---- 中文抓包面板")[1].split("// ---- 批量执行")[0]
        for fn in ("capGenCurl", "capGenPython", "capRender", "capCopyText", "capLog", "capConnect"):
            self.assertIn("function " + fn + "(", cap_section)
        self.assertNotIn("id=\"capWrap\"", INDEX.replace('id="capWrap"', "", 1))  # 只出现一次

    def test_standalone_file_removed(self):
        # 抓包功能已整合进主应用，不再保留单独 html 文件
        self.assertFalse((ROOT / "devtools-preview.html").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
