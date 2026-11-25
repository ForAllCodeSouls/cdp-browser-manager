# cdp-browser-manager

多 Chromium 实例管理器，每个实例独立 CDP 远程调试端口，可选 **VNC** 或 **CDP 抓屏** 查看画面。
适合配合 pydoll / DrissionPage / Playwright 等基于 CDP WS 协议的自动化脚本使用。

## 架构

采用**双镜像分层**设计，manager（小）与 browser（大）独立构建和发布：

| 镜像 | 大小 | 内容 | 更新频率 |
|---|---|---|---|
| `ghcr.io/forallcodesouls/cdp-browser-manager:latest` | 约 40 MB | FastAPI 管理面板，通过 Docker API 调度 browser 容器 | 代码改动时 |
| `ghcr.io/forallcodesouls/cdp-browser-browser:latest` | 约 650 MB | **Chrome for Testing 双内核**（完整 chrome + chrome-headless-shell）+ Xvfb + x11vnc + noVNC + 中文字体 | 浏览器版本升级时 |

浏览器使用 **Chrome for Testing**：Google 官方为自动化测试发布的版本，无自动更新、版本可复现、不含推广内容。镜像内同时装有两套二进制，新建实例时可选：

| 内核 | 二进制 | 画面方式 | 说明 |
|---|---|---|---|
| 完整内核（full，默认） | `chrome` | VNC + CDP 抓屏 | 有地址栏/标签栏，可看完整浏览器界面 |
| 无头内核（headless） | `chrome-headless-shell` | 仅 CDP 抓屏 | 更省资源，无浏览器界面；不分配 VNC/noVNC 端口 |

两种内核的 CDP 端口都是容器内 9222，pydoll / DrissionPage 的连接方式完全一致。版本号在 `Dockerfile.browser` 顶部的 `CHROME_VERSION` 中锁定，升级时只改这一处，两个二进制一起更新。

启动参数已加入去 Google 化开关（`--disable-background-networking`、`--disable-component-update`、`--disable-sync`、`--metrics-recording-only` 等），关闭后台联网与组件更新；官方 for Testing 包本身不含第三方渠道的广告内容。

manager 启动后挂载宿主机 Docker socket，按需创建/销毁 browser 容器。完整内核容器内部固定使用 9222（CDP）、5900（VNC）、6080（noVNC），无头内核只使用 9222，再通过端口映射分配宿主机独立端口。日常只更新 manager 时，大镜像层命中缓存，几乎零下载。

## 功能
- 一键新建浏览器实例，可选完整/无头内核，自动分配独立 CDP 端口（完整内核另分配 VNC、noVNC）
- **画面双模式，随时切换**：
  - **VNC**：完整桌面画面，可见地址栏/标签栏（仅完整内核）
  - **CDP 抓屏**：基于 CDP `Page.startScreencast`，画面与自动化共用同一 CDP 端口；支持标签切换、新建/关闭标签、地址栏导航、鼠标键盘交互（两种内核通用，无头内核只能用它）
    - 实现上由 manager 的 `/ws/cast/{sid}/{tid}` 把容器内的 screencast 帧代理到浏览器 canvas，并转发输入事件。不直接用 Chrome 自带的 `devtools/inspector.html`——它的 CSP 只允许 `ws://127.0.0.1`，公网访客连不上。
- **用户目录双模式**：
  - **临时目录**：停止实例即清理，适合一次性任务
  - **持久目录**：以 Docker Volume 保存在宿主机，停止实例后数据仍在；新建时可选择「新建持久目录」或「复用已有目录」恢复登录态，需在面板左侧手动点删除才会清除
- Web 面板预览浏览器画面（左侧点击实例切换，画面自动连接）
- 复制 CDP 调试地址，pydoll / DrissionPage / Playwright 通过 CDP WS 连接控制
- 停止单个 / 全部浏览器实例
- manager 重启后自动恢复已有实例（通过容器标签识别）

### 用户目录说明

