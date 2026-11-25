import os
import uuid
from typing import Dict

import docker
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="CDP Browser Manager")
templates = Jinja2Templates(directory="templates")

client = docker.from_env()
BROWSER_IMAGE = os.environ.get(
    "BROWSER_IMAGE", "ghcr.io/forallcodesouls/cdp-browser-browser:latest"
)
CONTAINER_LABEL = "cdp.browser.manager"
MANAGER_ID = os.environ.get("MANAGER_ID", "default")

INSTANCES: Dict[str, dict] = {}

PORT_DEBUG_START = 9222
PORT_DEBUG_END = 9322
PORT_VNC_START = 5901
PORT_VNC_END = 6000
PORT_NOVNC_START = 7901
PORT_NOVNC_END = 7999


def find_free(start: int, end: int, used: set) -> int:
    for port in range(start, end + 1):
        if port not in used:
            used.add(port)
            return port
    raise RuntimeError(f"端口池耗尽: {start}-{end}")


def collect_used_ports() -> dict:
    used = {"debug": set(), "vnc": set(), "novnc": set()}
    for c in client.containers.list(
        all=True, filters={"label": f"{CONTAINER_LABEL}={MANAGER_ID}"}
    ):
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        for internal, mappings in ports.items():
            if not mappings:
                continue
            host_port = int(mappings[0]["HostPort"])
            if internal == "9222/tcp":
                used["debug"].add(host_port)
            elif internal == "5900/tcp":
                used["vnc"].add(host_port)
            elif internal == "6080/tcp":
                used["novnc"].add(host_port)
    return used


def restore_instances():
    for c in client.containers.list(
        all=True, filters={"label": f"{CONTAINER_LABEL}={MANAGER_ID}"}
    ):
        labels = c.labels
        sid = labels.get("cdp.sid")
        if not sid:
            continue
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        try:
            debug_port = int(ports["9222/tcp"][0]["HostPort"])
            vnc_port = int(ports["5900/tcp"][0]["HostPort"])
            novnc_port = int(ports["6080/tcp"][0]["HostPort"])
        except (KeyError, IndexError, TypeError):
            continue
        INSTANCES[sid] = {
            "sid": sid,
            "container_id": c.id,
            "container_name": c.name,
            "debug_port": debug_port,
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
            "cdp_addr": f"127.0.0.1:{debug_port}",
            "status": c.status,
        }


def start_browser_instance(sid: str) -> dict:
    used = collect_used_ports()
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
        "cdp_addr": f"127.0.0.1:{debug_port}",
        "status": container.status,
    }
    INSTANCES[sid] = info
    return info


def stop_instance(sid: str):
    item = INSTANCES.pop(sid, None)
    if not item:
        return
    try:
        c = client.containers.get(item["container_id"])
        c.remove(force=True)
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError:
        pass


restore_instances()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "instances": INSTANCES,
    })


@app.post("/instance/new")
async def new_instance():
    sid = str(uuid.uuid4())
    data = start_browser_instance(sid)
    return JSONResponse(data)


@app.post("/instance/{sid}/stop")
async def stop_one(sid: str):
    stop_instance(sid)
    return {"ok": True}


@app.post("/instance/stop-all")
async def stop_all():
    for sid in list(INSTANCES.keys()):
        stop_instance(sid)
    return {"ok": True}


@app.get("/api/list")
async def api_list():
    return {"instances": INSTANCES}
