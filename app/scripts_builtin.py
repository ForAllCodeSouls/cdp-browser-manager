"""内置 CDP 注入脚本库。

所有脚本都作用于「页面 JS 可见层」：指纹、自动化标记、调试器检测、常用工具片段。
网络层指纹（TLS/JA3、HTTP/2 SETTINGS）由 Chromium 网络栈决定，JS 无法触及。

kind:
  js  - 通过 Runtime.evaluate（立即）或 Page.addScriptToEvaluateOnNewDocument（持久）注入
  cdp - 通过 CDP 域命令设置，不走 JS 注入（如 Emulation 时区/地域）
"""

BUILTIN_SCRIPTS = [
    # ---------------------------------------------------------------- 指纹伪装
    {
        "id": "stealth_core",
        "name": "基础指纹伪装",
        "category": "指纹伪装",
        "kind": "js",
        "description": "隐藏 webdriver、伪造 UA/platform、补齐 plugins/languages/chrome/permissions，覆盖最常见的自动化检测入口。",
        "code": r"""
(() => {
  const def = (obj, key, value) => {
    try { Object.defineProperty(obj, key, { get: () => value, configurable: true }); } catch (e) {}
  };
  def(navigator, 'webdriver', undefined);
  def(navigator, 'platform', 'Win32');
  def(navigator, 'hardwareConcurrency', 8);
  def(navigator, 'deviceMemory', 8);

  const plugins = [
    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
  ];
  plugins.item = (i) => plugins[i] || null;
  plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
  plugins.refresh = () => {};
  def(navigator, 'plugins', plugins);
  def(navigator, 'languages', ['zh-CN', 'zh', 'en']);

  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
  try {
    window.chrome.runtime.onConnect = { addListener() {}, removeListener() {} };
    window.chrome.runtime.onMessage = { addListener() {}, removeListener() {} };
  } catch (e) {}

  const origQuery = navigator.permissions && navigator.permissions.query;
  if (origQuery) {
    navigator.permissions.query = (p) =>
      p && p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : origQuery.call(navigator.permissions, p);
  }
})();
""",
    },
    {
        "id": "hide_automation",
        "name": "清除自动化框架痕迹",
        "category": "指纹伪装",
        "kind": "js",
        "description": "删除 __playwright__、__puppeteer__、cdc_* 等自动化框架注入到 window/document 的标记。",
        "code": r"""
(() => {
  const kill = (obj, key) => { try { delete obj[key]; } catch (e) { try { obj[key] = undefined; } catch (_) {} } };
  const patterns = [
    '__playwright__binding__', '__playwright', '__puppeteer__', '__puppeteer',
    '__selenium__', '_selenium', 'callSelenium', 'cdc_adoQpoasnfa76pfcZLmcfl_Array',
    'cdc_adoQpoasnfa76pfcZLmcfl_Promise', 'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
    '__webdriver_evaluate', '__selenium_evaluate', '__webdriver_script_function',
    '__webdriver_script_func', '__webdriver_script_fn', '__fxdriver_evaluate',
    '__driver_unwrapped', '__webdriver_unwrapped', '__selenium_unwrapped',
    '__fxdriver_unwrapped', '_phantom', 'phantom'
  ];
  patterns.forEach(k => kill(window, k));
  Object.keys(window).filter(k => /^cdc_/.test(k)).forEach(k => kill(window, k));
  Object.keys(document).filter(k => /^cdc_/.test(k)).forEach(k => kill(document, k));
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true }); } catch (e) {}
})();
""",
    },
    {
        "id": "canvas_noise",
        "name": "Canvas/WebGL 轻微扰动",
        "category": "指纹伪装",
        "kind": "js",
        "description": "对 Canvas toDataURL 与 WebGL vendor/renderer 做轻微随机化，规避简单指纹哈希比对。注意：可能干扰依赖精确像素的页面。",
        "code": r"""
(() => {
  const orig = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function (...args) {
    try {
      const ctx = this.getContext('2d');
      if (ctx && this.width > 0 && this.height > 0) {
        const d = ctx.getImageData(0, 0, 1, 1);
        d.data[0] = (d.data[0] + 1) % 256;
        ctx.putImageData(d, 0, 0);
      }
    } catch (e) {}
    return orig.apply(this, args);
  };
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, p);
  };
})();
""",
    },

    # ---------------------------------------------------------------- 环境模拟
    {
        "id": "env_tz_shanghai",
        "name": "时区 Asia/Shanghai",
        "category": "环境模拟",
        "kind": "cdp",
        "method": "Emulation.setTimezoneOverride",
        "params": {"timezoneId": "Asia/Shanghai"},
        "description": "把页面看到的时区固定为东八区（CDP 命令，刷新后仍有效）。",
    },
    {
        "id": "env_locale_zh",
        "name": "语言 zh-CN",
        "category": "环境模拟",
        "kind": "cdp",
        "method": "Emulation.setLocaleOverride",
        "params": {"locale": "zh-CN"},
        "description": "覆盖 navigator.language 的 CDP 原生方式（部分版本需配合 JS 伪装）。",
    },
    {
        "id": "env_screen_1920",
        "name": "分辨率 1920×1080",
        "category": "环境模拟",
        "kind": "cdp",
        "method": "Emulation.setDeviceMetricsOverride",
        "params": {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False},
        "description": "固定 CSS 视口为 1920×1080，与容器 Xvfb 窗口一致。",
    },
    {
        "id": "env_geo_beijing",
        "name": "地理位置 北京",
        "category": "环境模拟",
        "kind": "cdp",
        "method": "Emulation.setGeolocationOverride",
        "params": {"latitude": 39.9042, "longitude": 116.4074, "accuracy": 100},
        "description": "模拟经纬度，用于需要地理位置授权的页面。",
    },
    {
        "id": "env_geo_clear",
        "name": "清除地理位置",
        "category": "环境模拟",
        "kind": "cdp",
        "method": "Emulation.clearGeolocationOverride",
        "params": {},
        "description": "撤销地理位置模拟。",
    },

    # ---------------------------------------------------------------- 反调试
    {
        "id": "antidebug_eval",
        "name": "反调试：无限 debugger 绕过",
        "category": "反调试",
        "kind": "js",
        "description": "Hook eval / Function / Function.prototype.constructor，剥离 debugger 语句。参考 AntiDebug_Breaker 的 Bypass Debugger 方案；可能干扰页面自身 eval 作用域。",
        "code": r"""
(() => {
  const strip = (src) => { try { return String(src).replace(/\bdebugger\b\s*;?/g, ''); } catch (e) { return src; } };
  const origEval = window.eval;
  window.eval = function (src) { return origEval.call(window, strip(src)); };
  const OrigFn = Function;
  function PatchedFn(...args) {
    if (args.length) args[args.length - 1] = strip(args[args.length - 1]);
    return OrigFn.apply(this, args);
  }
  PatchedFn.prototype = OrigFn.prototype;
  window.Function = PatchedFn;
  try { Object.defineProperty(OrigFn.prototype, 'constructor', { value: PatchedFn, writable: true, configurable: true }); } catch (e) {}
})();
""",
    },
    {
        "id": "antidebug_console",
        "name": "反调试：console 保护",
        "category": "反调试",
        "kind": "js",
        "description": "防止页面重写 console.log 或频繁 clear/刷屏做时间差检测（AntiDebug_Breaker hook log / hook clear / hook table）。",
        "code": r"""
(() => {
  const origLog = console.log;
  try { Object.defineProperty(console, 'log', { value: origLog, writable: false, configurable: false }); } catch (e) {}
  console.clear = () => {};
  console.table = function () {};
  console.group = function () {};
  console.groupEnd = function () {};
})();
""",
    },
    {
        "id": "antidebug_close",
        "name": "反调试：阻止关闭/跳转",
        "category": "反调试",
        "kind": "js",
        "description": "Hook window.close、location.assign/replace 与 history，避免检测脚本强制跳走（AntiDebug_Breaker hook close / hook history）。",
        "code": r"""
(() => {
  window.close = () => {};
  const block = (name, fn) => { try { const orig = location[name]; location[name] = function (...a) { console.log('[CDP inject] blocked ' + name, a); return fn ? fn.apply(this, a) : undefined; }; } catch (e) {} };
  block('assign');
  block('replace');
  const origPush = history.pushState.bind(history);
  const origReplace = history.replaceState.bind(history);
  history.pushState = (...a) => { console.log('[CDP inject] pushState', a[2]); return origPush(...a); };
  history.replaceState = (...a) => { console.log('[CDP inject] replaceState', a[2]); return origReplace(...a); };
})();
""",
    },
    {
        "id": "antidebug_size",
        "name": "反调试：固定窗口尺寸",
        "category": "反调试",
        "kind": "js",
        "description": "把 innerWidth/innerHeight/outerWidth/outerHeight 固定为 1366/660/1400/760（AntiDebug_Breaker Fixed window size）。注意：可能影响响应式布局。",
        "code": r"""
(() => {
  const def = (k, v) => { try { Object.defineProperty(window, k, { get: () => v, configurable: true }); } catch (e) {} };
  def('innerWidth', 1366);
  def('innerHeight', 660);
  def('outerWidth', 1400);
  def('outerHeight', 760);
})();
""",
    },
    {
        "id": "antidebug_cdp",
        "name": "反 CDP 检测",
        "category": "反调试",
        "kind": "js",
        "description": "抹除 DevTools/CDP 附加的可观察痕迹：webdriver、window.chrome.devtools、性能时间差检测入口、Error.prepareStackTrace 等。",
        "code": r"""
(() => {
  const def = (obj, k, v) => { try { Object.defineProperty(obj, k, { get: () => v, configurable: true }); } catch (e) {} };
  def(navigator, 'webdriver', undefined);
  try { delete window.chrome.devtools; } catch (e) {}
  try { delete window.__devtools; } catch (e) {}
  // 避免 Error.prepareStackTrace 暴露自动化注入的调用栈帧
  if (!Object.prototype.hasOwnProperty.call(Error, 'prepareStackTrace')) {
    try { Error.prepareStackTrace = (err, stack) => stack; } catch (e) {}
  }
  // 防止通过 Function.prototype.toString 观察 NativeFunction 以外的内容
  const origToString = Function.prototype.toString;
  Function.prototype.toString = function () {
    if (this === Function.prototype.toString) return 'function toString() { [native code] }';
    return origToString.call(this);
  };
  // 抹平 DevTools 附加后的执行时间差：保留原始 now 的单调性但加固定偏移
  const origNow = performance.now.bind(performance);
  const base = origNow();
  performance.now = () => origNow() - base;
})();
""",
    },
    {
        "id": "antidebug_prototype",
        "name": "反调试：原型链检测绕过",
        "category": "反调试",
        "kind": "js",
        "description": "绕过通过 Function.prototype.toString 判断函数是否被 hook 的常见检测，并隐藏 toString 自身被改写的特征。",
        "code": r"""
(() => {
  const native = Function.prototype.toString;
  const marker = Function.prototype.toString;
  Function.prototype.toString = function () {
    if (this === Function.prototype.toString) return 'function toString() { [native code] }';
    try { return native.call(this); } catch (e) { return 'function () { [native code] }'; }
  };
  Object.defineProperty(Function.prototype.toString, 'name', { value: 'toString', configurable: true });
})();
""",
    },

    # ---------------------------------------------------------------- 实用片段
    {
        "id": "util_remove_ads",
        "name": "隐藏广告元素",
        "category": "实用片段",
        "kind": "js",
        "description": "隐藏常见广告容器与浮层（按 id/class/属性关键字匹配）。",
        "code": r"""
(() => {
  const sel = [
    '[id*="ad-" i]', '[class*="ad-" i]', '[id^="ad" i]', '[class^="ad" i]',
    '[id*="banner" i]', '[class*="banner" i]', '[class*="popup" i]',
    'iframe[src*="ads" i]', 'ins.adsbygoogle'
  ].join(',');
  document.querySelectorAll(sel).forEach(el => {
    const t = (el.className || '') + ' ' + (el.id || '');
    if (/advert|google-ad|sponsor|推广|广告/i.test(t)) el.style.display = 'none';
  });
})();
""",
    },
    {
        "id": "util_auto_scroll",
        "name": "自动滚动到底",
        "category": "实用片段",
        "kind": "js",
        "description": "启动一个匀速滚动定时器，便于长页面抓屏或懒加载；重复注入会先清理旧定时器。",
        "code": r"""
(() => {
  if (window.__cdpScrollTimer) clearInterval(window.__cdpScrollTimer);
  window.__cdpScrollTimer = setInterval(() => {
    window.scrollBy(0, 220);
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 2) {
      clearInterval(window.__cdpScrollTimer);
      window.__cdpScrollTimer = null;
    }
  }, 120);
})();
""",
    },
    {
        "id": "util_extract_text",
        "name": "提取页面正文",
        "category": "实用片段",
        "kind": "js",
        "description": "把正文写入 window.__cdpExtractedText，供 CDP Runtime.evaluate 读取；同时输出到控制台。",
        "code": r"""
(() => {
  const nodes = document.querySelectorAll('article, main, [role="main"], .content');
  let text = '';
  nodes.forEach(n => { if ((n.innerText || '').length > text.length) text = n.innerText; });
  if (!text) text = document.body.innerText;
  window.__cdpExtractedText = text;
  console.log('[CDP inject] extracted', text.length, 'chars');
})();
""",
    },
    {
        "id": "util_hook_fetch",
        "name": "Hook fetch/XHR 日志",
        "category": "实用片段",
        "kind": "js",
        "description": "在控制台打印所有 fetch/XHR 请求与响应状态，便于逆向分析；重复注入不会叠加。",
        "code": r"""
(() => {
  if (window.__cdpHookedFetch) return;
  window.__cdpHookedFetch = true;
  const of = window.fetch;
  window.fetch = function (...a) {
    console.log('[fetch]', a[0], a[1] && a[1].method || 'GET');
    return of.apply(this, a).then(r => { console.log('[fetch resp]', r.status, a[0]); return r; });
  };
  const oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    console.log('[xhr]', m, u);
    return oo.call(this, m, u, ...rest);
  };
})();
""",
    },
    {
        "id": "util_hook_cookie",
        "name": "Hook document.cookie",
        "category": "实用片段",
        "kind": "js",
        "description": "记录 cookie 的读写到 window.__cdpCookieLog，便于定位加密参数。",
        "code": r"""
(() => {
  if (window.__cdpHookedCookie) return;
  window.__cdpHookedCookie = true;
  window.__cdpCookieLog = [];
  const desc = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
  if (!desc) return;
  Object.defineProperty(document, 'cookie', {
    get() { const v = desc.get.call(this); window.__cdpCookieLog.push(['get', v]); return v; },
    set(v) { window.__cdpCookieLog.push(['set', v]); return desc.set.call(this, v); },
    configurable: true
  });
})();
""",
    },
    {
        "id": "util_selfcheck",
        "name": "伪装自检（输出到控制台）",
        "category": "实用片段",
        "kind": "js",
        "description": "立即执行，检查 webdriver / window.chrome / plugins / 自动化标记 / UA / 时区等检测项，用 console.table 输出结果；需在伪装脚本之后执行。",
        "code": r"""
(() => {
  const probe = (name, fn) => {
    try {
      const v = fn();
      return { 检测项: name, 当前值: String(v), 结果: v ? '通过' : '可疑' };
    } catch (e) { return { 检测项: name, 当前值: '读取异常', 结果: '可疑' }; }
  };
  const rows = [
    probe('navigator.webdriver', () => !navigator.webdriver),
    probe('window.chrome', () => !!window.chrome),
    probe('window.chrome.runtime', () => !!(window.chrome && window.chrome.runtime)),
    probe('plugins 非空', () => navigator.plugins && navigator.plugins.length > 0),
    probe('languages', () => navigator.languages && navigator.languages.length > 0),
    probe('无 __playwright__', () => !('__playwright__' in window)),
    probe('无 cdc_ 标记', () => !Object.keys(window).some(k => /^cdc_[a-zA-Z]+$/.test(k))),
    probe('无 __driver__', () => !('__driver__' in window)),
    probe('UA 非 Headless', () => !/headless/i.test(navigator.userAgent)),
    probe('时区 Asia/Shanghai', () => Intl.DateTimeFormat().resolvedOptions().timeZone === 'Asia/Shanghai'),
    probe('outerWidth 与 innerWidth 接近', () => Math.abs(window.outerWidth - window.innerWidth) < 200),
    probe('permissions.query 可用', () => typeof (navigator.permissions && navigator.permissions.query) === 'function')
  ];
  const bad = rows.filter(r => r.结果 === '可疑').length;
  console.table(rows);
  console.log('[伪装自检] ' + (rows.length - bad) + '/' + rows.length + ' 项通过，' + bad + ' 项可疑');
  window.__cdpSelfCheck = rows;
  return rows;
})();
""",
    },
]


BUILTIN_MAP = {s["id"]: s for s in BUILTIN_SCRIPTS}


def get_script(script_id: str):
    return BUILTIN_MAP.get(script_id)
