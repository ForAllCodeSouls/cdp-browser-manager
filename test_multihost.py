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
import time
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
        # 详情栏在切换按钮后有按「模式+画质+抓屏方式」给出的建议文字。
        self.assertIn("qualityHint(mode, settings.quality, cm, settings.snapRate)", self.src)
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
        # 注释扩展后函数变长，截取到下一个路由定义，避免 opcode 代码被截出窗口。
        seg = src[i:src.index("@app.api_route(\"/{port:int}/json/", i)]
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


class VncRefreshTests(unittest.TestCase):
    """VNC 刷新档：全局持久化 + x11vnc -R 热改运行中实例，随时可切换。"""

    def test_normalize_falls_back_to_standard(self):
        self.assertEqual(main.normalize_vnc_refresh(""), "standard")
        self.assertEqual(main.normalize_vnc_refresh(None), "standard")
        self.assertEqual(main.normalize_vnc_refresh("bogus"), "standard")
        self.assertEqual(main.normalize_vnc_refresh("FAST"), "fast")

    def test_profiles_get_progressively_faster(self):
        std = main.VNC_REFRESH_PROFILES["standard"]
        fast = main.VNC_REFRESH_PROFILES["fast"]
        ultra = main.VNC_REFRESH_PROFILES["ultra"]
        # 标准保持 x11vnc 自带默认，旧实例行为不变
        self.assertEqual((std["wait"], std["defer"]), (20, 30))
        self.assertGreater(std["wait"], fast["wait"])
        self.assertGreater(fast["wait"], ultra["wait"])
        self.assertEqual((ultra["wait"], ultra["defer"]), (0, 0))

    def test_start_script_applies_throttle_flags(self):
        with open(os.path.join(ROOT, "start.sh"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('VNC_WAIT="${VNC_WAIT:-20}"', src)
        self.assertIn('VNC_DEFER="${VNC_DEFER:-30}"', src)
        self.assertIn('-wait "$VNC_WAIT" -defer "$VNC_DEFER"', src)
        # 非数字兜底：异常环境变量不能让 x11vnc 起不来
        self.assertIn("*[!0-9]*", src)

    def test_manager_passes_env_and_label(self):
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('environment["VNC_WAIT"] = str(vp["wait"])', src)
        self.assertIn('environment["VNC_DEFER"] = str(vp["defer"])', src)
        self.assertIn("VNC_REFRESH_LABEL: vnc_key", src)
        self.assertIn('"vnc_refresh": vnc_refresh', src)  # 远端创建转发
        self.assertIn('"vnc_refresh": vnc_key', src)      # 实例信息回传

    def test_global_persist_and_hot_apply(self):
        self.assertTrue(hasattr(main, "VNC_REFRESH_FILE"))
        self.assertTrue(hasattr(main, "get_global_vnc_refresh"))
        self.assertTrue(hasattr(main, "apply_vnc_refresh_to_running"))
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("x11vnc -R wait:", src)
        self.assertIn("x11vnc -R defer:", src)
        self.assertIn('@app.post("/api/vnc-refresh")', src)
        self.assertIn('@app.get("/api/vnc-refresh")', src)
        # 新建实例用全局档位（空值回落到持久化的档位）
        self.assertIn("vnc_refresh or get_global_vnc_refresh()", src)

    def test_js_global_dropdown_no_create_option(self):
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('class="vnc-sel"', src)
        self.assertIn("onchange=\"setVncRefresh(this.value)\"", src)
        self.assertIn("async function setVncRefresh(key)", src)
        self.assertIn("/api/vnc-refresh", src)
        # 新建弹窗不再有 VNC 刷新选项
        self.assertNotIn('id="createVncRefresh"', src)
        self.assertNotIn("vnc_refresh: $(\"createVncRefresh\")", src)
        self.assertIn("VNC_REFRESH_LABELS", src)


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

    def test_monitor_profile_is_small_and_throttled(self):
        mon = main.cast_quality_profile("monitor")
        self.assertLessEqual(mon["max_width"], 640)
        self.assertLessEqual(mon["max_height"], 360)
        self.assertLess(mon["quality"], main.cast_quality_profile("low")["quality"])
        # 监控档限帧，避免多路同屏打满带宽
        self.assertGreaterEqual(mon["min_interval"], 1.0)

    def test_cast_pump_honors_min_interval(self):
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('min_interval = float(profile.get("min_interval", 0) or 0)', src)
        self.assertIn("now_send - last_sent < min_interval", src)


class CaptureModeTests(unittest.TestCase):
    """抓屏方式：Cast 变化推帧 / Snap 定时截图，模式与间隔参数后端兜底。"""

    def setUp(self):
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            self.src = f.read()
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            self.js = f.read()

    def test_snap_interval_profiles(self):
        self.assertAlmostEqual(main.SNAP_INTERVALS["fast"], 0.2)
        self.assertAlmostEqual(main.SNAP_INTERVALS["normal"], 0.5)
        self.assertAlmostEqual(main.SNAP_INTERVALS["eco"], 1.0)

    def test_normalize_snap_interval_falls_back(self):
        self.assertEqual(main.normalize_snap_interval("FAST"), "fast")
        self.assertEqual(main.normalize_snap_interval("eco"), "eco")
        self.assertEqual(main.normalize_snap_interval(""), "normal")
        self.assertEqual(main.normalize_snap_interval("bogus"), "normal")
        self.assertEqual(main.normalize_snap_interval(None), "normal")

    def test_backend_accepts_mode_and_snap_params(self):
        self.assertIn('websocket.query_params.get("mode", "cast")', self.src)
        self.assertIn('websocket.query_params.get("snap", "")', self.src)
        self.assertIn('if capture_mode not in ("cast", "snap") or profile.get("min_interval"):', self.src)

    def test_snap_skips_screencast_and_polls(self):
        self.assertIn('if capture_mode == "cast":\n                upstream_send({\n                    "id": ack_id,\n                    "method": "Page.startScreencast"', self.src)
        self.assertIn('upstream.settimeout(max(0.05, min(snap_interval, 0.25)))', self.src)

    def test_both_modes_share_snap_interval_heartbeat(self):
        # Cast 与 Snap 共用 snap_interval 定时抓帧：Cast 作保底心跳，Snap 作主节奏。
        self.assertIn("if now - last_frame >= snap_interval:", self.src)
        self.assertNotIn("else CAST_KEEPALIVE_SEC", self.src)
        self.assertIn('if not str(websocket.query_params.get("snap", "") or "").strip():', self.src)
        self.assertIn("snap_interval = CAST_KEEPALIVE_SEC", self.src)

    def test_any_frame_resets_heartbeat(self):
        # screencast 帧与截图帧统一重置计时，避免截图在途时重复发请求。
        self.assertIn("last_frame = time.monotonic()  # 收到实帧，重置补帧计时", self.src)
        self.assertIn('pending_shots.discard(msg["id"])\n                    data = msg["result"].get("data", "")\n                    last_frame = time.monotonic()', self.src)

    def test_stop_screencast_only_in_cast(self):
        self.assertIn("if capture_mode == \"cast\":\n                try:\n                    upstream_send({\"id\": 999, \"method\": \"Page.stopScreencast\"", self.src)

    def test_js_mode_switch_persists_and_reconnects(self):
        self.assertIn("function setCaptureMode(m)", self.js)
        self.assertIn("function setSnapRate(r)", self.js)
        self.assertIn("captureMode: s.captureMode === \"snap\" ? \"snap\" : \"cast\"", self.js)
        self.assertIn("snapRate: [\"fast\", \"normal\", \"eco\"].includes(s.snapRate)", self.js)
        # 切换模式/间隔都要重连画面
        i = self.js.index("function setCaptureMode(m)")
        seg = self.js[i:i + 500]
        self.assertIn("resetFrameState()", seg)
        self.assertIn("render()", seg)

    def test_js_ws_url_carries_mode_and_interval(self):
        i = self.js.index("function castWsUrl(i, tid)")
        seg = self.js[i:i + 1100]
        self.assertIn("&mode=", seg)
        self.assertIn('"&snap=" + encodeURIComponent(sr)', seg)

    def test_js_detail_has_mode_seg_and_hint(self):
        self.assertIn("const CAPTURE_LABELS = { cast: \"Cast\", snap: \"Snap\" }", self.js)
        self.assertIn("const SNAP_RATE_LABELS", self.js)
        self.assertIn("onclick=\"setCaptureMode(\\'' + k + '\\')\"", self.js)
        self.assertIn("onclick=\"setSnapRate(\\'' + k + '\\')\"", self.js)
        seg = self.js[self.js.index("function qualityHint"):self.js.index("function qualityHint") + 600]
        self.assertIn("定时抓图每", seg)
        self.assertIn("变化推帧 + 每", seg)  # Cast 也说明保底节奏

    def test_cast_mode_also_shows_interval_seg(self):
        # 三档间隔按钮不再只属于 Snap：Cast 也显示作为保底心跳。
        i = self.js.index("const capseg =")
        seg = self.js[i:i + 1400]
        self.assertNotIn('cm === "snap"', seg)
        self.assertIn("Snap 的抓图间隔 / Cast 的保底心跳", seg)

    def test_both_modes_send_snap_param(self):
        # 两种模式都要带 snap 参数，否则后端回落旧 1.5s 兜底。
        i = self.js.index("function castWsUrl(i, tid)")
        seg = self.js[i:i + 1100]
        self.assertNotIn("if (cm === \"snap\")", seg)
        self.assertIn('"&mode=" + cm + "&snap=" + encodeURIComponent(sr)', seg)

    def test_monitor_forces_cast(self):
        i = self.js.index("function monitorWsUrl(inst, tid)")
        seg = self.js[i:i + 400]
        self.assertIn("quality=monitor&mode=cast", seg)


class MonitorPanelJsTests(unittest.TestCase):
    """监控主面板：独立视图 + 实例列表预览按钮 + 抓屏监控档。"""

    def setUp(self):
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            self.src = f.read()

    def test_view_and_toggle_button(self):
        self.assertIn('id="monitorView"', self.src)
        self.assertIn('id="monitorGrid"', self.src)
        # 监控开关支持 forceOpen/skipLoad（刷新后恢复监控视图时避免重复拉数据）。
        self.assertIn("function toggleMonitorView(forceOpen, skipLoad)", self.src)
        self.assertIn("function toggleMonitorSid(sid, forceOn)", self.src)
        self.assertIn('id="btnMonitor"', self.src)
        # 按钮从顶栏移到实例列表标题行右侧（head-actions 里，紧凑图标）。
        i = self.src.index('id="btnMonitor"')
        self.assertIn('class="head-icon"', self.src[i:i + 200])
        self.assertIn('class="head-actions"', self.src[:i])

    def test_preview_switch_in_instance_list(self):
        # 「预览」由文字按钮改为滑动开关（label + checkbox + switch）。
        self.assertIn('class="mon-toggle switch-wrap"', self.src)
        self.assertIn('this.checked)', self.src)
        self.assertIn('<span class="switch"></span>', self.src)
        self.assertIn('const MONITOR_KEY = "cdpManagerMonitorSids"', self.src)

    def test_monitor_view_not_hidden_with_layout(self):
        # 回归：曾把整个 .layout display:none 导致监控面板（其子元素）一起被藏。
        # 现在监控模式只隐藏 .viewer，侧栏保留。
        self.assertIn('.layout.monitor-mode > .viewer', self.src)
        self.assertIn('classList.toggle("monitor-mode", monitorViewOpen)', self.src)
        self.assertNotIn('.layout").style.display = monitorViewOpen', self.src)
        self.assertNotIn('.layout.monitor-mode > aside', self.src)

    def test_sidebar_global_toggle_and_persistence(self):
        # 全局侧栏显隐：普通视图与监控面板都生效，状态持久化。
        for token in ('id="btnSidebar"', 'const SIDEBAR_KEY = "cdpManagerSidebarHidden"',
                      'let sidebarHidden = false;', 'function toggleSidebar(force)',
                      'function loadSidebarHidden()', 'function applySidebar()',
                      '.layout.sidebar-hidden > aside { display: none; }',
                      'classList.toggle("sidebar-hidden", sidebarHidden)',
                      'sidebarHidden = loadSidebarHidden()', 'applySidebar();',
                      'id="i-sidebar"'):
            self.assertIn(token, self.src)

    def test_sidebar_handle_on_aside_edge_not_topbar(self):
        # 侧栏折叠按钮放在侧栏右边缘中部的把手上，顶栏不再放「侧栏」按钮。
        self.assertIn('class="sidebar-handle"', self.src)
        self.assertIn('.sidebar-handle {', self.src)
        # 细线 sash + hover 浮出三叠波浪纹（无蓝框方块/阴影）。
        self.assertIn('.sidebar-handle::before {', self.src)
        self.assertIn('width: 2px; background: var(--border);', self.src)
        self.assertIn('.sidebar-handle:hover::before, .sidebar-handle:focus-visible::before {', self.src)
        self.assertIn('background: var(--accent); width: 3px;', self.src)
        self.assertNotIn('box-shadow: 0 2px 10px', self.src)
        self.assertIn('width: 11px; height: 22px;', self.src)
        self.assertIn('opacity: 0; transform: scale(.8);', self.src)
        self.assertIn('.sidebar-handle:hover .ico, .sidebar-handle:focus-visible .ico { opacity: 1;', self.src)
        self.assertIn('rotate(90deg)', self.src)
        self.assertIn('.layout.sidebar-hidden .sidebar-handle { left: 0', self.src)
        # 收起态命中区加大到 32px + 波浪纹常态可见（不用先 hover 到 2px 细线上才显形）。
        self.assertIn('.layout.sidebar-hidden .sidebar-handle { left: 0; width: 32px; }', self.src)
        self.assertIn('.layout.sidebar-hidden .sidebar-handle .ico { opacity: .45; transform: scale(1); }', self.src)
        # 窄屏收起态横条命中高度加大到 26px，且波浪纹保持 90° 横向。
        self.assertIn('width: auto; height: 26px; }', self.src)
        self.assertIn('.layout.sidebar-hidden .sidebar-handle .ico { transform: rotate(90deg) scale(1); }', self.src)
        # 按钮在 layout 内、aside 之前，而非 header 的 toolbar 内。
        i = self.src.index('id="btnSidebar"')
        self.assertIn('<div class="layout">', self.src[max(0, i - 120):i])
        self.assertNotIn('class="btn-icon" title="隐藏/显示左侧实例列表', self.src)
        # 三叠波浪纹图标（无方向切换），隐藏侧栏后把手仍可见可点回来。
        self.assertIn('<use href="#i-wave"/>', self.src)
        self.assertIn('<symbol id="i-wave"', self.src)
        self.assertNotIn('setAttribute("href", sidebarHidden ? "#i-forward" : "#i-back")', self.src)

    def test_instances_local_first_then_created_at(self):
        self.assertIn("Object.values(instances).sort((a, b)", self.src)
        self.assertIn('const ha = (a.host && a.host !== "local") ? 1 : 0;', self.src)
        self.assertIn("if (ha !== hb) return ha - hb;", self.src)
        self.assertIn("(b.created_at || 0) - (a.created_at || 0)", self.src)

    def test_monitor_uses_screencast_monitor_quality(self):
        self.assertIn("quality=monitor", self.src)
        self.assertIn("function monitorWsUrl(inst, tid)", self.src)
        # 远端实例也走 /remote 前缀（复用 cast_ws）
        self.assertIn("inst.cast_ws", self.src)


class HostFilterJsTests(unittest.TestCase):
    """实例列表按主机筛选 + 远程面板跳转 + 令牌生成后立即刷新。"""

    def setUp(self):
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            self.src = f.read()

    def test_host_filter_ui_and_state(self):
        self.assertIn('id="hostFilter"', self.src)
        self.assertIn('const HOST_FILTER_KEY = "cdpManagerHostFilter"', self.src)
        self.assertIn("function setHostFilter(id)", self.src)
        self.assertIn("function renderHostFilter(all)", self.src)
        self.assertIn("loadHostFilter();", self.src)
        # 筛选标签含「全部」「本机」与远端主机
        self.assertIn('{ id: "all", name: "全部"', self.src)
        self.assertIn('{ id: "local", name: "本机"', self.src)

    def test_filter_applies_to_list(self):
        seg = self.src[self.src.index("const list = all.filter"):self.src.index("const list = all.filter") + 220]
        self.assertIn('hostFilter === "all"', seg)
        self.assertIn('i.host === hostFilter', seg)

    def test_remote_address_opens_host_panel(self):
        self.assertIn("function safeHostUrl(base)", self.src)
        self.assertIn("target=\"_blank\" rel=\"noopener\"", self.src)
        self.assertIn('title="在新标签页打开该主机面板"', self.src)

    def test_gen_token_refreshes_immediately(self):
        # 生成令牌后必须立即刷新列表，不能等重新打开弹窗/点刷新。
        i = self.src.index("async function genLinkToken()")
        seg = self.src[i:i + 900]
        self.assertIn("await refreshRemotePanel();", seg)


class CreatedAtTests(unittest.TestCase):
    """实例创建时间：重启恢复后按容器 Created 字段还原，供前端排序。"""

    def test_parse_docker_created(self):
        c = SimpleNamespace(attrs={"Created": "2026-11-27T06:12:33.123456789Z"})
        ts = main.container_created_at(c)
        self.assertGreater(ts, 0)
        # 与手工解析的 UTC 时间戳一致（纳秒被截到秒）
        from datetime import datetime, timezone
        expect = datetime(2026, 11, 27, 6, 12, 33, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ts, expect)

    def test_bad_value_falls_back(self):
        ts = main.container_created_at(SimpleNamespace(attrs={}))
        self.assertGreater(ts, 0)

    def test_restore_writes_created_at(self):
        # 恢复路径必须写入 created_at，否则重启后前端排序失效。
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            src = f.read()
        seg = src[src.index("def restore_instances()"):src.index("def to_view(")]
        self.assertIn('"created_at": container_created_at(c)', seg)


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
class VncRefreshApiTests(unittest.TestCase):
    """全局 VNC 刷新档接口：GET 读档 / POST 持久化并热改。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cdpmgr-vnc-")
        self.patches = [
            patch.object(main, "API_KEY", ""),
            patch.object(main, "VNC_REFRESH_FILE", Path(self.tmp) / "vnc_refresh.json"),
            patch.object(main, "INSTANCES", {}),
            patch.object(main, "apply_vnc_refresh_to_running",
                         side_effect=lambda key: {"refresh": key, "applied": ["abc12345"], "failed": []}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_get_default_and_post_persists(self):
        client = TestClient(main.app)
        self.assertEqual(client.get("/api/vnc-refresh").json()["refresh"], "standard")
        r = client.post("/api/vnc-refresh", json={"refresh": "fast"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["refresh"], "fast")
        self.assertEqual(r.json()["applied"], ["abc12345"])
        self.assertEqual(client.get("/api/vnc-refresh").json()["refresh"], "fast")
        # 新实例默认档位也应读到全局值
        self.assertEqual(main.get_global_vnc_refresh(), "fast")

    def test_post_bad_value_falls_back(self):
        client = TestClient(main.app)
        r = client.post("/api/vnc-refresh", json={"refresh": "bogus"})
        self.assertEqual(r.json()["refresh"], "standard")


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


class PanelUiStateTests(unittest.TestCase):
    """本轮 UI 改动：密钥按需、监控记忆、实例分组、停止弹窗。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_api_key_button_hidden_until_required(self):
        self.assertIn('id="btnKey"', self.src)
        # 初始隐藏，服务端确实启用 API_KEY 后才显示
        btn = self.src[self.src.index('id="btnKey"'):self.src.index('id="btnKey"') + 260]
        self.assertIn('style="display:none"', btn)
        i = self.src.index("function updateKeyLabel()")
        self.assertIn('btn.style.display = authRequired ? "" : "none"', self.src[i:i + 400])

    def test_401_reveals_key_entry(self):
        i = self.src.index("async function api(")
        seg = self.src[i:i + 1100]
        self.assertIn("res.status === 401", seg)
        self.assertIn("authRequired = true", seg)
        self.assertIn("showKeyMask(", seg)

    def test_manual_refresh_button_removed(self):
        self.assertNotIn('<button onclick="load()">刷新</button>', self.src)
        # 10 秒自动轮询仍在，移除手动按钮不会失去状态刷新
        self.assertIn("setInterval(() => { if (!document.hidden && !bgEcoMode) load(true); }, 10000)", self.src)

    def test_monitor_view_persisted(self):
        self.assertIn('const MONITOR_VIEW_KEY = "cdpManagerMonitorView";', self.src)
        self.assertIn("pendingMonitorRestore = loadMonitorView();", self.src)
        i = self.src.index("function toggleMonitorView(")
        seg = self.src[i:i + 700]
        self.assertIn("saveMonitorView()", seg)
        self.assertIn('localStorage.setItem(MONITOR_VIEW_KEY', self.src)
        # 首轮数据到位后恢复，且不重复拉数据
        self.assertIn("toggleMonitorView(true, true)", self.src)

    def test_instances_split_into_temp_and_persist_groups(self):
        self.assertIn('const GROUP_STATE_KEY = "cdpManagerGroups";', self.src)
        self.assertIn('renderInstGroup("temp", "临时实例"', self.src)
        self.assertIn('renderInstGroup("persist", "持久实例"', self.src)
        self.assertNotIn('renderInstGroup("temp", "临时列表"', self.src)
        self.assertIn("i.profile_mode !== \"persistent\"", self.src)
        self.assertIn("i.profile_mode === \"persistent\"", self.src)
        self.assertIn(".inst-group.collapsed .group-body", self.src)
        self.assertIn("function toggleGroup(key)", self.src)
        self.assertIn("localStorage.setItem(GROUP_STATE_KEY", self.src)

    def test_create_and_stop_icons_moved_into_group_heads(self):
        g = self.src.index("const groupBtns = (key, name) =>")
        gseg = self.src[g:g + 900]
        for token in ('data-grp-action="create"', 'data-grp-action="stop"',
                      "openCreate(\\'' + key + '\\')", "openStopDialog(\\'' + key + '\\')",
                      '#i-plus', '#i-stop'):
            self.assertIn(token, gseg)
        i = self.src.index('renderInstGroup("temp"')
        self.assertIn('groupBtns("temp", "临时实例")', self.src[i:i + 200])
        j = self.src.index('renderInstGroup("persist"')
        self.assertIn('groupBtns("persist", "持久实例")', self.src[j:j + 200])
        self.assertIn('document.querySelectorAll(".group-head .grp-btn")', self.src)

    def test_create_dialog_locked_to_clicked_group(self):
        i = self.src.index("function openCreate(group)")
        seg = self.src[i:i + 1700]
        for token in ('const persist = group === "persist"',
                      'textContent = persist ? "新建持久实例" : "新建临时实例"',
                      'eph.hidden = persist', 'eph.disabled = persist',
                      '$("createMode").value = "new"', '$("createMode").value = "ephemeral"',
                      'id="createModeEphemeral"'):
            self.assertIn(token, seg if token != 'id="createModeEphemeral"' else self.src)
        self.assertNotIn("function openCreate() {", self.src)

    def test_stop_dialog_scoped_to_clicked_group(self):
        i = self.src.index("function openStopDialog(group)")
        seg = self.src[i:i + 1200]
        for token in ('.filter(i => persist ? i.profile_mode === "persistent" : i.profile_mode !== "persistent")',
                      'textContent = persist ? "停止持久实例" : "停止临时实例"',
                      '默认全选本组全部持久实例', '默认全选本组全部临时实例'):
            self.assertIn(token, seg)
        self.assertNotIn("function openStopDialog() {", self.src)

    def test_stop_dialog_checks_all_with_full_browser_info(self):
        for token in ('id="stopMask"', 'id="stopList"', 'id="stopSelectAll"',
                      "function openStopDialog(group)", "function confirmStopSelected()",
                      'input type="checkbox" data-sid=', "Promise.allSettled"):
            self.assertIn(token, self.src)
        i = self.src.index("function stopItemHtml(i)")
        seg = self.src[i:i + 2200]
        # 复选项必须展示完整信息，而不只是 sid 前缀
        self.assertIn("esc(i.sid)", seg)
        self.assertIn("statusBadge(i)", seg)
        self.assertIn("kernelBadge(i)", seg)
        self.assertIn("profileBadge(i)", seg)
        self.assertIn("i.container_name", seg)
        self.assertIn("i.created_at", seg)
        self.assertIn("i.cdp_http_proxy", seg)
        self.assertIn("i.vnc_port", seg)
        self.assertIn("i.novnc_port", seg)
        # 远端实例也要能按所属主机路由停止
        self.assertIn('"/instance/" + encodeURIComponent(sid)', self.src)


class PanelEcoAndShareTests(unittest.TestCase):
    """后台省流、远程自动刷新、只读分享的静态回归断言。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "app", "templates", "index.html"), encoding="utf-8") as f:
            cls.src = f.read()
        with open(os.path.join(ROOT, "app", "templates", "share.html"), encoding="utf-8") as f:
            cls.share_src = f.read()
        with open(os.path.join(ROOT, "app", "templates", "cast.html"), encoding="utf-8") as f:
            cls.cast_src = f.read()
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as f:
            cls.main_src = f.read()

    def test_remote_panel_refresh_button_removed_and_auto_poll(self):
        i = self.src.index('id="remoteMask"')
        seg = self.src[i:i + 7000]
        self.assertNotIn('onclick="refreshRemotePanel()">刷新', seg)
        self.assertIn("remotePollTimer = setInterval", self.src)
        self.assertIn("}, 3000);", self.src)
        self.assertIn("clearInterval(remotePollTimer)", self.src)
        self.assertIn("每 3 秒自动刷新", self.src)

    def test_background_eco_disconnect_and_instant_restore(self):
        for token in ("let bgEcoMode = false;", "function enterBgEco()", "function leaveBgEco()",
                      'document.addEventListener("visibilitychange"',
                      'window.addEventListener("blur", enterBgEco)',
                      'window.addEventListener("focus"',
                      "!document.hidden && !bgEcoMode", "state.connect = connect;"):
            self.assertIn(token, self.src)
        i = self.src.index("function enterBgEco()")
        seg = self.src[i:i + 1600]
        self.assertIn("castWs.close()", seg)
        self.assertIn("frameHost", seg)  # iframe 也要移除，VNC 才能真正断流
        j = self.src.index("function leaveBgEco()")
        seg2 = self.src[j:j + 1200]
        self.assertIn("c.connect(0)", seg2)
        self.assertIn("mountFrame(instances[selectedSid])", seg2)

    def test_share_backend_persistence_and_routes(self):
        for token in ('SHARE_FILE = DATA_DIR / "share.json"',
                      "SHARE_TTLS = (3600, 21600, 86400, 604800)",
                      "def share_active(", "def share_allows(",
                      '@app.get("/api/share")', '@app.post("/api/share")',
                      '@app.post("/api/share/renew")', '@app.get("/api/share/view")',
                      '@app.get("/share", response_class=HTMLResponse)',
                      'path == "/api/share/view" and method == "GET"'):
            self.assertIn(token, self.main_src)

    def test_share_ws_is_readonly(self):
        i = self.main_src.index("async def ws_cast(")
        seg = self.main_src[i:i + 1600]
        self.assertIn("readonly_share = share_allows(share_token, sid)", seg)
        self.assertIn("not readonly_share and not key_valid", seg)
        self.assertIn('websocket.query_params.get("readonly", "")', seg)
        j = self.main_src.index('kind = data.get("type")', i)
        self.assertIn('if readonly_share and kind in ("input", "viewport")',
                      self.main_src[j:j + 400])
        # 远端代理必须把 share 换成 readonly=1，否则远端会用 host key 认证成可操控连接
        k = self.main_src.index("async def remote_ws_proxy(")
        seg3 = self.main_src[k:k + 1600]
        self.assertIn("is_share = share_active(share_token)", seg3)
        self.assertIn('query_items.append(("readonly", "1"))', seg3)
        self.assertIn('if k not in ("key", "share", "readonly")', seg3)
        # 远端 WS 端点只从 query 读 key（不读 X-API-Key 头），host key 必须进 query，
        # 否则远端设了 API_KEY 时抓屏/devtools/noVNC 一律 4401。
        self.assertIn('query_items.append(("key", host["key"]))', seg3)

    def test_cast_mouse_coords_map_to_real_viewport(self):
        # 画质档会缩小图片；点击必须经「图片像素 -> 真实视口」两步换算，
        # 否则键盘可用但鼠标点不中（旧代码把图片像素直接当 CDP 坐标）。
        for token in ('bindCastInput(canvas, wsRef, sel, viewport)',
                      'const scale = Math.min(r.width / iw, r.height / ih)',
                      'const offX = (r.width - drawW) / 2',
                      'ix * vw / iw', 'iy * vh / ih',
                      'viewport.w = msg.vw', 'viewport.h = msg.vh',
                      'point(ev, dragging)', 'document.addEventListener("mouseup"',
                      'if (!p) return;'):
            self.assertIn(token, self.src)
        self.assertNotIn("canvas.width / r.width", self.src)
        # 后端必须把浏览器真实视口尺寸随帧推送给前端
        for token in ('"Page.getLayoutMetrics"', 'cssLayoutViewport',
                      'metadata.get("deviceWidth")', '"vw": viewport_w',
                      '"vh": viewport_h'):
            self.assertIn(token, self.main_src)

    def test_cast_full_mouse_and_keyboard_input(self):
        # 鼠标功能要对齐 VNC：侧键、组合按钮、修饰键、滚轮缩放都要下发到 CDP。
        i = self.src.index("function bindCastInput(")
        j = self.src.index("function castSig(", i)
        seg = self.src[i:j]
        for token in (
            'n === 3 ? "back" : n === 4 ? "forward"',  # 前进/后退侧键
            'const buttonBits = (n) => n === 2 ? 2 : n === 1 ? 4',
            ': n === 3 ? 8 : n === 4 ? 16 : 1',  # 侧键 buttons 位
            'const mods = (e) =>',  # 修饰键 Alt/Ctrl/Meta/Shift
            'buttons: e.buttons || buttonBits(e.button)',  # 按下保留组合状态
            'buttons: e.buttons, modifiers: mods(e)',  # 释放也带修饰键
            'type: "mousePressed"', 'type: "mouseReleased"', 'type: "mouseMoved"',
            'clickCount: e.detail || 1',  # 单击/双击/三击
            'e.deltaMode === 1 ? 40',  # 行/页滚动量归一
            'deltaX: dx, deltaY: dy',  # 滚轮走 dispatchMouseEvent + 小步长放大
            'pointerType: "mouse"',
            'modifiers: mods(e),\n                });',  # Ctrl+滚轮缩放
            'canvas.addEventListener("auxclick"',  # 中键自动滚动本地拦截
            'canvas.addEventListener("contextmenu"',  # 右键菜单本地拦截
            'modifiers: mods(e),\n                    text:',  # 键盘组合键
        ):
            self.assertIn(token, seg)
        # 所有输入方法必须留在后端白名单里，否则前端发了也被丢弃
        j = self.main_src.index('kind == "input":')
        seg2 = self.main_src[j:j + 700]
        for method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent",
                       "Input.insertText", "Input.dispatchWheelEvent"):
            self.assertIn(method, seg2)

    def test_share_admin_ui_drag_and_reorder(self):
        for token in ('id="btnShare"', 'id="shareMask"', 'id="shareDrop"',
                      'id="shareList"', 'id="shareTtl"', "function renderSharePanel()",
                      "function dropToShare(ev)", "function dropOnShareItem(ev, targetSid)",
                      "function dropToMonitor(ev)", "function syncMonitorOrder()",
                      '"/api/share/renew"', 'ondrop="dropToMonitor(event)"',
                      'card.draggable = true', 'draggable="true"'):
            self.assertIn(token, self.src)

    def test_share_card_shows_full_instance_info(self):
        # 分享卡片只显示 sid+主机时多个实例难以区分，须与左侧列表一致显示完整信息。
        i = self.src.index("function shareItemHtml(")
        seg = self.src[i:i + 1400]
        for token in ("statusBadge(i)", "kernelBadge(i)", "profileBadge(i)",
                      'i.debug_port', "i.host_name || i.host", "share-sid", "share-meta"):
            self.assertIn(token, seg)

    def test_share_left_list_excludes_shared_instances(self):
        # 拖进分享区后左侧实例应立即消失，只有未分享的才留在左侧。
        i = self.src.index("function renderSharePanel(")
        seg = self.src[i:i + 1100]
        self.assertIn("const pending = all.filter(i => !chosen.includes(i.sid));", seg)
        self.assertIn('$("shareAllCount").textContent = pending.length', seg)
        self.assertIn("pending.map(i => shareItemHtml(i, false, false))", seg)
        self.assertIn("可分享实例", self.src)

    def test_scrollbars_are_dark_and_unified(self):
        # 深色主题里不应出现浏览器默认的亮色滚动条；管理页与观看页都要有统一样式。
        for src in (self.src, self.share_src):
            self.assertIn("::-webkit-scrollbar-thumb", src)
            self.assertIn("scrollbar-color:", src)
            self.assertIn("--scroll-thumb", src)
        self.assertIn("::-webkit-scrollbar-corner", self.src)
        self.assertIn("::-webkit-scrollbar-corner", self.share_src)

    def test_cast_wheel_event_has_required_type(self):
        # 滚轮必须走 dispatchMouseEvent + type:mouseWheel（Puppeteer/Playwright 同款路径），
        # 旧的 dispatchWheelEvent 在抓屏链路里不生效，已弃用。
        i = self.src.index('// 滚轮走 CDP 的 dispatchMouseEvent')
        seg = self.src[i:i + 520]
        self.assertIn('"Input.dispatchMouseEvent"', seg)
        self.assertIn('type: "mouseWheel"', seg)
        self.assertIn('pointerType: "mouse"', seg)
        self.assertIn("deltaX: dx, deltaY: dy", seg)
        self.assertIn("MIN_STEP = 20", self.src)
        self.assertIn("point(e, true)", self.src)
        # 画面光标用普通箭头，不再是 crosshair。
        self.assertIn("cursor: default;", self.src)
        self.assertNotIn("cursor: crosshair;", self.src)

    def test_tabs_sorted_by_open_order_backend(self):
        # 后端按打开顺序返回标签：新标签追加末尾、切换不改变顺序、关闭后相对顺序不变。
        for token in ('TAB_ORDER_FILE = DATA_DIR / "tab_order.json"',
                      "def sort_targets_by_open_order(sid: str, pages: list)",
                      'ordered_ids = [tid for tid in old if tid in known]',
                      "ordered_ids.extend(reversed(fresh))",
                      "return [known[tid] for tid in ordered_ids]",
                      'return {"targets": sort_targets_by_open_order(sid, pages)}'):
            self.assertIn(token, self.main_src)

    def test_new_tab_auto_switch_and_restore_button(self):
        # 新标签（含脚本弹出）自动切到最新打开的；VNC 同步激活。
        i = self.src.index("async function pollCastTargets(")
        seg = self.src[i:i + 2200]
        self.assertIn("const added = targets.filter(t => !knownIds.has(t.id));", seg)
        self.assertIn("const newest = added[added.length - 1].id;", seg)
        self.assertIn("setPreferredTid(newest)", seg)
        self.assertIn('"/targets/" + encodeURIComponent(newest) + "/activate"', seg)
        # X 直接关闭：closeTargetById 内不应再出现 confirm。
        j = self.src.index("async function closeTargetById(")
        seg2 = self.src[j:j + 1300]
        self.assertNotIn("confirm(", seg2)
        # 恢复上次关闭标签：栈持久化 + 允许连续恢复 + 导航条图标按钮（VNC 共用）。
        for token in ('const CLOSED_TABS_KEY = "cdpManagerClosedTabs"',
                      "function pushClosedTab(sid, url, title)",
                      "async function restoreClosedTab()",
                      "list.pop()", "pushClosedTab(sel.sid, target.url, target.title)",
                      'id="btnRestoreTab"', "#i-restore", "loadClosedTabs();"):
            self.assertIn(token, self.src)

    def test_vnc_auto_switch_toggle_and_screen_lock_restore(self):
        # VNC 新标签自动切换默认关；屏幕锁期间挂起，解锁后恢复用户原状态。
        for token in (
            "autoSwitchTab: s.autoSwitchTab === true",
            "autoSwitchTab: false, quality: suggestQuality()",
            "let autoSwitchBeforeLock = null;",
            "function autoSwitchEnabled()",
            "settings.autoSwitchTab === true && !screenLocked",
            "function toggleAutoSwitchTab()",
            'id="setAutoSwitchTab"',
            'id="btnAutoSwitch"',
            '"自动切:暂停"',
            # 文字按钮不能套 btn-nav（固定 30px 宽会挤成一团）；状态用圆点区分。
            'class="btn-mini btn-nav btn-auto-switch',  # 断言该冲突写法已不存在
        ):
            if token.startswith('class="btn-mini btn-nav btn-auto-switch'):
                self.assertNotIn(token, self.src)
            else:
                self.assertIn(token, self.src)
        for token in (
            '.castbar .btn-auto-switch {',
            'width: auto; height: 28px; padding: 0 10px;',
            '.castbar .btn-auto-switch::before {',
            '.castbar .btn-auto-switch.on::before { background: var(--accent); }',
            '.castbar .btn-auto-switch.paused {',
            'const autoSwitchState = screenLocked ? " paused"',
            'class="btn-mini btn-auto-switch\' + autoSwitchState',
        ):
            self.assertIn(token, self.src)

        # 新标签分支必须受开关约束，且只在 VNC 模式生效。
        i = self.src.index("async function pollCastTargets(")
        seg = self.src[i:i + 3000]
        self.assertIn("if (vncMode && !autoSwitchEnabled())", seg)
        self.assertIn("renderCastBar(sel, true);", seg)
        # 当前标签被外部关掉的兜底激活：屏幕锁期间也跳过。
        self.assertIn("if (!screenLocked) {", seg)

        # 屏幕锁：上锁记录用户原状态，解锁恢复（不写死为开/关）。
        j = self.src.index("function toggleScreenLock()")
        seg2 = self.src[j:j + 1400]
        self.assertIn(
            "if (autoSwitchBeforeLock === null) autoSwitchBeforeLock = settings.autoSwitchTab === true;",
            seg2,
        )
        self.assertIn("settings.autoSwitchTab = autoSwitchBeforeLock;", seg2)
        self.assertIn("autoSwitchBeforeLock = null;", seg2)

        # 手动激活/实例切换时也不在屏幕锁期间抢焦点。
        k = self.src.index("async function mountVncNav(")
        seg3 = self.src[k:k + 1400]
        self.assertIn("if (changed && !screenLocked)", seg3)

    def test_standalone_cast_page(self):
        # 后端 /cast 页面路由：免鉴权打开，数据层各自鉴权。
        self.assertIn('@app.get("/cast", response_class=HTMLResponse)', self.main_src)
        self.assertIn('templates.TemplateResponse(request, "cast.html", {})', self.main_src)
        # 独立抓屏页：纯画面 + 简易导航条，?sid=&tid= 直达，同源继承密钥与设置。
        c = self.cast_src
        self.assertIn("const SID = QS.get(\"sid\") || \"\";", c)
        self.assertIn("const TID_HINT = QS.get(\"tid\") || \"\";", c)
        self.assertIn('localStorage.getItem(KEY_STORAGE)', c)
        self.assertIn('localStorage.getItem(SETTINGS_KEY)', c)
        self.assertIn('QS.get("key")', c)
        # 复用 manager 的抓屏 WS 通道（含画质/方式/节奏参数）。
        self.assertIn('inst.cast_ws || ("/ws/cast/" + inst.sid + "/{tid}")', c)
        self.assertIn('"&mode=" + captureMode + "&snap=" + encodeURIComponent(snapRate)', c)
        # 远端实例：实例级 API 加 /remote/<host> 前缀。
        self.assertIn('function scopeUrl(url)', c)
        self.assertIn('"/remote/" + encodeURIComponent(host) + url', c)
        # 完整鼠标键盘 + 滚轮（走 dispatchMouseEvent + mouseWheel，与 Puppeteer 同路径）。
        self.assertIn('type: "mouseWheel"', c)
        self.assertIn('pointerType: "mouse"', c)
        self.assertIn('Input.dispatchKeyEvent', c)
        self.assertIn('Input.dispatchMouseEvent', c)
        self.assertIn('const MIN_STEP = 20;', c)
        # 简易导航条：标签切换/关闭、地址栏、前进后退、刷新。
        for token in (
            'id="tabRow"', 'id="navRow"', 'id="castUrl"',
            'function switchTarget(id)', 'function closeTarget(id)',
            'function navigate(raw)', 'function reloadPage()', 'function historyStep(delta)',
            'Page.getNavigationHistory', 'Page.navigateToHistoryEntry',
            'function newTarget()', 'function refreshTargets()',
        ):
            self.assertIn(token, c)
        # 无屏幕锁等管理页专有控件：独立页是纯观看 + 操控画面，不介入脚本前台。
        self.assertNotIn("screenLocked", c)

    def test_cast_frame_drawing_no_flicker(self):
        # Cast 模式两路帧尺寸不同：screencast 按画质档缩放（960x540/1280x720），
        # captureScreenshot 保底帧是原始视口（1920x1080）。旧前端每帧按图片尺寸
        # 重设 canvas，重设会先清空画布露出深色背景 → 周期性黑闪；异步 Image
        # onload 还会让旧帧后画覆盖新帧。三处绘制统一改为：帧序号丢弃过期帧，
        # canvas 只在宽高比真正变化时重设，其余帧缩放绘制。
        for src, label in ((self.src, "index.html"), (self.cast_src, "cast.html")):
            self.assertIn("frameSeq", src, label)
            self.assertIn("drawnSeq", src, label)
            self.assertIn("seq <= drawnSeq", src, label)
            self.assertIn("Math.abs(ratio - cw / ch) > 0.02", src, label)
            self.assertIn("drawImage(img, 0, 0, canvas.width, canvas.height)", src, label)
            self.assertNotIn("canvas.width !== img.width || canvas.height !== img.height", src, label)
        # 管理页两处各自维护序号：主抓屏是挂载级变量，监控是每张卡片 state 上的字段。
        self.assertIn("let frameSeq = 0, drawnSeq = 0;", self.src)
        self.assertIn("frameSeq: 0, drawnSeq: 0", self.src)
        self.assertIn("++state.frameSeq", self.src)
        self.assertIn("seq <= state.drawnSeq", self.src)

    def test_audit_fixes_cast_auth_remote_key_and_lifecycle(self):
        # 1) /cast 页面与首页/分享页一样免鉴权打开：路由注释、鉴权白名单必须一致。
        #    数据层（/api/list、/api/instance/*/cmd、/ws/cast）仍各自鉴权。
        self.assertIn('if path in ("/", "/api/health", "/share", "/cast"):', self.main_src)

        # 2) 后台 CDP 监控线程整轮 try/except：Docker 偶发异常不能让线程永久死亡。
        i = self.main_src.index("def cdp_monitor_loop():")
        seg = self.main_src[i:i + 2600]
        self.assertIn("while True:", seg)
        self.assertIn("try:", seg)
        self.assertIn("[CDP 监控] 本轮探测异常，8 秒后重试: %s", seg)
        self.assertIn("time.sleep(8)", seg)

        # 3) 独立抓屏页：地址栏是静态 DOM，轮询只改值不重建；输入中不被冲掉。
        c = self.cast_src
        self.assertIn('<input class="url" id="castUrl"', c)
        self.assertIn('if (document.activeElement !== urlBox) urlBox.value = cur.url || "";', c)
        self.assertNotIn('$("navRow").innerHTML', c)
        # 断线自动重试 2 次（600ms/1200ms），与管理页一致。
        self.assertIn("function connect(attempt)", c)
        self.assertIn("if (attempt < 2)", c)
        self.assertIn("600 * (attempt + 1)", c)
        self.assertIn("setTimeout(() => { if (myGen === gen) connect(attempt + 1);", c)

        # 4) 管理页 document mouseup 监听可解绑：重挂载前移除，避免切换实例累积。
        self.assertIn("let castDocMouseup = null;", self.src)
        self.assertIn('document.removeEventListener("mouseup", castDocMouseup)', self.src)
        self.assertIn('document.addEventListener("mouseup", castDocMouseup)', self.src)

        # 5) HTTP fetch 只带 X-API-Key 头，不再把密钥拼进 URL（WS/脚本展示除外）。
        self.assertNotIn('fetch(keyQuery("/api/hosts")', self.src)
        self.assertNotIn('fetch(keyQuery("/api/link/token")', self.src)
        for token in (
            'fetch("/api/hosts", { headers: authHeaders() })',
            'fetch("/api/link/token", { headers: authHeaders() })',
            'fetch(scopeUrl(url), opts)',
        ):
            self.assertIn(token, self.src)

    def test_snippet_modal_width_overrides_base_panel(self):
        # 脚本弹窗要宽于 .panel 基类(440px)。基类规则在文件后面定义，特异性相同会覆盖，
        # 因此必须用 .panel.snippet-panel 提高特异性，并显式给 max-width 覆盖 92vw。
        self.assertIn(".panel.snippet-panel { width: min(1280px, 96vw); max-width: 96vw; }", self.src)
        self.assertNotIn("\n        .snippet-panel { width: min(1280px, 96vw); }", self.src)
        # 窄屏断点里同样要提高特异性，否则仍被基类的 440px 压住。
        self.assertIn(".panel.snippet-panel { width: 96vw; }", self.src)

    def test_detail_cdp_quick_links_and_current_page(self):
        # 原「新窗口打开」→「当前网页」已按用户要求移除：详情栏只留画面直达按钮。
        # 「CDP 信息」仍在左侧实例列表项右上角，不再把 JSON 当画面入口。
        self.assertNotIn("function currentPageUrl()", self.src)
        self.assertNotIn("function openCurrentPage()", self.src)
        self.assertNotIn('">当前网页 ↗</button>', self.src)
        self.assertNotIn('>新窗口打开</a>', self.src)
        # 详情栏画面按钮组：主按钮 + 箭头下拉，菜单里选「VNC 画面 / 抓屏画面」。
        self.assertIn('class="view-group" id="viewGroup"', self.src)
        self.assertIn("function toggleViewMenu(ev)", self.src)
        self.assertIn("function openDefaultView()", self.src)
        self.assertIn('onclick="toggleViewMenu(event)"', self.src)
        self.assertIn("'画面 ↗</button>'", self.src)
        self.assertIn(">VNC 画面 ↗</button>", self.src)
        self.assertIn(">抓屏画面 ↗</button>", self.src)
        self.assertNotIn(">桌面画面 ↗</button>", self.src)
        self.assertNotIn(">网页画面 ↗</button>", self.src)
        self.assertIn('(!headless && vncUrl(sel)', self.src)
        # VNC 画面：非无头才在菜单项渲染，打开 noVNC 地址。
        self.assertIn("function openVncPage()", self.src)
        self.assertIn('onclick="openVncPage()"', self.src)
        # 抓屏画面：打开独立 /cast?sid=..&tid=.. 页面，不再走管理页 ?preview=cast 直达。
        self.assertIn("function openCastPage()", self.src)
        self.assertIn('onclick="openCastPage()"', self.src)
        self.assertIn('window.open(base + "/cast" + q, "_blank", "noopener");', self.src)
        self.assertNotIn("function castPageQuery(sel)", self.src)
        self.assertNotIn('"&preview=cast"', self.src)
        self.assertNotIn('q.delete("sid"); q.delete("tid"); q.delete("preview")', self.src)
        # 远端实例跳到那台主机自己的面板（不是当前面板经代理）：从 hosts 按 host id 取 base。
        self.assertIn("function instancePanelBase(i)", self.src)
        self.assertIn('const h = (hosts || []).find(x => x.id === i.host);', self.src)
        self.assertIn("const base = instancePanelBase(sel) || location.origin;", self.src)
        # 实例列表项主机 badge 可点跳对端面板，点链接不触发选中实例。
        self.assertIn('class="badge host badge-host-link"', self.src)
        self.assertIn("esc(instancePanelBase(i) + \"/\")", self.src)
        self.assertIn('a.badge-host-link:hover', self.src)
        # CDP 信息移到左侧实例列表项右上角（红框位置），点它不切换当前实例。
        self.assertIn("function cdpVersionUrl(i)", self.src)
        self.assertIn('class="inst-cdp-link"', self.src)
        self.assertIn("openCdpInfo(", self.src)
        self.assertNotIn('title="浏览器版本与 webSocketDebuggerUrl">CDP 信息 ↗</a>', self.src)

        # 详情栏第二行快捷链接：版本 / 标签 / 协议 / VNC / noVNC / 文档 / WS 复制。
        for token in (
            'class="detail-links"',
            '{ label: "版本信息", url: versionUrl',
            'cdpRoot + "/json/list"',
            'cdpRoot + "/json/protocol"',
            'sel.vnc_url_proxy || sel.vnc_url',
            "vncUrl(sel)",
            "chromedevtools.github.io/devtools-protocol",
            'onclick="copyBrowserWs()"',
            "async function copyBrowserWs()",
            "data.webSocketDebuggerUrl",
        ):
            self.assertIn(token, self.src)

    def test_cdp_info_panel_and_script_copy(self):
        # CDP 信息弹窗：结构化展示 /json/version，每项可复制，不再跳原始 JSON。
        for token in (
            'id="cdpInfoMask"', 'id="cdpInfoBody"',
            "async function openCdpInfo(sid)", "async function refreshCdpInfo()",
            "function closeCdpInfo()", "cdp-info-row",
            'd["Protocol-Version"]', 'd["User-Agent"]', 'd["V8-Version"]',
            'd["WebKit-Version"]', "d.webSocketDebuggerUrl", "copyTextValue(",
        ):
            self.assertIn(token, self.src)

        # 当前页 WS：代理重写后的 WS + 自动附加 API_KEY。
        for token in (
            "function cdpRootOf(i)",
            "async function copyTextValue(text, label)",
            "async function copyCurrentPageWs()",
            '"/json/list"',
            "t.webSocketDebuggerUrl || t.wssUrl || t.wsUrl",
        ):
            self.assertIn(token, self.src)
        self.assertNotIn("withKey(", self.src)  # 曾经的 bug：未定义的 withKey

    def test_script_snippet_panel_curlconverter_style(self):
        # 脚本代码生成弹窗：类 curlconverter，选库实时生成连接代码，自动附 API_KEY。
        for token in (
            'id="snippetMask"', 'id="snippetWs"', 'id="snippetLibs"',
            'id="snippetCode"', 'id="snippetNote"', 'id="snippetSid"',
            "function openSnippet()", "function closeSnippet()",
            "async function loadSnippetWs()", "async function copySnippetWs()",
            "function renderSnippetLibs()", "function selectSnippetLib(key)",
            "function renderSnippetCode()", "async function copySnippetCode()",
            "const SNIPPET_LIBS = [",
            # 五个库都在列表里
            'key: "pydoll"', 'key: "websocket"', 'key: "playwright"',
            'key: "nodriver"', 'key: "drission"',
            # 各库的关键 API 片段
            "from pydoll.sync import Chrome", "browser.connect(WS)",
            "import json, websocket", "websocket.create_connection(WS)",
            "from playwright.sync_api import sync_playwright",
            "p.chromium.connect_over_cdp(WS)",
            "import nodriver as uc", "await uc.connect(WS)",
            "from DrissionPage import Chromium",
            # WS 自动附 key：修复用现成的 keyQuery，不再有未定义的 withKey
            "$(\"snippetWs\").value = keyQuery(ws)",
            "onclick=\"openSnippet()\"", "oninput=\"renderSnippetCode()\"",
        ):
            self.assertIn(token, self.src)

    def test_viewer_page_is_readonly_with_grid_and_zoom(self):
        for token in ('id="grid"', 'id="large"', "function openLarge(sid)",
                      "function closeLarge()", 'wsUrl(inst, "monitor")',
                      'wsUrl(inst, "low")', "&mode=cast&share=",
                      "tickExpire", "只读画面"):
            self.assertIn(token, self.share_src)
        # 观看端绝不绑定输入事件、导航或管理控件
        for bad in ("dispatchMouseEvent", "dispatchKeyEvent", "Input.insertText",
                    "castSend(", "stopAll(", "openCreate("):
            self.assertNotIn(bad, self.share_src)


class TabOrderTests(unittest.TestCase):
    """标签打开顺序：新标签追加末尾，切换不动，关闭后相对顺序保持。"""

    def setUp(self):
        self.saved = dict(main.TAB_ORDER)

    def tearDown(self):
        main.TAB_ORDER.clear()
        main.TAB_ORDER.update(self.saved)

    def test_open_order_and_close(self):
        sid = "test-tab-order"
        main.TAB_ORDER[sid] = []
        with patch.object(main, "save_tab_order", return_value=True):
            p = lambda tid: {"id": tid, "title": tid, "url": "", "type": "page"}
            # CDP 常把最近激活放最前；首次调用反转后 oldest 在左。
            got = main.sort_targets_by_open_order(sid, [p("c"), p("a"), p("b")])
            self.assertEqual([t["id"] for t in got], ["b", "a", "c"])
            # 切换顺序（CDP 顺序变了）不改变已知标签的相对位置
            got = main.sort_targets_by_open_order(sid, [p("a"), p("c"), p("b")])
            self.assertEqual([t["id"] for t in got], ["b", "a", "c"])
            # 新弹出的标签追加到最右
            got = main.sort_targets_by_open_order(sid, [p("d"), p("a"), p("c"), p("b")])
            self.assertEqual([t["id"] for t in got], ["b", "a", "c", "d"])
            # 关闭一个后其余相对顺序不变
            got = main.sort_targets_by_open_order(sid, [p("d"), p("c"), p("b")])
            self.assertEqual([t["id"] for t in got], ["b", "c", "d"])


@unittest.skipUnless(HAS_TESTCLIENT, "缺少 fastapi/httpx，跳过集成测试")
class ShareBackendTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.saved = dict(main.SHARE_STATE)
        self.saved["sids"] = list(self.saved["sids"])

    def tearDown(self):
        main.SHARE_STATE.clear()
        main.SHARE_STATE.update(self.saved)

    def test_share_view_requires_valid_token(self):
        r = self.client.get("/api/share/view")
        self.assertEqual(r.status_code, 403)
        self.assertIn("过期", r.json()["detail"])

    def test_share_token_expiry_and_sid_scope(self):
        main.SHARE_STATE["token"] = "unit-token"
        main.SHARE_STATE["expires"] = int(time.time()) + 60
        main.SHARE_STATE["sids"] = ["abc"]
        self.assertTrue(main.share_active("unit-token"))
        self.assertTrue(main.share_allows("unit-token", "abc"))
        self.assertFalse(main.share_allows("unit-token", "other"))
        main.SHARE_STATE["expires"] = int(time.time()) - 1
        self.assertFalse(main.share_active("unit-token"))

    def test_share_admin_requires_key_when_api_key_set(self):
        with patch.object(main, "API_KEY", "secret"):
            self.assertEqual(self.client.get("/api/share").status_code, 401)
            self.assertEqual(self.client.post("/api/share", json={}).status_code, 401)
            self.assertEqual(self.client.post("/api/share/renew").status_code, 401)


if __name__ == "__main__":
    print("集成测试:", "启用" if HAS_TESTCLIENT else "跳过（本地缺少 fastapi/httpx）")
    unittest.main(verbosity=2)
