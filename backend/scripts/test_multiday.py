# -*- coding: utf-8 -*-
"""验证同城多日: 重庆两日游应排2天"""
import os, sys, asyncio, json, logging
sys.path.insert(0, "/app")
logging.basicConfig(level=logging.WARNING)
from app.agent.travel.services.travel_plan_tool import solve, plan_to_frontend


async def main():
    for q in ["帮我规划重庆两日游", "重庆三日游预算3000", "成都一日游"]:
        plan = await solve(q)
        p = plan_to_frontend(plan)
        if p:
            print(f"{q} => {p['days']}天, {len(p['itinerary'])}个日块, 总花费{p['total_cost']}, 活动数{sum(len(d['activities']) for d in p['itinerary'])}")
        else:
            print(f"{q} => 空, warning={plan.get('warning')}")


asyncio.run(main())
