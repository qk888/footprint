from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SetPlanInput(BaseModel):
    adcode: str = Field(min_length=6, max_length=20, description="城市编码")
    content: str = Field(default="", max_length=10000, description="计划内容")
    # 保存方式：overwrite=覆盖该城市已有规划（默认，兼容老前端）；new=另存为新的一份
    mode: Literal["overwrite", "new"] = Field("overwrite", description="覆盖还是新建")
    # 覆盖时的目标行：规划页在编辑"第 N 份"时必须带，否则会覆盖到最新那份上
    plan_id: int | None = Field(None, description="覆盖指定规划（可选）")


class PlanOut(BaseModel):
    id: int
    adcode: str
    content: str
    update_time: datetime

    class Config:
        from_attributes = True
