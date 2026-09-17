# -*- coding: utf-8 -*-
"""偏好 → 规划筛选 常驻测试（travel/services/preference_filter）

跑法：docker exec footprint-backend python scripts/test_preferences.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from app.agent.travel.services.preference_filter import (
    tags_from_texts, apply_preferences,
)

REST = pd.DataFrame([
    {"name": "老火锅", "cuisine": "重庆火锅", "rating": 4.8, "price": 100.0},
    {"name": "海鲜大咖", "cuisine": "海鲜自助", "rating": 4.7, "price": 180.0},
    {"name": "清淡面馆", "cuisine": "面食", "rating": 4.5, "price": 30.0},
    {"name": "江南小炒", "cuisine": "本帮菜", "rating": 4.6, "price": 60.0},
    {"name": "寿司店", "cuisine": "日本料理", "rating": 4.4, "price": 90.0},
])
ATTR = pd.DataFrame([
    {"name": "超级风景名胜区", "type": "风景名胜", "rating": 5.0},
    {"name": "市博物馆", "type": "博物馆", "rating": 4.6},
    {"name": "欢乐主题乐园", "type": "主题乐园", "rating": 4.9},
    {"name": "湿地公园", "type": "公园", "rating": 4.4},
])


def main() -> int:
    bad = 0

    def check(name, got, expect):
        nonlocal bad
        ok = got == expect
        bad += 0 if ok else 1
        print(("  PASS  " if ok else "  FAIL  ") + f"{name}: {got}" + ("" if ok else f"  期望 {expect}"))

    # ── 记忆文本 → 标签 ──
    check("不吃辣", tags_from_texts(["我不吃辣"])["no_spicy"], True)
    check("讨厌排队", tags_from_texts(["我讨厌排队"])["avoid_crowd"], True)
    check("海鲜过敏", tags_from_texts(["我对海鲜过敏"])["no_seafood"], True)
    check("无关偏好不误判", tags_from_texts(["我喜欢安静的位置"])["no_spicy"], False)

    # ── 不吃辣：辣味店直接从池子里剔除（只排序没用：引擎按距离选店）──
    data = {"restaurants": REST.copy(), "attractions": ATTR.copy()}
    notes = apply_preferences(data, {"no_spicy": True})
    names = list(data["restaurants"]["name"])
    check("辣味被剔除", "老火锅" in names, False)
    check("不辣选项保留", len(data["restaurants"]), 4)
    check("给了说明", bool(notes), True)

    # 不辣店太少（<3）时不能删空，退回"尽量排后面"
    few = pd.DataFrame([
        {"name": "老火锅", "cuisine": "重庆火锅", "rating": 4.8, "price": 100.0},
        {"name": "麻辣烫", "cuisine": "麻辣烫", "rating": 4.5, "price": 30.0},
        {"name": "清淡面馆", "cuisine": "面食", "rating": 4.5, "price": 30.0},
    ])
    data = {"restaurants": few.copy(), "attractions": ATTR.copy()}
    apply_preferences(data, {"no_spicy": True})
    check("不足3家时不删空", len(data["restaurants"]), 3)
    check("不足3家时不辣在前", "辣" not in data["restaurants"].iloc[0]["cuisine"], True)

    # ── 海鲜过敏：硬过滤（安全）──
    data = {"restaurants": REST.copy(), "attractions": ATTR.copy()}
    apply_preferences(data, {"no_seafood": True})
    left = list(data["restaurants"]["name"])
    check("海鲜被剔除", any("海鲜" in n for n in left) or any("寿司" in n for n in left), False)
    check("保留了安全选项", "清淡面馆" in left, True)

    # ── 讨厌排队：景点优先不挤的类型 ──
    data = {"restaurants": REST.copy(), "attractions": ATTR.copy()}
    apply_preferences(data, {"avoid_crowd": True})
    top2 = list(data["attractions"]["type"].head(2))
    check("前两个不是拥挤类型", all(t not in ("风景名胜", "主题乐园") for t in top2), True)

    # ── 没偏好：一个字段都不动 ──
    data = {"restaurants": REST.copy(), "attractions": ATTR.copy()}
    notes = apply_preferences(data, {})
    check("空偏好无动作", (notes, list(data["restaurants"]["name"])[0]), ([], "老火锅"))

    print(f"\n{'全部通过' if not bad else f'{bad} 条失败'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
