from langchain_core.tools import tool
from app.services.memories import MemoryService
from app.agent.utils.logger_handler import logger
from app.agent.constraints import (
    CURRENT_USER_QUERY, PREFERENCE_HINTS, write_intent_ok, WRITE_DENIED_MSG,
)
from app.agent.operation.schemas.memory import SaveMemoryInput, RecallMemoryInput

#记忆工具: 把用户偏好存进长期记忆, 按关键词捞回来
def memory_tools(service: MemoryService, user_id: int):
    @tool(
        args_schema=SaveMemoryInput,
        # 不加 return_direct: 存完让模型用自然语气确认一句
        description="当用户说出希望以后被记住的个人偏好、习惯或事实时使用，例如：我不吃辣、我住在广州、我预算不多。",
    )
    async def save_memory(content: str) -> str:
        content = (content or "").strip()
        if not content:
            return "没有需要记住的内容"
        # 授权护栏（harness 约束层的规则，闸门已拦一道，这里是防御纵深）：
        # 用户原话里必须真有"记住X"或偏好陈述，否则模型可能凭空写一条记忆。
        allowed, why = write_intent_ok("save_memory")
        if not allowed:
            logger.warning(f"[save_memory] 拒绝执行：{why}")
            return WRITE_DENIED_MSG
        # 防改写/幻觉护栏：偏好类记忆必须来自用户原话，模型只该摘录不该创作。
        # 实测"我讨厌排队"被写成 content="我不喜欢吃排队"（凭空多一个"吃"），
        # 而记忆是长期反复引用的，改写比存不上更糟 —— 不在原话里就拽回原话。
        original = (CURRENT_USER_QUERY.get() or "").strip()
        if original and content not in original and any(k in original for k in PREFERENCE_HINTS):
            logger.warning(f"记忆内容疑似被模型改写: 模型给[{content}] → 改用原话[{original}]")
            content = original
        logger.info(f"用户{user_id}请求记住: {content}")
        try:
            await service.remember(user_id=user_id, content=content, kind="chat", source="chat")
            return f"已记住：{content}"
        except Exception as e:
            logger.error(f"用户{user_id}请求记住失败: {e}", exc_info=True)
            return "记忆保存失败"

    @tool(
        args_schema=RecallMemoryInput,
        description="当需要回忆用户以前告诉过你的偏好或事实时使用，keyword 填要回忆的关键词。",
    )
    async def recall_memory(keyword: str) -> str:
        keyword = (keyword or "").strip()
        if not keyword:
            return "没有提供回忆关键词"
        logger.info(f"用户{user_id}请求回忆: {keyword}")
        try:
            memories = await service.recall(user_id=user_id, keyword=keyword)
            if not memories:
                return f"没有与{keyword}相关的记忆"
            return f"关于{keyword}的记忆：" + "；".join(m.content for m in memories)
        except Exception as e:
            logger.error(f"用户{user_id}请求回忆失败: {e}", exc_info=True)
            return "回忆记忆失败"

    return [save_memory, recall_memory]
