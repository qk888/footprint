# -*- coding: utf-8 -*-
"""点亮城市直达通道测试 + 计时(应秒回, 不再走30秒tool calling重试)"""
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
from app.services.cities import CitiesService
from app.services.trips import TripsService
from app.services.bills import BillsService
from app.services.memories import MemoryService
from app.agent.footprint_agent import FootprintAgent

USER_ID = 1


async def turn(agent, q):
    t0 = time.time()
    ans = ""
    async for c in agent.execute_stream(q, []):
        ans += c
    dt = time.time() - t0
    print(f"Q: {q}")
    print(f"A({dt:.2f}秒): {ans}\n")
    return dt


async def main():
    s = async_session_factory()
    try:
        cr, tr, br, nr = CitiesRepository(session=s), TripsRepository(session=s), BillsRepository(session=s), NotesRepository(session=s)
        ms = MemoryService(repo=MemoryRepository(session=s))
        cs = CitiesService(cr, tr, br, nr)
        ts = TripsService(tr, br, nr)
        bs = BillsService(br, tr)
        mb = await ms.prompt_block(USER_ID)
        agent = FootprintAgent(city_service=cs, trip_service=ts, bill_service=bs,
                             memory_service=ms, memory_block=mb, user_id=USER_ID)

        await turn(agent, "点亮城市成都")          # 新点亮
        await turn(agent, "我去过西安")            # 口语化点亮
        await turn(agent, "点亮城市成都")          # 重复点亮
        await turn(agent, "我去过武汉和长沙")       # 一次多个
        await turn(agent, "我去过哪些城市")         # 查询
    finally:
        await s.close()


if __name__ == "__main__":
    asyncio.run(main())
