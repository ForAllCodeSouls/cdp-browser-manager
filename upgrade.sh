#!/bin/bash
# 一键升级：拉取最新镜像 -> 删除所有旧 browser 容器 -> 重建 manager。
# 解决"改了代码但容器仍用旧镜像/旧启动脚本"的问题。
set -e

cd "$(dirname "$0")"

echo "==> 1/5 拉取最新 compose 配置"
git pull

echo "==> 2/5 拉取最新镜像（manager + full/headless 两份 browser 镜像）"
# 显式带上 profile，确保两份带 profile 的占位服务也被拉取。
docker compose --profile no-auto-start pull

echo "==> 3/5 删除所有旧的 browser 实例容器（持久目录 Volume 会保留，不会删数据）"
docker ps -aq --filter label=cdp.browser.manager | xargs -r docker rm -f

echo "==> 4/5 重建 manager"
docker compose up -d --remove-orphans

echo "==> 5/5 等待 manager 就绪并校验（最多 30 秒）"
ready=0
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
        ready=1
        echo "manager 已就绪"
        break
    fi
    sleep 1
done
if [ "$ready" -ne 1 ]; then
    echo "警告：manager 30 秒内未就绪，请执行 docker compose logs -f 查看原因。"
fi
curl -fsS http://127.0.0.1:8000/api/list >/dev/null 2>&1 || true

echo
echo "当前镜像摘要（应为此前公布的最新 digest）："
docker images --digests | grep cdp-browser || true
echo
echo "浏览器容器（应为空，新建实例后才出现）："
docker ps -a --filter label=cdp.browser.manager --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo
echo "持久用户目录（升级后应完整保留）："
docker volume ls --filter label=cdp.profile=1 --format 'table {{.Name}}'
echo
echo "完成。打开面板：http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000"
echo "然后点「新建实例」，卡片 CDP 徽章应在 15 秒内变为「CDP 已就绪」。"
