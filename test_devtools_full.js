// 九面板 CDP 复刻交互测试（jsdom）。
// 契约：主应用开发者工具抽屉内含九面板 + CDP 台 + 工具；
//       统一 domain=all 连接，dtCmd 带 id，事件按 method 分发；
//       增强功能：请求重放、CDP 命令台、敏感扫描、批量导出。
// 运行：node test_devtools_full.js
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, 'app', 'templates', 'index.html'), 'utf-8');
let passed = 0, failed = 0;
function ok(c, m) { if (c) { passed++; console.log('  ok  - ' + m); } else { failed++; console.log('  FAIL- ' + m); } }
function eq(a, b, m) { ok(a === b, m + ' (期望 ' + JSON.stringify(b) + ', 实际 ' + JSON.stringify(a) + ')'); }

let sentCmds = [];
function makeDom() {
  sentCmds.length = 0;
  const vc = new VirtualConsole();
  vc.on('jsdomError', () => {});
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', url: 'https://example.test/', virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async () => ({ ok: true, json: async () => ({}) });
      w.WebSocket = class {
        constructor(url) { this.url = url; w.FakeWS = this; }
        close() { this.closed = true; if (this.onclose) this.onclose(); }
        send(msg) { w.__sent.push(JSON.parse(msg)); }
      };
      w.__sent = sentCmds;
      w.alert = () => {}; w.confirm = () => true; w.prompt = () => 'k';
      w.open = () => ({});
      w.URL.createObjectURL = () => 'blob:x'; w.URL.revokeObjectURL = () => {};
      w.navigator.clipboard = { writeText: () => Promise.resolve() };
    }
  });
  const w = dom.window;
  w.sessionStorage.setItem('tabs_sid1', JSON.stringify([{ id: 'tid123', type: 'page' }]));
  w.eval(`
    instances = { sid1: { sid:"sid1", status:"running", debug_port:9333 } };
    selectedSid = "sid1"; devSid = "sid1"; apiKey = "testkey";
    api = async (u) => u.endsWith('/targets') ? { targets:[{id:'tid123',type:'page'}] } : {};
    load = async()=>{}; render=function(){}; loadLogs=async()=>{}; applyTheme=function(){}; setPreview=function(){};
  `);
  return w;
}

console.log('\n[1] 九面板 DOM 与 tab 结构');
const w = makeDom();
const tabs = ['elements','console','sources','network','performance','memory','application','security','device','cdp','tools'];
tabs.forEach(t => ok(w.document.querySelector('#dtTabbar .cap-tab[data-p="' + t + '"]') !== null, 'tab ' + t + ' 存在'));
tabs.forEach(t => ok(w.document.querySelector('#capWrap .cap-panel[data-p="' + t + '"]') !== null, '面板 ' + t + ' 容器存在'));
eq(w.eval('dtPanel'), 'network', '默认网络面板');

console.log('\n[2] 面板切换 dtSwitch');
w.eval('capConnected = true;');
['elements','sources','performance','memory','application','security','device','cdp','tools'].forEach(t => {
  w.eval('dtSwitch("' + t + '")');
  eq(w.eval('dtPanel'), t, '切到 ' + t);
  ok(w.document.querySelector('#capWrap .cap-panel[data-p="' + t + '"]').classList.contains('on'), t + ' 面板激活');
});
eq(w.document.querySelectorAll('#capWrap .cap-panel.on').length, 1, '同时只有一个面板激活');

console.log('\n[3] 统一 CDP 连接与命令 id');
const url = w.eval('capWsUrl("sid1","tid123")');
ok(url.includes('domain=all'), 'WS 使用 domain=all');
ok(url.includes('key=testkey'), 'WS 携带 API_KEY');
w.eval('capWs = { send: function(m){ window.__sent.push(JSON.parse(m)); } }; capConnected = true;');
sentCmds.length = 0;
w.dtCmd('Runtime.evaluate', { expression: '1+1' });
ok(sentCmds.length === 1, '命令已发送');
const cmd = sentCmds[0];
ok(cmd.type === 'cmd' && cmd.method === 'Runtime.evaluate', '命令结构正确');
ok(typeof cmd.id === 'number' && cmd.id > 1000, '命令带独立 id');
// 模拟响应按 id 关联
w.eval('dtOnResult({ id: ' + cmd.id + ', result: { result: { value: 2 } } })');
ok(w.eval('Object.keys(dtPending).length') === 0, '响应按 id 清除等待队列');

console.log('\n[4] 事件分发 dtDispatch');
w.eval('dtConsoleRows = []; dtScripts = [];');
w.dtDispatch('Runtime.consoleAPICalled', { type: 'log', args: [{ value: 'hello' }] });
eq(w.eval('dtConsoleRows.length'), 1, 'console 事件进入控制台');
eq(w.eval('dtConsoleRows[0].text'), 'hello', 'console 内容正确');
w.dtDispatch('Runtime.exceptionThrown', { exceptionDetails: { text: 'boom' } });
eq(w.eval('dtConsoleRows[1].kind'), 'error', '异常标记为 error');
w.dtDispatch('Debugger.scriptParsed', { scriptId: 'S1', url: 'https://a.test/app.js', startLine: 0, endLine: 10 });
eq(w.eval('dtScripts.length'), 1, 'scriptParsed 收集脚本');
w.dtDispatch('Security.stateChanged', { securityState: { state: 'secure' } });
ok(w.document.getElementById('dtSecurityBody').textContent.includes('secure'), '安全事件渲染');

