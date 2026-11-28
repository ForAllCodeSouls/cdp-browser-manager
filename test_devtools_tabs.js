// DevTools 标签栏注入脚本的端到端测试：从 main.py 提取 DEVTOOLS_TAB_SNIPPET，
// 在 jsdom 里以真实 inspector.html URL 运行，验证渲染、切换、新建、关闭、语言热切换。
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

let pass = 0, fail = 0;
function ok(cond, msg) { if (cond) { pass++; } else { fail++; console.log('  FAIL: ' + msg); } }

const root = __dirname;
const mainSrc = fs.readFileSync(path.join(root, 'app', 'main.py'), 'utf8');
const m = mainSrc.match(/DEVTOOLS_TAB_SNIPPET = r"""([\s\S]*?)"""/);
if (!m) { console.error('无法提取 DEVTOOLS_TAB_SNIPPET'); process.exit(1); }
const snippet = m[1]
  .replace('<script id="__cdmpTabSrc">', '')
  .replace('</script>', '')
  .replace(/\/\*__cdmpTabbar\*\//, '')
  // 仅把"写导航"换成可记录的替身；location.href/pathname 等读取仍用真实 jsdom location
  .replace('location.assign(', 'window.__nav(');

const TARGETS = [
  { id: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', title: '百度一下，你就知道', url: 'https://www.baidu.com/', type: 'page' },
  { id: 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB', title: '从河长制看河长治', url: 'https://www.baidu.com/s?wd=x', type: 'page' },
];

// 构造 jsdom 环境运行 snippet。用 beforeParse 注入可记录的 mock location —— 
// jsdom 原生 location.assign 只读且会触发"未实现导航"错误，无法被直接覆盖。
async function makeEnv(opts) {
  opts = opts || {};
  const url = opts.url || 'http://156.233.226.94:8001/9223/devtools/inspector.html?ws=' +
    encodeURIComponent('156.233.226.94:8001/9223/devtools/page/' + TARGETS[0].id + '?key=k');
  const calls = [];
  const stored = Object.assign({ cdpManagerApiKey: 'testkey', cdpLang: 'zh' }, opts.storage || {});

  const vc = new VirtualConsole();
  vc.on('jsdomError', e => { calls.push({ jsdomError: String(e && e.message || e) }); });

  const dom = new JSDOM('<!doctype html><html><head><title>DevTools</title></head><body class="undocked" id="-blink-dev-tools"></body></html>', {
    url,
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const w = dom.window;

  const assigned = [];
  w.__nav = target => assigned.push(String(target));
  Object.keys(stored).forEach(k => w.localStorage.setItem(k, stored[k]));

  w.fetch = async (u, o) => {
    o = o || {};
    calls.push({ url: String(u), method: o.method || 'GET', body: o.body, headers: o.headers });
    let data = { targets: TARGETS };
    if (/\/tabs$/.test(u) && (o.method || 'GET') === 'POST') {
      data = { ok: true, id: 'CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC' };
    } else if (/\/tabs\/[0-9A-F]+$/.test(u) && o.method === 'DELETE') {
      data = { ok: true };
    }
    return { ok: true, status: 200, json: async () => data };
  };
  w.alert = () => {};
  w.confirm = () => opts.confirm === undefined ? true : opts.confirm;

  w.eval(snippet);
  await new Promise(r => setTimeout(r, 60));
  return { w, dom, calls, assigned };
}

(async () => {
  // 1. 渲染：本地路径解析端口、列出两个标签、带上 API_KEY
  {
    const { w, calls } = await makeEnv();
    const bar = w.document.getElementById('cdmpTabbar');
    ok(!!bar, '标签栏容器已挂载');
    const btns = w.document.querySelectorAll('#cdmpTabList button');
    ok(btns.length === 2, '渲染出 2 个标签按钮（实际 ' + btns.length + '）');
    ok(btns[0] && btns[0].textContent.indexOf('百度') >= 0, '标签标题来自 targets');
    const listCall = calls.find(c => c.url === '/api/devtools/9223/tabs');
    ok(!!listCall, '调用本地按端口标签 API（无 /remote 前缀）');
    ok(listCall && listCall.headers && listCall.headers['X-API-Key'] === 'testkey', '带上 localStorage 的 API_KEY');
    w.close();
  }

  // 2. 切换标签：点第二个标签 → 新 URL 的 ws 换成对应 tid，保留 key
  {
    const { w, assigned } = await makeEnv();
    const btns = w.document.querySelectorAll('#cdmpTabList button');
    btns[1].click();
    await new Promise(r => setTimeout(r, 20));
    ok(assigned.length === 1, '点击标签触发一次导航（实际 ' + assigned.length + '）');
    if (assigned.length) {
      const ws = new URL(assigned[0]).searchParams.get('ws');
      ok(ws.indexOf(TARGETS[1].id) >= 0, '新 ws 指向被点标签的 tid');
      ok(ws.indexOf(TARGETS[0].id) < 0, '新 ws 不再含旧 tid');
      ok(ws.indexOf('?key=k') >= 0, '新 ws 保留 key 参数');
    }
    w.close();
  }

  // 3. 新建标签：点 + → 输入网址回车 → POST tabs → 导航到新 tid
  {
    const { w, calls, assigned } = await makeEnv();
    const plus = Array.from(w.document.querySelectorAll('#cdmpTabbar button')).find(b => b.textContent === '+');
    ok(!!plus, '新建按钮存在');
    plus.click();
    const inp = w.document.querySelector('#cdmpTabbar input');
    ok(inp.style.display !== 'none', '点 + 后输入框显示');
    inp.value = 'example.com';
    inp.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await new Promise(r => setTimeout(r, 30));
    const post = calls.find(c => c.method === 'POST' && c.url === '/api/devtools/9223/tabs');
    ok(!!post, '调用 POST /api/devtools/9223/tabs');
    ok(post && JSON.parse(post.body).url === 'example.com', '新建请求带输入的 URL');
    ok(assigned.length === 1 && assigned[0].indexOf('CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC') >= 0, '新建后导航到新标签 tid');
    w.close();
  }

  // 4. 关闭当前标签：点 × → DELETE → 导航到剩余标签
  {
    const { w, calls, assigned } = await makeEnv();
    const x = w.document.querySelector('#cdmpTabList span span');
    ok(!!x, '关闭按钮存在');
    x.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 40));
    const del = calls.find(c => c.method === 'DELETE');
    ok(!!del && del.url.indexOf('/api/devtools/9223/tabs/' + TARGETS[0].id) >= 0, '调用 DELETE 关闭当前标签');
    ok(assigned.length === 1 && assigned[0].indexOf(TARGETS[1].id) >= 0, '关闭当前标签后导航到剩余标签');
    w.close();
  }

  // 5. 远端路径解析：/remote/<host>/<port>/devtools → API 带 /remote/<host> 前缀
  {
    const url = 'http://156.233.226.94:8001/remote/host123/9333/devtools/inspector.html?ws=' +
      encodeURIComponent('156.233.226.94:8001/remote/host123/9333/devtools/page/' + TARGETS[0].id);
    const { w, calls } = await makeEnv({ url });
    const call = calls.find(c => c.url.indexOf('/tabs') >= 0);
    ok(!!call && call.url === '/remote/host123/api/devtools/9333/tabs', '远端标签 API 带 /remote/<host> 前缀与远端端口');
    w.close();
  }

  // 6. 语言热切换：storage 事件改 cdpLang → 标题即时更新（不刷新）
  {
    const { w } = await makeEnv();
    const title = w.document.querySelector('#cdmpTabbar span');
    ok(title.textContent === '标签页', '默认中文标题');
    w.localStorage.setItem('cdpLang', 'en');
    w.dispatchEvent(new w.StorageEvent('storage', { key: 'cdpLang', newValue: 'en' }));
    await new Promise(r => setTimeout(r, 30));
    ok(title.textContent === 'Tabs', '切英文后标题热更新为 Tabs');
    w.localStorage.setItem('cdpLang', 'ja');
    w.dispatchEvent(new w.StorageEvent('storage', { key: 'cdpLang', newValue: 'ja' }));
    await new Promise(r => setTimeout(r, 30));
    ok(title.textContent === 'タブ', '切日文后标题热更新为 タブ');
    w.close();
  }

  // 7. 折叠：点收起 → 高度变小、文案变"展开"
  {
    const { w } = await makeEnv();
    const bar = w.document.getElementById('cdmpTabbar');
    const fold = Array.from(w.document.querySelectorAll('#cdmpTabbar button')).find(b => b.textContent === '收起');
    ok(!!fold, '收起按钮存在（中文）');
    fold.click();
    ok(bar.style.height === '7px', '收起后高度 7px');
    ok(fold.textContent === '展开', '收起后按钮文案变展开');
    fold.click();
    ok(bar.style.height === '30px', '展开后恢复 30px');
    w.close();
  }

  console.log('DevTools 标签栏测试：' + pass + ' 通过, ' + fail + ' 失败');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
