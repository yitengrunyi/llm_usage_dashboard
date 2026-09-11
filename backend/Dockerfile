FROM docker.m.daocloud.io/library/python:3.11-slim-bookworm

# 使用阿里云镜像源加速
RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources

# 系统依赖：Xvfb (虚拟显示) + x11vnc + noVNC (Web VNC)
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify procps \
    x11-xkb-utils xkb-data \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt \
    && playwright install --with-deps --no-shell chromium

COPY . .

# 确保 config 和 data 目录存在
RUN mkdir -p config/sessions data

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000 6080

CMD ["/docker-entrypoint.sh"]
