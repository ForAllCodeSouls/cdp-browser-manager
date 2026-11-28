/**
 * 自研抓包面板移除验证（jsdom + 静态）。
 * 原抽屉内中文抓包面板已删除；抓包能力由浏览器原生 DevTools 的 Network 面板承担，
 * 原生右键「复制为 Python」由后端注入保留。
 * 运行：node test_capture_panel.js
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "app", "templates", "index.html"), "utf8");
const main = fs.readFileSync(path.join(__dirname, "app", "main.py"), "utf8");

let passed = 0, failed = 0;
function ok(cond, msg) {
  if (cond) { passed++; console.log("  ok  -", msg); }
  else { failed++; console.log("  FAIL-", msg); }
}
function eq(a, b, msg) { ok(a === b, `${msg} (期望 ${b}, 实际 ${a})`); }

console.log("== 自研抓包面板移除验证 ==");

// 1. 静态：抓包面板 DOM 与函数移除
["capWrap", "capNetBody", "capCtxMenu", "capBtnConn", "capFilter"].forEach(id =>
  ok(!html.includes('id="' + id + '"'), "DOM 移除: " + id));
["capToggleConnect", "capGenCurl", "capGenPython", "capCtxAction", "capRender"].forEach(fn =>
  ok(!html.includes("function " + fn + "("), "函数移除: " + fn));

// 2. 静态：原生 DevTools 的 Python 复制注入保留
ok(main.includes("DEVTOOLS_PY_ANCHOR"), "原生 Python 注入锚点保留");
ok(main.includes("import requests") && main.includes("import httpx"), "requests/httpx 代码生成保留");
ok(main.includes("await r.requestFormData()"), "异步请求体读取保留");

// 3. jsdom：抽屉含日志 + 原生 DevTools iframe，初始不弹任何窗口
{
  const vc = new VirtualConsole(); vc.on("jsdomError", () => {});
  let opened = [];
  const dom = new JSDOM(html, {
    runScripts: "dangerously", url: "https://example.test/", virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async () => ({ ok: true, json: async () => ({}) });
      w.WebSocket = class { constructor() {} close() {} send() {} };
      w.open = (u) => { opened.push(u); return {}; };
    }
  });
  const w = dom.window;
  w.eval(`
    instances = { sid1: { sid:"sid1", status:"running", debug_port:9333, host:"local" } };
    selectedSid = "sid1";
    apiKey = "";
    api = async (url) => url.endsWith("/targets") ? { targets:[{id:"tid123",type:"page"}] } : {};
    load = async () => {};
    render = function () {};
    loadLogs = async () => {};
    applyTheme = function () {};
    setPreview = function () {};
  `);
  const d = w.document;
  ok(!!d.getElementById("sideViewLog"), "运行日志视图存在");
  ok(!d.getElementById("devMask"), "开发者工具自研视图不存在");
  ok(!!d.getElementById("devToolsFrame"), "原生 DevTools iframe 存在");
  eq(opened.length, 0, "初始未打开任何窗口");
  w.close();
}

console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
if (failed) process.exit(1);
