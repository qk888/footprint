# -*- coding: utf-8 -*-
"""槽位提取健壮性验证: 多种说法 x 2遍"""
import os, sys, asyncio, logging
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))
logging.basicConfig(level=logging.ERROR)
from app.agent.travel.services.slot_filler import get_slot_filler

QS = [
    "北京到上海三日游预算3000",
    "帮我规划重庆两日游",
    "我想去杭州。帮我规划3天，预算2000",
    "从成都出发去西安玩5天大概4000元两个人",
    "广州旅游",
]


async def main():
    sf = get_slot_filler()
    for q in QS:
        for _ in range(2):
            c = await sf.fill_slots(q)
            print(f"{q} => 出发{c['start_city']} 目的{c['target_city']} "
                  f"{c['days']}天 预算{c['budget']} {c['people_number']}人")


if __name__ == "__main__":
    asyncio.run(main())
