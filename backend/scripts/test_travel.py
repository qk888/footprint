# -*- coding: utf-8 -*-
"""travel 直达通道测试 + 计时(对比旧 tool calling 30秒)"""
import os, sys, asyncio, time, logging
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))
logging.basicConfig(level=logging.WARNING)

from app.config.db_config import async_session_factory
from app.crud.cities import CitiesRepository
from app.crud.trips import TripsRepository
from app.crud.bills import BillsRepository
from app.crud.notes import NotesRepository
from app.crud.memories import MemoryRepository
from app.crud.notes import NotesRepository
from app.crud.plans import PlanRepository
from app.services.cities import CitiesService
from app.services.trips import TripsService
from app.services.bills import BillsService
from app.services.memories import MemoryService
from app.services.plans import PlanService
from app.agent.footprint_agent import FootprintAgent

USER_ID = 1


def H(*pairs):
    return [{"role": r, "content": c} for r, c in zip(pairs[0::2], pairs[1::2])]


async def turn(agent, q, hist=None):
    t0 = time.time()
    ans = ""
    async for c in agent.execute_stream(q, hist or []):
        ans += c
    dt = time.time() - t0
    show = ans if len(ans) <= 220 else ans[:220] + " ...(截断)"
    print(f"Q: {q}\nA({dt:.2f}秒): {show}\n")


async def budget_followup(services):
    """行程花费追问：必须走直达通道，不能落到 operation 子 agent

    实测原状：用户看完卡片问"花费的有点啥少了"，被判成 operation →
    1.5B 走 tool calling 空转 → 吐出"要记账/点亮城市请告诉我"的固定话术（答非所问）；
    "这个行程花了多少钱"还会被账单通道接走，答成"你还没有记账记录"。
    """
    slots = {"target_city": "重庆", "days": 2, "budget": 4000, "people": 2,
             "last_travel_mode": "plan", "last_plan_cost": 1538,
             "last_plan_days": 2, "last_plan_people": 2}
    agent = FootprintAgent(session_slots=slots, session_id="test-budget-followup", **services)

    print("=" * 62)
    print("行程花费追问（有行程上下文时）")
    print("=" * 62)
    ok = True
    for q in ("花费的有点啥少了", "这个行程花了多少钱", "预算用完了吗"):
        hit = agent._looks_like_plan_cost_query(q)
        ans = ""
        if hit:
            async for c in agent.execute_stream(q, []):
                ans += c
        good = hit and "¥" in ans and "记账" not in ans and "点亮城市" not in ans
        ok &= good
        print("  %-5s %-16s 命中=%-6s → %s" % ("OK" if good else "FAIL", q, hit,
                                              ans[:70].replace("\n", " ")))
    for q in ("我花了多少钱", "查账", "记账 我在重庆吃饭花了50"):
        hit = agent._looks_like_plan_cost_query(q)
        ok &= not hit
        print("  %-5s %-16s 命中=%-6s（反例：应让给账单/记账通道）"
              % ("OK" if not hit else "FAIL", q, hit))
    return ok


