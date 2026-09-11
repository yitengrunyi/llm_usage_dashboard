#!/bin/bash
set -e

# 清理上一次没干净的 Xvfb lock (docker compose restart 不重建容器, /tmp 残留)
rm -f /tmp/.X99-lock
rm -f /tmp/.X11-unix/X99

# 启动虚拟显示 (Playwright headless=False 需要)
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp +extension RANDR &
export DISPLAY=:99

# 等 Xvfb 就绪后设置键盘布局
sleep 1
setxkbmap -display :99 us

# 启动 VNC 服务 + noVNC Web 界面 (用户通过浏览器 :6080 看到桌面)
x11vnc -display :99 -forever -nopw -shared -rfbport 5900 -xkb -capslock -skip_dups -q &
websockify --web /usr/share/novnc 6080 localhost:5900 > /dev/null 2>&1 &

# 创建自动跳转页面，打开 6080 直接进入 VNC 桌面
cat > /usr/share/novnc/index.html << 'NOVNC_EOF'
<!DOCTYPE html>
<html>
<head><meta http-equiv="refresh" content="0;url=vnc.html?autoconnect=true&resize=scale&clipboard_up=true&clipboard_down=true"></head>
<body>Connecting to VNC...</body>
</html>
NOVNC_EOF

echo "noVNC ready at http://0.0.0.0:6080"

# 运行数据库迁移 (LiteLLM 模块)
echo "Running database migrations..."
alembic -c litellm/alembic.ini upgrade head || echo "Migration warning (may already be up to date)"

# 启动 FastAPI
# --workers 1: PG 服务器 max_connections=100 但是共享池 (其他应用占 60+ idle slot),
# 多 worker 会翻倍占连接 → 砍到 1 worker, 配合 pool_size=3 控制总占用
echo "Starting backend on port 3081..."
exec uvicorn main:app --host 0.0.0.0 --port 3081 --workers 1
