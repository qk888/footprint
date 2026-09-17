#!/bin/sh
# ============================================================
# 后端容器启动脚本：等数据库 → 跑迁移 → 启动 uvicorn
# compose 已用 depends_on(healthy) 把关，这里再做一次 socket 兜底等待
# ============================================================
set -e

echo "[entrypoint] waiting for MySQL at ${DB_HOST:-mysql}:${DB_PORT:-3306} ..."
python - <<'PY'
import os, socket, time
host = os.getenv("DB_HOST", "mysql")
port = int(os.getenv("DB_PORT", "3306"))
for attempt in range(30):
    try:
        socket.create_connection((host, port), timeout=2).close()
        print(f"[entrypoint] MySQL ready after {attempt} retries")
        break
    except OSError:
        time.sleep(2)
else:
    raise SystemExit("[entrypoint] MySQL not reachable, aborting")
PY

echo "[entrypoint] running alembic upgrade head ..."
alembic upgrade head

# 知识库入库(攻略 + 槽位示例)。脚本内部会等 embed-model、按 md5 去重, 入过就是空操作;
# 不入 → 槽位检索不到 few-shot 示例 → travel_plan 永远"无法规划"。
# 失败绝不能挡住 API 启动, 所以只告警(set -e 下必须用 || 兜住)。
echo "[entrypoint] loading knowledge base ..."
python scripts/load_knowledge.py || echo "[entrypoint] 知识库入库失败, 旅行规划暂不可用"

# 若传入了自定义命令则执行它，否则启动 API
if [ "$#" -gt 0 ]; then
    echo "[entrypoint] exec: $*"
    exec "$@"
else
    echo "[entrypoint] starting uvicorn on 0.0.0.0:8000 ..."
    exec uvicorn main:app --host 0.0.0.0 --port 8000
fi
