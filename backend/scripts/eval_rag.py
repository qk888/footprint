"""RAG 回归评估 / 评估集跑分脚本。

用法（在 backend 容器里跑）：
    docker exec footprint-backend python scripts/eval_rag.py                 # 全部五组
    docker exec footprint-backend python scripts/eval_rag.py --skip e2e      # 只跑离线四组（毫秒级）
    docker exec footprint-backend python scripts/eval_rag.py --only e2e

评估集：`train_data/rag_eval_cases.json`，分五组：

    检索层 / 数字校验 / 中文检查 / 城市匹配   —— 离线、纯确定性、毫秒级
    端到端                                    —— 真调模型（每条 3~13 秒）

为什么要它：这四层护栏和检索策略都是**靠临时探针**一个个 bug 抠出来的，探针用完就删。
没有常驻评估集，下次改检索/提示词/护栏就不知道自己是不是又踩回去了。
每次改 `kb_retriever` / `rag_service` / `city_match` / `session_slots` 之后跑一遍。

端到端那组还会量**首字延迟（TTFT）**——流式改造前后就靠这个数看效果。
"""
import argparse
import asyncio
import inspect
import json
import os
import statistics
import sys
import time
from pathlib import Path

# 直接 python scripts/eval_rag.py 时 sys.path[0] 是 scripts/，找不到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CASES_PATH = Path(os.getenv("EVAL_CASES", "/app/train_data/rag_eval_cases.json"))
if not CASES_PATH.exists():
    CASES_PATH = Path(__file__).resolve().parent.parent / "train_data" / "rag_eval_cases.json"

GROUPS = ("retrieval", "numbers", "chinese", "city_mention", "e2e")


def _retrieval(cases):
    """检索层：认不认得出地名 + 判不判命中（geo 字典污染、同形词误判都会在这里暴露）"""
    from app.agent.travel.services.kb_retriever import get_kb_retriever

    kr = get_kb_retriever()
    bad = []
    for c in cases:
        cities = kr.find_cities(c["query"])
        rel = kr.is_relevant(c["query"], kr.search(c["query"]))
        if cities != c["cities"] or rel != c["relevant"]:
            bad.append("%s | 城市 %s(期望%s) 命中 %s(期望%s)"
                       % (c["id"], cities, c["cities"], rel, c["relevant"]))
    return len(cases) - len(bad), len(cases), bad


def _numbers(cases):
    """数字校验：用户自报的不算编造、资料外的要检出、裸计数不校验"""
    from app.agent.travel.services.rag_service import RagSummaryService as S

    bad = []
    for c in cases:
        ref = c["context"] + "\n" + c.get("query", "")
        got = S._unverified_numbers(c["answer"], ref)
        if got != c["expect"]:
            bad.append("%s | 检出 %s(期望%s)" % (c["id"], got, c["expect"]))
    return len(cases) - len(bad), len(cases), bad


def _chinese(cases):
    """中文检查：短中文要放行（放太低会丢掉合法短回答）、英文漂移要拦"""
    from app.agent.travel.services.rag_service import RagSummaryService as S

    bad = []
    for c in cases:
        got = S._looks_chinese(c["text"])
        if got != c["expect"]:
            bad.append("%s | 判 %s(期望%s) %r" % (c["id"], got, c["expect"], c["text"][:30]))
    return len(cases) - len(bad), len(cases), bad


def _city_mention(cases):
    """城市匹配：同形词（三明治/长白山/开封一下）不该被抽成城市"""
    from app.services.session_slots import _find_cities

    bad = []
    for c in cases:
        got = _find_cities(c["text"])
        if got != c["cities"]:
            bad.append("%s | 抽出 %s(期望%s) %s" % (c["id"], got, c["cities"], c["text"]))
    return len(cases) - len(bad), len(cases), bad


