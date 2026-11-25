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
  -p 7901-8000:7901-8000 \
  ghcr.io/forallcodesouls/cdp-browser-manager:latest
```

访问面板：

```
http://你的IP:8000
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

## 注意

- 容器使用 `--no-sandbox`，适合内网受控环境，不要裸奔公网。
- 端口池默认范围：CDP `9222~9322`，noVNC `7901~8000`。
- 镜像由 GitHub Actions 自动构建推送至 ghcr.io。
- GHCR 仓库名必须全小写，因此镜像路径使用 `forallcodesouls`（对应 GitHub 账号 `ForAllCodeSouls`）。
