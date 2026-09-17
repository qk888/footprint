"""
旅游攻略问答服务：用户提问 → 检索本地攻略知识库 → 把提问和参考资料交给模型总结

**流式版是唯一实现**，`rag_summary()` 只是把它排空（给用不了 async 的调用方）。

检索与兜底顺序：
  本地知识库（kb_retriever，3.4 万块维基导游）→ 命中就边生成边吐字
      ↓ 没命中 / 答完自称"资料里没写" / 问的是资料里根本没有的数字
  SearXNG 联网 → 拿真实网页摘要当参考资料再答一次
      ↓ 失败或无结果
  一条都不 yield（调用方按失败处理，交给 chat 兜底）

为什么改成流式：整段生成要等 3~13 秒才吐第一个字（实测最慢那条 = 本地生成 + 复核
+ 联网 1.1s + 联网再答 + 复核，共 4 次模型调用），而前端 `ChatView` 的 onChunk
本来就有"每读到一块就重绘"的写法，`api/agent.ts` 的注释也写着"仍按流读取以**兼容
未来逐字输出**" —— 缺的只是后端这一环。

与"非流式 + 整段复核"的**唯一行为差异**（刻意取舍，不是漏做）：
  检出"资料外的数字"时不再整段重写 —— 整段重写必须先拿到全文，那就等于没流式。
  改成在末尾追加一句更正（DISCLAIMER）。**代价**：用户可能先看到一个编出来的数字，
  再看到更正。为压低这个风险，进流之前先判 `_number_risk`：问的是价格/时间、
  而参考资料里一个数字都没有时，先去联网（实测"洪崖洞门票多少"正是这种：本地只有
  "4A 景区"这类介绍，模型硬编"门票 10 元"）—— 联网拿到真数字再流，模型有得抄。
"""
import asyncio
import re
from typing import AsyncIterator

from app.agent.travel.services.kb_retriever import get_kb_retriever
from app.agent.travel.services.web_search import get_web_search
from app.agent.utils.prompt_loader import load_rag_prompt
from app.agent.utils.logger_handler import logger
from langchain_core.prompts import PromptTemplate
from app.agent.model.factory import chat_model
from langchain_core.output_parsers import StrOutputParser

# 走联网时保留几条本地资料。本地 6 + 网络 6 = 12 条参考资料会把 1.5B 的注意力摊薄
LOCAL_KEEP_ON_WEB = 2

# 检索和联网都没结果时给调用方的话术。它同时是 harness 的失败信号
# （footprint_agent.FAIL_TEXTS 里有"暂时没找到相关资料"）—— 所以别随手改这个字面量
NOT_FOUND_TEXT = "抱歉，暂时没找到相关资料。"

# 检出"资料外的数字"后追加的更正：流式没法把已发出去的字收回来，只能这样兜
DISCLAIMER = "\n\n（以上价格和时间请以景区/官方最新公布为准）"

# ---------- 后置数字校验 ----------
# 小模型会编具体数字（实测"洪崖洞门票10元"、"喀纳斯票价60元"，而资料里写的是35元）。
# prompt 里已经写了"资料没写就直说、不许填数字"，但它服从率低 —— 所以用代码兜一层：
# 把回答里的**事实数字**跟参考资料比对，对不上就是编的。
#
# 只盯"数字 + 单位"的组合（价格/时长/时间）。不校验裸数字 —— 免得把列表编号、
# "3个景点"这种计数也当成可疑，把正常回答也拖去复核。
# 已知漏检：中文数字（"三天"、"两百元"）不在此列，属可接受的漏报。
_RE_FACT_NUM = re.compile(
    r"(\d+(?:\.\d+)?)\s*(块钱|元|块|万元|万|个月|月|天|日|小时|分钟|公里|千米|米|度|点)"
)

