// 同屏开发者工具页（devtools.html）端到端测试：在 jsdom 里真实运行页面脚本，
// 验证画面+DevTools 并排、标签栏联动、dock/折叠、URL 构造、鉴权与远端路径。
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

let pass = 0, fail = 0;
function ok(cond, msg) { if (cond) { pass++; } else { fail++; console.log('  FAIL: ' + msg); } }
const root = __dirname;
const html = fs.readFileSync(path.join(root, 'app', 'templates', 'devtools.html'), 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const TARGETS = [
  { id: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', title: '百度一下', url: 'https://www.baidu.com/', type: 'page' },
  { id: 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB', title: '河长制搜索', url: 'https://www.baidu.com/s', type: 'page' },
];

function makeEnv(opts) {
  opts = opts || {};
  const url = 'http://156.233.226.94:8001/devtools?sid=sid-full-001&tid=' + TARGETS[0].id + '&key=testkey';
  const vc = new VirtualConsole();
  const errors = [];
  vc.on('jsdomError', e => errors.push(String(e && e.message || e)));
  const dom = new JSDOM(html, {
    url, runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: vc,
  });
  const w = dom.window;

  // 结构断言依赖的 DOM（jsdom 解析 html 已带，确认关键节点存在）
  const calls = [];
  w.fetch = async (u, o) => {
    o = o || {};
    const full = String(u);
    const clean = full.split('?')[0];
    calls.push({ url: full, clean: clean, method: o.method || 'GET', headers: o.headers, body: o.body });
    let data;
    if (clean.endsWith('/api/list')) {
      data = { instances: { 'sid-full-001': opts.inst || {
        sid: 'sid-full-001', status: 'running', debug_port: 9223, novnc_port: 6080,
        novnc_url_proxy: 'http://156.233.226.94:8001/6080', host: 'local', browser_mode: 'full',
      } } };
    } else if (/\/targets\/[^/]+\/activate$/.test(clean)) {
      data = { ok: true };
    } else if (/\/api\/devtools\/\d+\/tabs$/.test(clean) && (o.method || 'GET') === 'POST') {
      data = { ok: true, id: 'CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC' };
    } else if (/\/api\/devtools\/\d+\/tabs\/[0-9A-F]+$/.test(clean) && o.method === 'DELETE') {
      data = { ok: true };
    } else if (/\/api\/devtools\/\d+\/tabs$/.test(clean)) {
      data = { targets: TARGETS };
    } else {
      data = {};
    }
    return { ok: true, status: 200, json: async () => data };
  };
  w.prompt = () => opts.promptUrl === undefined ? 'https://example.com' : opts.promptUrl;
  w.confirm = () => true;
  const opened = [];
  w.open = (u) => { opened.push(String(u)); return {}; };
  // iframe.src 赋值在 jsdom 会触发导航警告；用 setter 记录而不真导航
  [['stageFrame', 'stageSrc'], ['devFrame', 'devSrc']].forEach(([id, prop]) => {
    const el = w.document.getElementById(id);
    let v = '';
    Object.defineProperty(el, 'src', {
      get() { return v; },
      set(x) { v = String(x); w[prop] = v; },
      configurable: true,
    });
  });
  w.eval(js);
  return { w, dom, calls, opened, errors };
}

(async () => {
  // 1. 静态结构：iframe 双栏 + sandbox 不给 top-navigation
  ok(html.includes('id="stageFrame"'), '画面 iframe 存在');
  ok(html.includes('id="devFrame"'), 'DevTools iframe 存在');
  const frameSeg = html.split('id="devFrame"')[1].split('>')[0];
  ok(frameSeg.includes('sandbox='), 'DevTools iframe 有 sandbox');
  ok(!frameSeg.includes('allow-top-navigation'), '拒绝顶层导航防跳出');
  ok(frameSeg.includes('allow-same-origin'), '保留同源');
  ok(frameSeg.includes('allow-clipboard-write'), '保留复制能力');
  ok(!frameSeg.includes('allow-popups'), '拒绝弹窗，避免多开一个窗口');
  ok(html.includes('id="tabRow"'), '标签栏存在');
  ok(html.includes('id="btnDock"') && html.includes('id="btnFoldStage"') && html.includes('id="btnFoldDev"'),
    'dock 与折叠按钮存在');
  ok(!html.includes('id="btnStandalone"'), '移除独立窗口按钮，避免多开一个窗口');
  ok(!/window\.open/.test(js), '同屏页脚本不调用 window.open');

  // 2. 初始化：读实例、列标签、activate、两侧 URL 就位
  const e = makeEnv({});
  await new Promise(r => setTimeout(r, 500));
  const w = e.w;
  ok(e.calls.some(c => c.clean.endsWith('/api/list')), '读取实例列表');
  ok(e.calls.some(c => /\/targets\/[^/]+\/activate$/.test(c.clean)), '初始化 activate 目标标签');
  const devSrc0 = w.document.getElementById('devFrame').getAttribute('src') || '';
  ok(devSrc0.includes('/devtools/inspector.html?cdmp_bare=1&ws='), 'DevTools 用 inspector.html?cdmp_bare=1&ws=');
  ok(devSrc0.includes(encodeURIComponent('156.233.226.94:8001/9223/devtools/page/' + TARGETS[0].id + '?key=testkey')), 'DevTools ws 指向当前标签');
  ok(devSrc0.includes('ws=' + encodeURIComponent('156.233.226.94:8001/9223/devtools/page/' + TARGETS[0].id + '?key=testkey')),
    'ws 参数只给 host:port/path + key，无协议前缀');
  ok(w.stageSrc && w.stageSrc.includes('/cast?sid='), '有头画面也走 /cast CDP 抓屏（非 VNC）');
  ok(!w.stageSrc.includes('vnc') && !w.stageSrc.includes('6080'), '有头画面不出现 noVNC 整窗');
  ok(w.stageSrc.includes('key=testkey'), '抓屏 URL 带 key');
  const chips = w.document.querySelectorAll('#tabRow .chip');
  ok(chips.length === 2, '标签栏渲染两个标签');
  ok(chips[0].classList.contains('on'), '当前标签高亮');
  ok(w.document.getElementById('sidLabel').textContent.includes('完整'), '标注完整内核');

  // 3. 切换标签：activate 新目标 + 两侧 URL 都更新
  chips[1].dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 500));
  ok(e.calls.some(c => c.clean.includes('/targets/' + TARGETS[1].id + '/activate')), '切换时 activate 新标签');
  ok((w.document.getElementById('devFrame').getAttribute('src') || '').includes(TARGETS[1].id), 'DevTools 指向新标签');
  ok(w.stageSrc.includes(TARGETS[1].id), '抓屏画面按 tid 切换到新标签');
  ok(w.document.querySelectorAll('#tabRow .chip')[1].classList.contains('on'), '新标签高亮');

  // 4. dock 方向 / 折叠
  w.document.getElementById('btnDock').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  ok(w.document.getElementById('split').classList.contains('col'), 'dock 切到上下排列');
  w.document.getElementById('btnFoldStage').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  ok(w.document.getElementById('stagePane').classList.contains('hidden'), '折叠画面');
  w.document.getElementById('btnFoldStage').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  ok(!w.document.getElementById('stagePane').classList.contains('hidden'), '再点展开画面');

  // 5. 不再有独立窗口入口（点击后不会多开窗口）
  ok(e.opened.length === 0, '同屏页不打开任何额外窗口');

  // 6. 新建标签
  const before = e.calls.filter(c => c.method === 'POST' && /\/api\/devtools\/\d+\/tabs$/.test(c.clean)).length;
  w.document.getElementById('btnNew').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 500));
  const after = e.calls.filter(c => c.method === 'POST' && /\/api\/devtools\/\d+\/tabs$/.test(c.clean)).length;
  ok(after === before + 1, '新建标签 POST 调用');
  ok(e.calls.some(c => c.method === 'POST' && (c.body || '').includes('https://example.com')), '新建带输入的网址');

  // 7. 无头实例：画面走 /cast 而非 noVNC
  const h = makeEnv({ inst: {
    sid: 'sid-full-001', status: 'running', debug_port: 9223, novnc_port: 6080,
    novnc_url_proxy: 'http://156.233.226.94:8001/6080', host: 'local', browser_mode: 'headless',
  } });
  await new Promise(r => setTimeout(r, 500));
  ok(h.w.stageSrc && h.w.stageSrc.includes('/cast?sid='), '无头画面走 /cast 抓屏');
  ok(!h.w.stageSrc.includes('vnc_lite.html'), '无头不显示 noVNC');
  ok(h.w.document.getElementById('sidLabel').textContent.includes('无头'), '标注无头内核');

  // 8. 远端实例：API 加 /remote 前缀，DevTools URL 带远端前缀
  const rem = makeEnv({ inst: {
    sid: 'sid-full-001', status: 'running', debug_port: 9223, novnc_port: 6080,
    novnc_url_proxy: 'http://156.233.226.94:8001/remote/rhost/6080', host: 'rhost', browser_mode: 'full',
  } });
  await new Promise(r => setTimeout(r, 500));
  ok(rem.calls.some(c => c.clean.includes('/api/devtools/9223/tabs') && !c.clean.includes('/remote/')), '标签 API 按端口路由（本机即可）');
  ok(rem.calls.some(c => c.clean.startsWith('/remote/rhost/api/instance/')), '远端 instance 级 API 走 /remote 前缀');
  ok((rem.w.document.getElementById('devFrame').getAttribute('src') || '').includes('/remote/rhost/9223/devtools/inspector.html'), '远端 DevTools URL 带前缀');

  // 9. 无 jsdom 报错
  ok(e.errors.length === 0, '页面脚本无运行时错误：' + e.errors.join('; '));

  console.log('同屏开发者工具页测试：' + pass + ' 通过, ' + fail + ' 失败');
  process.exit(fail ? 1 : 0);
})();
