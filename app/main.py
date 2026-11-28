import asyncio
import hmac
import io
import json
import logging
import os
import re
import secrets
import socket
import tarfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict
from urllib.parse import quote, urlencode, urlsplit

import docker
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

try:
    from .scripts_builtin import BUILTIN_SCRIPTS, BUILTIN_MAP, get_script
except ImportError:  # 兼容直接以 main.py 作为模块启动
    from scripts_builtin import BUILTIN_SCRIPTS, BUILTIN_MAP, get_script

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("cdp-manager")

# uvicorn 裸装不含 WebSocket 协议实现，缺库时 /ws/cast 会静默变成 404，
# 面板只显示「画面无法连接」。启动时显式报错，便于从全局日志一眼定位。
try:
    import websockets  # noqa: F401
except ImportError:
    try:
        import wsproto  # noqa: F401
    except ImportError:
        log.error("缺少 WebSocket 协议库：抓屏通道 /ws/cast 将返回 404。请安装 requirements.txt 中的 websockets 后重建 manager 镜像。")

# 内存环形缓冲：即使无法读取自身容器日志（如本地非容器运行），也能提供进程日志。
LOG_BUFFER = deque(maxlen=int(os.environ.get("LOG_BUFFER_SIZE", "500")))


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record) + "\n")
        except Exception:
            pass


_buffer_handler = _BufferHandler(level=logging.NOTSET)
_buffer_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
)
logging.getLogger().addHandler(_buffer_handler)

# ---------- 可选 API 密钥鉴权（多主机统一操控） ----------
# 默认不设置：完全不启用鉴权，现有单主机部署行为零变化。
# 设置后：本机管理 API / CDP 代理 / WS 通道都必须带同一密钥。
# 远端主机的密钥只写在服务端 REMOTE_HOSTS 里，前端拿不到。
API_KEY = os.environ.get("API_KEY", "").strip()


def key_ok(supplied: str) -> bool:
    if not API_KEY:
        return True
    supplied = str(supplied or "")
    if not supplied:
        return False
    if hmac.compare_digest(supplied, API_KEY):
        return True
    # 面板生成的接入令牌同样可用：远端主机的面板拿连接串里的令牌调用本机。
    with _STORE_LOCK:
        tokens = list(LINK_TOKENS)
    for tok in tokens:
        if hmac.compare_digest(supplied, tok):
            return True
    return False


# ---------- 多主机聚合（后端代理，浏览器只连本机） ----------
# REMOTE_HOSTS 是 JSON 数组，每项：
#   {"id":"b","name":"主机B","base":"https://b.example.com","key":"远端API_KEY"}
# 也支持简易格式：id|名称|base|key;id2|名称2|base2|key2
# id 只允许字母数字下划线短横线，防止路径注入；"local" 保留给本机。
REMOTE_HOST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
STATIC_RESOURCE_RE = re.compile(
    r"\.(js|mjs|css|html?|png|svg|ico|woff2?|ttf|webp|jpe?g|gif|map|json)$", re.I
)


def parse_remote_hosts(raw: str) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []
    entries = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            entries = parsed
    except Exception:
        for part in raw.split(";"):
            bits = [b.strip() for b in part.strip().split("|")]
            if len(bits) >= 3:
                entries.append({
                    "id": bits[0], "name": bits[1] or bits[0],
                    "base": bits[2], "key": bits[3] if len(bits) > 3 else "",
                })
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hid = str(entry.get("id") or "").strip()
        base = str(entry.get("base") or entry.get("url") or "").strip().rstrip("/")
        if not REMOTE_HOST_ID_RE.fullmatch(hid) or hid == "local":
            log.warning("忽略非法的 REMOTE_HOSTS id: %r", hid)
            continue
        if not base.startswith(("http://", "https://")):
            log.warning("忽略非法的 REMOTE_HOSTS base: %r", base)
            continue
        result[hid] = {
            "id": hid,
            "name": str(entry.get("name") or hid),
            "base": base,
            "key": str(entry.get("key") or entry.get("api_key") or "").strip(),
        }
    return list(result.values())


REMOTE_HOSTS_ENV = parse_remote_hosts(os.environ.get("REMOTE_HOSTS", ""))
UI_REMOTE_HOSTS: list = []                       # 面板里添加的远程主机，持久化在 DATA_DIR
REMOTE_HOSTS: list = list(REMOTE_HOSTS_ENV)      # 合并结果，reload_hosts() 原地刷新


# ---------- 数据目录：面板添加的远程主机 / 本机接入令牌 ----------
# Docker 部署时把宿主机目录挂到 /app/data（compose 里已预留），容器重建不丢配置。
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent / "data")))
UI_HOSTS_FILE = DATA_DIR / "remote_hosts.json"
LINK_TOKENS_FILE = DATA_DIR / "link_tokens.json"

# 用 RLock：reload_hosts() 内部也加锁，而 api_hosts_add 等调用方在持锁状态下
# 再次调用它，普通 Lock 会直接死锁。
_STORE_LOCK = threading.RLock()


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if data is not None else default


def _save_json(path: Path, data) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
        return True
    except Exception as e:
        log.warning("持久化 %s 失败: %s", path, e)
        return False


def valid_host_entry(entry: dict) -> dict:
    """校验并规范化一条远程主机记录（环境变量与面板写入共用）。"""
    hid = str(entry.get("id") or "").strip()
    base = str(entry.get("base") or entry.get("url") or "").strip().rstrip("/")
    if not REMOTE_HOST_ID_RE.fullmatch(hid) or hid == "local":
        raise ValueError(f"主机标识不合法: {hid!r}")
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"主机地址不合法: {base!r}")
    return {
        "id": hid,
        "name": str(entry.get("name") or hid),
        "base": base,
        "key": str(entry.get("key") or entry.get("api_key") or "").strip(),
    }


def load_ui_hosts():
    raw = _load_json(UI_HOSTS_FILE, [])
    hosts = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                try:
                    hosts.append(valid_host_entry(entry))
                except ValueError as e:
                    log.warning("忽略 %s 里的非法主机: %s", UI_HOSTS_FILE, e)
    UI_REMOTE_HOSTS[:] = hosts


def save_ui_hosts() -> bool:
    return _save_json(UI_HOSTS_FILE, UI_REMOTE_HOSTS)


def reload_hosts():
    """环境变量主机 + 面板添加主机合并生效；同 id 时面板添加的覆盖环境变量。"""
    with _STORE_LOCK:
        merged = {h["id"]: h for h in REMOTE_HOSTS_ENV}
        for h in UI_REMOTE_HOSTS:
            merged[h["id"]] = h
        REMOTE_HOSTS[:] = list(merged.values())


load_ui_hosts()
reload_hosts()


def get_remote(host_id: str):
    for host in REMOTE_HOSTS:
        if host["id"] == host_id:
            return host
    return None


# ---------- 本机接入令牌（被控端） ----------
# 在面板「远程接入」里生成，供其他主机的面板粘贴连接串（本机地址|令牌）后
# 远控本机。令牌等同本机 API_KEY 的访问权限，保存在数据目录，重启不丢。
LINK_TOKENS: dict = {}   # token -> {"created": ts}


def load_link_tokens():
    raw = _load_json(LINK_TOKENS_FILE, {"tokens": {}})
    toks = raw.get("tokens") if isinstance(raw, dict) else None
    result = {}
    if isinstance(toks, dict):
        for tok, meta in toks.items():
            tok = str(tok).strip()
            if tok:
                result[tok] = meta if isinstance(meta, dict) else {"created": 0}
    LINK_TOKENS.clear()
    LINK_TOKENS.update(result)


def save_link_tokens() -> bool:
    return _save_json(LINK_TOKENS_FILE, {"tokens": LINK_TOKENS})


load_link_tokens()


# ---------- 只读分享：一份全局链接，令牌 + 有效期 ----------
SHARE_FILE = DATA_DIR / "share.json"
SHARE_TTLS = (3600, 21600, 86400, 604800)  # 1 小时 / 6 小时 / 24 小时 / 7 天
SHARE_STATE = {"token": "", "expires": 0, "sids": [], "ttl": 86400}


def load_share():
    raw = _load_json(SHARE_FILE, {})
    if not isinstance(raw, dict):
        return
    try:
        ttl = int(raw.get("ttl") or 86400)
    except Exception:
        ttl = 86400
    SHARE_STATE["token"] = str(raw.get("token") or "")
    SHARE_STATE["expires"] = int(raw.get("expires") or 0)
    SHARE_STATE["sids"] = [str(x) for x in (raw.get("sids") or []) if x]
    SHARE_STATE["ttl"] = ttl if ttl in SHARE_TTLS else 86400


def save_share() -> bool:
    return _save_json(SHARE_FILE, SHARE_STATE)


def share_active(token: str) -> bool:
    token = str(token or "")
    with _STORE_LOCK:
        return bool(token) and hmac.compare_digest(token, SHARE_STATE["token"]) and SHARE_STATE["expires"] > time.time()


def share_allows(token: str, sid: str) -> bool:
    if not share_active(token):
        return False
    with _STORE_LOCK:
        return str(sid) in SHARE_STATE["sids"]


load_share()


def ws_requested_subprotocols(headers) -> list:
    """读取浏览器在 WS 握手时请求的子协议列表（如 noVNC 的 binary）。

    noVNC 只有收到服务端回显的 binary 子协议才会用二进制帧解析 RFB 数据；
    代理若不回显，它会误入 base64 模式，表现为画面一直停在 Connecting。
    """
    raw = ""
    try:
        raw = headers.get("sec-websocket-protocol", "") or ""
    except Exception:
        pass
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def ws_is_idle_timeout(exc: BaseException) -> bool:
    """识别 WS 读超时，避免把「暂时没数据」误判成断连。

    websocket-client 的 recv() 在 socket 超时后抛自有异常类
    WebSocketTimeoutException；标准库超时也会以 socket.timeout / TimeoutError
    出现。代理帧泵统一用 1 秒短超时轮询 stop 信号，必须把这类异常放行。
    原生 DevTools 对此最敏感：空闲十几秒就被断开会直接弹出
    “Debugging connection was closed”。
    """
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    return type(exc).__name__ in ("WebSocketTimeoutException", "TimeoutException")


def auth_exempt(path: str, method: str) -> bool:
    """免鉴权路径：面板首页、健康检查、主机列表（仅 GET），以及 noVNC 静态资源。

    noVNC 页面里的 <script>/<link> 无法携带 X-API-Key 头，且这些资源不含敏感
    信息，因此按扩展名放行；CDP 的 /<port>/json/* 仍然必须带密钥。
    注意 /api/hosts 只豁免 GET：POST/DELETE 是面板添加/删除远程主机的写操作，
    必须鉴权，否则设置 API_KEY 后任何人都能往本机挂载远程主机。
    """
    # /cast 与 /share 一样是纯页面壳：页面本身不含数据，实例列表 / CDP 命令 /
    # 抓屏 WS 各自按 API_KEY 或分享令牌鉴权，所以页面本身免鉴权打开。
    if path in ("/", "/api/health", "/share", "/cast"):
        return True
    if path == "/api/share/view" and method == "GET":
        return True  # 观看端没有 API_KEY，由分享令牌 + 有效期单独鉴权
    if path == "/api/hosts" and method == "GET":
        return True
    m = re.match(r"^/\d+/(.+)$", path)
    if m and method == "GET":
        rest = m.group(1)
        # DevTools frontend 资源路径不一定带扩展名，整体放行；其 WS 连接
        # 由 cdp_devtools_ws 单独校验 key，页面本身不含敏感数据。
        if rest.startswith("devtools/"):
            return True
        return not rest.startswith("json/") and bool(STATIC_RESOURCE_RE.search(rest))
    # 远端主机上的 DevTools 前端同样由通用远端代理转发。浏览器无法给 iframe
    # 加 X-API-Key 头，因此这里只放行静态前端资源；WS 仍由 remote_ws_proxy
    # 用服务端配置的远端密钥鉴权。
    if method == "GET":
        m = re.match(r"^/remote/([^/]+)/(\d+/devtools/.+)$", path)
        if m:
            return True
    return False


app = FastAPI(title="CDP Browser Manager")


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    path = request.url.path
    if API_KEY and not auth_exempt(path, request.method):
        supplied = request.headers.get("x-api-key") or request.query_params.get("key", "")
        if not key_ok(supplied):
            return JSONResponse(status_code=401, content={"detail": "API 密钥无效或缺失"})
    return await call_next(request)


# 用绝对路径定位模板，避免从别的目录启动 uvicorn 时找不到 templates。
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

client = docker.from_env()
# 完整内核与无头内核是两份独立镜像，改哪个只重新构建哪个。
# 兼容旧部署：若仍只设置了 BROWSER_IMAGE，则作为 full 镜像沿用。
BROWSER_IMAGE_FULL = os.environ.get(
    "BROWSER_IMAGE_FULL",
    os.environ.get("BROWSER_IMAGE", "ghcr.io/forallcodesouls/cdp-browser-full:latest"),
)
BROWSER_IMAGE_HEADLESS = os.environ.get(
    "BROWSER_IMAGE_HEADLESS", "ghcr.io/forallcodesouls/cdp-browser-headless:latest"
)


# 三种启动方式：有头完整 Chrome、老无头壳、完整 Chrome 新无头；两种无头都需身份补丁。
# headless-new 复用 full 镜像——里面已有完整 Chrome，加 --headless=new 即可，无需第三份镜像。
VALID_BROWSER_MODES = ("full", "headless", "headless-new")
HEADLESS_MODES = ("headless", "headless-new")

# VNC 刷新档：只控制完整内核里 x11vnc 的采集节流（-wait 轮询 / -defer 合并延迟），
# 前端 noVNC 的 quality/compression 只影响清晰度与带宽，改不了帧率。
# 档位是**全局**的：面板一次切换对所有实例生效，且持久化到 DATA_DIR，重启不丢。
# 对运行中的实例用 x11vnc -R 热改参数（无需重建容器、不断开 noVNC）；
# 新建实例时仍把参数写进 start.sh 环境变量，容器启动即按当前全局档位运行。
# 默认值与 x11vnc 自带默认一致（20/30ms），旧实例行为不变。
VNC_REFRESH_LABEL = "cdp.vnc.refresh"
VNC_REFRESH_PROFILES = {
    "standard": {"wait": 20, "defer": 30, "label": "标准"},
    "fast": {"wait": 10, "defer": 10, "label": "快速"},
    "ultra": {"wait": 0, "defer": 0, "label": "极速"},
}
VNC_REFRESH_FILE = DATA_DIR / "vnc_refresh.json"


def normalize_vnc_refresh(name: str) -> str:
    key = str(name or "").strip().lower()
    return key if key in VNC_REFRESH_PROFILES else "standard"


def get_global_vnc_refresh() -> str:
    with _STORE_LOCK:
        return normalize_vnc_refresh(
            _load_json(VNC_REFRESH_FILE, {}).get("refresh", "standard")
        )


def set_global_vnc_refresh(name: str) -> str:
    key = normalize_vnc_refresh(name)
    with _STORE_LOCK:
        _save_json(VNC_REFRESH_FILE, {"refresh": key})
    return key


def apply_vnc_refresh_to_running(key: str) -> dict:
    """对所有运行中的完整内核实例热改 x11vnc 采集节流（-R wait/defer）。

    x11vnc 的 -R 通过 X11 属性通道与已运行的实例通信，无需重启容器、
    不会断开 noVNC 连接。无头实例没有 x11vnc，跳过。
    """
    key = normalize_vnc_refresh(key)
    vp = VNC_REFRESH_PROFILES[key]
    done, failed = [], []
    with LOCK:
        targets = [
            (sid, dict(item)) for sid, item in INSTANCES.items()
            if item.get("status") == "running" and item.get("browser_mode") == "full"
        ]
    for sid, item in targets:
        cid = item.get("container_id")
        try:
            c = client.containers.get(cid)
            # -R 走 X11 属性通道，容器内需带 DISPLAY；x11vnc 启动时未带 -noremote，
            # 默认 yesremote，热改被接受。用 && 串联，任一条失败都反映到退出码，
            # 不能用 "; echo ok"——那样退出码永远 0，会把失败误报成成功。
            script = (
                f"DISPLAY=:99 x11vnc -R wait:{vp['wait']} >/dev/null 2>&1 && "
                f"DISPLAY=:99 x11vnc -R defer:{vp['defer']} >/dev/null 2>&1"
            )
            code, out = c.exec_run(["sh", "-c", script], timeout=8)
            if code != 0:
                failed.append({"sid": sid[:8], "error": "x11vnc -R 执行失败"})
                log.warning("实例 %s 热改 VNC 刷新失败: code=%s", sid[:8], code)
                continue
            with LOCK:
                if sid in INSTANCES:
                    INSTANCES[sid]["vnc_refresh"] = key
            done.append(sid[:8])
        except Exception as e:
            failed.append({"sid": sid[:8], "error": str(e)[:120]})
            log.warning("实例 %s 热改 VNC 刷新异常: %s", sid[:8], e)
    return {"refresh": key, "applied": done, "failed": failed}


def image_for_mode(browser_mode: str) -> str:
    """按内核选择对应的浏览器镜像。headless-new 复用 full 镜像。"""
    return BROWSER_IMAGE_HEADLESS if browser_mode == "headless" else BROWSER_IMAGE_FULL


# 是否由 manager 自行拉取浏览器镜像。默认开启：Dockge / 部分 compose 面板更新时会跳过
# 带 profile 的占位服务，导致两份 browser 镜像长期停留在旧版本，容器启动即崩溃。
AUTO_PULL_IMAGES = os.environ.get("AUTO_PULL_IMAGES", "1").strip().lower() not in (
    "0", "false", "no", "off"
)
# 定时自动更新：manager 每 N 小时检查一次浏览器镜像，仅当无实例运行时才真正拉取。
try:
    AUTO_UPDATE_INTERVAL_HOURS = max(1, int(os.environ.get("AUTO_UPDATE_INTERVAL_HOURS", "6")))
except ValueError:
    AUTO_UPDATE_INTERVAL_HOURS = 6

# 自动更新运行状态，供设置页展示「上次/下次检查时间、结果」。
AUTO_UPDATE_STATE = {
    "enabled": AUTO_PULL_IMAGES,
    "interval_hours": AUTO_UPDATE_INTERVAL_HOURS,
    "last_check": 0.0,
    "next_check": time.time() + 5,
    "last_status": "pending",   # pending / up_to_date / updated / skipped / error / disabled
    "last_detail": "尚未检查",
    "last_result": {},
}
AUTO_UPDATE_LOCK = threading.Lock()
PULL_LOCK = threading.Lock()

# 镜像更新检测缓存：避免面板轮询时反复打 GHCR API。
IMAGE_STATUS_CACHE = {"ts": 0.0, "data": {}, "ttl": 600.0}
IMAGE_STATUS_LOCK = threading.Lock()
IMAGE_STATUS_TTL = 600.0


def _split_repo_tag(image: str) -> tuple:
    """从 ghcr.io/owner/repo:tag 解析出 (owner/repo, tag)。"""
    name = image
    tag = "latest"
    tail = image.rsplit("/", 1)[-1]
    if ":" in tail:
        name, tag = image.rsplit(":", 1)
    if "/" in name:
        name = name.split("/", 1)[1]  # 去掉 registry host
    return name, tag


def local_image_digest(image: str) -> str:
    """读取本地镜像的 manifest 摘要（RepoDigests）。"""
    try:
        img = client.images.get(image)
    except Exception:
        return ""
    repo, _ = _split_repo_tag(image)
    for rd in img.attrs.get("RepoDigests") or []:
        if rd.split("@")[0].endswith(repo):
            return rd.split("@", 1)[1]
    rds = img.attrs.get("RepoDigests") or []
    return rds[0].split("@", 1)[1] if rds else ""


