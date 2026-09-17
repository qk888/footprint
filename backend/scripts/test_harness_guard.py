# -*- coding: utf-8 -*-
"""harness 约束层测试：授权(L1) / 取证(L2) / 声称校验(L3)

纯逻辑，零第三方依赖（constraints.py 只 import 标准库 + 本仓库 logger），
所以既能进 CI，也能直接在容器里跑：
    docker exec footprint-backend python scripts/test_harness_guard.py

为什么单独测这一层：它管的是"写操作能不能落地"和"回复能不能声称写过了"，
这两件事出错都是数据事故（实测模型在 query="4" 时编出 light_city(['北京','上海'])
并回复"我已经为你点亮了北京和上海"）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.constraints import (          # noqa: E402
    WRITE_RULES, WRITE_DENIED_MSG, MAX_WRITES_PER_TURN,
    reset_turn, note_write, write_count, writes_this_turn,
    write_intent_ok, verify_write_claim, classify_outcome, strip_fluff_tail, format_web_refs,
)

bad = 0


def check(name, got, expect):
    global bad
    ok = got == expect
    if not ok:
        bad += 1
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  got={got!r} 期望={expect!r}"))


# ═══════════ L1 授权：用户原话里没有意图就不许写 ═══════════
print("── L1 授权 ──")
reset_turn("4")                                    # 实测出事故的那句
check("原话'4' → 点亮被否决", write_intent_ok("light_city")[0], False)
check("原话'4' → 记账被否决", write_intent_ok("add_bill")[0], False)
check("原话'4' → 写记忆被否决", write_intent_ok("save_memory")[0], False)

reset_turn("帮我点亮三亚")
check("'帮我点亮三亚' → 点亮放行", write_intent_ok("light_city")[0], True)

reset_turn("三亚吃饭花了50")
check("'三亚吃饭花了50' → 记账放行", write_intent_ok("add_bill")[0], True)
check("'三亚吃饭花了50' → 点亮仍否决", write_intent_ok("light_city")[0], False)

reset_turn("我讨厌排队")
check("'我讨厌排队' → 写记忆放行", write_intent_ok("save_memory")[0], True)

reset_turn("从北极到撒哈拉观海")                     # travel 追问链路里的那句
check("'从北极到撒哈拉观海' → 点亮否决", write_intent_ok("light_city")[0], False)

reset_turn("")                                     # 批处理脚本直接调工具：原话拿不到
check("无原话(批处理) → 放行, 保持兼容", write_intent_ok("light_city")[0], True)
check("非写工具 → 放行", write_intent_ok("get_lighted_cities")[0], True)

reset_turn("我有什么偏好")                           # 查询类：不该触发记忆写入
check("'我有什么偏好' → 写记忆否决", write_intent_ok("save_memory")[0], False)

# "提问不是写指令"：查询句里常常含写意图词，只按意图词核对会放行 → 会写坏数据
reset_turn("我去过哪些城市")                         # 含"去过"（点亮意图词），但这是查询
check("'我去过哪些城市' → 点亮否决", write_intent_ok("light_city")[0], False)
reset_turn("我花了多少钱")                           # 含"多少钱"（记账意图词），也是查询
check("'我花了多少钱' → 记账否决", write_intent_ok("add_bill")[0], False)
reset_turn("洪崖洞门票多少钱")
check("'洪崖洞门票多少钱' → 记账否决", write_intent_ok("add_bill")[0], False)
reset_turn("帮我点亮三亚")                           # 祈使句里有写动作 → 放行
check("'帮我点亮三亚' → 提问规则不误伤", write_intent_ok("light_city")[0], True)
reset_turn("门票多少钱，帮我记一下")
check("'…帮我记一下' → 提问规则不误伤", write_intent_ok("add_bill")[0], True)

# ═══════════ L2 取证：台账记全，且能区分"真落地"和"被否决/失败" ═══════════
print("── L2 取证 ──")
ledger = reset_turn("帮我点亮三亚")
check("reset_turn 返回台账对象", isinstance(ledger, list), True)
note_write("light_city", "ok", "三亚")
check("台账条数", len(writes_this_turn()), 1)
check("落地计数只算 ok/already", write_count(), 1)
note_write("add_bill", "denied", "没有意图")
note_write("add_bill", "failed", "上游超时")
check("被否决/失败的写入不计入落地数", write_count(), 1)

check("失败文本判定", classify_outcome("记账失败: 上游超时"), "failed")
check("已存在判定", classify_outcome("三亚之前已点亮过"), "already")
check("正常判定", classify_outcome("点亮城市三亚成功，已为你创建对应旅行"), "ok")

# ═══════════ L3 声称校验：说了"已经做了"就必须有证据 ═══════════
print("── L3 声称校验 ──")
FABRICATED = "好的，我已经为你点亮了北京和上海。"          # 实测出现过的那句假话

log = reset_turn("4")
check("编造的点亮声称 → 换成如实答复", verify_write_claim(FABRICATED, ledger=log), WRITE_RULES["light_city"].honest_reply)

log = reset_turn("帮我点亮三亚")
note_write("light_city", "ok", "三亚")
check("真有写入 → 原样放行", verify_write_claim(FABRICATED, ledger=log), FABRICATED)

log = reset_turn("4")
note_write("light_city", "denied", "没有意图")            # 被闸门否决过
check("只有被否决记录 → 仍判为编造", verify_write_claim(FABRICATED, ledger=log), WRITE_RULES["light_city"].honest_reply)

log = reset_turn("我去过哪些城市")                        # 查询类回话，陈述现状不算声称
note_write("light_city", "already", "查询现有点亮")
QUERY_REPLY = "你已经点亮过这些城市：三亚、海口"
check("查询现状的答复 → 不被误判", verify_write_claim(QUERY_REPLY, ledger=log), QUERY_REPLY)
check("未记录的查询答复也不误判(不含声称词)", verify_write_claim(QUERY_REPLY, ledger=reset_turn("x")), QUERY_REPLY)

log = reset_turn("帮我记一下我不吃辣")
check("引导语(非声称) → 放行", verify_write_claim("要点亮的话直接说「点亮三亚」就行", ledger=log),
      "要点亮的话直接说「点亮三亚」就行")
check("记账声称无证据 → 换成如实答复", verify_write_claim("已记录：三亚 50 元", ledger=log),
      WRITE_RULES["add_bill"].honest_reply)

log = reset_turn("把这份行程保存到规划")
note_write("save_plan", "ok", "重庆")
check("保存规划真有写入 → 放行",
      verify_write_claim("已经把这 3 天重庆的行程存进「规划」了", ledger=log),
      "已经把这 3 天重庆的行程存进「规划」了")

log = reset_turn("你好")
note_write("save_memory", "ok", "我讨厌排队")
check("记忆声称有证据 → 放行", verify_write_claim("已记住：我讨厌排队", ledger=log), "已记住：我讨厌排队")

check("空文本不炸", verify_write_claim("", ledger=reset_turn("x")), "")
check("普通闲聊不受影响", verify_write_claim("今天天气不错，想去走走", ledger=reset_turn("x")),
      "今天天气不错，想去走走")

# ═══════════ 输出约束：空话尾巴 ═══════════
# 刚说完"这条线路我排不了"，紧接着补"不过你可以试试看、祝你好运" = 把结论又软化了，
# 用户读到的意思变成"还有戏"。提示词禁不住，靠确定性清理。
print("── 输出约束：空话尾巴 ──")
check("拒绝 + 客套尾巴 → 只留结论",
      strip_fluff_tail("这条线路我排不了。不过你可以试试看看吧！祝你好运~"),
      "这条线路我排不了。")
check("多条客套尾巴一起剥",
      strip_fluff_tail("这条线路我排不了。可以试试看吧。祝您旅途愉快"),
      "这条线路我排不了。")
check("整条都是客套 → 不动（别剥成空）",
      strip_fluff_tail("祝你好运"), "祝你好运")
check("没有客套 → 原样",
      strip_fluff_tail("第1天去蜈支洲岛，晚上去第一市场吃海鲜。"),
      "第1天去蜈支洲岛，晚上去第一市场吃海鲜。")
check("正经内容里的'试试看'不误剥",
      strip_fluff_tail("想省钱的话可以把住宿换成青旅，餐饮每天控制在 100 元以内。"),
      "想省钱的话可以把住宿换成青旅，餐饮每天控制在 100 元以内。")

print("── 输出约束：联网参考条目 ──")
check("条目格式：去换行 + 加 ·",
      format_web_refs(["标题A：摘要\n换行了", "标题B：摘要"]),
      "· 标题A：摘要 换行了\n· 标题B：摘要")
check("超长截断并加省略号",
      format_web_refs(["x" * 100]).endswith("…"), True)
check("最多 3 条", len(format_web_refs(["a"] * 9).splitlines()), 3)
check("空输入不炸", format_web_refs([]), "")
check("空串条目被跳过", format_web_refs(["", "  ", "标题C"]), "· 标题C")

# ═══════════ 登记表自检：防止以后加规则时写出"自我触发"的话术 ═══════════
print("── 登记表自检 ──")
for tool, rule in WRITE_RULES.items():
    check(f"{tool} 有意图词", bool(rule.intent_words), True)
    check(f"{tool} 有声称词", bool(rule.claim_words), True)
    # honest_reply 若命中本规则的 claim_words，替换后会再被判定一次 → 死循环式误判
    hit = [w for w in rule.claim_words if w in rule.honest_reply]
    check(f"{tool} 的如实答复不会自我触发", hit, [])
    # 否决哨兵同理：它会被回给模型，语义上不能长得像"成功"
    check(f"{tool} 的否决哨兵不与声称词混淆",
          [w for w in rule.claim_words if w in WRITE_DENIED_MSG], [])
check("单轮写入上限为正数", MAX_WRITES_PER_TURN > 0, True)

print(f"\n{'全部通过' if not bad else f'{bad} 条失败'}")
sys.exit(1 if bad else 0)