async def _ask(svc, query):
    """调一次 RAG。优先走流式接口（能顺带量首字延迟），没有就退回同步版。"""
    t0 = time.time()
    stream = getattr(svc, "rag_summary_stream", None)
    if stream is not None:
        pieces, ttft = [], None
        async for piece in stream(query):
            if ttft is None:
                ttft = time.time() - t0
            pieces.append(piece)
        total = time.time() - t0
        text = "".join(pieces)
        return text, ttft if ttft is not None else total, total
    out = svc.rag_summary(query)               # 有可能是协程（改造后是排空包装）
    if inspect.isawaitable(out):
        out = await out
    total = time.time() - t0
    return out, total, total                   # 非流式：首字 == 全部


async def _e2e(cases):
    """端到端：真调模型。断言"有内容 + 是中文 + 没回兜底话术 + 命中要求词"，并记录延迟"""
    from app.agent.travel.services import rag_service as RS

    # 改造前后都能跑：常量化是后加的，没有就退回字面量
    NOT_FOUND = getattr(RS, "NOT_FOUND_TEXT", "抱歉，暂时没找到相关资料")
    svc = RS.RagSummaryService()
    bad, ttfts, totals, disclaimers = [], [], [], 0
    for c in cases:
        try:
            text, ttft, total = await _ask(svc, c["query"])
        except Exception as e:
            bad.append("%s | 异常 %s: %s" % (c["id"], type(e).__name__, e))
            continue
        ttfts.append(ttft)
        totals.append(total)
        han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fa5")
        if "以景区/官方最新公布为准" in text or "以景区官方最新公布为准" in text:
            disclaimers += 1
        why = []
        if not text.strip():
            why.append("空回答")
        if han < 4:
            why.append("汉字过少(%d)" % han)
        if text.strip() == NOT_FOUND.strip() or "抱歉，暂时没找到相关资料" in text:
            why.append("回了兜底话术")
        for f in c.get("forbid", []):
            if f in text:
                why.append("出现禁用词 %r" % f)
        need = c.get("require_any") or []
        if need and not any(k in text for k in need):
            why.append("要求词一个都没命中 %s" % need)
        if why:
            bad.append("%s | %s | %r" % (c["id"], "；".join(why), text[:60]))
    return len(cases) - len(bad), len(cases), bad, ttfts, totals, disclaimers


def _stat(xs):
    if not xs:
        return "-", "-"
    return "%.1fs" % statistics.median(xs), "%.1fs" % max(xs)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=GROUPS, help="只跑这一组")
    ap.add_argument("--skip", choices=GROUPS, action="append", default=[], help="跳过某一组（可重复）")
    args = ap.parse_args()

    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    todo = [g for g in GROUPS if (not args.only or g == args.only) and g not in args.skip]

    print("=" * 74)
    print("RAG 评估 —— %s" % CASES_PATH.name)
    print("=" * 74)

    passed = total = 0
    offline_fail, e2e_extra = [], None
    for g in todo:
        cases = data.get(g) or []
        if not cases:
            continue
        if g == "retrieval":
            ok, n, bad = _retrieval(cases)
        elif g == "numbers":
            ok, n, bad = _numbers(cases)
        elif g == "chinese":
            ok, n, bad = _chinese(cases)
        elif g == "city_mention":
            ok, n, bad = _city_mention(cases)
        else:
            ok, n, bad, ttfts, totals, dis = await _e2e(cases)
            e2e_extra = (ttfts, totals, dis)
        passed += ok
        total += n
        print("[%-12s] %3d 条  通过 %3d  %6.1f%%" % (g, n, ok, 100.0 * ok / max(n, 1)))
        if bad:
            offline_fail += ["[%s] %s" % (g, b) for b in bad]

    print("-" * 74)
    print("合计 %d/%d = %.1f%%" % (passed, total, 100.0 * passed / max(total, 1)))
    if e2e_extra:
        ttfts, totals, dis = e2e_extra
        tt, tm = _stat(ttfts)
        ct, cm = _stat(totals)
        print("首字延迟  中位 %s / 最长 %s" % (tt, tm))
        print("完成耗时  中位 %s / 最长 %s" % (ct, cm))
        print("追加更正  %d/%d" % (dis, len(totals)))

    if offline_fail:
        print()
        print("--- 失败明细（%d 条）---" % len(offline_fail))
        for b in offline_fail:
            print("  " + b)


asyncio.run(main())
