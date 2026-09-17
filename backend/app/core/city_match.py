"""城市名匹配：判断句子里的城市名是不是真的在"指那座城市"

要解决的问题：城市表里有一批**和常用词同形**的二字市名，裸子串匹配
（`text.find(name)`）会把下面这些句子都认成"提到了那座城市"：

    开封 / 帮我开封一下这个文件          三明 / 早上吃个三明治就行
    日照 / 日出日照金山，真是美景        中山 / 我晚上在中山路吃饭
    大同 / 大同小异，没什么区别          白银 / 白银价格最近涨了
    白山 / 长白山的雪景一定很美          来宾 / 欢迎各位来宾参观
    四平 / 四平八稳地推进项目            东方 / 东方明珠塔在浦东

后果不轻：
  - `session_slots` 每轮请求都会抽一次槽（app/routers/agent.py），误抽的 target_city
    会写进 Redis **活 7 天** —— 用户在闲聊里说"早上吃个三明治就行"，之后说
    "帮我规划三天"，排出来的是三明市的行程。
  - `kb_retriever` 拿它做城市限定检索，会把"长白山的雪景"限定到**白山市**的内容上，
    而且 `is_relevant` 对"认得出地名"是无条件信任的，于是直接答下去。

所以在裸匹配之外补一道**邻字语境**判据，而且**只对同形词那一批收紧**：
  - 左边是地点介词/动词：去/到/在/从/往/回/飞/住/游/玩/经
  - 右边是行政区后缀（市/省/区/县/州/岛/镇…）或旅游词（旅游/攻略/好玩/有什么…）
  - 右边是"N天/N日/N晚/N周"（"重庆三日游"那种形态）
  - 就在句尾（"推荐日照"）

不在名单里的城市名（绝大多数）保持原来的宽松匹配 —— 不误伤正常识别。
名单没有覆盖到的同形词，等引入分词（jieba）后可以整段删掉这个模块。
"""
import re

from app.core.constants import CITY_ADCODE_MAP
from app.core.city_alias import ALIAS_CITY

# 和常用词同形的二字市名。**只收二字**：三字以上的名字撞常用词的概率低得多
# （长白山市那种"前缀+城市名"的情况，被左边的前缀字符挡掉了）
AMBIGUOUS_CITIES = frozenset("""
    开封 来宾 日照 中山 三明 东方 白山 四平 大同 白银 长治 朝阳 仙桃 天门
    北海 安康 泰安 云浮 百色 长春
""".split())

# 左侧：地点介词/动词（"去重庆""在中山""从日照出发""推荐日照"）
_LEFT_ANCHORS = frozenset("去到在从往向回飞住游玩的经停离荐议绍说聊介")
# 右侧：行政区后缀
_RIGHT_SUFFIX = frozenset("市省区县州岛镇乡村港")
# 右侧：旅游词前缀（"日照旅游攻略"）
_RIGHT_WORDS = ("旅游", "旅行", "游玩", "自由行", "攻略", "好玩", "怎么", "有什么", "景点", "美食")
# 右侧：路名/建筑后缀 —— "中山路""三明街"里的才是路名，不是城市
_COMPOUND_TAIL = frozenset("路街道巷桥站口店园")
# 左侧：会跟前一个字组成**更长专名**的修饰字 —— "长白山"(白山)、"大同一"? 这类
# 光看右边看不出问题（"长白山怎么去"的"怎么"是个正常锚点），得从左边挡
_COMPOUND_HEAD = frozenset("长大小新老东西南北中金银白黑红青龙凤天云石黄紫上下")
# "三日游""两天""一晚""一周" 这种天数说法
_NUM_DAYS = re.compile(r"^[0-9一二两三四五六七八九十]+(天|日|晚|周|个月|月)")


def is_city_mention(text: str, name: str, idx: int) -> bool:
    """text[idx:idx+len(name)] 处的 name，是不是在指这座**城市**

    非名单城市、以及三字以上的名字，一律放行（保持原行为）。
    """
    if len(name) != 2 or name not in AMBIGUOUS_CITIES:
        return True

    left = text[idx - 1] if idx > 0 else ""
    after = text[idx + len(name):]
    right = after[:1]

    # 判据顺序有讲究：
    # 1) "中山路""三明街" 命中的是路名 —— 先挡，别被左边的"在"带走
    if right in _COMPOUND_TAIL:
        return False
    # 2) "白山市""中山区" 是明确的行政区名
    if right in _RIGHT_SUFFIX:
        return True
    # 3) "长白山"(白山) —— 左边的修饰字说明这是个更长的专名的一部分
    if left in _COMPOUND_HEAD:
        return False
    # 4) "去开封""推荐日照"
    if left in _LEFT_ANCHORS:
        return True
    # 5) "日照旅游攻略""日照有什么好玩的"
    if after.startswith(_RIGHT_WORDS):
        return True
    # 6) 就在句尾："推荐日照"
    if not after:
        return True
    # 7) "三明三日游" 这种天数说法
    return bool(_NUM_DAYS.match(after))


