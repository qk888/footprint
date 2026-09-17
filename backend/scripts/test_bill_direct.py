# -*- coding: utf-8 -*-
"""测试记账直达通道"""
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
from app.agent.footprint_agent import FootprintAgent

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

        agent = FootprintAgent(
            city_service=city_service, trip_service=trip_service,
            bill_service=bill_service, memory_service=memory_service,
            memory_block=memory_block, user_id=USER_ID,
        )

        tests = [
            "在重庆吃饭花了50，打车30",
            "我在北京住宿花了300",
        ]
        for q in tests:
            print(f"\n{'='*50}")
            print(f"Q: {q}")
            async for chunk in agent.execute_stream(q):
                print(f"A: {chunk}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
