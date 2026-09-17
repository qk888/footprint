from typing import Annotated
import contextvars
import json
import re
import random
import asyncio
from langchain.agents import create_agent
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from app.agent.model.factory import supervisor_model, chat_model
from app.agent.utils.prompt_loader import load_prompt
from app.agent.utils.logger_handler import logger
from app.agent.utils.state_utils import last_user_query
from app.agent.middleware import (
    log_before_model, monitor_tool, force_tool_choice,
    harness_route, ANSWER_PREFIX, FAIL_PREFIX, GIVE_UP_MSG,
    SUBAGENT_NO_TOOL_MSG, OUT_OF_SCOPE_MSG,
)
from app.agent.operation.agent import build_operation_agent
from app.agent.operation.graphs.bill_workflow import AutoBillGraph
from app.agent.travel.agent import build_travel_agent
from app.agent.constraints import (
    CURRENT_USER_QUERY as _CURRENT_USER_QUERY,
    PREFERENCE_HINTS as _PREFERENCE_HINTS,
    WRITE_DENIED_MSG,
    reset_turn,
    note_write,
    verify_write_claim,
    strip_fluff_tail,
    format_web_refs,
)
from app.core.city_match import find_cities, alias_notes, find_province, route_destination
from app.services.memories import KIND_LABEL
from app.agent.travel.services.travel_plan_tool import solve, plan_to_frontend, plan_to_text, normalize_city
from app.agent.travel.services.rag_service import NOT_FOUND_TEXT, RagSummaryService
from app.agent.chat.agent import build_chat_agent
from app.agent.intent_vectors import intent_index
from app.core.constants import CITY_ADCODE_MAP, ADCODE_CITY_MAP
from fastapi import HTTPException

# 递归上限：超过就判定死循环，强制停止
RECURSION_LIMIT = 8

# 多轮历史窗口：模型 ctx-size=4096, 要给系统提示/工具定义/回复留空间, 故条数+总字数双限制
HISTORY_MAX_MSGS = 20       # 最多带20条(约10轮)
HISTORY_MAX_CHARS = 1500    # 历史总字数上限(中文约1字≈1token), 超了从最旧的开始丢

# 子agent回复命中这些 → 验收判定为"没解决问题"(harness黑名单)
FAIL_TEXTS = (
    "无法规划", "规划失败", "处理时出了点问题",
    "暂时没找到相关资料",
    "记账失败", "点亮城市失败",
    "查看自己的消费账单失败", "查看自己曾经去过的城市失败",
    SUBAGENT_NO_TOOL_MSG,
    OUT_OF_SCOPE_MSG,
    # 写操作被 harness 闸门否决时会返回这句；模型若照抄，说明这轮没本事解决，
    # 当失败处理（外层摘掉这个子助手换人），别把"需要用户明确要求"当答案甩给用户
    WRITE_DENIED_MSG,
)

# ═══════════ 子 agent 工具（保留，供 gen_finetune_data.py 提取定义用）═══════════

def build_supervisor_tools(travel_agent, operation_agent, chat_agent, exclude=None):
    def _current_query(state: dict) -> str:
        """当前用户问题。**不是 messages[0]** —— 子 agent 的 state 是「历史 + 当前问题」，
        取首条在多轮会话里拿到的是最早那条历史消息（详见 utils/state_utils.py）"""
        return last_user_query(state)

    def _is_fail(text: str) -> bool:
        if not text or not text.strip():
            return True
        return any(t in text for t in FAIL_TEXTS)

    async def _run_subagent(agent, query: str) -> tuple[bool, str]:
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": query}]},
                config={"recursion_limit": RECURSION_LIMIT},
            )
            msgs = result.get("messages", [])
            text = msgs[-1].content if msgs else ""
        except Exception as e:
            logger.error(f"[_run_subagent] 子agent执行异常: {e}")
            return False, f"执行出错: {e}"
        return not _is_fail(text), text

    def _wrap_result(ok: bool, agent_name: str, text: str) -> str:
        if ok:
            return ANSWER_PREFIX + text
        return f"{FAIL_PREFIX}{agent_name}::{text[:100]}"

    @tool(description="处理旅行规划、行程推荐、攻略")
    async def travel_agent_tool(state: Annotated[dict, InjectedState()]) -> str:
        ok, text = await _run_subagent(travel_agent, _current_query(state))
        return _wrap_result(ok, "travel_agent", text)

    @tool(description="处理记账、点亮城市、查账单")
    async def operation_agent_tool(state: Annotated[dict, InjectedState()]) -> str:
        ok, text = await _run_subagent(operation_agent, _current_query(state))
        return _wrap_result(ok, "operation_agent", text)

    @tool(description="处理闲聊、日常对话")
    async def chat_agent_tool(state: Annotated[dict, InjectedState()]) -> str:
        ok, text = await _run_subagent(chat_agent, _current_query(state))
        return _wrap_result(ok, "chat_agent", text)

    tools = [travel_agent_tool, operation_agent_tool, chat_agent_tool]
    if exclude:
        bad = {n + "_tool" for n in exclude}
        tools = [t for t in tools if t.name not in bad]
    return tools


# ═══════════ 纯文本意图分类（替代 supervisor tool calling，避免 peg-native 500）═══════════

CLASSIFY_PROMPT = """你是意图分类器。判断用户这句话属于哪一类，只输出一个词：travel、operation 或 chat，不要输出其他内容。

分类规则：
- travel：旅行规划、行程安排、攻略、景点/美食推荐、去哪玩、怎么玩
- operation：记账、记消费、花了多少钱、点亮城市、去过哪里、查账单、查消费、记住偏好、回忆偏好
- chat：其他闲聊、日常对话、寒暄、问候、问问题但不属于上面两类

用户：{query}
分类："""

# 强关键词：命中直接判定，不用问模型（小模型对这些场景分类不稳）
STRONG_OPERATION_KEYWORDS = [
    "记账", "记消费", "入账", "花了", "花钱", "消费", "花费",
    "住宿", "吃饭", "打车", "门票", "机票", "车票", "酒店",
    "点亮", "去过", "足迹", "账单", "查账", "花多少",
    "记住", "回忆", "偏好", "习惯",
    "交通费", "支出",
]
STRONG_TRAVEL_KEYWORDS = [
    "规划", "行程", "攻略", "景点", "美食推荐", "去哪玩", "怎么玩",
    "玩法", "玩几天", "预算", "推荐景点", "推荐美食",
]
# "旅游/旅行"太宽：中文里大量出现在闲聊里("刚旅游回来"/"旅游太累了"/"好喜欢旅行")。
# 单独出现就判 travel，会让闲聊跑去做行程规划。只有配上动作词才算真的想安排去玩。
# (实测 199 条路由样本里 14 条错判都出自这两个词)
WEAK_TRAVEL_WORDS = ("旅游", "旅行")
_RE_TRAVEL_ACTION = re.compile(
    r"想去|要去|打算去|准备去|计划去|怎么去|去哪儿|去哪玩|去哪好|去哪"
    r"|安排|规划|计划|几天|几日|行程|路线"
)
# 没提"旅游/旅行"两个字、但明显在问怎么玩的: "怎么去西藏玩" / "都江堰怎么逛"
# 只认**疑问语境** —— "要去X玩"这种陈述句交给模型判断,
# 免得把"过几天要去哈尔滨玩"这类硬拉去出行程（它可能只是随口说说）
_RE_TRAVEL_PLAY = re.compile(r"(?:怎么|如何|哪里|哪儿|去哪).{0,6}(?:玩|逛|游)")


def _travel_by_weak_word(query: str) -> bool:
    """"旅游/旅行"要配上动作词才算规划意图, 光说"旅游"多半是闲聊"""
    if not any(w in query for w in WEAK_TRAVEL_WORDS):
        return False
    return bool(_RE_TRAVEL_ACTION.search(query))


def _looks_like_travel_play(query: str) -> bool:
    """没提"旅游/旅行"两个字, 但在问怎么玩 ("怎么去西藏玩")"""
    return bool(_RE_TRAVEL_PLAY.search(query))


def _mentions_travel_word(query: str) -> bool:
    """提了"旅游/旅行"但没配动作词 —— 中文里这多半是闲聊。

    这类**必须直接判 chat**，不能交给模型：实测把"刚旅游回来"/"旅游太累了"丢给
    1.5B，它看到"旅游"两个字仍然会选 travel，然后跑去做行程规划、回一句"你要去哪个城市"。
    规则治不了（词太宽），模型也接不住 —— 只能在这一层明确钉死。
    """
    return any(w in query for w in WEAK_TRAVEL_WORDS)


# 攻略类词：和"旅游/旅行"同时出现时，说明是在要攻略 —— 不能因为"没配动作词"就钉成闲聊。
# 199 条回归里 "南京旅游避雷" / "去珠海旅游有什么注意事项吗" / "给我一些南宁旅游的避坑指南"
# 都是期望 travel 却被一律钉成了 chat。
# 只用**明确的攻略信号**。别放"推荐/值得/好玩/有什么"这类泛化词 ——
# 它们会把"你觉得国外旅行值得吗""你推荐什么旅行方式啊"这类闲聊也拉成 travel。
_RE_GUIDE_HINT = re.compile(
    r"避雷|避坑|注意事项|指南|攻略|必去|必吃"
    r"|有什么好玩|有什么好吃|有什么景点|哪里好玩|怎么玩|什么时候去"
)


def _travel_word_with_guide(query: str) -> bool:
    """"旅游/旅行" + 攻略词 → 真的在要攻略"""
    if not any(w in query for w in WEAK_TRAVEL_WORDS):
        return False
    return bool(_RE_GUIDE_HINT.search(query))


# "刚从X回来" / "刚去了一趟X" / "刚来到X" —— 这是**记足迹**（operation），不是 travel。
# 语义上和"想去X玩"很近，字面又踩不到关键词表，所以以前全靠猜（向量也常猜成 travel）。
_RE_FOOTPRINT = re.compile(
    r"刚从.{1,8}回来"
    r"|刚去了一趟"
    r"|刚来到[\u4e00-\u9fa5]{2,6}"
    r"|刚到[\u4e00-\u9fa5]{2,4}(了|，|,|！|!|\s|$)"
    r"|上个月去了"
    r"|前几天在.{1,6}玩"
    r"|旅行地图|足迹地图"
)


def _looks_like_footprint(query: str) -> bool:
    """足迹记录/查询 —— 确定性的 operation 信号，交给代码判，不猜"""
    return bool(_RE_FOOTPRINT.search(query))
# 记账直达关键词：命中就走直达通道（不用 operation 子 agent 的 tool calling）
DIRECT_BILL_KEYWORDS = [
    "花了", "花钱", "消费", "花费", "住宿", "吃饭", "打车", "门票",
    "机票", "车票", "酒店", "记账", "记消费", "入账", "花多少",
]
# 金额+消费名词的组合兜底：用户说法千变万化，只靠上面的关键词表会漏。
# 实测漏过「我在重庆出行新增了一笔100交通费」→ 落到闲聊，还复读了卡片话术。
BILL_HINT_KEYWORDS = [
    "交通费", "交通", "出行", "油费", "停车", "餐费", "餐饮", "住宿费",
    "购物", "支出", "费用", "付款", "付了", "买了", "一笔", "消费了", "交了",
]
_AMOUNT_RE = re.compile(r"\d")


