"""会话级结构化槽位记忆（P1）。

为什么不是"滚动摘要"：本地 1.5B 写摘要既慢（一次调用 ~10 秒）又会幻觉，
而一次对话里真正需要跨轮带住的字段是**确定性的**（城市 / 天数 / 预算 / 人数），
用规则抽取可以做到 100% 可靠、零模型开销，而且不会随对话窗口滚掉。

存储走 Redis（按 用户+会话），兜住"前端只送最近 20 条历史"的截断：
用户 20 条之前说的"我想去重庆"，到了第 21 条依然能带进提示词。
"""
import json
import re

from app.agent.utils.logger_handler import logger
from app.config.cache_config import redis_client
from app.core.city_match import is_city_mention, find_cities
from app.core.constants import CITY_ADCODE_MAP

#会话槽位保留 7 天
SLOTS_TTL = 7 * 24 * 3600
#拼进提示词的块最长字符数(本地模型 4096 上下文, 这一块必须小)
SLOTS_BLOCK_MAX = 160

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN = "一两二三四五六七八九十"

#"3天" / "3日"
_RE_DAYS_ARABIC = re.compile(r"(\d{1,2})\s*[天日]")
#"三天" / "两日游"
_RE_DAYS_CN = re.compile(rf"([{_CN}]{{1,2}})\s*[天日]")
#相对天数: "再加一天" / "多玩两天" / "延长一天"
_RE_DAYS_ADD = re.compile(rf"(?:再|多|加|增加|延长)\s*(?:玩|去)?\s*([{_CN}\d]{{1,2}})\s*[天日]")
#相对天数: "减一天" / "少玩两天" / "缩短一天"
_RE_DAYS_SUB = re.compile(rf"(?:减|少|去掉|缩短)\s*(?:玩|去)?\s*([{_CN}\d]{{1,2}})\s*[天日]")
#"预算4000" / "预算: 4000" / "预算大概5000"
_RE_BUDGET = re.compile(r"预算[^\d]{0,4}(\d{3,6})")
#预算紧的说法
_RE_BUDGET_TIGHT = re.compile(r"预算(?:不多|有限|紧张|少)|省钱|穷游")
#"2人" / "2个人" / "两个人" / "一个人"
_RE_PEOPLE_ARABIC = re.compile(r"(\d{1,2})\s*个?人")
_RE_PEOPLE_CN = re.compile(rf"([{_CN}])\s*个?人")
_RE_PEOPLE_ALONE = re.compile(r"一个人|独自|自己一个人|单人")
#周: "玩一周" / "待两周" → 折算成天(7/14)。中文说行程时"周"很常见
_RE_WEEKS = re.compile(rf"([{_CN}\d]{{1,2}})\s*个?\s*周")
#出发地: "从北京出发"
_RE_FROM = re.compile(r"从([\u4e00-\u9fa5]{2,6}?)(?:出发|去|到|飞)")
#"北京到杭州" / "北京去杭州"
_RE_A_TO_B = re.compile(r"([\u4e00-\u9fa5]{2,4})\s*(?:到|去|飞往|飞)\s*([\u4e00-\u9fa5]{2,4})")

#改口信号: 出现这些词说明最新说法要覆盖旧的(按合并顺序天然成立, 这里只是记录口径)
#last_travel_mode: 上一次 travel 走的是"攻略问答"还是"行程规划", 让"改成X"能接着同一个语境
#last_plan_*: 上一次排出来的行程的花费/天数/人数 —— 用来回答"花费是不是太少了"这类追问
SCALAR_KEYS = ("target_city", "start_city", "days", "budget", "budget_level", "people",
               "last_travel_mode", "last_plan_cost", "last_plan_days", "last_plan_people")


def _cn_to_int(text: str) -> int | None:
    """中文数字转阿拉伯数字（只支持 1~99，够用了）"""
    text = text.strip()
    if not text:
        return None
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_NUM.get(head, 1) if head else 1
        ones = _CN_NUM.get(tail, 0) if tail else 0
        return tens * 10 + ones
    total = 0
    for ch in text:
        if ch not in _CN_NUM:
            return None
        total = total * 10 + _CN_NUM[ch]
    return total or None


