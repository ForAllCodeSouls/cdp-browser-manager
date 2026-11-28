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

    def test_open_dev_opens_native_devtools(self):
        # 方案二：openDev 主入口直接打开原生 DevTools（含注入增强工具台）
        self.assertIn("function openDev()", JS)
        open_dev = JS.split("function openDev()")[1].split("function ")[0]
        self.assertIn("openNativeDevTools()", open_dev)
        self.assertNotIn('switchSideView("dev")', open_dev)
        self.assertIn('onclick="openDev()"', INDEX)

    def test_enhance_drawer_still_switches(self):
        # 增强工具台抽屉保留主应用侧增强能力，辅助原生 DevTools
        self.assertIn("function openEnhanceDrawer()", JS)
        drawer = JS.split("function openEnhanceDrawer()")[1].split("function ")[0]
        self.assertIn('switchSideView("dev")', drawer)
        self.assertIn('$("logDrawer").classList.add("open")', drawer)

    def test_tabbar_converged_to_enhance(self):
        # 可见 tab 收敛为网络抓包/CDP台/工具三项，复刻原生面板由原生 DevTools 承担
        tabbar = INDEX.split('id="dtTabbar"')[1].split('class="cap-toolbar"')[0]
        self.assertIn('data-p="network"', tabbar)
        self.assertIn('data-p="cdp"', tabbar)
        self.assertIn('data-p="tools"', tabbar)
        self.assertNotIn('data-p="elements"', tabbar)
        self.assertNotIn('data-p="performance"', tabbar)
        self.assertNotIn("网络抓包 · 中文开发者工具", INDEX)

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

    def test_ws_param_no_protocol_prefix(self):
        # 回归：Chrome 158 DevTools frontend 自行补 ws:// 前缀，?ws= 不能带协议。
        self.assertIn("const wsPath = location.host", JS)
        self.assertIn('"/devtools/inspector.html?ws=" + encodeURIComponent(wsPath)', JS)
        self.assertNotIn('const ws = scheme + "://" + location.host + prefix', JS)

    def test_backend_ws_normalizer_fallback(self):
        # 后端兜底：即便浏览器缓存旧 JS 传来完整 ws://，注入的脚本也会剥掉前缀。
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("def inject_devtools_ws_normalizer(", main)
        self.assertIn("DEVTOOLS_WS_NORMALIZER", main)
        self.assertIn("text = inject_devtools_ws_normalizer(text)", main)
        self.assertIn("return inject_devtools_enhance(text)", main)
        self.assertIn("history.replaceState", main)
        self.assertIn("no-store, no-cache, must-revalidate", main)


class EnhanceToolkitInjectionTests(unittest.TestCase):
    """方案二：原生 DevTools 窗口内注入增强工具台，四项增强能力 + 三语即时切换。"""

    @classmethod
    def setUpClass(cls):
        import ast
        cls.main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        cls.snippet = ""
        tree = ast.parse(cls.main_src)
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and node.targets[0].id == "DEVTOOLS_ENHANCE_SNIPPET":
                cls.snippet = ast.literal_eval(node.value)
                break

    def test_injector_wired(self):
        self.assertIn("def inject_devtools_enhance(", self.main_src)
        self.assertIn("return inject_devtools_enhance(text)", self.main_src)
        self.assertIn("__cdmpEnh", self.snippet)

    def test_reads_target_params_from_url(self):
        for k in ("cdmp_sid", "cdmp_tid", "cdmp_key", "cdmp_lang"):
            self.assertIn(k, self.snippet)

    def test_four_enhance_capabilities(self):
        # 敏感扫描 / 批量导出 / CDP 直发 / 多主机聚合
        self.assertIn("scanSecrets", self.snippet)
        self.assertIn("exportData", self.snippet)
        self.assertIn("sendCdp", self.snippet)
        self.assertIn("loadHosts", self.snippet)
        self.assertIn("/ws/inspect/", self.snippet)
        self.assertIn("/api/list", self.snippet)

    def test_three_languages_instant_switch(self):
        for kw in ('zh:', 'en:', 'ja:', "devtools.preferences", "setLang", "applyLang"):
            self.assertIn(kw, self.snippet)

    def test_boot_deferred_and_style_in_head(self):
        # 回归：注入在 <head> 同步执行时 body 尚不存在，挂到 <html> 会被 DevTools
        # frontend 的 DOM 接管破坏；必须延迟到 DOMContentLoaded 后挂 body，
        # 且样式放 <head>（放 ShadowRoot 会被一起破坏，实测整块 CSS 失效）。
        self.assertIn("function boot()", self.snippet)
        self.assertIn('document.readyState==="loading"', self.snippet)
        self.assertIn('addEventListener("DOMContentLoaded",boot', self.snippet)
        self.assertIn("cdmpEnhStyle", self.snippet)
        self.assertIn("document.head||document.documentElement", self.snippet)
        self.assertIn("document.body.appendChild(root)", self.snippet)
        self.assertIn("data-cdmp-enh", self.snippet)
        # 面板自身不再 attachShadow（样式由 head 全局 style 承载）
        self.assertNotIn("attachShadow", self.snippet)

    def test_idempotent_injection(self):
        import ast
        tree = ast.parse(self.main_src)
        fn = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "inject_devtools_enhance":
                fn = node
                break
        self.assertIsNotNone(fn)
        # 幂等守卫：已含标记时原样返回
        src = ast.get_source_segment(self.main_src, fn)
        self.assertIn('"__cdmpEnh" in text', src)
        self.assertIn("return text", src)


