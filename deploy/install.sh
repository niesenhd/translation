#!/usr/bin/env bash
# ====================================================================
# 测试服务器一键部署脚本（Ubuntu 24.04 / 26.04）
# 用法：
#   sudo bash install.sh
# 前置：本脚本需要放在项目根目录的 deploy/ 下，且整个项目已经
# 上传到服务器（例如 /opt/translation/）
#
# 历次迭代修复：
#  - v2: Docker 官方源在国内无法访问 → 改用清华源
#  - v2: 后端镜像装 LibreOffice 慢，SSH 容易 idle 中断 → build 改后台 nohup
#  - v2: 容器启动慢，hub.docker.com 直拉超时 → 加 docker 镜像加速
#  - v4: 不修改主机防火墙/fail2ban，由运维按现网安全策略管理
#  - v4: 所有随机凭据仅写入 chmod 600 的 deploy/.env，不打印、不另存副本
#  - v3: SSH idle 超时断开 → 手动配置 KexAlgorithms + ClientAliveInterval
#  - v3: PaddleOCR 推理 CPU 爆满 → 通过 num_threads=4 限制 OCR 线程数
# ====================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$PROJECT_DIR/deploy"
BUILD_LOG="/var/log/translation-build.log"

# 必须 root 跑
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "请用 sudo 运行：sudo bash install.sh"
  exit 1
fi

echo "==[1/7] 系统更新 + 基础工具==============================="
export DEBIAN_FRONTEND=noninteractive

# 切换到清华 Ubuntu 源（仅当还在用官方 archive.ubuntu.com 时）
# Ubuntu 24.04+ 使用 deb822 格式 /etc/apt/sources.list.d/ubuntu.sources
if [ -f /etc/apt/sources.list.d/ubuntu.sources ] && \
   grep -q "archive.ubuntu.com" /etc/apt/sources.list.d/ubuntu.sources; then
  echo "  -> 切换 Ubuntu apt 源到清华镜像"
  cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.bak
  sed -i 's|http://archive.ubuntu.com/ubuntu/|https://mirrors.tuna.tsinghua.edu.cn/ubuntu/|g; s|http://security.ubuntu.com/ubuntu/|https://mirrors.tuna.tsinghua.edu.cn/ubuntu/|g' \
    /etc/apt/sources.list.d/ubuntu.sources
fi
# 兼容老格式 /etc/apt/sources.list
if [ -f /etc/apt/sources.list ] && grep -q "archive.ubuntu.com" /etc/apt/sources.list; then
  cp /etc/apt/sources.list /etc/apt/sources.list.bak
  sed -i 's|http://archive.ubuntu.com/ubuntu/|https://mirrors.tuna.tsinghua.edu.cn/ubuntu/|g; s|http://security.ubuntu.com/ubuntu/|https://mirrors.tuna.tsinghua.edu.cn/ubuntu/|g' \
    /etc/apt/sources.list
fi

apt-get update -y
apt-get -y install ca-certificates curl gnupg lsb-release \
                   git vim htop tar unzip openssl jq
# 防火墙和 fail2ban 属于宿主机安全策略，本脚本既不安装也不卸载。

timedatectl set-timezone Asia/Shanghai || true

echo "==[2/7] 安装 Docker Engine + Compose v2（清华源） =========="
# 官方源 download.docker.com 在国内 SSL 经常被重置，直接用清华
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  # 清理可能存在的旧 docker.list
  rm -f /etc/apt/sources.list.d/docker.list

  curl -fsSL https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu/gpg | \
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  # Docker 源对新 Ubuntu codename（如 resolute）通常滞后，回退到 noble
  if ! curl -fsI "https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu/dists/${CODENAME}/Release" >/dev/null 2>&1; then
    echo "  -> 清华 Docker 源暂未发布 ${CODENAME}，回退使用 noble"
    CODENAME="noble"
  fi

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu ${CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -y
  apt-get -y install docker-ce docker-ce-cli containerd.io \
                     docker-buildx-plugin docker-compose-plugin

  # 把调用本脚本的 sudo 用户加入 docker 组
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    usermod -aG docker "$SUDO_USER" || true
    echo "  -> 已将 ${SUDO_USER} 加入 docker 组（重新登录后生效）"
  fi
else
  echo "  -> Docker 已安装：$(docker --version)"
fi

echo "==[3/7] 配置 Docker 镜像加速 =============================="
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://docker.nju.edu.cn",
    "https://docker.mirrors.ustc.edu.cn"
  ],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF
systemctl daemon-reload
systemctl enable docker >/dev/null 2>&1 || true
# 条件重启：已在运行时只 reload 配置，避免中断现有容器
if systemctl is-active --quiet docker; then
  systemctl reload docker 2>/dev/null || systemctl restart docker
else
  systemctl start docker
fi
docker info 2>/dev/null | grep -A 4 "Registry Mirrors" || true

