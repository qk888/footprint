import asyncio
from langchain.agents.middleware import wrap_tool_call, before_model, wrap_model_call
from langchain.agents.middleware.types import ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage, AIMessage, HumanMessage
from langgraph.types import Command
from typing import Callable
from app.agent.utils.logger_handler import logger
from app.agent.utils.state_utils import last_user_query
from app.agent.constraints import (
    WRITE_RULES, WRITE_DENIED_MSG, MAX_WRITES_PER_TURN,
    write_intent_ok, note_write, classify_outcome, write_count,
)
from langchain.agents import AgentState
from langgraph.runtime import Runtime


#强制路由层必须调用工具(只看意图, 不许直接回答)。
#微调模型在 --reasoning off 下对模糊问候(如"你好")会随机输出路由名裸文本
#而不是发起工具调用, tool_choice=required 强制模型每次必选一个子助手。temp配0.1。
#max_tokens=256: supervisor 只做路由选择, 空转时别打满1024 token(实测空转率~40%,
#每次12秒, 截断后空转变成廉价失败, 重试更快)。
@wrap_model_call
async def force_tool_choice(request, handler):
    request.tool_choice = "required"
    request.max_tokens = 256
    return await handler(request)


#子agent首轮必须真的调起工具: 小模型对 tool_choice=required 只是"概率性听话"(实测约4成概率
#无视, 直接复读成一大段垃圾文本)。所以首轮强制 + 重试, 重试耗尽就回失败哨兵,
#让 harness 摘掉这个子助手重开, 绝不把复读文本当答案。
SUBAGENT_NO_TOOL_MSG = "助手暂时没响应，请稍后再试"
#重试时的纠正信号: 原样重_roll 命中率不变, 把空转回复喂回去+纠正一句, 小模型吃这套
NO_TOOL_NUDGE = "上一条回复不合格：你没有调用任何工具。现在必须调用一个工具，禁止直接回答。"


@wrap_model_call
async def force_first_tool_call(request, handler):
    if any(isinstance(m, ToolMessage) for m in request.messages):
        return await handler(request)
    request.tool_choice = "required"
    msgs = list(request.messages)
    response = None
    for attempt in range(3):
        response = await handler(request)
        msg = response.result[0] if response.result else None
        if msg is not None and getattr(msg, "tool_calls", None):
            return response
        logger.warning(f"[force_first_tool_call] 第{attempt + 1}轮没调起工具, 带纠正重试")
        # 把这次空转的回复和纠正提示拼进下一轮上下文
        msgs = msgs + [msg, HumanMessage(content=NO_TOOL_NUDGE)]
        request = request.override(messages=msgs)
    logger.warning("[force_first_tool_call] 重试耗尽, 回失败哨兵")
    return ModelResponse(result=[AIMessage(content=SUBAGENT_NO_TOOL_MSG)])


# ===== harness: 子agent结果验收 =====
#工具用这两个标记回传结果, harness 据此决定 直出/重启
ANSWER_PREFIX = "__ANSWER__::"
FAIL_PREFIX = "__FAIL__::"
#所有子助手都失败后的固定话术
GIVE_UP_MSG = "抱歉，没太理解你的意思。你是想规划旅行行程，还是记一笔账或查账单？"


@wrap_model_call
async def harness_route(request, handler):
    """子agent结果验收层(harness核心), 在模型开口前拦截:
    - 成功(__ANSWER__) → 短路直出, 不给小模型转述原答案的机会
    - 失败(__FAIL__) → 不喂回给模型(实测 0.8B 没训过折返对话, 会复读成裸文本),
      把失败标记原样抛出, 由外层 execute_stream 摘掉失败子助手、重开一轮全新对话
    """
    msgs = request.messages
    last = msgs[-1] if msgs else None
    if isinstance(last, ToolMessage) and isinstance(last.content, str):
        # 成功: 短路, 答案原样给用户
        if last.content.startswith(ANSWER_PREFIX):
            answer = last.content[len(ANSWER_PREFIX):]
            logger.info(f"[harness] 验收通过, 短路直出 ({len(answer)}字)")
            # 打标: 外层只认带标记的答案, supervisor 自由发挥的裸文本会被丢弃
            return ModelResponse(result=[AIMessage(content=answer, additional_kwargs={"harness_direct": True})])
        # 失败: 原样抛给外层重启
        if last.content.startswith(FAIL_PREFIX):
            _, agent_name, reason = last.content.split("::", 2)
            logger.warning(f"[harness] {agent_name} 失败({reason}), 交给外层重启")
            return ModelResponse(result=[AIMessage(content=last.content)])
    return await handler(request)

# ===== 子agent侧: 范围逃生口 + 串行执行 =====
#误路由时的"这题不归我管"出口。没有它, 被 force_first_tool_call 逼着必须调工具的小模型
#会硬挑一个业务工具顶上(实测把"重庆旅游两日计划"点成了 light_city 张家界 + add_bill 重庆)
OUT_OF_SCOPE_MSG = "这个问题不归我管"

#收口信号：出现任一个就中止这个子agent、交回外层换人（裸哨兵，答案不给用户看）
ABORT_TEXTS = (OUT_OF_SCOPE_MSG, WRITE_DENIED_MSG)