def _looks_like_bill(query: str) -> bool:
    """记账意图判定：明确消费词直接命中；否则要求「有数字 + 消费名词」同时成立，
    避免把「北京到杭州三日游预算4000」这类规划问题误判成记账。"""
    if any(kw in query for kw in DIRECT_BILL_KEYWORDS):
        return True
    return bool(_AMOUNT_RE.search(query)) and any(kw in query for kw in BILL_HINT_KEYWORDS)


# 账单查询关键词: "查账 / 我花了多少钱 / 账单一共多少 / 消费明细"
# 以前这类问题没人接, 落到 operation 子 agent 的 tool calling 上 ——
# 实测「查账」空转 30 秒答非所问, 「我的账单一共多少」还把内部错误"未找到的城市编码"直接吐给用户。
# 数字类问题代码直接算最快也最准, 不需要模型参与。
BILL_QUERY_KEYWORDS = (
    "查账", "看账", "我的账单", "账单一共", "账单多少", "花了多少钱", "花多少钱",
    "花了多少", "消费多少", "一共花", "总共花", "一共多少", "总共多少",
    "总花费", "消费明细", "统计一下",
)


# "一共花了300" 这种又带查询词、又带金额的，是记账不是查询 —— 别被"一共多少"这类词骗走
_RE_SPEND_WITH_NUM = re.compile(r"(?:花了|消费了?|花费|付了|买了|记一笔)\s*\d")


def _looks_like_bill_query(query: str) -> bool:
    """账单**查询**（不是记账）。跟 _looks_like_bill 互斥，判定顺序上要放在它前面 ——
    "我花了多少钱"含"花了"，不先截住会被记账通道当成一笔消费。"""
    if not any(kw in query for kw in BILL_QUERY_KEYWORDS):
        return False
    return not _RE_SPEND_WITH_NUM.search(query)


# ---------- 行程花费追问 ----------
# "花费是不是太少了 / 这个行程花了多少钱 / 预算用完了吗" —— 问的是**刚排出来的那张行程**，
# 不是"我记账记了多少"。以前这类句子有两种坏下场：
#   ① 判成 operation → 1.5B 走 tool calling 空转，最后吐出"要记账/点亮城市请告诉我"的固定话术
#   ② 被账单通道接走 → 答成"你还没有记账记录"
# 两种都是答非所问（用户实测就是这么被回的）。所以给它一个**直达通道**：
# 用槽位里记下的行程花费直接算，确定性、零延迟。
_RE_PLAN_COST_ASK = re.compile(r"花费|费用|开销|花销|预算|总价|多少钱|花了")
_RE_PLAN_COST_TONE = re.compile(r"少|多|够|超|剩|用|怎么|为什么|是不是|吗|多少|划算|合算")
# 明显在说"记账记录"的说法，让给账单通道：那是查账目，不是问这张行程
_RE_BILL_RECORD = re.compile(r"记账|账单|明细|记录|我花|我今天|昨天花|这周花|这个月花")


def _fmt_money(amount: float) -> str:
    """50.0 → 50，49.5 → 49.5"""
    return f"{amount:.2f}".rstrip("0").rstrip(".")


# "重庆玩七天" / "重庆待十天" / "重庆六日游"：有城市 + 有天数，却踩不到关键词表
# （那边是"两天/三日/四日/五日/一日游"这种枚举，中文数字必然漏）。这类句子以前
# 全靠小模型判意图，实测同一个输入两次能给出不同结果。
_RE_DAYS_ANY = re.compile(r"[一两二三四五六七八九十\d]{1,2}\s*[天日]")
# 周也当时间量词："玩一周" / "待两周"
_RE_WEEK_ANY = re.compile(r"[一两二三四五六七八九\d]{1,2}\s*个?\s*周")


def _looks_like_travel_plan(query: str) -> bool:
    """有城市 + 有天数/周数 → 就是规划意图（不调模型，判定确定）。
    要求含城市是关键：免得"玩七天"这种没头没尾的话也被当成规划。"""
    if not (_RE_DAYS_ANY.search(query) or _RE_WEEK_ANY.search(query)):
        return False
    return any(name in query for name in CITY_ADCODE_MAP)


def _travel_evidence_in_query(query: str) -> bool:
    """**这句话自己**带没带"我要出行"的证据：城市/景区别名/省名、天数、旅行词。

    为什么要单独判这个：会话槽位是跨轮存活的（Redis 里活 7 天），而**写槽位的消息
    不一定是出行** —— 实测「帮我点亮三亚」会把 target_city=三亚 写进槽位，
    紧接着一句毫不相干的「从北极到撒哈拉观海」被判成 travel 后，
    直接拿旧槽位排了一份**三亚一日游**卡片。用户根本没说要出行，却收到行程。
    这就是槽位变成了"暗写"：没写数据库，但凭空产出了用户没要的东西。

    规则：句子自己没有出行要素、且上一轮助手也没在问行程 → 不许排（见 execute_stream）。
    """
    q = query or ""
    if _is_bare_number(q):                                   # "4"（回答"玩几天"的那种）
        return True
    if _RE_DAYS_ANY.search(q) or _RE_WEEK_ANY.search(q):      # 天数/周数
        return True
    if any(k in q for k in TRAVEL_PLAN_KEYWORDS) or any(k in q for k in TRAVEL_GUIDE_KEYWORDS):
        return True
    if find_cities(q) or alias_notes(q) or find_province(q):  # 城市 / 景区 / 省名
        return True
    return False

# 点亮/查询城市直达关键词：城市名固定可直接字符串匹配, 无需模型 tool calling
DIRECT_LIGHT_KEYWORDS = ["点亮", "去过", "到过", "打卡", "足迹", "地图"]
# 其中含这些词表示"查询去过哪些城市", 而不是点亮
# (查询词写具体些: "足迹"只负责进通道, 判查询要靠"旅行足迹/我的足迹"这类完整说法, 免得"点亮足迹"被误判)
LIGHT_QUERY_KEYWORDS = ["哪些", "哪里", "哪儿", "几个", "列表", "都有", "有没有", "多少个",
                        "我的足迹", "旅行足迹", "足迹地图", "地图", "什么样", "是什么"]
# travel 内部二次分流: 行程规划(出时间轴卡片) vs 攻略问答(走RAG知识库)
TRAVEL_PLAN_KEYWORDS = [
    "规划", "行程", "计划", "几日游", "几天", "安排", "路线", "玩几天",
    "天几夜", "两日", "三日", "四日", "五日", "一日游", "预算",
]
TRAVEL_GUIDE_KEYWORDS = [
    "景点", "美食", "攻略", "推荐", "有什么好玩", "好玩的", "好吃",
    "注意事项", "避雷", "特产", "怎么玩", "去哪玩", "哪里玩",
    # 下面这些是"问信息"，不是"要一份行程"：以前一个都不命中 → 掉进规划分支 →
    # 卡在"你想去哪个城市/玩几天"，用户问"喀纳斯几月去最好"会被反问城市
    "几月", "什么时候去", "什么季节", "最佳", "最合适",
    "天气", "冷不冷", "热不热", "穿什么",
    "怎么去", "怎么走", "多远",
    "门票", "票价", "多少钱", "贵不贵", "费用",
    "值得", "好玩吗", "好吃吗", "怎么样",
]

# 问"旅游信息"的强信号，判定要排在 STRONG_OPERATION_KEYWORDS 之前：
# "门票"同时在强 operation 表里(记账用)，于是"洪崖洞门票多少"被判成 operation
# → 走 operation 子 agent 调 get_bills → 回一堆账目汇总(实测)。
# 区分办法：带数字的才是记账("重庆门票花了200")，不带数字的是问信息。
TRAVEL_INFO_KEYWORDS = (
    "门票", "票价", "几月", "什么时候去", "什么季节", "天气",
    "冷不冷", "热不热", "穿什么", "怎么去", "怎么走", "值得去", "值得吗",
)
# 明确在"要记账"的说法：即使含上面的信息词也不算问攻略
_REMEMBER_VERBS = ("记一笔", "记账", "记一下", "记下来", "帮我记", "入账")
# "保存行程"类请求: agent不持有上一张卡片、小模型会杜撰景点和金额, 故拦截并引导用卡片按钮
# 注意要覆盖"你给我写进去"这种口语说法, 否则会落到闲聊层, 闲聊会顺着上下文复读卡片话术
SAVE_PLAN_KEYWORDS = [
    "写进", "录进", "记进", "补进", "存进", "存一下", "存起来",
    "保存行程", "保存规划", "保存这", "保存一下", "帮我保存", "帮我写", "你给我写",
    "记下来", "记录下来", "收藏",
]

# ---- "把这张行程加入/更新到我的规划"直达通道 ----
# 上面那张表**覆盖不到口语说法**：实测"给我这个加入规划中且更新""把这份行程加入规划"
# 一个关键词都不命中 → 被判成 travel（含"规划"）→ 走规划支 → **又重排一张新卡片**：
# 用户要的是保存现在这张，拿到的却是"另一张新行程"，而且新卡片还会把他看中的那版覆盖掉。
# 现在有了上一张卡片的 payload（save_last_plan），就能真的落库了 —— 走和前端按钮同一个
# PlanService.set_plan。
SAVE_PLAN_TARGETS = ("规划", "行程", "计划")
SAVE_PLAN_SAVE_VERBS = ("加入", "加进", "加到", "存到", "存进", "保存", "写进", "录进",
                        "补进", "同步", "更新", "收藏")
# "改一版"的说法不算保存（那些走改行程通道）
_SAVE_PLAN_NOT = ("改成", "换成", "重新规划", "再规划", "重排", "调整", "换个")

# 保存方式：显式说要"新建/另存"就存成单独一份，说"覆盖/替换/更新"就覆盖原规划。
# 都没说时由调用方按"这个城市有没有旧的"决定（有→覆盖，没有→新建，兼容老行为）。
# 注意"新建"优先 —— "不要覆盖，另存一份"这种两个词都有的句子要按新建算。
SAVE_PLAN_NEW_WORDS = ("新建", "另存", "新的一份", "新一份", "再来一份", "加一份",
                       "保留原", "不要覆盖", "别覆盖")
SAVE_PLAN_OVERWRITE_WORDS = ("覆盖", "替换", "顶掉", "更新", "改掉")
# ===== 修改行程直达通道 =====
# "改成成都" / "换成杭州两天" 这类话本身不含"规划/行程"关键词, 以前会落到闲聊被反问。
# 其实会话槽位里已经有完整条件(城市+天数), 只要给它一个触发信号就能重跑规划。
REVISE_PLAN_SIGNALS = (
    "改成", "改为", "换成", "换到", "改到", "变成", "改一下", "调整一下", "调整成",
    "重新规划", "再规划", "重新安排", "重新来", "换个城市", "换个地方", "换座城市",
)
#"再加一天"/"少玩两天" 只认天数, 免得"再加一个景点"也触发重规划
_RE_REVISE_DAYS = re.compile(
    rf"(?:再|多|加|增加|延长|减|少|去掉|缩短)\s*(?:玩|去)?\s*[一两二三四五六七八九十\d]{{1,2}}\s*[天日]"
)
# 命中"改"的意思但会话里还没条件可改时, 确认改意并追问缺的条件。
# 不交给闲聊: 本地 1.5B 会顺着历史编出一份假行程(实测编过"5天畅游成都, 从第二天开始")
REVISE_ASK_DAYS_REPLIES = (
    "好，去{c}。玩几天？告诉我天数，我马上给你排一份行程。",
    "行，改{c}。几天？顺便说下预算，我直接排一版给你看。",
    "收到，换成{c}。给我个天数（比如 3 天），我就开工。",
    "可以，{c}。玩几天？说个天数我就排。",
)
REVISE_ASK_CITY_REPLIES = (
    "行，那你想去哪儿？把城市和天数告诉我，我给你排一份行程。",
    "好，换成哪儿？告诉我城市和天数，我直接排一版。",
    "可以～不过得先知道目的地：城市加天数，我就能开工。",
)

