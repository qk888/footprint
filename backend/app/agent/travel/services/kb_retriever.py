"""知识库分层检索

为什么不能直接用 Chroma 的全局相似度（实测依据）：
  1. 地名是中文旅游 query 最强的信号。全局检索时 "重庆三日游攻略" 的 top1 是**西安**
     （被"攻略"这类词的字面干扰带偏）；先识别出地名、把范围限定在该城市内，
     命中率 86% → 100%（三种检索模式都变准）。
  2. 反过来，泛化 query（"适合春天去的城市有哪些"）没有地名，纯字面检索完全无能
     （BM25 出的是"花蓮/马可·波罗的足迹"），必须靠向量语义 —— 此时**不能**限定范围。
  所以按"有没有地名"路由。**不要做 RRF 融合**：实测融合反而更差
  （"我下个月想去个暖和的地方" 纯向量 top 是哈尔滨，融合后变成"中国/月球/电话"，
   字面检索带进来的无关块会把向量的正确结果挤掉）。

零新依赖：
  项目用 uv + --frozen 锁依赖（见 Dockerfile），引入 jieba/rank_bm25 需要改 uv.lock；
  而向量已经由 Chroma 持久化好了，城市限定后向量表现已够（86%），
  再补一层"字符二元组重合度"重排，就能覆盖 "洪崖洞" 这类纯字面命中的专名。

地名字典也不打接口：直接从入库的攻略文件里解析块头，再并上项目已有的城市表。
实测 8412 个标题 → 7196 个地名，覆盖 89%，匹配耗时 0.0035ms。
"""
import os
import re
import threading

from app.agent.travel.services.vector_store import VectorStoreService
from app.agent.utils.config_handler import chroma_config
from app.agent.utils.logger_handler import logger
from app.agent.utils.path_tool import get_abs_path
from app.core.city_match import is_city_mention
from app.core.constants import CITY_ADCODE_MAP