def ambiguous_names_known() -> list[str]:
    """名单里不在城市表的条目（自检用：名单写错了名字会在这里暴露）"""
    return sorted(n for n in AMBIGUOUS_CITIES if n not in CITY_ADCODE_MAP)


# ── 省级名 → 规划/点亮用的具体城市 ──
# 为什么必须归一：各数据源对省名的解释不一致 —— 实测"海南"被景点/餐厅/12306
# 解析成海南省，酒店接口(RollingGo)却解析成青海海南藏族自治州（拉回来的是共和、贵德的
# 宾馆），两边坐标差 2000+ 公里 → 引擎算市内交通直接爆炸 → 无论几天都"无法规划"。
# 所以入口一律把省名落到具体城市（海南例外：旅游核心是三亚，不按省会海口），
# 并在回复里写明，用户想去别的城市可以再说。
PROVINCE_CAPITAL = {
    "河北": "石家庄", "山西": "太原", "辽宁": "沈阳", "吉林": "长春", "黑龙江": "哈尔滨",
    "江苏": "南京", "浙江": "杭州", "安徽": "合肥", "福建": "福州", "江西": "南昌",
    "山东": "济南", "河南": "郑州", "湖北": "武汉", "湖南": "长沙", "广东": "广州",
    "海南": "三亚", "四川": "成都", "贵州": "贵阳", "云南": "昆明", "陕西": "西安",
    "甘肃": "兰州", "青海": "西宁", "台湾": "台北",
    "广西": "南宁", "内蒙古": "呼和浩特", "西藏": "拉萨", "宁夏": "银川", "新疆": "乌鲁木齐",
}


def normalize_city(name: str) -> tuple[str, str | None]:
    """省名 / 带'省'后缀的名字 → 具体城市。返回 (城市, 给用户的说明 or None)

    归一目标自己也必须能在城市表里查到，否则归一等于把用户送进死路：
    实测「台湾」→ 台北，而台北不在本地城市表（港澳台只收了香港/澳门），
    归一后下游查不到城市直接失败。这种情况保持原名并说明。
    """
    if not name or name in ("?",):
        return name, None
    bare = name[:-1] if name.endswith("省") else name
    if bare in PROVINCE_CAPITAL:
        city = PROVINCE_CAPITAL[bare]
        if city in CITY_ADCODE_MAP:
            return city, f"「{name}」是省，先按{city}给你排（想去别的城市直接说）"
        return name, f"「{name}」本地暂时没有行程数据，换个城市试试"
    return name, None


def find_province(text: str) -> str:
    """句子里提到的省名（取最长，避免"海南"撞"海南藏族自治州"这类前缀），没提到返回 "" """
    hit = ""
    for name in PROVINCE_CAPITAL:
        if name in text and len(name) > len(hit):
            hit = name
    # "云南省" 这种带后缀的也算
    for name in PROVINCE_CAPITAL:
        if name + "省" in text and len(name) > len(hit):
            hit = name
    return hit


# ═══════════ 路线句式：用户说了目的地，本地有没有这座城市的数据 ═══════════
# "到/去/飞往/前往/飞到 + 地名"。**必须带后视断言**：不带的话
# "从北京到纽约怎么走" 会把"纽约怎么走"整段吃掉，再被下面的"不是地名"过滤掉，
# 于是这类跨境路线漏判（实测漏过一次）。断言集 = 动作词 + 疑问词 + 标点 + 句尾。
_RE_ROUTE_DEST = re.compile(
    r"(?:到|去|飞往|前往|飞到)([\u4e00-\u9fa5]{2,6}?)"
    r"(?=(?:玩|旅游|度假|旅居|观海|看海|看|吃|逛|溜|住|待|呆"
    r"|几天|几日|一周|一星期|半个月|天|的|是|有|怎么|多少|怎样|如何"
    r"|\d|，|。|,|\.|！|!|？|\?|$))"
)
# 不是地名的"目的地"（"想去个暖和点的地方""去哪儿好"）—— 不该被当成城市名去报"没数据"
_NOT_A_PLACE = ("地方", "哪", "什么", "怎么", "自己", "个", "点儿", "点", "这里", "那里")
# 地名后面拖的动作词，展示时去掉："撒哈拉观海" → "撒哈拉"
_DEST_TAIL_VERBS = ("观海", "看海", "旅游", "度假", "旅居", "玩", "逛", "走走", "看看", "吃", "看")


def resolve_place(token: str) -> str:
    """把一段文字解析成城市表里的城市（**最长前缀优先**："重庆吃火锅" → 重庆）。

    覆盖三层：地级市 → 景区别名（香格里拉/九寨沟…）→ 省名（→省会）。
    解析不到返回 ""。这是 `find_cities` 之外的补充场景：这里拿到的是**没切干净的**
    短句片段（"重庆吃火锅"），不是整句话。
    """
    if not token:
        return ""
    for i in range(len(token), 1, -1):
        name = token[:i]
        if name in CITY_ADCODE_MAP:
            return name
    alias_hit = ""
    for surface, city in ALIAS_CITY.items():
        if token.startswith(surface) and len(surface) > len(alias_hit):
            alias_hit = city
    if alias_hit:
        return alias_hit
    prov = find_province(token)
    if prov:
        return PROVINCE_CAPITAL[prov]
    return ""