def _find_cities(text: str) -> list[str]:
    """按在原文中出现的先后顺序返回城市名

    统一走 core/city_match.find_cities：过邻字判据（"早上吃个**三明**治"不算地名，
    裸 find 会把槽位写错、而槽位活 7 天，后面规划全排错城市）+ 最长匹配优先。
    """
    return find_cities(text)


def extract_slots(text: str) -> dict:
    """从一句话里抽确定性槽位。抽不到就不放这个键（不猜、不填默认值）。"""
    text = (text or "").strip()
    slots: dict = {}
    if not text:
        return slots

    cities = _find_cities(text)
    if cities:
        slots["cities"] = cities

    #出发地/目的地: 优先看"A到B"这种明确说法
    m = _RE_A_TO_B.search(text)
    if m and m.group(1) in CITY_ADCODE_MAP and m.group(2) in CITY_ADCODE_MAP:
        slots["start_city"] = m.group(1)
        slots["target_city"] = m.group(2)
    else:
        m = _RE_FROM.search(text)
        if m and m.group(1) in CITY_ADCODE_MAP:
            slots["start_city"] = m.group(1)
        if cities:
            last = cities[-1]
            if last != slots.get("start_city"):
                slots["target_city"] = last

    #天数: 先看相对增减("再加一天"), 再看绝对值。
    #顺序反了会把"再加一天"抽成"1天"(绝对天数正则先命中"一天")
    m_add = _RE_DAYS_ADD.search(text)
    m_sub = _RE_DAYS_SUB.search(text)
    if m_add or m_sub:
        raw = (m_add or m_sub).group(1)
        n = int(raw) if raw.isdigit() else _cn_to_int(raw)
        if n and 1 <= n <= 30:
            slots["days_delta"] = n if m_add else -n
    else:
        m = _RE_DAYS_ARABIC.search(text) or _RE_DAYS_CN.search(text)
        if m:
            raw = m.group(1)
            days = int(raw) if raw.isdigit() else _cn_to_int(raw)
            if days and 1 <= days <= 30:
                slots["days"] = days
        else:
            # "一周"这种说法没"天/日"字, 单独折算(实测"去重庆玩一周"被模型理解成 5 天)
            m = _RE_WEEKS.search(text)
            if m:
                raw = m.group(1)
                weeks = int(raw) if raw.isdigit() else _cn_to_int(raw)
                if weeks and 1 <= weeks <= 8:
                    slots["days"] = min(30, weeks * 7)

    m = _RE_BUDGET.search(text)
    if m:
        slots["budget"] = int(m.group(1))
        slots.pop("budget_level", None)
    elif _RE_BUDGET_TIGHT.search(text):
        slots["budget_level"] = "tight"

    if _RE_PEOPLE_ALONE.search(text):
        slots["people"] = 1
    else:
        m = _RE_PEOPLE_ARABIC.search(text) or _RE_PEOPLE_CN.search(text)
        if m:
            raw = m.group(1)
            people = int(raw) if raw.isdigit() else _cn_to_int(raw)
            if people and 1 <= people <= 20:
                slots["people"] = people

    return slots


def merge_slots(base: dict, new: dict) -> dict:
    """后说的覆盖先说的；城市名单按出现顺序累积去重（最多留 5 个）"""
    out = dict(base or {})
    for key in SCALAR_KEYS:
        if new.get(key) not in (None, "", 0):
            out[key] = new[key]
    # 预算和"预算紧"互斥：有明确数字就不要 tight 标记
    if new.get("budget"):
        out.pop("budget_level", None)
    if new.get("cities"):
        seen = list(out.get("cities") or [])
        for c in new["cities"]:
            if c not in seen:
                seen.append(c)
        out["cities"] = seen[-5:]
    # 相对天数："再加一天" 是在基准天数上加减，没基准就不放（免得凭空造出天数）
    delta = new.get("days_delta")
    if delta:
        base_days = out.get("days")
        if base_days:
            out["days"] = max(1, min(30, base_days + delta))
    return out


