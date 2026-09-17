# -*- coding: utf-8 -*-
"""验证: 1)行程payload带city/adcode 2)保存类请求被拦截不杜撰"""
import sys, asyncio, logging
sys.path.insert(0, "/app")
logging.basicConfig(level=logging.ERROR)
from app.agent.travel.services.travel_plan_tool import solve, plan_to_frontend
from app.agent.footprint_agent import FootprintAgent


async def collect(agent, query, history=None):
    chunks = []
    async for c in agent.execute_stream(query, history or []):
        chunks.append(c)
    return "".join(chunks)


async def main():
    # 1. 行程带城市
    plan = await solve("帮我规划重庆两日游")
    p = plan_to_frontend(plan)
    print(f"[行程] days={p['days']} city={p.get('city')} adcode={p.get('adcode')}")

    # 2. 保存拦截
    agent = FootprintAgent(1, None, None, None)
    for q in ["写进记录里面", "保存行程", "帮我存一下这个规划"]:
        out = await collect(agent, q, [{"role": "user", "content": "帮我规划重庆两日游"}])
        print(f"[保存拦截] {q} => {out[:60]}")
    # 注：agent 没有 close()（原先这里调用它，脚本结尾必然抛 AttributeError）。
    # 这个脚本没开 session，检查跑完就结束，不需要收尾。


asyncio.run(main())
