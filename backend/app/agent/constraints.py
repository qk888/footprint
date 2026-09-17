"""harness 约束层 —— 把"谁能写 / 写成了没 / 说了算不算"收到一个地方

为什么需要框架级约束（而不是在每个工具里各写一遍护栏）：
  ① 散着写就一定会漏。原先 light_city / add_bill / save_memory 各核一遍用户原话，
     再新增一个写工具，只要忘了加护栏就是一次数据事故。
  ② 工具护栏只管得住"模型调工具"，管不住"模型在回复里声称自己做了"。
     实测 query="4" 被判成 operation 后，模型编出 light_city(['北京','上海'])；
     侥幸没写进库（本就点亮过），**回复仍然是"我已经为你点亮了北京和上海"** ——
     凭空宣布完成比写错更隐蔽：用户会以为数据变了。
  ③ 没有地方能回答"这一轮到底写了什么"。排查这类事故时只能靠翻日志猜。

所以约束分成三层，登记表只有一份（中间件 + 工具 + 话术校验共用它）：

  L1 授权（工具调起时）  write_intent_ok()      用户原话没有意图词 → 否决，工具体不执行
  L2 取证（执行过程中）  note_write()           任何写入（工具或直达通道）都留一条台账
  L3 声称（出话术前）    verify_write_claim()   说了"已经做了"就必须真有证据，否则换成如实答复

**写权限只属于确定性通道**：直达通道（关键词 + 城市表）不需要模型，是可靠的；
模型手里只留读工具，它就不可能凭空写坏数据。

跨项目通用：只要判定链路上还有"交给小模型"这一环，写操作就必须有框架级约束。
"""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass

from app.agent.utils.logger_handler import logger


# ══════════════ 本轮上下文 ══════════════
# 用 contextvar 而不是塞进工具参数：工具签名是给模型看的，多一个参数它就会乱填。

# 本轮用户原话。用途：给写工具做授权核对（L1）、给记忆工具做防改写。
CURRENT_USER_QUERY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "footprint_user_query", default=""
)
# 本轮写入台账（L2）。reset_turn 时新建列表对象，之后各层 append ——
# 上下文在"任务创建时拷贝"，但拷贝拿到的是**同一个列表对象**，所以子 agent / 工具里
# 的追加在本轮末尾读得到（这与 CURRENT_USER_QUERY 同一套机制，已被线上验证）。
TURN_WRITES: contextvars.ContextVar[list] = contextvars.ContextVar(
    "footprint_turn_writes", default=None
)


def reset_turn(query: str) -> list:
    """每轮对话开始时调一次：记下原话 + 清空写入台账。

    **返回值就是台账列表本身**，调用方（FootprintAgent）存到自己实例上，
    出话术前用它做 L3 校验 —— 不靠 contextvar 往上穿（contextvar 只会向下传到
    子任务，读取方在另一个生成器里时拿到的可能是别的上下文对象；
    直接持有同一个列表对象最稳）。
    """
    ledger: list = []
    CURRENT_USER_QUERY.set(query or "")
    TURN_WRITES.set(ledger)
    return ledger


def _ledger() -> list:
    log = TURN_WRITES.get()
    if log is None:                     # 没走 execute_stream（批处理脚本/单测直接调工具）
        log = []
        TURN_WRITES.set(log)
    return log


def note_write(tool: str, outcome: str, detail: str = "") -> None:
    """L2 取证：记一条写入台账。outcome ∈ ok / already / failed / denied"""
    _ledger().append({"tool": tool, "outcome": outcome, "detail": (detail or "")[:80]})
    logger.info(f"[harness/write-audit] {tool} → {outcome} {detail or ''}".rstrip())


def writes_this_turn() -> list:
    """本轮写入台账（排查用：能直接回答"这一轮到底写了什么"）"""
    return list(_ledger())


def write_count() -> int:
    """本轮**真的落地**的写入次数（不含被否决/失败的）"""
    return sum(1 for e in _ledger() if e["outcome"] in ("ok", "already"))


def classify_outcome(text: str) -> str:
    """从工具返回文本粗判结果，给 L2 归档用。

    粗判够用：这里只回答"有没有落地"，不追求精确；判断错最多影响话术校验，
    不会影响数据本身（真正的成败由工具体自己保证）。
    """
    t = text or ""
    if any(k in t for k in ("失败", "出错", "异常", "error", "Error")):
        return "failed"
    if any(k in t for k in ("之前已", "已经点亮过", "已点亮过", "已记过", "已经记过", "重复")):
        return "already"
    return "ok"


# ══════════════ L1 授权：写工具登记表 ══════════════

