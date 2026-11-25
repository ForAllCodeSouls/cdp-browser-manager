import os
import threading
import time
import uuid
from typing import Dict

import docker
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

app = FastAPI(title="CDP Browser Manager")
templates = Jinja2Templates(directory="templates")

client = docker.from_env()
BROWSER_IMAGE = os.environ.get(
    "BROWSER_IMAGE", "ghcr.io/forallcodesouls/cdp-browser-browser:latest"
)
CONTAINER_LABEL = "cdp.browser.manager"
MANAGER_ID = os.environ.get("MANAGER_ID", "default")

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


def public_base(request: Request) -> str:
    if PUBLIC_HOST:
        return PUBLIC_HOST
    return f"{request.url.scheme}://{request.headers.get('host', request.client.host)}"


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


def sync_instances():
    for sid, item in list(INSTANCES.items()):
        try:
            container = client.containers.get(item["container_id"])
            item["status"] = container.status
            item.update(parse_ports(container))
        except docker.errors.NotFound:
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
        ports = parse_ports(c)
        if any(v is None for v in ports.values()):
            continue
        INSTANCES[sid] = {
            "sid": sid,
            "container_id": c.id,
            "container_name": c.name,
            **ports,
            "status": c.status,
        }


def to_view(item: dict, request: Request) -> dict:
    data = dict(item)
    base = public_base(request)
    host_only = base.split("://", 1)[-1]
    host = host_only.split(":")[0]
    debug = item.get("debug_port")
    vnc = item.get("vnc_port")
    novnc = item.get("novnc_port")
    data["cdp_addr"] = f"{host}:{debug}" if debug else ""
    data["cdp_http"] = f"http://{host}:{debug}" if debug else ""
    data["vnc_url"] = f"http://{host}:{vnc}" if vnc else ""
    data["novnc_url"] = f"http://{host}:{novnc}" if novnc else ""
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

    ip = container_ip(container)
    if not ip:
        return False

    deadline = time.time() + timeout
    url = f"http://{ip}:9222/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def start_browser_instance(sid: str) -> dict:
    # 端口分配必须在锁内完成，避免并发创建分配到同一端口
    with LOCK:
        used = {"debug": set(), "vnc": set(), "novnc": set()}
        # parse_ports 返回的是 debug_port/vnc_port/novnc_port，需做键名映射
        key_map = {
            "debug": "debug_port",
            "vnc": "vnc_port",
            "novnc": "novnc_port",
        }
        for c in client.containers.list(
            all=True, filters={"label": f"{CONTAINER_LABEL}={MANAGER_ID}"}
        ):
            ports = parse_ports(c)
            for short_key, long_key in key_map.items():
                value = ports.get(long_key)
                if value is not None:
                    used[short_key].add(value)

        debug_port = find_free(PORT_DEBUG_START, PORT_DEBUG_END, used["debug"])
        vnc_port = find_free(PORT_VNC_START, PORT_VNC_END, used["vnc"])
        novnc_port = find_free(PORT_NOVNC_START, PORT_NOVNC_END, used["novnc"])

        container = client.containers.run(
            BROWSER_IMAGE,
            detach=True,
            name=f"cdpb-{sid[:8]}",
            labels={
                CONTAINER_LABEL: MANAGER_ID,
                "cdp.sid": sid,
            },
            ports={
                "9222/tcp": debug_port,
                "5900/tcp": vnc_port,
                "6080/tcp": novnc_port,
            },
            shm_size="2gb",
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
        }
        INSTANCES[sid] = info

    # 锁外等待 CDP 就绪，避免阻塞其他请求查询；超时也标记状态而非无限阻塞
    info["cdp_ready"] = wait_cdp_ready(container)
    return info


def stop_instance(sid: str) -> bool:
    with LOCK:
        item = INSTANCES.pop(sid, None)
    if not item:
        return False
    try:
        c = client.containers.get(item["container_id"])
        c.remove(force=True)
    except (docker.errors.NotFound, docker.errors.APIError):
        pass
    return True


restore_instances()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/list")
async def api_list(request: Request):
    await run_in_threadpool(sync_instances)
    with LOCK:
        snapshot = list(INSTANCES.items())
    return {
        "instances": {
            sid: to_view(item, request) for sid, item in snapshot
        }
    }


@app.post("/instance/new")
async def new_instance(request: Request):
    try:
        sid = str(uuid.uuid4())
        data = await run_in_threadpool(start_browser_instance, sid)
        return JSONResponse(to_view(data, request))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
