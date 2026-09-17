"""操作 agent：点亮城市 + 记账 + 查账单 + 长期记忆"""
from langchain.agents import create_agent
from app.agent.model.factory import chat_model
from app.agent.middleware import (
    log_before_model, monitor_tool, force_first_tool_call,
    scope_guard, serialize_tool_calls, guard_write_tools,
)
from app.agent.utils.prompt_loader import load_prompt
from app.agent.operation.tools.cities import city_tools
from app.agent.operation.tools.bills import bill_tools
from app.agent.operation.tools.smart_bill import smart_add_bill_tool
from app.agent.operation.tools.memory import memory_tools
from app.agent.operation.tools.scope import out_of_scope


def build_operation_agent(city_service, trip_service, bill_service, user_id, memory_service=None, memory_block: str = ""):
    # ⚠️ light_city 是**写操作**，故意不给模型：实测（query="4"）被判成 operation 后，
    # `force_first_tool_call` 逼着模型"首轮必须调工具"，它没内容可调就**从上下文/记忆里
    # 编了 light_city(['北京','上海']) 直接写库** —— 用户根本没提点亮城市。
    # 点亮已经有确定性直达通道（关键词 + 城市表匹配），不需要模型参与；这里只留查询工具。
    city = [t for t in city_tools(city_service, user_id)
            if getattr(t, "name", "") != "light_city"]
    get_bills = bill_tools(bill_service, trip_service, user_id)
    add_bill = smart_add_bill_tool(city_service, trip_service, bill_service, user_id)
    # 没传记忆服务(批处理脚本)就不挂记忆工具, 免得给模型一个用不了的工具
    memory = memory_tools(memory_service, user_id) if memory_service else []
    return create_agent(
        model=chat_model,
        # out_of_scope 放最后: 被强制调工具时, 模型先看见业务工具, 不会图省事选无参数的 out_of_scope
        tools=[*city, add_bill, get_bills, *memory, out_of_scope],
        system_prompt=load_prompt("operation") + memory_block,
        middleware=[
            log_before_model, scope_guard, force_first_tool_call,
            # harness 写操作闸门: 授权(对不上用户原话就否决) + 取证(记本轮写入台账)。
            # 放在 serialize_tool_calls 之前 —— 否决掉的调用不必去抢串行锁。
            guard_write_tools,
            # 这些工具共用请求级 AsyncSession, 必须串行执行;
            # 同一批里出现 out_of_scope 时, 其余工具直接否决, 不让误路由变成真写库
            serialize_tool_calls(veto_tool=out_of_scope.name), monitor_tool,
        ],
    )