# ===== 记忆直达通道 =====
# 明确吩咐("记住X")→ 直接生效; 随口说的偏好 → 只存候选, 等用户在「我的」页确认
REMEMBER_PREFIXES = ("帮我记住", "帮我记一下", "帮我记下来", "帮我记", "你要记住", "你记住", "记住", "记一下", "记下来", "记下")
# PREFERENCE_HINTS / CURRENT_USER_QUERY 已挪到 harness 约束层（constraints.py）：
# 写工具（含记忆工具）的授权核对要用同一份词表，两处各写一遍迟早改歪。
# 这里保持同名导入，老代码（本文件 + 各工具的 `from app.agent.footprint_agent import ...`）不受影响。
PREFERENCE_HINTS = _PREFERENCE_HINTS
CURRENT_USER_QUERY = _CURRENT_USER_QUERY
# 出现这些词说明用户在提问/要行程, 不能被"我喜欢"这类字样误判成偏好
# "什么/哪些/啥/哪/多少"是疑问词: "我喜欢什么"是在问, 不是要把"什么"记成偏好
MEMORY_SKIP_HINTS = ("规划", "行程", "攻略", "怎么玩", "去哪", "几天", "几日", "预算", "推荐",
                     "吗", "？", "?", "什么", "哪些", "啥", "哪", "多少")

# 记忆查询关键词: "我有什么偏好 / 你记得我什么 / 我的记忆" —— 跟"记住X"是反方向。
# 同样不能交给 operation 子 agent 的 tool calling(实测"我有什么偏好"空转 30 秒还答非所问),
# 而且"我喜欢什么"含"我喜欢", 不先截住还会被当成一条待写偏好存进去。
MEMORY_QUERY_KEYWORDS = (
    "我的偏好", "我有什么偏好", "我的习惯", "我的口味", "我的忌口", "我的记忆",
    "你记得我", "记得我什么", "我记忆", "记忆里", "我的资料", "我的信息",
    "我喜欢什么", "我不吃什么", "我住哪", "我住哪儿", "你知道我什么",
)


def _looks_like_memory_query(query: str) -> bool:
    """用户是在**问**"你记了我什么", 不是要写入新偏好"""
    return any(kw in query for kw in MEMORY_QUERY_KEYWORDS)


# 记忆回执话术: 固定一句话回复会显得像机器人复读, 随机挑一条(纯本地模板, 不调模型, 保证可靠)
REMEMBER_REPLIES = (
    "好，记住了：{c}",
    "记下了：{c}",
    "嗯，已记住 —— {c}",
    "行，我记着：{c}",
    "收到，记住了：{c}",
    "好的，{c} 这条我记下了",
)
PENDING_REPLIES = (
    "记下了：{c}。到「我的」页点一下确认，以后每次对话都会带上它。",
    "好，先记着「{c}」—— 去「我的」页确认一下就长期生效。",
    "收到：{c}。这条还算候选，到「我的」页点确认我就一直记得了。",
    "嗯，「{c}」我记下了，去「我的」页确认之后它会跟着每次对话。",
    "先收下：{c}。确认按钮在「我的」页，点了才算长期生效。",
    "这条我留意到了：{c}。在「我的」页确认一下，以后就不用重复说了。",
)
# 重复说同一条: 明确告诉用户"早记过了", 别装作第一次听到
EXISTS_REPLIES = (
    "这条我已经记下了：{c}，不用再说一遍啦。",
    "「{c}」我记着呢 —— 再说一遍也还是这条，放心。",
    "已经有了：{c}，我一直带着这条。",
)
EXISTS_PENDING_REPLIES = (
    "这条我之前就记下了：{c}。还差一步 —— 在「我的」页点确认就长期生效。",
    "「{c}」我早记过了，不过它还在「我的」页等确认，点一下就不用再重复说了。",
)
REVIVED_REPLIES = (
    "这条我以前记过（后来被新说法顶掉了），现在又给你翻回来了：{c}",
    "「{c}」重新生效了 —— 之前那条已被新说法取代，这条我调回生效中。",
)
UPDATED_REPLIES = (
    "更新了：{c} —— 旧的那条说法我自动标成失效了。",
    "换成新的了：{c}，之前那条旧说法已经作废。",
)


# 上一次的回执, 连续两条别用一模一样的话
_last_reply = ""


def _pick_reply(templates: tuple[str, ...], content: str) -> str:
    global _last_reply
    text = random.choice(templates).format(c=content)
    for _ in range(3):
        if text != _last_reply:
            break
        text = random.choice(templates).format(c=content)
    _last_reply = text
    return text


# 裸数字/纯天数：追问"玩几天"之后用户常直接答"4"或"四天"
_BARE_NUMBER_RE = re.compile(
    r"^\s*(?:[0-9]{1,3}|[一二两三四五六七八九十]{1,3})\s*(?:天|日|晚|人|天左右|左右)?\s*$"
)
# 最近的助手回复里出现这些词 = 上一轮在聊行程（用于判断裸数字该接回哪条路）
TRAVEL_CONTEXT_HINTS = ("行程", "城市", "几天", "几日", "预算", "攻略", "景点", "规划")


def _is_bare_number(query: str) -> bool:
    return bool(_BARE_NUMBER_RE.match(query or ""))


def _extract_memory_command(query: str) -> tuple[str, bool]:
    """识别"要不要记这条记忆"。返回 (内容, 是否只当候选)。
    - "记住我不吃辣" → (我不吃辣, False) 用户明确吩咐, 直接生效
    - "我习惯坐靠窗的位置" → (原句, True) 顺口一提, 只当候选, 待用户在「我的」页确认
    """
    q = (query or "").strip()
    for p in REMEMBER_PREFIXES:
        if q.startswith(p):
            rest = q[len(p):].lstrip("：:，,。 　")
            return (rest, False) if rest else ("", False)
    if 3 <= len(q) <= 60 and any(k in q for k in PREFERENCE_HINTS):
        if any(k in q for k in MEMORY_SKIP_HINTS):
            return "", False
        if _looks_like_bill(q):         # "我习惯在重庆吃饭花50" 这类先归记账
            return "", False
        return q, True
    return "", False

# 记账参数提取提示词（纯文本JSON，不依赖 tool calling，不会 peg-native 500）
BILL_EXTRACT_PROMPT = """从用户这句话中提取消费信息，只输出JSON，不要输出任何其他内容、解释或markdown标记。

JSON格式：{"city": "城市名", "bills": [{"amount": 金额数字, "category": 分类编号}]}

分类编号：
1=交通（打车、公交、地铁、飞机、火车、车票、机票）
2=餐饮（吃饭、早餐、午餐、晚餐、咖啡、奶茶、火锅）
3=购物（买东西、商品、纪念品）
4=住宿（酒店、民宿、住宿）
5=娱乐（门票、电影、游玩、景区）

如果用户没说城市名，city填""。每笔消费一个对象。

示例：
用户：在重庆吃饭花了50，打车30
输出：{"city": "重庆", "bills": [{"amount": 50, "category": 2}, {"amount": 30, "category": 1}]}
用户：上海酒店500
输出：{"city": "上海", "bills": [{"amount": 500, "category": 4}]}
用户：门票90
输出：{"city": "", "bills": [{"amount": 90, "category": 5}]}
用户：我在重庆出行新增了一笔100交通费
输出：{"city": "重庆", "bills": [{"amount": 100, "category": 1}]}

用户：__QUERY__
输出："""


async def classify_intent(query: str, exclude: set | None = None) -> str:
    """纯文本分类，返回 travel/operation/chat。
    强关键词优先匹配（记账/旅行场景关键词明确，比小模型可靠），
    模糊情况才问模型。不依赖 tool calling，不会 peg-native 500。
    """
    q = query.lower()

    # 0. 问旅游信息优先（"门票/票价/几月/怎么去"在记账表里也有同名词，先拦一道）
    if (any(kw in q for kw in TRAVEL_INFO_KEYWORDS)
            and not _AMOUNT_RE.search(q)
            and not any(v in q for v in _REMEMBER_VERBS)):
        intent = "travel"
        logger.info(f"[classify_intent] query='{query[:30]}' → 旅游信息词(无金额) → travel")
    # 1. 强关键词优先
    elif any(kw in q for kw in STRONG_OPERATION_KEYWORDS) or _looks_like_footprint(q):
        intent = "operation"
        logger.info(f"[classify_intent] query='{query[:30]}' → 关键词/足迹命中 operation")
    elif any(kw in q for kw in STRONG_TRAVEL_KEYWORDS):
        intent = "travel"
        logger.info(f"[classify_intent] query='{query[:30]}' → 关键词命中 travel")
    elif _travel_by_weak_word(q):
        # "想去旅游" 这类要靠动作词才认, 光有"旅游/旅行"不算(那是闲聊常用词)
        intent = "travel"
        logger.info(f"[classify_intent] query='{query[:30]}' → 旅游/旅行+动作词 → travel")
    elif _looks_like_travel_play(q):
        # "怎么去西藏玩": 没提"旅游/旅行", 但明显在问怎么玩
        intent = "travel"
        logger.info(f"[classify_intent] query='{query[:30]}' → 问怎么玩 → travel")
    elif _looks_like_travel_plan(q):
        # "重庆玩七天"/"重庆六日游"/"去重庆玩一周" 这类踩不到关键词表的, 用"城市+天数"兜底。
        # 不兜的话要靠小模型判, 实测同一句话两次能给出不同结果
        intent = "travel"
        logger.info(f"[classify_intent] query='{query[:30]}' → 城市+天数 → travel")
    elif _travel_word_with_guide(q):
        # "南京旅游避雷"/"去珠海旅游有什么注意事项吗"：提了旅游/旅行 + 攻略词 → 真的要攻略
        intent = "travel"
        logger.info(f"[classify_intent] query='{query[:30]}' → 旅游/旅行+攻略词 → travel")
    elif _mentions_travel_word(q):
        # 提了"旅游/旅行"、既没动作词也没攻略词 → 闲聊("刚旅游回来"/"好喜欢旅行")
        # 直接钉死, 不给模型机会(给了它会判 travel)
        intent = "chat"
        logger.info(f"[classify_intent] query='{query[:30]}' → 只提旅游/旅行 → chat")
    else:
        # 2. 向量语义匹配（替代生成模型）
        #    199 条人工标注实测：规则不命中的长尾段，向量 73.1% vs 生成模型 50.7%；
        #    且"向量 + 模型兜底"反而更低（70.1% < 73.1%）—— 模糊时也信向量，不问模型。
        try:
            vec_label, gap, score = await intent_index.classify(query)
            if vec_label:
                intent = vec_label
                logger.info(
                    f"[classify_intent] query='{query[:30]}' → 向量={intent}({score:.3f},分差{gap:.3f})"
                )
            else:
                # 三类都不像（最高相似度低于阈值）→ 闲聊最安全（无副作用）
                intent = "chat"
                logger.info(f"[classify_intent] query='{query[:30]}' → 都不像({score:.3f}) → chat")
        except Exception as e:
            # 降级：只在向量服务不可用（embed-model 挂了）时才退回生成模型
            logger.warning(f"[classify_intent] 向量分类失败({e}), 降级生成模型")
            intent = "chat"
            try:
                resp = await chat_model.ainvoke(CLASSIFY_PROMPT.format(query=query))
                text = (resp.content if hasattr(resp, "content") else str(resp)).strip().lower()
                first = text.split()[0] if text.split() else text
                first = first.strip(".,;:!?。，；：！？\"'")
                if first in ("travel", "operation", "chat"):
                    intent = first
                logger.info(f"[classify_intent] query='{query[:30]}' → model='{text[:30]}' → {intent}")
            except Exception as e2:
                logger.warning(f"[classify_intent] 生成模型也失败({e2}), 默认 chat")
                intent = "chat"

    # 被摘掉的子助手不能再选
    if exclude and intent in exclude:
        # chat无副作用优先兜底; operation有记账/点亮等写操作, 放最后, 避免别的agent失败时误写数据库
        remaining = [x for x in ("chat", "travel", "operation") if x not in exclude]
        intent = remaining[0] if remaining else "chat"
        logger.info(f"[classify_intent] 原选被摘掉, 改选 {intent}")
    return intent


