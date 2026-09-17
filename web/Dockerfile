# ============================================================
# 足迹 Footprint 前端镜像（多阶段：node 构建 → nginx 服务）
# ============================================================

# ---------- Stage 1: build ----------
FROM node:22-alpine AS build
WORKDIR /app

# 先装依赖命中缓存
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# 拷源码并构建
COPY . .
RUN npm run build

# ---------- Stage 2: serve ----------
FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
