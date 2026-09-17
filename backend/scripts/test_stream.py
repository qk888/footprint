# -*- coding: utf-8 -*-
"""流式输出 + 联网空缓存 自检。

用法（在 backend 容器里跑）：
    docker exec footprint-backend python scripts/test_stream.py

查五件事：
  0) 攻略/规划的路由判定（确定性，不调模型）
  1) 攻略支走 `execute_stream` 时**是不是真分多块吐**（改造前恒为 1 块）
  2) 每个 query 的**首字延迟**（用户实际感知的等待）与完成耗时
  3) 行程规划支没被流式改坏（卡片必须完整、带 ⟦PLAN⟧ 标记）
  4) 联网搜索的**空结果短缓存**（TTL 应 ≈300 秒，不是 3600）

注意路由语义（`_travel_is_guide_only`）：**有攻略词且没有规划词**才走攻略支，
否则一律走规划。所以"重庆三日游攻略"因为含"三日"其实走的是规划 —— 这是既有语义，
不是 bug（别照着直觉改测试用例）。
"""
import asyncio
import hashlib
import logging
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))
logging.basicConfig(level=logging.WARNING)

from app.agent.footprint_agent import FootprintAgent
from app.agent.travel.services.web_search import _get_cache, get_web_search
from app.config.db_config import async_session_factory
from app.crud.bills import BillsRepository
from app.crud.cities import CitiesRepository
from app.crud.memories import MemoryRepository
from app.crud.notes import NotesRepository
from app.crud.trips import TripsRepository
from app.services.bills import BillsService
from app.services.cities import CitiesService
from app.services.memories import MemoryService
from app.services.session_slots import save_slots, update_session_slots
from app.services.trips import TripsService

USER_ID = 1
SID = "smoke-stream-session"

# (query, 期望走攻略支?)
ROUTE_CASES = [
    ("重庆有什么好玩的", True),
    ("重庆美食推荐", True),
    ("洪崖洞门票多少", True),
    ("厦门鼓浪屿怎么去", True),
    ("重庆三日游攻略", False),      # 含"三日" → 规划词优先级更高
    ("我要去重庆玩三天", False),    # 没有攻略词 → 规划
    ("帮我规划重庆两日游", False),
]

# (query, 说明) —— 都必须走攻略支（含攻略词、不含规划词）
GUIDE_CASES = [
    ("重庆有什么好玩的", "本地命中"),
    ("厦门鼓浪屿怎么去", "本地命中"),
    ("洪崖洞门票多少", "本地+联网"),
]
PLAN_CASES = [("我要去重庆玩三天", "行程规划")]
REVISE_CASE = ("改成成都", "改攻略(revise)", {"target_city": "重庆", "last_travel_mode": "guide"})


def _new_agent(cs, ts, bs, ms, memory_block, slots):
    return FootprintAgent(
        city_service=cs, trip_service=ts, bill_service=bs,
        memory_service=ms, memory_block=memory_block,
        session_slots=slots, session_id=SID, user_id=USER_ID,
    )


async def run_turn(agent, query, history=None):
    """排空 execute_stream，顺便量首字延迟与分块数（分块数 > 1 才说明是真流式）"""
    t0 = time.time()
    ttft, chunks, text = None, 0, []
    async for piece in agent.execute_stream(query, history or []):
        if ttft is None:
            ttft = time.time() - t0
        chunks += 1
        text.append(piece)
    return "".join(text), (ttft if ttft is not None else time.time() - t0), time.time() - t0, chunks


