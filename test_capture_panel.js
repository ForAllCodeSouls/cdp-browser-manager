// 主应用「开发者工具」抽屉内中文抓包面板交互测试（jsdom）。
// 契约：抓包面板整合在 app/templates/index.html 内，全部 cap* 函数；
//       验证右键菜单、cURL/Python 生成、CDP 事件解析、视图切换、无命名冲突。
// 运行：node test_capture_panel.js
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, 'app', 'templates', 'index.html'), 'utf-8');
let passed = 0, failed = 0;
function ok(cond, msg) { if (cond) { passed++; console.log('  ok  - ' + msg); } else { failed++; console.log('  FAIL- ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + ' (期望 ' + JSON.stringify(b) + ', 实际 ' + JSON.stringify(a) + ')'); }

let copied = '', fakeWs = null;
function makeDom() {
  copied = ''; fakeWs = null;
  const vc = new VirtualConsole();
  vc.on('jsdomError', () => {});
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', url: 'https://example.test/', virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async () => ({ ok: true, json: async () => ({}) });
      w.WebSocket = class {
        constructor(url) { this.url = url; fakeWs = this; }
        close() { this.closed = true; if (this.onclose) this.onclose(); }
        send(msg) { this.lastSend = msg; }
      };
      w.navigator.clipboard = { writeText: (t) => { copied = t; return Promise.resolve(); } };
      w.alert = () => {}; w.confirm = () => true; w.open = () => ({});
    }
  });
  const w = dom.window;
  w.sessionStorage.setItem('tabs_sid1', JSON.stringify([{ id: 'tid123', type: 'page' }]));
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

const w = makeDom();

console.log('\n[1] 抓包面板整合进主应用抽屉');
ok(w.document.getElementById('devMask') !== null, '抽屉内有 devMask 开发者工具视图');
ok(w.document.getElementById('sideTabLog') && w.document.getElementById('sideTabDev'), '有运行日志/开发者工具切换按钮');
ok(w.document.getElementById('capWrap') && w.document.getElementById('capNetBody'), '抓包面板 DOM 存在');
ok(w.document.body.textContent.includes('复制为 Python (requests)'), '右键菜单中文 Python requests');
ok(w.document.body.textContent.includes('复制为 cURL (cmd)'), '右键菜单中文 cURL cmd');
ok(typeof w.capToggleConnect === 'function', 'capToggleConnect 已定义');
ok(typeof w.openNativeDevTools === 'function', '原生 DevTools 入口仍保留');

console.log('\n[2] 视图切换：openDev 打开抽屉的开发者工具，不弹窗');
w.openDev();
eq(w.eval('sideView'), 'dev', 'openDev 切到 dev 视图');
ok(w.document.getElementById('logDrawer').classList.contains('open'), 'openDev 打开右侧抽屉');
ok(w.document.getElementById('devMask').classList.contains('on'), 'devMask 视图激活');
ok(w.document.getElementById('sideViewLog').classList.contains('on') === false, '日志视图隐藏');
w.switchSideView('log');
eq(w.eval('sideView'), 'log', '切回日志视图');
ok(w.document.getElementById('sideViewLog').classList.contains('on'), '日志视图重新显示');

console.log('\n[3] WS 地址构造（同源 /ws/inspect + 密钥）');
const url = w.capWsUrl('sid1', 'tid123');
ok(url.startsWith('wss://example.test/ws/inspect/sid1/tid123'), 'WS 走同源 /ws/inspect（https 下为 wss）');
ok(url.includes('domain=network'), '带 domain=network');
ok(url.includes('key=testkey'), '携带 API_KEY');

console.log('\n[4] cURL 生成（unix + win）');
const req = {
  id: 'R1', url: 'https://example.com/api/data?x=1', method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Host': 'example.com', 'Authorization': 'Bearer t' },
  postData: '{"a":1}', type: 'xhr', status: 200, start: 1, name: 'data'
};
const curlUnix = w.capGenCurl('unix', req);
ok(curlUnix.includes('curl'), 'cURL 含 curl 命令');
ok(curlUnix.includes("--url 'https://example.com/api/data?x=1'"), 'cURL 含 url');
ok(curlUnix.includes("-X 'POST'"), 'cURL 含方法');
ok(curlUnix.includes('Content-Type: application/json'), 'cURL 含自定义头');
ok(!curlUnix.includes('Host:'), 'cURL 跳过 Host 头');
ok(curlUnix.includes('--data-raw'), 'cURL 含请求体');
ok(w.capGenCurl('win', req).includes(' ^'), 'cmd 版使用 ^ 续行');

console.log('\n[5] Python 生成（requests + httpx）');
const pyReq = w.capGenPython('requests', req);
ok(pyReq.includes('import requests'), 'requests 导入');
ok(pyReq.includes('url = "https://example.com/api/data?x=1"'), 'requests url');
ok(pyReq.includes('"Content-Type": "application/json"'), 'requests 头');
ok(pyReq.includes('data = "{\\"a\\":1}"'), 'requests 请求体');
ok(pyReq.includes('requests.request('), 'requests 调用');
const pyHttpx = w.capGenPython('httpx', req);
ok(pyHttpx.includes('import httpx'), 'httpx 导入');
ok(pyHttpx.includes('with httpx.Client() as cl:'), 'httpx 上下文管理');
ok(pyHttpx.includes('cl.request('), 'httpx 调用');

console.log('\n[6] CDP 事件解析');
w.eval('capRequests').push(req);
const before = w.eval('capRequests').length;
w.capHandleEvent('Network.requestWillBeSent', {
  requestId: 'R2', timestamp: 1, type: 'fetch',
  request: { url: 'https://a.test/list', method: 'GET', headers: { 'X-Test': '1' } }
});
eq(w.eval('capRequests').length, before + 1, 'requestWillBeSent 新增一行');
eq(w.eval('capRequests')[0].name, 'list', '名称取 URL 末段');
w.capHandleEvent('Network.responseReceived', {
  requestId: 'R2', timestamp: 2,
  response: { status: 200, mimeType: 'application/json', headers: { 'Content-Type': 'application/json' } }
});
eq(w.eval('capRequests')[0].status, 200, 'responseReceived 回填状态');
w.capHandleEvent('Network.loadingFinished', { requestId: 'R2', timestamp: 2.05 });
ok(w.eval('capRequests')[0].duration > 0, 'loadingFinished 计算耗时');

console.log('\n[7] 过滤');
w.document.getElementById('capFilter').value = 'a.test';
w.capApplyFilter();
ok(w.eval('capFiltered').length >= 1 && w.eval('capFiltered').every(r => r.url.includes('a.test')), '过滤生效');

console.log('\n[8] 右键菜单交互');
w.eval('capRequests').push({ id: 'R3', url: 'https://post.test/save', method: 'POST', headers: {}, postData: 'x=1', status: 201, start: 1, name: 'save' });
w.capRender();
w.capSelectReq('R3');
w.eval('capCtxReqId = "R3";');
w.capCtxAction('py-requests');
ok(copied.includes('import requests') && copied.includes('post.test'), '右键 Python 复制内容正确');
w.capCtxAction('curl-unix');
ok(copied.includes('curl') && copied.includes('post.test'), '右键 cURL 复制内容正确');
w.capCtxAction('copy-url');
eq(copied, 'https://post.test/save', '右键复制 URL');

console.log('\n[9] 录制暂停 / 面板切换');
w.capToggleRecord();
eq(w.eval('capRecording'), false, '录制可暂停');
w.capSwitchPanel('console');
ok(w.document.getElementById('capPanelConsole').classList.contains('on'), '控制台面板可切换');
ok(!w.document.getElementById('capPanelNetwork').classList.contains('on'), '网络面板隐藏');

console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
process.exit(failed ? 1 : 0);
