/**
 * 开发者工具交互测试（jsdom）：验证按钮点击后的状态流转，而不只是静态文本。
 * 运行：node test_devtools_ui.js
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "app", "templates", "index.html"), "utf8");
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");

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
        constructor(url) { this.url = url; w.FakeWSLast = this; setTimeout(() => this.onopen && this.onopen(), 0); }
        close() { this.closed = true; if (this.onclose) this.onclose(); }
        send() {}
      };
      w.toast = () => {};
      w.alert = () => {};
      w.confirm = () => true;
      w.open = (url) => { openUrls.push(url); return {}; };
      Object.defineProperty(w.navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (t) => { w.CopiedText = t; } }
      });
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
      if (url.endsWith("/cmd")) {
        var method = JSON.parse(opts.body).method;
        if (method === "DOM.getDocument") return { ok:true, result:{ root:{ nodeId:1 } } };
        if (method === "DOM.querySelector") return { ok:true, result:{ nodeId:2 } };
        if (method === "DOM.getOuterHTML") return { ok:true, result:{ outerHTML:"<div>ok</div>" } };
        if (method === "Performance.getMetrics") return { ok:true, result:{ metrics:[
          {name:"JSHeapUsedSize",value:1048576},{name:"JSHeapTotalSize",value:2097152},
          {name:"Nodes",value:10},{name:"Documents",value:1},{name:"LayoutCount",value:5}
        ] } };
        if (method === "Runtime.evaluate") return { ok:true, result:{ result:{ value:"document.title" } } };
        return { ok:true, result:{} };
      }
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

console.log("== 开发者工具交互测试 ==");

// 1. openDev 打开抽屉，并默认展示内嵌原生 DevTools
{
  const w = makeDom();
  w.openDev();
  ok(w.document.getElementById("logDrawer").classList.contains("open"), "openDev 打开右侧抽屉");
  eq(w.eval("sideView"), "dev", "openDev 切到开发者工具视图");
  ok(w.document.getElementById("devMask").classList.contains("on"), "devMask 作为抽屉视图被激活");
  ok(w.document.getElementById("nativeDevFrame") !== null, "默认渲染内嵌原生 DevTools iframe");
  ok(w.document.getElementById("logDrawer").classList.contains("dev-mode"), "原生面板自动加宽抽屉");
  w.switchDev("network");
  ok(w.document.getElementById("devMainBtn") !== null, "网络面板生成主按钮");
  eq(w.document.getElementById("devMainBtn").textContent, "开始抓包", "网络主按钮初始文案");
}

// 2~8 串行异步场景
{
  const w = makeDom();
  w.openDev();
  w.switchDev("network");
  w.devToggle();
  setTimeout(() => {
    ok(w.eval("devWs") !== null, "点开始抓包后建立 WS");
    eq(w.document.getElementById("devStatus").textContent, "运行中 · tid123", "状态显示运行中");
    eq(w.document.getElementById("devMainBtn").textContent, "停止", "主按钮变为停止");
    const wsRef = w.eval("devWs");
    w.devToggle();
    ok(wsRef.closed || w.eval("devWs") === null, "再点一次断开 WS");
    eq(w.document.getElementById("devStatus").textContent, "已停止", "状态显示已停止");

    w.devToggle();
    setTimeout(() => {
      w.switchSideView("log");
      eq(w.eval("sideView"), "log", "切换到日志视图");
      ok(w.eval("devWs") === null, "切到日志时自动停止 WS");
      ok(w.document.getElementById("sideViewLog").classList.contains("on"), "日志视图可见");

      w.switchSideView("dev");
      w.switchDev("console");
      w.eval("devConsoleRows").push({ lvl: "log", text: "hello" });
      w.devRenderConsole();
      ok(w.document.getElementById("devConsoleBody").innerHTML.includes("hello"), "Console 渲染输出");
      w.eval("devConsoleRows = []"); w.devRenderConsole();
      ok(!w.document.getElementById("devConsoleBody").innerHTML.includes("hello"), "Console 清空后内容消失");

      w.switchDev("performance");
      w.document.getElementById("devPerfResult").textContent = "旧数据";
      w.devClearPerf();
      ok(w.document.getElementById("devPerfResult").textContent.includes("开始监控"), "性能清空更新界面文案");

      w.switchDev("element");
      w.eval("devTid = null");
      w.devQueryElement().then(() => {
        ok(w.document.getElementById("devElemResult").textContent.includes("<div>ok</div>"), "元素查询自动补齐 tid 并返回 HTML");

        w.switchDev("console-cmd");
        w.devRunCmd().then(() => {
          ok(w.document.getElementById("devCmdResult").textContent.includes("document.title"), "CDP 执行显示结果");
          eq(w.document.getElementById("devStatus").textContent, "执行完成", "CDP 执行状态完成");

          w.switchDev("network");
          w.openNativeDevTools().then(() => {
            ok(openUrls.length === 1, "原生 DevTools 打开一个新窗口");
            const url = openUrls[0] || "";
            ok(url.includes("/9333/devtools/inspector.html"), "原生入口走 /<port>/devtools 同源代理");
            ok(url.includes("ws=") && decodeURIComponent(url).includes("/devtools/page/tid123"), "ws 参数指向 page 级 CDP");
            ok(decodeURIComponent(url).includes("key=testkey"), "原生入口携带 API_KEY");

            w.switchDev("native");
            setTimeout(() => {
              const frame = w.document.getElementById("nativeDevFrame");
              ok(frame && frame.src.includes("/9333/devtools/inspector.html"), "内嵌 iframe 加载同源 DevTools 前端");
              ok(w.document.getElementById("devStatus").textContent.includes("已连接"), "内嵌面板显示连接状态");
              w.copyDevToolsLink().then(() => {
                ok(w.CopiedText && w.CopiedText === frame.src, "复制链接复用当前 DevTools 地址");
                w.eval('instances.sid1.host = "R"; instances.sid1.host_name = "主机R";');
                return w.devToolsTarget();
              }).then(t => {
                ok(t && t.url.includes("/remote/R/9333/devtools/inspector.html"), "远端实例使用远端 DevTools 代理路径");
                ok(t && decodeURIComponent(t.url).includes("/remote/R/9333/devtools/page/tid123"), "远端 WS 也走远端代理");
                console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
                process.exit(failed ? 1 : 0);
              });
            }, 10);
          });
        });
      });
    }, 10);
  }, 10);
}
