"""把用户记下的偏好变成**规划时的筛选条件**（记忆 → 规划闭环）

背景：记忆里明明存着"我不吃辣""我讨厌排队"，但规划引擎完全不看 ——
排出来的行程照样是火锅店 + 最热门的景点，用户会觉得"记了也没用"。

这一层只做**确定性**的事，不调模型：
  - `no_spicy`      不吃辣 → 辣味餐厅排到最后（不硬删，删了可能没得吃）
  - `no_seafood`    海鲜过敏 → **硬过滤**海鲜类餐厅（安全约束，宁可少选也不能端上来）
  - `avoid_crowd`   讨厌排队 → 景点排序：博物馆/公园/湿地这类先排，风景名胜/主题乐园往后

作用点在**数据层**（`travel_plan_tool.solve` 拿到 data 之后）：Planner 只吃
name/price/rating/经纬度，`cuisine`、`type` 在装载时就被丢掉了 —— 在 Planner 里做不了。
"""
import pandas as pd

# 辣味线索（命中 cuisine 或店名就算"辣"）
SPICY_HINTS = (
    "辣", "川菜", "湘菜", "火锅", "麻辣", "香辣", "酸辣", "水煮", "串串", "冒菜",
    "江湖菜", "小龙虾", "剁椒", "泡椒", "藤椒", "口味虾", "小龙虾",
)
# 海鲜线索（过敏是硬约束）
SEAFOOD_HINTS = ("海鲜", "蟹", "虾", "贝", "蚝", "鱼", "刺身", "寿司", "鲍", "海产", "鱿")

# 通常最挤的类型（含"必去/打卡"性质的风景名胜、主题乐园）
CROWD_HEAVY_TYPES = ("风景名胜", "主题乐园", "游乐园", "动物园", "水族馆", "商业街", "步行街")
# 通常宽松些的类型
CROWD_LIGHT_TYPES = ("博物馆", "美术馆", "展览馆", "公园", "湿地", "寺庙", "教堂",
                     "古镇", "图书馆", "植物园", "科技馆", "纪念地")

# 偏好 → 记忆里出现这些说法就算开了这项
TAG_HINTS: dict[str, tuple[str, ...]] = {
    "no_spicy": ("不吃辣", "不能吃辣", "吃不了辣", "怕辣", "忌辣", "不吃辣椒", "别放辣", "微辣都不行"),
    "no_seafood": ("海鲜过敏", "对海鲜过敏", "不吃海鲜", "不能吃海鲜", "海鲜过敏症", "对虾过敏", "对蟹过敏"),
    "avoid_crowd": ("讨厌排队", "不想排队", "不喜欢排队", "怕排队", "不想人挤", "怕挤",
                    "不喜欢人多", "不喜欢人太多", "不想凑热闹", "避开人群", "不喜欢排队等"),
}


def tags_from_texts(texts) -> dict[str, bool]:
    """记忆内容 → 偏好开关。只认确定性的关键词，不做语义推断（推错比不做更糟）。"""
    joined = "；".join(t for t in texts if t)
    return {tag: any(h in joined for h in hints) for tag, hints in TAG_HINTS.items()}


def _text_blob(row) -> str:
    return f"{row.get('name', '')} {row.get('cuisine', '')} {row.get('type', '')}"


def _is_spicy(row) -> bool:
    return any(h in _text_blob(row) for h in SPICY_HINTS)


def _is_seafood(row) -> bool:
    return any(h in _text_blob(row) for h in SEAFOOD_HINTS)


def _is_crowd_heavy(row) -> bool:
    t = str(row.get("type", ""))
    if any(h in t for h in CROWD_HEAVY_TYPES):
        return True
    if any(h in t for h in CROWD_LIGHT_TYPES):
        return False
    return False        # 没类型信息的当普通，不动


def apply_preferences(data: dict, prefs: dict) -> list[str]:
    """按偏好调整 data 里的餐厅/景点池，返回"实际生效了哪些"的人话（卡片上用）"""
    notes: list[str] = []
    if not prefs:
        return notes

    rest = data.get("restaurants")
    attr = data.get("attractions")

    # ── 不吃辣：把辣味店从池子里拿掉 ──
    # 只排序是没用的 —— 引擎挑餐厅看的是"离当前位置近不近"（`_meal_slots`），
    # 不看列表顺序，实测排序后 4 顿饭照样全是江湖菜/小龙虾。必须真过滤。
    # 兜底：过滤后不足 3 家就整体保留（否则用户没得吃），并说明是"尽量"。
    if prefs.get("no_spicy") and isinstance(rest, pd.DataFrame) and not rest.empty:
        kept = rest[~rest.apply(_is_spicy, axis=1)]
        dropped = len(rest) - len(kept)
        if len(kept) >= 3:
            data["restaurants"] = kept
            notes.append(f"按你记的「不吃辣」，已排除 {dropped} 家辣味餐厅（还剩 {len(kept)} 家）")
        else:
            data["restaurants"] = rest.assign(_k=rest.apply(_is_spicy, axis=1)) \
                                      .sort_values("_k", kind="stable").drop(columns="_k")
            notes.append("你记过「不吃辣」，但这座城市不辣的餐厅太少，只能尽量把辣味店往后排")

    # ── 海鲜过敏：硬过滤（安全第一）──
    if prefs.get("no_seafood") and isinstance(rest, pd.DataFrame) and not rest.empty:
        kept = rest[~rest.apply(_is_seafood, axis=1)]
        if len(kept) >= 3:
            data["restaurants"] = kept
            notes.append(f"按你记的「海鲜过敏」，已排除 {len(rest) - len(kept)} 家海鲜类餐厅")
        else:
            data["restaurants"] = kept
            notes.append("你记过海鲜过敏，但这座城市不卖海鲜的餐厅太少，我都排掉了 —— 到店前请再确认一次")

    # ── 讨厌排队：景点排序换一换 ──
    if prefs.get("avoid_crowd") and isinstance(attr, pd.DataFrame) and not attr.empty:
        attr = (attr.assign(_k=attr.apply(_is_crowd_heavy, axis=1))
                    .sort_values(["_k", "rating"], ascending=[True, False], kind="stable")
                    .drop(columns="_k"))
        data["attractions"] = attr
        first_type = str(attr.iloc[0].get("type", ""))
        notes.append(f"按你记的「讨厌排队」，景点优先安排了相对不挤的类型（先从{first_type}这类开始）")

    return notes