async def main():
    ok_all = True
    session = async_session_factory()
    try:
        cr, tr, br, nr = (CitiesRepository(session=session), TripsRepository(session=session),
                          BillsRepository(session=session), NotesRepository(session=session))
        cs, ts, bs = CitiesService(cr, tr, br, nr), TripsService(tr, br, nr), BillsService(br, tr)
        ms = MemoryService(repo=MemoryRepository(session=session))
        memory_block = await ms.prompt_block(USER_ID)

        print("=" * 74)
        print("0) 攻略/规划路由判定（确定性）")
        print("=" * 74)
        for q, want_guide in ROUTE_CASES:
            got = FootprintAgent._travel_is_guide_only(q)
            good = got == want_guide
            ok_all &= good
            print("  %-5s %-22s 判定=%-6s 期望=%s"
                  % ("OK" if good else "FAIL", q, "攻略" if got else "规划",
                     "攻略" if want_guide else "规划"))

        print()
        print("=" * 74)
        print("1) 攻略支：是否真流式 + 首字延迟")
        print("=" * 74)
        for q, note in GUIDE_CASES:
            await save_slots(USER_ID, SID, {})          # 干净槽位，别把上一个 case 的城市带进来
            slots = await update_session_slots(USER_ID, SID, q, [])
            agent = _new_agent(cs, ts, bs, ms, memory_block, slots)
            text, ttft, total, chunks = await run_turn(agent, q)
            good = bool(text.strip()) and chunks > 1     # 分块 > 1 才算真流式
            ok_all &= good
            print("  %-5s %-12s 首字 %5.2fs  完成 %5.2fs  分块 %3d  %3d字  | %s"
                  % ("OK" if good else "FAIL", note, ttft, total, chunks, len(text),
                     text[:32].replace("\n", " ")))

        q, note, seed = REVISE_CASE
        await save_slots(USER_ID, SID, seed)
        slots = await update_session_slots(USER_ID, SID, q, [])
        agent = _new_agent(cs, ts, bs, ms, memory_block, slots)
        text, ttft, total, chunks = await run_turn(agent, q)
        good = bool(text.strip()) and "抱歉，暂时没找到" not in text
        ok_all &= good
        print("  %-5s %-12s 首字 %5.2fs  完成 %5.2fs  分块 %3d  %3d字  | %s"
              % ("OK" if good else "FAIL", note, ttft, total, chunks, len(text),
                 text[:32].replace("\n", " ")))

        print()
        print("=" * 74)
        print("2) 行程规划支：卡片必须完整（不能被流式改坏）")
        print("=" * 74)
        for q, note in PLAN_CASES:
            await save_slots(USER_ID, SID, {})
            slots = await update_session_slots(USER_ID, SID, q, [])
            agent = _new_agent(cs, ts, bs, ms, memory_block, slots)
            text, ttft, total, chunks = await run_turn(agent, q)
            has_card = "⟦PLAN⟧" in text and "⟦/PLAN⟧" in text
            ok_all &= has_card
            print("  %-5s %-12s 首字 %5.2fs  完成 %5.2fs  分块 %3d  %3d字  卡片=%s"
                  % ("OK" if has_card else "FAIL", note, ttft, total, chunks, len(text), has_card))

        print()
        print("=" * 74)
        print("3) 联网空结果短缓存（伪造空结果，不依赖上游返回空）")
        print("=" * 74)
        ws = get_web_search()
        cache = _get_cache()

        def _key(q):
            return "web_search:" + hashlib.md5(q.encode("utf-8")).hexdigest()

        real_fetch, calls = ws._fetch, {"n": 0}

        def fake_fetch(q):
            calls["n"] += 1
            return []

        ws._fetch = fake_fetch
        q_empty = "smoke-empty-" + str(int(time.time()))
        a1 = ws.search(q_empty)
        a2 = ws.search(q_empty)
        ttl_empty = cache.ttl(_key(q_empty)) if cache is not None else -1
        ws._fetch = real_fetch
        good = (a1 == [] and a2 == [] and calls["n"] == 1 and 0 < ttl_empty <= 300)
        ok_all &= good
        print("  %-5s 空结果: 第1次调用上游 %d 次 / 第2次走缓存 / TTL=%s 秒（期望 ≤300）"
              % ("OK" if good else "FAIL", calls["n"], ttl_empty))

        q_hit = "北京故宫开放时间"
        b = ws.search(q_hit)
        ttl_hit = cache.ttl(_key(q_hit)) if cache is not None else -1
        good2 = bool(b) and 0 < ttl_hit <= 3600
        ok_all &= good2
        print("  %-5s 有结果: %d 条 / TTL=%s 秒（期望 ≤3600；空结果才压到 300）"
              % ("OK" if good2 else "FAIL", len(b), ttl_hit))

        print()
        print("=" * 74)
        print("4) 走 HTTP 的真实链路（StreamingResponse 有没有逐块 flush 到客户端）")
        print("=" * 74)
        import requests

        from app.core.token import AuthHandler

        # 用项目自己的签发接口造一个本地测试令牌（只在本机测，不落盘不打印）。
        # 注意 encode_login_token 返回的是 dict{access_token, refresh_token}，取 access 那一份
        token = AuthHandler().encode_login_token(USER_ID)["access_token"]
        q = "重庆有什么好玩的"
        t0 = time.time()
        ttfb, chunks, text, status = None, 0, [], 0
        with requests.post(
            "http://127.0.0.1:8000/agent/chat",
            json={"query": q, "session_id": SID, "history": []},
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=120,
        ) as resp:
            status = resp.status_code
            for piece in resp.iter_content(chunk_size=None):
                if not piece:
                    continue
                if ttfb is None:
                    ttfb = time.time() - t0
                chunks += 1
                text.append(piece.decode("utf-8", "replace"))
        body = "".join(text)
        # 分块 > 1 才说明 HTTP 层也是逐块推的（否则前端拿到的还是一次性整段）
        good = status == 200 and chunks > 1 and bool(body.strip())
        ok_all &= good
        print("  %-5s HTTP %s  首字节 %s  分块 %d  %d字  | %s"
              % ("OK" if good else "FAIL", status, ("%.2fs" % ttfb) if ttfb else "-",
                 chunks, len(body), body[:32].replace("\n", " ")))

        print()
        print("=" * 74)
        print("自检结果：%s" % ("全部通过" if ok_all else "有失败项，看上面 FAIL"))
        print("=" * 74)
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
