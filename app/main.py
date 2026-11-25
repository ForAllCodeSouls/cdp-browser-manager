import uuid
import subprocess
import time
from typing import Dict

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="CDP Browser Manager")
templates = Jinja2Templates(directory="templates")

INSTANCES: Dict[str, dict] = {}

def find_free(start: int, used: set, limit: int = 100):
    current = start
    for _ in range(limit):
        if current not in used:
            used.add(current)
            return current
        current += 1
    raise RuntimeError("端口池耗尽，请扩大端口范围")

def start_browser_instance(sid: str):
    used_debug = {i["debug_port"] for i in INSTANCES.values()}
    used_vnc = {i["vnc_port"] for i in INSTANCES.values()}
    used_novnc = {i["novnc_port"] for i in INSTANCES.values()}
    used_display = {i["display_num"] for i in INSTANCES.values()}

    debug_port = find_free(9222, used_debug)
    vnc_port = find_free(5901, used_vnc)
    novnc_port = find_free(7901, used_novnc)
    disp_num = find_free(1, used_display)
    display = f":{disp_num}"

    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1920x1080x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    xvnc = subprocess.Popen(
        [
            "x11vnc",
            "-display", display,
            "-rfbport", str(vnc_port),
            "-forever", "-shared", "-nopw", "-quiet"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    websockify_proc = subprocess.Popen(
        [
            "websockify",
            f":{novnc_port}",
            f"127.0.0.1:{vnc_port}",
            "--web=/usr/share/novnc"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    chrome = subprocess.Popen(
        [
            "chromium-browser",
            f"--remote-debugging-port={debug_port}",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-infobars",
            "--window-size=1920,1080",
            "--start-maximized",
        ],
        env={"DISPLAY": display},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    info = {
        "sid": sid,
        "display": display,
        "display_num": disp_num,
        "debug_port": debug_port,
        "vnc_port": vnc_port,
        "novnc_port": novnc_port,
        "xvfb_pid": xvfb.pid,
        "xvnc_pid": xvnc.pid,
        "ws_pid": websockify_proc.pid,
        "chrome_pid": chrome.pid,
        "cdp_addr": f"127.0.0.1:{debug_port}",
    }
    INSTANCES[sid] = info
    return info

def kill_pid_safe(pid):
    try:
        subprocess.run(["kill", str(pid)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def stop_instance(sid: str):
    if sid not in INSTANCES:
        return
    item = INSTANCES.pop(sid)
    kill_pid_safe(item["chrome_pid"])
    kill_pid_safe(item["ws_pid"])
    kill_pid_safe(item["xvnc_pid"])
    kill_pid_safe(item["xvfb_pid"])

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "instances": INSTANCES
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