def route_destination(text: str) -> tuple[str, str, str]:
    """从路线句式里抽目的地，并判断本地有没有这座城市的数据。

    返回 (目的地原文, 解析出的城市, 原因)：
      - 没有路线结构               → ("", "", "")
      - 有结构且能解析             → ("三亚玩", "三亚", "")
      - **有结构但解析不到**       → ("撒哈拉观海", "", "unsupported")
      - 明显不是地名（在提问）     → ("", "", "")

    为什么要这个：模型遇到"没有数据"时不会说"我做不到"，而是顺着编 ——
    实测「从北极到撒哈拉观海」被闲聊助手接走，回了"可以坐火车前往…注意保暖和防晒"，
    再问"这个真的可以实现么"又答"当然可以！可以订机票、酒店和旅行保险"。
    边界必须由确定性代码来声明（见 footprint_agent._direct_unservable_route）。
    """
    m = _RE_ROUTE_DEST.search(text or "")
    if not m:
        return "", "", ""
    raw = m.group(1)
    city = resolve_place(raw)
    if city:
        return raw, city, ""
    if any(bad in raw for bad in _NOT_A_PLACE):
        return "", "", ""          # "想去个暖和点的地方" —— 在提问，不是在说地名
    for verb in _DEST_TAIL_VERBS:
        if raw.endswith(verb) and len(raw) > len(verb) + 1:
            raw = raw[: -len(verb)]
            break
    return raw, "", "unsupported"


def find_cities(text: str) -> list[str]:
    """从文本里找出**真的在指城市**的名字，按出现先后返回。

    统一入口，把两件容易漏的事一次做掉（历史上 5 处各写一遍裸 `name in text`）：

    ① 邻字语境判据 `is_city_mention` —— "早上吃个三明治就行" 不算提到三明市；
    ② **最长匹配优先** —— 城市表里有 `鞍山 ⊂ 马鞍山`，
       裸匹配会把"帮我点亮马鞍山"同时点亮**辽宁鞍山**和**安徽马鞍山**（用户在安徽，
       辽宁那块莫名其妙亮了）。命中区间被更长名字完全包住的短名一律丢掉。

    另外把**景区/县级市别名**并进来（`city_alias.ALIAS_CITY`）：用户说"香格里拉""九寨沟"
    "婺源"时，返回的是归属地级市（迪庆/阿坝/上饶）—— 城市表只有地级市，不并这一层
    就只能回一句"请告诉我你想去哪个城市"。要跟用户解释"为什么变成了迪庆"用 `alias_notes()`。
    """
    hits: list[tuple[int, str]] = []
    for name in CITY_ADCODE_MAP:
        start = 0
        while True:
            idx = text.find(name, start)
            if idx < 0:
                break
            if is_city_mention(text, name, idx):
                hits.append((idx, name))
                break
            start = idx + 1      # 这一处是常用词（"帮我开封一下"），继续往后找

    for idx, _surface, city in _alias_hits(text):
        # 别名落在某个城市名里面 → 以城市名为准（"张家界市" 里的别名不重复计入）
        if any(o_idx <= idx < o_idx + len(o_name) for o_idx, o_name in hits):
            continue
        hits.append((idx, city))

    kept = []
    for i, (idx, name) in enumerate(hits):
        end = idx + len(name)
        # 被更长名字完全包住 → 是那个长名的一部分，不是独立城市
        if any(
            o_idx <= idx and end <= o_idx + len(o_name)
            for j, (o_idx, o_name) in enumerate(hits)
            if j != i and o_name != name and len(o_name) > len(name)
        ):
            continue
        kept.append((idx, name))

    kept.sort()
    return list(dict.fromkeys(n for _, n in kept))


def _alias_hits(text: str) -> list[tuple[int, str, str]]:
    """文本里出现的景区别名 → [(位置, 用户说的名字, 归属城市)]

    同名多次出现只取第一次；产物按位置排序，方便调用方跟城市名一起排先后。
    """
    out: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for surface, city in ALIAS_CITY.items():
        idx = text.find(surface)
        if idx < 0 or surface in seen:
            continue
        seen.add(surface)
        out.append((idx, surface, city))
    out.sort()
    return out


def alias_notes(text: str, verb: str = "给你找") -> list[str]:
    """给用户看的解释：说了景区名、实际按哪座城市办。

    实测的必要性：用户说"香格里拉玩三天"，卡片上写"迪庆"却没解释，用户会以为弄错了。
    verb 用来适配场景（规划/点亮文案不一样）。
    """
    notes, seen = [], set()
    for _idx, surface, city in _alias_hits(text):
        if city in seen:
            continue
        seen.add(city)
        notes.append(f"「{surface}」在{city}，我按{city}{verb}")
    return notes