# 问的就是"硬数字"。配上"参考资料里没有任何数字"= 编造风险最高，进流前先去联网
_RE_ASK_NUMBER = re.compile(r"门票|价格|票价|多少钱|费用|几月|什么时候去|开放时间|几点|多久")

# 概数价格："几十元""上百块""十几万" —— 同样是编的（资料里通常没有这种表述），
# 而且比具体数字更误导：读起来像"大致就是这个价"。只盯**钱的单位**，
# 别把"几十米""十几分钟"也拉进来（那些是正常描述，误伤会让回答被追加免责声明）。
_RE_FUZZY_PRICE = re.compile(r"(几十|十几|上百|上千|数百)\s*(块钱|元|块|万)")

# 1.5B 会在事实回答后面接一句抒情反问（实测"洪水滔天、大河奔流的感觉是不是也很棒呢？"）。
# prompt 里已经禁止，但它服从率不稳，所以流式再加一道**止损**：见到反问标记就切在那儿。
# 流式发出去收不回，所以只能提前拦 —— 见下面 _answer_stream 的短滞留缓冲。
_RE_FILLER_TAIL = re.compile(r"(是不是也|是不是很|岂不|对吧[，,。？?]|不是吗|想不想去|要不要去看看)")
_TAIL_HOLD = 16          # 滞留字符数：跨 chunk 的标记要先攒够才认得出
_TAIL_SENT_END = "。！？!?\n"

# 回答里出现这些说法 = 本地资料没覆盖用户问的点 → 该联网补（这是我们自己在提示词里
# 指定的措辞，所以拿它当信号很准）
_NOT_FOUND_HINTS = (
    "资料里没写", "资料里没有写", "没有写具体信息", "资料中未",
    "未提及", "资料里没有", "资料中没有",
)


