/**
 * 自研九面板移除验证（静态）。
 * 原自研九面板 CDP 复刻已彻底删除，改由浏览器原生 DevTools 承担。
 * 本测试断言：九面板 DOM/函数确实不存在；原生 DevTools 代理链与 Python 注入保留。
 * 运行：node test_devtools_full.js
 */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "app", "templates", "index.html"), "utf8");
const main = fs.readFileSync(path.join(__dirname, "app", "main.py"), "utf8");

let passed = 0, failed = 0;
function ok(cond, msg) {
  if (cond) { passed++; console.log("  ok  -", msg); }
  else { failed++; console.log("  FAIL-", msg); }
}

console.log("== 自研九面板移除验证 ==");

// 1. 自研九面板 DOM 全部移除（sideTabDev 保留：承载原生 DevTools iframe）
const domTokens = ["dtTabbar", "capWrap", "capCtxMenu", "dtPanelElements", "dtPanelSources",
  "dtPanelPerformance", "dtFlameSvg", "dtPanelApplication", "devMask"];
domTokens.forEach(t => ok(!html.includes('id="' + t + '"'), "DOM 移除: " + t));
// 原生 DevTools 内嵌到抽屉
ok(html.includes('id="sideTabDev"'), "开发者工具 tab 保留");
ok(html.includes('id="devToolsFrame"'), "原生 DevTools iframe 保留");

// 2. 九面板 JS 函数全部移除
const fnTokens = ["dtSwitch", "dtCmd", "dtDispatch", "dtPerfToggle", "dtLoadProfile",
  "dtFlameRender", "dtHeapSnapshot", "dtScanSecrets", "dtExport", "capConnect", "capGenCurl"];
fnTokens.forEach(fn => ok(!html.includes("function " + fn + "("), "函数移除: " + fn));

// 3. 原生 DevTools 入口与代理链保留
ok(html.includes("async function openDev()"), "openDev 入口保留");
ok(html.includes("await openNativeDevTools();"), "openDev 打开原生 DevTools");
ok(html.includes('switchSideView("dev")'), "openDev 切到抽屉 dev 视图");
ok(html.includes("async function openNativeDevTools()"), "原生入口函数保留");
ok(html.includes("frame.src = t.url"), "原生 DevTools 内嵌到 iframe");
ok(html.includes("/devtools/inspector.html?ws="), "inspector.html 代理保留");
// sandbox 防跳出：不给顶层导航，保留脚本/同源/剪贴板
const frameSeg = html.split('id="devToolsFrame"')[1].split(">")[0];
ok(!frameSeg.includes("allow-top-navigation"), "iframe 拒绝顶层导航");
ok(frameSeg.includes("allow-same-origin"), "iframe 保留同源");
ok(frameSeg.includes("allow-clipboard-write"), "iframe 保留复制能力");
ok(main.includes('@app.api_route("/{port:int}/devtools/{rest:path}"'), "后端 DevTools 代理保留");
ok(main.includes("def inject_devtools_ws_normalizer("), "ws 参数修正保留");
ok(main.includes("DEVTOOLS_PY_SNIPPET"), "原生右键 Python 注入保留");

// 4. 语言热切换保留、无刷新
ok(html.includes("function applyLang(lang)"), "i18n 应用函数保留");
ok(html.includes("const I18N"), "i18n 字典保留");
ok(!html.includes("location.reload"), "无强制刷新");

console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
if (failed) process.exit(1);