def remote_image_digest(image: str) -> str:
    """通过 GHCR 匿名 API 读取远端 latest 的 manifest 摘要。失败时抛异常由调用方降级。"""
    import urllib.request

    repo, tag = _split_repo_tag(image)
    scope = quote(f"repository:{repo}:pull", safe="")
    token_url = f"https://ghcr.io/token?scope={scope}&service=ghcr.io"
    with urllib.request.urlopen(token_url, timeout=10) as resp:
        token = json.load(resp)["token"]
    req = urllib.request.Request(
        f"https://ghcr.io/v2/{repo}/manifests/{tag}",
        method="HEAD",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": (
                "application/vnd.oci.image.index.v1+json, "
                "application/vnd.docker.distribution.manifest.list.v2+json, "
                "application/vnd.oci.image.manifest.v1+json"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.headers.get("Docker-Content-Digest", "")


def check_images_status(force: bool = False) -> dict:
    """对比本地与远端镜像摘要，判断是否有新版本。

    带缓存，失败自动降级不影响面板。远端摘要做三次采样取多数，避免 GHCR/CDN
    多节点抖动或 CI 正在推送时出现"有更新/已是最新"反复横跳：

    - 三次里至少两次一致才算「已确认」；无法确认时不翻转上一轮的结论（黏性），
      并标记为 checking，前端显示「检测中」而不是「已是最新」。
    - 有未确认项时用短缓存尽快重试，避免错误结论停留 10 分钟。
    """
    now = time.time()
    with IMAGE_STATUS_LOCK:
        cached = IMAGE_STATUS_CACHE["data"]
        cache_ttl = IMAGE_STATUS_CACHE.get("ttl", IMAGE_STATUS_TTL)
        if not force and cached and now - IMAGE_STATUS_CACHE["ts"] < cache_ttl:
            return cached
        prev = cached or {}

    result = {}
    any_checking = False
    for label, image in (("full", BROWSER_IMAGE_FULL), ("headless", BROWSER_IMAGE_HEADLESS)):
        local = local_image_digest(image)
        samples = []
        err = ""
        for i in range(3):
            if i:
                time.sleep(0.3)
            try:
                samples.append(remote_image_digest(image))
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                log.debug("查询 %s 远端镜像摘要失败: %s", label, err)
        # 取出现次数最多的摘要；三次全不同则视为抖动，本轮不确认。
        remote = ""
        confirmed = False
        if samples:
            counts = {}
            for s in samples:
                counts[s] = counts.get(s, 0) + 1
            remote, hits = max(counts.items(), key=lambda kv: kv[1])
            confirmed = hits >= 2
            if not confirmed:
                log.warning(
                    "%s 远端摘要三次采样不一致，本轮未确认（%s）",
                    label, "/".join(s[:12] for s in samples),
                )

        # 无法可靠确认时，沿用上一轮的结论，避免状态翻转；并标记检测中。
        prev_item = prev.get(label, {}) if isinstance(prev, dict) else {}
        if confirmed and local and remote:
            available = remote != local
            checking = False
        else:
            available = bool(prev_item.get("update_available", False))
            checking = True
            any_checking = True

        if checking:
            status = "checking"
        elif available:
            status = "update_available"
        else:
            status = "up_to_date"

        result[label] = {
            "image": image,
            "local": local[:19] if local else "",
            "remote": remote[:19] if remote else "",
            "local_missing": not local,
            "remote_unknown": not remote,
            "unstable": not confirmed,
            "confirmed": confirmed,
            "status": status,
            # checking 时保留历史结论，避免连续抖动把"有更新"抹掉；前端另按 status 显示"检测中"。
            "update_available": bool(available),
            "error": err if not remote else "",
        }

    ttl = 60.0 if any_checking else IMAGE_STATUS_TTL
    with IMAGE_STATUS_LOCK:
        IMAGE_STATUS_CACHE["ts"] = now
        IMAGE_STATUS_CACHE["ttl"] = ttl
        IMAGE_STATUS_CACHE["data"] = result
    return result


def pull_browser_images() -> dict:
    """拉取 full + headless 两份浏览器镜像，返回每份结果。线程安全、失败不影响 manager。"""
    targets = (("full", BROWSER_IMAGE_FULL), ("headless", BROWSER_IMAGE_HEADLESS))
    seen = set()
    results = {}
    with PULL_LOCK:
        for label, image in targets:
            if image in seen:
                continue
            seen.add(image)
            try:
                log.info("开始拉取 %s 浏览器镜像: %s", label, image)
                client.images.pull(image)
                # 拉取成功 ≠ 拉到最新版本：CI 正在构建时，registry 上还是旧 manifest，
                # pull 会正常返回，但拉到的是上一版镜像。用摘要复核并如实告知。
                local = local_image_digest(image)
                try:
                    remote = remote_image_digest(image)
                except Exception as e:
                    remote = ""
                    log.debug("复核 %s 远端摘要失败: %s", label, e)
                stale = bool(remote and local and local != remote)
                results[label] = {
                    "image": image,
                    "ok": True,
                    "stale": stale,
                    "local": local[:19] if local else "",
                    "remote": remote[:19] if remote else "",
                }
                if stale:
                    log.warning(
                        "浏览器镜像 %s 拉取到的不是远端最新（本地 %s / 远端 %s），"
                        "可能 CI 仍在构建，请稍后重试",
                        label, local[:19], remote[:19],
                    )
                else:
                    log.info("浏览器镜像已更新: %s (%s)", label, image)
            except Exception as e:
                results[label] = {"image": image, "ok": False, "error": str(e)}
                log.warning("浏览器镜像拉取失败: %s (%s) - %s", label, image, e)
    # 拉取后本地摘要已变，清掉检测缓存，让红点下次读取立即反映最新状态。
    with IMAGE_STATUS_LOCK:
        IMAGE_STATUS_CACHE["ts"] = 0.0
        IMAGE_STATUS_CACHE["ttl"] = IMAGE_STATUS_TTL
    return results


def ensure_image_available(image: str) -> None:
    """创建前确认镜像已在本地，避免 containers.run 触发漫长 pull 并卡住全局锁。

    镜像缺失时快速失败并给出可执行的提示，而不是让整个面板的接口一起挂起。
    """
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        raise RuntimeError(
            f"镜像 {image} 尚未拉取到本地。manager 会在启动后自动拉取（约几分钟），"
            f"请稍候重试；也可在设置里点「立即更新浏览器镜像」，"
            f"或在服务器执行 docker compose --profile no-auto-start pull。"
        )
    except docker.errors.APIError as e:
        raise RuntimeError(f"检查镜像失败: {e}")


CONTAINER_LABEL = "cdp.browser.manager"
PROFILE_LABEL = "cdp.profile"
PROFILE_OWNER_LABEL = "cdp.profile.owner"
BROWSER_MODE_LABEL = "cdp.browser.mode"
MANAGER_ID = os.environ.get("MANAGER_ID", "default")

# 参数白名单：与前端提示保持一致，避免同名目录映射到同一个 Volume。
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
# Chrome 的 target id 是十六进制串，这里放宽到常见的安全字符集即可。
TARGET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# 容器共享内存：启动参数已带 --disable-dev-shm-usage，无需给每个实例预留 2GB。
SHM_SIZE = os.environ.get("BROWSER_SHM_SIZE", "512m")
# 抓屏帧上限与 start.sh 的 1920x1080 窗口对齐，避免大帧拖慢 WS 转发。
CAST_MAX_WIDTH = int(os.environ.get("CAST_MAX_WIDTH", "1920"))
CAST_MAX_HEIGHT = int(os.environ.get("CAST_MAX_HEIGHT", "1080"))
# 抓屏画质三档：省流 / 均衡 / 高清。三档都不隔帧（everyNthFrame 恒为 1），
# 帧率一致、只差清晰度与流量：省流靠缩分辨率 + 低 JPEG 质量省带宽，画面不慢半拍。
# 高清保持原(start.sh 窗口)上限。前端按档位把参数带在 WS 查询串上。
CAST_PROFILES = {
    "low": {"quality": 45, "max_width": 960, "max_height": 540, "every_nth": 1},
    "medium": {"quality": 65, "max_width": 1280, "max_height": 720, "every_nth": 1},
    "high": {"quality": 80, "max_width": CAST_MAX_WIDTH, "max_height": CAST_MAX_HEIGHT, "every_nth": 1},
    # 监控主面板专用：多画面同屏，缩小分辨率 + 低质量，并限制推帧频率（约 1 帧/秒），
    # 避免 N 路并发把 manager 与浏览器的 CPU/带宽打满。用户在画质三档里看不到此档。
    "monitor": {"quality": 30, "max_width": 640, "max_height": 360, "every_nth": 2, "min_interval": 1.0},
}
# 无 screencast 帧时，每隔多久主动 captureScreenshot 补一帧（秒）。
# 仅作旧前端/外部脚本不带 snap 参数时的兜底；新版前端按 snap 三档传间隔。
CAST_KEEPALIVE_SEC = float(os.environ.get("CAST_KEEPALIVE_SEC", "1.5"))
# 抓屏方式：Cast = startScreencast 变化推帧 + captureScreenshot 定时保底；
# Snap = 不发 startScreencast，纯 captureScreenshot 按固定间隔轮询抓图。
# 两模式共用同一组定时抓帧间隔：快 0.2s（约 5fps）/ 标准 0.5s（约 2fps）/ 省 1s（约 1fps）。
# Cast 的保底心跳就是这组间隔——变化推帧最快，静止时至少按此节奏兜底，画面不会定格。
SNAP_INTERVALS = {"fast": 0.2, "normal": 0.5, "eco": 1.0}


def normalize_snap_interval(name: str) -> str:
    key = str(name or "").strip().lower()
    return key if key in SNAP_INTERVALS else "normal"


def cast_quality_profile(name: str) -> dict:
    """按档位名返回抓屏参数；未知/空档位回落均衡，避免恶意超大值。"""
    p = CAST_PROFILES.get(str(name or "").strip().lower())
    return dict(p or CAST_PROFILES["medium"])


def navigation_url_from_cdp(msg: dict, main_frame: dict) -> str:
    """从 CDP 导航事件里提取主 frame 的新 URL，供地址栏实时更新。

    网页自动化跳转很快时，靠 HTTP 轮询标签列表拿 URL 会滞后数秒。CDP 在页面
    导航时会主动推 Page.frameNavigated（整页跳转）与 Page.navigatedWithinDocument
    （SPA 内部/hash 跳转），抓屏 WS 已 attach 该 target，能实时收到这些事件。
    只认主 frame（无 parentId / frameId 等于已知主 frame），忽略 iframe 噪声。
    main_frame 是可变 dict，用 'id' 键在事件流里记住主 frame 的 id。
    """
    method = msg.get("method", "")
    params = msg.get("params") or {}
    if method == "Page.frameNavigated":
        frame = params.get("frame") or {}
        if frame.get("parentId"):  # 子 iframe，忽略
            return ""
        fid = frame.get("id", "")
        if fid:
            main_frame["id"] = fid
        return frame.get("url", "") or ""
    if method == "Page.navigatedWithinDocument":
        fid = params.get("frameId", "")
        known = main_frame.get("id", "")
        # 主 frame 尚未确立时忽略，避免把首个 iframe 的站内跳转当成主页面。
        if not fid or (known and fid != known):
            return ""
        return params.get("url", "") or ""
    return ""


INSTANCES: Dict[str, dict] = {}
LOCK = threading.Lock()

PORT_DEBUG_START = 9222
PORT_DEBUG_END = 9322
PORT_VNC_START = 5901
PORT_VNC_END = 6000
PORT_NOVNC_START = 7901
PORT_NOVNC_END = 7999

# 若通过反向代理访问，可设置 PUBLIC_HOST，例如 https://cdp.example.com
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip().rstrip("/")


def find_free(start: int, end: int, used: set) -> int:
    for port in range(start, end + 1):
        if port not in used:
            used.add(port)
            return port
    raise RuntimeError(f"端口池耗尽: {start}-{end}")


def collect_used_ports() -> dict:
    """收集宿主机上所有 Docker 容器占用的端口，避让非本 manager 创建的容器。

    只看带 manager 标签的容器会漏掉其它服务占用的 9222/5900/6080 等端口，
    创建时才会被 Docker 报 "port is already allocated"。这里统一扫描全部容器，
    并把落在三个端口池内的宿主机端口加入避让集合。
    """
    used = {"debug": set(), "vnc": set(), "novnc": set()}
    try:
        containers = client.containers.list(all=True)
    except docker.errors.APIError as e:
        log.warning("读取容器列表失败，尝试只读取本 manager 的容器: %s", e)
        try:
            containers = client.containers.list(
                all=True, filters={"label": f"{CONTAINER_LABEL}={MANAGER_ID}"}
            )
        except Exception:
            return used
    for c in containers:
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        for bindings in ports.values():
            if not bindings:
                continue
            for binding in bindings:
                raw = binding.get("HostPort") if isinstance(binding, dict) else None
                if not raw or not str(raw).isdigit():
                    continue
                port = int(raw)
                if PORT_DEBUG_START <= port <= PORT_DEBUG_END:
                    used["debug"].add(port)
                elif PORT_VNC_START <= port <= PORT_VNC_END:
                    used["vnc"].add(port)
                elif PORT_NOVNC_START <= port <= PORT_NOVNC_END:
                    used["novnc"].add(port)
    return used


def public_base(request: Request) -> str:
    if PUBLIC_HOST:
        return PUBLIC_HOST
    return f"{request.url.scheme}://{request.headers.get('host', request.client.host)}"


def host_from_base(base: str) -> str:
    """从 public_base 中取纯 host，兼容 IPv6 地址。"""
    parsed = urlsplit(base if "://" in base else "//" + base)
    return parsed.hostname or ""


def host_for_url(host: str) -> str:
    """IPv6 地址放进 URL 时需要方括号。"""
    return f"[{host}]" if ":" in host else host


def external_base(request: Request) -> str:
    """对外可访问的 base（协议 + host），用于同源 CDP 代理改写地址。

    反向代理场景下 request.url.scheme 常是 http（nginx→manager 是明文），
    因此优先取 X-Forwarded-Proto；host 直接用请求头，保持用户访问的域名。
    """
    if PUBLIC_HOST:
        return PUBLIC_HOST
    host = request.headers.get("host", "")
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if forwarded:
        scheme = forwarded
    elif host and not re.match(r"^[\d.:\[\]]+(:\d+)?$", host):
        # 纯域名（无端口，如 browservnc.example.com）走 443 反代，按 https 处理；
        # IP 或带端口的 Host 是直连，沿用请求实际 scheme。
        scheme = "https"
    else:
        scheme = request.url.scheme
    if host:
        return f"{scheme}://{host}"
    return str(request.base_url).rstrip("/")


def parse_ports(container) -> dict:
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    result = {}
    for internal, key in (
        ("9222/tcp", "debug_port"),
        ("5900/tcp", "vnc_port"),
        ("6080/tcp", "novnc_port"),
    ):
        try:
            result[key] = int(ports[internal][0]["HostPort"])
        except (KeyError, IndexError, TypeError):
            result[key] = None
    return result


def profile_volume_name(name: str) -> str:
    """把用户给的 profile 名规范成 Docker volume 名。"""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(name)).strip("-.")[:48]
    if not safe:
        safe = uuid.uuid4().hex[:12]
    return f"cdp-profile-{MANAGER_ID}-{safe}"


def list_profiles() -> list:
    """列出本 manager 的所有持久 profile volume，并标注是否被实例占用。"""
    with LOCK:
        used_by = {
            item.get("profile_name"): sid
            for sid, item in INSTANCES.items()
            if item.get("profile_mode") == "persistent" and item.get("profile_name")
        }
    result = []
    try:
        volumes = client.volumes.list(
            filters={"label": f"{PROFILE_LABEL}=1"}
        )
    except docker.errors.APIError:
        return result
    for v in volumes:
        labels = v.attrs.get("Labels") or {}
        if labels.get(PROFILE_OWNER_LABEL) != MANAGER_ID:
            continue
        raw = labels.get("cdp.profile.name", "")
        result.append(
            {
                "name": raw,
                "volume": v.name,
                "created": v.attrs.get("CreatedAt", ""),
                "used_by": used_by.get(raw),
            }
        )
    return sorted(result, key=lambda x: x["created"], reverse=True)


def remove_profile(name: str) -> bool:
    """删除一个持久 profile volume。被运行中实例占用时拒绝。"""
    vname = profile_volume_name(name)
    with LOCK:
        for sid, item in INSTANCES.items():
            if (
                item.get("profile_mode") == "persistent"
                and item.get("profile_name") == name
            ):
                raise RuntimeError(f"实例 {sid[:8]} 正在使用该目录，请先停止实例")
    try:
        v = client.volumes.get(vname)
        v.remove(force=True)
        log.info("删除持久目录 %s（%s）", name, vname)
        return True
    except docker.errors.NotFound:
        return False
    except docker.errors.APIError as e:
        raise RuntimeError(str(e))


def export_profile_bytes(name: str) -> bytes:
    """把一个持久目录卷打包成 tar 字节，供跨主机迁移。

    用临时容器挂载该卷（只读），通过 Docker 的 get_archive 直接取 tar 流，
    不依赖容器内是否装了 tar 命令。被运行中实例占用时拒绝，避免拷到
    写一半的 Cookie/SQLite。
    """
    vname = profile_volume_name(name)
    with LOCK:
        for sid, item in INSTANCES.items():
            if (item.get("profile_mode") == "persistent"
                    and item.get("profile_name") == name):
                raise ProfileInUseError(
                    f"实例 {sid[:8]} 正在使用该目录，请先停止实例再导出"
                )
    try:
        client.volumes.get(vname)
    except docker.errors.NotFound:
        raise RuntimeError(f"目录不存在: {name}")
    img = image_for_mode("headless")
    ensure_image_available(img)
    tmp = None
    try:
        tmp = client.containers.create(
            img,
            command="sleep 3600",
            volumes={vname: {"bind": "/data", "mode": "ro"}},
        )
        stream, _ = tmp.get_archive("/data")
        buf = io.BytesIO()
        for chunk in stream:
            buf.write(chunk)
        return buf.getvalue()
    finally:
        if tmp is not None:
            try:
                tmp.remove(force=True)
            except docker.errors.APIError:
                pass


def import_profile_bytes(name: str, raw: bytes) -> bool:
    """把 tar 字节解包成本机新的持久目录卷（带正确 label）。

    Docker 的 get_archive 把卷内容放在 tar 的 data/ 目录下；put_archive 到
    容器的 / 即可还原成 /data。卷名与 label 按本机 MANAGER_ID 重建，保证
    面板「持久用户目录」能列出并正常复用。
    """
    if not PROFILE_NAME_RE.fullmatch(name):
        raise RuntimeError("目录名只能包含字母、数字、下划线、点和短横线，长度 1-48")
    vname = profile_volume_name(name)
    try:
        client.volumes.get(vname)
        raise ProfileExistsError(f"本机已存在同名目录「{name}」，请换一个名字")
    except docker.errors.NotFound:
        pass
    img = image_for_mode("headless")
    ensure_image_available(img)
    volume = client.volumes.create(
        name=vname,
        labels={
            PROFILE_LABEL: "1",
            PROFILE_OWNER_LABEL: MANAGER_ID,
            "cdp.profile.name": name,
        },
    )
    tmp = None
    try:
        tmp = client.containers.create(
            img,
            command="sleep 3600",
            volumes={vname: {"bind": "/data", "mode": "rw"}},
        )
        tmp.put_archive("/", io.BytesIO(raw))
        log.info("导入持久目录 %s（%s，%d 字节）", name, vname, len(raw))
        return True
    except Exception:
        # 解包失败不留半个卷，避免面板列出一个损坏的目录。
        try:
            volume.remove(force=True)
        except docker.errors.APIError:
            pass
        raise
    finally:
        if tmp is not None:
            try:
                tmp.remove(force=True)
            except docker.errors.APIError:
                pass


def sync_instances():
    with LOCK:
        snapshot = list(INSTANCES.items())
    for sid, item in snapshot:
        try:
            container = client.containers.get(item["container_id"])
            status = container.status
            ports = parse_ports(container)
            # 统一在锁内写回，避免与 cdp 监控线程/创建接口并发改同一字典。
            with LOCK:
                current = INSTANCES.get(sid)
                if current is None:
                    continue
                current["status"] = status
                current.update(ports)
                if status != "running":
                    current["cdp_ready"] = False
        except docker.errors.NotFound:
            with LOCK:
                INSTANCES.pop(sid, None)
        except docker.errors.APIError:
            pass


def container_created_at(container) -> float:
    """容器创建时间（epoch 秒）；解析失败时兜底为当前时间。排序键，重启后仍稳定。"""
    raw = (container.attrs or {}).get("Created") or ""
    try:
        # Docker 返回形如 2026-11-27T06:12:33.123456789Z，Python 只接受微秒级。
        return datetime.fromisoformat(str(raw).rstrip("Z").split(".")[0] + "+00:00").timestamp()
    except Exception:
        return time.time()


def restore_instances():
    for c in client.containers.list(
        all=True, filters={"label": f"{CONTAINER_LABEL}={MANAGER_ID}"}
    ):
        sid = c.labels.get("cdp.sid")
        if not sid:
            continue
        mode = c.labels.get(BROWSER_MODE_LABEL, "full")
        if mode not in VALID_BROWSER_MODES:
            mode = "full"
        ports = parse_ports(c)
        # 两种无头模式都没有 VNC/noVNC 端口，只要求 CDP 端口存在
        if ports.get("debug_port") is None:
            continue
        if mode == "full" and (
            ports.get("vnc_port") is None or ports.get("novnc_port") is None
        ):
            continue
        INSTANCES[sid] = {
            "sid": sid,
            "container_id": c.id,
            "container_name": c.name,
            **ports,
            "status": c.status,
            "created_at": container_created_at(c),
            "browser_mode": mode,
            "profile_mode": c.labels.get("cdp.profile.mode", "ephemeral"),
            "profile_name": c.labels.get("cdp.profile.name", ""),
            "vnc_refresh": normalize_vnc_refresh(c.labels.get(VNC_REFRESH_LABEL, "")),
        }
        # 全局档位是权威：manager 重启后，把持久化的档位覆盖回实例视图，
        # 这样界面显示与实际已热改的 x11vnc 参数保持一致。运行中实例在启动时
        # 也会由 reapply_global_vnc_refresh() 再热改一次（见下方）。
        if mode == "full":
            INSTANCES[sid]["vnc_refresh"] = get_global_vnc_refresh()


def to_view(item: dict, request: Request) -> dict:
    data = dict(item)
    base = external_base(request)
    host = host_for_url(host_from_base(base))
    debug = item.get("debug_port")
    vnc = item.get("vnc_port")
    novnc = item.get("novnc_port")
    # ---- 旧字段：保持历史格式（host:port / http://host:port），老项目零影响 ----
    # 这些字段被外部自动化脚本读取用于拼接连接地址。之前两版把它们改成了
    # 「域名/端口」的同源格式，会破坏仍在用直连方式的老项目，故全部恢复原样。
    data["cdp_addr"] = f"{host}:{debug}" if debug else ""
    data["cdp_http"] = f"http://{host}:{debug}" if debug else ""
    data["vnc_url"] = f"http://{host}:{vnc}" if vnc else ""
    # novnc 包不提供根目录 index.html。这里返回基础地址，
    # 具体页面（vnc_lite.html / vnc.html）与参数由前端设置决定。
    data["novnc_url"] = f"http://{host}:{novnc}" if novnc else ""
    # 直连别名（与旧字段同义，供明确区分语义的调用方使用）
    data["vnc_direct"] = f"http://{host}:{vnc}" if vnc else ""
    data["novnc_direct"] = f"http://{host}:{novnc}" if novnc else ""

    # ---- 新字段：同源代理地址（/<port>/...），供新版面板在域名下使用 ----
    # 域名只转发 443 到 manager，浏览器容器发布的主机端口经域名带端口不可达；
    # manager 新增的同源代理按路径端口转发到容器内网，两者共存互不干扰。
    data["cdp_addr_proxy"] = f"{host}/{debug}" if debug else ""
    data["cdp_http_proxy"] = f"{base}/{debug}" if debug else ""
    data["vnc_url_proxy"] = f"{base}/{vnc}" if vnc else ""
    data["novnc_url_proxy"] = f"{base}/{novnc}" if novnc else ""
    # screencast 前端模板：把 {tid} 换成标签页 id 即可。
    # inspector.html 由浏览器自带的 CDP HTTP 服务提供，页面与 WS 同源，避免 CORS。
    # 抓屏走 manager 的 WS 代理（见 /ws/cast），不依赖浏览器自带 devtools 前端。
    data["cast_ws"] = (
        f"/ws/cast/{item.get('sid', '')}/{{tid}}"
    )
    return data


def remote_request(host: dict, path: str, method: str = "GET", payload=None, timeout: float = 30.0):
    """调用远端 manager 的 HTTP API，返回 (status, bytes, content_type)。"""
    import urllib.request

    url = host["base"] + "/" + str(path).lstrip("/")
    headers = {"Accept": "application/json"}
    if host.get("key"):
        headers["X-API-Key"] = host["key"]
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), resp.headers.get("Content-Type", "")


def remote_view(host: dict, item: dict, request: Request) -> dict:
    """改写远端实例的代理地址，使全部走本机 /remote/<host_id>/ 前缀。"""
    data = dict(item)
    prefix = "/remote/" + host["id"]
    base = external_base(request).rstrip("/") + prefix
    hostpart = host_for_url(host_from_base(external_base(request)))
    debug = item.get("debug_port")
    vnc = item.get("vnc_port")
    novnc = item.get("novnc_port")
    # 旧字段保留远端 manager 生成的直连地址，老项目的读取方式不变。
    data["host"] = host["id"]
    data["host_name"] = host["name"]
    data["cdp_addr_proxy"] = f"{hostpart}{prefix}/{debug}" if debug else ""
    data["cdp_http_proxy"] = f"{base}/{debug}" if debug else ""
    data["vnc_url_proxy"] = f"{base}/{vnc}" if vnc else ""
    data["novnc_url_proxy"] = f"{base}/{novnc}" if novnc else ""
    data["cast_ws"] = f"{prefix}/ws/cast/{item.get('sid', '')}/{{tid}}"
    return data


def container_ip(container) -> str:
    container.reload()
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    for net in networks.values():
        if net.get("IPAddress"):
            return net["IPAddress"]
    return ""


