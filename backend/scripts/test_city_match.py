# -*- coding: utf-8 -*-
"""城市名匹配常驻测试（core/city_match.find_cities）

跑法：docker exec footprint-backend python scripts/test_city_match.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.city_match import (
    find_cities, ambiguous_names_known, alias_notes, route_destination,
)
from app.core.city_alias import invalid_alias_targets

# (输入, 期望)  —— 期望 [] 表示不该识别出任何城市
CASES = [
    # ── 基础 ──
    ("帮我点亮三亚", ["三亚"]),
    ("把重庆和成都都点亮", ["重庆", "成都"]),
    ("从北京到杭州三日游", ["北京", "杭州"]),
    ("我想去云南玩", []),                     # 省名不在城市表里(云南不是市)
    ("点亮香港和澳门", ["香港", "澳门"]),
    # ── 最长匹配优先：鞍山 ⊂ 马鞍山 ──
    ("帮我点亮马鞍山", ["马鞍山"]),            # 曾经会把辽宁鞍山一起点亮
    ("马鞍山有什么好玩的", ["马鞍山"]),
    ("点亮鞍山", ["鞍山"]),
    ("从鞍山去马鞍山", ["鞍山", "马鞍山"]),
    # ── 邻字语境：同形常用词 ──
    ("早上吃个三明治就行", []),                # 三明
    ("帮我开封一下这个文件", []),              # 开封
    ("欢迎各位来宾参观", []),                  # 来宾
    ("日出日照金山，真是美景", []),            # 日照
    ("我们在中山路吃饭", []),                  # 中山
    ("大同小异，没什么区别", []),              # 大同
    ("四平八稳地推进项目", []),                # 四平
    ("白银价格最近涨了", []),                  # 白银
    # ── 但真在说城市时要认出来 ──
    ("推荐日照", ["日照"]),
    ("去开封玩三天", ["开封"]),
    ("三明三日游", ["三明"]),
    ("我想在中山住一晚", ["中山"]),
    # ── 景区别名 → 归属地级市 ──
    ("我想去香格里拉玩五天", ["迪庆"]),
    ("九寨沟三日游", ["阿坝"]),
    ("婺源看油菜花", ["上饶"]),
    ("去阳朔玩", ["桂林"]),
    ("敦煌莫高窟怎么去", ["酒泉"]),            # 两个别名同一归属 → 只算一次
    ("想爬泰山", ["泰安"]),
    ("鼓浪屿好玩吗", ["厦门"]),
    ("香格里拉和九寨沟哪个好玩", ["迪庆", "阿坝"]),
    # 别名与短城市名相撞：只说"长白山"（真目的地）不能顺带认成白山市
    ("长白山的雪景一定很美", ["延边"]),
    ("白山有什么好玩的", ["白山"]),
]

# 路线目的地解析：用户说的目的地，本地有没有数据
# (输入, 期望目的地原文, 期望城市, 期望原因) —— 原因 "unsupported" = 说了地方但没数据
ROUTE_CASES = [
    # ── 本地有数据：解析成城市（后视断言切干净 + 最长前缀优先）──
    ("从成都去重庆吃火锅", "重庆", "重庆", ""),
    ("去三亚玩三天", "三亚", "三亚", ""),
    ("北京到上海7日游", "上海", "上海", ""),
    ("去九寨沟玩", "九寨沟", "阿坝", ""),          # 景区别名 → 归属城市
    ("到婺源看油菜花", "婺源", "上饶", ""),
    # ── 本地没数据：必须能识别出"用户说了个我们去不了的地方"──
    ("从北极到撒哈拉观海", "撒哈拉", "", "unsupported"),   # 尾部动作词"观海"要去掉
    ("从北京到纽约", "纽约", "", "unsupported"),
    ("从北京到纽约怎么走", "纽约", "", "unsupported"),     # 曾经漏判（正则太贪被"怎么"吃掉）
    ("去巴黎", "巴黎", "", "unsupported"),
    ("去洛杉矶", "洛杉矶", "", "unsupported"),
    # ── 没有路线结构 / 在提问：都不该当成"说了个去不了的地方" ──
    ("我想去个暖和点的地方", "", "", ""),
    ("帮我规划云南五天", "", "", ""),
    ("巴黎有什么好玩的", "", "", ""),
    ("北京有什么好玩的", "", "", ""),
    ("广州有什么好吃的", "", "", ""),
]


def main() -> int:
    bad = 0
    for text, expect in CASES:
        got = find_cities(text)
        ok = got == expect
        bad += 0 if ok else 1
        print(("  PASS  " if ok else "  FAIL  ") + f"{text!r} -> {got}" + ("" if ok else f"  期望 {expect}"))
    # 名单自检：AMBIGUOUS_CITIES 里写了不存在的城市名要暴露
    unknown = ambiguous_names_known()
    if unknown:
        print("  FAIL  同形词名单里有不存在的城市:", unknown)
        bad += 1
    # 别名自检：右值必须是城市表里真实存在的城市，写错就静默失效
    bad_alias = invalid_alias_targets()
    if bad_alias:
        print("  FAIL  别名指向的城市不在城市表:", bad_alias)
        bad += 1
    # 解释文案：说了景区名要能跟用户解释"按哪座城找的"
    note = alias_notes("香格里拉玩三天")
    if note != ["「香格里拉」在迪庆，我按迪庆给你找"]:
        print("  FAIL  alias_notes 文案不对:", note)
        bad += 1
    # 路线目的地：本地有没有这座城市的数据（决定"排不了"要不要如实说）
    print("── 路线目的地解析 ──")
    for text, want_raw, want_city, want_reason in ROUTE_CASES:
        raw, city, reason = route_destination(text)
        ok = (raw, city, reason) == (want_raw, want_city, want_reason)
        bad += 0 if ok else 1
        print(("  PASS  " if ok else "  FAIL  ")
              + f"{text!r} -> ({raw!r}, {city!r}, {reason!r})"
              + ("" if ok else f"  期望 ({want_raw!r}, {want_city!r}, {want_reason!r})"))
    print(f"\n{'全部通过' if not bad else f'{bad} 条失败'}（{len(CASES) + len(ROUTE_CASES) + 2} 项）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
