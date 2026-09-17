from app.crud.bills import BillsRepository
from app.crud.trips import TripsRepository
from decimal import Decimal
from fastapi import HTTPException
from app.services.memories import category_label, city_name, remember_event

def check_category(category: int, custom_category: str | None):
    if category == 6 and not custom_category:
        raise HTTPException(status_code=400, detail="其他分类必须填写自定义分类")
    if category != 6 and custom_category:
        raise HTTPException(status_code=400, detail="非其他分类不能填写自定义分类")

class BillsService:
    def __init__(self, repo: BillsRepository, trip_repo: TripsRepository):
        self.repo = repo
        self.trip_repo = trip_repo

    #添加账单
    async def add_bill(self, user_id: int, trip_id: int, amount: Decimal, category: int, custom_category: str | None = None):

        trip = await self.trip_repo.get_trip(
            user_id=user_id,
            trip_id=trip_id
        )
        if not trip:
            raise HTTPException(status_code=400, detail="旅行不存在")

        check_category(category, custom_category)

        bill = await self.repo.add_bill(
            trip_id = trip_id, 
            amount = amount,
            category = category,
            custom_category = custom_category
        )
        #同一城市同金额同分类的重复消费会合并成一条记忆, 避免提示词被流水账塞满
        await remember_event(
            self.repo.session,
            user_id,
            "bill",
            f"在{city_name(trip.adcode)}花了{amount}元（{category_label(category, custom_category)}）",
        )
        return bill
    
    #获取旅行账单
    async def get_trip_bills(self, user_id: int, trip_id: int):
        trip = await self.trip_repo.get_trip(
            user_id=user_id,
            trip_id=trip_id
        )
        if not trip:
            raise HTTPException(status_code=400, detail="旅行不存在")
        return await self.repo.get_trip_bills(trip_id)

    #用户全部消费的汇总(跨城市、跨行程), 按分类聚合 —— 给"我花了多少钱 / 查账"这类查询用。
    #不限城市: 用户问"一共花了多少"要的就是全部, 而不是某个城市的最新一次。
    async def summary_all(self, user_id: int):
        """返回 (分类汇总 dict, 总计, 账单条数)"""
        trips = await self.trip_repo.get_trips(user_id)
        summary: dict[str, float] = {}
        total = 0.0
        count = 0
        for trip in trips:
            for bill in await self.repo.get_trip_bills(trip.id):
                label = category_label(bill.category, bill.custom_category)
                amount = float(bill.amount)
                summary[label] = summary.get(label, 0.0) + amount
                total += amount
                count += 1
        return summary, total, count
    
    #获取单个账单
    async def get_bill(self, user_id: int, bill_id: int):
        bill = await self.repo.get_bill(bill_id)
        if not bill:
            raise HTTPException(status_code=400, detail="账单不存在")
        trip = await self.trip_repo.get_trip(
            user_id=user_id,
            trip_id=bill.trip_id
        )
        if not trip:
            raise HTTPException(status_code=400, detail="旅行不存在")
        return bill
    
    #删除账单
    async def delete_bill(self, user_id: int, bill_id: int):
        bill = await self.repo.get_bill(bill_id)
        if not bill:
            raise HTTPException(status_code=400, detail="账单不存在")
        trip = await self.trip_repo.get_trip(
            user_id=user_id,
            trip_id=bill.trip_id
        )
        if not trip:
            raise HTTPException(status_code=400, detail="旅行不存在")
        return await self.repo.delete_bill(bill_id)
    
    #删除旅行账单
    async def delete_trip_bill(self, user_id: int, trip_id: int):
        trip = await self.trip_repo.get_trip(
            user_id=user_id,
            trip_id=trip_id
        )
        if not trip:
            raise HTTPException(status_code=400, detail="旅行不存在")
        result = await self.repo.delete_trip_bill(trip_id)
        if not result:
            raise HTTPException(status_code=400, detail="该旅行没有账单")
        return result
    
    #修改账单
    async def update_bill(self, user_id: int, bill_id: int, amount: Decimal, category: int, custom_category: str | None = None):
        bill = await self.repo.get_bill(bill_id)
        if not bill:
            raise HTTPException(status_code=400, detail="账单不存在")
        trip = await self.trip_repo.get_trip(
            user_id=user_id,
            trip_id=bill.trip_id
        )
        if not trip:
            raise HTTPException(status_code=400, detail="旅行不存在")

        check_category(category, custom_category)
    
        return await self.repo.update_bill(
            bill_id = bill_id, 
            amount = amount,
            category = category,
            custom_category = custom_category
        )