def wait_cdp_ready(container, timeout: float = 20.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    url = ""
    while time.time() < deadline:
        # 容器若已退出或在反复重启，立即判定失败，不再空等整个超时。
        try:
            container.reload()
            state = container.attrs.get("State", {})
            if state.get("Status") in ("exited", "dead") or state.get("Restarting"):
                log.warning(
                    "容器 %s 在 CDP 就绪前已异常退出/重启：status=%s exit=%s restarting=%s error=%s",
                    container.id[:12], state.get("Status"), state.get("ExitCode"),
                    state.get("Restarting"), state.get("Error", ""),
                )
                # start.sh 把 chrome 报错重定向到 /tmp/chromium.log，容器 stdout 看不到。
                # 主动从容器文件系统读出来写进全局日志，一眼就能定位根因。
                for p in ("/tmp/chromium.log", "/tmp/xvfb.log", "/tmp/websockify.log"):
                    inner = read_container_file(container.id, p, tail=40)
                    if inner.strip():
                        log.error("容器 %s 的 %s:\n%s", container.id[:12], p, inner)
                return False
        except Exception:
            pass
        if not url:
            ip = container_ip(container)
            if not ip:
                time.sleep(0.5)
                continue
            url = f"http://{ip}:9222/json/version"
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


class ProfileInUseError(RuntimeError):
    """复用持久目录时该目录已被运行中实例占用。"""


class ProfileExistsError(RuntimeError):
    """导入持久目录时本机已存在同名目录。"""


def start_browser_instance(
    sid: str,
    profile_mode: str = "ephemeral",
    profile_name: str = "",
    browser_mode: str = "full",
    vnc_refresh: str = "",
    proxy: str = "",
) -> dict:
    persistent = profile_mode == "persistent"
    # 每实例独立代理：只放行常见 scheme，禁止注入空白/换行/引号。
    proxy = str(proxy or "").strip()
    if proxy and not re.match(r"^(http|https|socks5|socks4)://[^\s'\"]+$", proxy):
        raise ValueError("代理地址格式不合法（仅支持 http/https/socks5/socks4）")
    # 空值 = 用全局档位：新建弹窗已不再提供该选项，全部实例统一由面板切换的档位决定。
    vnc_key = normalize_vnc_refresh(vnc_refresh or get_global_vnc_refresh())
    # 先确认镜像在本地：否则 containers.run 会触发 pull，长时间占着全局锁，
    # 导致面板其它接口（/api/list 等）全部超时。镜像缺失时立即给出提示。
    ensure_image_available(image_for_mode(browser_mode))
    # 端口分配必须在锁内完成，避免并发创建分配到同一端口
    with LOCK:
        # 复用持久目录时，先确认没有其它运行中实例占用同名目录。两个 Chrome 同时
        # 挂同一个 user-data-dir 会触发 SingletonLock 冲突并写坏 Cookie/SQLite，
        # 比「删除时被挡」严重得多，必须在创建这一侧就拦下。检查放在锁内，避免
        # 两个并发请求都通过检查后挂上同一卷。
        if persistent:
            target = profile_name or ""
            for other_sid, item in INSTANCES.items():
                if (item.get("profile_mode") == "persistent"
                        and item.get("profile_name") == target):
                    raise ProfileInUseError(
                        f"实例 {other_sid[:8]} 正在使用该持久目录，请先停止实例再复用"
                    )
        used = collect_used_ports()

        debug_port = find_free(PORT_DEBUG_START, PORT_DEBUG_END, used["debug"])
        if browser_mode in HEADLESS_MODES:
            # 无头模式没有 Xvfb/VNC，不占用也不映射这两个端口
            vnc_port = None
            novnc_port = None
        else:
            vnc_port = find_free(PORT_VNC_START, PORT_VNC_END, used["vnc"])
            novnc_port = find_free(PORT_NOVNC_START, PORT_NOVNC_END, used["novnc"])

        image = image_for_mode(browser_mode)
        labels = {
            CONTAINER_LABEL: MANAGER_ID,
            "cdp.sid": sid,
            BROWSER_MODE_LABEL: browser_mode,
            "cdp.browser.image": image,
            "cdp.profile.mode": profile_mode,
            "cdp.profile.name": profile_name if persistent else "",
            VNC_REFRESH_LABEL: vnc_key,
        }
        environment = {"BROWSER_MODE": browser_mode}
        if proxy:
            environment["BROWSER_PROXY"] = proxy
        if browser_mode == "full":
            # 只有完整内核跑 x11vnc；无头实例不传这组变量（传了也无害，但少点噪音）。
            vp = VNC_REFRESH_PROFILES[vnc_key]
            environment["VNC_WAIT"] = str(vp["wait"])
            environment["VNC_DEFER"] = str(vp["defer"])
        volumes = None
        if persistent:
            name = profile_name or uuid.uuid4().hex[:12]
            vname = profile_volume_name(name)
            # 不存在则创建；已存在则复用，达到"登录态长期保留"
            try:
                client.volumes.get(vname)
            except docker.errors.NotFound:
                client.volumes.create(
                    name=vname,
                    labels={
                        PROFILE_LABEL: "1",
                        PROFILE_OWNER_LABEL: MANAGER_ID,
                        "cdp.profile.name": name,
                    },
                )
            profile_name = name
            labels["cdp.profile.name"] = name
            environment["PROFILE_DIR"] = "/cdp-profile"
            volumes = {vname: {"bind": "/cdp-profile", "mode": "rw"}}
        else:
            environment["PROFILE_DIR"] = "/tmp/cdp-profile"

        published = {"9222/tcp": debug_port}
        if browser_mode == "full":
            published["5900/tcp"] = vnc_port
            published["6080/tcp"] = novnc_port

        run_kwargs = {
            "detach": True,
            "name": f"cdpb-{sid[:8]}",
            "labels": labels,
            "ports": published,
            "environment": environment,
            "volumes": volumes,
            "shm_size": SHM_SIZE,
            "restart_policy": {"Name": "unless-stopped"},
        }
        # 关键：跟 manager 同网，否则跨 bridge 网络被 Docker 隔离，CDP 必然超时。
        same_net = manager_network()
        if same_net:
            run_kwargs["network"] = same_net
            log.info("实例 %s 加入 manager 网络: %s", sid[:8], same_net)
        try:
            container = client.containers.run(image, **run_kwargs)
        except docker.errors.APIError as e:
            if not same_net:
                raise
            # 网络名失效（如 compose 项目名变了）时退回默认 bridge，
            # 后续由 ensure_instances_on_manager_network() 再补接。
            log.warning("使用网络 %s 创建实例失败，退回默认 bridge: %s", same_net, e)
            run_kwargs.pop("network", None)
            container = client.containers.run(image, **run_kwargs)

        info = {
            "sid": sid,
            "container_id": container.id,
            "container_name": container.name,
            "created_at": time.time(),
            "debug_port": debug_port,
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
            "status": container.status,
            "cdp_ready": False,  # 必须在锁内随实例一起初始化，避免与监控线程竞态
            "browser_mode": browser_mode,
            "profile_mode": profile_mode,
            "profile_name": profile_name if persistent else "",
            "vnc_refresh": vnc_key,
        }
        INSTANCES[sid] = info

    log.info(
        "创建实例 %s：内核=%s 镜像=%s CDP=%s VNC=%s",
        sid[:8], browser_mode, image, debug_port, vnc_port,
    )
    # 不再同步空等 CDP 就绪（最长 20 秒会让前端「创建中」显得卡住）。
    # 容器已创建即立即返回，cdp_ready 由后台 cdp_monitor_loop 每 8 秒刷新，
    # 前端拿到 sid 后可立刻在列表显示实例，并异步等待画面连接。
    return info


# 每个实例的身份伪装守护（两种无头都需要：实测 Chrome 158 的 --headless=new 仍报 HeadlessChrome）。
STEALTH_GUARDS: dict[str, object] = {}
STEALTH_LOCK = threading.Lock()


def start_stealth_guard(sid: str, ip: str) -> None:
    """给无头实例挂身份伪装守护；重复调用幂等。"""
    if not ip:
        return
    with STEALTH_LOCK:
        if sid in STEALTH_GUARDS:
            return
        try:
            from stealth import StealthGuard
        except ImportError:
            from app.stealth import StealthGuard
        guard = StealthGuard(sid, f"http://{ip}:9222", log)
        STEALTH_GUARDS[sid] = guard
    guard.start()


def stop_stealth_guard(sid: str) -> None:
    with STEALTH_LOCK:
        guard = STEALTH_GUARDS.pop(sid, None)
    if guard is not None:
        try:
            guard.stop()
        except Exception as e:
            log.debug("停止身份伪装守护失败 %s: %s", sid[:8], e)


def stop_instance(sid: str) -> bool:
    with LOCK:
        item = INSTANCES.get(sid)
    if not item:
        return False
    stop_stealth_guard(sid)
    # 脚本注入守护与注入记录同属该实例的运行时状态，实例销毁后一并清理。
    try:
        stop_inject_watcher(sid)
    except Exception:
        pass
    with LOCK:
        INJECTIONS.pop(sid, None)
    try:
        c = client.containers.get(item["container_id"])
        c.remove(force=True)
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError as e:
        log.warning("删除实例 %s 的容器失败: %s", sid[:8], e)
    finally:
        # 无论 Docker 删除是否报错，都从面板移除，避免"幽灵实例"一直卡在列表里。
        with LOCK:
            INSTANCES.pop(sid, None)
    # 持久模式的 volume 不随容器删除，数据保留
    log.info("停止实例 %s", sid[:8])
    return True


try:
    restore_instances()
except Exception as e:
    # Docker 暂时不可用时仍允许 manager 启动，后续接口给出明确错误。
    log.error("恢复已有实例失败（不影响新建）: %s", e)

# manager 重启后，已运行容器的 x11vnc 仍是创建时的参数；用持久化的全局档位
# 热改一次，让"界面显示 / 实际帧率 / 用户上次选择"三者一致。
try:
    apply_vnc_refresh_to_running(get_global_vnc_refresh())
except Exception as e:
    log.debug("启动时热改 VNC 刷新跳过: %s", e)


def container_ip_by_id(container_id: str) -> str:
    if not container_id:
        return ""
    try:
        c = client.containers.get(container_id)
        return container_ip(c)
    except Exception:
        return ""


def manager_network() -> str:
    """返回 manager 容器所在的 Docker 网络名。

    docker compose 会自动为项目创建专属 bridge 网络；而 Docker SDK 直接
    containers.run() 创建的浏览器容器默认落到 `bridge`。不同 bridge 网络之间
    被 iptables 的 DOCKER-ISOLATION 规则隔离，manager 按容器 IP 访问 CDP 会
    **超时**（不是 ConnectionRefused），表现为 targets 502 / 抓屏不可用。
    让浏览器容器跟 manager 同网即可根治。
    """
    candidates = [os.environ.get("MANAGER_CONTAINER", "cdp-manager")]
    # 容器里 socket.gethostname() 就是自身 container id，改过容器名也能找到。
    try:
        candidates.append(socket.gethostname())
    except Exception:
        pass
    for name in candidates:
        if not name:
            continue
        try:
            c = client.containers.get(name)
            for net in (c.attrs.get("NetworkSettings", {}).get("Networks") or {}):
                if net not in ("host", "none"):
                    return net
        except Exception as e:
            log.debug("按 %s 读取 manager 网络失败: %s", name, e)
    return ""


def container_logs(container_id: str, tail: int = 120) -> str:
    """读取 browser 容器的 stdout/stderr，用于排查启动崩溃。"""
    c = client.containers.get(container_id)
    raw = c.logs(tail=tail, stdout=True, stderr=True)
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def read_container_file(container_id: str, path: str, tail: int = 120) -> str:
    """从容器文件系统读取一个文件（容器退出后仍可读）。

    start.sh 把 chrome 报错重定向到 /tmp/*.log，容器 stdout 看不到；
    这个函数通过 get_archive 把文件从容器里取出来。
    """
    c = client.containers.get(container_id)
    try:
        bits, _stat = c.get_archive(path)
        buf = io.BytesIO()
        for chunk in bits:
            buf.write(chunk)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:") as tf:
            member = next((m for m in tf.getmembers() if m.isfile()), None)
            if member is None:
                return ""
            f = tf.extractfile(member)
            data = f.read() if f else b""
        text = data.decode("utf-8", "replace")
        lines = text.splitlines()
        return "\n".join(lines[-tail:])
    except Exception:
        return ""


def container_diag_files(container_id: str) -> dict:
    """在容器内搜集启动诊断：各后台服务日志 + chrome 动态库缺失检查。

    start.sh 把 chrome/Xvfb 的输出重定向到 /tmp/*.log，容器 stdout 看不到，
    崩溃根因都在这里。容器反复重启时用 exec 在存活窗口读取。
    """
    c = client.containers.get(container_id)
    result = {"files": {}, "ldd": "", "error": ""}
    # 1) 读取容器内重定向的日志文件（退出容器也能读）
    for path in ("/tmp/chromium.log", "/tmp/xvfb.log", "/tmp/x11vnc.log", "/tmp/websockify.log"):
        text = read_container_file(container_id, path, tail=120)
        if text.strip():
            result["files"][path] = text
    # 2) 尝试用 exec 检查 chrome 动态库（仅容器短暂 running 时可用，失败不影响文件读取）
    try:
        c.reload()
        mode = next(
            (v for v in c.attrs.get("Config", {}).get("Env", []) if str(v).startswith("BROWSER_MODE=")),
            "",
        )
        bin_path = "/usr/bin/chrome-headless-shell" if "headless" in str(mode) else "/usr/bin/chrome"
        result["checked_bin"] = bin_path
        code, out = c.exec_run(
            ["sh", "-c", f"ldd {bin_path} 2>&1 | grep -i 'not found' || true"]
        )
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        result["ldd"] = text
    except Exception as e:
        result["ldd"] = "(exec 不可用，容器未在 running: " + type(e).__name__ + ")"
    return result


def manager_container_logs(tail: int = 200) -> dict:
    """读取 manager 自身的全局运行日志（含 uvicorn 访问日志）。

    优先读 manager 容器的 stdout/stderr；读不到时回退到进程内存缓冲。
    返回 source 字段标明来源，便于前端提示。
    """
    name = os.environ.get("MANAGER_CONTAINER", "cdp-manager")
    try:
        c = client.containers.get(name)
        raw = c.logs(tail=tail, stdout=True, stderr=True)
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        return {"source": f"container:{name}", "logs": text}
    except Exception as e:
        # 容器日志不可用（本地开发、权限不足等）时回退到内存缓冲
        lines = list(LOG_BUFFER)[-tail:]
        return {
            "source": f"memory ({type(e).__name__})",
            "logs": "".join(lines),
        }


def container_diagnostics(container_id: str) -> dict:
    """返回容器退出码、OOM、重启次数等诊断字段。"""
    c = client.containers.get(container_id)
    c.reload()
    state = c.attrs.get("State", {}) or {}
    return {
        "status": c.status,
        "exit_code": state.get("ExitCode"),
        "oom_killed": bool(state.get("OOMKilled")),
        "error": state.get("Error", "") or "",
        "restart_count": state.get("RestartCount", 0),
        "started_at": state.get("StartedAt", ""),
        "finished_at": state.get("FinishedAt", ""),
    }


_LAST_NET_FIX = [0.0]


def ensure_instances_on_manager_network() -> None:
    """把历史遗留的浏览器容器补进 manager 网络（升级后无需删实例重建）。

    老版本创建的实例落在默认 bridge，跨网访问 CDP 会超时。manager 更新后
    这里会自动 network connect，旧实例随即恢复；已同网的容器是幂等操作。
    """
    if time.time() - _LAST_NET_FIX[0] < 30:
        return
    _LAST_NET_FIX[0] = time.time()
    net = manager_network()
    if not net:
        return
    with LOCK:
        targets = [
            (sid, item) for sid, item in INSTANCES.items()
            if item.get("status") == "running"
        ]
    for sid, item in targets:
        try:
            c = client.containers.get(item.get("container_id", ""))
            networks = (c.attrs.get("NetworkSettings", {}).get("Networks") or {})
            if net in networks:
                continue
            client.networks.get(net).connect(c)
            log.info("实例 %s 已接入 manager 网络 %s", sid[:8], net)
            with LOCK:
                if sid in INSTANCES:
                    INSTANCES[sid]["cdp_ready"] = False
        except Exception as e:
            log.debug("实例 %s 接入网络 %s 失败: %s", sid[:8], net, e)


def cdp_monitor_loop():
    """后台持续探测 running 实例的 CDP 就绪状态，覆盖 restore 恢复的实例。"""
    import urllib.request

    while True:
        # 整轮包 try：Docker SDK 偶发抛错（守护进程瞬时不可用、容器刚好被删）
        # 不能让线程直接退出，否则所有实例的 cdp_ready 从此永不更新。
        try:
            # 每轮先自愈网络归属，再探测 CDP；已同网时这里只是几次本地查询。
            ensure_instances_on_manager_network()
            with LOCK:
                targets = [
                    (sid, item) for sid, item in INSTANCES.items()
                    if item.get("status") == "running"
                ]
            for sid, item in targets:
                ip = container_ip_by_id(item.get("container_id"))
                ready = False
                if ip:
                    try:
                        with urllib.request.urlopen(
                            f"http://{ip}:9222/json/version", timeout=1
                        ) as resp:
                            ready = resp.status == 200
                    except Exception:
                        ready = False
                with LOCK:
                    if sid in INSTANCES:
                        prev = INSTANCES[sid].get("cdp_ready")
                        INSTANCES[sid]["cdp_ready"] = ready
                        if prev is True and ready is False:
                            log.warning("实例 %s 的 CDP 失联", sid[:8])
                        elif prev is False and ready is True:
                            log.info("实例 %s 的 CDP 恢复", sid[:8])
                # 两种无头都挂身份伪装守护（实测 Chrome 158 的 --headless=new 仍报
                # HeadlessChrome，并非天然干净）。放在锁外避免阻塞列表接口；幂等。
                if ready and item.get("browser_mode") in HEADLESS_MODES:
                    start_stealth_guard(sid, ip)
        except Exception as e:
            log.error("[CDP 监控] 本轮探测异常，8 秒后重试: %s", e)
        time.sleep(8)


def _auto_update_tick() -> None:
    """一次定时检查：无实例且远端有更新时才拉取，并记录结果供设置页展示。"""
    if not AUTO_PULL_IMAGES:
        with AUTO_UPDATE_LOCK:
            AUTO_UPDATE_STATE["last_status"] = "disabled"
            AUTO_UPDATE_STATE["last_detail"] = "自动更新已在配置中关闭（AUTO_PULL_IMAGES=0）"
        log.info("[自动更新] 已关闭，跳过本次检查")
        return

    running = running_instance_count()
    if running:
        with AUTO_UPDATE_LOCK:
            AUTO_UPDATE_STATE["last_status"] = "skipped"
            AUTO_UPDATE_STATE["last_detail"] = f"有 {running} 个运行中的实例，跳过更新；关闭全部实例后下次检查会自动更新"
        log.info("[自动更新] 有 %d 个运行中的实例，跳过本次检查", running)
        return

    # 无实例：先比对远端摘要，确实有更新才拉镜像，避免每轮空耗几分钟拉大镜像。
    try:
        status = check_images_status(force=True)
    except Exception as e:
        with AUTO_UPDATE_LOCK:
            AUTO_UPDATE_STATE["last_status"] = "error"
            AUTO_UPDATE_STATE["last_detail"] = f"检查更新失败: {type(e).__name__}: {e}"
        log.error("[自动更新] 检查更新失败: %s", e)
        return
    outdated = [k for k, v in status.items() if v.get("update_available") or v.get("local_missing")]
    if not outdated:
        unknown = [k for k, v in status.items() if v.get("remote_unknown")]
        with AUTO_UPDATE_LOCK:
            AUTO_UPDATE_STATE["last_status"] = "up_to_date"
            AUTO_UPDATE_STATE["last_detail"] = (
                "两份镜像均已是最新版本" if not unknown
                else "无法检测远端，暂不更新（下次检查会重试）"
            )
        log.info("[自动更新] 无需更新（有更新=%s，远端未知=%s）", outdated, unknown)
        return

    log.info("[自动更新] 检测到镜像更新（%s），开始拉取", ", ".join(outdated))
    result = pull_browser_images()
    stale = any(v.get("stale") for v in result.values())
    # 必须遍历 items()：values() 给出的是结果字典（含 image/ok/stale/local/remote 五个键），
    # 用 k, v 解包会把字典的 5 个键拆给两个变量，抛 "too many values to unpack (expected 2)"，
    # 导致每次自动更新拉取完镜像后都在这里崩溃、状态被误记为 error。
    failed = [k for k, v in result.items() if not v.get("ok")]
    with AUTO_UPDATE_LOCK:
        AUTO_UPDATE_STATE["last_result"] = result
        if failed:
            AUTO_UPDATE_STATE["last_status"] = "error"
            AUTO_UPDATE_STATE["last_detail"] = "部分镜像拉取失败，详见运行日志"
        elif stale:
            AUTO_UPDATE_STATE["last_status"] = "pending"
            AUTO_UPDATE_STATE["last_detail"] = "拉到的仍是旧版本，CI 可能仍在构建，下轮会自动重试"
        else:
            AUTO_UPDATE_STATE["last_status"] = "updated"
            AUTO_UPDATE_STATE["last_detail"] = "已自动更新浏览器镜像，新建实例将使用新版本"


def auto_pull_loop() -> None:
    """定时自动更新循环：每 AUTO_UPDATE_INTERVAL_HOURS 小时检查一次。

    启动时先等片刻做一次检查（兼容 Dockge 只更新 manager、跳过 profile 占位服务），
    之后严格按固定间隔执行；检查间隔与结果写入 AUTO_UPDATE_STATE 供设置页展示。
    """
    # 启动稍等片刻，让 web 服务先可用，拉取大镜像期间面板不卡死。
    time.sleep(5)
    interval = AUTO_UPDATE_INTERVAL_HOURS * 3600
    while True:
        with AUTO_UPDATE_LOCK:
            AUTO_UPDATE_STATE["next_check"] = time.time()
        try:
            _auto_update_tick()
        except Exception as e:
            log.error("[自动更新] 未预期的异常: %s", e)
            with AUTO_UPDATE_LOCK:
                AUTO_UPDATE_STATE["last_status"] = "error"
                AUTO_UPDATE_STATE["last_detail"] = f"{type(e).__name__}: {e}"
        with AUTO_UPDATE_LOCK:
            AUTO_UPDATE_STATE["last_check"] = time.time()
            AUTO_UPDATE_STATE["next_check"] = time.time() + interval
        log.info("[自动更新] 本轮完成，下次检查约 %d 小时后", AUTO_UPDATE_INTERVAL_HOURS)
        time.sleep(interval)


threading.Thread(target=cdp_monitor_loop, daemon=True).start()
threading.Thread(target=auto_pull_loop, daemon=True, name="browser-image-puller").start()


# ---------- 内置 CDP 自测：临时有头/无头实例上跑官方 CDP WebSocket 用例 ----------
SELFTEST_STATE = {
    "running": False, "started": 0.0, "result": {}, "error": "",
    # 当前正在自测的临时实例信息，供前端把画面临时挂到被测浏览器上依次展示。
    # {"sid": ..., "mode": "full"|"headless", "phase": "starting"|"running"|"display"}
    "current": None,
}
SELFTEST_LOCK = threading.Lock()
# 每个内核测完后让临时实例多存活几秒，供面板画面区展示被测浏览器。
SELFTEST_DISPLAY_SECONDS = 6.0


def _selftest_set_current(sid: str, mode: str, phase: str):
    with SELFTEST_LOCK:
        SELFTEST_STATE["current"] = {"sid": sid, "mode": mode, "phase": phase}


def _selftest_one_mode(browser_mode: str) -> dict:
    """在一份临时实例上跑完整自测，无论成败都在结束时销毁实例。

    过程中把临时实例信息写进 SELFTEST_STATE["current"]，并在测试跑完后
    刻意多存活 SELFTEST_DISPLAY_SECONDS 秒（phase=display），让面板画面区
    能挂载并展示这个被测浏览器，然后才销毁。
    """
    sid = str(uuid.uuid4())
    created = False
    try:
        log.info("[自测] 创建临时实例：内核=%s sid=%s", browser_mode, sid[:8])
        _selftest_set_current(sid, browser_mode, "starting")
        info = start_browser_instance(sid, "ephemeral", "", browser_mode)
        created = True
        container = client.containers.get(info["container_id"])
        # 等待 CDP 就绪（自测可以同步等；用户实例创建接口已经改为异步）。
        if not wait_cdp_ready(container, timeout=25.0):
            raise RuntimeError("临时实例 CDP 未就绪")
        ip = container_ip(container)
        if not ip:
            raise RuntimeError("临时实例容器网络未就绪")
        # 两种无头的临时实例都要打身份补丁，否则自测里的身份断言会看到 HeadlessChrome。
        if browser_mode in HEADLESS_MODES:
            start_stealth_guard(sid, ip)
            time.sleep(0.8)  # 给守护一点时间接管现有页面，避免首轮断言读旧值
        # 镜像里 main.py 被平铺到 /app 且以 uvicorn main:app 启动，模块是顶层 selftest；
        # 本地源码目录结构是 app/ 包。两种环境都兼容。
        try:
            from app.selftest import run_all
        except ImportError:
            from selftest import run_all

        def emit(level, name, detail):
            log.info("[自测][%s][%s] %s %s", browser_mode, level, name, detail)

        _selftest_set_current(sid, browser_mode, "running")
        result = run_all(f"http://{ip}:9222", emit, browser_mode=browser_mode)
        # 测试全部跑完，留一段展示窗口，前端趁此把画面挂到该临时实例上。
        _selftest_set_current(sid, browser_mode, "display")
        time.sleep(SELFTEST_DISPLAY_SECONDS)
        return result
    except Exception as e:
        log.error("[自测][%s] 执行异常: %s", browser_mode, e)
        return {"passed": 0, "failed": 1, "cases": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        if created:
            try:
                stop_instance(sid)
                log.info("[自测] 已销毁临时实例 %s", sid[:8])
            except Exception as e:
                log.warning("[自测] 销毁临时实例失败 %s: %s", sid[:8], e)


def selftest_worker():
    log.info("========== 开始内置 CDP 自测（有头 + 老无头 + 新无头）==========")
    modes = ("full", "headless", "headless-new")
    result = {m: {} for m in modes}
    # 先发布空壳结果，前端据此逐卡渲染（full 先出结果，随后两种无头）。
    with SELFTEST_LOCK:
        SELFTEST_STATE["result"] = {"modes": result, "passed": 0, "failed": 0}
    # 整体 try/finally：任何意外异常都必须复位 running，否则自测状态永久卡在
    # running=True，后续所有 POST /api/selftest/run 都会返回 409，再也跑不了。
    try:
        # 顺序与设置页三栏卡片一致：有头 → 老无头 → 新无头。
        for mode in modes:
            label = {
                "full": "完整内核(full)",
                "headless": "老无头(headless-shell)",
                "headless-new": "新无头(chrome --headless=new)",
            }[mode]
            log.info("---------- 自测开始：%s ----------", label)
            one = _selftest_one_mode(mode)
            result[mode] = one
            # 每跑完一个内核就发布一次，前端能逐个看到卡片结果，不必等全部结束。
            with SELFTEST_LOCK:
                SELFTEST_STATE["result"] = {
                    "modes": result,
                    "passed": sum((result[m] or {}).get("passed", 0) for m in result),
                    "failed": sum((result[m] or {}).get("failed", 0) for m in result),
                }
            log.info(
                "---------- 自测结束：%s 通过 %d / 失败 %d ----------",
                label,
                one.get("passed", 0),
                one.get("failed", 0),
            )
        total_pass = sum((result[m] or {}).get("passed", 0) for m in result)
        total_fail = sum((result[m] or {}).get("failed", 0) for m in result)
        log.info(
            "========== 自测完成：通过 %d / 失败 %d ==========",
            total_pass, total_fail,
        )
    except Exception as e:
        log.error("========== 自测 worker 意外异常: %s ==========", e)
        with SELFTEST_LOCK:
            SELFTEST_STATE["error"] = f"{type(e).__name__}: {e}"
    finally:
        total_pass = sum((result[m] or {}).get("passed", 0) for m in result)
        total_fail = sum((result[m] or {}).get("failed", 0) for m in result)
        with SELFTEST_LOCK:
            SELFTEST_STATE["running"] = False
            SELFTEST_STATE["current"] = None
            SELFTEST_STATE["result"] = {
                "modes": result,
                "passed": total_pass,
                "failed": total_fail,
            }


# ---------- CDP 代理：供面板的 screencast 视图管理标签与下发命令 ----------
# 面板页面与 CDP 端口是不同源，浏览器 JS 直接 fetch /json/* 会被 CORS 拦截；
# 因此统一经 manager 用容器内网 IP 代理，前端只跟 8000 端口打交道。

def get_instance(sid: str) -> dict:
    with LOCK:
        item = INSTANCES.get(sid)
    if not item:
        raise HTTPException(status_code=404, detail="实例不存在")
    return item


def valid_target_id(tid: str) -> bool:
    return bool(TARGET_ID_RE.fullmatch(tid or ""))


def instance_by_port(port: int) -> dict | None:
    """按对外 CDP 端口找运行中的实例（同源代理用 /<port>/... 定位目标）。"""
    with LOCK:
        for item in INSTANCES.values():
            if item.get("debug_port") == port and item.get("status") == "running":
                return dict(item)
    return None


def instance_by_novnc_port(port: int) -> dict | None:
    """按对外 noVNC 端口找运行中的实例（VNC 同源代理用 /<port>/... 定位目标）。"""
    with LOCK:
        for item in INSTANCES.values():
            if item.get("novnc_port") == port and item.get("status") == "running":
                return dict(item)
    return None


def cdp_http(sid: str, path: str, method: str = "GET") -> object:
    """从容器内网访问 CDP HTTP 端点，返回解析后的 JSON。"""
    import urllib.request

    if not path.startswith("/"):
        raise RuntimeError("非法 CDP 路径")
    item = get_instance(sid)
    ip = container_ip_by_id(item.get("container_id"))
    if not ip:
        raise RuntimeError("容器网络未就绪")
    url = f"http://{ip}:9222{path}"
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    except Exception as e:
        raise RuntimeError(f"CDP 请求失败: {e}")


def cdp_command(sid: str, target_id: str, method: str, params: dict) -> dict:
    """通过 CDP WebSocket 向指定标签下发命令，等待对应 id 的响应。"""
    import websocket

    item = get_instance(sid)
    ip = container_ip_by_id(item.get("container_id"))
    if not ip:
        raise RuntimeError("容器网络未就绪")
    url = f"ws://{ip}:9222/devtools/page/{target_id}"
    ws = None
    try:
        ws = websocket.create_connection(
            url, timeout=6, suppress_origin=True
        )
        call_id = 1
        ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        deadline = time.time() + 6
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                break
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})
        raise RuntimeError("CDP 命令响应超时")
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # 主页面内联了全部 JS（含原生 DevTools 的 ?ws= URL 构造）。浏览器若缓存
    # 旧 HTML，升级后会继续用旧逻辑，典型症状是原生 DevTools 仍传完整 ws://
    # 前缀而一开就断。强制 no-store，确保每次刷新都拿到当前版本。
    return templates.TemplateResponse(
        request,
        "index.html",
        {},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                 "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/api/vnc-refresh")
