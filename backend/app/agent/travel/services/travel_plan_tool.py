"""
旅行规划 — 小模型槽位填充 → 规划引擎搜索 → JSON/API 数据源
搜索内核: planner_engine.py, 依据 docs/planner_engine_spec.md 独立实现
数据层: 本地 JSON 优先 → 高德/12306/RollingGo API 兜底
"""

import pandas as pd
from app.agent.utils.logger_handler import logger
from app.agent.travel.services.planner_engine import Planner, sleep_ok
from app.core.constants import CITY_ADCODE_MAP
# 省名归一已下沉到 core/city_match（槽位填充也要用，而它反过来被本模块导入，
# 放在这里会循环导入）。这里保留同名导入，老的调用点不用改。
from app.core.city_match import PROVINCE_CAPITAL, normalize_city, alias_notes

# ═══════════════ 数据加载 ═══════════════

# 花费低于预算这个比例时，卡片上补一句"钱没花到"的说明
# （实测"重庆有什么好玩的推荐预算4000两个人"只花到 1296 —— 不提用户就会追问）
BUDGET_SPENT_RATIO = 0.7


# ---- 行程 → 可读文本（格式与前端 ItineraryCard 的 planToText 对齐）----
# 前端「保存到规划」按钮写进 plans 表的就是这段文本（规划页按城市存文本计划）。
# 后端也要能生成它：用户在对话里直接说"把这张加入规划"时，得由后端自己落库，
# 不能只回一句"请点卡片上的按钮"（用户会觉得没理他）。
_TYPE_LABELS = {
    "train": "火车", "airplane": "飞机", "intercity": "跨城",
    "breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐",
    "attraction": "游览", "accommodation": "入住", "free": "自由活动",
}


def _money(v) -> str:
    """0 → 免费；整数 → ¥73；小数 → ¥73.5（与前端 utils/plan.ts 的 money() 一致）"""
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n <= 0:
        return "免费"
    return f"¥{n:.2f}".rstrip("0").rstrip(".")


def plan_to_text(payload: dict) -> str:
    """把前端卡片数据渲染成规划页里存的那段文本"""
    city = payload.get("city") or payload.get("target_city") or ""
    lines = [f"{city} {payload.get('days')}天{payload.get('people')}人行程，"
             f"总花费约{_money(payload.get('total_cost'))}"]
    for day in payload.get("itinerary") or []:
        lines.append(f"第{day.get('day')}天：")
        for a in day.get("activities") or []:
            start = a.get("start_time") or ""
            if a.get("start") and a.get("end"):        # 跨城段用"车次 出发→到达"
                name = (f"{a['vehicle']} {a['start']}→{a['end']}" if a.get("vehicle")
                        else f"{a['start']}→{a['end']}")
            else:
                name = a.get("position") or ""
            label = _TYPE_LABELS.get(str(a.get("type")), str(a.get("type") or ""))
            lines.append(f"  {start + ' ' if start else ''}{label} {name}（{_money(a.get('cost'))}）")
    if payload.get("warning"):
        lines.append(f"提示：{payload['warning']}")
    return "\n".join(lines)

def _fix_hotel_coords(hotels, attractions, restaurants):
    """酒店缺坐标时用同城 POI 质心兜住（返回 (酒店表, 是否是估算房源)）

    为什么必须兜：RollingGo 一挂就返回占位酒店，坐标 (0,0) 落在几内亚湾，
    与真实景点相距上万公里 → 引擎算"景点↔酒店"市内交通要几万分钟 →
    每个组合都失败 → 用户看到的是"无法规划"，完全看不出真实原因是第三方接口挂了
    （实测 2026-09-16 上游 503 时，所有城市都排不出）。
    """
    if hotels is None or hotels.empty:
        return hotels, False
    estimated = bool(hotels.get("estimated", pd.Series(dtype=bool)).fillna(False).any())
    bad = hotels[(hotels["lat"].fillna(0) == 0) | (hotels["lon"].fillna(0) == 0)]
    if bad.empty:
        return hotels, estimated
    src = [d for d in (attractions, restaurants)
           if d is not None and not d.empty and {"lat", "lon"} <= set(d.columns)]
    if not src:
        return hotels, estimated
    pts = pd.concat(src, ignore_index=True)
    pts = pts[(pts["lat"].fillna(0) != 0) & (pts["lon"].fillna(0) != 0)]
    if pts.empty:
        return hotels, estimated
    lat, lon = float(pts["lat"].mean()), float(pts["lon"].mean())
    hotels = hotels.copy()
    # 兜底房源的坐标是整数 0 → 列可能是 int dtype，直接写 float 会 LossySetitemError
    hotels["lat"] = hotels["lat"].astype(float)
    hotels["lon"] = hotels["lon"].astype(float)
    hotels.loc[bad.index, "lat"] = lat
    hotels.loc[bad.index, "lon"] = lon
    logger.warning(f"[酒店] {len(bad)} 家酒店缺坐标(数据源异常)，已用同城 POI 质心({lat:.3f},{lon:.3f})兜住")
    return hotels, estimated