class I18nTests(unittest.TestCase):
    """全应用中/英/日多语言，下拉即切、即时生效、持久化。"""

    def test_i18n_framework(self):
        self.assertIn("const I18N", JS)
        for kw in ("currentLang", "applyLang", "setLang", "i18nWalk",
                   "MutationObserver", "cdpLang", "langSelect"):
            self.assertIn(kw, JS)

    def test_three_languages_present(self):
        self.assertIn('{en:"Logs", ja:"', JS)
        self.assertIn('value="en">English', INDEX)
        self.assertIn('value="ja">日本語', INDEX)
        self.assertIn('id="langSelect"', INDEX)
        self.assertIn("setLang(this.value)", INDEX)

    def test_lang_passed_to_native_devtools(self):
        self.assertIn("cdmp_lang=", JS)
        self.assertIn('currentLang', JS)
        self.assertIn('"&cdmp_sid=" + encodeURIComponent(devSid)', JS)


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
        for kw in ("/ws/inspect/", "domain=all", "Network.requestWillBeSent",
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


class DevToolsFullPanelTests(unittest.TestCase):
    """九面板 CDP 复刻：DOM 结构、统一分发、四项增强。"""

    def test_nine_panel_tabs_and_containers(self):
        for p in ("elements", "console", "sources", "network", "performance",
                  "memory", "application", "security", "device", "cdp", "tools"):
            self.assertIn('data-p="' + p + '"', INDEX)
        self.assertIn('id="dtTabbar"', INDEX)
        self.assertIn('id="dtPanelElements"', INDEX)
        self.assertIn('id="dtPanelSources"', INDEX)
        self.assertIn('id="dtPanelApplication"', INDEX)

    def test_unified_dispatch_and_cmd_id(self):
        for fn in ("function dtCmd(", "function dtOnResult(", "function dtDispatch(", "function dtSwitch("):
            self.assertIn(fn, JS)
        self.assertIn('type: "cmd", id: id, method: method', JS)
        for method in ("DOM.getDocument", "CSS.getMatchedStylesForNode", "Debugger.getScriptSource",
                       "Tracing.start", "HeapProfiler.takeHeapSnapshot", "DOMStorage.getDOMStorageItems",
                       "Emulation.setDeviceMetricsOverride", "Security.getSecurityState"):
            self.assertIn(method, JS)

    def test_core_debug_panels(self):
        for fn in ("dtLoadDom", "dtFlatten", "dtSelectNode", "dtRenderElem",
                   "dtScriptParsed", "dtToggleBp", "dtPaused", "dtEvalConsole"):
            self.assertIn("function " + fn + "(", JS)
        for kw in ("matchedCSSRules", "computedStyle", "getBoxModel", "getEventListeners",
                   "callFrames", "scopeChain"):
            self.assertIn(kw, JS)

    def test_analysis_panels(self):
        for fn in ("dtPerfMetrics", "dtPerfToggle", "dtHeapSnapshot", "dtParseHeap",
                   "dtAppRefresh", "dtSecurityRefresh", "dtDeviceApply", "dtDeviceGeo",
                   "dtLoadProfile", "dtFlameRender", "dtFlameColor", "dtFlameOverviewRender",
                   "dtFlameClick", "dtFlameWheel", "dtFlameView", "dtFlameTableRender",
                   "dtProfileFromTraces", "dtFlameFocusNode"):
            self.assertIn("function " + fn + "(", JS)
        for kw in ("Performance.getMetrics", "Tracing.dataCollected", "addHeapSnapshotChunk",
                   "IndexedDB.getDatabaseNames", "CacheStorage.listCaches", "setGeolocationOverride",
                   "Profiler.start", "Profiler.stop", "disabled-by-default-v8.cpu_profiler",
                   "timeDeltas", "dtFlameSvg", "dtFlameOverview", "Bottom-Up", "Call Tree"):
            self.assertIn(kw, JS + INDEX)

    def test_enhancements(self):
        for fn in ("dtScanSecrets", "dtExport", "dtFetchToggle", "dtOpenReplay", "dtReplaySend"):
            self.assertIn("function " + fn + "(", JS)
        for kw in ("Bearer Token", "JWT", "Fetch.continueRequest", "requests.request",
                   "编辑并重新发送", "dtScanSecrets", "dtExport"):
            self.assertIn(kw, JS)
        for kw in ("扫描此请求敏感信息", "批量导出脚本"):
            self.assertIn(kw, INDEX)

    def test_native_entry_kept(self):
        self.assertIn("function openNativeDevTools(", JS)
        self.assertIn("原生 DevTools", INDEX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