async def api_get_vnc_refresh():
    """读取全局 VNC 刷新档位（面板打开时同步显示用）。"""
    return {"refresh": get_global_vnc_refresh()}


@app.post("/api/vnc-refresh")
async def api_set_vnc_refresh(request: Request):
    """全局切换 VNC 刷新档位：持久化 + 对所有运行中的完整内核实例热改。

    前端同时在每一台已接入的远端主机上调用同名接口，实现"全部实例统一"。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    name = payload.get("refresh", "") if isinstance(payload, dict) else ""
    key = set_global_vnc_refresh(name)
    result = await run_in_threadpool(apply_vnc_refresh_to_running, key)
    # 远端主机由控制端面板通过 /remote/<id>/api/vnc-refresh 代理调用；
    # 这里不做服务端 fan-out，避免 A 控制 B、B 又反向控制 A 的环路。
    return JSONResponse(result)


@app.get("/api/hosts")
async def api_hosts(request: Request):
    """主机列表（免鉴权，供面板在输入密钥前判断是否需要鉴权）。

    未通过本机鉴权时不泄露远端主机信息，只返回 needs_key。
    removable=True 表示是面板里添加的，可在界面删除；
    环境变量 REMOTE_HOSTS 配置的主机需在服务端修改。
    """
    needs_key = bool(API_KEY) and not key_ok(
        request.headers.get("x-api-key") or request.query_params.get("key", "")
    )
    with _STORE_LOCK:
        ui_ids = {h["id"] for h in UI_REMOTE_HOSTS}
    return {
        "auth_required": bool(API_KEY),
        "needs_key": needs_key,
        "hosts": [] if needs_key else [
            {"id": "local", "name": "本机", "base": "", "removable": False}
        ] + [
            {"id": h["id"], "name": h["name"], "base": h["base"],
             "removable": h["id"] in ui_ids}
            for h in REMOTE_HOSTS
        ],
    }


def _derive_host_id(base: str) -> str:
    """从地址推导一个不冲突的主机标识，如 b.example.com -> b-example-com。"""
    host = (urlsplit(base).hostname or "").lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", host).strip("-")[:24] or "host"
    with _STORE_LOCK:
        existing = {h["id"] for h in REMOTE_HOSTS} | {"local"}
    hid, n = slug, 2
    while hid in existing:
        hid = f"{slug}-{n}"
        n += 1
    return hid


def _probe_remote(base: str, key: str):
    """添加前先试连远端 /api/list，验证地址可达且密钥正确。"""
    probe = {"id": "probe", "name": "probe", "base": base, "key": key}
    status, raw, _ = remote_request(probe, "/api/list", "GET", None, 10.0)
    body = json.loads(raw.decode("utf-8", "replace"))
    if status != 200 or not isinstance(body, dict) or "instances" not in body:
        raise ValueError(f"远端返回异常（HTTP {status}）")


@app.post("/api/hosts")
async def api_hosts_add(request: Request):
    """面板添加远程主机：粘贴对方面板「远程接入」生成的连接串（地址|令牌）。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    link = str(payload.get("link") or "").strip()
    if link:
        # 连接串格式：地址|令牌（令牌可为空，对方未启用鉴权时允许）
        if "|" not in link:
            raise HTTPException(
                status_code=400,
                detail="连接串格式应为：地址|令牌，例如 https://b.example.com|令牌",
            )
        base, key = link.split("|", 1)
        payload = {"base": base.strip(), "key": key.strip()}
    base = str(payload.get("base") or "").strip().rstrip("/")
    key = str(payload.get("key") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="地址必须以 http:// 或 https:// 开头")
    try:
        await run_in_threadpool(_probe_remote, base, key)
    except Exception as e:
        text = str(e)
        if "401" in text or "Unauthorized" in text:
            raise HTTPException(
                status_code=400,
                detail="远端开启了 API_KEY 鉴权，但连接串里的令牌无效",
            )
        raise HTTPException(
            status_code=400, detail="无法连接远端面板，请确认地址与网络可达：" + text
        )
    hid = str(payload.get("id") or "").strip() or _derive_host_id(base)
    try:
        entry = valid_host_entry({
            "id": hid, "name": name or (urlsplit(base).hostname or hid), "base": base, "key": key,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with _STORE_LOCK:
        UI_REMOTE_HOSTS[:] = [h for h in UI_REMOTE_HOSTS if h["id"] != entry["id"]]
        UI_REMOTE_HOSTS.append(entry)
        saved = save_ui_hosts()
        reload_hosts()
    if not saved:
        log.warning("远程主机 %s 已加入内存但未能写入 %s（检查 DATA_DIR 挂载）", entry["id"], UI_HOSTS_FILE)
    log.info("面板添加远程主机 %s -> %s", entry["id"], entry["base"])
    return {"ok": True, "host": {"id": entry["id"], "name": entry["name"], "base": entry["base"]}}


@app.delete("/api/hosts/{host_id}")
async def api_hosts_delete(host_id: str, request: Request):
    """删除面板添加的远程主机（只移除接入关系，远端实例不受影响）。"""
    with _STORE_LOCK:
        before = len(UI_REMOTE_HOSTS)
        UI_REMOTE_HOSTS[:] = [h for h in UI_REMOTE_HOSTS if h["id"] != host_id]
        if len(UI_REMOTE_HOSTS) == before:
            raise HTTPException(
                status_code=400,
                detail="该主机不是面板添加的（环境变量 REMOTE_HOSTS 配置的需在服务端修改）",
            )
        save_ui_hosts()
        reload_hosts()
    log.info("面板删除远程主机 %s", host_id)
    return {"ok": True}


@app.get("/api/link/token")
async def api_link_tokens(request: Request):
    """列出本机接入令牌（被控端）。已生成令牌的掩码摘要不回传完整值。"""
    if API_KEY and not key_ok(
        request.headers.get("x-api-key") or request.query_params.get("key", "")
    ):
        raise HTTPException(status_code=401, detail="API 密钥无效或缺失")
    with _STORE_LOCK:
        tokens = [
            {"masked": t[:10] + "…" + t[-6:], "created": int((m or {}).get("created", 0)), "token": t}
            for t, m in LINK_TOKENS.items()
        ]
    return {"auth_required": bool(API_KEY), "tokens": tokens}


@app.post("/api/link/token")
async def api_link_token_create(request: Request):
    """生成本机接入令牌：给其他主机的面板粘贴连接串（本机地址|令牌）用。"""
    if API_KEY and not key_ok(
        request.headers.get("x-api-key") or request.query_params.get("key", "")
    ):
        raise HTTPException(status_code=401, detail="API 密钥无效或缺失")
    token = secrets.token_hex(24)
    with _STORE_LOCK:
        LINK_TOKENS[token] = {"created": int(time.time())}
        saved = save_link_tokens()
    if not saved:
        raise HTTPException(
            status_code=500,
            detail="令牌保存失败（数据目录不可写），请检查 DATA_DIR 挂载",
        )
    log.info("生成新的本机接入令牌（当前共 %d 个）", len(LINK_TOKENS))
    return {"ok": True, "token": token, "created": int(time.time())}


@app.delete("/api/link/token")
async def api_link_token_revoke(request: Request, token: str = ""):
    """吊销接入令牌：token 为空时吊销全部。吊销后旧连接串立即失效。"""
    if API_KEY and not key_ok(
        request.headers.get("x-api-key") or request.query_params.get("key", "")
    ):
        raise HTTPException(status_code=401, detail="API 密钥无效或缺失")
    with _STORE_LOCK:
        if token:
            if token not in LINK_TOKENS:
                raise HTTPException(status_code=404, detail="令牌不存在或已吊销")
            del LINK_TOKENS[token]
        else:
            LINK_TOKENS.clear()
        save_link_tokens()
    log.info("吊销接入令牌%s（剩余 %d 个）", "全部" if not token else " " + token[:10] + "…", len(LINK_TOKENS))
    return {"ok": True}


async def build_list_payload(request: Request) -> dict:
    """本机 + 所有远端主机的实例聚合，管理列表与分享观看页共用。"""
    await run_in_threadpool(sync_instances)
    with LOCK:
        snapshot = list(INSTANCES.items())
    profiles = await run_in_threadpool(list_profiles)
    instances = {}
    for sid, item in snapshot:
        view = to_view(item, request)
        view["host"] = "local"
        view["host_name"] = "本机"
        instances[sid] = view

    host_errors = []
    for host in REMOTE_HOSTS:
        try:
            _, raw, _ = await run_in_threadpool(remote_request, host, "/api/list")
            payload = json.loads(raw.decode("utf-8", "replace"))
            for sid, view in (payload.get("instances") or {}).items():
                # key 必须等于 view.sid：前端按 sid 索引实例并决定请求前缀。
                # sid 是 uuid4，跨主机碰撞可忽略；真碰撞时以后端为准覆盖。
                instances[sid] = remote_view(host, view, request)
        except Exception as e:
            log.warning("远端主机 %s 拉取实例失败: %s", host["id"], e)
            host_errors.append({
                "host": host["id"], "name": host["name"], "error": str(e)[:200]
            })
    return {
        "instances": instances,
        "profiles": profiles,
        "host_errors": host_errors,
    }


@app.get("/api/list")
async def api_list(request: Request):
    return await build_list_payload(request)


# ---------- 只读分享 API ----------
def require_manager_key(request: Request):
    if API_KEY and not key_ok(
        request.headers.get("x-api-key") or request.query_params.get("key", "")
    ):
        raise HTTPException(status_code=401, detail="API 密钥无效或缺失")


def share_payload(request: Request) -> dict:
    with _STORE_LOCK:
        data = dict(SHARE_STATE)
        data["sids"] = list(SHARE_STATE["sids"])
    data["active"] = bool(data["token"]) and data["expires"] > time.time()
    data["url"] = (
        str(external_base(request)).rstrip("/") + "/share?token=" + data["token"]
        if data["token"] else ""
    )
    return data


@app.get("/api/share")
async def api_share_get(request: Request):
    require_manager_key(request)
    return share_payload(request)


@app.post("/api/share")
async def api_share_update(request: Request):
    """更新分享实例列表与有效期；已有链接地址不变，只续期。"""
    require_manager_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    sids = body.get("sids") or []
    if not isinstance(sids, list):
        raise HTTPException(status_code=400, detail="sids 必须是数组")
    sids = [str(x) for x in sids if x][:100]
    try:
        ttl = int(body.get("ttl") or 86400)
    except Exception:
        ttl = 86400
    if ttl not in SHARE_TTLS:
        raise HTTPException(status_code=400, detail="有效期只能是 1 小时 / 6 小时 / 24 小时 / 7 天")
    with _STORE_LOCK:
        SHARE_STATE["sids"] = sids
        SHARE_STATE["ttl"] = ttl
        SHARE_STATE["expires"] = int(time.time()) + ttl
        if not SHARE_STATE["token"]:
            SHARE_STATE["token"] = secrets.token_hex(24)
        saved = save_share()
    if not saved:
        raise HTTPException(status_code=500, detail="分享配置保存失败（数据目录不可写）")
    log.info("更新分享配置：%d 个实例，有效期 %d 秒", len(sids), ttl)
    return share_payload(request)


@app.post("/api/share/renew")
async def api_share_renew(request: Request):
    """重置分享令牌：旧链接立即失效，生成新链接。"""
    require_manager_key(request)
    with _STORE_LOCK:
        SHARE_STATE["token"] = secrets.token_hex(24)
        SHARE_STATE["expires"] = int(time.time()) + SHARE_STATE["ttl"]
        saved = save_share()
    if not saved:
        raise HTTPException(status_code=500, detail="分享配置保存失败（数据目录不可写）")
    return share_payload(request)


@app.get("/api/share/view")
async def api_share_view(request: Request, token: str = ""):
    """观看端：仅返回分享的实例和 tid，不暴露 CDP/VNC 管理地址。"""
    if not share_active(token):
        raise HTTPException(status_code=403, detail="分享链接无效或已过期")
    payload = await build_list_payload(request)
    with _STORE_LOCK:
        sids = list(SHARE_STATE["sids"])
        expires = SHARE_STATE["expires"]
    result = {}
    for sid in sids:
        view = payload["instances"].get(sid)
        if not view:
            continue
        tid = ""
        try:
            host_id = view.get("host") or "local"
            if host_id != "local":
                host = next((h for h in REMOTE_HOSTS if h["id"] == host_id), None)
                if host:
                    _, raw, _ = await run_in_threadpool(
                        remote_request, host, f"/api/instance/{sid}/targets"
                    )
                    tl = json.loads(raw.decode("utf-8", "replace")).get("targets") or []
                    tid = str(tl[0].get("id") or "") if tl else ""
            else:
                tl = await run_in_threadpool(cdp_http, sid, "/json/list")
                raw_pages = [
                    {"id": t.get("id", ""), "title": t.get("title", ""),
                     "url": t.get("url", ""), "type": t.get("type", "")}
                    for t in (tl or []) if t.get("type") == "page" and t.get("id")
                ]
                pages = sort_targets_by_open_order(sid, raw_pages)
                tid = str(pages[0]["id"]) if pages else ""
        except Exception as e:
            log.warning("分享实例 %s 读取标签失败: %s", sid[:8], e)
        result[sid] = {
            "sid": sid,
            "host_name": view.get("host_name") or "本机",
            "browser_mode": view.get("browser_mode") or "full",
            "status": view.get("status") or "",
            "cast_ws": view.get("cast_ws") or "",
            "tid": tid,
        }
    return {"instances": result, "expires": expires, "token": token}


@app.get("/share", response_class=HTMLResponse)
async def share_page(request: Request):
    return templates.TemplateResponse(request, "share.html", {})


@app.get("/cast", response_class=HTMLResponse)
async def cast_page(request: Request):
    """独立抓屏页：?sid=&tid= 直达，纯画面 + 简易导航条。

    页面本身免鉴权打开（同 / 与 /share），实例数据走 /api/list、CDP 命令走
    /api/instance/*/cmd、画面走 /ws/cast/*，这些通道各自按 API_KEY 鉴权。
    """
    return templates.TemplateResponse(request, "cast.html", {})


@app.get("/api/health")
async def api_health():
    """存活探针：面板在线且 Docker 守护进程可达。"""
    try:
        await run_in_threadpool(client.ping)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker 不可用: {e}")


def running_instance_count() -> int:
    """当前运行中的浏览器实例数（自愈/自动更新前的安全检查）。"""
    with LOCK:
        return sum(1 for v in INSTANCES.values() if v.get("status") == "running")


@app.post("/api/images/pull")
async def api_pull_images():
    """手动拉取最新的 full/headless 浏览器镜像（Dockge 只更新 manager 时用）。"""
    return await run_in_threadpool(pull_browser_images)


class UpdateBlocked(Exception):
    """因仍有实例运行而拒绝自动更新。"""


def auto_update_when_idle() -> dict:
    """仅在没有任何运行实例时更新浏览器镜像，避免更新后旧实例仍用旧镜像。"""
    n = running_instance_count()
    if n:
        raise UpdateBlocked(f"仍有 {n} 个运行中的实例，请先停止全部实例再更新")
    return pull_browser_images()


@app.post("/api/images/update-auto")
async def api_images_update_auto():
    """自动更新浏览器镜像：仅当无实例运行时执行，否则返回 409 并说明原因。"""
    try:
        return await run_in_threadpool(auto_update_when_idle)
    except UpdateBlocked as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/instances/count")
async def api_instances_count():
    """返回运行实例数，前端据此在更新前提醒用户关闭浏览器。"""
    return {"running": running_instance_count()}


@app.get("/api/images/auto-status")
async def api_images_auto_status():
    """定时自动更新的计划与上次结果，设置页据此展示「下次检查时间」。"""
    with AUTO_UPDATE_LOCK:
        s = dict(AUTO_UPDATE_STATE)
    s["running"] = running_instance_count()
    s["now"] = time.time()
    return s


@app.post("/api/selftest/run")
async def api_selftest_run():
    """启动内置 CDP 自测（后台运行，日志写入全局运行日志，可实时查看）。"""
    with SELFTEST_LOCK:
        if SELFTEST_STATE["running"]:
            raise HTTPException(status_code=409, detail="自测正在进行中，请稍候")
        SELFTEST_STATE["running"] = True
        SELFTEST_STATE["started"] = time.time()
        SELFTEST_STATE["result"] = {}
        SELFTEST_STATE["error"] = ""
        SELFTEST_STATE["current"] = None
    threading.Thread(target=selftest_worker, daemon=True, name="cdp-selftest").start()
    return {"started": True}


@app.get("/api/selftest/status")
async def api_selftest_status():
    """查询自测运行状态与结果摘要。"""
    with SELFTEST_LOCK:
        return {
            "running": SELFTEST_STATE["running"],
            "result": SELFTEST_STATE["result"],
            "error": SELFTEST_STATE["error"],
            "current": SELFTEST_STATE.get("current"),
        }


@app.get("/api/images/status")
async def api_images_status(force: bool = False):
    """对比本地与 GHCR 远端镜像摘要，用于设置入口的更新红点。"""
    return await run_in_threadpool(check_images_status, force)


@app.get("/api/logs/global")
async def api_global_logs(tail: int = 200):
    """manager 自身的全局运行日志，用于整体排查（访问日志、异常堆栈等）。"""
    if tail < 1 or tail > 2000:
        tail = 200
    return await run_in_threadpool(manager_container_logs, tail)


@app.get("/api/instance/{sid}/diag")
async def api_instance_diag(sid: str):
    """读取实例容器内的重定向日志与动态库缺失检查，用于定位启动崩溃根因。"""
    item = get_instance(sid)
    try:
        result = await run_in_threadpool(container_diag_files, item["container_id"])
        # 附带网络归属：CDP 超时最常见的原因就是 manager 与浏览器不同网。
        result["manager_network"] = manager_network()
        c = client.containers.get(item["container_id"])
        result["container_networks"] = list(
            (c.attrs.get("NetworkSettings", {}).get("Networks") or {}).keys()
        )
        return result
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="容器已不存在")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/instance/{sid}/logs")
async def api_instance_logs(sid: str, tail: int = 120):
    """读取实例容器日志与退出状态；容器反复重启时用它定位根因。"""
    item = get_instance(sid)
    try:
        logs = await run_in_threadpool(container_logs, item["container_id"], tail)
        diag = await run_in_threadpool(container_diagnostics, item["container_id"])
        return {"ok": True, "diagnostics": diag, "logs": logs}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="容器已不存在")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/profiles")