class RagSummaryService:
    def __init__(self):
        # 检索交给 KbRetriever 的单例（它自己持有向量连接），这里不再单独建一个
        self.prompt_text = load_rag_prompt()
        self.prompt_template = PromptTemplate.from_template(self.prompt_text)
        self.model = chat_model
        # 非流式链：只在"流式不可用"的兜底里用（拿到全文后还能过一遍中文检查）
        self.chain = self.prompt_template | self.model | StrOutputParser()
        # 流式链：到模型为止，不接 StrOutputParser —— 直接取 chunk 里的增量文本
        self.stream_chain = self.prompt_template | self.model

    # ---------------- 检索 ----------------
    def _build_local(self, query: str) -> list[str]:
        """本地知识库命中的块内容

        检索走 KbRetriever 的分层策略（有地名 → 限定城市范围；无地名 → 纯向量），
        比全局 top-1 准：全局 top-1 会被同词干扰带到别的城市
        （实测 "重庆三日游攻略" 的全局 top1 是西安）。

        miss 判定看**向量最短距离**：重排会因字面重合把某条往前挪，
        那条的原始 distance 可能偏大，拿"第一条"判会误报没找到。
        """
        kr = get_kb_retriever()
        hits = kr.search(query)
        # 命中判定交给 kb_retriever 的 is_relevant：光看 distance 会把
        # "火星上的酒店多少钱一晚"这种硬凑成命中（见那边的注释）
        if kr.is_relevant(query, hits):
            return [doc.page_content for doc, _ in hits]
        return []

    @staticmethod
    def _render(lines: list[str]) -> str:
        """统一编号成参考资料序列。

        **本地资料和网络资料必须用同一套编号** —— 之前分开标注（[参考资料i] / [网络资料i]）
        会让小模型读串，实测出现过"资料里没写这个。不过不用担心，免费的！"这种自相矛盾的回答。
        """
        return "".join(f"[参考资料{i}]: {t}\n" for i, t in enumerate(lines, 1))

    # ---------------- 护栏判据（eval_rag.py 直接调用它们，改签名记得同步）----------------
    @staticmethod
    def _unverified_numbers(answer: str, context: str) -> list[str]:
        """回答里"参考资料中不存在"的事实数字（价格/时长/时间），按出现顺序去重

        比对基准由调用方给：**要把用户自己说的话也并进去**，否则"按你3000元的预算"
        里的 3000 会被判成编造，点名复核又要求改写成"资料里没有写具体信息"。
        """
        known = {m.group(1) for m in _RE_FACT_NUM.finditer(context)}
        bad = []
        for m in _RE_FACT_NUM.finditer(answer):
            if m.group(1) not in known:
                bad.append(m.group(0).replace(" ", ""))
        # 概数价格一律当未核实：它不是"资料里的数字"，是模型猜的量级
        for m in _RE_FUZZY_PRICE.finditer(answer):
            token = m.group(0).replace(" ", "")
            if token not in context:
                bad.append(token)
        return list(dict.fromkeys(bad))

    @staticmethod
    def _looks_chinese(text: str) -> bool:
        """还是不是中文 —— 实测 1.5B 重写时会把整段漂成英文

        门槛只设 4 个汉字：**合法回答可以很短**（"洪崖洞免费。"）。原来卡"至少 20 个汉字"
        会把短中文整个丢掉（实测 '洪崖洞免费。'、'是的，门票免费。' 都判 False），
        用户在联网/复核路径上就白拿一句"抱歉，暂时没找到相关资料"。
        英文漂移靠汉字占比拦（实测整段英文的占比 < 5%，远低于 0.25）。
        """
        han = len(re.findall(r"[\u4e00-\u9fa5]", text))
        return han >= 4 and han / max(len(text), 1) >= 0.25

    @staticmethod
    def _reports_missing(answer: str) -> bool:
        return any(h in answer for h in _NOT_FOUND_HINTS)

    def _number_risk(self, query: str, context: str) -> bool:
        """在问价格/时间，但参考资料里一个数字都没有 → 编造风险最高，先联网再答"""
        return bool(_RE_ASK_NUMBER.search(query)) and not _RE_FACT_NUM.search(context)

    @staticmethod
    def _chunk_text(chunk) -> str:
        """AIMessageChunk.content 可能是 str，也可能是分块列表（多模态）"""
        content = getattr(chunk, "content", chunk)
        if isinstance(content, list):
            return "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        return content if isinstance(content, str) else ""

    # ---------------- 生成 ----------------
    async def _stream_answer(self, query: str, context: str) -> AsyncIterator[str]:
        """逐块吐出生成结果

        服务端不支持流式时退回整段生成 —— 但**只在"一块都还没吐出去"时**才退，
        否则会把同一段内容吐两遍。退回那次能拿到全文，顺手过一遍中文检查
        （流式路径发出去就收不回，只有这里还拦得住）。
        """
        emitted = False
        try:
            async for chunk in self.stream_chain.astream({"input": query, "context": context}):
                piece = self._chunk_text(chunk)
                if piece:
                    emitted = True
                    yield piece
            return
        except Exception as e:
            logger.warning(f"[rag] 流式生成失败: {type(e).__name__} {e}")
            if emitted:
                return      # 已经吐过一部分，不能再补一段整的（会重复）
        text = await asyncio.to_thread(
            self.chain.invoke, {"input": query, "context": context}
        )
        if text and not self._looks_chinese(text):
            logger.warning("[rag] 整段生成不是中文，弃用")
            return
        if text:
            yield text

    async def _answer_stream(self, query: str, context: str) -> AsyncIterator[str]:
        """生成 + 流式吐出；吐完再查数字，有问题就追加一句更正

        流式路上多一道"结尾反问止损"：**留下最后 16 个字符不吐**，等它们和下一个
        chunk 拼起来看清有没有反问标记 —— 有就切在上一句结尾并停手，没有就照常放行。
        滞留量很小（十几字符 ≈ 几十毫秒），首字延迟几乎不受影响。
        """
        buf: list[str] = []
        held = ""
        cut = False
        async for piece in self._stream_answer(query, context):
            buf.append(piece)
            if cut:
                continue                      # 已经切过了, 后面的一律不吐
            held += piece
            m = _RE_FILLER_TAIL.search(held)
            if m:
                head = held[: m.start()]
                keep = max((head.rfind(ch) for ch in _TAIL_SENT_END), default=-1)
                if keep >= 0:
                    yield head[: keep + 1]     # 只保留反问之前完整的句子
                logger.info("[rag] 结尾出现反问/抒情，已截断止损")
                cut = True
                continue
            if len(held) > _TAIL_HOLD:
                emit = len(held) - _TAIL_HOLD
                yield held[:emit]
                held = held[emit:]
        if not cut:
            m = _RE_FILLER_TAIL.search(held)
            if m:
                head = held[: m.start()]
                keep = max((head.rfind(ch) for ch in _TAIL_SENT_END), default=-1)
                if keep >= 0:
                    yield head[: keep + 1]
                logger.info("[rag] 结尾出现反问/抒情（收尾检查），已截断")
            else:
                yield held
        # 用户自己说的数字不算编造 → 比对基准里并上 query
        bad = self._unverified_numbers("".join(buf), context + "\n" + (query or ""))
        if bad:
            logger.warning(
                f"[rag] 回答含资料外的数字 {bad} → 流式模式只追加更正（不做整段重写）"
            )
            yield DISCLAIMER

    async def rag_summary_stream(self, query: str) -> AsyncIterator[str]:
        """流式回答。**一条都不吐 = 本地和联网都没结果**，调用方按失败处理

        为什么失败时不吐 NOT_FOUND_TEXT：旧行为里"没找到"不是给用户看的，而是让 harness
        摘掉 travel 交给 chat 兜底 —— 真吐出去就收不回来了。
        """
        local = await asyncio.to_thread(self._build_local, query)
        ctx = self._render(local)

        # ① 本地没命中 → 没有本地答案可先给，直接联网再流
        # ② 本地命中、但问的是资料里没有的数字 → 也先联网（这种最容易编）
        if not local or self._number_risk(query, ctx):
            web = await asyncio.to_thread(get_web_search().search, query)
            if web:
                logger.info("[rag] 进流前联网（本地未命中 / 问的是资料里没有的数字）")
                async for piece in self._answer_stream(
                    query, self._render(local[:LOCAL_KEEP_ON_WEB] + web)
                ):
                    yield piece
                return
            if not local:
                return          # 本地没有、联网也没有 → 交给调用方按失败处理

        # 本地作答（边生成边吐）
        buf: list[str] = []
        async for piece in self._answer_stream(query, ctx):
            buf.append(piece)
            yield piece

        # 回答自己说"资料里没写" → 追加一段联网内容（已经吐出去的部分不动）
        if self._reports_missing("".join(buf)):
            web = await asyncio.to_thread(get_web_search().search, query)
            if web:
                logger.info("[rag] 回答自称资料没写 → 追加联网补充")
                yield "\n\n另外查到一些：\n"
                async for piece in self._answer_stream(
                    query, self._render(local[:LOCAL_KEEP_ON_WEB] + web)
                ):
                    yield piece

    async def rag_summary(self, query: str) -> str:
        """一次性拿到完整回答（内部排空流式版，保证两条路径行为一致）"""
        text = "".join([piece async for piece in self.rag_summary_stream(query)])
        return text or NOT_FOUND_TEXT

    async def answer_from(self, query: str, lines: list[str]) -> str:
        """只用**调用方给的资料**作答（不检索本地、不联网）

        给"本地排不出行程 → 拿网络资料救场"用（见 `_direct_travel` 的天数超限分支）。
        走的是同一条 `_answer_stream`，所以编造数字的护栏、结尾反问的止损都还在。
        """
        if not lines:
            return ""
        return "".join([piece async for piece in self._answer_stream(query, self._render(lines))])