async def save_plan_followup(services):
    """把行程加入规划：必须真落库，不能只回"请点按钮"，更不能又排一张新卡片

    实测原状：'给我这个加入规划中且更新' 一个保存关键词都不命中（旧表里只有"保存行程/写进/录进…"）
    → 被判成 travel（含"规划"）→ 走规划支 → **又吐一张新行程卡片**：用户要保存现在这张，
    拿到的是另一张，而且新卡片还把他看中的那版覆盖了。
    """
    from app.services.session_slots import load_last_plan

    ok = True
    print("=" * 62)
    print("保存行程直达（把这张加入规划）")
    print("=" * 62)
    CASES = [("给我这个加入规划中且更新", True), ("把这份行程加入规划", True), ("更新我的规划", True),
             ("帮我存到规划里", True), ("帮我规划重庆两日游", False), ("改成成都", False),
             ("重新规划一下", False), ("再加一天", False)]
    for q, want in CASES:
        got = FootprintAgent._looks_like_save_plan_query(q)
        ok &= got == want
        print("  %-5s %-22s 判定=%-6s 期望=%s" % ("OK" if got == want else "FAIL", q, got, want))

    sid = "test-save-plan"
    agent = FootprintAgent(session_slots={"target_city": "重庆", "days": 2, "budget": 4000,
                                        "people": 2, "cities": ["重庆"]},
                         session_id=sid, **services)
    first = ""
    async for c in agent.execute_stream("重庆有什么好玩的推荐预算4000两个人", []):
        first += c
    has_card = "⟦PLAN⟧" in first
    ok &= has_card
    print("  %-5s 第1轮排出行程（含卡片=%s）" % ("OK" if has_card else "FAIL", has_card))

    ans = ""
    async for c in agent.execute_stream("给我这个加入规划中且更新", []):
        ans += c
    good = "⟦PLAN⟧" not in ans and "规划" in ans
    ok &= good
    print("  %-5s 第2轮 → %s" % ("OK" if good else "FAIL", ans[:80].replace("\n", " ")))

    payload = await load_last_plan(USER_ID, sid)
    rec = await services["plan_service"].get_plan(USER_ID, str(payload.get("adcode") or ""))
    content = getattr(rec, "content", "") or (rec.get("content") if isinstance(rec, dict) else "")
    stored = bool((content or "").strip())
    ok &= stored
    print("  %-5s 落库：adcode=%s 共 %d 字，首行=%s"
          % ("OK" if stored else "FAIL", payload.get("adcode"), len(content or ""),
             (content or "").splitlines()[0][:46] if stored else "-"))

    empty = FootprintAgent(session_slots={}, session_id="test-save-plan-empty", **services)
    ans2 = ""
    async for c in empty.execute_stream("给我这个加入规划中且更新", []):
        ans2 += c
    good2 = "⟦PLAN⟧" not in ans2 and ("没有" in ans2 or "先让我排" in ans2)
    ok &= good2
    print("  %-5s 没排过行程就说保存 → %s" % ("OK" if good2 else "FAIL", ans2[:60].replace("\n", " ")))

    # ── 新建 / 覆盖：同城现在允许多份，说"新建一份"必须真的多一份 ──
    print("  " + "-" * 56)
    print("  新建 vs 覆盖（同一个城市可以并存多份）")
    adcode = str(payload.get("adcode") or "")
    for q, want_delta in [("把这份行程新建一份规划", 1), ("把这份行程加入规划并更新", 0)]:
        a = FootprintAgent(session_slots={"target_city": "重庆", "days": 2, "budget": 4000,
                                        "people": 2, "cities": ["重庆"]},
                         session_id="test-save-mode-" + str(want_delta), **services)
        async for _c in a.execute_stream("帮我规划重庆两日游", []):
            pass
        n0 = await services["plan_service"].count_plans(USER_ID, adcode)
        out = ""
        async for c in a.execute_stream(q, []):
            out += c
        n1 = await services["plan_service"].count_plans(USER_ID, adcode)
        good3 = (n1 - n0) == want_delta and "⟦PLAN⟧" not in out
        ok &= good3
        print("  %-5s %-22s 份数 %d→%d（期望 +%d）→ %s"
              % ("OK" if good3 else "FAIL", q, n0, n1, want_delta, out[:52].replace("\n", " ")))
    return ok


async def main():
    s = async_session_factory()
    try:
        cr, tr, br, nr = CitiesRepository(session=s), TripsRepository(session=s), BillsRepository(session=s), NotesRepository(session=s)
        ms = MemoryService(repo=MemoryRepository(session=s))
        cs = CitiesService(cr, tr, br, nr)
        ts = TripsService(tr, br, nr)
        bs = BillsService(br, tr)
        mb = await ms.prompt_block(USER_ID)
        services = dict(city_service=cs, trip_service=ts, bill_service=bs,
                        memory_service=ms, memory_block=mb, user_id=USER_ID,
                        plan_service=PlanService(repo=PlanRepository(session=s)))
        agent = FootprintAgent(**services)

        await turn(agent, "重庆有什么好玩的")              # RAG攻略
        await turn(agent, "帮我规划重庆两日游")            # 行程规划
        await turn(agent, "帮我规划一下行程")              # 缺城市→追问
        await turn(agent, "帮我规划3天，预算2000",
                   H("user", "我想去杭州", "assistant", "好的，杭州是个好地方"))  # 多轮补全

        await budget_followup(services)
        await save_plan_followup(services)
    finally:
        await s.close()


if __name__ == "__main__":
    asyncio.run(main())
