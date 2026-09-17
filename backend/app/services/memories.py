import logging

from app.core.constants import ADCODE_CITY_MAP, CATEGORY_ENUM_MAP, CITY_ADCODE_MAP
from app.core.city_match import find_cities
from app.crud.memories import MemoryRepository
from app.services.memory_index import (
    VECTOR_K as MEMORY_VECTOR_K,
    VECTOR_MIN_COUNT as MEMORY_VECTOR_MIN_COUNT,
    memory_index,
)
from fastapi import HTTPException

logger = logging.getLogger(__name__)

#记忆类别的中文标签(展示 + 拼提示词都用它)
KIND_LABEL = {
    "city_light": "点亮城市",
    "bill": "消费记录",
    "budget": "预算",
    "plan": "行程计划",
    "manual": "手动记录",
    "chat": "聊天记住",
}

#单条记忆最长字数(数据库字段 500)
MAX_CONTENT_LEN = 500

#提示词记忆块的预算: 条数 + 总字数(本地模型上下文只有 4096, 这块不能超过 400 字)
BLOCK_MAX_ITEMS = 6
BLOCK_ITEM_LEN = 60
BLOCK_MAX_CHARS = 400

#规则通道的候选上限: 按意图类别过滤后, 先取最近的这些条再做加权排序
RULE_CANDIDATE_LIMIT = 60
#做索引对账时一次最多拉多少条生效中记忆(含待确认); 超出这个量的历史靠 /memory/reindex 全量重建
VECTOR_INDEX_LIMIT = 5000
#语义相似度在排序里的权重: 相似度 0~1, 乘完最多加 6 分, 刚好压过"人说的偏好"(5 分)
VECTOR_SCORE_WEIGHT = 6.0

#按意图只注入相关类别: 记账时不需要塞行程计划, 闲聊时更不该把消费流水倒进去
INJECT_KINDS = {
    "travel": ("manual", "chat", "budget", "plan", "city_light"),
    "operation": ("manual", "chat", "budget"),
    "chat": ("manual", "chat"),
}
#类别权重(同分时优先注入"人说的偏好", 而不是系统记的事件)
KIND_WEIGHT = {"manual": 5, "chat": 4, "budget": 3, "plan": 2, "city_light": 1}
#当前句里出现这些词就按对应意图过滤(和 agent 层的三分类同源, 这里只用来挑记忆)
TRAVEL_HINT = ("规划", "行程", "攻略", "景点", "美食", "去哪", "怎么玩", "旅游", "旅行", "预算", "几日", "几天", "路线")
OPERATION_HINT = ("记账", "记消费", "花了", "花费", "消费", "点亮", "去过", "账单", "查账", "记住", "回忆", "偏好", "习惯")


#猜当前这句话的意图(纯关键词, 不调模型)
def _guess_intent(query: str) -> str:
    q = query or ""
    if any(k in q for k in OPERATION_HINT):
        return "operation"
    if any(k in q for k in TRAVEL_HINT):
        return "travel"
    return "chat"


#句子里提到的城市(城市表固定, 字符串匹配就够可靠 —— 但要走 find_cities:
# 它额外做邻字判据("三明治"≠三明)和最长匹配("马鞍山"≠鞍山))
def _guess_city(query: str) -> str:
    hit = find_cities(query or "")
    return hit[0] if hit else ""


#记忆块里的单条内容截断
def _clip(content: str):
    return content if len(content) <= BLOCK_ITEM_LEN else content[:BLOCK_ITEM_LEN] + "…"


#业务事件写记忆: 记忆是附属品, 失败只记日志, 绝不能拖垮点亮/记账等主流程
async def remember_event(session, user_id: int, kind: str, content: str):
    try:
        await MemoryRepository(session).remember(
            user_id=user_id, kind=kind, content=content, source="event"
        )
    except Exception as e:
        logger.warning(f"用户{user_id}写入{kind}记忆失败: {e}")


#业务事件撤销时抹掉对应记忆: 同样只记日志, 不影响主流程
async def forget_event(session, user_id: int, kind: str, content: str):
    try:
        await MemoryRepository(session).forget(user_id=user_id, kind=kind, content=content)
    except Exception as e:
        logger.warning(f"用户{user_id}抹掉{kind}记忆失败: {e}")