新建实例弹窗中可选三种模式：

| 模式 | 数据存储 | 停止实例后 | 适用场景 |
|---|---|---|---|
| 临时目录 | 容器内 `/tmp/cdp-profile` | 数据随容器删除 | 日常自动化，不需登录态 |
| 新建持久目录 | Docker Volume `cdp-profile-*` | 数据保留 | 需要保留 Cookie / 登录态 |
| 复用已有持久目录 | 挂载同名 Volume | 数据保留并复用 | 下次继续上次的登录状态 |

持久目录在面板左侧「持久用户目录」区列出。被运行中实例占用的目录显示「使用中」，不能删除；停止实例后可点「删除」永久清除。

API：

```bash
# 新建持久目录实例（完整内核）
curl -X POST http://你的IP:8000/instance/new \
  -H 'Content-Type: application/json' \
  -d '{"profile_mode":"persistent","profile_name":"shop-account","browser_mode":"full"}'

# 新建无头内核实例（不分配 VNC/noVNC 端口）
curl -X POST http://你的IP:8000/instance/new \
  -H 'Content-Type: application/json' \
  -d '{"browser_mode":"headless"}'

# 列出所有持久目录
curl http://你的IP:8000/api/profiles

# 删除持久目录（需先停止使用它的实例）
curl -X DELETE http://你的IP:8000/profile/shop-account
```

面板抓屏用到的 CDP 代理接口（标签 id 从 `/api/instance/{sid}/targets` 获取）：

```bash
# 列出标签页
curl http://你的IP:8000/api/instance/{sid}/targets

# 新建标签（面板用此接口代理，浏览器 JS 不直接访问 CDP 端口）
curl -X POST http://你的IP:8000/api/instance/{sid}/targets \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'

# 下发 CDP 命令
curl -X POST http://你的IP:8000/api/instance/{sid}/cmd \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"标签ID","method":"Page.navigate","params":{"url":"https://example.com"}}'
```

## 部署（推荐 Docker Compose）

项目根目录已附带 `docker-compose.yml`，内容如下：

```yaml
services:
  cdp-manager:
    image: ghcr.io/forallcodesouls/cdp-browser-manager:latest
    container_name: cdp-manager
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      BROWSER_IMAGE: ghcr.io/forallcodesouls/cdp-browser-browser:latest

  # 占位服务：让 docker compose pull 能同时更新 browser 大镜像
  cdp-browser-image:
    image: ghcr.io/forallcodesouls/cdp-browser-browser:latest
    profiles: ["no-auto-start"]
```

> `cdp-browser-image` 带 profile，不会被 `docker compose up -d` 启动，只用于让 `docker compose pull` 把 browser 镜像一并更新。**升级时务必先 pull**，否则 manager 会用本地缓存的旧 browser 镜像创建容器，导致 CDP 无法连接。

在 `docker-compose.yml` 所在目录执行。**推荐直接用一键升级脚本**（含拉取镜像、删旧容器、重建、校验）：

```bash
bash upgrade.sh
```

等价的手动命令：

```bash
docker compose pull             # 拉取两个镜像（含 browser 大镜像）
docker ps -aq --filter label=cdp.browser.manager | xargs -r docker rm -f   # 升级时清理旧浏览器容器
docker compose up -d            # 后台启动 manager
docker compose ps              # 查看 manager 状态
docker compose logs -f         # 跟踪日志
docker compose down            # 停止并移除 manager（browser 实例会保留）
```

> **为什么升级必须删旧容器**：browser 容器一旦创建，就固定使用创建时的镜像和 `start.sh`。只更新镜像、不删容器，旧实例仍跑旧代码（典型症状：画面能开但 CDP 连不上、切换实例画面不变）。`upgrade.sh` 已包含这一步。

访问面板：

```
http://你的IP:8000
```

> 停止 manager 后 browser 容器仍会保留；需要彻底清理时执行 `docker ps -a --filter label=cdp.browser.manager` 查看，再逐个 `docker rm -f`。

