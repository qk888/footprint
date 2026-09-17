from fastapi import APIRouter, Depends
from app.schemas import ResponseOut
from app.config.db_config import AsyncSession
from app.core.dependencies import get_session
from app.core.token import AuthHandler
from app.crud.memories import MemoryRepository
from app.services.memories import MemoryService
from app.schemas.memory import AddMemoryInput, MemoriesOut, MemoryStatsOut

router = APIRouter(prefix="/memory", tags=["memory"])


#获取记忆列表
@router.get("", response_model=MemoriesOut)
async def list_memories(
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(AuthHandler().auth_access_denpendency),
):
    service = MemoryService(repo=MemoryRepository(session=session))
    result = await service.list_memories(user_id)
    return MemoriesOut(memories=result)


#记忆体检: 条数 + 语义检索是否启用(要写在 /{memory_id} 之前, 否则 stats 会被当成 id)
@router.get("/stats", response_model=MemoryStatsOut)
async def memory_stats(
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(AuthHandler().auth_access_denpendency),
):
    service = MemoryService(repo=MemoryRepository(session=session))
    return MemoryStatsOut(**(await service.stats(user_id)))


#重建记忆的向量索引(换向量模型 / 索引损坏 / 上线前批量灌历史记忆时手动跑一次)
@router.post("/reindex", response_model=ResponseOut)
async def reindex_memories(
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(AuthHandler().auth_access_denpendency),
):
    service = MemoryService(repo=MemoryRepository(session=session))
    await service.rebuild_index(user_id)
    return ResponseOut()


#新增记忆
@router.post("/add", response_model=ResponseOut)
async def add_memory(
    data: AddMemoryInput,
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(AuthHandler().auth_access_denpendency),
):
    service = MemoryService(repo=MemoryRepository(session=session))
    await service.remember(user_id=user_id, content=data.content, kind=data.kind)
    return ResponseOut()


#清空全部记忆(必须写在 /{memory_id} 前面, 否则 all 会被当成 id)
@router.delete("/all", response_model=ResponseOut)
async def delete_all_memories(
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(AuthHandler().auth_access_denpendency),
):
    service = MemoryService(repo=MemoryRepository(session=session))
    await service.delete_all(user_id)
    return ResponseOut()


#确认一条待确认的记忆(AI 自己记下的候选, 确认后才会自动带进提示词)
@router.post("/{memory_id}/confirm", response_model=ResponseOut)
async def confirm_memory(
    memory_id: int,
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(AuthHandler().auth_access_denpendency),
):
    service = MemoryService(repo=MemoryRepository(session=session))
    await service.confirm_memory(user_id=user_id, memory_id=memory_id)
    return ResponseOut()


#删除单条记忆
@router.delete("/{memory_id}", response_model=ResponseOut)
async def delete_memory(
    memory_id: int,
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(AuthHandler().auth_access_denpendency),
):
    service = MemoryService(repo=MemoryRepository(session=session))
    await service.delete_memory(user_id=user_id, memory_id=memory_id)
    return ResponseOut()
