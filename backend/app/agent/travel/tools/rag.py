from typing import Annotated
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from app.agent.travel.services.rag_service import RagSummaryService
from app.agent.utils.state_utils import last_user_query

rag = RagSummaryService()

# rag检索工具：原话从 state 透传，避免模型转述失真（与 travel_plan 一致）
@tool(
    description="回答旅行攻略类问题：某地有什么景点、美食推荐、避雷、注意事项等。用户只是问信息、没有要求规划行程时调用。",
    return_direct=True
)
async def rag_summary(state: Annotated[dict, InjectedState()]) -> str:
    # 取**最后一条**用户消息而不是 messages[0] —— 子 agent 的 state 是「历史 + 当前问题」，
    # messages[0] 会是历史里最早那条（多轮下等于拿旧问题去检索）
    query = last_user_query(state)
    # rag_summary 内部自己把检索/联网这些阻塞活丢线程池，且它是 async 的 ——
    # 这里直接 await，不要再包 to_thread（会多一层没用的线程切换）
    # 只查本地攻略知识库+联网兜底; miss 返回失败话术 → harness 黑名单 → 摘掉 travel_agent 重开一轮,
    # 非旅行问题(如"刘备是三国的人物吗")自然进不了 rag, 会折返给 chat
    return await rag.rag_summary(query)
