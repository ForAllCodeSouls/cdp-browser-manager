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
        self.assertGreaterEqual(len(set(masks)), 19)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