class KbRetriever:
    # 章节名，不是地名。维基导游的块头有两种格式：
    #   `重庆 · 景点：正文`（带城市前缀）和 `住宿：正文`（不带前缀，title 直接就是章节名）
    # 后者的 title 落进地名字典后会**污染城市识别**：实测 find_cities("货币贬值对经济的影响")
    # 返回 ["货币"]，而 is_relevant 对"认得出地名"是无条件信任的 —— 于是财经问题被判成
    # "本地命中"，1.5B 拿旅游攻略硬答（"怎么在网上购物"/"宿舍住宿条件怎么样" 同样中招）。
    # 所以构造字典时把"不带城市前缀的章节块头"整块剔掉（_is_section_head），
    # 这里再兜一层显式名单，防的是"无前缀 + 无冒号"那种写法漏网。
    _SECTION_WORDS = frozenset(
        "了解 抵达 到达 离开 交通 市内交通 观光 景点 活动 购物 饮食 用餐 美食 餐厅 小吃 "
        "住宿 酒店 安全 治安 通讯 网络 货币 语言 签证 夜生活 娱乐 医疗 节日 节庆 "
        "气候 天气 穿衣 注意 提示 实用信息 概况 概览 简介 历史 文化 特色 攻略 游记 "
        "消费 门票 提醒 下一站 其他目的地 周边 其他".split()
    )

    def __init__(self):
        self.vs = VectorStoreService()
        self.geo: set[str] = set()
        self._ready = False
        self._lock = threading.Lock()

    # ---------------- 地名字典 ----------------
    @staticmethod
    def _is_section_head(chunk: str) -> bool:
        """块头是"章节名"（形如 `住宿：正文`）而不是"城市 · 章节"

        判据：块首行里，**冒号出现在 " · " 之前** —— 说明这个块直接以章节名开头，没有城市前缀。
        `重庆 · 景点：xxx` / `重庆/渝中区 · 景点：xxx` 都不满足；
        `重庆` 这种无冒号的裸地名也不满足（那是早期手写攻略的格式，仍是地名）。
        """
        first_line = chunk.strip().split("\n", 1)[0]
        for colon in ("：", ":"):
            i = first_line.find(colon)
            if i != -1:
                return " · " not in first_line[:i]
        return False

    @staticmethod
    def _head_of(chunk: str) -> str:
        """块头：'重庆 · 了解：正文...' -> '重庆'；'重庆/渝中区 · 景点：...' -> '重庆'"""
        head = re.split(r"[：\n]", chunk.strip(), 1)[0].strip()
        if " · " in head:
            head = head.split(" · ", 1)[0].strip()
        # 下辖条目（重庆/渝中区）的城市名取斜杠前一段，便于按城市过滤
        if "/" in head:
            head = head.split("/", 1)[0].strip()
        return head

    def _build_geo(self):
        data_dir = get_abs_path(chroma_config["data_path"])
        allow = tuple(chroma_config["allow_knowledge_file_type"])
        names: set[str] = set()
        dropped = 0
        try:
            files = [f for f in os.listdir(data_dir) if f.endswith(allow)]
        except OSError as e:
            logger.warning(f"[kb] 知识库目录读取失败({data_dir}): {e}")
            files = []
        for name in files:
            if not name.endswith(".txt"):
                continue
            try:
                with open(os.path.join(data_dir, name), encoding="utf-8") as f:
                    for part in f.read().split("###"):
                        if not part.strip():
                            continue
                        # 章节块（`住宿：...`）的 title 是章节名不是地名 —— 收进去会让
                        # "货币贬值""网上购物"这类问题被当成"认得出地名"而无条件判命中
                        if self._is_section_head(part):
                            dropped += 1
                            continue
                        head = self._head_of(part)
                        if head in self._SECTION_WORDS:
                            dropped += 1
                            continue
                        if 1 < len(head) <= self.geo_max_len and re.fullmatch(r"[\u4e00-\u9fa5]+", head):
                            names.add(head)
            except OSError as e:
                logger.warning(f"[kb] 读取 {name} 建地名字典失败: {e}")
        names |= set(CITY_ADCODE_MAP.keys())
        self.geo = names
        logger.info(
            f"[kb] 地名字典 {len(names)} 条(攻略块头 + 项目城市表), 剔除章节名 {dropped} 块"
        )

    def _ensure_ready(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            # 配置项都带默认值：chroma.yml 没加也能跑
            self.geo_max_len = int(chroma_config.get("kb_geo_max_len", 8))
            self.k = int(chroma_config.get("kb_k", 6))
            self.cand_mult = int(chroma_config.get("kb_candidate_mult", 4))
            self.lit_weight = float(chroma_config.get("kb_literal_weight", 0.35))
            # 判"命中"的硬阈值 + 没地名时的字面交集下限（见 is_relevant）
            self.miss_threshold = float(chroma_config.get("kb_miss_threshold", 0.55))
            self.literal_min = float(chroma_config.get("kb_literal_min", 0.15))
            self._build_geo()
            self._ready = True

    def is_relevant(self, query: str, hits) -> bool:
        """这次检索算不算"命中"（决定是本地作答，还是转联网/如实说没找到）

        为什么不能只看 distance：向量检索**永远**会返回"最像的那条"，距离只反映相对排序，
        不是绝对相关性。实测无关 query 的 top1 距离能低到 0.425
        （"火星上的酒店多少钱一晚" 命中了"香港·住宿"），比真命中的
        "洪崖洞门票多少"（0.443）还小 —— 单靠阈值根本分不开。

        所以叠一道绝对判据：
          - 认得出地名 → 信。城市限定已经把范围收窄到那座城了，命中的就是该城内容
          - 认不出地名 → 要求 query 和命中块有字面交集，避免被硬凑到不相关的条目上
        """
        self._ensure_ready()
        if not hits:
            return False
        if min(s for _, s in hits) > self.miss_threshold:
            return False
        if self.find_cities(query):
            return True
        return max(self._literal_ratio(query, doc.page_content) for doc, _ in hits) >= self.literal_min

    def find_cities(self, query: str) -> list[str]:
        """最长匹配（贪心、不重叠）：'从北京到杭州三日游' -> ['北京', '杭州']

        命中还要过 `is_city_mention`（见 core/city_match.py）：城市表里有一批和常用词
        同形的二字市名（三明/三明治、白山/长白山、日照/日照金山），裸匹配会把
        "长白山的雪景"认成"提到了白山市"，于是城市限定检索被带到白山市的内容上，
        而 is_relevant 对"认得出地名"是无条件信任的 —— 直接答错。
        """
        self._ensure_ready()
        hits: list[str] = []
        i, L = 0, len(query)
        while i < L:
            got = None
            for j in range(min(self.geo_max_len, L - i), 1, -1):
                cand = query[i:i + j]
                if cand in self.geo and is_city_mention(query, cand, i):
                    got = cand
                    break
            if got:
                hits.append(got)
                i += len(got)
            else:
                i += 1
        return hits

    # ---------------- 字面重合 ----------------
    @staticmethod
    def _literal_ratio(query: str, text: str) -> float:
        """query 与块的字符 n-gram 重合度(0~1)。不用分词，补向量对专名不敏感的短板。

        **3-gram 的权重是 2-gram 的两倍**：更长的组合更接近专名。
        实测 "洪崖洞门票多少" 里 "洪崖洞" 是 3-gram（全库只此一处），
        而 "门票" 是 2-gram（到处都有）—— 只按 2-gram 算，含 "门票¥150" 的峨眉山块
        会跟真正写到洪崖洞的块打平，把答案带偏。
        """
        n = len(query)
        if n < 2:
            return 0.0
        t2 = {text[i:i + 2] for i in range(len(text) - 1)}
        q2 = {query[i:i + 2] for i in range(n - 1)}
        r2 = len(q2 & t2) / len(q2) if q2 else 0.0
        if n < 3:
            return r2
        t3 = {text[i:i + 3] for i in range(len(text) - 2)}
        q3 = {query[i:i + 3] for i in range(n - 2)}
        r3 = len(q3 & t3) / len(q3) if q3 else 0.0
        return (r2 + 2 * r3) / 3

    # ---------------- 检索 ----------------
    def search(self, query: str, k: int | None = None, where: dict | None = None):
        """分层检索，返回 [(doc, chroma_distance)]

        返回的是**原始 distance**（不是重排后的分数）—— 调用方用它判断知识库是否 miss。
        """
        self._ensure_ready()
        k = k or self.k
        cities = self.find_cities(query) if where is None else []

        pool = []
        if where is not None:
            pool = self.vs.search_with_score(query, k=k * self.cand_mult, where=where)
        elif cities:
            # 先在该城市范围内找（范围从全库缩到单城，噪声自然消失）
            try:
                pool = self.vs.search_with_score(
                    query, k=k * self.cand_mult, where={"title": {"$in": cities}}
                )
            except Exception as e:
                logger.warning(f"[kb] 城市范围检索失败({cities}): {e}")
                pool = []
            # 该城内容少（小城市/新条目）时补足全局，别让提问落空
            if len(pool) < k:
                seen = {d.page_content for d, _ in pool}
                for d, s in self.vs.search_with_score(query, k=k * self.cand_mult):
                    if d.page_content not in seen:
                        pool.append((d, s))
        else:
            # 无地名 = 泛化 query，纯向量语义（这类 query 字面检索完全无能）
            pool = self.vs.search_with_score(query, k=k * self.cand_mult)

        if not pool:
            return []

        ranked = []
        for doc, dist in pool:
            lit = self._literal_ratio(query, doc.page_content)
            ranked.append((doc, dist, dist - self.lit_weight * lit, lit))
        ranked.sort(key=lambda x: x[2])          # distance 越小越相关，字面重合度高则再往前挪

        if ranked:
            logger.info(
                "[kb] %r 城市=%s 候选%d top距离%.3f(字面%.2f)"
                % (query[:24], cities or "-", len(ranked), ranked[0][1], ranked[0][3])
            )
        return [(d, dist) for d, dist, _, _ in ranked[:k]]


_retriever: KbRetriever | None = None
_retriever_lock = threading.Lock()


def get_kb_retriever() -> KbRetriever:
    """惰性单例：地名字典在首次检索时构建（约 1 秒，读一遍知识库文件）"""
    global _retriever
    if _retriever is None:
        with _retriever_lock:
            if _retriever is None:
                _retriever = KbRetriever()
    return _retriever