#加载规划所需数据(景点/餐厅/酒店/城际交通), 供规划引擎使用
async def load_data(cons):
    from app.agent.travel.data_source.data_sources import (
        load_attractions, load_restaurants, load_hotels, load_transport
    )
    city = cons.get("target_city", "")
    start_city = cons.get("start_city", "")
    # 目的地没填出来, 不调API, 避免浪费额度
    if not city or city == "?":
        return {"attractions": pd.DataFrame(), "restaurants": pd.DataFrame(),
                "accommodations": pd.DataFrame(), "intercity_transport": pd.DataFrame(),
                "hotel_estimated": False}

    df_attractions = await load_attractions(city)
    df_hotels = await load_hotels(city, max_price=cons.get("budget", 5000))
    df_restaurants = await load_restaurants(city)
    df_hotels, hotel_estimated = _fix_hotel_coords(df_hotels, df_attractions, df_restaurants)

    if start_city == city:
        df_rides = pd.DataFrame()
    else:
        df_rides_go = await load_transport(start_city, city)
        df_rides_back = await load_transport(city, start_city)
        df_rides = pd.concat([df_rides_go, df_rides_back], ignore_index=True) \
            if not df_rides_go.empty or not df_rides_back.empty else pd.DataFrame()

    return {"attractions": df_attractions, "restaurants": df_restaurants,
            "accommodations": df_hotels, "intercity_transport": df_rides,
            "hotel_estimated": hotel_estimated}

# ═══════════════ 规划主流程 ═══════════════

#行程plan → 前端时间轴卡片要的精简JSON, 没有行程返回None
def plan_to_frontend(plan):
    if not plan.get("itinerary"):
        return None
    days = []
    for day in plan.get("itinerary", []):
        acts = []
        for act in day.get("activities", []):
            acts.append({
                "type": act.get("type", ""),                  # train/airplane/intercity/breakfast/lunch/dinner/attraction/accommodation/free
                "position": act.get("position", ""),          # 地点(车站/餐厅/景点/酒店)
                "start": act.get("start", ""),                # 跨城交通: 出发城市
                "end": act.get("end", ""),                    # 跨城交通: 到达城市
                "vehicle": act.get("TrainID", "") or act.get("FlightID", ""),  # 车次/航班号
                "start_time": act.get("start_time", ""),
                "cost": round(float(act.get("cost", 0) or 0), 2),
                "transports": [                               # 市内交通(前往该活动的过程)
                    {"mode": ride.get("mode", ""), "start_time": ride.get("start_time", ""),
                     "cost": round(float(ride.get("cost", 0) or 0), 2)}
                    for ride in act.get("transports", [])
                ],
            })
        days.append({"day": day.get("day", 0), "activities": acts})
    # 总花费口径与 solve() 的预算警告一致: 活动费用 + 各自交通费
    total_cost = sum(act["cost"] + sum(ride["cost"] for ride in act["transports"])
                     for day in days for act in day["activities"])
    return {
        "days": len(days),
        "people": plan.get("people_number", 1),
        "total_cost": round(total_cost, 2),
        "warning": plan.get("warning"),       # 如"预算不足"，可为 None
        # 带上城市名与编码, 供前端"保存到规划"接口定位城市
        "city": plan.get("target_city", ""),
        "adcode": CITY_ADCODE_MAP.get(plan.get("target_city", ""), ""),
        "itinerary": days,
    }