若不用 Compose，等价的 `docker run` 命令：

```bash
docker run -d \
  --name cdp-manager \
  --restart unless-stopped \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e BROWSER_IMAGE=ghcr.io/forallcodesouls/cdp-browser-browser:latest \
  ghcr.io/forallcodesouls/cdp-browser-manager:latest
```

## 外部调用示例

pydoll（直连 CDP WS，无需 WebDriver）：

```python
import asyncio
from pydoll.browser import Chrome

async def main():
    # cdp_url 填面板里复制的 CDP 地址，如 http://你的IP:9222
    browser = Chrome(options=..., connection_port=9222)
    async with browser:
        tab = await browser.start()
        await tab.go_to("https://example.com")
        print(await tab.title)

asyncio.run(main())
```

> pydoll 版本 API 有差异，若上述写法不适用，用底层 CDP WS 地址 `ws://你的IP:9222/devtools/browser/...`（可从 `/json/version` 的 `webSocketDebuggerUrl` 取得）。

DrissionPage：

```python
from DrissionPage import ChromiumOptions, WebPage

co = ChromiumOptions()
co.set_address("你的IP:9222")
page = WebPage(chromium_options=co)
page.get("https://www.baidu.com")
print(page.title)
```

端口分配范围：CDP `9222~9322`，VNC `5901~6000`，noVNC `7901~7999`（无头内核只分配 CDP 端口）。页面显示的是宿主机端口，可直接在宿主机上连接。

## 本地构建

```bash
docker build -f Dockerfile.browser -t cdp-browser-browser:latest .
docker build -f Dockerfile.manager -t cdp-browser-manager:latest .
docker compose up -d --build
```

## 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| CDP 一直"未响应"，端口连不上 | browser 用旧镜像，缺 `--user-data-dir`，Chrome 136+ 静默忽略远程调试参数 | 执行 `bash upgrade.sh`（会删旧容器并用新镜像重建） |
| 切换实例画面仍是上一个 | 目标实例容器卡死，iframe 加载失败保留旧画面 | 新版面板会显示"画面无法连接"并可重试；执行 `bash upgrade.sh` 重建 |
| noVNC 打开是目录列表 | Debian novnc 包无根目录 index.html | 面板已自动用 `/vnc_lite.html` 或 `/vnc.html`，勿手动访问根路径 |
| 抓屏一直没画面 | 标签页列表为空，或 WS 代理连不上容器 | 确认实例 CDP 已就绪；查看 manager 日志里 `/ws/cast` 是否有报错；无头内核与完整内核都已实测支持 screencast 帧 |
| 无头内核点 VNC 没反应 | 无头内核没有 Xvfb/VNC 服务 | 属正常，切到「抓屏」模式 |
| 持久目录删除报「实例正在使用」 | 有实例挂载了该 Volume | 先停止使用该目录的实例，再删除 |
| 想彻底清理持久目录数据 | Volume 不会随容器自动删除 | 面板左侧「持久用户目录」点删除，或 `docker volume rm cdp-profile-*` |

查看某个 browser 容器的内部日志（排查 chromium / Xvfb 是否起来）：

```bash
docker ps --filter label=cdp.browser.manager --format '{{.Names}}'
docker exec <容器名> tail -n 30 /tmp/chromium.log
docker exec <容器名> tail -n 20 /tmp/xvfb.log
```

## 注意

- 容器使用 `--no-sandbox`，适合内网受控环境，不要裸奔公网。
- manager 挂载了 `docker.sock`，等同于拥有宿主机 Docker 控制权，只能在可信内网使用。
- 两个镜像均由 GitHub Actions 自动构建推送至 ghcr.io。
- GHCR 仓库名必须全小写，因此镜像路径使用 `forallcodesouls`（对应 GitHub 账号 `ForAllCodeSouls`）。
- 若 GHCR 镜像为私有，先执行 `docker login ghcr.io -u ForAllCodeSouls` 再启动。
