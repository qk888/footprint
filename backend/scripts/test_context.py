# -*- coding: utf-8 -*-
"""多轮上下文聊天测试：验证子agent能看到历史、记账能从历史补城市"""
import os, sys, asyncio, logging
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))

logging.basicConfig(level=logging.WARNING)  # 只看warning, 输出干净

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


def H(*pairs):
    """把 (role, content) 两两拼成 history"""
    return [{"role": r, "content": c} for r, c in zip(pairs[0::2], pairs[1::2])]


async def run_turn(agent, query, history, label):
    print("=" * 50)
    print(f"【{label}】")
    print(f"历史: {history if history else '(无)'}")
    print(f"当前: {query}")
    answer = ""
    async for chunk in agent.execute_stream(query, history):
        answer += chunk
    print(f"AI回复: {answer}\n")
    return answer


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

        # 场景1: 记账从历史补全城市（当前句没说城市）
        await run_turn(
            agent, "吃饭花了60",
            H("user", "我现在在杭州", "assistant", "好的，你在杭州玩得开心！"),
            "场景1 记账补城市",
        )

        # 场景2: 闲聊能记住上文说过的名字
        await run_turn(
            agent, "我叫什么名字？",
            H("user", "我叫小明", "assistant", "你好小明，很高兴认识你！"),
            "场景2 闲聊记忆",
        )

        # 场景3: 指代"这里"结合上文城市
        await run_turn(
            agent, "这里有什么好玩的？",
            H("user", "我现在到成都了", "assistant", "成都可是个好地方！"),
            "场景3 指代理解",
        )
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
