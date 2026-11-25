import os
import re
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
PROFILE_LABEL = "cdp.profile"
PROFILE_OWNER_LABEL = "cdp.profile.owner"
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
            item["status"] = container.status
            item.update(parse_ports(container))
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
        ports = parse_ports(c)
        if any(v is None for v in ports.values()):
            continue
        INSTANCES[sid] = {
            "sid": sid,
            "container_id": c.id,
            "container_name": c.name,
            **ports,
            "status": c.status,
            "profile_mode": c.labels.get("cdp.profile.mode", "ephemeral"),
            "profile_name": c.labels.get("cdp.profile.name", ""),
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
    # novnc 包不提供根目录 index.html。这里返回基础地址，
    # 具体页面（vnc_lite.html / vnc.html）与参数由前端设置决定。
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


def start_browser_instance(sid: str, profile_mode: str = "ephemeral", profile_name: str = "") -> dict:
    persistent = profile_mode == "persistent"
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

        labels = {
            CONTAINER_LABEL: MANAGER_ID,
            "cdp.sid": sid,
            "cdp.profile.mode": profile_mode,
            "cdp.profile.name": profile_name if persistent else "",
        }
        environment = {}
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

        container = client.containers.run(
            BROWSER_IMAGE,
            detach=True,
            name=f"cdpb-{sid[:8]}",
            labels=labels,
            ports={
                "9222/tcp": debug_port,
                "5900/tcp": vnc_port,
                "6080/tcp": novnc_port,
            },
            environment=environment,
            volumes=volumes,
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
            "profile_mode": profile_mode,
            "profile_name": profile_name if persistent else "",
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
    # 持久模式的 volume 不随容器删除，数据保留
    return True


restore_instances()


def container_ip_by_id(container_id: str) -> str:
    if not container_id:
        return ""
    try:
        c = client.containers.get(container_id)
        return container_ip(c)
    except Exception:
        return ""


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


@app.get("/api/profiles")
async def api_profiles():
    return {"profiles": await run_in_threadpool(list_profiles)}


@app.post("/instance/new")
async def new_instance(request: Request):
    mode = "ephemeral"
    name = ""
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            mode = payload.get("profile_mode", mode)
            name = str(payload.get("profile_name", "") or "")
    except Exception:
        pass
    if mode not in ("ephemeral", "persistent"):
        raise HTTPException(status_code=400, detail="profile_mode 只能是 ephemeral 或 persistent")
    try:
        sid = str(uuid.uuid4())
        data = await run_in_threadpool(start_browser_instance, sid, mode, name)
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


@app.delete("/profile/{name}")
async def delete_profile(name: str):
    try:
        ok = await run_in_threadpool(remove_profile, name)
        return {"ok": ok}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
