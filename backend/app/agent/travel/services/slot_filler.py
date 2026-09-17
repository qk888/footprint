"""
槽位填充服务: 从 RAG 检索相似示例, 喂给模型填出旅行约束
"""

import os, re, httpx
from app.agent.travel.services.vector_store import VectorStoreService
from app.core.constants import CITY_ADCODE_MAP
from app.core.city_match import find_cities as _find_cities, find_province
from langchain_core.documents import Document

LLM_BASE  = os.environ.get("LLM_API_BASE", "http://127.0.0.1:8080/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen")


def _int(s, d):
    try: return int("".join(c for c in s if c.isdigit()))
    except Exception: return d


def _norm_city(city):
    """城市名不在映射表里就打回?"""
    return city if city in CITY_ADCODE_MAP else "?"


# "从A到B"句式是原文里的确定性信息, 不该指望小模型每次都抽出来
# (实测同一句"从北京坐飞机到海南玩一周", 目的地时而是海南时而是?)。
# 只在模型漏填时兜底, 抽出来的名字必须能对上城市表。
# 注意: 触发词不带裸"飞"("坐飞机到X"里左most的"飞"会把"机到X"整个吃进去)。
_RE_FROM_CITY = re.compile(
    r"从([\u4e00-\u9fa5]{2,6}?)"
    r"(?=(?:坐|乘|搭|开|自驾)?(?:飞机|高铁|火车|动车|大巴|车|到|去|飞往|前往|飞到))"
)
_RE_TO_CITY = re.compile(
    r"(?:到|去|飞往|前往|飞到)([\u4e00-\u9fa5]{2,6}?)"
    r"(?=(?:玩|旅游|度假|旅居|几天|一周|一星期|半个月|天|的|是|有|怎么|多少|，|。|,|\.|！|!|？|\?|$))"
)


def _regex_city(query: str, which: str) -> str:
    """从'从A…到B'句式里抽城市; 抽不到或不像城市名返回 '?'"""
    m = (_RE_FROM_CITY if which == "start" else _RE_TO_CITY).search(query)
    if not m:
        return "?"
    name = m.group(1)
    if any(ch in name for ch in "机到去往回从和与"):
        return "?"                      # 把连接词吃进去了, 说明切错了
    # 边界词没列全(吃火锅/看日出…)时允许按"最长前缀"对上城市表
    for i in range(len(name), 1, -1):
        if name[:i] in CITY_ADCODE_MAP:
            return name[:i]
    return "?"


async def _call_llm(prompt):
    """异步调 LLM，先试 /completions 再试 /chat/completions，避免阻塞事件循环"""
    # trust_env=False: 绕过系统代理(Windows 代理会劫持 127.0.0.1 → 502), 直接连本地模型
    async with httpx.AsyncClient(base_url=LLM_BASE, timeout=60.0, trust_env=False) as client:
        for ep, payload in [
            ("/completions", {
                "model": LLM_MODEL, "temperature": 0.1, "max_tokens": 2000, "prompt": prompt,
                "stop": ["\n\n", "需求:"],
            }),
            ("/chat/completions", {
                "model": LLM_MODEL, "temperature": 0.1, "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
                "stop": ["\n\n", "需求:"],
            }),
        ]:
            try:
                r = await client.post(ep, json=payload)
                data = r.json()
                if ep == "/completions":
                    text = data["choices"][0]["text"].strip()
                    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
                    if text: return text
                else:
                    m = data["choices"][0]["message"]
                    c = m.get("content", "") or ""
                    rc = m.get("reasoning_content", "") or ""
                    ret = (c or rc).strip()
                    if ret: return ret
            except Exception: continue
    return ""


