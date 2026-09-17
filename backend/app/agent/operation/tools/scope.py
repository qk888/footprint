from langchain_core.tools import tool
from app.agent.middleware import OUT_OF_SCOPE_MSG
from app.agent.utils.logger_handler import logger


#范围逃生口: 误路由到操作助手时, 让模型有个"这题不归我管"的选项。
#返回的哨兵在 FAIL_TEXTS 里, harness 会摘掉 operation_agent 换旅行/闲聊助手重答。
@tool(
    description="当用户这句话不是让你点亮城市、记账、查账单、记住或回忆偏好时使用，"
    "例如让你规划行程、推荐景点、闲聊。调用它之后不要再调用任何其他工具。",
)
async def out_of_scope() -> str:
    logger.info("[out_of_scope] 输入不属于操作助手范围, 交回 harness")
    return OUT_OF_SCOPE_MSG
