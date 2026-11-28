// 自建中文抓包面板交互测试：jsdom 加载真实页面，mock WebSocket/fetch/剪贴板，
// 验证右键菜单、cURL/Python 生成、CDP 事件解析、过滤等核心行为。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, 'devtools-preview.html'), 'utf-8');
let passed = 0, failed = 0;
function ok(cond, msg) { if (cond) { passed++; console.log('  ok  - ' + msg); } else { failed++; console.log('  FAIL- ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + ' (got ' + JSON.stringify(a) + ')'); }

const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://localhost/' });
const w = dom.window, d = w.document;

// mock 剪贴板
let copied = '';
w.navigator.clipboard = { writeText: (t) => { copied = t; return Promise.resolve(); } };

console.log('\n[1] 页面结构与中文界面');
eq(d.title, '网络抓包 · 开发者工具', '标题中文');
ok(d.querySelector('.brand').textContent.includes('网络抓包'), '品牌中文');
ok(d.body.textContent.includes('请填写实例 sid'), '提示中文');
ok([...d.querySelectorAll('.ctx-item')].some(x => x.textContent.includes('Python (requests)')), '右键菜单有 Python requests');
ok([...d.querySelectorAll('.ctx-item')].some(x => x.textContent.includes('cURL (cmd)')), '右键菜单有 cURL cmd');

console.log('\n[2] cURL 生成（unix + win）');
const req = {
  id: 'R1', url: 'https://example.com/api/data?x=1', method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Host': 'example.com', 'Authorization': 'Bearer t' },
  postData: '{"a":1}', type: 'xhr', status: 200, start: 1, name: 'data'
};
w.requests.push(req);
w.render();
const curlUnix = w.genCurl('unix', req);
ok(curlUnix.includes("curl"), 'cURL 含 curl 命令');
ok(curlUnix.includes("--url 'https://example.com/api/data?x=1'"), 'cURL 含 url');
ok(curlUnix.includes("-X 'POST'"), 'cURL 含方法');
ok(curlUnix.includes("Content-Type: application/json"), 'cURL 含自定义头');
ok(!curlUnix.includes("Host:"), 'cURL 跳过 Host 头');
ok(curlUnix.includes("--data-raw"), 'cURL 含请求体');
const curlWin = w.genCurl('win', req);
ok(curlWin.includes(' ^'), 'cmd 版使用 ^ 续行');

console.log('\n[3] Python 生成（requests + httpx）');
const pyReq = w.genPython('requests', req);
ok(pyReq.includes('import requests'), 'requests 导入');
ok(pyReq.includes('url = "https://example.com/api/data?x=1"'), 'requests url');
ok(pyReq.includes('"Content-Type": "application/json"'), 'requests 头');
ok(pyReq.includes('data = "{\\"a\\":1}"'), 'requests 请求体');
ok(pyReq.includes('requests.request('), 'requests 调用');
const pyHttpx = w.genPython('httpx', req);
ok(pyHttpx.includes('import httpx'), 'httpx 导入');
ok(pyHttpx.includes('with httpx.Client() as cl:'), 'httpx 上下文管理');
ok(pyHttpx.includes('cl.request('), 'httpx 调用');

console.log('\n[4] CDP 事件解析');
const before = w.requests.length;
w.handleEvent('Network.requestWillBeSent', {
  requestId: 'R2', timestamp: 1, type: 'fetch',
  request: { url: 'https://a.test/list', method: 'GET', headers: { 'X-Test': '1' } }
});
eq(w.requests.length, before + 1, 'requestWillBeSent 新增一行');
eq(w.requests[0].name, 'list', '名称取 URL 末段');
w.handleEvent('Network.responseReceived', {
  requestId: 'R2', timestamp: 2,
  response: { status: 200, mimeType: 'application/json', headers: { 'Content-Type': 'application/json' } }
});
eq(w.requests[0].status, 200, 'responseReceived 回填状态');
w.handleEvent('Network.loadingFinished', { requestId: 'R2', timestamp: 2.05 });
ok(w.requests[0].duration > 0, 'loadingFinished 计算耗时');

console.log('\n[5] 过滤');
w.$('filter').value = 'a.test';
w.applyFilter();
ok(w.filtered.length >= 1 && w.filtered.every(r => r.url.includes('a.test')), '过滤生效');

console.log('\n[6] 右键菜单交互');
w.requests.push({ id: 'R3', url: 'https://post.test/save', method: 'POST', headers: {}, postData: 'x=1', status: 201, start: 1, name: 'save' });
w.render();
w.selectReq('R3');
w.ctxReqId = 'R3';
w.ctxAction('py-requests');
ok(copied.includes('import requests') && copied.includes('post.test'), '右键 Python 复制内容正确');
w.ctxAction('curl-unix');
ok(copied.includes('curl') && copied.includes('post.test'), '右键 cURL 复制内容正确');
w.ctxAction('copy-url');
eq(copied, 'https://post.test/save', '右键复制 URL');

console.log('\n[7] WS 地址构造（中文面板接真 CDP）');
w.$('fBase').value = 'http://156.233.226.94:8001';
ok(w.baseUrl() === 'http://156.233.226.94:8001', 'baseUrl');
ok(w.wsUrl('ws', '/ws/inspect/s1/t1?domain=network') === 'ws://156.233.226.94:8001/ws/inspect/s1/t1?domain=network', 'WS 地址同源构造');

console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
process.exit(failed ? 1 : 0);