class SlotFillerService:
    def __init__(self):
        self.vector_store = VectorStoreService()
        self.retriever = self.vector_store.get_slot_retriever()

    #检索相似示例
    def retrieve_examples(self, query: str) -> list[Document]:
        return self.retriever.invoke(query)

    #拼提示词: 槽位说明 + 检索到的示例 + 当前query
    def _build_prompt(self, query: str, docs: list[Document]) -> str:
        lines = ['提取旅行需求，只输出以下字段，如果天数,预算,人数信息不知道的填"?"', "", "示例"]
        for doc in docs:
            slots = doc.metadata["slots"].replace(" ", "\n")
            lines += [f"需求: {doc.page_content}", "输出:", slots, ""]
        lines += [f"需求: {query}", "输出:"]
        return "\n".join(lines)

    #解析模型输出 → 约束dict
    #字段名清单必须和下面 k in (...) 的分支一一对应
    FIELD_RE = re.compile(
        r"\s*(出发地|出发|起点|始发|from|目的地|目的城市|目的|终点|目标|to"
        r"|天数|时长|时间|天|days|预算|费用|花费|budget|人数|人员|people)\s*[:：]?",
        re.IGNORECASE,
    )

    def _parse(self, raw: str) -> dict:
        c = {"start_city": "?", "target_city": "?", "days": 1, "budget": 5000, "people_number": 1}
        raw = (raw or "").replace("；", "\n").replace(";", "\n").replace("？", "?")
        # 实测模型有时不写冒号("出发北京"而非"出发:北京"), 也有时字段挤一行用空格隔开。
        # 在每个字段名前强制断行并补上统一冒号(冒号本身可选匹配), 再按行解析。
        raw = self.FIELD_RE.sub(lambda m: f"\n{m.group(1)}:", raw)
        for line in raw.strip().split("\n"):
            if ":" not in line: continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            v = re.sub(r'[<>]', '', v).strip()
            if not v: continue
            if k in ("出发","from","出发地","起点","始发"): c["start_city"] = v
            elif k in ("目的","to","目的地","终点","目的城市","目标"): c["target_city"] = v
            elif k in ("天数","days","时长","时间","天"): c["days"] = max(1, _int(v, 1))
            elif k in ("预算","budget","费用","花费"): c["budget"] = max(100, _int(v, 5000))
            elif k in ("人数","people","人员"): c["people_number"] = max(1, _int(v, 1))
        em = {"beijing": "北京", "shanghai": "上海", "guangzhou": "广州", "shenzhen": "深圳",
              "chengdu": "成都", "chongqing": "重庆", "hangzhou": "杭州", "nanjing": "南京",
              "wuhan": "武汉", "suzhou": "苏州"}
        c["start_city"] = em.get(c["start_city"].lower(), c["start_city"])
        c["target_city"] = em.get(c["target_city"].lower(), c["target_city"])
        # 城市名不在映射表里就打回?
        c["start_city"] = _norm_city(c["start_city"])
        c["target_city"] = _norm_city(c["target_city"])
        c["overall_budget"] = c["budget"]
        return c

    #填槽
    async def fill_slots(self, query: str, override: dict | None = None) -> dict:
        docs = self.retrieve_examples(query)
        prompt = self._build_prompt(query, docs)
        raw = await _call_llm(prompt)
        c = self._parse(raw)
        # 城市幻觉校验: 填出的城市若没在用户原文出现, 是小模型从示例里抄的, 打回"?"
        # (用户真提到的城市一定在原文里; 打回后 solve 会按同城模式 start=target 处理)
        for key in ("start_city", "target_city"):
            if c[key] != "?" and c[key] not in query:
                c[key] = "?"
        # "从A到B"正则兜底: 模型漏填哪个就按原文补哪个, 抽不出来维持"?"
        if c["start_city"] == "?":
            c["start_city"] = _regex_city(query, "start")
        if c["target_city"] == "?":
            c["target_city"] = _regex_city(query, "to")
        # 极简说法兜底(如"广州旅游"): 两个槽位都空时, 直接从原文匹配已知城市当目的地
        # (走 find_cities: 过邻字判据 + 最长匹配优先)
        if c["start_city"] == "?" and c["target_city"] == "?":
            hit = _find_cities(query)
            if hit:
                c["target_city"] = hit[0]
        # 省名兜底(如"帮我规划云南五天"): 省名不在城市表, 上面几步全抽不到 ——
        # 实测会退化成"请告诉我你想去哪个城市"的追问, 用户明明已经说了云南。
        # 这里**保留省名**交给 solve 的 normalize_city 归一(云南→昆明),
        # 好处是卡片上会带上"「云南」是省，先按昆明给你排"那句说明；
        # 归一到具体城市就没这句了, 用户会疑惑"我说的是云南, 怎么变成昆明了"。
        if c["target_city"] == "?":
            prov = find_province(query)
            if prov:
                c["target_city"] = prov
        # 会话槽位兜底放最后: 它是规则抽取出来的确定性值, 比小模型可靠。
        # 小模型是 few-shot 填槽, 经常把示例里的"天数:1"照抄过来 —— 这就是
        # "要两天只出一天"的成因。只有槽位里确实有值时才覆盖, 没有就不动。
        for key in ("start_city", "target_city", "days", "budget", "people_number"):
            val = (override or {}).get(key)
            if val not in (None, "", "?", 0):
                c[key] = val
        c["overall_budget"] = c["budget"]
        return c


# 单例：VectorStoreService 初始化会建 Chroma 连接，复用避免每次请求重建
_slot_filler: SlotFillerService | None = None

def get_slot_filler() -> SlotFillerService:
    global _slot_filler
    if _slot_filler is None:
        _slot_filler = SlotFillerService()
    return _slot_filler
