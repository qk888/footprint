from pydantic import BaseModel, Field


class HistoryMsg(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")  # 谁说的: 用户/AI
    content: str = Field(..., min_length=1)               # 说了什么


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    # 最近几轮对话历史(不含当前query), 让AI能联系上下文; 老前端不传也兼容, 默认空
    history: list[HistoryMsg] = Field(default_factory=list)
    # 前端会话 id: 后端按它维护"会话级结构化槽位"(Redis), 兜住历史被截断后丢失的城市/天数/预算
    session_id: str | None = Field(default=None, max_length=64)
