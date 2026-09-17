from app.crud.plans import PlanRepository
from app.services.memories import city_name, remember_event
from fastapi import HTTPException


class PlanService:
    def __init__(self, repo: PlanRepository):
        self.repo = repo    # plans表的数据库操作

    #新增或更新计划
    #mode="overwrite"（默认）覆盖该城市已有规划，没有就新建；"new" 总是新建一份
    #（同一城市可以有多份 —— 用户要的是"保存到规划可以新建，也可以覆盖原规划"）
    async def set_plan(self, user_id: int, adcode: str, content: str,
                       mode: str = "overwrite", plan_id: int | None = None):
        result = await self.repo.set_plan(user_id=user_id, adcode=adcode, content=content,
                                          mode=mode, plan_id=plan_id)
        #说法固定不含正文: 反复保存只会刷新同一条记忆
        await remember_event(self.repo.session, user_id, "plan", f"为{city_name(adcode)}制定了行程")
        return result

    #获取某城市计划（最新一份）
    async def get_plan(self, user_id: int, adcode: str):
        return await self.repo.get_plan(user_id, adcode)

    #某城市的规划份数
    async def count_plans(self, user_id: int, adcode: str) -> int:
        return await self.repo.count_plans(user_id, adcode)

    #获取用户所有计划
    async def get_plans(self, user_id: int):
        return await self.repo.get_plans(user_id)

    #删除计划
    async def delete_plan(self, user_id: int, plan_id: int):
        result = await self.repo.delete_plan(user_id, plan_id)
        if not result:
            raise HTTPException(status_code=400, detail="计划不存在")
        return result
