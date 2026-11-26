# cdp-browser-manager

多 Chromium 实例管理器，每个实例独立 CDP 远程调试端口，可选 **VNC** 或 **CDP 抓屏** 查看画面。
适合配合任何基于 CDP 协议的自动化脚本或客户端使用。

> ⚠️ **免责声明**：本项目**仅限合法授权的自动化测试、开发调试与内网运维**。严禁用于未经授权的访问、爬取、绕过风控、批量注册、刷量或任何违法违规活动。下载、部署或使用本项目即表示你已阅读并同意文末 [免责声明](#免责声明)，并自行承担全部责任。

## 架构

采用**三份镜像独立构建**的设计，manager 与两种内核的 browser 各自发布，改哪份只重新构建哪份：

| 镜像 | 大小 | 内容 | 更新频率 |
|---|---|---|---|
| `ghcr.io/forallcodesouls/cdp-browser-manager:latest` | 约 40 MB | FastAPI 管理面板，通过 Docker API 调度 browser 容器 | 代码改动时 |
| `ghcr.io/forallcodesouls/cdp-browser-full:latest` | 约 480 MB | 精简后的完整 `chrome` + Xvfb + x11vnc + noVNC + 中文字体 | 完整内核升级时 |
| `ghcr.io/forallcodesouls/cdp-browser-headless:latest` | 约 200 MB | 精简后的 `chrome-headless-shell` + 中文字体，无 VNC 栈 | 无头内核升级时 |

浏览器使用 **Chrome for Testing**：Google 官方为自动化测试发布的版本，无自动更新、版本可复现、不含推广内容。两份镜像各只装一份二进制，新建实例时选择内核，manager 按 `browser_mode` 拉取对应镜像：

| 内核 | 镜像 / 二进制 | 画面方式 | 说明 |
|---|---|---|---|
| 完整内核（full，默认） | `cdp-browser-full` / `chrome` | VNC + CDP 抓屏 | 有地址栏/标签栏，可看完整浏览器界面 |
| 无头内核（headless） | `cdp-browser-headless` / `chrome-headless-shell` | 仅 CDP 抓屏 | 更省资源，无浏览器界面；不分配 VNC/noVNC 端口 |

两种内核的 CDP 端口都是容器内 9222，外部客户端的连接方式完全一致。版本号分别在 `Dockerfile.browser` 和 `Dockerfile.browser-headless` 顶部的 `CHROME_VERSION` 中锁定：需要单独升级某个内核就只改对应文件；想保持一致就两处一起改。CI 会分别构建推送，互不影响。

启动参数已加入去 Google 化开关（`--disable-background-networking`、`--disable-component-update`、`--disable-sync`、`--metrics-recording-only` 等），关闭后台联网与组件更新；官方 for Testing 包本身不含第三方渠道的广告内容。

### 镜像精简说明

Chrome 官方 zip 本身约 198 MB，但解压后是 412 MB，再加系统库和中文字体才堆到几百 MB。两份镜像做了以下精简（实测不影响 CDP、抓屏和 WebGL）：

| 精简项 | 节省 | 说明 |
|---|---|---|
| Noto CJK → 文泉驿微米黑 | 约 86 MB | 简体中文显示正常；繁体/日韩字形覆盖较弱 |
| `locales/` 220 个语言包 → 只留中英 | 约 46 MB | 仅影响 Chrome 界面语言，网页内容不受影响 |
| 删除 WidevineCdm（完整版） | 约 21 MB | DRM 视频播放组件，自动化用不到 |
| 清理 apt 文档 / 构建期工具 | 约 12 MB | 同一构建层内清除，分层删不会减小镜像 |

**两个绝对不能删的东西**（曾因误删导致容器反复重启，已修复）：

- `chrome_crashpad_handler`：Linux Chrome 启动时会 `exec` 它，文件缺失直接 FATAL 崩溃。
- 动态库 `libxkbcommon.so.0` / `libudev.so.1` / `libatspi.so.0`：它们在包里是传递依赖，若不在 Dockerfile 里**显式安装**，`apt autoremove` 会把它们当孤儿删掉，浏览器随即起不来。现在已显式列出，标记为 manual 不会被清理。

**刻意保留**：`libvk_swiftshader.so` / `libEGL.so` / `libGLESv2.so`。虽然启动参数有 `--disable-gpu`，实测删除后 WebGL 会变成 `NO_WEBGL`；它们合计仅 4 MB，不值得为省这点空间丢功能。

manager 启动后挂载宿主机 Docker socket，按需创建/销毁 browser 容器。完整内核容器内部固定使用 9222（CDP）、5900（VNC）、6080（noVNC），无头内核只使用 9222，再通过端口映射分配宿主机独立端口。`start.sh` 由两份镜像共用，改动它会同时重建两份 browser 镜像；只升级 manager 时大镜像层命中缓存，几乎零下载。

### 可配置环境变量

manager 支持以下环境变量（Compose 中已给出常用项）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BROWSER_IMAGE_FULL` / `BROWSER_IMAGE_HEADLESS` | 官方 GHCR 镜像 | 指定两份 browser 镜像；旧 `BROWSER_IMAGE` 仍作为 full 镜像兼容 |
| `PUBLIC_HOST` | 空 | 反向代理时填对外地址，如 `https://cdp.example.com`；留空按请求 Host 生成链接 |
| `MANAGER_ID` | `default` | 多个 manager 共用一台宿主机时，隔离容器标签与持久卷 |
| `BROWSER_SHM_SIZE` | `512m` | 每个 browser 容器的 `/dev/shm`；启动参数已带 `--disable-dev-shm-usage` |
| `CAST_MAX_WIDTH` / `CAST_MAX_HEIGHT` | `1920` / `1080` | 抓屏帧上限，避免高分屏帧拖慢 WS 转发 |
| `LOG_LEVEL` | `INFO` | manager 日志级别 |

`GET /api/health` 是存活探针：同时检查面板服务与 Docker 守护进程是否可用。镜像内 `HEALTHCHECK` 和 Compose 健康检查都使用它。

## 功能
- 一键新建浏览器实例，可选完整/无头内核，自动分配独立 CDP 端口（完整内核另分配 VNC、noVNC）
- **画面双模式，随时切换**：
  - **VNC**：完整桌面画面，可见地址栏/标签栏（仅完整内核）
  - **CDP 抓屏**：基于 CDP `Page.startScreencast`，画面与自动化共用同一 CDP 端口；支持标签切换、新建/关闭标签、地址栏导航、鼠标键盘交互（两种内核通用，无头内核只能用它）
    - 实现上由 manager 的 `/ws/cast/{sid}/{tid}` 把容器内的 screencast 帧代理到浏览器 canvas，并转发输入事件。不直接用 Chrome 自带的 `devtools/inspector.html`——它的 CSP 只允许 `ws://127.0.0.1`，公网访客连不上。
    - 输入支持键盘字符、左/右/中键与滚轮；鼠标移动事件按帧合并，WS 意外断开会自动重连，切换实例或标签时会作废旧连接避免画面串台。
- **用户目录双模式**：
  - **临时目录**：停止实例即清理，适合一次性任务
  - **持久目录**：以 Docker Volume 保存在宿主机，停止实例后数据仍在；新建时可选择「新建持久目录」或「复用已有目录」恢复登录态，需在面板左侧手动点删除才会清除
- Web 面板预览浏览器画面（左侧点击实例切换，画面自动连接）
- 复制 CDP 调试地址，外部客户端可通过 CDP WebSocket 连接控制
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

# 查看容器日志与退出码（实例反复 restarting 时用；面板详情栏也有「日志」按钮）
curl http://你的IP:8000/api/instance/{sid}/logs?tail=200
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
      BROWSER_IMAGE_FULL: ghcr.io/forallcodesouls/cdp-browser-full:latest
      BROWSER_IMAGE_HEADLESS: ghcr.io/forallcodesouls/cdp-browser-headless:latest

  # 占位服务：让 docker compose pull 能同时更新两份 browser 大镜像
  cdp-browser-full-image:
    image: ghcr.io/forallcodesouls/cdp-browser-full:latest
    profiles: ["no-auto-start"]

  cdp-browser-headless-image:
    image: ghcr.io/forallcodesouls/cdp-browser-headless:latest
    profiles: ["no-auto-start"]
```

> 两个占位服务带 profile，不会被 `docker compose up -d` 启动，只用于让 `docker compose --profile no-auto-start pull` 把两份 browser 镜像一并更新。**升级时务必先 pull**，否则 manager 会用本地缓存的旧 browser 镜像创建容器，导致 CDP 无法连接。`upgrade.sh` 已带上该参数。
>
> 兼容旧部署：如果仍只设置了 `BROWSER_IMAGE`，它会被当作 full 镜像使用；headless 镜像始终读 `BROWSER_IMAGE_HEADLESS`。建议尽快换成上面两个新变量。

在 `docker-compose.yml` 所在目录执行。**推荐直接用一键升级脚本**（含拉取镜像、删旧容器、重建、校验）：

```bash
bash upgrade.sh
```

等价的手动命令：

```bash
docker compose --profile no-auto-start pull   # 拉取 manager + full/headless 三份镜像（含占位服务）
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
  -e BROWSER_IMAGE_FULL=ghcr.io/forallcodesouls/cdp-browser-full:latest \
  -e BROWSER_IMAGE_HEADLESS=ghcr.io/forallcodesouls/cdp-browser-headless:latest \
  ghcr.io/forallcodesouls/cdp-browser-manager:latest
```

## 外部调用

每个实例都暴露标准的 Chrome DevTools Protocol（CDP）端点，任何实现 CDP 客户端的库都可以直接连接，无需针对本项目做适配。

连接信息从面板的实例详情里复制，包含两部分：

- **HTTP 地址**：如 `http://你的IP:9222`，用于发现目标和读取元信息
  - `GET /json/version` —— 浏览器版本，以及 `webSocketDebuggerUrl`
  - `GET /json/list` —— 当前标签页列表，含每个页面的 `id` 与 `webSocketDebuggerUrl`
- **WebSocket 地址**：如 `ws://你的IP:9222/devtools/browser/...`（浏览器级）或 `ws://你的IP:9222/devtools/page/<id>`（页面级），连接后按 CDP 协议发送 `Page.navigate`、`Runtime.evaluate` 等命令即可。

两种内核的 CDP 端口与协议完全一致，连接代码不需要区分内核；无头内核只是不提供 VNC/noVNC 端口。

端口分配范围：CDP `9222~9322`，VNC `5901~6000`，noVNC `7901~7999`（无头内核只分配 CDP 端口）。页面显示的是宿主机端口，可直接在宿主机上连接。

## 本地构建

```bash
# 两份 browser 镜像可单独构建，互不影响
docker build -f Dockerfile.browser         -t ghcr.io/forallcodesouls/cdp-browser-full:latest .
docker build -f Dockerfile.browser-headless -t ghcr.io/forallcodesouls/cdp-browser-headless:latest .
docker build -f Dockerfile.manager         -t ghcr.io/forallcodesouls/cdp-browser-manager:latest .

# 若改的是本地 tag，同步修改 compose 里的镜像名后再启动
docker compose up -d
```

## 修改后自检

改完代码可以先在本地跑一遍静态检查，确认没有低级错误再提交：

```bash
python -m py_compile app/main.py                  # 后端语法
bash -n start.sh upgrade.sh                       # 启动/升级脚本
docker compose config -q                          # Compose 配置
curl -fsS http://127.0.0.1:8000/api/health        # 启动后探针
```

## 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| CDP 一直"未响应"，端口连不上 | browser 用旧镜像，缺 `--user-data-dir`，Chrome 136+ 静默忽略远程调试参数 | 执行 `bash upgrade.sh`（会删旧容器并用新镜像重建） |
| 切换实例画面仍是上一个 | 目标实例容器卡死，iframe 加载失败保留旧画面 | 新版面板会显示"画面无法连接"并可重试；执行 `bash upgrade.sh` 重建 |
| noVNC 打开是目录列表 | Debian novnc 包无根目录 index.html | 面板已自动用 `/vnc_lite.html` 或 `/vnc.html`，勿手动访问根路径 |
| 抓屏一直没画面 | 标签页列表为空，或 WS 代理连不上容器 | 确认实例 CDP 已就绪；查看 manager 日志里 `/ws/cast` 是否有报错；无头内核与完整内核都已实测支持 screencast 帧 |
| 无头内核点 VNC 没反应 | 无头内核没有 Xvfb/VNC 服务 | 属正常，切到「抓屏」模式 |
| 创建实例报镜像不存在 | 本地未拉取对应内核镜像，或 compose 仍用旧的 `cdp-browser-browser` | 执行 `bash upgrade.sh`（会 pull full/headless 两份镜像）；自定义部署需设 `BROWSER_IMAGE_FULL` / `BROWSER_IMAGE_HEADLESS` |
| 持久目录删除报「实例正在使用」 | 有实例挂载了该 Volume | 先停止使用该目录的实例，再删除 |
| 想彻底清理持久目录数据 | Volume 不会随容器自动删除 | 面板左侧「持久用户目录」点删除，或 `docker volume rm cdp-profile-*` |

查看某个 browser 容器的内部日志（排查 chromium / Xvfb 是否起来）：

```bash
docker ps --filter label=cdp.browser.manager --format '{{.Names}}'
docker exec <容器名> tail -n 30 /tmp/chromium.log
docker exec <容器名> tail -n 20 /tmp/xvfb.log
```

## 免责声明

在下载、部署、修改或使用本项目之前，请完整阅读以下条款：

### 1. 用途限制

本项目是通用的浏览器实例管理工具，仅用于**自有系统或已获得明确授权的目标**上的自动化测试、开发调试、演示与内网运维。它不提供、也不应被用于规避任何网站的安全措施。

### 2. 禁止用途

使用者**不得**将本项目或其衍生成果用于：

- 未经授权访问、爬取、抓取他人网站、系统或数据；
- 绕过验证码、反爬机制、风控策略、访问控制或付费限制；
- 批量注册、刷量、刷单、薅羊毛、制造虚假流量、群发垃圾信息；
- 钓鱼、仿冒、窃取账号凭证或个人隐私数据；
- 对他人系统发起扫描、攻击、渗透或其他破坏性操作；
- 任何违反目标网站服务条款、`robots.txt` 或适用法律法规的行为。

### 3. 责任归属

- 使用者是其所创建的实例、运行的脚本及产生的流量的**唯一责任人**，须自行确认已获得目标方授权，并遵守所在地法律法规与目标网站条款。
- 本项目按「原样」提供，不附带任何明示或暗示的担保（包括但不限于适销性、特定用途适用性、不侵权）。作者与维护者对因使用、滥用或无法使用本项目造成的任何直接、间接或连带损失及法律后果**不承担任何责任**。
- 因使用本项目引发的投诉、调查或诉讼，由使用者自行处理并承担全部费用与后果。

### 4. 安全提示

默认配置使用 `--no-sandbox`、挂载宿主机 Docker socket，且 CDP/VNC 端口无认证。**仅限可信内网使用，禁止直接暴露到公网**。如需公网部署，须自行配置认证、防火墙与访问控制，由此产生的入侵、数据泄露等风险由部署者自负。

### 5. 其他

作者与维护者有权拒绝为可疑或违规用途提供任何支持，并可随时修改或停止本项目的分发。若你不接受以上条款，请勿下载、部署或使用本项目；继续使用即视为已充分理解并同意本免责声明。

## 注意

- 容器使用 `--no-sandbox`，适合内网受控环境，不要裸奔公网。
- manager 挂载了 `docker.sock`，等同于拥有宿主机 Docker 控制权，只能在可信内网使用。
- 三份镜像均由 GitHub Actions 自动构建推送至 ghcr.io；CI 里是三个独立步骤、独立构建缓存，改无头镜像不会重打完整镜像；同一分支的构建会排队，避免 `latest` 被旧 run 覆盖，也可在 Actions 页面手动触发重建。
- 创建实例时会扫描宿主机上所有 Docker 容器占用的端口，避让非本管理器创建的 9222/5900/6080 段端口。
- 旧镜像名 `cdp-browser-browser:latest` 已弃用；升级时执行 `bash upgrade.sh` 会用新镜像重建实例容器。
- GHCR 仓库名必须全小写，因此镜像路径使用 `forallcodesouls`（对应 GitHub 账号 `ForAllCodeSouls`）。
- 若 GHCR 镜像为私有，先执行 `docker login ghcr.io -u ForAllCodeSouls` 再启动。
