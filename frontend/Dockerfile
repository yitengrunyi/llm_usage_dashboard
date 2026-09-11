# Stage 1: 构建 Vue 主前端 (dashboard)
FROM docker.m.daocloud.io/library/node:20-alpine AS vue-builder
WORKDIR /app
RUN npm config set registry https://registry.npmmirror.com
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# Stage 2: 构建 LiteLLM admin React UI (源码在 litellm-ui-src/, vite base 已配 /litellm-ui/)
FROM docker.m.daocloud.io/library/node:20-alpine AS react-builder
WORKDIR /app
RUN npm config set registry https://registry.npmmirror.com
COPY litellm-ui-src/package*.json ./
RUN npm ci
COPY litellm-ui-src/ .
RUN npm run build

# Stage 3: Nginx 服务 — Vue 在根, React 在 /litellm-ui/
FROM docker.m.daocloud.io/library/nginx:alpine
COPY --from=vue-builder /app/dist /usr/share/nginx/html
COPY --from=react-builder /app/dist /usr/share/nginx/html/litellm-ui
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
