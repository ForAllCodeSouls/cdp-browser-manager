/**
 * 开发者工具交互测试（jsdom）。
 * 契约：开发者工具 = 右侧抽屉内中文 CDP 抓包面板，与运行日志按钮切换；
 *       面板顶部保留「原生 DevTools ↗」打开浏览器原生独立窗口（与 F12 一致）。
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
      w.alert = () => {}; w.confirm = () => true;
      w.open = (url) => { openUrls.push(url); return {}; };
    }
  });
  const w = dom.window;
  w.sessionStorage.setItem("tabs_sid1", JSON.stringify([{ id: "tid123", type: "page" }]));
  w.eval(`
    instances = { sid1: { sid:"sid1", status:"running", debug_port:9333, host_name:"本机" } };
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

console.log("== 开发者工具交互测试（抽屉抓包面板 + 原生入口）==");

// 1. openDev 打开抽屉开发者工具视图（不弹原生窗口）
{
  const w = makeDom();
  w.openDev();
  eq(w.eval("sideView"), "dev", "openDev 切到开发者工具视图");
  ok(w.document.getElementById("logDrawer").classList.contains("open"), "openDev 打开右侧抽屉");
  ok(w.document.getElementById("devMask").classList.contains("on"), "devMask 抓包视图激活");
  eq(openUrls.length, 0, "openDev 不弹原生窗口（原生入口独立触发）");

  // 2. 切回日志会断开抓包
  w.switchSideView("log");
  eq(w.eval("sideView"), "log", "切回日志视图");
  ok(w.document.getElementById("sideViewLog").classList.contains("on"), "日志视图显示");

  // 3. 未选中实例时 openDev 不开抽屉
  const w2 = makeDom();
  w2.eval('selectedSid = "none";');
  w2.openDev();
  ok(!w2.document.getElementById("logDrawer").classList.contains("open"), "未选中运行实例时不打开抽屉");

  // 4. 标签 ID 自动补齐
  const w3 = makeDom();
  w3.eval('sessionStorage.removeItem("tabs_sid1"); devTid = null;');
  w3.devResolveTid().then(tid => {
    eq(tid, "tid123", "devResolveTid 从 /targets 自动补齐标签");

    // 5. 原生 DevTools 入口打开独立窗口，含密钥
    const w4 = makeDom();
    w4.eval('devSid = "sid1";');
    w4.openNativeDevTools().then(() => {
      setTimeout(() => {
        eq(openUrls.length, 1, "原生入口打开一个窗口");
        const url = openUrls[0] || "";
        ok(url.includes("/9333/devtools/inspector.html"), "走 /<port>/devtools 同源代理");
        ok(decodeURIComponent(url).includes("/devtools/page/tid123"), "ws 指向 page 级 CDP");
        ok(decodeURIComponent(url).includes("key=testkey"), "携带 API_KEY");
        // 回归：Chrome 158 DevTools frontend 的 ?ws= 只接受 host:port/path，
        // 传完整 ws:// 会被拼成 ws://ws//... 导致 DNS 失败、窗口一开就断。
        const wsParam = new URL(url).searchParams.get("ws") || "";
        ok(!wsParam.includes("ws://") && !wsParam.includes("wss://"), "?ws= 不含协议前缀（交给 frontend 补）");
        ok(wsParam.startsWith("example.test") && wsParam.includes("/9333/devtools/page/tid123"), "?ws= 为 主机/端口/路径 格式（frontend 自行补协议）");

        // 6. 远端实例原生入口走 /remote/<host>/<port>/...
        const w5 = makeDom();
        w5.eval('devSid = "sid1"; instances.sid1.host = "R";');
        w5.openNativeDevTools().then(() => {
          setTimeout(() => {
            const rurl = openUrls[0] || "";
            ok(rurl.includes("/remote/R/9333/devtools/inspector.html"), "远端走 /remote 代理路径");

            // 7. 缓存/期望 TID 已失效时，用实时 /targets 校验并回退到有效标签，
            //    避免原生 DevTools 连到不存在的 page 立即断连。
            const w6 = makeDom();
            w6.eval(`
              devTid = "GONE_TID";
              castPreferredTid = "OLD_TID";
              sessionStorage.setItem("tabs_sid1", JSON.stringify([{id:"STALE_TID",type:"page"}]));
              api = async (url) => url.endsWith("/targets") ? { targets:[{id:"LIVE_TID",type:"page"}] } : {};
            `);
            w6.devResolveTid().then(tid => {
              eq(tid, "LIVE_TID", "失效 TID 被实时列表校正为有效标签");
              eq(w6.eval('devTid'), "LIVE_TID", "校正后的 TID 回写缓存");

              const w7 = makeDom();
              w7.eval(`
                devTid = "KEEP_TID";
                api = async (url) => url.endsWith("/targets") ? { targets:[{id:"KEEP_TID",type:"page"}] } : {};
              `);
              w7.devResolveTid().then(t2 => {
                eq(t2, "KEEP_TID", "有效期望 TID 保持不变");
                console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
                process.exit(failed ? 1 : 0);
              });
            });
          }, 10);
        });
      }, 10);
    });
  });
}
