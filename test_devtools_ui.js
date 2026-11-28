/**
 * 开发者工具入口测试（jsdom）。
 * 当前契约：自研九面板已彻底移除；开发者工具 = 在右侧抽屉内嵌浏览器原生
 * DevTools（F12 同款，iframe + sandbox 防跳出）。openDev 与 D 键统一打开
 * 该抽屉的 dev 视图；绝不 window.open 新窗口。
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
    window.__activated = [];
    api = async (url, opts) => {
      if (url.endsWith("/targets")) return { targets:[{id:"tid123",type:"page"}] };
      if (url.includes("/targets/") && url.endsWith("/activate")) { window.__activated.push(url); return {ok:true}; }
      return {};
    };
    load = async () => {};
    render = function () {};
    loadLogs = async () => {};
    applyTheme = function () {};
    setPreview = function () {};
  `);
  return w;
}

(async function main() {
  console.log("== 开发者工具入口测试（抽屉内嵌原生 DevTools）==");

  // 1. openDev 打开抽屉的 dev 视图，绝不 window.open 新窗口
  {
    const w = makeDom();
    await w.openDev();
    await tick();
    const d = w.document;
    eq(openUrls.length, 0, "openDev 不弹任何新窗口");
    ok(d.getElementById("logDrawer").classList.contains("open"), "openDev 打开右侧抽屉");
    ok(d.getElementById("logDrawer").classList.contains("dev-wide"), "dev 视图抽屉自动加宽");
    ok(d.getElementById("sideViewDev").classList.contains("on"), "dev 视图激活");
    ok(!d.getElementById("sideViewLog").classList.contains("on"), "日志视图让位");
    ok(d.getElementById("sideTabDev").classList.contains("on"), "开发者工具 tab 高亮");
    eq(w.__activated.length, 1, "打开 DevTools 前激活目标标签一次");
    ok((w.__activated[0] || "").includes("/targets/tid123/activate"), "激活的是被调试的标签 tid");
    w.close();
  }

  // 2. iframe src 指向原生 DevTools 同源代理（内嵌而非新窗口）
  {
    const w = makeDom();
    await w.openDev();
    await tick();
    const src = w.document.getElementById("devToolsFrame").getAttribute("src") || "";
    ok(src.includes("/devtools/inspector.html"), "iframe 指向原生 DevTools 代理");
    w.close();
  }

  // 3. iframe sandbox 防跳出：不给 allow-top-navigation，保留必要能力
  {
    const w = makeDom();
    const sb = w.document.getElementById("devToolsFrame").getAttribute("sandbox") || "";
    ok(!sb.includes("allow-top-navigation"), "sandbox 拒绝顶层导航（防跳出）");
    ok(sb.includes("allow-scripts"), "sandbox 保留脚本");
    ok(sb.includes("allow-same-origin"), "sandbox 保留同源（CDP WS 必需）");
    ok(sb.includes("allow-clipboard-write"), "sandbox 保留复制 cURL/Python 能力");
    w.close();
  }

  // 4. 未选中运行实例时不打开抽屉
  {
    const w = makeDom();
    w.eval('selectedSid = "none";');
    await w.openDev();
    await tick();
    eq(openUrls.length, 0, "未选中实例不弹窗");
    ok(!w.document.getElementById("logDrawer").classList.contains("open"), "未选中实例不开抽屉");
    w.close();
  }

  // 4b. 屏幕锁期间不激活标签（脚本执行中不抢浏览器前台）
  {
    const w = makeDom();
    w.eval('screenLocked = true;');
    await w.openDev();
    await tick();
    eq(w.__activated.length, 0, "屏幕锁期间不激活标签");
    ok(!!w.document.getElementById("devToolsFrame").getAttribute("src"), "屏幕锁期间 DevTools 仍打开");
    w.close();
  }

  // 5. 自研九面板 DOM 已彻底移除，但抽屉 dev tab 保留（承载原生 iframe）
  {
    const w = makeDom();
    const d = w.document;
    ok(!d.getElementById("devMask"), "devMask 已移除");
    ok(!d.getElementById("capWrap"), "capWrap 已移除");
    ok(!d.getElementById("dtTabbar"), "dtTabbar 已移除");
    ok(!!d.getElementById("sideTabDev"), "sideTabDev 抽屉 tab 保留（内嵌原生入口）");
    ok(!!d.getElementById("sideViewDev"), "抽屉 dev 视图保留");
    ok(!!d.getElementById("devToolsFrame"), "原生 DevTools iframe 存在");
    ok(!!d.getElementById("sideViewLog"), "运行日志视图保留");
    ok(typeof w.openNativeDevTools === "function", "原生 DevTools 入口保留");
    w.close();
  }

  // 6. 原生 DevTools URL 走同源代理、page 级 CDP、带 API_KEY、ws 不带协议前缀
  {
    const w = makeDom();
    w.eval('devSid = "sid1";');
    await w.openNativeDevTools();
    await tick();
    const src = w.document.getElementById("devToolsFrame").getAttribute("src") || "";
    ok(src.includes("/9333/devtools/inspector.html"), "走 /<port>/devtools 同源代理");
    ok(decodeURIComponent(src).includes("/devtools/page/tid123"), "ws 指向 page 级 CDP");
    ok(decodeURIComponent(src).includes("key=testkey"), "携带 API_KEY");
    const dec = decodeURIComponent(src);
    ok(!dec.includes("ws=ws://") && !dec.includes("ws=wss://"), "?ws= 不带协议前缀（Chrome 158 兼容）");
    ok(!src.includes("cdmp_sid"), "不再携带已废弃的 cdmp_sid 参数");
    w.close();
  }

  // 7. 远端主机走 /remote/<host> 前缀
  {
    const w = makeDom();
    w.eval('devSid = "sid1"; instances.sid1.host = "h1";');
    await w.openNativeDevTools();
    await tick();
    const src = w.document.getElementById("devToolsFrame").getAttribute("src") || "";
    ok(src.includes("/remote/h1/9333/devtools/inspector.html"), "远端主机走 /remote/<host> 前缀");
    w.close();
  }

  // 8. 收回抽屉时卸载 iframe，断开 CDP
  {
    const w = makeDom();
    await w.openDev();
    await tick();
    ok(!!w.document.getElementById("devToolsFrame").getAttribute("src"), "打开后 iframe 有 src");
    w.toggleLogs(false);
    ok(!w.document.getElementById("devToolsFrame").getAttribute("src"), "收起抽屉后卸载 iframe");
    ok(!w.document.getElementById("logDrawer").classList.contains("dev-wide"), "收起后取消加宽");
    w.close();
  }

  // 9. 语言热切换：zh→en→ja 即时生效，不刷新
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
