# cdp-browser-manager

多 Chromium 实例管理器，每个实例独立 CDP 远程调试端口 + noVNC 可视化画面。
适合配合 DrissionPage / CDP 自动化脚本使用。

## 功能
- 一键新建浏览器实例，自动分配独立 CDP 端口、VNC、noVNC
- Web 面板预览浏览器画面
- 复制 CDP 调试地址，外部程序连接控制
- 停止单个 / 全部浏览器实例

## 运行
```bash
docker run -d \
  --name cdp-browser \
  --restart unless-stopped \
  -p 8000:8000 \
  -p 9222-9322:9222-9322 \
  -p 7901-7999:7901-7999 \
  ghcr.io/forallcodesouls/cdp-browser-manager:latest
```

访问面板：

```
http://你的IP:8000
```

## Docker Compose 部署（推荐）

项目根目录已附带 `docker-compose.yml`，内容如下：

```yaml
services:
  cdp-browser:
    image: ghcr.io/forallcodesouls/cdp-browser-manager:latest
    container_name: cdp-browser
    restart: unless-stopped
    ports:
      - "8000:8000"
      - "9222-9322:9222-9322"
      - "7901-7999:7901-7999"
    shm_size: "2gb"
```

在 `docker-compose.yml` 所在目录执行：

```bash
docker compose up -d          # 后台启动
docker compose ps             # 查看容器状态
docker compose logs -f        # 跟踪日志
docker compose down           # 停止并移除容器
docker compose pull           # 拉取最新镜像（配合 up -d 更新）
```

说明：

- `shm_size: "2gb"`：为 Chromium 提供共享内存，避免默认 64MB 导致页面崩溃；镜像内虽已加 `--disable-dev-shm-usage`，仍建议保留。
- 端口 `7901-7999`：noVNC 实例上限 99 个，已避开被管理面板占用的 8000 端口。
- 若 GHCR 镜像为私有，先执行 `docker login ghcr.io -u ForAllCodeSouls` 再启动。
- 如需本地构建而非拉取远程镜像，把 `image:` 改为 `build: .`，然后执行 `docker compose up -d --build`。

## 外部调用示例（DrissionPage）

```
from DrissionPage import ChromiumOptions, WebPage

co = ChromiumOptions()
co.set_address("127.0.0.1:9222")
page = WebPage(chromium_options=co)
page.get("https://www.baidu.com")
print(page.title)
```

## 注意

- 容器使用 `--no-sandbox`，适合内网受控环境，不要裸奔公网。
- 端口池默认范围：CDP `9222~9322`，noVNC `7901~7999`。
- 镜像由 GitHub Actions 自动构建推送至 ghcr.io。
- GHCR 仓库名必须全小写，因此镜像路径使用 `forallcodesouls`（对应 GitHub 账号 `ForAllCodeSouls`）。
