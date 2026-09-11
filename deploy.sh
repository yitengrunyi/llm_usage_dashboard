#!/bin/bash
# ─── 配置区：改成你的服务器信息 ───
SERVER_USER="root"
SERVER_HOST="你的服务器IP"
SERVER_DIR="/opt/llm-usage-dashboard"
SSH_PORT=22
# ─────────────────────────────────

set -e
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== 1/4 同步文件到服务器 ==="
rsync -avz --progress \
  --exclude 'node_modules/' \
  --exclude '__pycache__/' \
  --exclude '.git/' \
  --exclude '*.pyc' \
  --exclude 'frontend/dist/' \
  --exclude '.DS_Store' \
  --exclude 'backend/venv/' \
  --exclude 'backend/data/' \
  --exclude 'backend/config/sessions/' \
  --exclude '*.log' \
  -e "ssh -p ${SSH_PORT}" \
  "${PROJECT_DIR}/" "${SERVER_USER}@${SERVER_HOST}:${SERVER_DIR}/"

echo "=== 2/4 上传 .env 文件 ==="
rsync -avz -e "ssh -p ${SSH_PORT}" \
  "${PROJECT_DIR}/backend/.env" \
  "${SERVER_USER}@${SERVER_HOST}:${SERVER_DIR}/backend/.env"

echo "=== 3/4 在服务器上构建并启动 ==="
ssh -p ${SSH_PORT} "${SERVER_USER}@${SERVER_HOST}" << 'REMOTE'
  cd /opt/llm-usage-dashboard
  docker compose down 2>/dev/null || true
  docker compose up -d --build
  echo ""
  echo "=== 容器状态 ==="
  docker compose ps
REMOTE

echo ""
echo "=== 4/4 部署完成 ==="
echo "  前端:   http://${SERVER_HOST}:3080"
echo "  noVNC:  http://${SERVER_HOST}:6080"
echo "  API:    http://${SERVER_HOST}:3081/api/litellm/health"