console.log('\n[5] 元素面板 DOM 树');
w.eval('dtNodes = [{nodeId:1,name:"#document",type:9,attrs:[],text:"",depth:0,childIds:[2],expanded:true},{nodeId:2,name:"HTML",type:1,attrs:[],text:"",depth:1,childIds:[],expanded:true}]; dtRenderTree();');
ok(w.document.querySelectorAll('#dtDomTree .dt-node').length === 2, 'DOM 树渲染两层节点');
ok(w.document.querySelector('#dtDomTree .dt-tag').textContent.includes('document'), '节点标签正确');

console.log('\n[6] 源码断点');
w.eval('dtScripts = [{url:"https://a.test/x.js",scriptId:"S1",lineCount:10,bp:[]}]; dtSrcSelected=0; dtRenderScripts();');
w.dtToggleBp(3);
eq(w.eval('dtScripts[0].bp.length'), 1, '断点已添加');
ok(sentCmds.some(c => c.method === 'Debugger.setBreakpointByUrl'), '下发 setBreakpointByUrl');
w.dtToggleBp(3);
eq(w.eval('dtScripts[0].bp.length'), 0, '断点可移除');

console.log('\n[7] 控制台执行 JS');
w.eval('dtHist=[]; dtHistIdx=-1;');
w.document.getElementById('dtConsoleInput').value = '1+1';
sentCmds.length = 0;
w.dtEvalConsole();
ok(sentCmds.some(c => c.method === 'Runtime.evaluate' && c.params.expression === '1+1'), '执行 JS 下发 Runtime.evaluate');
eq(w.eval('dtHist[0]'), '1+1', '历史记录保存');

console.log('\n[8] CDP 命令台');
w.document.getElementById('dtCdpMethod').value = 'DOM.getDocument';
w.document.getElementById('dtCdpParams').value = '{"depth":1}';
sentCmds.length = 0;
w.dtCdpSend();
ok(sentCmds.some(c => c.method === 'DOM.getDocument' && c.params.depth === 1), '命令台发送带参数命令');

console.log('\n[9] 敏感信息扫描');
w.eval('capRequests = [{id:"R1",url:"https://a.test/?token=abcdefghijklmnop",method:"GET",headers:{Authorization:"Bearer abcdefghijklmnop1234"},postData:"",status:200,start:1,name:"a"}]');
const found = w.dtScanSecrets();
ok(found.length === 1, '扫描到敏感请求');
ok(w.document.getElementById('dtScanResult').textContent.includes('Bearer'), '扫描结果含 Bearer Token');
w.eval('capRequests = [{id:"R2",url:"https://safe.test/",method:"GET",headers:{},postData:"",status:200,start:1,name:"safe"}]');
eq(w.dtScanSecrets().length, 0, '正常请求无误报');

console.log('\n[10] 批量导出');
w.eval('capRequests = [{id:"R1",url:"https://a.test/x",method:"GET",headers:{},postData:"",status:200,start:1,name:"x"}]');
w.dtExport('py');
w.dtExport('json');
ok(true, '导出 Python / JSON 无异常');

console.log('\n[11] 请求编辑重放');
w.eval('capRequests = [{id:"R1",url:"https://a.test/x",method:"POST",headers:{"Content-Type":"application/json"},postData:"{}",status:200,start:1,name:"x"}]; capCtxReqId="R1";');
w.capCtxAction('edit-replay');
ok(w.document.getElementById('dtEditModal').classList.contains('open'), '右键编辑重放打开弹窗');
eq(w.document.getElementById('dtEditUrl').value, 'https://a.test/x', '弹窗回填 URL');
w.document.getElementById('dtEditUrl').value = 'https://b.test/y';
sentCmds.length = 0;
w.dtReplaySend();
ok(sentCmds.some(c => c.method === 'Runtime.evaluate' && c.params.expression.includes('https://b.test/y')), '重放使用新 URL');

console.log('\n[12] 设备模拟');
w.document.getElementById('dtDevW').value = '390';
w.document.getElementById('dtDevH').value = '844';
w.document.getElementById('dtDevDpr').value = '3';
sentCmds.length = 0;
w.dtDeviceApply();
ok(sentCmds.some(c => c.method === 'Emulation.setDeviceMetricsOverride' && c.params.width === 390), '设备尺寸命令正确');
w.dtDeviceReset();
ok(sentCmds.some(c => c.method === 'Emulation.clearDeviceMetricsOverride'), '设备恢复命令下发');

console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
process.exit(failed ? 1 : 0);
