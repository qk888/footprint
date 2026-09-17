"""意图分类评估 / 回归脚本。

用法（在 backend 容器里跑）：
    docker exec footprint-backend python scripts/eval_intent.py

评估集：`train_data/routing_eval_cases.json` —— 199 条人工标注的路由样本
（Qoder 时期 supervisor 路由数据，标签是人工标的，**与 intent_seeds.yml 的种子句来源独立**，
所以拿它测向量方案不会被"自己出题自己考"污染）。

历史成绩（改意图判定前后）：
    线上旧方案（规则 + 生成模型）      79.4%
    向量方案第一版（规则 + 向量）      93.0%
    + 修攻略误伤 / 补足迹规则          96.0%
    + 补"问 AI 自身"类闲聊种子         97.0%
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# 直接 `python scripts/eval_intent.py` 时 sys.path[0] 是 scripts/，找不到 app 包
# （README/记忆里记的就是这条命令 —— 之前一直跑不起来，只会报 ModuleNotFoundError:
#  No module named 'app'。load_knowledge.py 早就有这行，这里补上）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.footprint_agent import classify_intent
from app.agent.utils.logger_handler import logger as app_logger

CASES_PATH = Path(os.getenv("EVAL_CASES", "/app/train_data/routing_eval_cases.json"))
if not CASES_PATH.exists():
    CASES_PATH = Path(__file__).resolve().parent.parent / "train_data" / "routing_eval_cases.json"

RULE_MARKS = (
    "关键词/足迹命中", "关键词命中", "旅游/旅行+动作词", "问怎么玩",
    "城市+天数", "旅游/旅行+攻略词", "只提旅游/旅行",
)

_records: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        try:
            _records.append(record.getMessage())
        except Exception:
            pass


async def main() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    app_logger.addHandler(_Capture())
    app_logger.setLevel(logging.INFO)

    rows = []
    for q, expect in cases:
        _records.clear()
        got = await classify_intent(q)
        path = "vector"
        for m in _records:
            if any(k in m for k in RULE_MARKS):
                path = "rule"
                break
        rows.append((q, expect, got, got == expect, path))

    rule = [r for r in rows if r[4] == "rule"]
    vec = [r for r in rows if r[4] == "vector"]
    rok = sum(1 for r in rule if r[3])
    vok = sum(1 for r in vec if r[3])
    total = rok + vok
    n = len(rows)

    print("=" * 74)
    print("意图分类评估 —— %s（%d 条）" % (CASES_PATH.name, n))
    print("=" * 74)
    print("[规则层]  %d 条, 正确 %d, 准确率 %.1f%%" % (len(rule), rok, 100.0 * rok / max(len(rule), 1)))
    print("[向量层]  %d 条, 正确 %d, 准确率 %.1f%%" % (len(vec), vok, 100.0 * vok / max(len(vec), 1)))
    print("[端到端]  %d/%d = %.1f%%" % (total, n, 100.0 * total / n))

    miss = [r for r in rows if not r[3]]
    if miss:
        print()
        print("--- 判错的 %d 条（预期里有一部分是原标签本身可疑）---" % len(miss))
        for q, expect, got, _ok, path in miss:
            print("  [%-6s] 期望 %-9s 判 %-9s | %s" % (path, expect, got, q))


asyncio.run(main())
