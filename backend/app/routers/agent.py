from app.core.token import AuthHandler
from fastapi import APIRouter, Depends
from app.config.db_config import AsyncSession
from app.core.dependencies import get_session
from app.crud.cities import CitiesRepository
from app.services.cities import CitiesService
from app.agent.footprint_agent import FootprintAgent
from app.schemas.agent import ChatRequest
from fastapi.responses import StreamingResponse
from app.crud.trips import TripsRepository
from app.services.trips import TripsService
from app.crud.bills import BillsRepository
from app.services.bills import BillsService
from app.crud.notes import NotesRepository
from app.crud.memories import MemoryRepository
from app.crud.plans import PlanRepository
from app.services.memories import MemoryService
from app.services.plans import PlanService
from app.services.session_slots import update_session_slots, render_slots_block

router = APIRouter(prefix="/agent", tags=["agent"])
@router.post("/chat")
async def chat(
    request: ChatRequest,
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(AuthHandler().auth_access_denpendency)
):
    
    cities_repo = CitiesRepository(session=session)
    trips_repo = TripsRepository(session=session)
    bills_repo = BillsRepository(session=session)
    notes_repo = NotesRepository(session=session)
    city_service = CitiesService(cities_repo, trips_repo, bills_repo, notes_repo)
    trip_service = TripsService(trips_repo, bills_repo, notes_repo)
    bill_service = BillsService(bills_repo, trips_repo)
    memory_service = MemoryService(repo=MemoryRepository(session=session))

    # 记忆块在构造子agent时就拼进系统提示词了, 所以每次请求都要重新取一遍;
    # 带上当前这句话(而不是全量记忆), 让 prompt_block 按意图/城市做规则过滤再注入
    memory_block = await memory_service.prompt_block(user_id, query=request.query)

    # 会话级结构化槽位(城市/天数/预算/人数): 规则抽取后存 Redis,
    # 这样"20 条之前的约定"不会因为历史窗口截断而丢掉
    session_slots: dict = {}
    session_block = ""
    if request.session_id:
        session_slots = await update_session_slots(
            user_id, request.session_id, request.query, request.history
        )
        session_block = render_slots_block(session_slots)

    agent = FootprintAgent(
        city_service = city_service,
        trip_service = trip_service,
        bill_service = bill_service,
        memory_service = memory_service,
        # 用户在对话里说"把这张行程加入规划"时要真写进 plans 表
        # （与前端「保存到规划」按钮同一个 PlanService.set_plan）
        plan_service = PlanService(repo=PlanRepository(session=session)),
        # 两块都是"追加进子agent系统提示词"的纯文本, 直接拼一起, 不用改各子 agent 的构造
        memory_block = memory_block + session_block,
        # 直达通道(规划/记账)要的是结构化槽位本身
        session_slots = session_slots,
        # 槽位所在的会话 key: agent 要把"上次走的是攻略还是规划"写回槽位
        session_id = request.session_id,
        user_id = user_id
    )

    return StreamingResponse(
        agent.execute_stream(request.query, request.history),
        media_type="text/plain"
)