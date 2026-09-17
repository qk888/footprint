# -*- coding: utf-8 -*-
"""快速验证 supervisor 纯文本分类 + 记账"""
import os, sys, asyncio, logging
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

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
from app.agent.footprint_agent import FootprintAgent, classify_intent

USER_ID = 1

async def main():
    session = async_session_factory()
    try:
        cities_repo = CitiesRepository(session=session)
        trips_repo = TripsRepository(session=session)
        bills_repo = BillsRepository(session=session)
        notes_repo = NotesRepository(session=session)
        memory_service = MemoryService(repo=MemoryRepository(session=session))
        city_service = CitiesService(cities_repo, trips_repo, bills_repo, notes_repo)
        trip_service = TripsService(trips_repo, bills_repo, notes_repo)
        bill_service = BillsService(bills_repo, trips_repo)
        memory_block = await memory_service.prompt_block(USER_ID)

        # 先测分类
        print("=== 意图分类测试 ===")
        test_queries = [
            "在重庆吃饭花了50，打车30",
            "我在北京住宿花了300",
            "帮我规划北京到杭州三日游",
            "你好",
            "广州有什么好玩的",
            "点亮城市上海",
        ]
        for q in test_queries:
            intent = await classify_intent(q)
            print(f"  {q[:30]:30s} → {intent}")

        # 再测完整记账
        print("\n=== 完整记账测试 ===")
        agent = FootprintAgent(
            city_service=city_service, trip_service=trip_service,
            bill_service=bill_service, memory_service=memory_service,
            memory_block=memory_block, user_id=USER_ID,
        )
        async for chunk in agent.execute_stream("在重庆吃饭花了50，打车30"):
            print(f"  回复: {chunk}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
