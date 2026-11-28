/**
 * 开发者工具交互测试（jsdom）。
 * 契约：开发者工具 = 浏览器原生 DevTools 独立顶层窗口，与 F12 一模一样；
 *       不再占用右侧抽屉，抽屉只承载运行日志。
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
      w.toast = () => {};
      w.alert = () => {};
      w.confirm = () => true;
      w.open = (url) => { openUrls.push(url); return {}; };
    }
  });
  const w = dom.window;
  w.sessionStorage.setItem("tabs_sid1", JSON.stringify([{ id: "tid123", type: "page" }]));
  w.eval(`
    instances = { sid1: { sid:"sid1", status:"running", debug_port:9333, host_name:"本机" } };
    selectedSid = "sid1";
    apiKey = "testkey";
    api = async (url, opts) => {
      if (url.endsWith("/targets")) return { targets:[{id:"tid123",type:"page"}] };
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

console.log("== 开发者工具交互测试（原生独立窗口）==");

// 1. openDev 直接弹原生 DevTools 独立窗口，不打开日志抽屉
{
  const w = makeDom();
  w.openDev();
  setTimeout(() => {
    ok(openUrls.length === 1, "openDev 打开一个新窗口");
    const url = openUrls[0] || "";
    ok(url.includes("/9333/devtools/inspector.html"), "走 /<port>/devtools 同源代理的原生前端");
    ok(url.includes("ws=") && decodeURIComponent(url).includes("/devtools/page/tid123"), "ws 参数指向 page 级 CDP");
    ok(decodeURIComponent(url).includes("key=testkey"), "URL 携带 API_KEY");
    ok(!w.document.getElementById("logDrawer").classList.contains("open"), "不打开日志抽屉（不占用右侧位置）");
    ok(w.document.getElementById("devMask") === null, "抽屉内不再有内嵌 devMask");

    // 2. 远端实例走 /remote/<host>/<port>/devtools/...
    const w2 = makeDom();
    w2.eval('instances.sid1.host = "R"; instances.sid1.host_name = "主机R";');
    w2.openDev();
    setTimeout(() => {
      const rurl = openUrls[0] || "";
      ok(rurl.includes("/remote/R/9333/devtools/inspector.html"), "远端实例使用远端 DevTools 代理路径");
      ok(decodeURIComponent(rurl).includes("/remote/R/9333/devtools/page/tid123"), "远端 WS 也走远端代理");

      // 3. 未选中实例时不会打开窗口
      const w3 = makeDom();
      w3.eval('selectedSid = "none";');
      w3.openDev();
      setTimeout(() => {
        eq(openUrls.length, 0, "未选中运行实例时不打开窗口");

        // 4. 标签 ID 自动补齐（无画面区优先时从 /targets 取）
        const w4 = makeDom();
        w4.eval('sessionStorage.removeItem("tabs_sid1"); devTid = null;');
        w4.devResolveTid().then(tid => {
          eq(tid, "tid123", "devResolveTid 从 /targets 自动补齐标签");

          console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
          process.exit(failed ? 1 : 0);
        });
      }, 10);
    }, 10);
  }, 10);
}