def render_slots_block(slots: dict) -> str:
    """把槽位拼成一段小提示块；没有可用字段就返回空串（不占上下文）"""
    parts = []
    if slots.get("target_city"):
        parts.append(f"目的地：{slots['target_city']}")
    if slots.get("start_city"):
        parts.append(f"出发地：{slots['start_city']}")
    if slots.get("days"):
        parts.append(f"天数：{slots['days']}天")
    if slots.get("budget"):
        parts.append(f"预算：{slots['budget']}元")
    elif slots.get("budget_level") == "tight":
        parts.append("预算：偏紧")
    if slots.get("people"):
        parts.append(f"人数：{slots['people']}人")
    if not parts:
        return ""
    line = "；".join(parts)[:SLOTS_BLOCK_MAX]
    return (
        "\n【本次会话已知条件】\n"
        + line
        + "\n（这是用户在这次对话里已经说过的条件，直接用，别重复追问；与他最新说法冲突时以最新为准。）\n"
    )


def _key(user_id: int, session_id: str) -> str:
    return f"chat_slots:{user_id}:{session_id}"


def _plan_key(user_id: int, session_id: str) -> str:
    return f"chat_last_plan:{user_id}:{session_id}"


async def save_last_plan(user_id: int, session_id: str, payload: dict) -> None:
    """把刚生成的行程卡片整份存下来（不是摘要）

    为什么单独一个 key、不并进槽位：槽位只同步 SCALAR_KEYS（城市/天数/预算…）且语义是"条件"，
    这里是**产物**（几 KB 的 JSON），生命周期和用途都不同。

    为什么需要它：用户看完卡片会说"把这张加入规划 / 更新一下"。以前 agent 手里没有卡片，
    只能回一句"请点卡片上的按钮"（更糟的是这话还可能被当成"重新规划"再出一张新卡片，
    把用户看中的那版覆盖掉）。有了这份 payload，后端就能自己落库。
    """
    if not session_id:
        return
    try:
        await redis_client.setex(_plan_key(user_id, session_id), SLOTS_TTL,
                                 json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"[last_plan] 写入失败: {e}")


async def load_last_plan(user_id: int, session_id: str) -> dict:
    """取最近一次生成的行程 payload；没有/读失败都返回 {}"""
    if not session_id:
        return {}
    try:
        raw = await redis_client.get(_plan_key(user_id, session_id))
        return json.loads(raw) if raw else {}
    except Exception as e:
        logger.warning(f"[last_plan] 读取失败: {e}")
        return {}


async def load_slots(user_id: int, session_id: str) -> dict:
    try:
        raw = await redis_client.get(_key(user_id, session_id))
        return json.loads(raw) if raw else {}
    except Exception as e:
        logger.warning(f"[session_slots] 读取会话槽位失败: {e}")
        return {}


async def save_slots(user_id: int, session_id: str, slots: dict) -> None:
    try:
        await redis_client.setex(
            _key(user_id, session_id), SLOTS_TTL, json.dumps(slots, ensure_ascii=False)
        )
    except Exception as e:
        logger.warning(f"[session_slots] 写入会话槽位失败: {e}")


def _msg_field(m, key: str):
    """历史消息可能是 pydantic 对象也可能是 dict, 统一取值"""
    val = getattr(m, key, None)
    if val is None and isinstance(m, dict):
        val = m.get(key)
    return val


async def update_session_slots(user_id: int, session_id: str, query: str, history=None) -> dict:
    """更新会话槽位, 返回最新值。

    分两种情况, 不能一律重放历史:
      - 已有槽位 → **只用当前句**增量更新。否则"再加一天"这种相对说法会被重复累加,
        而且历史里较早的绝对说法("重庆三日游")会把后来改成的城市/天数顶回去。
      - 槽位为空(首次 / 过期) → 才用历史把条件重建出来。

    **只抽 user 说的话**: 助手回复里同样会带城市和天数("给你推荐一个3天玩转杭城的攻略"),
    抽进去等于把 AI 的话记成用户的条件 —— 实测会让"改成成都"被误判成"改行程"而甩出卡片。
    """
    slots = await load_slots(user_id, session_id)
    if slots:
        slots = merge_slots(slots, extract_slots(query))
    else:
        for m in history or []:
            if _msg_field(m, "role") != "user":
                continue
            content = _msg_field(m, "content")
            if content:
                slots = merge_slots(slots, extract_slots(content))
        slots = merge_slots(slots, extract_slots(query))
    await save_slots(user_id, session_id, slots)
    return slots
