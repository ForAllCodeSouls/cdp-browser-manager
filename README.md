# cdp-browser-manager

多 Chromium 实例管理器，每个实例独立 CDP 远程调试端口 + noVNC 可视化画面。
适合配合 DrissionPage / CDP 自动化脚本使用。

## 架构

采用**双镜像分层**设计，manager（小）与 browser（大）独立构建和发布：

| 镜像 | 大小 | 内容 | 更新频率 |
|---|---|---|---|
| `ghcr.io/forallcodesouls/cdp-browser-manager:latest` | 约 40 MB | FastAPI 管理面板，通过 Docker API 调度 browser 容器 | 代码改动时 |
| `ghcr.io/forallcodesouls/cdp-browser-browser:latest` | 约 500 MB | Chromium + Xvfb + x11vnc + noVNC + 中文字体 | 很少变化 |

manager 启动后挂载宿主机 Docker socket，按需创建/销毁 browser 容器。每个 browser 容器内部固定使用 9222（CDP）、5900（VNC）、6080（noVNC），再通过端口映射分配宿主机独立端口。日常只更新 manager 时，大镜像层命中缓存，几乎零下载。

## 功能
- 一键新建浏览器实例，自动分配独立 CDP 端口、VNC、noVNC
- Web 面板预览浏览器画面
- 复制 CDP 调试地址，外部程序连接控制
- 停止单个 / 全部浏览器实例
- manager 重启后自动恢复已有实例（通过容器标签识别）

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
```

在 `docker-compose.yml` 所在目录执行：

```bash
docker compose pull             # 拉取两个镜像
docker compose up -d            # 后台启动 manager
docker compose ps              # 查看 manager 状态
docker compose logs -f         # 跟踪日志
docker compose down            # 停止并移除 manager（browser 实例会保留）
```

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

## 外部调用示例（DrissionPage）

```
from DrissionPage import ChromiumOptions, WebPage

co = ChromiumOptions()
co.set_address("127.0.0.1:9222")
page = WebPage(chromium_options=co)
page.get("https://www.baidu.com")
print(page.title)
```

端口分配范围：CDP `9222~9322`，VNC `5901~6000`，noVNC `7901~7999`。页面显示的是宿主机端口，可直接在宿主机上连接。

## 本地构建

```bash
docker build -f Dockerfile.browser -t cdp-browser-browser:latest .
docker build -f Dockerfile.manager -t cdp-browser-manager:latest .
docker compose up -d --build
```

## 注意

- 容器使用 `--no-sandbox`，适合内网受控环境，不要裸奔公网。
- manager 挂载了 `docker.sock`，等同于拥有宿主机 Docker 控制权，只能在可信内网使用。
- 两个镜像均由 GitHub Actions 自动构建推送至 ghcr.io。
- GHCR 仓库名必须全小写，因此镜像路径使用 `forallcodesouls`（对应 GitHub 账号 `ForAllCodeSouls`）。
- 若 GHCR 镜像为私有，先执行 `docker login ghcr.io -u ForAllCodeSouls` 再启动。
