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
    """开发者工具 = 打开同屏整合页 /devtools（内嵌原生 DevTools）；抽屉只承载日志。"""

    def test_drawer_only_has_log_view(self):
        # 自研九面板与抽屉内嵌 iframe 均已移除，抽屉只剩运行日志。
        self.assertIn('class="side-switch"', INDEX)
        self.assertIn('class="side-view on" id="sideViewLog"', INDEX)
        self.assertNotIn('id="sideTabDev"', INDEX)
        self.assertNotIn("switchSideView('dev')", INDEX)
        self.assertNotIn('id="devToolsFrame"', INDEX)
        self.assertNotIn('id="devMask"', INDEX)

    def test_no_embedded_iframe_left(self):
        # 抽屉内嵌 iframe 方案已放弃，不留 sandbox/dev-wide 残留。
        for dead in ("devToolsFrame", "dev-wide", "dev-frame", "dev-view",
                     "allow-top-navigation", "allow-clipboard-write"):
            self.assertNotIn(dead, INDEX, dead)

    def test_open_dev_opens_integrated_page(self):
        # 主入口与 D 键统一打开同屏整合页 /devtools（内部仍是原生 DevTools）。
        self.assertIn("async function openDev()", JS)
        open_dev = JS.split("async function openDev()")[1].split("function ")[0]
        self.assertIn('"/devtools?sid="', open_dev)
        self.assertIn("window.open(url, \"_blank\", \"noopener\")", open_dev)
        self.assertIn("await devResolveTid()", open_dev)
        self.assertNotIn("await openNativeDevTools();", open_dev)
        self.assertNotIn('switchSideView("dev")', open_dev)
        self.assertIn('onclick="openDev()"', INDEX)

    def test_enhance_drawer_is_alias(self):
        # 兼容旧调用：openEnhanceDrawer 现在等价于 openDev
        self.assertIn("function openEnhanceDrawer()", JS)
        drawer = JS.split("function openEnhanceDrawer()")[1].split("function ")[0]
        self.assertIn("openDev();", drawer)

    def test_nine_panel_tabbar_removed(self):
        # 自研九面板已移除，不再有 dtTabbar / 品牌文案 / 面板 data-p。
        self.assertNotIn('id="dtTabbar"', INDEX)
        self.assertNotIn('data-p="elements"', INDEX)
        self.assertNotIn("网络抓包 · 中文开发者工具", INDEX)

    def test_native_devtools_still_available(self):
        # 原生 DevTools 作为独立窗口入口保留
        self.assertIn("function openNativeDevTools(", JS)
        self.assertIn("/devtools/inspector.html", JS)
        self.assertIn("/devtools/page/", JS)
        self.assertNotIn('frame.src = t.url', JS)
        self.assertIn("openNativeDevTools()", JS)

    def test_no_old_custom_panel_JS_left(self):
        # 自研九面板的旧 JS 实现不得残留（现改为原生 DevTools 独立窗口）
        for dead in ("devMask", "mountNativeDevTools", "devNativeReload",
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

    def test_backend_activates_target_for_direct_link(self):
        # 直链打开 inspector.html 时后端兜底激活目标标签，避免「此标签页不活跃」。
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('rest == "inspector.html"', main)
        self.assertIn("/devtools/page/", main)
        self.assertIn('"/json/activate/"', main)

    def test_frontend_activates_target_before_devtools(self):
        # 主应用打开 DevTools 前先 activate 目标标签，屏幕锁期间不抢前台。
        self.assertIn('"/targets/" + encodeURIComponent(tid) + "/activate"', JS)
        active = JS.split("async function devToolsTarget()")[1].split("async function openNativeDevTools()")[0]
        self.assertIn("if (!screenLocked)", active)
        self.assertIn("setTimeout", active)

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
        self.assertIn("history.replaceState", main)
        self.assertIn("no-store, no-cache, must-revalidate", main)


class NativeDevToolsCleanTests(unittest.TestCase):
    """原生 DevTools 窗口保持干净：不注入任何增强工具台，只保留 WS 参数修正。"""

    @classmethod
    def setUpClass(cls):
        cls.main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    def test_enhance_injection_removed(self):
        for token in ("DEVTOOLS_ENHANCE_SNIPPET", "inject_devtools_enhance",
                      "__cdmpEnh", "cdmpEnhStyle", "attachShadow"):
            self.assertNotIn(token, self.main_src)

    def test_only_ws_normalizer_injected(self):
        # 保留 WS 参数修正；标签栏是独立的小注入，不算被否定的"增强工具台"。
        self.assertIn("text = inject_devtools_ws_normalizer(text)", self.main_src)
        self.assertIn("return text", self.main_src)

    def test_no_cdmp_params_in_devtools_url(self):
        self.assertNotIn("cdmp_sid", INDEX)
        self.assertNotIn("cdmp_lang", INDEX)


class DevToolsTabbarTests(unittest.TestCase):
    """原生 DevTools 窗口顶部标签栏：切换/新建/关闭，直链可用，多语言热切换。"""

    @classmethod
    def setUpClass(cls):
        cls.main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        cls.js = JS

    def test_snippet_injected_in_both_proxies(self):
        # 本地与远端代理都注入标签栏；幂等标记防重复。
        self.assertIn("DEVTOOLS_TAB_SNIPPET", self.main)
        self.assertIn("def inject_devtools_tabbar(", self.main)
        self.assertIn("__cdmpTabbar", self.main)
        self.assertIn("text = inject_devtools_tabbar(text)", self.main)  # 本地代理
        self.assertIn("injected = inject_devtools_tabbar(injected)", self.main)  # 远端代理

    def test_snippet_parses_port_and_prefix_from_url(self):
        # 从自身 URL 解析端口与远端前缀：直链打开也能用。
        self.assertIn("var pm=path.match(", self.main)
        self.assertIn("devtools\\/inspector\\.html", self.main)
        self.assertIn("var rm=path.match(/^\\/remote\\/", self.main)

    def test_snippet_switch_new_close(self):
        # 切换=换 ws 里的 tid 重新导航；新建/关闭走按端口 API。
        self.assertIn("u.searchParams.set('ws',nw)", self.main)
        self.assertIn("location.assign(", self.main)
        self.assertIn("'/api/devtools/'+port+'/tabs'", self.main)
        self.assertIn("method:'POST'", self.main)
        self.assertIn("method:'DELETE'", self.main)

    def test_snippet_i18n_hot_update(self):
        # 中/英/日 + storage 事件热切换，不刷新页面。
        self.assertIn("getk('cdpLang')", self.main)
        self.assertIn("zh:{t:'标签页'", self.main)
        self.assertIn("en:{t:'Tabs'", self.main)
        self.assertIn("ja:{t:'タブ'", self.main)
        self.assertIn("window.addEventListener('storage'", self.main)

    def test_snippet_auth_and_collapse(self):
        self.assertIn("getk('cdpManagerApiKey')", self.main)
        self.assertIn("'X-API-Key'", self.main)
        self.assertIn("function setCollapsed(", self.main)

    def test_backend_tabs_api_reuses_existing(self):
        # 三个按端口接口复用按 sid 的既有实现，含关闭前补保底页逻辑。
        self.assertIn('@app.get("/api/devtools/{port}/tabs")', self.main)
        self.assertIn('@app.post("/api/devtools/{port}/tabs")', self.main)
        self.assertIn('@app.delete("/api/devtools/{port}/tabs/{tid}")', self.main)
        self.assertIn("def _devtools_sid_by_port(", self.main)
        self.assertIn("return await api_targets(sid)", self.main)
        self.assertIn("return await api_new_target(sid, request)", self.main)
        self.assertIn("return await api_close_target(sid, tid)", self.main)


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

    def test_lang_hot_switch_no_reload(self):
        # 主应用语言切换是纯热更新，不刷新页面
        self.assertIn("function applyLang(lang)", JS)
        self.assertIn("function setLang(lang) { applyLang(lang); }", JS)
        self.assertNotIn("location.reload", JS)
        self.assertIn("currentLang", JS)


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


class CapturePanelRemovedTests(unittest.TestCase):
    """自研中文抓包面板已彻底移除，抓包能力由原生 DevTools 承担。"""

    def test_capture_panel_dom_removed(self):
        for token in ('id="capWrap"', 'id="dtTabbar"', 'id="capCtxMenu"',
                      "网络抓包 · 中文开发者工具"):
            self.assertNotIn(token, INDEX)

    def test_capture_functions_removed(self):
        for fn in ("function capGenCurl(", "function capGenPython(",
                   "function capConnect(", "function dtDispatch("):
            self.assertNotIn(fn, JS)

    def test_standalone_file_removed(self):
        self.assertFalse((ROOT / "devtools-preview.html").exists())

    def test_native_python_copy_kept(self):
        # 原生 DevTools 右键「复制为 Python」注入保留在后端，不受影响。
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("DEVTOOLS_PY_SNIPPET", main)
        self.assertIn("appendCopyAsPreloadItem", main)


class SelfBuiltPanelsRemovedTests(unittest.TestCase):
    """自研九面板 CDP 复刻已彻底移除，由浏览器原生 DevTools 取代。"""

    def test_nine_panel_dom_removed(self):
        for token in ('id="dtTabbar"', 'id="dtPanelElements"', 'id="dtPanelSources"',
                      'id="dtPanelApplication"', 'data-p="elements"', 'data-p="performance"'):
            self.assertNotIn(token, INDEX)

    def test_dispatch_functions_removed(self):
        for fn in ("function dtCmd(", "function dtOnResult(", "function dtDispatch(",
                   "function dtSwitch(", "function dtPerfToggle(", "function dtFlameRender(",
                   "function dtScanSecrets(", "function dtExport("):
            self.assertNotIn(fn, JS)

    def test_native_entry_kept(self):
        self.assertIn("async function openNativeDevTools()", JS)
        self.assertIn("原生 DevTools", INDEX)

    def test_backend_devtools_proxy_kept(self):
        # 原生 DevTools 所需的同源代理与 ws 修正必须保留。
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("@app.api_route(\"/{port:int}/devtools/{rest:path}\"", main)
        self.assertIn("def inject_devtools_ws_normalizer(", main)


class IntegratedDevToolsPageTests(unittest.TestCase):
    """同屏开发者工具页 /devtools：套皮壳，内部仍是 CDP 原生 DevTools。"""

    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "app" / "templates" / "devtools.html").read_text(encoding="utf-8")
        cls.main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    def test_page_has_dual_panes(self):
        # 左侧画面 + 右侧 DevTools 两个 iframe，无自研面板。
        self.assertIn('id="stageFrame"', self.page)
        self.assertIn('id="devFrame"', self.page)
        self.assertIn('id="splitter"', self.page)
        self.assertIn('id="tabRow"', self.page)

    def test_devtools_iframe_sandbox_no_top_nav(self):
        frame = self.page.split('id="devFrame"')[1].split(">")[0]
        self.assertIn("sandbox=", frame)
        self.assertNotIn("allow-top-navigation", frame)
        self.assertIn("allow-same-origin", frame)
        self.assertIn("allow-clipboard-write", frame)

    def test_uses_native_inspector_ws(self):
        # 内部仍用原生 inspector.html?ws=，不改 DevTools 本体。
        self.assertIn('"/devtools/inspector.html?cdmp_bare=1&ws="', self.page)
        self.assertIn('"/devtools/page/" + targetTid', self.page)
        self.assertNotIn("location.assign(", self.page)

    def test_headless_uses_cast_full_uses_novnc(self):
        self.assertIn('location.origin + "/cast?sid="', self.page)
        self.assertIn("vnc_lite.html", self.page)
        self.assertIn("if (isHeadless(inst))", self.page)

    def test_activates_target_before_switch(self):
        self.assertIn("/targets/\" + encodeURIComponent(targetTid) + \"/activate", self.page)

    def test_backend_skips_tabbar_for_bare_iframe(self):
        # 整合页 iframe 带 cdmp_bare=1，后端两处注入点都应跳过标签栏，避免双层标签栏。
        token = 'get("cdmp_bare") or [""])[0] == "1"'
        self.assertIn(token, self.main)
        self.assertEqual(self.main.count(token), 2)
        self.assertIn("if not bare:", self.main)

    def test_backend_route_and_auth_exempt(self):
        self.assertIn('@app.get("/devtools", response_class=HTMLResponse)', self.main)
        self.assertIn('templates.TemplateResponse(request, "devtools.html", {})', self.main)
        self.assertIn('"/share", "/cast", "/devtools"', self.main)


