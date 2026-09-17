"""
演示数据种子脚本（绕过邮箱验证码，方便本地直接登录测试）

用法（容器内）:
    docker compose exec backend python scripts/seed_demo.py
用法（本机原生，已配好 .env）:
    python scripts/seed_demo.py

幂等：重复执行不会产生重复数据。
创建内容：
    - 用户 demo@footprint.dev / demo1234
    - 点亮城市：北京(110000)、上海(310000)、杭州(330100)
    - 一次杭州行程 + 若干账单 + 一条笔记
    - 杭州预算 + 一条杭州计划
"""
import sys, os, io, asyncio
from decimal import Decimal

# 让脚本无论从哪运行都能 import app.*（容器内 WORKDIR=/app）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Windows 控制台 UTF-8 输出
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

from sqlalchemy import select
from app.config.db_config import async_session_factory
from app.models import User, LightCity, Trip, Bill, Note, Budget, Plan

DEMO_EMAIL = "demo@footprint.dev"
# EmailStr 把 .local 当保留域拒收, 老库里的旧演示账号改名复用, 不新建第二个用户
LEGACY_EMAILS = ("demo@footprint.local",)
DEMO_PASSWORD = "demo1234"
DEMO_USERNAME = "演示用户"

CITIES = [("北京", "110000"), ("上海", "310000"), ("杭州", "330100")]


async def seed():
    async with async_session_factory() as session:
        # ---- 用户 ----
        user = (await session.execute(
            select(User).where(User.email == DEMO_EMAIL)
        )).scalar_one_or_none()
        if user is None:
            user = (await session.execute(
                select(User).where(User.email.in_(LEGACY_EMAILS))
            )).scalars().first()
            if user is not None:
                user.email = DEMO_EMAIL
                await session.flush()
                print(f"[seed] 旧演示邮箱不合法, 改名 -> {DEMO_EMAIL} (id={user.id})")
        if user is None:
            user = User(email=DEMO_EMAIL, username=DEMO_USERNAME, password=DEMO_PASSWORD)
            session.add(user)
            await session.flush()  # 拿到 user.id
            print(f"[seed] 创建用户 {DEMO_EMAIL} / {DEMO_PASSWORD} (id={user.id})")
        else:
            print(f"[seed] 用户已存在 (id={user.id})，跳过")

        # ---- 点亮城市 ----
        for name, adcode in CITIES:
            exists = (await session.execute(
                select(LightCity).where(
                    LightCity.user_id == user.id, LightCity.adcode == adcode
                )
            )).scalar_one_or_none()
            if exists is None:
                session.add(LightCity(user_id=user.id, adcode=adcode))
                print(f"[seed] 点亮城市 {name}({adcode})")
        await session.flush()

        # ---- 行程（杭州）----
        hz_adcode = "330100"
        trip = (await session.execute(
            select(Trip).where(Trip.user_id == user.id, Trip.adcode == hz_adcode)
        )).scalars().first()
        if trip is None:
            trip = Trip(user_id=user.id, adcode=hz_adcode)
            session.add(trip)
            await session.flush()
            print(f"[seed] 创建杭州行程 (id={trip.id})")

            # 账单：1交通 2餐饮 3购物 4住宿 5娱乐 6其他
            bills = [
                Bill(trip_id=trip.id, category=1, amount=Decimal("263.50")),  # 交通
                Bill(trip_id=trip.id, category=2, amount=Decimal("188.00")),  # 餐饮
                Bill(trip_id=trip.id, category=4, amount=Decimal("699.00")),  # 住宿
                Bill(trip_id=trip.id, category=5, amount=Decimal("120.00")),  # 娱乐
                Bill(trip_id=trip.id, category=6, custom_category="伴手礼",
                     amount=Decimal("96.00")),                                # 其他
            ]
            session.add_all(bills)
            print(f"[seed] 写入 {len(bills)} 条账单")

            session.add(Note(trip_id=trip.id, content="西湖断桥、灵隐寺、河坊街，记得带伞。"))
            print("[seed] 写入 1 条笔记")
        else:
            print(f"[seed] 杭州行程已存在 (id={trip.id})，跳过账单/笔记")

        # ---- 预算（杭州，分类示例）----
        for category, amount in [(1, Decimal("300")), (2, Decimal("400")),
                                 (4, Decimal("800"))]:
            exists = (await session.execute(
                select(Budget).where(
                    Budget.user_id == user.id, Budget.adcode == hz_adcode,
                    Budget.category == category
                )
            )).scalar_one_or_none()
            if exists is None:
                session.add(Budget(user_id=user.id, adcode=hz_adcode,
                                   category=category, amount=amount))
        print("[seed] 预算已对齐")

        # ---- 计划（杭州，唯一）----
        plan = (await session.execute(
            select(Plan).where(Plan.user_id == user.id, Plan.adcode == hz_adcode)
        )).scalar_one_or_none()
        if plan is None:
            session.add(Plan(user_id=user.id, adcode=hz_adcode,
                             content="Day1 西湖环湖；Day2 灵隐寺+龙井；Day3 河坊街购物。"))
            print("[seed] 写入杭州计划")

        await session.commit()
        print("[seed] 完成。可用 demo@footprint.dev / demo1234 登录。")


if __name__ == "__main__":
    asyncio.run(seed())
