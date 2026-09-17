"""从 LangGraph state 里取"当前用户问题"

子 agent 的工具（travel_plan / rag_summary）都要用**用户原话**，不能用模型转述 ——
转述会把地名、天数这些关键信息丢掉。

原来这些工具写的是 `msgs[0].content`，那是**最早那条历史消息**，不是当前问题：
`_run_subagent` 交给子 agent 的 messages 是 `history + [当前问题]`，历史非空时
`msgs[0]` 就是旧的（甚至可能是 assistant 的回复）。第 5 轮问"重庆有什么好玩的"，
工具却拿第 1 轮的"成都三日游"去检索/规划。

所以统一走 `last_user_query()`：从后往前找最后一条 user 消息，兼容 dict 和
LangChain BaseMessage 两种形态（state 里两种都会出现）。
"""
from typing import Any

_USER_ROLES = ("user", "human")


def last_user_query(state: dict | None) -> str:
    """state['messages'] 里最后一条用户消息的文本；没有 user 消息就退回最后一条，都没有则 "" """
    msgs = (state or {}).get("messages") or []
    for m in reversed(msgs):
        role, content = _role_content(m)
        if role in _USER_ROLES:
            text = _as_text(content)
            if text:
                return text
    # 没有任何 user 消息（工具被直接调用）→ 退回最后一条，避免取到空串
    return _as_text(_role_content(msgs[-1])[1]) if msgs else ""


def _role_content(m: Any) -> tuple[Any, Any]:
    if isinstance(m, dict):
        return m.get("role"), m.get("content")
    return getattr(m, "type", None), getattr(m, "content", "")


def _as_text(content: Any) -> str:
    """多模态 content 是分块列表（[{'type':'text','text':...}]），这里拼成纯文本"""
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return content.strip() if isinstance(content, str) else ""