class FootprintAgent:
    _rag_service: "RagSummaryService | None" = None  # RAG服务复用单例(内部建向量连接, 避免每请求重建)

    def __init__(self, city_service, trip_service, bill_service, user_id: int, memory_service=None, memory_block: str = "", session_slots: dict | None = None, session_id: str | None = None, plan_service=None):
        self.city_service = city_service
        self.trip_service = trip_service
        self.bill_service = bill_service
        self.user_id = user_id
        self.memory_service = memory_service
        # 规划落库服务：用户在对话里说"把这张加入规划"时要真写进 plans 表
        # （和前端「保存到规划」按钮走同一个 PlanService.set_plan）
        self.plan_service = plan_service
        # 会话级结构化槽位(城市/天数/预算/人数), 由路由层规则抽取后传进来
        self.session_slots = session_slots or {}
        # 槽位所在的会话 key: 要把"上一次走的是攻略还是规划"写回 Redis 时要用
        self.session_id = session_id
        # 本轮写入台账（L2 取证）。由 reset_turn 在每轮开头换成新列表，
        # 出话术前 L3 用它核对"声称写了"是否真有写入 —— 直达通道和工具都往里记。
        self._turn_log: list = []
        self.travel_agent = build_travel_agent(memory_block)
        self.operation_agent = build_operation_agent(
            city_service, trip_service, bill_service, user_id, memory_service, memory_block
        )
        self.chat_agent = build_chat_agent(memory_block)
        # RAG攻略服务(懒加载单例, 复用向量连接)
        if FootprintAgent._rag_service is None:
            FootprintAgent._rag_service = RagSummaryService()
        self.rag_service = FootprintAgent._rag_service
        # 记账直达：绕过 operation 子 agent 的 tool calling，直接用 LLM 提取参数 + AutoBillGraph
        self.bill_graph = AutoBillGraph(city_service, trip_service, bill_service)
        # 完整版 supervisor（tool calling 选子助手的旧链路）**懒构建**：
        # 生产链路已换成 classify_intent 纯文本三分类，这个 agent 只留给脚本/实验用
        # （比如 gen_finetune_data.py 提取工具定义）。原先写在 __init__ 里，
        # 而 FootprintAgent 是**每请求新建**的 → 每个请求都白建一个 agent + 读一次提示词。
        self._supervisor = None
        self._agents = {
            "travel": (self.travel_agent, "travel_agent"),
            "operation": (self.operation_agent, "operation_agent"),
            "chat": (self.chat_agent, "chat_agent"),
        }

    @property
    def supervisor(self):
        """完整版 supervisor：意图路由走 tool calling 的旧链路（首次访问才构建）

        现在是死路一条 —— 生产用 `classify_intent` 纯文本分类（小模型 tool_choice 不稳，
        会 peg-native 500）。保留它是因为 `build_supervisor_tools` 还被脚本用来提取工具定义，
        想做对比实验时也能直接把这个 agent 拿出来跑。**懒构建**：不访问就不建。
        """
        if self._supervisor is None:
            self._supervisor = create_agent(
                model=supervisor_model,
                tools=build_supervisor_tools(self.travel_agent, self.operation_agent, self.chat_agent),
                system_prompt=load_prompt("intent"),
                middleware=[log_before_model, monitor_tool, force_tool_choice, harness_route],
            )
        return self._supervisor

    def _is_fail(self, text: str) -> bool:
        if not text or not text.strip():
            return True
        return any(t in text for t in FAIL_TEXTS)

    async def _extract_bills(self, query: str) -> dict | None:
        """用 LLM 纯文本提取消费明细（JSON），不依赖 tool calling。"""
        prompt = BILL_EXTRACT_PROMPT.replace("__QUERY__", query)
        try:
            resp = await chat_model.ainvoke(prompt)
            text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
            # 清理 markdown 代码块标记
            text = text.replace("```json", "").replace("```", "").strip()
            # 取第一个 { 到最后一个 }
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end + 1]
            data = json.loads(text)
            logger.info(f"[_extract_bills] 提取结果: {data}")
            return data
        except Exception as e:
            logger.warning(f"[_extract_bills] 提取失败: {e}, 原始输出: {text[:100]}")
            return None

    async def _direct_add_bill(self, query: str, history: list[dict] | None = None) -> tuple[bool, str]:
        """记账直达通道：提取参数 → AutoBillGraph 记账。返回 (是否成功, 回复文本)。"""
        data = await self._extract_bills(query)
        if not data or not data.get("bills"):
            return False, "没能识别出消费明细，请换个说法，比如'在重庆吃饭花了50，打车30'"

        city_name = data.get("city", "").strip()
        bills = data.get("bills", [])
        # LLM 没提取出城市名时，先从当前句匹配, 再往历史对话里找（联系上下文补全城市）
        if not city_name:
            search_texts = [query]
            for m in reversed(history or []):
                if m.get("role") == "user":
                    search_texts.append(m.get("content", ""))
            for text in search_texts:
                hit = find_cities(text)          # 最长匹配优先, 避免"马鞍山"被认成"鞍山"
                if hit:
                    city_name = hit[0]
                    logger.info(f"[_direct_add_bill] LLM漏提取城市, 匹配到: {city_name}")
                    break
        # 句子里和历史里都没有城市 → 用会话槽位里记住的城市(跨轮/跨窗口都还在)
        if not city_name and self.session_slots.get("target_city"):
            city_name = self.session_slots["target_city"]
            logger.info(f"[_direct_add_bill] 用会话槽位补城市: {city_name}")
        # 校验 bills 格式
        clean_bills = []
        for b in bills:
            try:
                amount = float(b.get("amount", 0))
                category = int(b.get("category", 2))
                if amount > 0 and 1 <= category <= 5:
                    clean_bills.append({"amount": amount, "category": category})
            except (ValueError, TypeError):
                continue

        if not clean_bills:
            return False, "没能识别出消费金额，请明确说花了多少钱"

        if not city_name:
            return False, "请告诉我在哪个城市消费的，比如'在重庆吃饭花了50'"

        try:
            result = await self.bill_graph.run(
                user_id=self.user_id, city_name=city_name, bills=clean_bills,
            )
            logger.info(f"[_direct_add_bill] 记账结果: {result}")
            # AutoBillGraph 返回的 result 可能包含成功/失败信息
            if "失败" in str(result) or "未找到" in str(result):
                return False, str(result)
            total = sum(b["amount"] for b in clean_bills)
            # L2 取证：直达通道也是写操作，必须留台账 —— 出话术前 L3 拿它核对
            # "说了已记账"是否真有记账（见 constraints.note_write）。
            note_write("add_bill", "ok", f"{city_name} {len(clean_bills)}笔")
            # AutoBillGraph 的 result 已包含"在X记账N笔共Y元成功"，直接用
            return True, str(result)
        except Exception as e:
            logger.error(f"[_direct_add_bill] 记账异常: {e}", exc_info=True)
            return False, f"记账失败: {e}"

    @staticmethod
    def _extract_cities(text: str) -> list[str]:
        """从文本里匹配所有支持的城市名（城市表固定，直接字符串匹配，不靠模型生成）。
        按城市名在原文中出现的先后顺序返回，更符合用户表达。
        走 core/city_match.find_cities：既过邻字判据，又保证最长匹配优先
        （否则"马鞍山"会连"鞍山"一起点亮）。"""
        return find_cities(text)

    async def _direct_light_city(self, query: str) -> tuple[bool, str]:
        """点亮/查询城市直达通道：字符串匹配城市名 → 直接写库, 绕过 tool calling, 秒回。"""
        # 1) 查询类："我去过哪些城市"
        if any(kw in query for kw in LIGHT_QUERY_KEYWORDS):
            try:
                adcodes = await self.city_service.get_lighted_cities(self.user_id)
                names = [ADCODE_CITY_MAP[a] for a in adcodes if a in ADCODE_CITY_MAP]
                # L2 取证：这不是"这次写入"，但回话里的"你已经点亮过这些城市"是有库依据的事实，
                # 记一条 already 让 L3 的声称校验不至于把正经查询答复误判成编造。
                note_write("light_city", "already", "查询现有点亮")
                if names:
                    return True, f"你已经点亮过这些城市：{'、'.join(names)}"
                return True, "你还没有点亮过任何城市，告诉我你去过哪里，我帮你点亮"
            except Exception as e:
                logger.error(f"[_direct_light_city] 查询失败: {e}")
                return False, "查询城市失败"

        # 2) 点亮类：直接从原文匹配城市名
        cities = self._extract_cities(query)
        if not cities:
            return False, "没识别到城市名"  # 交回正常流程, 让子agent引导用户说城市

        # 省名归一："点亮海南"如果直接查 CITY_ADCODE_MAP 会命中青海省海南藏族自治州(632500),
        # 实测真把青海那块点亮了。与规划链路共用 normalize_city: 海南→三亚, 其余省→省会。
        # 注意两件事：
        #   ① 归一目标**不一定在表里**（台湾→台北，台北不在本地城市表）→ 用 .get，别 KeyError；
        #   ② 这里的说明文案要自己写 —— 直接借规划那句"本地没有行程数据"会和
        #      "点亮成功"自相矛盾（实测回复过"本地没有数据…点亮成功"）。
        cities, notes = [], []
        # 景区名的解释放在最前："「香格里拉」在迪庆，我按迪庆给你点亮"
        notes.extend(alias_notes(query, verb="给你点亮"))
        for raw in self._extract_cities(query):
            real, _plan_note = normalize_city(raw)
            if real != raw:
                notes.append(f"「{raw}」是省，按{real}给你点亮")
            if real not in cities:                   # 归一后可能重名, 去重保序
                cities.append(real)

        success, already, failed = [], [], []
        for name in cities:
            adcode = CITY_ADCODE_MAP.get(name)
            if not adcode:                           # 归一目标不在城市表 → 归它没用
                failed.append(name)
                continue
            try:
                if await self.city_service.city_repo.is_lighted(self.user_id, adcode):
                    already.append(name)
                    continue
                await self.city_service.light_city(user_id=self.user_id, adcode=adcode)
                success.append(name)
            except HTTPException as e:
                if e.status_code == 400 and "已点亮" in str(e.detail):
                    already.append(name)
                else:
                    failed.append(name)
            except Exception:
                failed.append(name)

        parts = []
        if notes:
            parts.extend(notes)          # "「海南」是省，先按三亚给你排" —— 点亮也得说清楚
        if success:
            parts.append(f"点亮城市{'、'.join(success)}成功，已为你创建对应旅行")
        if already:
            parts.append(f"{'、'.join(already)}之前已经点亮过啦")
        if failed:
            parts.append(f"{'、'.join(failed)}暂时点亮不了（本地没有这座城市的资料）")
        logger.info(f"[_direct_light_city] 成功{success} 已亮{already} 失败{failed}")
        # L2 取证：直达通道是**写权限的主要持有者**（模型手里已经没有 light_city 了），
        # 所以这里的台账尤其要记全 —— 出话术前 L3 要靠它确认"说了已点亮"是否真写入。
        if success:
            note_write("light_city", "ok", "、".join(success))
        if already:
            note_write("light_city", "already", "、".join(already))
        # 已经给了用户明确答复(成功/已亮/有条目说明)就算处理完;
        # 纯失败才交回正常流程, 别让用户等子 agent 空转
        handled = bool(success or already or notes)
        return handled, "；".join(parts) or "点亮失败"

    async def _direct_remember(self, content: str, pending: bool) -> tuple[bool, str]:
        """记忆直达通道: 绕开小模型的 tool calling(它调 save_memory 只有概率性成功),
        明确吩咐直接生效, 顺口一提的偏好只当候选。"""
        try:
            row, action = await self.memory_service.remember(
                user_id=self.user_id,
                content=content,
                kind="chat",
                source="chat",
                needs_review=pending,
            )
        except Exception as e:
            logger.error(f"[_direct_remember] 写入失败: {e}", exc_info=True)
            return False, "记忆保存失败，稍后再试试"
        # 回话要看"库里这条真实的状态", 不能看本次输入被判成什么 ——
        # 否则用户已经点过确认了, 还会收到一句"还在等确认"
        still_pending = bool(getattr(row, "needs_review", 0))
        # L2 取证（action=exists 说明这条早就有了，不是这次写的，分开记）
        note_write("save_memory", "already" if action == "exists" else "ok", f"{action}:{content[:20]}")
        logger.info(
            f"[_direct_remember] {action}(输入候选={pending}/库里待确认={still_pending}): {content[:30]}"
        )
        # 重复发同一条要明说"早就记过了", 别装作第一次听到
        if action == "exists":
            return True, _pick_reply(
                EXISTS_PENDING_REPLIES if still_pending else EXISTS_REPLIES, content
            )
        if action == "revived":
            return True, _pick_reply(REVIVED_REPLIES, content)
        if action == "updated":
            return True, _pick_reply(UPDATED_REPLIES, content)
        return True, _pick_reply(PENDING_REPLIES if still_pending else REMEMBER_REPLIES, content)

    def _travel_context_alive(self, history: list[dict] | None) -> bool:
        """上一轮还在聊行程吗 —— 决定"裸数字"接回哪条通道

        两个信号：① 会话槽位里有行程相关键；② 最近一条**助手**回复里出现行程词
        （追问"你想去哪个城市、打算玩几天"就是这种）。
        非要用户每次都把"我想规划行程"说全不现实 —— 追问后的短回答必须靠上下文接。

        **但只有信号②算"强信号"**：槽位是跨轮存活的，写它的消息不一定是出行
        （「帮我点亮三亚」就写了 target_city=三亚）。只看槽位的话，
        用户隔一句回个"4"就被排了一份三亚行程。所以这里改成：
        上一轮助手确实在问行程才算"行程上下文还活着"。
        """
        return self._last_assistant_asked_travel(history)

    @staticmethod
    def _last_assistant_asked_travel(history: list[dict] | None) -> bool:
        """最近一条**助手**回复是不是在聊行程（追问城市/天数/预算那种）"""
        for m in reversed(history or []):
            if m.get("role") == "assistant":
                return any(k in (m.get("content") or "") for k in TRAVEL_CONTEXT_HINTS)
        return False

    @staticmethod
    def _merge_travel_query(query: str, history: list[dict] | None) -> str:
        """把最近2条用户历史和当前句拼起来, 让槽位填充能从上下文补全城市/天数
        (如 历史'我想去重庆' + 当前'帮我规划两天' → 槽位能填出 重庆/2天)"""
        user_msgs = [m["content"] for m in (history or []) if m.get("role") == "user"][-2:]
        user_msgs.append(query)
        merged, seen = [], set()
        for m in user_msgs:
            if m and m not in seen:
                seen.add(m)
                merged.append(m)
        return "。".join(merged)

    def _slots_hint(self) -> str:
        """把会话槽位拼成一句"像人说的话", 补进规划/记账的输入。

        为什么需要它: 直达通道的输入只看"当前句 + 前端送来的历史",
        而前端只送最近 20 条 —— 20 条之前说过的城市/天数/预算就丢了。
        槽位存在 Redis 里, 所以这里能把它捞回来。
        """
        s = self.session_slots
        bits = []
        if s.get("target_city"):
            bits.append(f"目的地{s['target_city']}")
        if s.get("start_city"):
            bits.append(f"从{s['start_city']}出发")
        if s.get("days"):
            bits.append(f"玩{s['days']}天")
        if s.get("budget"):
            bits.append(f"预算{s['budget']}")
        elif s.get("budget_level") == "tight":
            bits.append("预算不多")
        if s.get("people"):
            bits.append(f"{s['people']}个人")
        return "，".join(bits)

    async def _direct_memory_query(self, query: str) -> tuple[bool, str]:
        """记忆查询直达：把记下的东西直接列出来，不调模型、不算金额。

        以前这个问题落到 operation 子 agent 的 tool calling 上：实测 30 秒空转，
        最后回一句答非所问的话。
        """
        if not self.memory_service:
            return False, "记忆服务不可用"
        try:
            rows = await self.memory_service.list_memories(self.user_id)
        except Exception as e:
            logger.error(f"[_direct_memory_query] 查询失败: {e}", exc_info=True)
            return False, "记忆查询出错"
        # 只把"画像类"记忆直说：消费流水(bill)是账目、有专门通道，且条数最多；
        # 点亮城市(city_light)、行程计划(plan)是**动作记录**，地图和规划页本来就看得见，
        # 一锅端出来会变成"去过开封；为重庆制定了行程；去过三亚…"这种流水账
        # （实测用户问"我有什么偏好"拿到 9 条这类记录，真正想看的 2 条偏好被淹了）。
        # 它们改成报个数，指个去处。
        PREF_KINDS = ("chat", "manual", "budget")
        ACT_KINDS = ("city_light", "plan")
        active = [m for m in rows if m.status == "active"]
        pref = [m for m in active if m.kind in PREF_KINDS]
        confirmed = [m.content for m in pref if not m.needs_review]
        pending = [m.content for m in pref if m.needs_review]
        act_count = {}
        for m in active:
            if m.kind in ACT_KINDS:
                act_count[m.kind] = act_count.get(m.kind, 0) + 1
        if not confirmed and not pending:
            logger.info("[_direct_memory_query] 无偏好类记忆")
            extra = ""
            if act_count:
                summary = "、".join(f"{n} 条{KIND_LABEL[k]}" for k, n in act_count.items())
                extra = f"（另外还有{summary}，在地图和规划页能看到）"
            return True, (
                "我还没记下你的什么偏好 —— 说一句「记住我不吃辣」这样的，我就会记住。" + extra
            )
        parts = []
        if confirmed:
            parts.append("我记着这些：" + "；".join(confirmed[:8]) + "。")
        if pending:
            parts.append(
                "还有几条没确认（到「我的」页点一下确认，我就一直记得）："
                + "；".join(pending[:5]) + "。"
            )
        if act_count:
            summary = "、".join(f"{n} 条{KIND_LABEL[k]}" for k, n in act_count.items())
            parts.append(f"另外还记着{summary}（地图/规划页能看到）。")
        logger.info(f"[_direct_memory_query] 偏好{len(confirmed)}条 待确认{len(pending)}条 动作{act_count}")
        return True, "".join(parts)

    async def _direct_bill_query(self, query: str) -> tuple[bool, str]:
        """账单查询直达：数字在代码里算，不让模型碰 —— 毫秒级、准确、不会胡说。

        以前这类问题没有通道，落到 operation 子 agent 的 tool calling 上：
        「查账」空转 30 秒还答非所问，「我的账单一共多少」直接把工具内部的
        "未找到的城市编码" 吐给了用户。
        """
        try:
            summary, total, count = await self.bill_service.summary_all(self.user_id)
        except Exception as e:
            logger.error(f"[_direct_bill_query] 汇总失败: {e}", exc_info=True)
            return False, "账单汇总出错"
        if not count:
            return True, "你还没记过账。跟我说一句「在重庆吃饭花了50」这样的，我就帮你记上。"
        # 金额大的分类排前面，一眼看出钱花在哪
        items = sorted(summary.items(), key=lambda kv: -kv[1])
        detail = "，".join(f"{name} {_fmt_money(amt)}元" for name, amt in items)
        logger.info(f"[_direct_bill_query] 汇总{count}笔 共{_fmt_money(total)}元")
        return True, f"你一共记了 {count} 笔，合计 ¥{_fmt_money(total)}：{detail}。"

    def _slot_override(self) -> dict:
        """会话槽位 → 交给槽位填充的权威值。

        槽位是规则抽取的确定性结果，而 fill_slots 是 few-shot —— 小模型会把示例里的
        "天数:1" 照抄过来（实测「改成成都，还是三天」就出了 1 天）。只传确实有值的字段，
        没有的传 None，不影响小模型自己的判断。
        """
        s = self.session_slots or {}
        return {
            "start_city": s.get("start_city"),
            "target_city": s.get("target_city"),
            "days": s.get("days"),
            "budget": s.get("budget") or (3000 if s.get("budget_level") == "tight" else None),
            "people_number": s.get("people"),
        }

    def _looks_like_revise_intent(self, query: str) -> bool:
        """句子里有没有"改一版"的意思（不看会话里有没有东西可改）"""
        return any(kw in query for kw in REVISE_PLAN_SIGNALS) or bool(
            _RE_REVISE_DAYS.search(query)
        )

    def _revise_target(self) -> str | None:
        """「改成X」该接着哪个语境: "guide"=换成另一个城市的攻略, "plan"=重出一份行程,
        None=会话里还没有东西可改。

        关键看 `last_travel_mode` —— 上一轮在问攻略, "改成成都"就是"换成成都的攻略";
        上一轮在排行程, 才是"重排一份成都的行程"。
        只看"槽位里有没有城市+天数"是不够的: 槽位是跨轮累积的, 用户早先问过一句
        "重庆三日游", 后来的攻略追问也会被当成在规划, 于是"改成成都"凭空弹出卡片。

        注意这里**不接受"没有 mode 记录"的兜底**: 有 mode 才说明这个会话里真的走过
        一次 travel(问过攻略或排过行程)。否则"改"字只会得到追问, 不会凭空出卡片。
        """
        s = self.session_slots or {}
        if not s.get("target_city"):
            return None
        mode = s.get("last_travel_mode")
        if mode == "guide":
            return "guide"
        if mode == "plan" and s.get("days"):
            return "plan"      # 规划得知道玩几天才排得出来
        return None

    async def _remember_travel_meta(self, extra: dict) -> None:
        """把 travel 的附属信息合并进会话槽位。记忆是附属品, 失败只记日志。

        一次写入而不是分两次 —— load/merge/save 各来一遍虽然也能对，但没必要多打一次 Redis。
        """
        if not self.session_id:
            return
        try:
            from app.services.session_slots import load_slots, merge_slots, save_slots
            slots = await load_slots(self.user_id, self.session_id)
            slots = merge_slots(slots, extra)
            await save_slots(self.user_id, self.session_id, slots)
            self.session_slots = slots
        except Exception as e:
            logger.warning(f"[travel_meta] 记录失败: {e}")

    async def _remember_travel_mode(self, mode: str) -> None:
        """记下这次 travel 走的是攻略还是规划, 下次「改成X」好接着同一个语境。"""
        await self._remember_travel_meta({"last_travel_mode": mode})

    def _looks_like_plan_cost_query(self, query: str) -> bool:
        """在问"刚那张行程花了多少/够不够/超没超"吗

        **必须排在账单查询通道之前**：否则"这个行程花了多少钱"含"花了多少"，
        会被账单通道接走、答成"你还没有记账记录"。
        还要有上下文才算 —— 要求槽位里有 `last_plan_cost`（这条会话真出过行程），
        这样"我花了多少钱"（没排过行程）仍然归账单通道。
        """
        if not (self.session_slots or {}).get("last_plan_cost"):
            return False
        if _RE_BILL_RECORD.search(query):
            return False
        return bool(_RE_PLAN_COST_ASK.search(query)) and bool(_RE_PLAN_COST_TONE.search(query))

    def _plan_cost_reply(self) -> str:
        """用槽位里的数字直接回答，不调模型（确定性、零延迟）"""
        s = self.session_slots or {}
        cost = float(s.get("last_plan_cost") or 0)
        days = int(s.get("last_plan_days") or s.get("days") or 0)
        people = int(s.get("last_plan_people") or s.get("people") or 0)
        budget = float(s.get("budget") or 0)
        what = (s.get("target_city") or "") + (f"{days}天" if days else "") + (f"{people}人" if people else "")
        head = f"你刚看的那份行程（{what}）" if what else "你刚看的那份行程"
        per = f"，人均约 ¥{cost / people:.0f}" if people else ""
        lines = [f"{head}预计花 ¥{cost:.0f}{per}。"]
        if budget and cost < budget:
            lines.append(
                f"你给的预算是 ¥{budget:.0f}，差 ¥{budget - cost:.0f} 没花到。"
                f"这不是行程在替你省钱，是两个原因：① 行程按「不超预算」来排，还会尽量把预算用足；"
                f"② 本地攻略数据的价格上限就在这（酒店可选档位少、景点门票基本免费），"
                f"再贵就排不出来了。想更贴近预算，可以让我多加一天，或者把预算调低些，我按新条件重排。"
            )
        elif budget:
            lines.append(f"比你 ¥{budget:.0f} 的预算多花了 ¥{cost - budget:.0f}（本地数据里没有更省钱的完整方案）。")
        else:
            lines.append("想按某个预算重排的话，说一句「预算4000」我就照着重排。")
        return "\n".join(lines)

    @staticmethod
    def _looks_like_save_plan_query(query: str) -> bool:
        """在说"把这张行程存进我的规划"吗（不是"改一版"）

        必须排在**规划支之前**：这类句子含"规划"，不管的话会被 travel 接走重新排一份，
        用户要的是保存现在这张。也要排在旧的"保存行程拦截"之前 —— 那只会让人去点按钮。
        """
        if any(kw in query for kw in _SAVE_PLAN_NOT):
            return False
        # 动词表里也要带上"新建"那批词：只说"把这份行程新建一份规划"时全句没有
        # 加入/保存/更新这类动词，不带就会漏到规划支重新排一张卡片
        return (any(kw in query for kw in SAVE_PLAN_TARGETS)
                and any(kw in query for kw in SAVE_PLAN_SAVE_VERBS + SAVE_PLAN_NEW_WORDS))

    @staticmethod
    def _save_plan_mode(query: str) -> str:
        """"新建"还是"覆盖"？返回 "new" / "overwrite" / ""（没说）

        "新建"优先：一句里两个词都出现（"不要覆盖，另存一份"）要按新建算。
        """
        if any(kw in query for kw in SAVE_PLAN_NEW_WORDS):
            return "new"
        if any(kw in query for kw in SAVE_PLAN_OVERWRITE_WORDS):
            return "overwrite"
        return ""

    async def _direct_save_plan(self, query: str = "") -> tuple[bool, str]:
        """把上一张行程卡片落库（走和前端「保存到规划」按钮同一个 PlanService.set_plan）

        新建还是覆盖：看用户怎么说（`_save_plan_mode`）；没说就"有旧的覆盖、没有就新建"，
        并在回复里讲清楚这次到底是哪种 —— 不然用户不知道原来那份还在不在。

        成功 → (True, 确认话术)；手里没有卡片 / 没注入服务 → (False, 说明话术)
        """
        from app.services.session_slots import load_last_plan
        payload = await load_last_plan(self.user_id, self.session_id)
        if not payload:
            return False, ("我这轮还没有生成过行程，没有可以保存的内容。"
                           "先让我排一份行程，或者在上方那张【行程卡片】上点「保存到规划」按钮。")
        if self.plan_service is None:
            logger.warning("[_direct_save_plan] 未注入 plan_service")
            return False, "保存功能这会儿不可用，请点卡片上的「保存到规划」按钮。"
        adcode = str(payload.get("adcode") or "")
        if not adcode:
            return False, "这张行程缺少城市编码，没能保存。可以重新排一份再试。"

        existed = await self.plan_service.get_plan(self.user_id, adcode)
        mode = self._save_plan_mode(query) or "overwrite"
        try:
            await self.plan_service.set_plan(self.user_id, adcode, plan_to_text(payload), mode=mode)
            total = await self.plan_service.count_plans(self.user_id, adcode)
        except Exception as e:
            logger.error(f"[_direct_save_plan] 保存失败: {e}", exc_info=True)
            return False, "保存到规划时出错了，稍后再试，或点卡片上的「保存到规划」按钮。"

        city = payload.get("city") or payload.get("target_city") or "该城市"
        days = payload.get("days")
        people = payload.get("people")
        cost = float(payload.get("total_cost") or 0)
        # L2 取证：规划落库也是写操作
        note_write("save_plan", "ok", f"{city} {mode}")
        logger.info(f"[_direct_save_plan] {city} 已保存(mode={mode}, 现在{total}份)")
        if mode == "new":
            return True, (f"已经把这 {days} 天{city}的行程另存为新的一份规划了"
                          f"（{people}人，预计 ¥{cost:.0f}）。这个城市现在有 {total} 份，"
                          f"在「规划」页可以切换查看。")
        if existed:
            return True, (f"已经把「{city}」原来的规划覆盖成这份新的了"
                          f"（{days}天{people}人，预计 ¥{cost:.0f}）。想保留旧的那份，可以说「新建一份」。")
        return True, (f"已经把这 {days} 天{city}的行程存进「规划」了"
                      f"（{people}人，预计 ¥{cost:.0f}）。在「规划」页可以随时查看和修改。")

    @staticmethod
    def _unservable_destination(query: str) -> str:
        """用户说了目的地、而本地没有那座城市的数据 → 返回目的地名；否则 ""（见 route_destination）"""
        raw, _city, reason = route_destination(query)
        return raw if reason == "unsupported" else ""

    @staticmethod
    def _route_boundary_text(raw: str) -> str:
        """边界那句话：说清楚排不了、数据范围、以及我们确实能做的那件事"""
        return (
            f"「{raw}」这条线路我排不了 —— 我的行程数据只覆盖国内城市，"
            f"跨境、极地这类线路没有景点酒店和交通数据，我不能给你编一条出来。"
        )

    async def _direct_unservable_route(self, query: str) -> tuple[bool, str]:
        """边界 + 联网检索到的**原始条目**。

        为什么不做成"让模型把检索结果转述一遍"：这个分支存在的全部意义就是"不许编"，
        转述等于再给它一次编的机会（实测它会把"坐火车去撒哈拉"说得更顺）。
        所以直接列检索结果的标题摘要，并如实标注"我没核对过准确性"；
        联网拿不到东西就只回边界那句，不硬凑。
        """
        raw = self._unservable_destination(query)
        if not raw:
            return False, ""
        refs = await self._web_route_refs(query)
        logger.info(f"[_direct_unservable_route] 目的地「{raw}」本地无数据 → 如实说明"
                    f"（联网参考 {len(refs.splitlines()) if refs else 0} 条）")
        if not refs:
            return True, self._route_boundary_text(raw) + (
                "\n国内的目的地能排：说个城市和天数（比如「三亚 5 天」），我就按天数和预算给你排。"
            )
        return True, (
            self._route_boundary_text(raw)
            + "\n\n网上查到这些，供参考（我没核对过准确性）：\n" + refs
            + "\n跨境交通、签证这类信息请以官方渠道为准。国内的目的地我能排："
              "说个城市和天数（比如「三亚 5 天」），我就按天数和预算给你排。"
        )

    @staticmethod
    async def _web_route_refs(query: str) -> str:
        """用用户原话去联网检索，把命中的条目整理成几行（失败/没结果都返回 ""）"""
        try:
            from app.agent.travel.services.web_search import get_web_search
            lines = await asyncio.to_thread(get_web_search().search, query)
        except Exception as e:                      # 联网挂了不该影响"如实说明"这件事
            logger.warning(f"[_web_route_refs] 联网失败, 只回边界说明: {e}")
            return ""
        return format_web_refs(lines)

    @staticmethod
    def _travel_is_guide_only(query: str, force: str | None = None) -> bool:
        """这句该走"攻略"(RAG问答)还是"规划"(出行程)?

        明确攻略词且没有规划词 → 攻略; 其余(含规划词/模糊) → 规划。
        force: "guide"/"plan" 显式指定("改成X"这类句子本身没有意图词)。

        抽成静态方法是为了让**流式入口和直达通道共用同一套判定** —— 各写一份迟早改歪。
        """
        if force == "guide":
            return True
        if force == "plan":
            return False
        is_plan = any(kw in query for kw in TRAVEL_PLAN_KEYWORDS)
        is_guide = any(kw in query for kw in TRAVEL_GUIDE_KEYWORDS)
        return is_guide and not is_plan

    def _merge_for_travel(self, query: str, history: list[dict] | None, revise: bool) -> str:
        """拼出交给内核的完整输入：槽位提示 + (本轮问题 / 最近两轮用户历史 + 本轮问题)

        revise=True 时**不带历史** —— 槽位里已是最新最全的条件, 而历史里较早的城市会把
        新城市带偏("改成成都"时尤其明显)。
        """
        merged = query if revise else self._merge_travel_query(query, history)
        # 会话槽位(20 条之前说的城市/天数/预算)补在开头; 槽位里的值已是最新说法
        hint = self._slots_hint()
        return f"{hint}。{merged}" if hint else merged

    async def _travel_guide_stream(self, query: str, history: list[dict] | None = None, revise: bool = False):
        """攻略支的流式输出: 边生成边 yield

        **一条都没 yield = 知识库和联网都没结果**。这种情况不要把"没找到"吐给用户
        (旧行为是让 harness 摘掉 travel、交给 chat 兜底), 由调用方按失败处理。
        """
        merged = self._merge_for_travel(query, history, revise)
        got = False
        async for piece in self.rag_service.rag_summary_stream(merged):
            got = True
            yield piece
        if got:
            await self._remember_travel_mode("guide")

    async def _direct_travel(self, query: str, history: list[dict] | None, revise: bool = False, force: str | None = None) -> tuple[bool, str]:
        """travel 直达通道(非流式)：绕过 create_agent 的 tool calling(小模型频繁
        peg-native 500+重试30秒), 直接按 规划/攻略 分流调用内核。

        攻略支现在**排空流式版**(`rag_summary` 就是排空包装) —— 这样流式和非流式一定是
        同一套逻辑, 不会各改各的。真正逐字输出的入口是 `_travel_guide_stream`。
        """
        merged = self._merge_for_travel(query, history, revise)

        # 明确攻略词且没有规划词 → RAG问答; 其余(含规划词/模糊) → 行程规划
        if self._travel_is_guide_only(query, force):
            try:
                text = await self.rag_service.rag_summary(merged)
            except Exception as e:
                logger.error(f"[_direct_travel] RAG异常: {e}")
                return False, "攻略查询出错"
            if NOT_FOUND_TEXT in text:
                return False, text      # 知识库+联网都没结果 → 摘掉travel, 让chat兜底
            await self._remember_travel_mode("guide")
            return True, text

        # 行程规划
        try:
            # 把记下的偏好翻译成规划条件（不吃辣 / 海鲜过敏 / 讨厌排队）
            prefs = {}
            if self.memory_service:
                try:
                    prefs = await self.memory_service.preference_tags(self.user_id)
                except Exception as e:
                    logger.warning(f"[_direct_travel] 偏好读取失败(忽略): {e}")
            plan = await solve(merged, override=self._slot_override(),
                               budget_stated=bool((self.session_slots or {}).get("budget")),
                               prefs=prefs)
        except Exception as e:
            logger.error(f"[_direct_travel] solve异常: {e}", exc_info=True)
            return False, "行程规划出错"
        payload = plan_to_frontend(plan)
        if payload:
            # 用户点名了多座城市：**不硬拆、也不静默忽略**（用户要求"按需求来定"）。
            # 先按第一座排一份完整的，然后在卡片上说清另一座怎么办 ——
            # 以前会默默按一座城市排满全部天数，用户以为两座城市都排进去了。
            others = [c for c in find_cities(merged) if c != payload.get("city")]
            if others:
                tip = (f"你提到了{payload.get('city')}和{'、'.join(others[:2])}，这份先按{payload.get('city')}排。"
                       f"多城要分开排（单城精确排程大概 5 天以内）：{'、'.join(others[:2])}那份接着说一声就行。")
                payload["warning"] = f"{tip}{payload.get('warning') or ''}"
            card = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            logger.info(f"[_direct_travel] 规划成功 {payload.get('days')}天 花费{payload.get('total_cost')}")
            # 顺手把这张行程的花费/天数/人数记进槽位 —— 用户看完卡片十有八九会追问
            # "花费是不是太少了/超了吗"，那时候没法再回头查这张卡（见 _plan_cost_reply）
            await self._remember_travel_meta({
                "last_travel_mode": "plan",
                "last_plan_cost": int(round(float(payload.get("total_cost") or 0))),
                "last_plan_days": int(payload.get("days") or 0),
                "last_plan_people": int(payload.get("people") or 0),
            })
            # 整份卡片也留一份：用户可能接着说"把这张加入规划/更新一下"（见 _direct_save_plan）
            from app.services.session_slots import save_last_plan
            await save_last_plan(self.user_id, self.session_id, payload)
            return True, f"⟦PLAN⟧{card}⟦/PLAN⟧"
        # 没规划出来: 区分"没说目的地"和"有城市但排不出"
        target = plan.get("target_city", "?")
        if target in ("?", "", None):
            logger.info("[_direct_travel] 缺目的地, 追问用户")
            return True, "好的，我来帮你规划行程～请告诉我：你想去哪个城市？打算玩几天、预算大概多少呢？"
        # 有城市但排不出: 最常见的原因是"要的天数超过本地数据能撑的量"
        # (实测单城最多排 5 天, 6 天以上引擎返回"无法规划")。
        # 但**排不出不等于没答案** —— 用户要的是行程，不是"排不出"。
        # 本地结构化数据只够精确排程，天数一多就该去网上找资料（用户的明确要求：
        # "网上什么都有，不要太局限"）。这里拿搜索结果按同一套 RAG 提示词整理成
        # 一份"参考路线"，并讲清它和精确卡片的区别。
        days = self.session_slots.get("days")
        reason = plan.get("fail_reason", "")
        logger.warning(
            f"[_direct_travel] {target}未排出行程(要{days}天, 原因={reason}), warning={plan.get('warning')}"
        )
        web_text = await self._web_reference_plan(merged, target, days, reason)
        if web_text:
            return True, web_text
        tip = f"要{days}天的话，" if days else ""
        return True, (
            f"{target}的行程数据有限，{tip}暂时排不出一份完整的安排，联网也没查到可用的资料。"
            f"可以换个具体城市、少要几天，或者先问我「{target}有什么好玩的」，看看景点再定。"
        )

    async def _web_reference_plan(self, query: str, city: str, days, reason: str = "") -> str:
        """本地排不出时的联网兜底：按网上的资料整理一份**参考路线**（不是精确卡片）

        为什么不做成卡片：卡片的每一分钟、每一笔花费都是结构化数据算出来的，
        网页文本给不了这些。硬编成卡片等于编数据 —— 所以明确标成"参考资料整理"，
        并告诉用户怎么拿到精确排程。
        """
        try:
            from app.agent.travel.services.web_search import get_web_search
            q = f"{city} {days}天 行程 攻略" if days else f"{city} 行程 攻略"
            web = await asyncio.to_thread(get_web_search().search, q)
            if not web:
                logger.info("[_direct_travel] 联网也没结果, 退回解释文案")
                return ""
            body = await self.rag_service.answer_from(
                f"请给一份{city} {days or ''}天的行程路线建议", web
            )
            if not body.strip():
                return ""
            logger.info(f"[_direct_travel] 已用 {len(web)} 条网络资料生成参考行程")
            # 原因不同，说法必须不同 —— 天数超限和"数据源挂了"是两回事，
            # 一律说成"本地数据只够 5 天"会让用户按错误的方向去改需求。
            if reason == "no_hotel":
                head = (f"{city}的实时房源（酒店）接口这会儿连不上，排不出带时间轴和花费的精确行程。"
                        f"我按网上的资料给你整理了一份**参考路线**：")
                tail = "等酒店接口恢复了，我可以再给你排一份带时间轴和花费的精确卡片。"
            elif reason == "no_poi":
                head = f"没查到{city}的景点/餐厅数据，精确排程做不了。我按网上的资料给你整理了一份**参考路线**："
                tail = "想要带时间轴和花费的卡片，得等景点数据能取到；也可以先问我「{city}有什么好玩的」。".format(city=city)
            else:
                head = (f"本地数据排不出 {days} 天这么长的精确行程（这套排程靠景点/酒店/车次的结构化数据，"
                        f"单座城市大概能精确排到 5 天）。我按网上的资料给你整理了一份**参考路线**：")
                tail = ("这份是网络资料整理的，没有精确时间轴和花费。想要带时间轴、花费的那种卡片，"
                        "可以把天数缩到 5 天以内；如果想去不止一座城市，告诉我各待几天，我按城市分开给你排。")
            return f"{head}\n\n{body.strip()}\n\n{tail}"
        except Exception as e:
            logger.error(f"[_direct_travel] 联网参考行程失败: {e}", exc_info=True)
            return ""

    async def _run_subagent(self, agent, query: str, history: list[dict] | None = None) -> tuple[bool, str]:
        # 历史对话在前、当前问题在尾, 让子agent能联系上下文
        messages = list(history or []) + [{"role": "user", "content": query}]
        try:
            result = await agent.ainvoke(
                {"messages": messages},
                config={"recursion_limit": RECURSION_LIMIT},
            )
            msgs = result.get("messages", [])
            text = msgs[-1].content if msgs else ""
        except Exception as e:
            # llama.cpp peg-native 500 等工具调用异常 → 判定失败，让 harness 换人
            if "peg-native" in str(e) or "500" in str(e):
                logger.warning(f"[_run_subagent] 模型语法500: {e}")
                return False, "模型调用失败"
            logger.error(f"[_run_subagent] 子agent执行异常: {e}")
            return False, f"执行出错: {e}"
        return not self._is_fail(text), text

    async def execute_stream(self, query: str, history=None):
        """对外入口 = harness 收口层：所有出口的话术都过 L3 声称校验（一处生效）。

        为什么套一层而不到处加校验：一轮里出话术的分支有十几个（直达通道 6 个、
        改行程 2 个、子 agent 2 个、兜底 1 个），逐个加迟早漏一个 ——
        而"模型声称自己做了没做的事"正是最需要兜住的那类事故
        （实测回过"好的，我已经为你点亮了北京和上海"，其实什么都没写）。

        分片校验：非流式路径一轮只 yield 一片，等于整段校验；攻略流式路径若某片里
        出现假的写操作声称，也是就地替换成如实答复 —— 宁可拼接突兀，
        也不能让假话出到用户面前。
        """
        async for piece in self._run_turn(query, history):
            yield self._audit(piece)

    def _audit(self, text: str) -> str:
        """出话术前的收口处理（两个确定性约束，都在这里做，避免十几处出口各写一遍）：

        ① 空话尾巴清理：拒绝完还补"不过你可以试试看/祝你好运"等于把结论软化了；
        ② L3 声称校验：说了"已经做了"某件写操作，本轮就必须真有对应写入台账。

        台账直接传对象（self._turn_log），不读 contextvar —— L3 在生成器外层执行，
        而台账是内层 reset_turn 建立的，跨生成器时传递对象最稳。
        """
        text = strip_fluff_tail(text)
        checked = verify_write_claim(text, ledger=self._turn_log)
        if checked != text:
            logger.warning("[harness] 话术校验命中：声称的写操作没有证据，已换成如实答复")
        return checked

    async def _run_turn(self, query: str, history=None):
        """harness 主循环（纯文本分类版）:
        1. 记账直达：命中记账关键词 → 直接提取参数记账（绕过 tool calling）
        2. classify_intent 选子助手（不依赖 tool calling，不会500）
        3. 调用子助手（带上最近几轮历史, 支持上下文），成功直出
        4. 失败 → 摘掉该子助手，重选（最多3轮）
        5. travel+operation 都真失败 → 兜底话术
        history: 前端传来的最近对话 [{role, content}], 不含当前query
        """
        # 开一轮：记下本轮原话（给写工具做授权核对/防改写），并清空本轮写入台账。
        # 台账是 L3 话术校验的依据 —— 「说了已点亮就必须查得到点亮记录」。
        self._turn_log = reset_turn(query)
        # 规范化历史: pydantic对象转dict, 只留 user/assistant, 截断最近12条(6轮)防拖慢小模型
        norm_history: list[dict] = []
        for m in (history or []):
            d = m.model_dump() if hasattr(m, "model_dump") else dict(m)
            role = d.get("role")
            content = (d.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                norm_history.append({"role": role, "content": content})
        norm_history = norm_history[-HISTORY_MAX_MSGS:]
        # 总字数超预算时, 从最旧的开始丢, 保住最近的对话
        budget = HISTORY_MAX_CHARS
        kept: list[dict] = []
        for m in reversed(norm_history):
            if budget - len(m["content"]) < 0 and kept:
                break
            kept.append(m)
            budget -= len(m["content"])
        norm_history = list(reversed(kept))
        if norm_history:
            logger.info(f"[execute_stream] 携带{len(norm_history)}条历史上下文")

        # 行程花费追问直达通道：**必须放在账单查询之前** —— "这个行程花了多少钱"含"花了多少"，
        # 会被账单通道接走、答成"你还没有记账记录"。它问的是刚排出来的那张行程。
        if self._looks_like_plan_cost_query(query):
            logger.info("[execute_stream] 命中行程花费追问直达")
            yield self._plan_cost_reply()
            return

        # 账单查询直达通道：必须放在记账之前 —— "我花了多少钱"含"花了"，
        # 不先截住会被记账通道当成一笔消费。数字类问题代码直接算，不调模型。
        if _looks_like_bill_query(query):
            logger.info("[execute_stream] 命中账单查询直达")
            ok, text = await self._direct_bill_query(query)
            if ok:
                yield text
                return
            logger.warning(f"[execute_stream] 账单查询失败({text[:30]}), 走正常流程")

        # 记账直达通道：小模型不会生成复杂 tool call 参数，直接用 LLM 提取 JSON
        if _looks_like_bill(query):
            logger.info(f"[execute_stream] 命中记账直达关键词, 走直达通道")
            ok, text = await self._direct_add_bill(query, norm_history)
            if ok:
                yield text
                return
            logger.warning(f"[execute_stream] 记账直达失败({text[:50]}), 降级走子agent")

        # 点亮/查询城市直达通道：城市名固定可直接匹配, 绕过 tool calling, 解决"点亮慢"
        if any(kw in query for kw in DIRECT_LIGHT_KEYWORDS):
            logger.info(f"[execute_stream] 命中城市直达关键词, 走直达通道")
            ok, text = await self._direct_light_city(query)
            if ok:
                yield text
                return
            logger.info(f"[execute_stream] 城市直达未处理({text[:30]}), 走正常流程")

        # 记忆查询直达：必须放在"记忆写入"之前 —— "我喜欢什么"含"我喜欢"，
        # 不先截住会被当成一条待写偏好存进去
        if _looks_like_memory_query(query):
            logger.info("[execute_stream] 命中记忆查询直达")
            ok, text = await self._direct_memory_query(query)
            if ok:
                yield text
                return
            logger.warning(f"[execute_stream] 记忆查询失败({text[:30]}), 走正常流程")

        # 记忆直达通道：明确"记住X"直接生效；顺口说的偏好只存候选(待确认)，都绕开 tool calling
        mem_content, mem_pending = _extract_memory_command(query)
        if mem_content:
            logger.info(f"[execute_stream] 命中记忆直达(候选={mem_pending}): {mem_content[:30]}")
            ok, text = await self._direct_remember(mem_content, mem_pending)
            if ok:
                yield text
                return
            logger.warning(f"[execute_stream] 记忆直达失败({text[:30]}), 走正常流程")

        # "把这张行程加入规划/更新一下"直达通道：真落库。
        # 位置很关键 —— 要在**保存行程拦截之前**（那句只让人去点按钮、不办事），
        # 也要在**规划支之前**（否则含"规划"会被 travel 接走重新排一份新卡片，
        # 用户看中的那版反而被覆盖）
        if self._looks_like_save_plan_query(query):
            logger.info("[execute_stream] 命中保存行程直达")
            _ok, text = await self._direct_save_plan(query)
            yield text
            return

        # "保存行程"类: 不交给chat(会杜撰景点/金额), 明确引导用卡片上的保存按钮
        if any(kw in query for kw in SAVE_PLAN_KEYWORDS):
            logger.info("[execute_stream] 命中保存行程意图, 拦截防杜撰")
            yield ("要保存行程的话，请在上方那张【行程卡片】上点「保存到规划」按钮，"
                   "就能存到对应城市的规划里。我不会在这里凭空编写行程或消费金额，以免和真实规划不一致。")
            return

        # "排不了的路线"直达通道：用户说了目的地，而本地没有那座城市的数据（跨境/极地/幻想地名）。
        # 必须放在 classify 之前 —— 否则会落到闲聊，而小模型遇到"没有数据"不会说"我做不到"，
        # 而是顺着编：实测「从北极到撒哈拉观海」回过"可以坐火车前往…注意保暖和防晒"，
        # 再问"这个真的可以实现么"又回"当然可以！可以订机票、酒店和旅行保险"。
        # 边界由这条确定性通道声明，不给模型发挥空间。
        ok, text = await self._direct_unservable_route(query)
        if ok:
            logger.info("[execute_stream] 命中排不了的路线, 如实说明")
            yield text
            return

        # 改攻略/改行程直达通道: "改成成都" / "换成杭州两天" / "再加一天"
        # 这些说法本身没有意图词, 要看**上一轮在聊什么**才知道接着哪条路:
        # 上一轮问攻略 → 换成另一个城市的攻略; 上一轮在排行程 → 重排一份行程
        if self._looks_like_revise_intent(query):
            target = self._revise_target()
            if target:
                label = "攻略" if target == "guide" else "行程"
                logger.info(f"[execute_stream] 命中改{label}意图, 接着上一轮语境重跑: {self._slots_hint()}")
                if target == "guide":
                    # 攻略支流式输出。一条都没吐 = 知识库和联网都没有这个城市的攻略,
                    # 那就把"没找到"明确回给用户并停下 —— 不能让流程继续往下走,
                    # 否则会落到"规划"上凭空出一张卡片
                    buf: list[str] = []
                    async for piece in self._travel_guide_stream(query, norm_history, revise=True):
                        buf.append(piece)
                        yield piece
                    if not buf:
                        yield NOT_FOUND_TEXT
                    return
                ok, text = await self._direct_travel(query, norm_history, revise=True, force=target)
                if ok:
                    yield text
                    return
                logger.warning(f"[execute_stream] 改{label}失败({text[:50]}), 走正常流程")
            else:
                # 会话里还没出过行程、也没问过攻略 —— 不能因为一个"改"字就凭空甩卡片,
                # 也不交闲聊(1.5B 会顺着历史编出假行程), 直接确认改意 + 追问缺的条件
                city = (self.session_slots or {}).get("target_city")
                logger.info(f"[execute_stream] 命中改的说法但无上下文可接(city={city}), 追问条件")
                yield _pick_reply(REVISE_ASK_DAYS_REPLIES, city) if city else _pick_reply(REVISE_ASK_CITY_REPLIES, "")
                return

        # 裸数字 + 上一轮在聊行程 → 当"天数"接回行程直达。
        # 实测：追问"想去哪个城市、玩几天"后用户答"4"，被判成 operation（向量 0.732、
        # 分差 0.004，等于抛硬币）→ 进 tool calling → 中间件强制首轮必调工具 →
        # 模型编出 light_city(['北京','上海']) 把用户没提的城市点亮了。
        # 这类"回答追问的短句"必须由上下文决定归属，不能交给分类器掷骰子。
        if _is_bare_number(query) and self._travel_context_alive(norm_history):
            logger.info("[execute_stream] 裸数字 + 行程上下文 → 接回 travel 直达")
            ok, text = await self._direct_travel(query, norm_history)
            if ok:
                yield text
                return
            logger.warning(f"[execute_stream] 裸数字接 travel 失败({text[:40]}), 走正常流程")

        failed = set()
        for attempt in range(3):
            intent = await classify_intent(query, exclude=failed)
            agent, agent_name = self._agents[intent]
            logger.info(f"[execute_stream] 第{attempt + 1}轮 选 {agent_name}")

            # travel 走直达通道(绕过 tool calling 提速); operation/chat 仍走子agent
            streamed = False
            if intent == "travel":
                # 槽位不是"暗写"通道：句子自己没有出行要素、上一轮助手也没在问行程时，
                # 不许拿旧槽位排行程（实测「帮我点亮三亚」污染槽位后，一句
                # 「从北极到撒哈拉观海」被排成了三亚一日游）。攻略支不受此限 —— 它只读知识库。
                if (not self._travel_is_guide_only(query)
                        and not _travel_evidence_in_query(query)
                        and not self._last_assistant_asked_travel(norm_history)):
                    logger.info("[execute_stream] travel 判定但无出行要素、上一轮也不是行程追问 → "
                                "不用旧槽位硬排, 交给闲聊")
                    ok, text = False, "没有出行要素"
                elif self._travel_is_guide_only(query):
                    # 攻略支：边生成边吐字（首字从"整段生成完"变成 1 秒内出字）
                    buf: list[str] = []
                    async for piece in self._travel_guide_stream(query, norm_history):
                        buf.append(piece)
                        yield piece
                    streamed = True
                    text = "".join(buf)
                    ok = bool(text)
                    if not ok:
                        # 一条都没吐 = 本地和联网都没结果。这句话不给用户看
                        # （旧行为是摘掉 travel 交给 chat 兜底），所以只把它当失败话术
                        text = NOT_FOUND_TEXT
                else:
                    ok, text = await self._direct_travel(query, norm_history)
            else:
                ok, text = await self._run_subagent(agent, query, norm_history)
            if ok:
                logger.info(f"[execute_stream] {agent_name} 成功 ({len(text)}字)")
                if not streamed:        # 流式支已经在上面边生成边吐过了，别重复
                    yield text
                return

            logger.warning(f"[execute_stream] {agent_name} 失败({text[:50]}), 摘掉后重选")
            failed.add(intent)

            # operation 弃权(误路由)只说明不该找它, 不触发收尾
            if {"travel", "operation"} <= failed and OUT_OF_SCOPE_MSG not in text:
                break

        yield GIVE_UP_MSG
