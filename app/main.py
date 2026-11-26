import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict
from urllib.parse import quote, urlsplit

import docker
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("cdp-manager")

app = FastAPI(title="CDP Browser Manager")
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


def image_for_mode(browser_mode: str) -> str:
    """按内核选择对应的浏览器镜像。"""
    return BROWSER_IMAGE_HEADLESS if browser_mode == "headless" else BROWSER_IMAGE_FULL


def ensure_image_available(image: str) -> None:
    """创建前确认镜像已在本地，避免 containers.run 触发漫长 pull 并卡住全局锁。

    镜像缺失时快速失败并给出可执行的提示，而不是让整个面板的接口一起挂起。
    """
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        raise RuntimeError(
            f"镜像 {image} 尚未拉取到本地。请先在服务器执行 "
            f"docker compose pull 或 bash upgrade.sh，再创建实例。"
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


def restore_instances():
    for c in client.containers.list(
        all=True, filters={"label": f"{CONTAINER_LABEL}={MANAGER_ID}"}
    ):
        sid = c.labels.get("cdp.sid")
        if not sid:
            continue
        mode = c.labels.get(BROWSER_MODE_LABEL, "full")
        if mode not in ("full", "headless"):
            mode = "full"
        ports = parse_ports(c)
        # headless 内核没有 VNC/noVNC 端口，只要求 CDP 端口存在
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
            "browser_mode": mode,
            "profile_mode": c.labels.get("cdp.profile.mode", "ephemeral"),
            "profile_name": c.labels.get("cdp.profile.name", ""),
        }


def to_view(item: dict, request: Request) -> dict:
    data = dict(item)
    base = public_base(request)
    host = host_for_url(host_from_base(base))
    debug = item.get("debug_port")
    vnc = item.get("vnc_port")
    novnc = item.get("novnc_port")
    data["cdp_addr"] = f"{host}:{debug}" if debug else ""
    data["cdp_http"] = f"http://{host}:{debug}" if debug else ""
    data["vnc_url"] = f"http://{host}:{vnc}" if vnc else ""
    # novnc 包不提供根目录 index.html。这里返回基础地址，
    # 具体页面（vnc_lite.html / vnc.html）与参数由前端设置决定。
    data["novnc_url"] = f"http://{host}:{novnc}" if novnc else ""
    # screencast 前端模板：把 {tid} 换成标签页 id 即可。
    # inspector.html 由浏览器自带的 CDP HTTP 服务提供，页面与 WS 同源，避免 CORS。
    # 抓屏走 manager 的 WS 代理（见 /ws/cast），不依赖浏览器自带 devtools 前端。
    data["cast_ws"] = (
        f"/ws/cast/{item.get('sid', '')}/{{tid}}"
    )
    return data


def container_ip(container) -> str:
    container.reload()
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    for net in networks.values():
        if net.get("IPAddress"):
            return net["IPAddress"]
    return ""


def wait_cdp_ready(container, timeout: float = 12.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    url = ""
    while time.time() < deadline:
        # 容器若已退出或在反复重启，立即判定失败，不再空等整个超时。
        try:
            container.reload()
            state = container.attrs.get("State", {})
            if state.get("Status") in ("exited", "dead") or state.get("Restarting"):
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


def start_browser_instance(
    sid: str,
    profile_mode: str = "ephemeral",
    profile_name: str = "",
    browser_mode: str = "full",
) -> dict:
    persistent = profile_mode == "persistent"
    # 先确认镜像在本地：否则 containers.run 会触发 pull，长时间占着全局锁，
    # 导致面板其它接口（/api/list 等）全部超时。镜像缺失时立即给出提示。
    ensure_image_available(image_for_mode(browser_mode))
    # 端口分配必须在锁内完成，避免并发创建分配到同一端口
    with LOCK:
        used = collect_used_ports()

        debug_port = find_free(PORT_DEBUG_START, PORT_DEBUG_END, used["debug"])
        if browser_mode == "headless":
            # 无头内核没有 Xvfb/VNC，不占用也不映射这两个端口
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
        }
        environment = {"BROWSER_MODE": browser_mode}
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

        container = client.containers.run(
            image,
            detach=True,
            name=f"cdpb-{sid[:8]}",
            labels=labels,
            ports=published,
            environment=environment,
            volumes=volumes,
            shm_size=SHM_SIZE,
            restart_policy={"Name": "unless-stopped"},
        )

        info = {
            "sid": sid,
            "container_id": container.id,
            "container_name": container.name,
            "debug_port": debug_port,
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
            "status": container.status,
            "browser_mode": browser_mode,
            "profile_mode": profile_mode,
            "profile_name": profile_name if persistent else "",
        }
        INSTANCES[sid] = info

    log.info(
        "创建实例 %s：内核=%s 镜像=%s CDP=%s VNC=%s",
        sid[:8], browser_mode, image, debug_port, vnc_port,
    )
    # 锁外等待 CDP 就绪，避免阻塞其他请求查询；超时也标记状态而非无限阻塞
    info["cdp_ready"] = wait_cdp_ready(container)
    return info