async def api_profiles():
    return {"profiles": await run_in_threadpool(list_profiles)}


@app.post("/instance/new")
async def new_instance(request: Request):
    mode = "ephemeral"
    name = ""
    browser_mode = "full"
    host_id = "local"
    # 全局档位：新建弹窗已不再让用户选 VNC 刷新，统一用面板当前保存的档位。
    # 仍接受 payload 里的 vnc_refresh（兼容旧前端 / 远端转发）。
    vnc_refresh = get_global_vnc_refresh()
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            mode = payload.get("profile_mode", mode)
            name = str(payload.get("profile_name", "") or "")
            browser_mode = payload.get("browser_mode", browser_mode)
            host_id = str(payload.get("host") or "local")
            if payload.get("vnc_refresh"):
                vnc_refresh = normalize_vnc_refresh(payload["vnc_refresh"])
    except Exception:
        pass
    vnc_refresh = normalize_vnc_refresh(vnc_refresh)
    proxy = str(payload.get("proxy") or "").strip()

    # 远端主机：由本机代理转发创建，密钥只在本机使用。
    if host_id != "local":
        host = get_remote(host_id)
        if not host:
            raise HTTPException(status_code=404, detail=f"未知主机: {host_id}")
        forwarded = {
            "profile_mode": mode, "profile_name": name,
            "browser_mode": browser_mode, "vnc_refresh": vnc_refresh,
            "proxy": proxy,
        }
        import urllib.error
        try:
            _, raw, _ = await run_in_threadpool(
                remote_request, host, "/instance/new", "POST", forwarded
            )
            view = json.loads(raw.decode("utf-8", "replace"))
            return JSONResponse(remote_view(host, view, request))
        except urllib.error.HTTPError as e:
            # 远端目录被它自己的实例占用时，远端 /instance/new 返回 409；
            # 原样透传状态码与提示，让本机面板知道是哪台主机、哪个目录被占。
            if e.code == 409:
                try:
                    detail = json.loads(e.read().decode("utf-8", "replace")).get(
                        "detail", "远端持久目录正被占用"
                    )
                except Exception:
                    detail = "远端持久目录正被占用"
                raise HTTPException(status_code=409, detail=detail)
            raise HTTPException(status_code=502, detail=f"远端主机创建失败: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"远端主机创建失败: {e}")

    if mode not in ("ephemeral", "persistent"):
        raise HTTPException(status_code=400, detail="profile_mode 只能是 ephemeral 或 persistent")
    if browser_mode not in VALID_BROWSER_MODES:
        raise HTTPException(
            status_code=400,
            detail="browser_mode 只能是 " + " / ".join(VALID_BROWSER_MODES),
        )
    if mode == "persistent" and not PROFILE_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="目录名只能包含字母、数字、下划线、点和短横线，长度 1-48",
        )
    try:
        sid = str(uuid.uuid4())
        data = await run_in_threadpool(
            start_browser_instance, sid, mode, name, browser_mode, vnc_refresh, proxy
        )
        audit("instance.create", sid, detail=f"{mode}/{browser_mode}")
        return JSONResponse(to_view(data, request))
    except ProfileInUseError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        log.warning("创建实例失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


TAB_ORDER_FILE = DATA_DIR / "tab_order.json"
TAB_ORDER: dict = {}  # sid -> [tid, ...]，按打开顺序；切换标签不改变顺序，关闭后其余相对顺序不变


def load_tab_order():
    raw = _load_json(TAB_ORDER_FILE, {})
    TAB_ORDER.clear()
    if isinstance(raw, dict):
        for sid, tids in raw.items():
            if isinstance(tids, list):
                TAB_ORDER[str(sid)] = [str(x) for x in tids if x]


def save_tab_order():
    return _save_json(TAB_ORDER_FILE, TAB_ORDER)


def sort_targets_by_open_order(sid: str, pages: list) -> list:
    """把 CDP 返回的页面标签按打开顺序排列。

    CDP /json/list 的顺序是 Chrome 内部的（通常是最近激活在前），直接展示会让
    "切换标签后顺序跳变"。这里按已记录的顺序排：老标签保持原有相对位置，
    新出现的追加到末尾（脚本 window.open 弹出的新标签也排到最后），关闭的自动消失。
    顺序持久化到数据目录，manager 重启后仍保持。
    """
    known = {t.get("id"): t for t in pages if t.get("id")}
    with _STORE_LOCK:
        old = TAB_ORDER.get(sid, [])
        ordered_ids = [tid for tid in old if tid in known]
        seen = set(ordered_ids)
        # 新标签按 CDP 返回顺序追加到末尾；CDP 一般把最新的放前面，这里反转
        # 让"同时冒出多个新标签"时最新打开的落在最右。
        fresh = [t["id"] for t in pages if t.get("id") and t["id"] not in seen]
        ordered_ids.extend(reversed(fresh))
        if ordered_ids != old:
            TAB_ORDER[sid] = ordered_ids
            save_tab_order()
        return [known[tid] for tid in ordered_ids]


load_tab_order()


@app.get("/api/instance/{sid}/targets")
async def api_targets(sid: str):
    """列出实例里的页面标签，供抓屏视图选择要显示的目标（按打开顺序）。"""
    try:
        raw = await run_in_threadpool(cdp_http, sid, "/json/list")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    pages = [
        {
            "id": t.get("id", ""),
            "title": t.get("title", ""),
            "url": t.get("url", ""),
            "type": t.get("type", ""),
        }
        for t in (raw if isinstance(raw, list) else [])
        if t.get("type") == "page" and t.get("id")
    ]
    return {"targets": sort_targets_by_open_order(sid, pages)}


def normalize_target_url(raw: object) -> str:
    """把用户输入的 baidu.com / localhost:8080 补成 CDP 能接受的完整 URL。

    CDP 的 Page.navigate 和 /json/new 都不接受无协议的裸域名，
    直接透传会得到 Chrome 的 -32000 Cannot navigate to invalid URL。
    """
    s = str(raw or "").strip()
    if not s:
        return "about:blank"
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", s):  # 已有协议：http/https/about/file 等
        return s
    if s.startswith("//"):
        return "https:" + s
    if re.match(r"^localhost(:\d+)?(/|$|\?)", s, re.I):
        return "http://" + s
    return "https://" + s


@app.post("/api/instance/{sid}/targets")
async def api_new_target(sid: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    url = normalize_target_url(payload.get("url", "about:blank"))
    # Chrome 要求 /json/new 使用 PUT，且 URL 必须放在查询串里
    # 返回值是新标签的描述（含 id），前端据此自动切到新标签
    try:
        created = await run_in_threadpool(
            cdp_http, sid, "/json/new?" + quote(url, safe=""), "PUT"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    tid = ""
    if isinstance(created, dict):
        tid = str(created.get("id", "") or "")
    return {"ok": True, "id": tid}


@app.post("/api/instance/{sid}/targets/{tid}/activate")
async def api_activate_target(sid: str, tid: str):
    if not valid_target_id(tid):
        raise HTTPException(status_code=400, detail="非法的标签 ID")
    try:
        await run_in_threadpool(cdp_http, sid, "/json/activate/" + quote(tid, safe=""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


@app.delete("/api/instance/{sid}/targets/{tid}")
async def api_close_target(sid: str, tid: str):
    if not valid_target_id(tid):
        raise HTTPException(status_code=400, detail="非法的标签 ID")

    # 关键：有头 Chrome（full 内核）关掉最后一个窗口会直接退出浏览器进程，
    # start.sh 的 `wait -n` 随即结束容器并触发重启——此时"关完再补"根本来不及，
    # 表现为补建 500、CDP 失联、抓屏 [Errno 111] Connection refused。
    # 因此改为"先建保底、再关目标"：仅当目标是最后一个页面时预先建一个
    # about:blank，保证任何时刻都至少有一个窗口，Chrome 不会退出（headless 同样安全）。
    recreated = False
    new_tid = ""
    try:
        remaining = await run_in_threadpool(cdp_http, sid, "/json/list")
        pages = [t for t in (remaining or []) if t.get("type") == "page" and t.get("id")]
        if len(pages) <= 1 and any(t.get("id") == tid for t in pages):
            created = await run_in_threadpool(
                cdp_http, sid, "/json/new?" + quote("about:blank", safe=""), "PUT"
            )
            if isinstance(created, dict):
                new_tid = str(created.get("id", "") or "")
            recreated = bool(new_tid)
            if recreated:
                log.info(
                    "实例 %s 关闭最后一个标签前已补建保底空白页 %s",
                    sid[:8], new_tid[:12],
                )
            else:
                log.warning("实例 %s 预建保底空白页未返回有效 id", sid[:8])
    except Exception as e:
        # 预建失败不阻断关闭主流程，继续尝试关闭；由前端兜底逻辑处理空标签。
        log.warning("实例 %s 预建保底空白页失败: %s", sid[:8], e)

    try:
        await run_in_threadpool(cdp_http, sid, "/json/close/" + quote(tid, safe=""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"ok": True, "recreated": recreated, "tid": new_tid}


# ---- DevTools 窗口内嵌标签栏用的按端口 API ----
# 注入到 inspector.html 的标签栏脚本从自身 URL 解析 CDP 端口，无需在链接里
# 额外传 sid（用户直接贴 inspector.html?ws=... 时同样能用）。这三个接口
# 完全复用上面按 sid 的实现：新建含 URL 规范化、关闭含"最后一个标签先补
# 保底空白页"的防退出逻辑。远端场景加 /remote/<host> 前缀后由通用代理转发。
def _devtools_sid_by_port(port: int) -> str:
    item = instance_by_port(port)
    return str((item or {}).get("sid") or "")


@app.get("/api/devtools/{port}/tabs")
async def api_devtools_tabs(port: int):
    sid = _devtools_sid_by_port(port)
    if not sid:
        raise HTTPException(status_code=404, detail=f"端口 {port} 没有运行中的实例")
    return await api_targets(sid)


@app.post("/api/devtools/{port}/tabs")
async def api_devtools_new_tab(port: int, request: Request):
    sid = _devtools_sid_by_port(port)
    if not sid:
        raise HTTPException(status_code=404, detail=f"端口 {port} 没有运行中的实例")
    return await api_new_target(sid, request)


@app.delete("/api/devtools/{port}/tabs/{tid}")
async def api_devtools_close_tab(port: int, tid: str):
    sid = _devtools_sid_by_port(port)
    if not sid:
        raise HTTPException(status_code=404, detail=f"端口 {port} 没有运行中的实例")
    return await api_close_target(sid, tid)


@app.post("/api/instance/{sid}/cmd")
async def api_cdp_cmd(sid: str, request: Request):
    """下发 CDP 命令，如 Page.navigate / Page.reload。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    tid = str(payload.get("target_id", "") or "")
    method = str(payload.get("method", "") or "")
    params = payload.get("params") or {}
    if not tid or not method:
        raise HTTPException(status_code=400, detail="缺少 target_id 或 method")
    if not valid_target_id(tid):
        raise HTTPException(status_code=400, detail="非法的标签 ID")
    # 导航条的后退/前进需要读取历史栈并按 entryId 回退/前进；
    # 与 Page.navigate 一样作用在当前 target 上，不接触其他标签页。
    allowed = {
        "Page.navigate",
        "Page.reload",
        "Page.bringToFront",
        "Page.getNavigationHistory",
        "Page.navigateToHistoryEntry",
        "Page.captureScreenshot",
        "Page.printToPDF",
        "Page.getLayoutMetrics",
        "Runtime.evaluate",
        # CDP 脚本注入：新文档持久注入 / 撤销。
        "Page.addScriptToEvaluateOnNewDocument",
        "Page.removeScriptToEvaluateOnNewDocument",
        "Page.enable",
        # 元素检查器：DOM 查询 + 计算样式 + 外链 CSS 内容。
        "DOM.getDocument",
        "DOM.querySelector",
        "DOM.querySelectorAll",
        "DOM.getBoxModel",
        "DOM.getOuterHTML",
        "CSS.getComputedStyleForNode",
        "CSS.getMatchedStylesForNode",
        "CSS.getStyleSheetText",
        # 网络抓包：允许按需启用/停用并读取 CDP 缓存的 Cookie。
        "Network.enable",
        "Network.disable",
        "Network.getCookies",
        "Network.setExtraHTTPHeaders",
        "Network.setUserAgentOverride",
        # Console / 性能 / 设备模拟 / 离线限速。
        "Runtime.enable",
        "Runtime.discardConsoleEntries",
        "Log.enable",
        "Log.clear",
        "Performance.enable",
        "Performance.getMetrics",
        "Emulation.setDeviceMetricsOverride",
        "Emulation.clearDeviceMetricsOverride",
        "Emulation.setUserAgentOverride",
        "Emulation.setGeolocationOverride",
        "Emulation.clearGeolocationOverride",
        "Emulation.setTimezoneOverride",
        "Emulation.setLocaleOverride",
        "Network.emulateNetworkConditions",
    }
    if method not in allowed:
        raise HTTPException(status_code=400, detail="不支持的 CDP 命令: " + method)
    if method == "Page.navigate" and isinstance(params, dict) and "url" in params:
        params["url"] = normalize_target_url(params.get("url"))
    try:
        result = await run_in_threadpool(cdp_command, sid, tid, method, params)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "result": result}


@app.websocket("/ws/cast/{sid}/{tid}")
async def ws_cast(websocket: WebSocket, sid: str, tid: str):
    """抓屏代理：把容器 CDP 的 screencast 帧转发给浏览器，并转发输入事件。

    不直接用 devtools/inspector.html 的原因：它内置 CSP 只放行 ws://127.0.0.1，
    公网访客无法连接其 WS；这里由 manager 中转，前端只需连 8000 端口。
    """
    import websocket as ws_client

    # WS 不走 HTTP 中间件，需单独校验密钥（从查询参数取，浏览器 WS 无法自定义头）。
    # 分享观看端用 share 令牌且只能看配置里列出的实例；即使伪造输入消息也只读。
    share_token = str(websocket.query_params.get("share", "") or "")
    # readonly=1 由本机远端代理转发分享流量时注入：远端自己的分享配置里没有
    # 本机的令牌，不能靠 share_allows 判定。它必须配合合法密钥才生效，
    # 否则任何人加个参数就能绕过 API_KEY 看画面。
    readonly_flag = str(websocket.query_params.get("readonly", "") or "") == "1"
    key_valid = key_ok(websocket.query_params.get("key", ""))
    if readonly_flag:
        if not key_valid:
            await websocket.close(code=4401)
            return
        readonly_share = True
    else:
        readonly_share = share_allows(share_token, sid)
        if not readonly_share and not key_valid:
            await websocket.close(code=4401)
            return

    if not valid_target_id(tid):
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "非法的标签 ID"})
        await websocket.close()
        return

    # 画质档位从 WS 查询串读取，未知档位后端回落均衡，前端无需信任用户输入。
    profile = cast_quality_profile(websocket.query_params.get("quality", ""))
    # 抓屏方式：cast（startScreencast 变化推帧，静止补帧）或 snap（纯定时截图）。
    # 监控档必须走 cast：它靠 screencast + min_interval 限帧，snap 会退化为定时抓图。
    capture_mode = str(websocket.query_params.get("mode", "cast") or "").strip().lower()
    if capture_mode not in ("cast", "snap") or profile.get("min_interval"):
        capture_mode = "cast"
    snap_key = normalize_snap_interval(websocket.query_params.get("snap", ""))
    snap_interval = SNAP_INTERVALS[snap_key]
    # 旧前端/外部脚本不带 snap 参数时用 CAST_KEEPALIVE_SEC 兜底，保持旧行为；
    # 新版前端始终带档位（默认 normal 0.5s），Cast 与 Snap 都按此节奏定时抓帧。
    if not str(websocket.query_params.get("snap", "") or "").strip():
        snap_interval = CAST_KEEPALIVE_SEC
    await websocket.accept()
    upstream = None
    stop = threading.Event()
    send_lock = threading.Lock()
    loop = asyncio.get_running_loop()

    def upstream_send(payload: dict) -> None:
        # websocket-client 的 send 不是线程安全的：帧泵与事件循环都会调用它。
        with send_lock:
            upstream.send(json.dumps(payload))

    try:
        item = get_instance(sid)
        ip = container_ip_by_id(item.get("container_id"))
        if not ip:
            await websocket.send_json({"type": "error", "message": "容器网络未就绪"})
            await websocket.close()
            return
        upstream = ws_client.create_connection(
            f"ws://{ip}:9222/devtools/page/{tid}",
            timeout=10,
            suppress_origin=True,
        )
        # recv 空闲超时设短一点：页面静止时靠超时唤醒循环，检查是否需要补帧、
        # 是否收到 stop 信号；空闲超时会被单独捕获并继续等待，绝不误当断连。
        upstream.settimeout(1)

        # 帧接收线程：转发 screencast 帧 / captureScreenshot 补帧给前端。
        # 两个关键修复：
        # 1) recv 空闲超时不再当断连退出 —— 页面静止（挑战页、静态页）时 CDP
        #    可能几十秒不推帧，旧实现直接 break，画面永久卡住。
        # 2) 持续补帧 —— 超过 CAST_KEEPALIVE_SEC 没有 screencast 帧，就主动
        #    captureScreenshot 推一帧，让静止页面也保持"活着"、画面不冻结。
        def pump():
            call_id = 100
            ack_id = call_id + 1
            shot_id = call_id + 2  # captureScreenshot 用递增 id，避免与 ack 混淆
            metrics_id = call_id + 3  # 查询浏览器真实视口尺寸，供前端换算点击坐标
            pending_shots = set()  # 已发出、尚未收到响应的截图 id（首帧 + 多次补帧可能重叠）
            main_frame = {}  # 记住主 frame id，用于过滤 iframe 的导航事件
            q = profile["quality"]
            # 画质档会把画面缩小（如 960×540），但 CDP 输入坐标用的是浏览器真实
            # 视口（如 1920×1080）。前端必须知道视口尺寸才能把点击坐标按比例回算，
            # 否则「键盘能输入、鼠标点不中」。screencast metadata 会在窗口变化时更新。
            viewport_w = 0
            viewport_h = 0
            upstream_send({"id": call_id, "method": "Page.enable", "params": {}})
            upstream_send({"id": metrics_id, "method": "Page.getLayoutMetrics", "params": {}})
            if capture_mode == "cast":
                upstream_send({
                    "id": ack_id,
                    "method": "Page.startScreencast",
                    "params": {
                        "format": "jpeg",
                        "quality": q,
                        "everyNthFrame": profile["every_nth"],
                        "maxWidth": profile["max_width"],
                        "maxHeight": profile["max_height"],
                    },
                })

            def request_shot() -> bool:
                nonlocal shot_id
                # 上一次补帧还没回来就不再堆请求：页面卡死时截图可能长时间无响应，
                # 无限累积 pending_shots 和 CDP 请求会拖慢通道，最多留 2 个在途。
                if len(pending_shots) >= 2:
                    return False
                shot_id += 1
                pending_shots.add(shot_id)
                try:
                    upstream_send({
                        "id": shot_id,
                        "method": "Page.captureScreenshot",
                        "params": {
                            "format": "jpeg", "quality": q,
                            "fromSurface": True,
                            "captureBeyondViewport": False,
                        },
                    })
                    return True
                except Exception:
                    pending_shots.discard(shot_id)
                    return False

            # 首帧兜底：两种模式都先主动抓一帧，避免等 screencast 推帧的空窗。
            request_shot()
            last_frame = time.monotonic()
            last_sent = 0.0  # 监控档限帧：距离上次转发不足 min_interval 的帧直接丢弃
            min_interval = float(profile.get("min_interval", 0) or 0)
            # 定时保底心跳靠 recv 超时唤醒检查；超时必须短于抓帧间隔才能准时
            # 发下一帧（0.2s 档才不会卡成 1s 一帧）。两种模式统一处理。
            upstream.settimeout(max(0.05, min(snap_interval, 0.25)))
            while not stop.is_set():
                now = time.monotonic()
                # 两模式共用定时节奏：Cast 变化推帧走 screencast（更快），
                # 同时按此间隔兜底抓帧，静止页最长滞后 = snap_interval。
                if now - last_frame >= snap_interval:
                    # 只有真的发出截图请求才重置计时；在途满时不重置，
                    # 这样一旦有响应腾出名额会立刻再补，不会卡住不补帧。
                    if request_shot():
                        last_frame = now
                try:
                    raw = upstream.recv()
                except ws_client.WebSocketTimeoutException:
                    # 空闲超时 ≠ 断连：没有新帧，继续等，绝不退出帧泵。
                    continue
                except Exception:
                    break
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                data = ""
                method = msg.get("method", "")
                if method == "Page.screencastFrame":
                    params = msg.get("params", {})
                    try:
                        upstream_send({
                            "id": ack_id,
                            "method": "Page.screencastFrameAck",
                            "params": {"sessionId": params.get("sessionId", "")},
                        })
                    except Exception:
                        pass
                    data = params.get("data", "")
                    # metadata 带 deviceWidth/deviceHeight，窗口缩放后会变化，
                    # 用它覆盖初次查询值，保证 click 坐标始终对应当前视口。
                    metadata = params.get("metadata") or {}
                    try:
                        dw = int(metadata.get("deviceWidth") or 0)
                        dh = int(metadata.get("deviceHeight") or 0)
                        if dw > 0 and dh > 0:
                            viewport_w, viewport_h = dw, dh
                    except Exception:
                        pass
                    last_frame = time.monotonic()  # 收到实帧，重置补帧计时
                elif msg.get("id") == metrics_id and isinstance(msg.get("result"), dict):
                    # 初次视口尺寸：cssLayoutViewport 是 CSS 像素，正好是
                    # Input.dispatchMouseEvent 使用的坐标系；Snap 模式没有
                    # screencast metadata，只能靠这里拿到尺寸。
                    lay = msg["result"].get("cssLayoutViewport") or {}
                    vis = msg["result"].get("cssVisualViewport") or {}
                    try:
                        viewport_w = int(lay.get("clientWidth") or vis.get("clientWidth") or 0)
                        viewport_h = int(lay.get("clientHeight") or vis.get("clientHeight") or 0)
                    except Exception:
                        pass
                elif msg.get("id") in pending_shots and isinstance(msg.get("result"), dict):
                    # captureScreenshot 响应：{id, result:{data}}；Snap 的每一帧、
                    # Cast 的首帧/保底帧都走这里。收到实帧即重置保底计时，
                    # screencast 帧与截图帧统一对待，避免截图在途时重复发请求。
                    pending_shots.discard(msg["id"])
                    data = msg["result"].get("data", "")
                    last_frame = time.monotonic()

                # 导航事件实时推给前端更新地址栏：网页自动化跳转很快，靠 HTTP
                # 轮询标签列表会滞后数秒，CDP 的 frameNavigated/站内跳转事件是
                # 实时的，抓屏 WS 已经 attach 到该 target，顺手转发即可。
                nav_url = navigation_url_from_cdp(msg, main_frame)
                if nav_url:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_json({"type": "url", "url": nav_url}),
                            loop,
                        ).result(timeout=2)
                    except Exception:
                        break

                if data:
                    # 监控档限帧：CDP 帧照常 ack（不能停流），但转发给前端的
                    # 频率压到约 1 帧/秒，多画面监控才不会拖垮带宽与 CPU。
                    if min_interval:
                        now_send = time.monotonic()
                        if now_send - last_sent < min_interval:
                            data = ""
                        else:
                            last_sent = now_send
                if data:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_json({
                                "type": "frame", "data": data,
                                "vw": viewport_w, "vh": viewport_h,
                            }),
                            loop,
                        ).result(timeout=2)
                    except Exception:
                        break

        threading.Thread(target=pump, daemon=True).start()

        # 前端 -> CDP：输入事件 / viewport / 停止
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                break
            kind = data.get("type")
            # 分享通道只读：丢弃输入与视口操控，观看端无法反向控制浏览器。
            if readonly_share and kind in ("input", "viewport"):
                continue
            if kind == "input":
                method = data.get("method", "")
                if method in (
                    "Input.dispatchMouseEvent",
                    "Input.dispatchKeyEvent",
                    "Input.insertText",
                    "Input.dispatchWheelEvent",
                ):
                    try:
                        upstream_send({
                            "id": 200, "method": method, "params": data.get("params", {})
                        })
                    except Exception:
                        break
            elif kind == "viewport":
                try:
                    upstream_send({
                        "id": 201,
                        "method": "Emulation.setDeviceMetricsOverride",
                        "params": {
                            "width": int(data.get("width", 1280)),
                            "height": int(data.get("height", 720)),
                            "deviceScaleFactor": 1,
                            "mobile": False,
                        },
                    })
                except Exception:
                    break
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)[:200]})
        except Exception:
            pass
    finally:
        stop.set()
        if upstream is not None:
            if capture_mode == "cast":
                try:
                    upstream_send({"id": 999, "method": "Page.stopScreencast", "params": {}})
                except Exception:
                    pass
            try:
                upstream.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


# ---------- 同源 CDP 代理：让 /<port>/... 等价于直连浏览器 CDP 端口 ----------
# 背景：反向代理通常只把 443 转给 manager，浏览器 CDP 端口发布在主机上却无法
# 通过域名访问；且 Chrome 校验 Host，用域名直连 CDP 会被拒。这里由 manager
# 按路径里的端口找到实例，从容器内网访问真实 CDP，并把响应里的 WS 地址改写
# 成同源地址。这样 Playwright/Puppeteer 等客户端用 https://域名/<port> 即可连接。

def rewrite_cdp_ws_urls(text: str, request: Request, port: int, prefix: str = "") -> str:
    """把 CDP JSON 里的 ws://127.0.0.1:9223/devtools/... 改成同源 wss 地址。

    prefix 用于远端聚合：改写为 /remote/<host_id>/<port>/devtools/...，
    否则远端返回的 WS 地址会指向用户浏览器不可达的远端主机。
    """
    base = external_base(request)
    ws_base = "wss://" + base.split("://", 1)[-1] if base.startswith("https://") else "ws://" + base.split("://", 1)[-1]
    # 保留 /devtools/... 之后的部分，替换协议与主机端口。
    pattern = re.compile(r'"(webSocketDebuggerUrl|wss?Url|wsUrl)"\s*:\s*"wss?://[^"]*?(/devtools/[^"]*)"')

    def repl(m):
        return '"' + m.group(1) + '":"' + ws_base + prefix + "/" + str(port) + m.group(2) + '"'

    return pattern.sub(repl, text)


# ---------- 远端主机通用代理（HTTP + WS） ----------
# 浏览器只与本机面板通信；/remote/<host_id>/<原路径> 由本机转发到远端 manager。
# 远端密钥放在服务端配置里，绝不出现在页面或 URL 中。


@app.api_route(
    "/remote/{host_id}/{rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def remote_http_proxy(host_id: str, rest: str, request: Request):
    host = get_remote(host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"未知主机: {host_id}")
    path = rest
    if request.url.query:
        path += "?" + request.url.query
    body = await request.body()

    def do():
        import urllib.error
        import urllib.request

        url = host["base"] + "/" + path
        headers = {}
        if host.get("key"):
            headers["X-API-Key"] = host["key"]
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        req = urllib.request.Request(
            url, data=body if body else None, headers=headers, method=request.method
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "")

    try:
        status, raw, ctype = await run_in_threadpool(do)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"远端主机 {host_id} 请求失败: {e}")

    # 远端 DevTools 的 network.js 同样注入中文「复制为 Python」菜单项。
    # 远端 manager 若已带注入则幂等跳过；未带则由这里补齐。
    if raw and rest.endswith("/devtools/panels/network/network.js"):
        text = raw.decode("utf-8", "replace")
        injected = inject_devtools_python_menu(text)
        if injected != text:
            raw = injected.encode("utf-8")
            ctype = "application/javascript; charset=utf-8"

    # 远端 DevTools 主文档同样注入顶部标签栏（幂等）。远端 manager 若是新版
    # 已自带则跳过；老版本则在此补齐，保证多主机场景下标签栏一致可用。
    if raw and rest.endswith("/devtools/inspector.html"):
        text = raw.decode("utf-8", "replace")
        injected = relax_devtools_csp(text)
        injected = inject_devtools_tabbar(injected)
        if injected != text:
            raw = injected.encode("utf-8")
            ctype = "text/html; charset=utf-8"

    # CDP JSON 里的 WS 地址需要二次改写到本机代理路径。
    first = rest.split("/", 1)[0]
    if first.isdigit() and rest.startswith(first + "/json/") and raw:
        try:
            json.loads(raw.decode("utf-8", "replace"))
            raw = rewrite_cdp_ws_urls(
                raw.decode("utf-8", "replace"), request, int(first),
                prefix="/remote/" + host_id,
            ).encode("utf-8")
        except Exception:
            pass
    return Response(content=raw, status_code=status, media_type=ctype or None)


@app.websocket("/remote/{host_id}/{rest:path}")
async def remote_ws_proxy(websocket: WebSocket, host_id: str, rest: str):
    import websocket as ws_client

    share_token = str(websocket.query_params.get("share", "") or "")
    is_share = share_active(share_token)
    if not is_share and not key_ok(websocket.query_params.get("key", "")):
        await websocket.close(code=4401)
        return
    host = get_remote(host_id)
    if not host:
        await websocket.accept()
        await websocket.send_json({"error": f"未知主机: {host_id}"})
        await websocket.close()
        return

    # 本机 key 与分享令牌都不原样转发：远端 manager 的分享配置里没有本机令牌。
    # 远端 WS 端点（/ws/cast、/{port}/devtools、/{port}/websockify）不走 HTTP
    # 中间件，只从 query 的 key 取密钥、不读 X-API-Key 头。服务端配置的 host key
    # 必须放进 query，否则远端设了 API_KEY 时一律 4401。服务端到服务端，浏览器看不到。
    query_items = [
        (k, v) for k, v in websocket.query_params.multi_items()
        if k not in ("key", "share", "readonly")
    ]
    if host.get("key"):
        query_items.append(("key", host["key"]))
    # 分享流量改传 readonly=1 让上游强制只读（上游 readonly 必须有合法 key）。
    if is_share:
        query_items.append(("readonly", "1"))
    query = urlencode(query_items)
    parsed = urlsplit(host["base"])
    scheme = "wss" if parsed.scheme == "https" else "ws"
    url = f"{scheme}://{parsed.netloc}/{rest}" + ("?" + query if query else "")
    # 头也保留：兼容未来改为读头部的上游，且与 HTTP 代理行为一致。
    headers = []
    if host.get("key"):
        headers.append(f"X-API-Key: {host['key']}")

    # 透传浏览器请求的子协议（noVNC 的 binary）：回显给浏览器，并请求上游。
    # 否则远端 VNC 会因协商失败停在 Connecting。CDP 等文本通道无子协议，None 即可。
    requested = ws_requested_subprotocols(websocket.headers)
    subprotocol = requested[0] if requested else None
    await websocket.accept(subprotocol=subprotocol)
    upstream = None
    stop = threading.Event()
    loop = asyncio.get_running_loop()

    def pump():
        while not stop.is_set():
            try:
                raw = upstream.recv()
            except Exception as e:
                # 远端 DevTools / VNC 都可能长时间静默；读超时不能当断连。
                if ws_is_idle_timeout(e):
                    continue
                break
            if raw is None:
                continue
            try:
                coro = (
                    websocket.send_bytes(raw) if isinstance(raw, (bytes, bytearray))
                    else websocket.send_text(raw)
                )
                asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)
            except Exception:
                break

    try:
        upstream_opts = dict(timeout=10, header=headers, suppress_origin=True)
        if subprotocol:
            upstream_opts["subprotocols"] = [subprotocol]
        upstream = ws_client.create_connection(url, **upstream_opts)
        upstream.settimeout(1)
        threading.Thread(target=pump, daemon=True).start()
        while True:
            try:
                msg = await websocket.receive()
            except Exception:
                break
            if msg.get("type") == "websocket.disconnect":
                break
            payload = msg.get("bytes") if msg.get("bytes") is not None else msg.get("text")
            if payload is None:
                continue
            # 与 noVNC 代理同理：二进制帧必须显式 BINARY opcode，否则上游按
            # 文本帧拒绝。CDP 的 JSON 命令是文本帧，保持默认 TEXT 即可。
            try:
                if isinstance(payload, (bytes, bytearray)):
                    await run_in_threadpool(
                        upstream.send, payload, ws_client.ABNF.OPCODE_BINARY
                    )
                else:
                    await run_in_threadpool(upstream.send, payload)
            except Exception:
                break
    except Exception as e:
        try:
            await websocket.send_json({"error": str(e)[:200]})
        except Exception:
            pass
    finally:
        stop.set()
        if upstream is not None:
            try:
                upstream.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


@app.api_route("/{port:int}/json/{rest:path}", methods=["GET", "PUT", "POST", "DELETE"])
async def cdp_json_proxy(port: int, rest: str, request: Request):
    """CDP HTTP 端点同源代理：/9222/json/version -> 容器 CDP /json/version。"""
    item = instance_by_port(port)
    if not item:
        raise HTTPException(status_code=404, detail=f"端口 {port} 没有运行中的实例")
    ip = container_ip_by_id(item.get("container_id"))
    if not ip:
        raise HTTPException(status_code=502, detail="容器网络未就绪")

    path = "/json/" + rest
    if request.url.query:
        path += "?" + request.url.query
    body = await request.body()

    def do():
        import urllib.request
        req = urllib.request.Request(
            f"http://{ip}:9222{path}",
            data=body if body else None,
            method=request.method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", "replace")

    try:
        raw = await run_in_threadpool(do)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CDP 请求失败: {e}")

    try:
        json.loads(raw)
        raw = rewrite_cdp_ws_urls(raw, request, port)
    except Exception:
        pass
    return Response(content=raw, media_type="application/json; charset=utf-8")


def relax_devtools_csp(text: str) -> str:
    """移除 DevTools frontend 自带的 CSP meta，使其能连 manager 同源 WS。

    Chrome 自带的 /devtools/inspector.html 内置 CSP 只放行 ws://127.0.0.1，
    通过 manager 域名访问时其 WebSocket 会被拦截。该页面只是调试工具外壳，
    不含敏感数据，WS 连接本身仍由 manager 的 key 校验；移除 CSP meta 即可
    让浏览器按默认策略加载资源与建立连接。没有 CSP meta 时原样返回。
    """
    text = re.sub(
        r"<meta\s+[^>]*http-equiv\s*=\s*[\"']Content-Security-Policy[\"'][^>]*>",
        "",
        text,
        flags=re.I,
    )
    text = inject_devtools_ws_normalizer(text)
    return text


# Chrome 158+ 的 DevTools frontend 读取地址栏 ?ws= 时会自行补 ws:// / wss://
# 前缀。若 manager 页面被浏览器缓存了旧 JS，仍会传完整的 ws://host:port/path，
# frontend 拼成 ws://ws//host... 而 DNS 解析失败、窗口一开就断。这里在返回的
# inspector.html 里注入一段最早执行的脚本：发现 ws= 带协议前缀就剥掉，并用
# replaceState 修正地址栏（frontend 从 location.search 读取）。这是后端兜底，
# 即使前端 JS 是旧缓存也能正确建连；已是新格式则原样返回。
DEVTOOLS_WS_NORMALIZER = (
    "<script>(function(){try{var u=new URL(location.href);"
    "var w=u.searchParams.get('ws');"
    "if(w&&/^wss?:\\/\\//i.test(w)){"
    "u.searchParams.set('ws',w.replace(/^wss?:\\/\\//i,''));"
    "history.replaceState(null,'',u.toString());}"
    "}catch(e){}})();</script>"
)


def inject_devtools_ws_normalizer(text: str) -> str:
    """在 inspector.html 的 <title> 后注入 ws= 参数规范化脚本（幂等）。

    Chrome inspector.html 省略了 <head> 标签，故以 <title> 为锚点；该脚本是
    普通同步脚本，早于随后 defer 的 module script 执行，能在 frontend 读取
    location.search 前完成地址栏修正。
    """
    if not text or "__cdmpWsNorm" in text:
        return text
    m = re.search(r"</title\s*>", text, flags=re.I)
    if not m:
        return text
    insert_at = m.end()
    snippet = DEVTOOLS_WS_NORMALIZER.replace("<script>", "<script>/*__cdmpWsNorm*/", 1)
    return text[:insert_at] + snippet + text[insert_at:]


# DevTools 右键「复制为 Python」注入：原生 Network 面板只有 cURL / fetch /
# PowerShell，没有 Python。DevTools frontend 是 ES module，UI/SDK 不挂全局，
# 因此不能靠外部脚本 hook；改为在同源代理 network.js 时，用唯一源码锚点把
# 两个中文菜单项插进右键菜单函数作用域内——这里 t 是 NetworkRequest，
# Ne.InspectorFrontendHost 是 DevTools 自带的复制接口，均为该作用域现成变量。
# 注入是一段自包含 IIFE，全程异常兜底；锚点随版本消失时静默跳过，原生不受影响。
DEVTOOLS_PY_ANCHOR = "this.appendCopyAsPreloadItem(o,t)"

DEVTOOLS_PY_SNIPPET = (
    "this.appendCopyAsPreloadItem(o,t),"
    "function(m,r){"
    "function q(s){return JSON.stringify(String(s==null?'':s))}"
    "function hs(){var d={},a=[];try{a=r.requestHeaders()||[]}catch(e){}"
    "a.forEach(function(x){if(x&&x.name)d[x.name]=x.value});return d}"
    "async function gen(k){var u='',me='GET',bo=null;"
    "try{u=r.url()}catch(e){}try{me=r.requestMethod||'GET'}catch(e){}"
    "try{bo=await r.requestFormData()}catch(e){}"
    "if(!u||/^(data|blob):/.test(u))return;"
    "var hj=JSON.stringify(hs(),null,2),L=['# \\u7531 DevTools \\u6293\\u5305\\u751f\\u6210'];"
    "L.push(k==='r'?'import requests':'import httpx');L.push('');"
    "L.push('url = '+q(u));L.push('headers = '+hj);if(bo)L.push('data = '+q(bo));L.push('');"
    "if(k==='r'){L.push('resp = requests.request(');L.push('  '+q(me)+', url, headers=headers'+(bo?', data=data':''));L.push(')')}"
    "else{L.push('with httpx.Client() as cl:');L.push('  resp = cl.request(');"
    "L.push('    '+q(me)+', url, headers=headers'+(bo?', data=data':''));L.push('  )')}"
    "L.push('print(resp.status_code)');L.push('print(resp.text)');"
    "try{Ne.InspectorFrontendHost.InspectorFrontendHostInstance.copyText(L.join('\\n'))}catch(e){}}"
    "m.defaultSection().appendItem('\\u590d\\u5236\\u4e3a Python (requests)',function(){gen('r')});"
    "m.defaultSection().appendItem('\\u590d\\u5236\\u4e3a Python (httpx)',function(){gen('h')})"
    "}(o,t)"
)


def inject_devtools_python_menu(text: str) -> str:
    """把中文「复制为 Python」菜单注入压缩后的 network.js（幂等）。

    仅当源码锚点恰好出现一次时替换，避免版本漂移造成误改；已注入过则原样返回。
    """
    if not text or "__cdmpyPython" in text:
        return text
    if text.count(DEVTOOLS_PY_ANCHOR) != 1:
        return text
    snippet = DEVTOOLS_PY_SNIPPET.replace("function(m,r){", "function __cdmpyPython(m,r){", 1)
    return text.replace(DEVTOOLS_PY_ANCHOR, snippet, 1)


# DevTools 窗口顶部标签栏：原生 DevTools 是启动时按 ?ws= 绑定单个目标的，
# 切换调试目标只能重新导航窗口到新的 ws（Chrome 自身换目标也是新开窗口，
# 这是前端固有限制）。这里在 inspector.html 里注入一条可折叠标签栏，列出
# 实例的所有页面标签，支持切换/新建/关闭，解决"加载了页面但 DevTools 仍连
# 着旧目标、Network 抓不到包"的问题。脚本从自身 URL 解析端口与远端前缀，
# 因此用户直接贴 inspector.html?ws=... 链接打开时同样可用；鉴权读同源
# localStorage 里的 API_KEY，语言跟随主应用的 cdpLang 并热更新。
DEVTOOLS_TAB_SNIPPET = r"""<script id="__cdmpTabSrc">/*__cdmpTabbar*/
(function(){
if(window.__cdmpTabbarReady)return;window.__cdmpTabbarReady=1;
try{
var path=location.pathname;
var pm=path.match(/(?:^|\/)(\d+)\/devtools\/inspector\.html/);
if(!pm)return;
var port=pm[1];
var rm=path.match(/^\/remote\/([^/]+)\//);
var prefix=rm?('/remote/'+rm[1]):'';
function getk(n){try{return localStorage.getItem(n)||''}catch(e){return ''}}
function wsv(){try{return new URL(location.href).searchParams.get('ws')||''}catch(e){return ''}}
function cur(){var m=wsv().match(/\/devtools\/page\/([0-9a-fA-F]{32})/);return m?m[1]:''}
var DICT={
 zh:{t:'标签页',newph:'输入网址，回车新建',c:'收起',e:'展开',x:'关闭标签',cf:'确定要关闭这个标签吗？',err:'操作失败：'},
 en:{t:'Tabs',newph:'Type URL, Enter to open',c:'Hide',e:'Show',x:'Close tab',cf:'Close this tab?',err:'Failed: '},
 ja:{t:'タブ',newph:'URL を入力して Enter',c:'閉じる',e:'表示',x:'タブを閉じる',cf:'このタブを閉じますか？',err:'失敗: '}
};
function lang(){var l=getk('cdpLang');return DICT[l]?l:'zh'}
function T(k){return DICT[lang()][k]}
async function call(u,o){o=o||{};var h={'X-API-Key':getk('cdpManagerApiKey')};if(o.body)h['Content-Type']='application/json';var r=await fetch(prefix+u,{method:o.method||'GET',headers:h,body:o.body});var j={};try{j=await r.json()}catch(e){}if(!r.ok)throw new Error(j.detail||('HTTP '+r.status));return j}
function go(tid){var w=wsv(),nw=w.replace(/\/devtools\/page\/[0-9a-fA-F]{32}/,'/devtools/page/'+tid);if(nw===w)return;var u=new URL(location.href);u.searchParams.set('ws',nw);location.assign(u.toString())}
var bar=document.createElement('div');bar.id='cdmpTabbar';
bar.style.cssText='position:fixed;top:0;left:0;right:0;height:30px;z-index:2147483647;display:flex;align-items:center;gap:5px;padding:0 8px;box-sizing:border-box;background:#202124;color:#e8eaed;font:12px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;box-shadow:0 1px 4px rgba(0,0,0,.45);';
var title=document.createElement('span');title.textContent=T('t');title.style.cssText='flex:none;opacity:.7;font-size:11px;';
var list=document.createElement('span');list.id='cdmpTabList';list.style.cssText='flex:1;display:flex;align-items:center;gap:3px;overflow-x:auto;white-space:nowrap;scrollbar-width:none;';
var inp=document.createElement('input');inp.type='text';inp.placeholder=T('newph');inp.style.cssText='display:none;flex:1;min-width:80px;max-width:240px;height:20px;border:1px solid #5f6368;border-radius:10px;background:#303134;color:#e8eaed;padding:0 8px;font:12px inherit;outline:none;';
var plus=document.createElement('button');plus.textContent='+';plus.title=T('t');plus.style.cssText='flex:none;width:20px;height:20px;line-height:18px;border:1px solid #5f6368;border-radius:50%;background:#303134;color:#e8eaed;cursor:pointer;font-size:15px;padding:0;';
var fold=document.createElement('button');fold.textContent=T('c');fold.style.cssText='flex:none;border:0;background:none;color:#9aa0a6;cursor:pointer;font-size:11px;padding:0 2px;';
bar.appendChild(title);bar.appendChild(list);bar.appendChild(inp);bar.appendChild(plus);bar.appendChild(fold);
var collapsed=false;
function setCollapsed(v){collapsed=v;bar.style.height=collapsed?'7px':'30px';list.style.display=collapsed?'none':'flex';title.style.display=collapsed?'none':'';plus.style.display=collapsed?'none':'';inp.style.display='none';fold.textContent=collapsed?T('e'):T('c');}
fold.onclick=function(){setCollapsed(!collapsed)};
plus.onclick=function(){inp.style.display='';inp.focus()};
inp.onkeydown=function(e){if(e.key==='Enter'){var v=inp.value.trim();inp.value='';inp.style.display='none';if(v)newTab(v)}else if(e.key==='Escape'){inp.value='';inp.style.display='none'}};
async function newTab(url){try{var d=await call('/api/devtools/'+port+'/tabs',{method:'POST',body:JSON.stringify({url:url})});if(d.id)go(d.id);else refresh()}catch(e){alert(T('err')+e.message)}}
async function closeTab(tid,c){if(!confirm(T('cf')))return;try{var d=await call('/api/devtools/'+port+'/tabs/'+tid,{method:'DELETE'});if(tid===c){var l=await call('/api/devtools/'+port+'/tabs');var a=(l.targets||[]).filter(function(x){return x.id&&x.id!==tid});if(a[0])go(a[0].id);else if(d&&d.tid)go(d.tid)}else refresh()}catch(e){alert(T('err')+e.message)}}
function render(tabs,c){list.innerHTML='';tabs.forEach(function(t){var wrap=document.createElement('span');wrap.style.cssText='flex:none;display:inline-flex;align-items:center;border-radius:4px;overflow:hidden;'+(t.id===c?'background:#3c4043;':'background:#2b2c2f;');var b=document.createElement('button');b.textContent=String(t.title||t.url||'...').slice(0,20);b.title=t.url||'';b.style.cssText='border:0;background:none;color:'+(t.id===c?'#fff':'#c8cacc')+';cursor:pointer;font-size:11px;padding:3px 6px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';b.onclick=function(){if(t.id!==c)go(t.id)};var x=document.createElement('span');x.textContent='\u00d7';x.title=T('x');x.style.cssText='cursor:pointer;opacity:.6;padding:0 5px;font-size:13px;';x.onclick=function(ev){ev.stopPropagation();closeTab(t.id,c)};wrap.appendChild(b);wrap.appendChild(x);list.appendChild(wrap)});}
var lastSig='';
async function refresh(){try{var d=await call('/api/devtools/'+port+'/tabs');var tabs=(d.targets||[]).filter(function(t){return t.id&&t.type==='page'});var c=cur();var sig=lang()+'|'+c+'|'+tabs.map(function(t){return t.id+':'+t.title}).join(',');if(sig===lastSig)return;lastSig=sig;render(tabs,c)}catch(e){}}
function mount(){if(!document.body){setTimeout(mount,60);return}if(!bar.isConnected)document.body.appendChild(bar);}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();
setInterval(function(){mount();refresh()},2500);
window.addEventListener('storage',function(e){if(e.key==='cdpLang'){lastSig='';title.textContent=T('t');inp.placeholder=T('newph');if(!collapsed)fold.textContent=T('c');refresh()}});
refresh();
}catch(e){console.warn('CDMP tabbar unavailable',e)}
})();
</script>"""


def inject_devtools_tabbar(text: str) -> str:
    """在 inspector.html 的 <title> 后注入顶部标签栏脚本（幂等）。"""
    if not text or "__cdmpTabbar" in text:
        return text
    m = re.search(r"</title\s*>", text, flags=re.I)
    if not m:
        return text
    return text[: m.end()] + DEVTOOLS_TAB_SNIPPET + text[m.end() :]


@app.api_route("/{port:int}/devtools/{rest:path}", methods=["GET", "HEAD"])
async def cdp_devtools_http(port: int, rest: str, request: Request):
    """DevTools frontend 静态资源同源代理：/<port>/devtools/inspector.html。

    浏览器自带的 inspector.html 与内核版本天然匹配，无需额外打包前端资源。
    这里从容器内网拉取资源，并对 HTML 改写 CSP 以允许 manager 同源 WS 连接。
    WS 本身由下方同名 websocket 路由透传。
    """
    item = instance_by_port(port)
    if not item:
        raise HTTPException(status_code=404, detail=f"端口 {port} 没有运行中的实例")
    ip = container_ip_by_id(item.get("container_id"))
    if not ip:
        raise HTTPException(status_code=502, detail="容器网络未就绪")

    path = "/devtools/" + rest
    if request.url.query:
        path += "?" + request.url.query

    def do():
        import urllib.request

        req = urllib.request.Request(f"http://{ip}:9222{path}", method=request.method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read(), resp.headers.get("Content-Type", "application/octet-stream")

    try:
        raw, ctype = await run_in_threadpool(do)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"DevTools 资源请求失败: {e}")

    if "html" in ctype.lower() and raw:
        # 直接贴 inspector.html?ws=... URL（不经过主应用）打开时，目标标签可能
        # 处于后台冻结状态，页面显示「此标签页不活跃」、Network 面板抓不到请求。
        # 主应用 openDev 已在前端 activate，但直链场景需后端兜底：解析 ws= 里
        # 的 page/<tid> 并激活。仅在 inspector.html 主文档执行（子资源不带 ws），
        # 激活失败静默忽略，不影响前端资源返回。
        if rest == "inspector.html":
            try:
                from urllib.parse import parse_qs, urlsplit
                ws_val = (parse_qs(urlsplit(str(request.url)).query).get("ws") or [""])[0]
                m = re.search(r"/devtools/page/([0-9A-Fa-f]{32})", ws_val)
                if m and item.get("sid"):
                    await run_in_threadpool(
                        cdp_http, item["sid"], "/json/activate/" + m.group(1)
                    )
            except Exception:
                pass
        text = raw.decode("utf-8", "replace")
        text = relax_devtools_csp(text)
        # 顶部注入标签栏：切换/新建/关闭标签，直链打开也能用（幂等）。
        text = inject_devtools_tabbar(text)
        # 让页面内相对路径资源与 WS 均走 manager 同源；URL 里的 ws= 由前端构造时给定。
        return Response(content=text, media_type="text/html; charset=utf-8")
    # 给 Network 面板注入中文「复制为 Python」菜单项；非 JS 或锚点缺失时原样返回。
    if raw and (rest.endswith("/network.js") or rest == "network.js"):
        text = raw.decode("utf-8", "replace")
        injected = inject_devtools_python_menu(text)
        if injected != text:
            raw = injected.encode("utf-8")
    return Response(content=raw, media_type=ctype)


@app.websocket("/{port:int}/devtools/{rest:path}")
async def cdp_devtools_ws(websocket: WebSocket, port: int, rest: str):
    """CDP WebSocket 通用透传：browser 级与 page 级都支持，供自动化客户端连接。"""
    import websocket as ws_client

    if not key_ok(websocket.query_params.get("key", "")):
        await websocket.close(code=4401)
        return

    item = instance_by_port(port)
    if not item:
        await websocket.close(code=4404, reason=f"端口 {port} 没有运行中的实例")
        return
    ip = container_ip_by_id(item.get("container_id"))
    if not ip:
        await websocket.close(code=503, reason="容器网络未就绪")
        return

    # 先把上游 CDP 连接建好再 accept 浏览器：目标标签已关闭时上游会握手失败，
    # 若先 accept 再报错，DevTools frontend 会误报「Debugging connection was
    # closed」并让用户检查 --remote-allow-origins（实际是 No such target id）。
    upstream = None
    try:
        upstream = await run_in_threadpool(lambda: ws_client.create_connection(
            f"ws://{ip}:9222/devtools/{rest}", timeout=10, suppress_origin=True
        ))
    except Exception as e:
        audit("inspect.devtools_failed", item.get("sid", ""), detail=("upstream=" + rest + " err=" + str(e)[:160]))
        await websocket.close(code=4404, reason=("目标不可达: " + str(e)[:120]))
        return

    await websocket.accept()
    stop = threading.Event()
    loop = asyncio.get_running_loop()

    def pump():
        while not stop.is_set():
            try:
                raw = upstream.recv()
            except Exception as e:
                # 关键：DevTools 空闲时 CDP 长时间无消息，websocket-client
                # 会在 recv 超时后抛异常。旧实现直接 break 并关闭上游，
                # 前端遂弹「Debugging connection was closed」。
                if ws_is_idle_timeout(e):
                    continue
                break
            if raw is None:
                continue
            try:
                # CDP 几乎全是文本帧（JSON），但截图/二进制响应按协议可能以
                # 二进制帧送达；按类型转发避免 send_text 收到 bytes 抛错断连。
                if isinstance(raw, (bytes, bytearray)):
                    coro = websocket.send_bytes(bytes(raw))
                else:
                    coro = websocket.send_text(raw)
                asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)
            except Exception:
                break

    try:
        # 上游已在 accept 前建好；这里只调整读超时并启动转发。
        # 1 秒短超时便于及时响应停止信号，超时由 pump 放行，不当作断连。
        upstream.settimeout(1)
        threading.Thread(target=pump, daemon=True).start()
        while True:
            try:
                msg = await websocket.receive()
            except Exception:
                break
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes") if msg.get("bytes") is not None else msg.get("text")
            if data is None:
                continue
            # 与 noVNC/远端代理同理：二进制帧显式 BINARY opcode，文本帧走默认 TEXT。
            try:
                if isinstance(data, (bytes, bytearray)):
                    await run_in_threadpool(
                        upstream.send, data, ws_client.ABNF.OPCODE_BINARY
                    )
                else:
                    await run_in_threadpool(upstream.send, data)
            except Exception:
                break
    except Exception as e:
        try:
            await websocket.send_json({"error": str(e)[:200]})
        except Exception:
            pass
    finally:
        stop.set()
        if upstream is not None:
            try:
                upstream.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


# ---------- VNC/noVNC 同源代理：让 /<novnc_port>/... 等价于直连 noVNC 端口 ----------
# 背景同 CDP：域名只把 443 转给 manager，浏览器容器发布的 noVNC 主机端口
# 经「域名:端口」访问不可达（混合内容 + 未转发）。这里由 manager 按路径里的
# noVNC 端口找到实例，从容器内网访问 6080 上的 websockify/web 服务。
# noVNC 页面把 WS 地址拼成 <scheme>://<host>[:<port>]/<path>，其中 path 来自
# URL 查询参数 ?path=（默认 websockify）。前端把 path 设为 "<novnc_port>/websockify"，
# WS 便落到同源的 /<port>/websockify 路由，无需改 nginx。

@app.api_route(
    "/{port:int}/{rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def novnc_http_proxy(port: int, rest: str, request: Request):
    """noVNC 静态资源同源代理：/7901/vnc_lite.html -> 容器 :6080/vnc_lite.html。"""
    item = instance_by_novnc_port(port)
    if not item:
        raise HTTPException(status_code=404, detail=f"端口 {port} 没有运行中的 VNC 实例")
    ip = container_ip_by_id(item.get("container_id"))
    if not ip:
        raise HTTPException(status_code=502, detail="容器网络未就绪")

    path = "/" + rest
    if request.url.query:
        path += "?" + request.url.query
    body = await request.body()

    def do():
        import urllib.request

        req = urllib.request.Request(
            f"http://{ip}:6080{path}",
            data=body if body else None,
            method=request.method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read(), resp.headers.get("Content-Type", "application/octet-stream")

    try:
        data, ctype = await run_in_threadpool(do)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"noVNC 请求失败: {e}")
    return Response(content=data, media_type=ctype)


@app.websocket("/{port:int}/websockify")
async def novnc_ws_proxy(websocket: WebSocket, port: int):
    """noVNC websockify 二进制 WS 同源透传（RFB 协议帧，非文本）。"""
    import websocket as ws_client

    if not key_ok(websocket.query_params.get("key", "")):
        await websocket.close(code=4401)
        return

    # noVNC 靠 binary 子协议决定用二进制帧解析 RFB 数据。必须原样回显浏览器
    # 请求的子协议；否则它会退回 base64 模式，把二进制字节当 base64 解，
    # 画面永远停在 Connecting。上游 websockify 也用同一子协议连接，帧模式一致。
    requested = ws_requested_subprotocols(websocket.headers)
    subprotocol = "binary" if "binary" in requested else (requested[0] if requested else None)
    await websocket.accept(subprotocol=subprotocol)
    item = instance_by_novnc_port(port)
    if not item:
        await websocket.close(code=1008)
        return
    ip = container_ip_by_id(item.get("container_id"))
    if not ip:
        await websocket.close(code=1011)
        return

    upstream = None
    stop = threading.Event()
    loop = asyncio.get_running_loop()

    def pump():
        while not stop.is_set():
            try:
                raw = upstream.recv()
            except Exception as e:
                # 静态 VNC 画面可能几十秒无更新；读超时必须继续等待。
                if ws_is_idle_timeout(e):
                    continue
                break
            if raw is None:
                continue
            try:
                # binary 模式下 recv 返回 bytes；base64 模式下返回 str。
                # 按类型回发对应帧，避免把 base64 文本错当二进制帧。
                if isinstance(raw, (bytes, bytearray)):
                    coro = websocket.send_bytes(bytes(raw))
                else:
                    coro = websocket.send_text(raw)
                asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)
            except Exception:
                break

    try:
        upstream_opts = dict(timeout=10, suppress_origin=True)
        if subprotocol:
            upstream_opts["subprotocols"] = [subprotocol]
        upstream = ws_client.create_connection(
            f"ws://{ip}:6080/websockify", **upstream_opts
        )
        upstream.settimeout(1)
        threading.Thread(target=pump, daemon=True).start()
        while not stop.is_set():
            try:
                msg = await websocket.receive()
            except Exception:
                break
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            is_binary = data is not None
            if not is_binary:
                text = msg.get("text")
                data = text.encode("utf-8") if text else b""
            # websocket-client 的 send() 默认 opcode=TEXT，直接发 bytes 会被
            # websockify 当作文本帧拒绝（"Unsupported: Text frames are not
            # supported"）并关闭连接 —— VNC 就此卡在 Connecting。RFB 帧必须
            # 显式用 BINARY opcode 转发。
            try:
                if is_binary:
                    await run_in_threadpool(
                        upstream.send, data, ws_client.ABNF.OPCODE_BINARY
                    )
                else:
                    await run_in_threadpool(upstream.send, data)
            except Exception:
                break
    finally:
        stop.set()
        if upstream is not None:
            try:
                upstream.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/instance/{sid}/stop")
@app.post("/api/instance/{sid}/stop")
async def stop_one(sid: str):
    await run_in_threadpool(stop_instance, sid)
    audit("instance.stop", sid)
    return {"ok": True}


@app.post("/instance/stop-all")
@app.post("/api/instance/stop-all")
async def stop_all():
    with LOCK:
        sids = list(INSTANCES.keys())
    for sid in sids:
        await run_in_threadpool(stop_instance, sid)
    return {"ok": True}


@app.get("/profile/{name}/export")
async def export_profile(name: str):
    """导出持久目录为 tar 字节（远端迁移的数据源）。"""
    try:
        raw = await run_in_threadpool(export_profile_bytes, name)
        return Response(
            content=raw,
            media_type="application/x-tar",
            headers={"Content-Disposition": f'attachment; filename="profile-{name}.tar"'},
        )
    except ProfileInUseError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/profile/import")
async def import_profile(request: Request):
    """从远端主机导入持久目录到本机。

    payload: {host: <远端主机 id>, source_name: <远端目录名>, name: <本机目录名>}
    远端 tar 走本机 /remote/<host> 代理，远端 key 由服务端配置提供。
    """
    import urllib.error
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    host_id = str(payload.get("host") or "")
    source_name = str(payload.get("source_name") or "")
    name = str(payload.get("name") or "")
    if not host_id or host_id == "local":
        raise HTTPException(status_code=400, detail="host 必须是远端主机 id")
    if not source_name or not name:
        raise HTTPException(status_code=400, detail="source_name 与 name 不能为空")
    host = get_remote(host_id)
    if not host:
        raise HTTPException(status_code=404, detail=f"未知主机: {host_id}")
    try:
        _, raw, _ = await run_in_threadpool(
            remote_request,
            host,
            "/profile/" + quote(source_name, safe="") + "/export",
            "GET", None, 300.0,
        )
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("detail", "")
        except Exception:
            detail = ""
        raise HTTPException(
            status_code=409 if e.code == 409 else 502,
            detail=detail or f"远端导出失败: HTTP {e.code}",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"远端导出失败: {e}")
    try:
        await run_in_threadpool(import_profile_bytes, name, raw)
        return {"ok": True, "name": name, "bytes": len(raw)}
    except ProfileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ProfileInUseError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/profile/{name}")
async def delete_profile(name: str):
    try:
        ok = await run_in_threadpool(remove_profile, name)
        return {"ok": ok}
    except ProfileInUseError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================================================
# 扩展功能基础设施：通用持久化（审计日志 / 标签 / 脚本库 / 模板 / 定时任务 /
# 录制脚本共用）。沿用 DATA_DIR 挂载，容器重建不丢；_STORE_LOCK 保证读写原子。
# ===========================================================================
STORE_FILES = {
    "audit": DATA_DIR / "audit_log.json",
    "tags": DATA_DIR / "instance_tags.json",
    "snippets": DATA_DIR / "js_snippets.json",
    "templates": DATA_DIR / "instance_templates.json",
    "schedules": DATA_DIR / "schedules.json",
    "recordings": DATA_DIR / "recordings.json",
    # CDP 脚本注入：自定义脚本库（内置脚本在 scripts_builtin.py 里，不落盘）。
    "inject_scripts": DATA_DIR / "inject_scripts.json",
}

# CDP 脚本注入的运行态：注入记录 + 自动跟随新标签守护。
# 记录不是持久化数据：实例停止即失效，重启后 identifier 也已失效。
INJECTIONS: Dict[str, list] = {}
WATCHERS: Dict[str, dict] = {}


def store_load(kind: str, default=None):
    if kind not in STORE_FILES:
        raise ValueError("未知存储类型: " + kind)
    with _STORE_LOCK:
        return _load_json(STORE_FILES[kind], default if default is not None else [])


def store_save(kind: str, data) -> bool:
    if kind not in STORE_FILES:
        raise ValueError("未知存储类型: " + kind)
    with _STORE_LOCK:
        return _save_json(STORE_FILES[kind], data)


def audit(action: str, sid: str = "", host: str = "local", detail: str = "") -> None:
    """记一条语义化操作审计，写失败不影响主操作。最多保留 2000 条。"""
    try:
        rows = store_load("audit", [])
        rows.append({
            "ts": time.time(),
            "action": action,
            "sid": sid,
            "host": host,
            "detail": detail[:300],
        })
        store_save("audit", rows[-2000:])
    except Exception as e:
        log.warning("审计日志写入失败: %s", e)


@app.get("/api/audit")
async def api_audit_get(limit: int = 200):
    rows = store_load("audit", [])
    return {"rows": rows[-max(1, min(limit, 2000)):][::-1]}


@app.delete("/api/audit")
async def api_audit_clear():
    store_save("audit", [])
    return {"ok": True}


# ---- 实例标签 / 备注：按 sid 存，跟随实例删除自动清理 ----
@app.get("/api/tags")
async def api_tags_get():
    return {"tags": store_load("tags", {})}


@app.post("/api/tags/{sid}")
async def api_tags_set(sid: str, request: Request):
    body = await request.json()
    tags = store_load("tags", {})
    entry = {
        "label": str(body.get("label") or "")[:80],
        "tags": [str(x)[:30] for x in (body.get("tags") or [])][:20],
    }
    tags[sid] = entry
    store_save("tags", tags)
    audit("tag.set", sid, detail=entry["label"])
    return {"ok": True, "entry": entry}


@app.delete("/api/tags/{sid}")
async def api_tags_delete(sid: str):
    tags = store_load("tags", {})
    tags.pop(sid, None)
    store_save("tags", tags)
    return {"ok": True}


# ---- 脚本片段库 ----
@app.get("/api/snippets")
async def api_snippets_get():
    return {"snippets": store_load("snippets", [])}


@app.post("/api/snippets")
async def api_snippets_save(request: Request):
    body = await request.json()
    items = store_load("snippets", [])
    item = {
        "id": str(body.get("id") or uuid.uuid4().hex),
        "name": str(body.get("name") or "未命名")[:60],
        "code": str(body.get("code") or "")[:20000],
    }
    idx = next((i for i, x in enumerate(items) if x.get("id") == item["id"]), -1)
    if idx >= 0:
        items[idx] = item
    else:
        items.append(item)
    store_save("snippets", items)
    return {"ok": True, "item": item}


@app.delete("/api/snippets/{sid}")
async def api_snippets_delete(sid: str):
    items = [x for x in store_load("snippets", []) if x.get("id") != sid]
    store_save("snippets", items)
    return {"ok": True}


# ---- 实例配置模板 ----
@app.get("/api/templates")
async def api_templates_get():
    return {"templates": store_load("templates", [])}


@app.post("/api/templates")
async def api_templates_save(request: Request):
    body = await request.json()
    items = store_load("templates", [])
    item = {
        "id": str(body.get("id") or uuid.uuid4().hex),
        "name": str(body.get("name") or "未命名模板")[:60],
        "config": body.get("config") or {},
    }
    idx = next((i for i, x in enumerate(items) if x.get("id") == item["id"]), -1)
    if idx >= 0:
        items[idx] = item
    else:
        items.append(item)
    store_save("templates", items)
    return {"ok": True, "item": item}


@app.delete("/api/templates/{tid}")
async def api_templates_delete(tid: str):
    items = [x for x in store_load("templates", []) if x.get("id") != tid]
    store_save("templates", items)
    return {"ok": True}


# ---- 录制脚本（前端事件序列，后端只存取） ----
@app.get("/api/recordings")
async def api_recordings_get():
    return {"recordings": store_load("recordings", [])}


@app.post("/api/recordings")
async def api_recordings_save(request: Request):
    body = await request.json()
    items = store_load("recordings", [])
    item = {
        "id": str(body.get("id") or uuid.uuid4().hex),
        "name": str(body.get("name") or "未命名录制")[:60],
        "events": body.get("events") or [],
    }
    idx = next((i for i, x in enumerate(items) if x.get("id") == item["id"]), -1)
    if idx >= 0:
        items[idx] = item
    else:
        items.append(item)
    store_save("recordings", items)
    return {"ok": True, "item": item}


@app.delete("/api/recordings/{rid}")
async def api_recordings_delete(rid: str):
    items = [x for x in store_load("recordings", []) if x.get("id") != rid]
    store_save("recordings", items)
    return {"ok": True}



# ===========================================================================
# 方向一：CDP 事件流代理。网络抓包 / Console 日志 / 性能面板共用一条 WS，
# 通过 domain 查询参数决定启用哪些 CDP 域，避免三套重复的转发代码。
# 前端连 manager 的 /ws/inspect，manager 连容器 CDP，鉴权同 /ws/cast。
# ===========================================================================
INSPECT_DOMAINS = {
    "network": ["Network.enable", "Runtime.enable"],
    "console": ["Runtime.enable", "Log.enable"],
    "performance": ["Performance.enable"],
    # all：九面板共用的全功能域，启用后前端按 method 自行分发事件。
    # 只启用事件型域；Emulation/Tracing/Storage 等命令型域按需直接调用即可。
    "all": [
        "Runtime.enable", "Log.enable", "Network.enable",
        "DOM.enable", "CSS.enable", "DOMDebugger.enable",
        "Debugger.enable", "Performance.enable", "HeapProfiler.enable",
        "DOMStorage.enable", "IndexedDB.enable", "ServiceWorker.enable",
        "Security.enable", "Page.enable",
    ],
}


@app.websocket("/ws/inspect/{sid}/{tid}")
async def ws_inspect(websocket: WebSocket, sid: str, tid: str):
    import websocket as ws_client

    if not key_ok(websocket.query_params.get("key", "")):
        await websocket.close(code=4401)
        return
    if not valid_target_id(tid):
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "非法的标签 ID"})
        await websocket.close()
        return
    domain = str(websocket.query_params.get("domain", "console") or "").strip().lower()
    if domain not in INSPECT_DOMAINS:
        domain = "console"
    await websocket.accept()

    upstream = None
    stop = threading.Event()
    send_lock = threading.Lock()
    loop = asyncio.get_running_loop()
    call_id = [100]

    def upstream_send(payload: dict) -> None:
        with send_lock:
            upstream.send(json.dumps(payload))

    try:
        item = get_instance(sid)
        ip = container_ip_by_id(item.get("container_id"))
        if not ip:
            await websocket.send_json({"type": "error", "message": "容器网络未就绪"})
            await websocket.close()
            return
        upstream = ws_client.create_connection(
            f"ws://{ip}:9222/devtools/page/{tid}", timeout=10, suppress_origin=True
        )
        upstream.settimeout(1)
        # 启用事件域。使用独立负数 id 段，避免与前端命令 id 冲突误触发前端回调。
        enable_id = [-1]
        for m in INSPECT_DOMAINS[domain]:
            enable_id[0] -= 1
            upstream_send({"id": enable_id[0], "method": m, "params": {}})
        audit("inspect.open", sid, detail="domain=" + domain)

        def pump():
            while not stop.is_set():
                try:
                    raw = upstream.recv()
                except Exception as e:
                    # 只放行空闲超时；真正断开时退出，避免 100% CPU 空转。
                    if ws_is_idle_timeout(e):
                        continue
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "id" in msg:
                    # 命令响应：转发给前端（用于 getResponseBody / getMetrics 等主动调用）
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "result", "msg": msg}), loop
                    )
                    continue
                method = msg.get("method", "")
                params = msg.get("params", {})
                # 按域过滤，只推前端订阅的事件，避免画面帧等无关流量。
                keep = False
                if domain == "all":
                    # 全功能模式：前端按 method 分发给九个面板，后端不再过滤。
                    keep = True
                elif domain == "network" and method.startswith("Network."):
                    keep = True
                elif domain == "console" and (
                    method in ("Runtime.consoleAPICalled", "Runtime.exceptionThrown",
                               "Log.entryAdded")
                ):
                    keep = True
                elif domain == "performance" and method == "Performance.metrics":
                    keep = True
                if keep:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "event", "method": method, "params": params}),
                        loop,
                    )

        t = threading.Thread(target=pump, daemon=True)
        t.start()

        # 前端 → CDP：转发命令（如 Network.getResponseBody 取响应体）。
        while True:
            try:
                text = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                req = json.loads(text)
            except Exception:
                continue
            if req.get("type") == "cmd" and req.get("method"):
                # 沿用前端自带的 id；前端按此 id 关联回调，后端不得另分配。
                cid = req.get("id")
                if not isinstance(cid, int):
                    call_id[0] += 1
                    cid = call_id[0]
                upstream_send({
                    "id": cid,
                    "method": req["method"],
                    "params": req.get("params") or {},
                })
                # 响应通过 pump 以 type=result 回推；前端按 id 关联。
                await websocket.send_json({"type": "sent", "id": cid})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)[:200]})
        except Exception:
            pass
    finally:
        stop.set()
        if upstream is not None:
            try:
                upstream.close()
            except Exception:
                pass


# ---- 批量执行 CDP 命令：对多个实例/标签下发同一 Runtime.evaluate ----
@app.post("/api/batch/exec")
async def api_batch_exec(request: Request):
    body = await request.json()
    targets = body.get("targets") or []   # [{sid, tid}]
    method = str(body.get("method") or "Runtime.evaluate")
    params = body.get("params") or {}
    if method != "Runtime.evaluate":
        raise HTTPException(status_code=400, detail="批量执行仅支持 Runtime.evaluate")
    results = []
    for t in targets[:100]:
        sid = str(t.get("sid") or "")
        tid = str(t.get("target_id") or t.get("tid") or "")
        if not sid or not valid_target_id(tid):
            results.append({"sid": sid, "ok": False, "error": "参数不合法"})
            continue
        try:
            r = await run_in_threadpool(cdp_command, sid, tid, method, params)
            results.append({"sid": sid, "ok": True, "result": r})
        except Exception as e:
            results.append({"sid": sid, "ok": False, "error": str(e)[:200]})
    audit("batch.exec", detail=f"{len(results)} targets, method={method}")
    return {"results": results}


# ---- 资源监控：逐实例容器 CPU/内存/网络 ----
@app.get("/api/metrics")
async def api_metrics():
    out = []
    with LOCK:
        items = list(INSTANCES.values())
    for item in items:
        try:
            c = client.containers.get(item["container_id"])
            c.reload()
            st = c.stats(stream=False)
            cpu = st.get("cpu_stats", {})
            mem = st.get("memory_stats", {})
            precpu = st.get("precpu_stats", {})
            cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - \
                precpu.get("cpu_usage", {}).get("total_usage", 0)
            sys_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
            cpu_pct = round((cpu_delta / sys_delta) * (cpu.get("cpu_usage", {}).get(
                "percpu_usage") and len(cpu["cpu_usage"]["percpu_usage"]) or 1) * 100, 1) \
                if sys_delta else 0
            mem_used = mem.get("usage", 0)
            mem_limit = mem.get("limit", 1)
            out.append({
                "sid": item.get("sid"),
                "host": "local",
                "cpu_pct": cpu_pct,
                "mem_used": mem_used,
                "mem_limit": mem_limit,
                "mem_pct": round(mem_used / mem_limit * 100, 1) if mem_limit else 0,
            })
        except Exception:
            continue
    return {"metrics": out}


# ---- 定时任务：轻量调度器，持久化到 schedules.json ----
SCHEDULE_JOBS = {}
SCHEDULE_LOCK = threading.Lock()


def schedule_run(job: dict) -> None:
    """执行一条定时任务：创建/停止实例，或 Runtime.evaluate。"""
    action = job.get("action")
    try:
        if action == "stop":
            req = urllib.request.Request(
                "http://127.0.0.1:8000/instance/" + quote(job.get("sid", ""), safe="") + "/stop",
                method="POST",
            )
            if API_KEY:
                req.add_header("X-API-Key", API_KEY)
            urllib.request.urlopen(req, timeout=10).read()
        elif action == "eval":
            targets = job.get("targets") or []
            for t in targets[:50]:
                cdp_command(t.get("sid", ""), t.get("tid", ""), "Runtime.evaluate",
                            {"expression": job.get("expression", ""), "returnByValue": True})
        audit("schedule.run", detail=job.get("name", ""))
    except Exception as e:
        log.warning("定时任务 %s 执行失败: %s", job.get("name"), e)


def schedule_tick() -> None:
    now = time.time()
    with SCHEDULE_LOCK:
        jobs = store_load("schedules", [])
    for job in jobs:
        if not job.get("enabled") or job.get("next_run", 0) > now:
            continue
        threading.Thread(target=schedule_run, args=(job,), daemon=True).start()
        with SCHEDULE_LOCK:
            current = store_load("schedules", [])
            for j in current:
                if j.get("id") == job.get("id"):
                    interval = max(60, int(job.get("interval_sec") or 3600))
                    j["next_run"] = now + interval
                    j["last_run"] = now
            store_save("schedules", current)


def schedule_loop() -> None:
    while True:
        try:
            schedule_tick()
        except Exception as e:
            log.warning("定时任务轮询异常: %s", e)
        time.sleep(5)


threading.Thread(target=schedule_loop, name="schedule-loop", daemon=True).start()


@app.get("/api/schedules")
async def api_schedules_get():
    return {"schedules": store_load("schedules", [])}


@app.post("/api/schedules")
async def api_schedules_save(request: Request):
    body = await request.json()
    items = store_load("schedules", [])
    item = {
        "id": str(body.get("id") or uuid.uuid4().hex),
        "name": str(body.get("name") or "未命名任务")[:60],
        "action": body.get("action") if body.get("action") in ("stop", "eval") else "eval",
        "enabled": bool(body.get("enabled", True)),
        "interval_sec": max(60, int(body.get("interval_sec") or 3600)),
        "sid": str(body.get("sid") or ""),
        "targets": body.get("targets") or [],
        "expression": str(body.get("expression") or "")[:10000],
        "next_run": time.time() + max(60, int(body.get("interval_sec") or 3600)),
        "last_run": 0,
    }
    idx = next((i for i, x in enumerate(items) if x.get("id") == item["id"]), -1)
    if idx >= 0:
        item["next_run"] = items[idx].get("next_run", item["next_run"])
        items[idx] = item
    else:
        items.append(item)
    store_save("schedules", items)
    return {"ok": True, "item": item}


@app.delete("/api/schedules/{job_id}")
async def api_schedules_delete(job_id: str):
    items = [x for x in store_load("schedules", []) if x.get("id") != job_id]
    store_save("schedules", items)
    return {"ok": True}


# ===========================================================================
# CDP 脚本注入：页面 JS 可见层的指纹伪装 / 反调试 / 环境模拟 / 实用片段。
# 与 stealth.py 的 UA/Client Hints 覆盖互补：stealth 改的是浏览器身份层，
# 这里改的是页面可读的 JS 对象层。网络层指纹（TLS/JA3、HTTP/2）不在范围内。
# ===========================================================================
def load_inject_scripts() -> dict:
    data = store_load("inject_scripts", {})
    return data if isinstance(data, dict) else {}


def resolve_inject_script(script_id: str):
    builtin = get_script(script_id)
    if builtin:
        return builtin
    return load_inject_scripts().get(script_id)


def all_inject_scripts() -> list:
    items = [{**s, "origin": "builtin"} for s in BUILTIN_SCRIPTS]
    for sid_, s in load_inject_scripts().items():
        items.append({**s, "id": sid_, "origin": "custom"})
    return items


def cdp_browser_ws_url(sid: str) -> str:
    info = cdp_http(sid, "/json/version")
    url = str(info.get("webSocketDebuggerUrl", ""))
    if not url:
        raise RuntimeError("未获取到 browser WebSocket 地址")
    item = get_instance(sid)
    ip = container_ip_by_id(item.get("container_id"))
    if not ip:
        raise RuntimeError("容器网络未就绪")
    parsed = urlsplit(url)
    return f"ws://{ip}:{parsed.port or 9222}{parsed.path}"


def cdp_ws_call(url: str, method: str, params: dict = None,
                session_id: str = None, timeout: int = 6) -> dict:
    import websocket as ws_client

    ws = ws_client.create_connection(url, timeout=timeout, suppress_origin=True)
    try:
        call_id = 1
        msg = {"id": call_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = json.loads(ws.recv())
            except Exception:
                break
            if data.get("id") == call_id:
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return data.get("result", {})
        raise RuntimeError("CDP 命令响应超时")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def apply_inject_script(sid: str, tid: str, script: dict, mode: str) -> dict:
    if script.get("kind") == "cdp":
        result = cdp_command(sid, tid, script["method"], script.get("params") or {})
        return {"identifier": "", "result": result, "mode": "cdp"}
    source = script.get("code", "")
    if not source.strip():
        raise RuntimeError("脚本内容为空")
    if mode == "new_document":
        result = cdp_command(
            sid, tid, "Page.addScriptToEvaluateOnNewDocument", {"source": source}
        )
        return {"identifier": result.get("identifier", ""), "result": result, "mode": mode}
    result = cdp_command(
        sid, tid, "Runtime.evaluate",
        {"expression": source, "awaitPromise": False, "returnByValue": False},
    )
    if "exceptionDetails" in result:
        raise RuntimeError("页面脚本执行异常: " + str(result["exceptionDetails"])[:200])
    return {"identifier": "", "result": result, "mode": mode}


def inject_watch_loop(sid: str, script_ids: list, stop_event: threading.Event):
    """browser 级守护：新标签出现时自动 attach 并注入脚本。

    注意 JS 脚本注册的是 addScriptToEvaluateOnNewDocument，只对后续导航生效；
    守护开启时已加载的页面需要手动刷新或单独执行「立即注入」。CDP 命令类脚本
    （如 Emulation.*）在 attach 后立即下发，无此限制。
    """
    import websocket as ws_client

    while not stop_event.is_set():
        ws = None
        try:
            url = cdp_browser_ws_url(sid)
            ws = ws_client.create_connection(url, timeout=2, suppress_origin=True)
            call_id = 0
            pending = {}

            def send(method, params=None, session_id=None, callback=None):
                nonlocal call_id
                call_id += 1
                msg = {"id": call_id, "method": method, "params": params or {}}
                if session_id:
                    msg["sessionId"] = session_id
                ws.send(json.dumps(msg))
                pending[call_id] = callback
                return call_id

            def on_attach(result):
                session = (result or {}).get("sessionId")
                if not session:
                    return
                for script_id in script_ids:
                    script = resolve_inject_script(script_id)
                    if not script:
                        continue
                    try:
                        if script.get("kind") == "cdp":
                            send(script["method"], script.get("params") or {}, session_id=session)
                        else:
                            send("Page.addScriptToEvaluateOnNewDocument",
                                 {"source": script.get("code", "")}, session_id=session)
                    except Exception as e:
                        log.warning("守护注入 %s 失败: %s", script_id, e)

            def attach(target_id):
                send("Target.attachToTarget",
                     {"targetId": target_id, "flatten": True}, callback=on_attach)

            def on_targets(result):
                for target in (result or {}).get("targetInfos", []):
                    if target.get("type") == "page":
                        attach(target["targetId"])

            send("Target.setDiscoverTargets", {"discover": True})
            send("Target.getTargets", callback=on_targets)

            while not stop_event.is_set():
                try:
                    raw = ws.recv()
                except ws_client.WebSocketTimeoutException:
                    continue
                except Exception:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("id") in pending:
                    cb = pending.pop(msg["id"])
                    if cb:
                        try:
                            cb(msg.get("result", {}))
                        except Exception as e:
                            log.warning("注入守护回调异常: %s", e)
                elif msg.get("method") == "Target.targetCreated":
                    target = msg.get("params", {}).get("targetInfo", {})
                    if target.get("type") == "page":
                        attach(target["targetId"])
        except Exception as e:
            log.debug("注入守护 %s 暂时失败: %s", sid[:8], e)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        if not stop_event.is_set():
            stop_event.wait(2)


def start_inject_watcher(sid: str, script_ids: list) -> dict:
    stop_inject_watcher(sid)
    stop = threading.Event()
    thread = threading.Thread(
        target=inject_watch_loop, args=(sid, list(script_ids), stop),
        name=f"inject-watch-{sid[:8]}", daemon=True,
    )
    WATCHERS[sid] = {"stop": stop, "thread": thread, "scripts": list(script_ids)}
    thread.start()
    return {"sid": sid, "scripts": list(script_ids), "running": True}


def stop_inject_watcher(sid: str):
    old = WATCHERS.pop(sid, None)
    if old:
        old["stop"].set()
        try:
            old["thread"].join(timeout=3)
        except Exception:
            pass


@app.get("/api/scripts")
async def api_inject_scripts_list(request: Request):
    # 面板每次打开/切换主机都会拉脚本库，这里不写审计，避免无意义地膨胀审计日志。
    return {"items": all_inject_scripts()}


@app.post("/api/scripts")
async def api_inject_scripts_save(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON")
    script_id = str(payload.get("id", "") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", script_id):
        raise HTTPException(status_code=400, detail="脚本 ID 只能包含字母数字、下划线、点和连字符")
    if script_id in BUILTIN_MAP:
        raise HTTPException(status_code=400, detail="内置脚本不可覆盖")
    name = str(payload.get("name", "") or "").strip()
    code = str(payload.get("code", "") or "")
    kind = str(payload.get("kind", "js") or "js")
    if kind not in ("js", "cdp"):
        raise HTTPException(status_code=400, detail="脚本类型只能是 js 或 cdp")
    if not name:
        raise HTTPException(status_code=400, detail="缺少脚本名称")
    if kind == "js" and not code.strip():
        raise HTTPException(status_code=400, detail="脚本内容为空")
    item = {
        "id": script_id, "name": name,
        "category": str(payload.get("category", "自定义") or "自定义"),
        "kind": kind,
        "description": str(payload.get("description", "") or ""),
    }
    if kind == "js":
        item["code"] = code
    else:
        method = str(payload.get("method", "") or "").strip()
        if not method or "." not in method:
            raise HTTPException(status_code=400, detail="CDP 命令格式不合法")
        item["method"] = method
        item["params"] = payload.get("params") or {}
    data = load_inject_scripts()
    data[script_id] = item
    if not store_save("inject_scripts", data):
        raise HTTPException(status_code=500, detail="脚本保存失败（检查 DATA_DIR 挂载）")
    audit("scripts_save", detail=script_id)
    return {"ok": True, "script": item}


@app.delete("/api/scripts/{script_id}")
async def api_inject_scripts_delete(script_id: str):
    if script_id in BUILTIN_MAP:
        raise HTTPException(status_code=400, detail="内置脚本不可删除")
    data = load_inject_scripts()
    if script_id not in data:
        raise HTTPException(status_code=404, detail="脚本不存在")
    del data[script_id]
    store_save("inject_scripts", data)
    audit("scripts_delete", detail=script_id)
    return {"ok": True}


@app.post("/api/instance/{sid}/inject")
async def api_inject(sid: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON")
    script_id = str(payload.get("script_id", "") or "").strip()
    code = str(payload.get("code", "") or "")
    script = resolve_inject_script(script_id) if script_id else None
    if script:
        # 编辑框内容优先：允许改完直接注入，但不写回脚本库。
        if script.get("kind") == "cdp":
            if "params" in payload:
                script = {**script, "params": payload["params"]}
        elif code.strip():
            script = {**script, "code": code}
    elif code.strip():
        script = {"id": "custom-inline", "name": "临时脚本", "kind": "js", "code": code}
    elif payload.get("method") and payload.get("kind") == "cdp":
        # 远程主机场景下脚本库可能不在目标 manager 上，前端会随请求带上完整定义。
        script = {
            "id": script_id or "custom-inline",
            "name": str(payload.get("name") or "临时 CDP 命令"),
            "kind": "cdp",
            "method": str(payload["method"]),
            "params": payload.get("params") or {},
        }
    if not script:
        raise HTTPException(status_code=400, detail="未找到脚本，且未提供脚本内容")

    mode = str(payload.get("mode", "new_document") or "new_document")
    if mode not in ("new_document", "immediate"):
        raise HTTPException(status_code=400, detail="注入模式不合法")

    target_id = str(payload.get("target_id", "") or "").strip()
    all_targets = bool(payload.get("all_targets", False)) or not target_id
    if all_targets:
        try:
            targets = await run_in_threadpool(cdp_http, sid, "/json/list")
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        tids = [t.get("id", "") for t in (targets if isinstance(targets, list) else [])
                if t.get("type") == "page" and valid_target_id(t.get("id", ""))]
        if not tids:
            raise HTTPException(status_code=404, detail="实例内没有可注入的页面标签")
    else:
        if not valid_target_id(target_id):
            raise HTTPException(status_code=400, detail="非法的标签 ID")
        tids = [target_id]

    results = []
    for tid in tids:
        try:
            info = await run_in_threadpool(apply_inject_script, sid, tid, script, mode)
            rec = {
                "id": uuid.uuid4().hex[:12], "tid": tid,
                "script_id": script.get("id", ""), "name": script.get("name", ""),
                "mode": info["mode"], "identifier": info.get("identifier", ""),
                "ts": time.time(),
            }
            with LOCK:
                INJECTIONS.setdefault(sid, []).append(rec)
            results.append({"tid": tid, "ok": True, "injection_id": rec["id"]})
        except Exception as e:
            results.append({"tid": tid, "ok": False, "error": str(e)[:200]})
    audit("scripts_inject", sid=sid,
          detail=f"{script.get('id')} mode={mode} ok={sum(1 for r in results if r['ok'])}/{len(results)}")
    return {"ok": all(r["ok"] for r in results), "results": results}


@app.get("/api/instance/{sid}/injections")
async def api_injections_list(sid: str):
    with LOCK:
        return {"items": list(INJECTIONS.get(sid, []))}


@app.delete("/api/instance/{sid}/injections/{injection_id}")
async def api_injections_delete(sid: str, injection_id: str):
    with LOCK:
        rec = next((r for r in INJECTIONS.get(sid, []) if r["id"] == injection_id), None)
    if not rec:
        raise HTTPException(status_code=404, detail="注入记录不存在")
    if rec.get("identifier") and rec.get("tid"):
        try:
            await run_in_threadpool(
                cdp_command, sid, rec["tid"],
                "Page.removeScriptToEvaluateOnNewDocument",
                {"identifier": rec["identifier"]},
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail="撤销注入失败: " + str(e)[:200])
    with LOCK:
        INJECTIONS[sid] = [r for r in INJECTIONS.get(sid, []) if r["id"] != injection_id]
    audit("scripts_inject_undo", sid=sid, detail=injection_id)
    return {"ok": True}


@app.post("/api/instance/{sid}/watch")
async def api_inject_watch_start(sid: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON")
    script_ids = payload.get("scripts") or []
    if not isinstance(script_ids, list) or not script_ids:
        raise HTTPException(status_code=400, detail="scripts 必须是非空数组")
    for script_id in script_ids:
        if not resolve_inject_script(str(script_id)):
            raise HTTPException(status_code=404, detail="脚本不存在: " + str(script_id))
    result = await run_in_threadpool(start_inject_watcher, sid, [str(x) for x in script_ids])
    audit("scripts_watch_start", sid=sid, detail=",".join(result["scripts"]))
    return result


@app.get("/api/instance/{sid}/watch")
async def api_inject_watch_status(sid: str):
    watcher = WATCHERS.get(sid)
    return {"running": bool(watcher), "scripts": watcher["scripts"] if watcher else []}


@app.delete("/api/instance/{sid}/watch")
async def api_inject_watch_stop(sid: str):
    await run_in_threadpool(stop_inject_watcher, sid)
    audit("scripts_watch_stop", sid=sid)
    return {"ok": True}
