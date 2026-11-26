import asyncio
import io
import json
import logging
import os
import re
import socket
import tarfile
import threading
import time
import uuid
from collections import deque
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


# 是否由 manager 自行拉取浏览器镜像。默认开启：Dockge / 部分 compose 面板更新时会跳过
# 带 profile 的占位服务，导致两份 browser 镜像长期停留在旧版本，容器启动即崩溃。
AUTO_PULL_IMAGES = os.environ.get("AUTO_PULL_IMAGES", "1").strip().lower() not in (
    "0", "false", "no", "off"
)
PULL_LOCK = threading.Lock()

# 镜像更新检测缓存：避免面板轮询时反复打 GHCR API。
IMAGE_STATUS_CACHE = {"ts": 0.0, "data": {}}
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

    带缓存，失败自动降级不影响面板。远端摘要做二次确认：仅当连续两次一致、
    且与本地不同时才认定「有更新」。避免 GHCR/CDN 抖动或 CI 正在推送时的一次性
    差异触发误报（表现为红点亮起、点开设置却又显示「已是最新」）。
    """
    now = time.time()
    with IMAGE_STATUS_LOCK:
        cached = IMAGE_STATUS_CACHE["data"]
        if not force and cached and now - IMAGE_STATUS_CACHE["ts"] < IMAGE_STATUS_TTL:
            return cached

    result = {}
    for label, image in (("full", BROWSER_IMAGE_FULL), ("headless", BROWSER_IMAGE_HEADLESS)):
        local = local_image_digest(image)
        remote = ""
        err = ""
        stable = True
        try:
            remote = remote_image_digest(image)
            if local and remote and local != remote:
                # 首次发现差异，短暂间隔后再查一次确认；两次不一致视为远端抖动。
                time.sleep(0.6)
                confirm = remote_image_digest(image)
                if confirm != remote:
                    stable = False
                    log.debug(
                        "%s 远端摘要两次不一致(%s/%s)，本次不报更新",
                        label, remote[:19], confirm[:19],
                    )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log.debug("查询 %s 远端镜像摘要失败: %s", label, err)
        result[label] = {
            "image": image,
            "local": local[:19] if local else "",
            "remote": remote[:19] if remote else "",
            "local_missing": not local,
            "remote_unknown": not remote,
            "unstable": not stable,
            "update_available": bool(stable and remote and local and remote != local),
            "error": err if not remote else "",
        }
    with IMAGE_STATUS_LOCK:
        IMAGE_STATUS_CACHE["ts"] = now
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
            "browser_mode": browser_mode,
            "profile_mode": profile_mode,
            "profile_name": profile_name if persistent else "",
        }
        INSTANCES[sid] = info

    log.info(
        "创建实例 %s：内核=%s 镜像=%s CDP=%s VNC=%s",
        sid[:8], browser_mode, image, debug_port, vnc_port,
    )
    # 不再同步空等 CDP 就绪（最长 20 秒会让前端「创建中」显得卡住）。
    # 容器已创建即立即返回，cdp_ready 由后台 cdp_monitor_loop 每 8 秒刷新，
    # 前端拿到 sid 后可立刻在列表显示实例，并异步等待画面连接。
    info["cdp_ready"] = False
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
        time.sleep(8)


def auto_pull_loop() -> None:
    """manager 启动后后台拉取最新浏览器镜像，再按较长间隔复查。

    这是为了兼容 Dockge 等只拉 manager、跳过 profile 占位服务的更新方式：
    即便用户只在面板点了「更新」，浏览器镜像也会在 manager 重启后自动补齐。
    """
    # 启动稍等片刻，让 web 服务先可用，拉取大镜像期间面板不卡死。
    time.sleep(5)
    while True:
        delay = 1800
        if AUTO_PULL_IMAGES:
            result = pull_browser_images()
            if any(v.get("stale") for v in result.values()):
                # CI 正在构建时 pull 到的仍是旧 manifest，2 分钟后重试，
                # 避免用户以为已更新却仍然 CDP 不通。
                delay = 120
                log.warning("浏览器镜像可能仍是旧版本，2 分钟后自动重试")
        # 每 30 分钟复查一次；检测到拉取到旧版本时缩短到 2 分钟。
        time.sleep(delay)


threading.Thread(target=cdp_monitor_loop, daemon=True).start()
threading.Thread(target=auto_pull_loop, daemon=True, name="browser-image-puller").start()


# ---------- 内置 CDP 自测：临时有头/无头实例上跑官方 CDP WebSocket 用例 ----------
SELFTEST_STATE = {"running": False, "started": 0.0, "result": {}, "error": ""}
SELFTEST_LOCK = threading.Lock()


def _selftest_one_mode(browser_mode: str) -> dict:
    """在一份临时实例上跑完整自测，无论成败都在结束时销毁实例。"""
    sid = str(uuid.uuid4())
    created = False
    try:
        log.info("[自测] 创建临时实例：内核=%s sid=%s", browser_mode, sid[:8])
        info = start_browser_instance(sid, "ephemeral", "", browser_mode)
        created = True
        container = client.containers.get(info["container_id"])
        # 等待 CDP 就绪（自测可以同步等；用户实例创建接口已经改为异步）。
        if not wait_cdp_ready(container, timeout=25.0):
            raise RuntimeError("临时实例 CDP 未就绪")
        ip = container_ip(container)
        if not ip:
            raise RuntimeError("临时实例容器网络未就绪")
        # 镜像里 main.py 被平铺到 /app 且以 uvicorn main:app 启动，模块是顶层 selftest；
        # 本地源码目录结构是 app/ 包。两种环境都兼容。
        try:
            from app.selftest import run_all
        except ImportError:
            from selftest import run_all

        def emit(level, name, detail):
            log.info("[自测][%s][%s] %s %s", browser_mode, level, name, detail)

        return run_all(f"http://{ip}:9222", emit)
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
    log.info("========== 开始内置 CDP 自测（临时有头 + 无头实例）==========")
    result = {"full": {}, "headless": {}}
    for mode in ("headless", "full"):
        result[mode] = _selftest_one_mode(mode)
    total_pass = sum(result[m].get("passed", 0) for m in result)
    total_fail = sum(result[m].get("failed", 0) for m in result)
    log.info(
        "========== 自测完成：通过 %d / 失败 %d ==========",
        total_pass, total_fail,
    )
    with SELFTEST_LOCK:
        SELFTEST_STATE["running"] = False
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