echo "==[4/7] 检查主机安全策略 ================================="
echo "  -> 不修改 UFW、iptables 或 fail2ban；请确保仅对外开放前端端口 8080"

echo "==[5/7] 准备 .env =========================================="
cd "$DEPLOY_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  SECRET_KEY=$(openssl rand -hex 32)
  ADMIN_TOKEN=$(openssl rand -hex 24)
  DATA_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
  PG_PWD=$(openssl rand -hex 12)
  REDIS_PWD=$(openssl rand -hex 24)
  MINIO_AK=$(openssl rand -hex 8)
  MINIO_SK=$(openssl rand -hex 16)

  sed -i "s|^APP_ENV=.*|APP_ENV=staging|" .env
  sed -i "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=${SECRET_KEY}|" .env
  sed -i "s|^APP_ADMIN_TOKEN=.*|APP_ADMIN_TOKEN=${ADMIN_TOKEN}|" .env
  sed -i "s|^APP_DATA_ENCRYPTION_KEY=.*|APP_DATA_ENCRYPTION_KEY=${DATA_ENCRYPTION_KEY}|" .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG_PWD}|" .env
  sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${REDIS_PWD}|" .env
  sed -i "s|^MINIO_ACCESS_KEY=.*|MINIO_ACCESS_KEY=${MINIO_AK}|" .env
  sed -i "s|^MINIO_SECRET_KEY=.*|MINIO_SECRET_KEY=${MINIO_SK}|" .env
  # AI 模型先占位，后续手动改
  sed -i "s|^DASHSCOPE_API_KEY=.*|DASHSCOPE_API_KEY=sk-placeholder-change-me|" .env

  chmod 600 .env
  echo "  -> 已生成随机凭据并写入 ${DEPLOY_DIR}/.env（权限 600，未打印明文）"
else
  echo "  -> 已存在 .env，跳过覆盖"
fi

echo "==[6/7] 拉镜像 + 构建 + 启动（后台执行）==================="
# 后端镜像装 LibreOffice 等系统依赖耗时较长（5-15 分钟）
# 使用 nohup 后台 + 日志文件，避免 SSH idle 断开导致中断
cd "$DEPLOY_DIR"

# 先拉公共镜像（短任务，前台跑）
docker compose pull --ignore-pull-failures || true

# 构建 + 启动 → 后台
echo "  -> 启动后台构建，日志：${BUILD_LOG}"
nohup bash -c "cd '${DEPLOY_DIR}' && docker compose up -d --build" \
      > "${BUILD_LOG}" 2>&1 < /dev/null &
BUILD_PID=$!
echo "  -> 后台 PID: ${BUILD_PID}"

# 等待构建完成（BUILD_TIMEOUT=0 表示无限等待，可设为秒数）
BUILD_TIMEOUT="${BUILD_TIMEOUT:-0}"
if [ "${BUILD_TIMEOUT}" -gt 0 ]; then
  echo "  -> 等待构建完成（每 30s 检查一次，超时 ${BUILD_TIMEOUT}s）..."
else
  echo "  -> 等待构建完成（每 30s 检查一次，无超时）..."
fi
elapsed=0
interval=30
while true; do
  if ! kill -0 "${BUILD_PID}" 2>/dev/null; then
    echo "  -> 构建进程已结束（耗时 ${elapsed}s）"
    break
  fi
  printf "    [%4ds] 构建中... 镜像数=%s\n" \
    "$elapsed" "$(docker images -q | wc -l | tr -d ' ')"
  if [ "${BUILD_TIMEOUT}" -gt 0 ] && [ "$elapsed" -ge "$BUILD_TIMEOUT" ]; then
    echo "  ⚠️ 构建超过 ${BUILD_TIMEOUT}s 仍在进行，请手动查看："
    echo "     tail -f ${BUILD_LOG}"
    break
  fi
  sleep "$interval"
  elapsed=$((elapsed + interval))
done

echo "==[7/7] 状态检查==========================================="
sleep 5
docker compose ps
IP=$(hostname -I | awk '{print $1}')
echo
echo "==========================================================="
echo "✅ 部署脚本执行完成"
echo "  前端:         http://${IP}:8080"
echo "  后端 Swagger: http://127.0.0.1:8000/docs（仅服务器本机/SSH 隧道）"
echo "  MinIO 控制台: http://127.0.0.1:9001（仅服务器本机/SSH 隧道）"
echo
echo "  本地账号登录：使用数据库中保留/创建的本地用户名与密码"
echo "  紧急 Bearer 管理 Token：${DEPLOY_DIR}/.env 中的 APP_ADMIN_TOKEN（不是登录密码）"
echo "  构建日志：${BUILD_LOG}"
echo
echo "  AI 模型机接入后，编辑 ${DEPLOY_DIR}/.env 修改 DASHSCOPE_*"
echo "  然后执行：cd ${DEPLOY_DIR} && docker compose up -d backend worker beat"
echo "==========================================================="