def stop_instance(sid: str) -> bool:
    with LOCK:
        item = INSTANCES.get(sid)
    if not item:
        return False
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


def container_ip_by_id(container_id: str) -> str:
    if not container_id:
        return ""
    try:
        c = client.containers.get(container_id)
        return container_ip(c)
    except Exception:
        return ""


def container_logs(container_id: str, tail: int = 120) -> str:
    """读取 browser 容器的 stdout/stderr，用于排查启动崩溃。"""
    c = client.containers.get(container_id)
    raw = c.logs(tail=tail, stdout=True, stderr=True)
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


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


def cdp_monitor_loop():
    """后台持续探测 running 实例的 CDP 就绪状态，覆盖 restore 恢复的实例。"""
    import urllib.request

    while True:
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
                    INSTANCES[sid]["cdp_ready"] = ready
        time.sleep(8)


threading.Thread(target=cdp_monitor_loop, daemon=True).start()


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
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/list")
async def api_list(request: Request):
    await run_in_threadpool(sync_instances)
    with LOCK:
        snapshot = list(INSTANCES.items())
    profiles = await run_in_threadpool(list_profiles)
    return {
        "instances": {
            sid: to_view(item, request) for sid, item in snapshot
        },
        "profiles": profiles,
    }


@app.get("/api/health")
async def api_health():
    """存活探针：面板在线且 Docker 守护进程可达。"""
    try:
        await run_in_threadpool(client.ping)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker 不可用: {e}")


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
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            mode = payload.get("profile_mode", mode)
            name = str(payload.get("profile_name", "") or "")
            browser_mode = payload.get("browser_mode", browser_mode)
    except Exception:
        pass
    if mode not in ("ephemeral", "persistent"):
        raise HTTPException(status_code=400, detail="profile_mode 只能是 ephemeral 或 persistent")
    if browser_mode not in ("full", "headless"):
        raise HTTPException(status_code=400, detail="browser_mode 只能是 full 或 headless")
    if mode == "persistent" and not PROFILE_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="目录名只能包含字母、数字、下划线、点和短横线，长度 1-48",
        )
    try:
        sid = str(uuid.uuid4())
        data = await run_in_threadpool(
            start_browser_instance, sid, mode, name, browser_mode
        )
        return JSONResponse(to_view(data, request))
    except Exception as e:
        log.warning("创建实例失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/instance/{sid}/targets")
async def api_targets(sid: str):
    """列出实例里的页面标签，供抓屏视图选择要显示的目标。"""
    try:
        raw = await run_in_threadpool(cdp_http, sid, "/json/list")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    targets = [
        {
            "id": t.get("id", ""),
            "title": t.get("title", ""),
            "url": t.get("url", ""),
            "type": t.get("type", ""),
        }
        for t in (raw if isinstance(raw, list) else [])
        if t.get("type") == "page" and t.get("id")
    ]
    return {"targets": targets}


@app.post("/api/instance/{sid}/targets")
async def api_new_target(sid: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    url = str(payload.get("url", "about:blank") or "about:blank")
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
    try:
        await run_in_threadpool(cdp_http, sid, "/json/close/" + quote(tid, safe=""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


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
    allowed = {"Page.navigate", "Page.reload", "Page.bringToFront", "Runtime.evaluate"}
    if method not in allowed:
        raise HTTPException(status_code=400, detail="不支持的 CDP 命令: " + method)
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

    if not valid_target_id(tid):
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "非法的标签 ID"})
        await websocket.close()
        return

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

        # 帧接收线程：把所有 CDP 消息转发给前端，screencastFrame 自动 Ack
        def pump():
            call_id = 100
            upstream_send({"id": call_id, "method": "Page.enable", "params": {}})
            upstream_send({
                "id": call_id + 1,
                "method": "Page.startScreencast",
                "params": {
                    "format": "jpeg",
                    "quality": 80,
                    "everyNthFrame": 1,
                    "maxWidth": CAST_MAX_WIDTH,
                    "maxHeight": CAST_MAX_HEIGHT,
                },
            })
            while not stop.is_set():
                try:
                    raw = upstream.recv()
                except Exception:
                    break
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                method = msg.get("method", "")
                if method == "Page.screencastFrame":
                    params = msg.get("params", {})
                    try:
                        upstream_send({
                            "id": call_id + 2,
                            "method": "Page.screencastFrameAck",
                            "params": {"sessionId": params.get("sessionId", "")},
                        })
                    except Exception:
                        pass
                    try:
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_json({
                                "type": "frame",
                                "data": params.get("data", ""),
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


@app.post("/instance/{sid}/stop")
async def stop_one(sid: str):
    await run_in_threadpool(stop_instance, sid)
    return {"ok": True}


@app.post("/instance/stop-all")
async def stop_all():
    with LOCK:
        sids = list(INSTANCES.keys())
    for sid in sids:
        await run_in_threadpool(stop_instance, sid)
    return {"ok": True}


@app.delete("/profile/{name}")
async def delete_profile(name: str):
    try:
        ok = await run_in_threadpool(remove_profile, name)
        return {"ok": ok}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
