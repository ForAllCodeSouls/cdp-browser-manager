/**
 * 开发者工具入口测试（jsdom）。
 * 当前契约：自研九面板已彻底移除；开发者工具 = 浏览器原生 DevTools 独立窗口
 * （F12 同款）。openDev 与 D 键统一打开该窗口；右侧抽屉只留运行日志；
 * 全应用中/英/日语言热切换即时生效、不刷新。
 * 运行：node test_devtools_ui.js
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "app", "templates", "index.html"), "utf8");

let passed = 0, failed = 0;
function ok(cond, msg) {
  if (cond) { passed++; console.log("  ok  -", msg); }
  else { failed++; console.log("  FAIL-", msg); }
}
function eq(a, b, msg) { ok(a === b, `${msg} (期望 ${JSON.stringify(b)}, 实际 ${JSON.stringify(a)})`); }
const tick = () => new Promise(r => setTimeout(r, 10));

let openUrls = [];
function makeDom() {
  openUrls = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", () => {});
  const dom = new JSDOM(html, {
    runScripts: "dangerously", url: "https://example.test/", virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async () => ({ ok: true, json: async () => ({}) });
      w.WebSocket = class {
        constructor(url) { this.url = url; w.FakeWSLast = this; }
        close() { this.closed = true; if (this.onclose) this.onclose(); }
        send() {}
      };
      w.alert = () => {}; w.confirm = () => {};
      w.open = (url) => { openUrls.push(url); return {}; };
      // 拦截 reload：语言切换绝不能刷新页面
      w.__reloadHits = 0;
    }
  });
  const w = dom.window;
  w.sessionStorage.setItem("tabs_sid1", JSON.stringify([{ id: "tid123", type: "page" }]));
  w.eval(`
    instances = { sid1: { sid:"sid1", status:"running", debug_port:9333, host:"local" } };
    selectedSid = "sid1";
    apiKey = "testkey";
    api = async (url) => url.endsWith("/targets") ? { targets:[{id:"tid123",type:"page"}] } : {};
    load = async () => {};
    render = function () {};
    loadLogs = async () => {};
    applyTheme = function () {};
    setPreview = function () {};
  `);
  return w;
}

(async function main() {
  console.log("== 开发者工具入口测试（原生 DevTools 独立窗口）==");

  // 1. openDev 主入口打开原生 DevTools 独立窗口，不开抽屉
  {
    const w = makeDom();
    await w.openDev();
    await tick();
    eq(openUrls.length, 1, "openDev 打开一个原生窗口");
    ok((openUrls[0] || "").includes("/devtools/inspector.html"), "openDev 指向原生 DevTools");
    ok(!w.document.getElementById("logDrawer").classList.contains("open"), "openDev 不再打开自研抽屉");
    w.close();
  }

  // 2. 未选中运行实例时不打开窗口
  {
    const w = makeDom();
    w.eval('selectedSid = "none";');
    await w.openDev();
    await tick();
    eq(openUrls.length, 0, "未选中运行实例时不打开原生窗口");
    w.close();
  }

  // 3. 自研面板 DOM 已彻底移除
  {
    const w = makeDom();
    const d = w.document;
    ok(!d.getElementById("devMask"), "devMask 已移除");
    ok(!d.getElementById("capWrap"), "capWrap 已移除");
    ok(!d.getElementById("dtTabbar"), "dtTabbar 已移除");
    ok(!d.getElementById("sideTabDev"), "sideTabDev 抽屉 tab 已移除");
    ok(!!d.getElementById("sideViewLog"), "运行日志视图保留");
    ok(typeof w.openNativeDevTools === "function", "原生 DevTools 入口保留");
    w.close();
  }

  // 4. 原生 DevTools URL 走同源代理、page 级 CDP、带 API_KEY、ws 不带协议前缀
  {
    const w = makeDom();
    w.eval('devSid = "sid1";');
    await w.openNativeDevTools();
    await tick();
    eq(openUrls.length, 1, "原生入口打开一个窗口");
    const url = openUrls[0] || "";
    ok(url.includes("/9333/devtools/inspector.html"), "走 /<port>/devtools 同源代理");
    ok(decodeURIComponent(url).includes("/devtools/page/tid123"), "ws 指向 page 级 CDP");
    ok(decodeURIComponent(url).includes("key=testkey"), "携带 API_KEY");
    const dec = decodeURIComponent(url);
    ok(!dec.includes("ws=ws://") && !dec.includes("ws=wss://"), "?ws= 不带协议前缀（Chrome 158 兼容）");
    ok(!url.includes("cdmp_sid"), "不再携带已废弃的 cdmp_sid 参数");
    w.close();
  }

  // 5. 远端主机走 /remote/<host> 前缀
  {
    const w = makeDom();
    w.eval('devSid = "sid1"; instances.sid1.host = "h1";');
    await w.openNativeDevTools();
    await tick();
    const url = openUrls[0] || "";
    ok(url.includes("/remote/h1/9333/devtools/inspector.html"), "远端主机走 /remote/<host> 前缀");
    w.close();
  }

  // 6. 语言热切换：zh→en→ja 即时生效，不刷新
  {
    const w = makeDom();
    const d = w.document;
    w.applyLang("zh");
    ok(d.body.textContent.includes("运行日志"), "zh 文案存在");
    w.applyLang("en");
    ok(d.body.textContent.includes("Logs"), "en 即时生效");
    eq(d.documentElement.getAttribute("lang"), "en", "html lang 同步");
    eq(d.getElementById("langSelect").value, "en", "语言下拉同步");
    w.applyLang("ja");
    ok(d.body.textContent.includes("ログ"), "ja 即时生效");
    eq(w.localStorage.getItem("cdpLang"), "ja", "语言持久化");
    eq(w.__reloadHits, 0, "语言切换零刷新");
    w.close();
  }

  console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
  if (failed) process.exit(1);
})().catch(e => { console.error(e); process.exit(1); });