class PreviewModeTests(unittest.TestCase):
    """完整内核可在 VNC 整窗 / CDP 抓屏间切换，默认 VNC 且记住上次选择。"""

    def test_full_kernel_effective_mode_respects_saved_choice(self):
        eff = JS.split("function effectiveMode(i)")[1].split("function ")[0]
        # 有头实例读 settings.preview（记住上次选择），无头固定 cast。
        self.assertIn('if (isHeadless(i)) return "cast";', eff)
        self.assertIn('return settings.preview === "cast" ? "cast" : "vnc";', eff)
        self.assertNotIn('return "vnc";\n        }', eff)

    def test_set_preview_allows_full_kernel_switch(self):
        sp = JS.split("function setPreview(mode)")[1].split("function ")[0]
        # 不再对完整内核一刀切拦截；只拦无头实例切 VNC。
        self.assertNotIn("完整内核固定用 VNC", sp)
        self.assertIn('if (mode === "vnc" && isHeadless(sel))', sp)
        self.assertIn("settings.preview = mode;", sp)
        self.assertIn("localStorage.setItem(SETTINGS_KEY", sp)

    def test_capture_button_not_disabled_for_full_kernel(self):
        # 「抓屏」按钮不再因非无头而被禁用；原那行三元禁用已删除。
        self.assertNotIn('完整内核固定用 VNC 显示完整浏览器', INDEX)
        self.assertNotIn("headless ? '' : ' disabled", INDEX)
        # 抓屏按钮的构建片段（从最近的 button 起点到 onclick）不含 disabled。
        tail = INDEX.split("setPreview(\\'cast\\')")[0]
        cap_btn = tail[tail.rfind('<button class="'):]
        self.assertNotIn("disabled", cap_btn)

    def test_default_preview_is_vnc(self):
        # 默认完整浏览器；loadSettings 会读回上次保存的 cast/vnc。
        self.assertIn('preview: s.preview === "cast" ? "cast" : "vnc",', JS)
        self.assertIn('preview: "vnc"', JS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