# 偏好陈述的识别词（原先散在 footprint_agent 里，挪到这儿是为了让
# "记忆写入"的授权词和"记忆直达通道"的触发词用**同一份**，不然两边迟早改歪）。
# 负面陈述原先漏得最多：不命中的会落到 operation 子 agent 的 tool calling，
# 实测小模型会把内容**改写**后才存 ——"我讨厌排队"存成"我不喜欢吃排队"（凭空多一个"吃"）。
PREFERENCE_HINTS = (
    "我喜欢", "我不喜欢", "我习惯", "我不吃", "我不喝", "我住", "我常住", "我怕", "我偏好", "我一般",
    "我讨厌", "我最讨厌", "我不爱", "我受不了", "我过敏", "我忌讳", "我不能吃",
    "我晕车", "我晕船", "我最喜欢", "我倾向", "我想住", "我要住", "我希望住",
)
# 明确的"记住X"吩咐
REMEMBER_WORDS = ("记住", "记一下", "记下来", "帮我记", "你要记住", "你记住", "别忘了")


@dataclass(frozen=True)
class WriteRule:
    """一个写工具的全部约束，一处登记、三处生效（中间件闸门 / 工具体自查 / 话术校验）"""
    label: str                          # 给人看的名字
    intent_words: tuple[str, ...]       # L1: 用户原话里必须命中至少一个才允许写
    claim_words: tuple[str, ...]        # L3: 回复里命中 = 在声称"已经做了"
    honest_reply: str                   # L3: 抓到时用来替换的话术
    ok_outcomes: tuple[str, ...] = ("ok", "already")


WRITE_RULES: dict[str, WriteRule] = {
    "light_city": WriteRule(
        label="点亮城市",
        intent_words=("点亮", "去过", "到过", "打卡", "足迹", "标记", "记录我去过", "我去过"),
        # 注意别把"已经点亮过"这类**陈述现状**的话当声称：
        # 查询回话就是"你已经点亮过这些城市：三亚、海口"，那不是这次写的，
        # 误判会把正经的查询答复也换成免责话术。
        claim_words=("已为你点亮", "已经为你点亮", "点亮成功", "帮你点亮了", "已点亮",
                     "已为你创建", "已经为你创建"),
        honest_reply=("这次我没有真的点亮任何城市 —— 不会有数据被改动。"
                      "要点亮的话直接说「点亮三亚」就行。"),
    ),
    "add_bill": WriteRule(
        label="记账",
        intent_words=("花了", "花费", "消费", "记账", "记一笔", "记一账", "买单", "付了", "支出",
                      "元", "块", "费用", "开销", "多少钱", "门票", "住宿", "打车", "吃饭"),
        claim_words=("已记录", "已记账", "已经记账", "记账成功", "记好了", "已为你记", "已经为你记",
                     "已保存这笔", "已经记上"),
        honest_reply=("这次我没有真的记下任何一笔账 —— 不会有数据被改动。"
                      "要记账就说「三亚吃饭花了50」。"),
    ),
    "save_memory": WriteRule(
        label="写入记忆",
        intent_words=REMEMBER_WORDS + PREFERENCE_HINTS,
        claim_words=("已记住", "已经记住", "已经帮你记住", "帮你记住了", "记住了你的"),
        honest_reply=("这次我没有真的写入记忆 —— 不会有数据被改动。"
                      "想让我记住就说「记住我不吃辣」。"),
    ),
    "save_plan": WriteRule(
        label="保存规划",
        intent_words=("保存", "加入规划", "存进规划", "更新规划", "另存", "覆盖", "新建一份"),
        claim_words=("已保存到规划", "已经保存到规划", "已加入规划", "已经加入规划",
                     "已更新规划", "已经把这", "已经把这份"),
        honest_reply=("这次我没有真的写入规划 —— 不会有数据被改动。"
                      "要保存请点行程卡片上的「保存到规划」按钮。"),
    ),
}


# 被约束否决时回给模型的哨兵文本（同时进 FAIL_TEXTS：模型若照抄，外层就判定这次失败）
WRITE_DENIED_MSG = "这条指令需要用户明确要求才能执行"
# 单轮写入次数上限：防模型在 tool_choice=required 下反复重试刷库
MAX_WRITES_PER_TURN = 6

# ── "提问不是写指令" ──
# L1 只按意图词核对时有个洞：**查询句里常常含写意图词**。
# 实测 "我去过哪些城市" 含"去过"（点亮的合法意图词）→ 放行 → 模型若误调
# light_city(['北京']) 就会把用户没提的城市点亮。同理 "我花了多少钱" 含"多少钱"（记账意图词）。
# 所以再加一条：句子里有提问标记、且没有明确的祈使写动作 → 一律否决。
QUESTION_MARKERS = ("哪些", "什么", "多少", "几笔", "几号", "吗", "查询", "查一下",
                    "看一下", "看看有", "汇总", "统计", "列举", "回顾")
WRITE_VERBS = ("点亮", "记住", "帮我记", "记一下", "记下来", "记一笔", "记一账",
               "保存", "加入规划", "存进规划", "新建一份", "覆盖", "更新规划", "打卡")


