"""旅游 agent：行程规划 + RAG 攻略"""
from langchain.agents import create_agent
from app.agent.model.factory import chat_model
from app.agent.middleware import (
    log_before_model, monitor_tool, force_first_tool_call, serialize_tool_calls,
    guard_write_tools,
)
from app.agent.utils.prompt_loader import load_prompt
from app.agent.travel.tools.travel_plan import travel_plan
from app.agent.travel.tools.rag import rag_summary


def build_travel_agent(memory_block: str = ""):
    return create_agent(
        model=chat_model,
        tools=[travel_plan, rag_summary],
        system_prompt=load_prompt("travel") + memory_block,
        # travel_plan 要读数据库, 同一轮多个工具调用必须串行, 否则会撞同一个 AsyncSession
        # harness 写操作闸门也挂上: 现在这两个工具都是只读的, 但闸门是**按工具名查登记表**
        # （见 constraints.WRITE_RULES）—— 以后往这里加写工具会自动被约束, 不用记得补护栏。
        middleware=[log_before_model, force_first_tool_call, guard_write_tools,
                    serialize_tool_calls(), monitor_tool],
    )
