# ============================================================
# 足迹 Footprint 后端镜像（多阶段：builder 装依赖 → runtime 精简）
# Python 3.13（项目要求 >=3.13）+ uv 管理依赖，venv 固定在 /opt/venv
# ============================================================

# uv 二进制来源（单独一个 stage 承接：BuildKit 不支持在 COPY --from 里写变量，
# 只能在 FROM 里用全局 ARG）
#
# 为什么可换：国内直连 ghcr.io 会 **TLS 握手超时**（实测 2026-09-15 完全不可达，
# 报 "failed to fetch anonymous token ... TLS handshake timeout"，三次重试都一样），
# 那会直接堵死"改后端 → 重建镜像"这条路。所以默认走南大镜像（实测 0.3 秒响应）。
# 换回官方源：
#   docker compose build --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:latest backend
ARG UV_IMAGE=ghcr.nju.edu.cn/astral-sh/uv:latest
FROM ${UV_IMAGE} AS uvbin

# ---------- Stage 1: builder ----------
FROM python:3.13-slim AS builder

COPY --from=uvbin /uv /uvx /usr/local/bin/

# 部分包可能需要本地编译，装上工具链
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先只拷依赖清单，命中 Docker 层缓存（源码变动不重装依赖）
COPY pyproject.toml uv.lock ./
# 项目本身无 build-system，用 --no-install-project 只装第三方依赖
RUN uv sync --frozen --no-install-project --no-dev

# ---------- Stage 2: runtime ----------
FROM python:3.13-slim AS runtime

# numpy/pandas/chromadb 运行时需要的共享库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 从 builder 拷已装好的虚拟环境
COPY --from=builder /opt/venv /opt/venv

# 拷后端源码
COPY . .

# 防 Windows CRLF 导致 entrypoint 无法执行
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