def is_question_for_write(query: str) -> bool:
    """这是在提问（而不是在吩咐做事）吗"""
    return any(m in query for m in QUESTION_MARKERS) and not any(v in query for v in WRITE_VERBS)


def write_intent_ok(tool: str, query: str | None = None) -> tuple[bool, str]:
    """L1 授权：写工具被调起时，核对**用户原话**里有没有对应意图。

    query: 授权依据。中间件传 `last_user_query(state)`（权威来源 —— state 里的最后一条
           用户消息，不依赖 contextvar 能不能穿过 task 边界）；工具内部自查时省略，
           退化成读 CURRENT_USER_QUERY，拿不到原话就放行（批处理脚本/单测场景）。
    非写工具 → 放行。
    """
    rule = WRITE_RULES.get(tool)
    if rule is None:
        return True, "非写工具"
    said = (query if query is not None else (CURRENT_USER_QUERY.get() or "")).strip()
    if not said:
        return True, "无原话(批处理场景)，不拦"
    if is_question_for_write(said):
        return False, f"这句话是在提问，不是「{rule.label}」的指令：{said[:40]}"
    if any(w in said for w in rule.intent_words):
        return True, "命中用户意图"
    return False, f"用户原话里没有「{rule.label}」的意图：{said[:40]}"


# ══════════════ L3 声称校验：说了就要有证据 ══════════════

def verify_write_claim(text: str, ledger: list | None = None) -> str:
    """出最终话术前过一遍：回复声称"已经做了"某件写操作，本轮就必须真有对应写入。

    这是"模型凭空宣布完成"的兜底 —— 工具层能拦住写操作，拦不住模型编造结果。
    命中就替换成如实答复（保留原话没有意义：那句话本身就是假的）。

    ledger: 本轮写入台账。显式传入优先（调用方持有列表对象，跨生成器最稳）；
            省略则读 contextvar。
    """
    if not text or not text.strip():
        return text
    log = ledger if ledger is not None else _ledger()
    for tool, rule in WRITE_RULES.items():
        if not any(w in text for w in rule.claim_words):
            continue
        if any(e["tool"] == tool and e["outcome"] in rule.ok_outcomes for e in log):
            continue
        logger.warning(
            f"[harness/claim] 回复声称已完成「{rule.label}」，但本轮没有成功写入记录 → 换成如实答复"
        )
        return rule.honest_reply
    return text


def audit_snapshot() -> dict:
    """本轮审计快照（测试与排障用）"""
    return {
        "query": CURRENT_USER_QUERY.get() or "",
        "writes": writes_this_turn(),
        "count": write_count(),
    }


# ══════════════ 输出约束：空话尾巴 ══════════════
# 模型很爱在正经内容后面追加一句客套 —— 刚说完"这条线路我排不了"，紧接着补
# "不过你可以试试看""祝你好运"，等于把前面的结论又软化了；用户读到的是"还有戏"。
# 提示词里写"不许用这类话术"也禁不住（1.5B 照样加），流式又没法回头改，
# 所以跟 RAG 那条"结尾反问止损"同一思路：确定性清理。
FLUFF_TAILS = (
    "祝你好运", "祝旅途愉快", "祝你旅途愉快", "祝你玩得开心", "祝你玩得愉快",
    "祝您旅途愉快", "祝您玩得开心", "希望对你有所帮助", "希望对你有帮助",
    "你可以试试看", "试试看吧", "相信你会", "加油哦",
)
_RE_SENTENCE = re.compile(r"(?<=[。！？!?~～])")


def strip_fluff_tail(text: str) -> str:
    """去掉结尾的客套/空话。只在**剥完还剩内容**时才剥，别把整条回复剥没了。"""
    if not text or not text.strip():
        return text
    parts = [p for p in _RE_SENTENCE.split(text.rstrip()) if p.strip()]
    while len(parts) > 1:
        tail = parts[-1].strip()
        if len(tail) <= 24 and any(k in tail for k in FLUFF_TAILS):
            parts.pop()
            continue
        break
    out = "".join(parts).strip()
    return out or text


# ══════════════ 输出约束：联网参考条目 ══════════════
# "本地排不了的路线"那条分支会把联网结果**原样**列给用户看，而不是让模型转述 ——
# 转述等于再给模型一次编的机会（这个分支存在的全部意义就是"不许编"）。
# 格式化抽在这里，是为了零依赖、可进 CI 测试。

def format_web_refs(lines: list[str], limit: int = 3, width: int = 70) -> str:
    """把 `[标题：摘要, ...]` 整理成几行 `· …`（去换行、超长截断、最多 limit 条）"""
    out: list[str] = []
    for line in list(lines or [])[:limit]:
        text = " ".join(str(line).split())
        if not text:
            continue
        out.append("· " + (text[:width] + "…" if len(text) > width else text))
    return "\n".join(out)