#跨城规划 or 同城推荐: 槽位填充 → 加载数据 → 引擎搜索 → 睡眠过滤 → 预算提示
#override: 会话槽位里已经确定的城市/天数/预算/人数, 覆盖小模型填出来的值
#budget_stated: 用户**是不是真说了预算**。槽位填充在用户没提预算时会塞默认值 5000，
#  拿它当"用户预算"去提示（"你给了5000，差4110没花到"）就是凭空造条件 —— 所以提示必须门控
async def solve(query: str, override: dict | None = None, budget_stated: bool = False,
                prefs: dict | None = None):
    from app.agent.travel.services.slot_filler import get_slot_filler
    cons = await get_slot_filler().fill_slots(query, override=override)
    # 省名归一（必须在加载数据之前）：说明优先取目的地的
    cons["start_city"], note_start = normalize_city(cons["start_city"])
    cons["target_city"], note_target = normalize_city(cons["target_city"])
    note = note_target or note_start
    logger.info(f"[solve] from={cons['start_city']} to={cons['target_city']} days={cons['days']} budget={cons['budget']}")

    # 无出发地 → 同城模式
    if cons["start_city"] in ("?", ""):
        cons["start_city"] = cons["target_city"]
    # 只填出一个城市时(实测"重庆旅游两日计划"会被填成 出发:重庆 目的:?), 那这个城市就是目的地;
    # 不兜这一步 target_city="?" 会让 load_data 直接返回空数据 → 永远"无法规划"
    if cons["target_city"] in ("?", "") and cons["start_city"] not in ("?", ""):
        cons["target_city"] = cons["start_city"]
    # 同城多日(如"重庆两日游")保留用户要的天数, 由规划引擎找酒店排多日; 酒店不足时引擎内部回退单日

    data = await load_data(cons)
    logger.info(f"[solve] data: attr={len(data['attractions'])} rest={len(data['restaurants'])} hotel={len(data['accommodations'])} ic={len(data['intercity_transport'])}")

    # 用户偏好 → 数据池筛选（不吃辣/海鲜过敏/讨厌排队）。必须在 Planner 之前：
    # Planner 装载时只留 name/price/rating/坐标，cuisine、type 拿不到就筛不了了。
    from app.agent.travel.services.preference_filter import apply_preferences
    pref_notes = apply_preferences(data, prefs or {})

    planner = Planner(cons, data)
    ok, result = planner.search()
    if isinstance(result, list):
        plan = {"people_number": cons["people_number"], "start_city": cons["start_city"],
                "target_city": cons["target_city"], "itinerary": result}
    else:
        plan = result
    plan["search_stats"] = {"nodes": planner.nodes, "backtracks": planner.bt}

    # 失败原因：不是所有"排不出"都是天数超限 —— 上层的回复文案要据此说真话
    if not ok:
        if data["accommodations"].empty:
            plan["fail_reason"] = "no_hotel"
        elif data["attractions"].empty and data["restaurants"].empty:
            plan["fail_reason"] = "no_poi"
        else:
            plan["fail_reason"] = "too_long"

    # 引擎已保证时间有序不重叠, 这里只兜底过滤深夜活动
    for day in plan.get("itinerary", []):
        day["activities"] = [act for act in day.get("activities", []) if sleep_ok(act)]

    # 总花费口径与 solve() 的预算提示一致: 活动费用 + 各自交通费
    total_cost = sum(act.get("cost", 0) + sum(ride.get("cost", 0) for ride in act.get("transports", []))
                     for day in plan.get("itinerary", []) for act in day.get("activities", []))

    # 预算提示：超了要说，**明显没花到也要说**。
    # 只说超预算的话，用户看到"给了 4000 只花 1538"会直接问"花费是不是太少了"——
    # 与其让他追问，不如卡片上就讲清楚：这是**数据集的价格上限**，不是行程在省钱。
    # 实测（重庆/2天/2人）：本地酒店池只有 5 家、价位 ¥191~486（每次请求还会换一批），
    # 景点门票基本是 0 —— 所以能排出的最贵方案也就 ¥1200~1800，约占 4000 预算的 30~45%。
    # budget 与 total_cost 都是"全体人数合计"口径（引擎里各项已乘人数），可以直接比。
    budget = float(cons.get("budget") or 0)
    if not budget_stated:
        # 用户没说过预算（这个 5000 是槽位填充的默认值）→ 一句预算相关的话都不要说
        if not ok:
            plan["warning"] = plan.get("warning", "无法规划")
    elif total_cost > budget:
        plan["warning"] = f"预算不足：预计花费 ¥{total_cost:.0f}，超出预算 ¥{total_cost - budget:.0f}"
    elif total_cost < budget * BUDGET_SPENT_RATIO:
        try:
            hotel_max = float(data["accommodations"]["price"].max())
            ceiling = f"（数据里可选的酒店最高 ¥{hotel_max:.0f}/晚，景点门票基本免费）"
        except Exception:
            hotel_max, ceiling = 0, ""
        plan["warning"] = (
            f"预算 ¥{budget:.0f}。本地攻略数据的价格上限就在这{ceiling}，"
            f"按「不超预算」排下来预计 ¥{total_cost:.0f}，差 ¥{budget - total_cost:.0f} 没花到。"
            f"想更贴近预算的话，可以多加一天，或者把预算调低些，我按新条件重排。"
        )
    elif not ok:
        plan["warning"] = plan.get("warning", "无法规划")

    # 省名归一说明：无论排没排出来，都要让用户知道"实际按哪个城市排的"
    if note:
        plan["warning"] = f"{note}。{plan['warning']}" if plan.get("warning") else note

    # 景区别名说明：用户说"香格里拉/九寨沟"，卡片上写"迪庆/阿坝"必须解释一句
    alias_note = "；".join(alias_notes(query))
    if alias_note:
        plan["warning"] = f"{alias_note}。{plan['warning']}" if plan.get("warning") else alias_note + "。"

    # 偏好生效说明：让用户看见"记下的偏好真的用上了"，否则下次就不记了
    if pref_notes:
        note_pref = "；".join(pref_notes) + "。"
        plan["warning"] = f"{note_pref}{plan['warning']}" if plan.get("warning") else note_pref

    # 酒店是兜底估算房源时要说清楚（上游挂了，价格不是实时的）
    if ok and data.get("hotel_estimated"):
        note_hotel = "酒店价格是估算值（实时房源接口暂时不可用，以实际预订价为准）。"
        plan["warning"] = f"{note_hotel}{plan['warning']}" if plan.get("warning") else note_hotel

    return plan
