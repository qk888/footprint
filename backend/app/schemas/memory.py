from datetime import datetime
from typing import List

from pydantic import BaseModel, Field


class AddMemoryInput(BaseModel):
    #记忆内容(一句人话)
    content: str = Field(min_length=1, max_length=500, description="记忆内容")

    #记忆类别, 默认手动记录
    kind: str = Field(default="manual", max_length=32, description="记忆类别")


class MemoryOut(BaseModel):
    id: int
    kind: str
    content: str
    source: str
    update_time: datetime
    # active=生效中, superseded=已被新说法取代(保留历史)
    status: str = "active"
    # True=模型自己记的候选, 用户在「我的」页确认后才会自动注入
    needs_review: bool = False

    class Config:
        from_attributes = True


class MemoriesOut(BaseModel):
    memories: List[MemoryOut]


class MemoryStatsOut(BaseModel):
    #生效中的记忆条数
    active: int
    #语义检索是否已启用(生效条数过了阈值才会启用)
    vector_enabled: bool
    #启用阈值
    vector_min_count: int
    #已经进了向量索引的条数
    vector_indexed: int
