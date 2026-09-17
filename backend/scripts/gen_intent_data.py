# -*- coding: utf-8 -*-
"""生成「意图分类」微调数据（对齐当前架构）

为什么不用原来的 tool_calling_train.jsonl：
    那份数据教模型"用 tool call 选子助手 + 调具体工具"，但这两条路现在都已经
    被纯文本三分类(classify_intent)和直达通道(记账/点亮/账单查询/记忆/规划/攻略)
    绕开了。拿它训，训出来的能力没有代码会去调用。

这份数据教的是**现在真正会走模型的那一件事**：给一句关键词规则判不出来的话，
输出 travel / operation / chat 三个词之一。

数据来源：
    1) 原 398 条里的 199 条路由样本 → 转成 "human 一句话 → gpt 一个标签"
       （标签是原来人工标的，权威；其中还包含"规则会误判"的句子，正好教模型别跟着错）
    2) 城市名替换派生：同句式换城市，低成本扩量
    3) 手工补充：覆盖规则完全抓不到的模糊表达

输出：backend/train_data/intent_classify_train.jsonl
运行：python scripts/gen_intent_data.py
"""

import ast
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(BASE)
AGENT_PY = os.path.join(BACKEND, "app", "agent", "footprint_agent.py")
SRC = os.path.join(BACKEND, "train_data", "tool_calling_train.jsonl")
OUT = os.path.join(BACKEND, "train_data", "intent_classify_train.jsonl")

LABELS = ("travel", "operation", "chat")
MAPPING = {
    "travel_agent_tool": "travel",
    "operation_agent_tool": "operation",
    "chat_agent_tool": "chat",
}

# ---------------------------------------------------------------- 1. 取线上真实的分类提示词
# 从源码解析，保证和线上完全一致（prompt 改了这里自动跟着变）
_tree = ast.parse(open(AGENT_PY, encoding="utf-8").read())
CLASSIFY_PROMPT = None
for _n in _tree.body:
    if isinstance(_n, ast.Assign) and getattr(_n.targets[0], "id", None) == "CLASSIFY_PROMPT":
        CLASSIFY_PROMPT = ast.literal_eval(_n.value)
if not CLASSIFY_PROMPT:
    raise SystemExit("没从 footprint_agent.py 里解析到 CLASSIFY_PROMPT")

