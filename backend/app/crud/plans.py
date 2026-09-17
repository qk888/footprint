#导入会话工厂
from app.config.db_config import AsyncSession
#获取操作数据库的方法
from sqlalchemy import select, and_, func
#导入计划模型
from app.models.plans import Plan


class PlanRepository:
    #效果同操作用户数据库
    def __init__(self, session: AsyncSession):
        self.session = session

    #同一城市可能有多份 → "最新一份"必须带 id 兜底排序
    #（同一秒写入的多条 update_time 完全相同，只按时间排结果不确定）
    @staticmethod
    def _latest_stmt(user_id: int, adcode: str):
        return (select(Plan)
                .where(and_(Plan.user_id == user_id, Plan.adcode == adcode))
                .order_by(Plan.update_time.desc(), Plan.id.desc())
                .limit(1))

    #新增或更新计划
    #mode="overwrite": 覆盖该城市**最新的一份**（没有就新建）—— 默认，保持老行为
    #mode="new": 总是新建一份（同一城市的"五一版""国庆版"能并存）
    #plan_id: 覆盖时指定目标（规划页在编辑"第 N 份"时必须带，否则会改错行）
    async def set_plan(self, user_id: int, adcode: str, content: str,
                       mode: str = "overwrite", plan_id: int | None = None):
        async with self.session.begin():
            if mode != "new":
                target = None
                if plan_id:
                    target = await self.session.scalar(
                        select(Plan).where(and_(Plan.id == plan_id, Plan.user_id == user_id))
                    )
                if target is None:
                    target = await self.session.scalar(self._latest_stmt(user_id, adcode))
                if target is not None:
                    target.content = content
                    return target
            plan = Plan(user_id=user_id, adcode=adcode, content=content)
            self.session.add(plan)
            return plan

    #获取某城市计划（最新一份）
    async def get_plan(self, user_id: int, adcode: str):
        async with self.session.begin():
            return await self.session.scalar(self._latest_stmt(user_id, adcode))

    #某城市的规划份数（同城可以有多份）
    async def count_plans(self, user_id: int, adcode: str) -> int:
        async with self.session.begin():
            stmt = select(func.count()).select_from(Plan).where(
                and_(Plan.user_id == user_id, Plan.adcode == adcode)
            )
            return int(await self.session.scalar(stmt) or 0)

    #获取用户所有计划
    async def get_plans(self, user_id: int):
        async with self.session.begin():
            stmt = (select(Plan).where(Plan.user_id == user_id)
                    .order_by(Plan.update_time.desc(), Plan.id.desc()))
            result = await self.session.scalars(stmt)
            return result.all()

    #删除计划
    async def delete_plan(self, user_id: int, plan_id: int):
        async with self.session.begin():
            stmt = select(Plan).where(
                and_(Plan.id == plan_id, Plan.user_id == user_id)
            )
            plan = await self.session.scalar(stmt)
            if plan:
                await self.session.delete(plan)
                return True
            return False