@wrap_model_call
async def scope_guard(request, handler):
    """看到 out_of_scope / 写操作被否决 的工具结果就立刻收口, 不给模型自由发挥道歉文本的机会。
    抛出的裸哨兵在 FAIL_TEXTS 里, 外层 harness 会摘掉这个子助手、换人重开一轮。"""
    # 只看最后一条 AIMessage 之后的工具结果(同一轮可能有好几个)
    tail = []
    for m in reversed(request.messages):
        if not isinstance(m, ToolMessage):
            break
        tail.append(m)
    if any(isinstance(m.content, str) and m.content in ABORT_TEXTS for m in tail):
        logger.info("[scope_guard] 命中收口信号(out_of_scope/写操作被否决), 交回 harness 换人")
        return ModelResponse(result=[AIMessage(content=OUT_OF_SCOPE_MSG)])
    return await handler(request)


# ===== harness 约束层: 写操作闸门（框架级，不依赖每个工具自觉）=====
#修这个 bug 时定的规矩：query="4" 被判成 operation → force_first_tool_call 逼着"首轮必须调工具"
#→ 模型没内容可调就从上下文里编出 light_city(['北京','上海']) 真去写库。
#当时的修法是①把 light_city 从模型手里摘掉 ②在每个写工具里各加一遍意图核对 —— 
#但那是"逐个补漏"：以后再挂一个写工具，忘了加护栏就再出一次。
#现在收敛成一道闸门 + 一份登记表（见 constraints.WRITE_RULES）：
#  L1 授权：用户原话没有对应意图 → 直接否决，工具体根本不执行
#  L2 取证：执行结果记进本轮写入台账，供出话术前的话术校验核对
#另外加了单轮写入次数上限，防模型在强制 tool_choice 下反复重试刷库。
@wrap_tool_call
async def guard_write_tools(request, handler):
    name = request.tool_call.get("name", "")
    if name not in WRITE_RULES:                      # 读工具不受约束
        return await handler(request)

    # 授权依据取 state 里的**最后一条用户消息**（权威来源）：contextvar 只保证向下传给
    # 子任务，工具跑在 LangGraph 的任务里时未必拿得到 —— 拿不到就等于这道闸门没生效。
    # 原话为空（批处理脚本直接调工具）时不拦，保持兼容。
    query = None
    try:
        query = last_user_query(request.state) or None
    except Exception as e:                           # state 结构变了也不能因此放行写操作
        logger.warning(f"[harness/guard] 取用户原话失败，退回 contextvar: {e}")

    allowed, reason = write_intent_ok(name, query)   # L1 授权
    if allowed and write_count() >= MAX_WRITES_PER_TURN:
        allowed, reason = False, f"单轮写入超过 {MAX_WRITES_PER_TURN} 次"
    if not allowed:
        logger.warning(f"[harness/guard] 否决写工具 {name}：{reason}")
        note_write(name, "denied", reason)           # L2 留痕（被否决也记，排查时能看到）
        # 回哨兵而不是业务错误：scope_guard 会收口、外层摘掉这个子助手换人，
        # 不给模型"那我换个城市再点一次"的发挥空间
        return ToolMessage(content=WRITE_DENIED_MSG, tool_call_id=request.tool_call["id"])

    result = await handler(request)
    content = getattr(result, "content", "")
    note_write(name, classify_outcome(content if isinstance(content, str) else str(content)))   # L2 取证
    return result


def serialize_tool_calls(veto_tool: str | None = None):
    """每个 agent 实例一把锁, 把同一轮里的多个工具调用排成串行。

    小模型偶尔在一条消息里塞好几个 tool_call, LangGraph 默认并发执行;
    而一个请求只有一个 AsyncSession, 同时只能开一个事务,
    并发就炸 "A transaction is already begun on this Session."。

    veto_tool: 同一批里出现这个工具时, 其余工具一律不执行(防误路由真写库),
    统一回哨兵文本, 交给 scope_guard 收口。
    """
    lock = asyncio.Lock()                                   # 锁跟着 agent 实例走, 不跨请求互相拖累

    @wrap_tool_call
    async def serialize_tools(request, handler):
        if veto_tool and request.tool_call["name"] != veto_tool:
            state = request.state
            msgs = state.get("messages", []) if isinstance(state, dict) else []
            siblings = (getattr(msgs[-1], "tool_calls", None) or []) if msgs else []
            if any(c.get("name") == veto_tool for c in siblings):
                logger.warning(f"[serialize_tools] 同批已有 {veto_tool}, 跳过 {request.tool_call['name']}")
                return ToolMessage(content=OUT_OF_SCOPE_MSG, tool_call_id=request.tool_call["id"])
        async with lock:
            return await handler(request)

    return serialize_tools


#工具执行的监控
@wrap_tool_call
async def monitor_tool(
    #请求的数据封装(用户输入的参数)
    request: ToolCallRequest,
    #执行的函数本身(函数)
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:

    logger.info(f"[tool_monitor] 执行工具: {request.tool_call["name"]}")
    logger.info(f"[tool_monitor] 传入参数: {request.tool_call["args"]}")
    try:
        result = await handler(request)
        logger.info(f"[tool_monitor] 工具{request.tool_call["name"]}调用成功")
        return result
    except Exception as e:
        logger.error(f"[tool_monitor] 工具执行异常: 原因: {str(e)}")
        raise e


#在模型执行前输出日志
@before_model
def log_before_model(
    #整个agent中的状态记录
    state: AgentState,
    #记录了整个执行过程中的上下文信息
    runtime: Runtime,
):
    logger.info(f"[log_before_model]即将调用模型, 带有{len(state["messages"])}条信息")
    logger.debug(f"[log_before_model] {type(state["messages"][-1]).__name__}: {state["messages"][-1].content.strip()}")

    return None