#内容会变的记忆(预算金额等)先按开头清掉旧的再写新的, 免得两句自相矛盾
async def replace_event(session, user_id: int, kind: str, prefix: str, content: str):
    try:
        repo = MemoryRepository(session)
        await repo.forget_prefix(user_id=user_id, kind=kind, prefix=prefix)
        await repo.remember(user_id=user_id, kind=kind, content=content, source="event")
    except Exception as e:
        logger.warning(f"用户{user_id}更新{kind}记忆失败: {e}")


#分类说法: "其他"分类用用户自己填的名字
def category_label(category: int, custom_category: str | None = None):
    if category == 6 and custom_category:
        return custom_category
    return CATEGORY_ENUM_MAP.get(category, "其他")


#城市编码转城市名(查不到就原样返回编码)
def city_name(adcode: str):
    return ADCODE_CITY_MAP.get(adcode, adcode)


class MemoryService:
    def __init__(self, repo: MemoryRepository):
        self.repo = repo      # user_memories表的数据库操作

    #新增记忆(管理页手写 / 聊天工具调用 / 记忆直达通道)
    #needs_review 显式传入时以它为准; 不传则: source=chat(模型自己决定记的) → 只当候选
    async def remember(
        self,
        user_id: int,
        content: str,
        kind: str = "manual",
        source: str = "manual",
        needs_review: bool | None = None,
    ):
        content = (content or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="记忆内容不能为空")
        if kind not in KIND_LABEL:
            raise HTTPException(status_code=400, detail="记忆类别不存在")
        if needs_review is None:
            needs_review = source == "chat"
        return await self.repo.remember(
            user_id=user_id,
            kind=kind,
            content=content[:MAX_CONTENT_LEN],
            source=source,
            needs_review=needs_review,
        )

    #确认一条待确认的记忆
    async def confirm_memory(self, user_id: int, memory_id: int):
        result = await self.repo.confirm_memory(user_id, memory_id)
        if not result:
            raise HTTPException(status_code=400, detail="记忆不存在或已失效")
        return result

    #按关键词召回记忆
    async def recall(self, user_id: int, keyword: str, limit: int = 5):
        keyword = (keyword or "").strip()
        if not keyword:
            return await self.repo.list_memories(user_id, limit)
        return await self.repo.search(user_id, keyword, limit)

    #获取记忆列表
    async def list_memories(self, user_id: int, limit: int = 100):
        return await self.repo.list_memories(user_id, limit)

    #偏好 → 规划可用的结构化开关（见 travel/services/preference_filter.py）
    async def preference_tags(self, user_id: int) -> dict:
        """把"我记下的偏好"翻译成规划能用的开关

        只用**已确认**的记忆（needs_review=0）：待确认的候选还没经过用户点头，
        拿它去改行程等于擅自作主（"我喜欢安静的位置"这种顺手一说也会被算进去）。
        整体 try 住：偏好拿不到不该让规划直接失败。
        """
        try:
            from app.agent.travel.services.preference_filter import tags_from_texts
            rows = await self.repo.list_memories(user_id, 100)
            texts = [m.content for m in rows
                     if getattr(m, "status", "") == "active" and not getattr(m, "needs_review", 0)]
            tags = tags_from_texts(texts)
            hit = [k for k, v in tags.items() if v]
            if hit:
                logger.info(f"用户{user_id}偏好标签: {hit}")
            return tags
        except Exception as e:
            logger.warning(f"偏好标签提取失败（忽略）: {e}")
            return {}

    #删除单条记忆
    async def delete_memory(self, user_id: int, memory_id: int):
        result = await self.repo.delete_memory(user_id, memory_id)
        if not result:
            raise HTTPException(status_code=400, detail="记忆不存在")
        return result

    #清空全部记忆
    async def delete_all(self, user_id: int):
        return await self.repo.delete_all(user_id)

    #拼进系统提示词的记忆块(没有记忆就返回空串)
    #本地模型只有 4096 上下文, 还要装系统提示词和工具定义, 所以只按需带少数几条:
    #  1) 按 query 猜意图 → 只取相关类别(记账不塞行程, 闲聊不倒消费流水)
    #  2) 只取生效中且用户已确认的(模型自己写的候选不自动注入)
    #  3) 记忆条数过阈值时再加一路语义召回 —— 否则半年前说的"海鲜过敏"会被
    #     后来的流水记忆挤出"最近 60 条"候选窗口, 等于永久丢失
    #  4) 命中当前句提到的城市优先, 其次按类别权重(人说的偏好 > 系统记的事件)
    #  5) 条数 + 总字数双预算, 超了就丢最不相关的
    async def prompt_block(self, user_id: int, query: str = "", limit: int = BLOCK_MAX_ITEMS):
        intent = _guess_intent(query)
        kinds = INJECT_KINDS.get(intent, INJECT_KINDS["chat"])
        city = _guess_city(query)

        active_total = await self.repo.count_active(user_id)
        pool = list(
            await self.repo.list_active(user_id, kinds=kinds, limit=RULE_CANDIDATE_LIMIT)
        )

        #语义召回: 向量库只负责把可能相关的 id 捞出来, 状态一律回 MySQL 核对,
        #所以改口/失效/删除都不用去同步向量库(以库为准)
        vec_scores: dict[int, float] = {}
        if query and active_total >= MEMORY_VECTOR_MIN_COUNT:
            active_all = await self.repo.list_active(
                user_id, limit=VECTOR_INDEX_LIMIT, include_pending=True
            )
            await memory_index.ensure_indexed(user_id, active_all)   # 惰性补建, 后台执行
            hits = await memory_index.recall(user_id, query, k=MEMORY_VECTOR_K * 2)
            if hits:
                known = {m.id for m in pool}
                extra = await self.repo.list_active_by_ids(
                    user_id, [i for i in hits if i not in known]
                )
                #语义命中可以突破"意图类别"的限制(相关就是相关),
                #但消费流水永远不进提示词 —— 这是硬规矩
                pool += [m for m in extra if m.kind != "bill"]
                valid = {m.id for m in pool}
                vec_scores = {i: s for i, s in hits.items() if i in valid}

        if not pool:
            return ""

        def rank(m):
            score = KIND_WEIGHT.get(m.kind, 1)
            if city and city in m.content:
                score += 10          # 跟当前这句话同一座城市的记忆最相关
            if m.id in vec_scores:
                score += VECTOR_SCORE_WEIGHT * vec_scores[m.id]
            return (-score, -m.update_time.timestamp())

        lines, budget = [], BLOCK_MAX_CHARS
        for m in sorted(pool, key=rank):
            line = f"- {KIND_LABEL.get(m.kind, m.kind)}：{_clip(m.content)}（{m.update_time:%Y-%m-%d}）"
            if len(line) > budget:
                continue                  # 预算不够就跳过这条, 后面的短条目还能进
            lines.append(line)
            budget -= len(line)
            if len(lines) >= limit:
                break
        if not lines:
            return ""
        extra_tag = f" +语义{len(vec_scores)}" if vec_scores else ""
        logger.info(
            f"[memory] 注入{len(lines)}条({intent}{'/' + city if city else ''}{extra_tag})"
        )
        return (
            "\n【用户长期记忆】\n"
            "下面是你在以往会话里为该用户记下的信息，跨会话有效，回答时直接当作已知事实使用，"
            "但不要主动向用户罗列这些条目：\n" + "\n".join(lines) + "\n"
        )

    #记忆检索的体检数据(管理页展示: 有多少条、语义检索是否已经启用)
    async def stats(self, user_id: int):
        return {
            "active": await self.repo.count_active(user_id),
            "vector_enabled": await self.repo.count_active(user_id) >= MEMORY_VECTOR_MIN_COUNT,
            "vector_min_count": MEMORY_VECTOR_MIN_COUNT,
            "vector_indexed": memory_index.count(user_id),
        }

    #重建该用户的记忆向量索引(换向量模型 / 索引损坏 / 上线前批量灌历史记忆)
    #分页灌: 记忆条数可能上万, 一次全拉进内存没必要
    async def rebuild_index(self, user_id: int, page: int = 500):
        await memory_index.clear(user_id)
        total, offset = 0, 0
        while True:
            batch = await self.repo.list_active(
                user_id, limit=page, include_pending=True, offset=offset
            )
            if not batch:
                break
            total += await memory_index.upsert(user_id, batch)
            offset += len(batch)
            if len(batch) < page:
                break
        memory_index.mark_synced(user_id)
        logger.info(f"[memory_index] 用户{user_id} 重建索引 {total} 条")
        return total