# ---------------------------------------------------------------- 2. 来源一：原路由样本
def load_original():
    out = []
    if not os.path.exists(SRC):
        return out
    for line in open(SRC, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        calls = [m for m in row.get("messages", []) if m.get("from") == "function_call"]
        if not calls:
            continue
        try:
            name = json.loads(calls[0]["value"])["name"]
        except Exception:
            continue
        label = MAPPING.get(name)
        if not label:
            continue
        q = next((m["value"] for m in row["messages"] if m.get("from") == "human"), "")
        if q:
            out.append((q.strip(), label))
    return out


# ---------------------------------------------------------------- 3. 来源二：城市替换派生
CITIES = ["重庆", "成都", "北京", "上海", "杭州", "广州", "深圳", "西安", "武汉", "长沙",
          "南京", "苏州", "青岛", "厦门", "昆明", "大理", "丽江", "哈尔滨", "沈阳", "天津",
          "郑州", "合肥", "福州", "南宁", "贵阳", "兰州", "西宁", "拉萨", "乌鲁木齐", "呼和浩特"]

# (模板, 标签)  —— {c} 会被城市名替换
TEMPLATES = [
    # --- operation：足迹 / 记账 的模糊说法（都不含强关键词）---
    ("刚去了一趟{c}", "operation"),
    ("我刚从{c}回来", "operation"),
    ("前几天在{c}玩了下", "operation"),
    ("我这地图上亮了哪些地方", "operation"),
    ("看看我的足迹", "operation"),
    ("我的旅行足迹是什么样的", "operation"),
    ("上个月去{c}住了三晚", "operation"),
    ("这趟出门一共花了多少", "operation"),
    ("这次去{c}的钱都花哪了", "operation"),
    ("过几天要去{c}玩", "operation"),
    ("从{c}坐高铁回来真的好累", "operation"),
    ("在{c}待了两天，花了点钱", "operation"),
    # --- travel：问怎么玩（不带"景点/攻略/行程"这些字眼）---
    ("{c}有什么好玩的", "travel"),
    ("周末想出去玩，有什么地方推荐", "travel"),
    ("去{c}玩要注意什么", "travel"),
    ("推荐一下{c}的玩法", "travel"),
    ("{c}，四天，吃吃吃为主", "travel"),
    ("帮我安排一个{c}的自驾游，三天两夜", "travel"),
    ("第一次去{c}，有什么建议", "travel"),
    ("{c}三天时间够不够", "travel"),
    ("下个月想去{c}，路线怎么走", "travel"),
    ("带爸妈去{c}合适吗", "travel"),
    ("{c}怎么逛比较好", "travel"),
    ("冬天去{c}有什么要提前准备的", "travel"),
    ("{c}和周边小城怎么串起来玩", "travel"),
    # --- chat：日常闲聊（关键词可能误伤的那些）---
    ("刚旅游回来", "chat"),
    ("我好喜欢旅行呀", "chat"),
    ("旅游太累了", "chat"),
    ("这次旅游还挺开心的", "chat"),
    ("去旅游回来好累啊", "chat"),
    ("你觉得旅行最重要的是什么", "chat"),
    ("{c}好热啊", "chat"),
    ("最近一直想出去走走", "chat"),
    ("我有点纠结要不要出去玩", "chat"),
    ("明天要出发了，有点小紧张", "chat"),
    ("今天天气真好，心情不错", "chat"),
    ("不想上班了，好想出去旅游", "chat"),
    ("旅行照片太多了不知道整理", "chat"),
    ("你觉得一个人旅行好吗", "chat"),
    ("好久没出门了", "chat"),
    ("坐飞机还是坐高铁纠结中", "chat"),
    ("刚看完一部旅行的电影", "chat"),
]

# 城市替换派生时，只对"一句话绑定一个城市"的模板做，避免生成怪句子
DERIVE_CITY_COUNT = 4


def derive():
    out = []
    for tpl, label in TEMPLATES:
        if "{c}" in tpl:
            for c in CITIES[:DERIVE_CITY_COUNT]:
                out.append((tpl.replace("{c}", c), label))
        else:
            out.append((tpl, label))
    return out


# ---------------------------------------------------------------- 4. 来源三：手工补充
MANUAL = [
    # travel
    ("元旦想去哪玩比较好", "travel"),
    ("三天两晚的城市推荐一下", "travel"),
    ("海边小众的地方有哪些", "travel"),
    ("适合一个人散心的地方", "travel"),
    ("我想出去走走，散散心", "travel"),
    ("古镇哪个值得去", "travel"),
    ("求个周末两日游的方案", "travel"),
    ("带小孩去哪玩比较合适", "travel"),
    ("现在去川西合适吗", "travel"),
    ("想找人少风景好的地方", "travel"),
    # operation
    ("我要记一笔", "operation"),
    ("补一下昨天的账", "operation"),
    ("这个月的花销统计一下", "operation"),
    ("我点亮的城市有几个", "operation"),
    ("把我去过的地方列出来", "operation"),
    ("今天吃完饭的钱记上", "operation"),
    ("帮我看看消费情况", "operation"),
    ("车票钱还没记", "operation"),
    # chat
    ("你好呀", "chat"),
    ("你是谁", "chat"),
    ("你能做什么", "chat"),
    ("谢谢", "chat"),
    ("哈哈哈哈", "chat"),
    ("好无聊啊", "chat"),
    ("今天上班好累", "chat"),
    ("我朋友说云南很美", "chat"),
    ("感觉最近状态不太好", "chat"),
    ("晚安", "chat"),
]

# chat 补充：闲聊样本最少(原数据只有 43 条), 而"旅游/旅行"改弱之后大量闲聊会走到模型,
# 这一类的训练量必须跟上 —— 不然模型会偏向判成 travel/operation
CHAT_EXTRA = [
    # 含"旅行/旅游/出去玩"但其实是闲聊 —— 误判高发区, 重点补
    ("我朋友刚从西藏回来，说特别震撼", "chat"),
    ("旅游真的会上瘾", "chat"),
    ("上次去海边晒伤了好几天", "chat"),
    ("我妈一直念叨着想去三亚", "chat"),
    ("放假在家躺着最舒服了", "chat"),
    ("刚刷到个旅行vlog，拍得真好看", "chat"),
    ("行李箱又坏了，烦", "chat"),
    ("出去玩一趟胖了三斤", "chat"),
    ("我同事说他下个月要去欧洲", "chat"),
    ("现在旅游怎么这么贵啊", "chat"),
    ("还是宅在家里舒服", "chat"),
    ("上次出去玩忘带充电器了", "chat"),
    ("出门在外最怕丢东西", "chat"),
    ("旅游回来要倒时差好痛苦", "chat"),
    ("我更喜欢一个人瞎逛", "chat"),
    ("朋友圈全是别人出去玩的照片", "chat"),
    # 寒暄 / 关于助手自身
    ("在吗", "chat"),
    ("早上好", "chat"),
    ("你是机器人吗", "chat"),
    ("你叫什么名字", "chat"),
    ("你会做什么呀", "chat"),
    ("你能帮我做什么", "chat"),
    ("再见", "chat"),
    ("辛苦你了", "chat"),
    ("你懂的真多", "chat"),
    ("你平时都干什么", "chat"),
    # 情绪 / 日常
    ("今天心情特别好", "chat"),
    ("好烦啊", "chat"),
    ("最近压力有点大", "chat"),
    ("周末不知道干什么", "chat"),
    ("下雨了，不想出门", "chat"),
    ("今天好热", "chat"),
    ("刚睡醒", "chat"),
    ("加班到现在", "chat"),
    ("想吃火锅了", "chat"),
    ("好想吃甜的", "chat"),
    ("我养的猫又拆家了", "chat"),
    ("刚看完一本书", "chat"),
    # 无关话题
    ("你会唱歌吗", "chat"),
    ("推荐首歌听听", "chat"),
    ("今天几号了", "chat"),
    ("你吃饭了吗", "chat"),
    ("讲个笑话", "chat"),
    ("你怕不怕黑", "chat"),
    ("你觉得人工智能会取代人类吗", "chat"),
    ("怎么才能早睡", "chat"),
    ("我该不该换工作", "chat"),
    ("有什么好看的电影", "chat"),
    ("这个怎么用", "chat"),
    ("什么意思啊", "chat"),
    ("再说一遍", "chat"),
    ("嗯嗯", "chat"),
    ("好的", "chat"),
    ("没问题", "chat"),
]


# ---------------------------------------------------------------- 组装
def build():
    seen = {}
    for q, label in load_original():
        seen.setdefault(q, label)
    orig_n = len(seen)

    added = 0
    for q, label in derive() + MANUAL + CHAT_EXTRA:
        if q not in seen:
            seen[q] = label
            added += 1

    rows = [(q, lbl) for q, lbl in seen.items()]
    from collections import Counter
    dist = Counter(lbl for _, lbl in rows)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for q, label in rows:
            f.write(json.dumps({
                "system": CLASSIFY_PROMPT,
                "tools": "",
                "messages": [
                    {"from": "human", "value": q},
                    {"from": "gpt", "value": label},
                ],
            }, ensure_ascii=False) + "\n")

    print(f"原路由样本: {orig_n} 条")
    print(f"派生+手工新增: {added} 条")
    print(f"合计写出: {len(rows)} 条 -> {os.path.relpath(OUT, BACKEND)}")
    print("标签分布:", dict(dist))


if __name__ == "__main__":
    build()
