"""记忆的向量索引(语义召回)

为什么需要它:
    长期记忆一多, prompt_block 原来只按"最近 60 条 + 类别权重"挑 6 条注入。
    用户半年前说过的"海鲜过敏"会被后来的流水记忆挤出候选窗口, 等于永久丢失。
    向量检索能按语义把"很久以前、但跟当前这句话相关"的记忆捞回来。

三条设计取舍(和项目既有约定保持一致):
    1) 向量库只当"召回器", 不存真相。召回的 id 一律回 MySQL 核对 status/needs_review,
       所以改口/失效/删除都不需要同步向量库 —— 天然不会漏(以库为准)。
    2) 索引写入是惰性的(检索时对账 + 后台补建, 带节流), 不去 remember/confirm/delete
       几个写入点逐个插代码。少改一处就少一个漏点。
    3) 任何一步失败都退回规则通道: 语义检索是增强项, 绝不能让聊天挂掉。

存储就复用知识库那个 chroma_db 持久卷, 单独开一个 collection, 不引新依赖。
embedding 复用 embed-model 容器(m3e, 768 维), 单条 query 约 30ms。
"""

import asyncio
import logging
import time

import chromadb

from app.agent.model.factory import embedding_model
from app.agent.utils.config_handler import chroma_config
from app.agent.utils.path_tool import get_abs_path

logger = logging.getLogger(__name__)

#记忆向量所在的 collection(和知识库 travel_guide / slot_examples 并存于同一持久目录)
MEMORY_COLLECTION = chroma_config.get("memory_collection_name", "user_memories")
#生效中的记忆超过这个条数才启用语义检索: 条数少时规则通道又快又准, 没必要花 embedding
VECTOR_MIN_COUNT = int(chroma_config.get("memory_vector_min_count", 200))
#每次语义召回的条数
VECTOR_K = int(chroma_config.get("memory_vector_k", 10))
#同一用户的索引对账节流(秒): 避免每轮聊天都去扫一遍向量库
SYNC_MIN_INTERVAL = int(chroma_config.get("memory_sync_interval", 60))
#一轮后台补建最多灌多少条: 防止首次几百条记忆把 embed 服务打满
SYNC_BATCH = int(chroma_config.get("memory_sync_batch", 50))


class MemoryIndex:
    """记忆向量索引: 只负责"按语义召回", 不负责存真相。"""

    def __init__(self):
        self._collection = None
        self._last_sync: dict[int, float] = {}
        self._syncing: set[int] = set()

    # ---------------- 底层 ----------------
    def _col(self):
        if self._collection is None:
            client = chromadb.PersistentClient(
                path=get_abs_path(chroma_config["persist_directory"])
            )
            self._collection = client.get_or_create_collection(
                name=MEMORY_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    @staticmethod
    def _doc_id(memory_id: int) -> str:
        return f"m{int(memory_id)}"

    @staticmethod
    def _sid(doc_id) -> int | None:
        try:
            return int(str(doc_id)[1:])
        except (ValueError, TypeError):
            return None

    #已索引的 {memory_id: 入库时的 update_time 字符串}
    def indexed_map(self, user_id: int) -> dict[int, str]:
        try:
            res = self._col().get(where={"user_id": int(user_id)}, include=["metadatas"])
        except Exception as e:
            logger.warning(f"[memory_index] 读取索引失败: {e}")
            return {}
        out: dict[int, str] = {}
        for doc_id, meta in zip(res.get("ids") or [], res.get("metadatas") or []):
            memory_id = self._sid(doc_id)
            if memory_id is not None:
                out[memory_id] = str((meta or {}).get("updated_at") or "")
        return out

    #该用户已索引的条数(给管理页看)
    def count(self, user_id: int) -> int:
        return len(self.indexed_map(user_id))

    # ---------------- 写入 ----------------
    #把这些记忆灌进索引(按 memory_id 幂等覆盖)
    async def upsert(self, user_id: int, memories) -> int:
        if not memories:
            return 0
        ids = [self._doc_id(m.id) for m in memories]
        texts = [m.content for m in memories]
        metas = [
            {
                "user_id": int(user_id),
                "kind": m.kind,
                "updated_at": m.update_time.isoformat(),
            }
            for m in memories
        ]
        vectors = await embedding_model.aembed_documents(texts)
        await asyncio.to_thread(
            self._col().upsert,
            ids=ids,
            embeddings=vectors,
            metadatas=metas,
            documents=texts,
        )
        return len(ids)

    # ---------------- 对账 / 补建 ----------------
    #把库里已有但没进索引(或内容变过)的记忆补进索引。节流 + 后台跑, 不阻塞聊天。
    async def ensure_indexed(self, user_id: int, active_memories) -> None:
        now = time.time()
        if user_id in self._syncing:
            return
        indexed = self.indexed_map(user_id)
        #索引是空的(从没建过 / 刚被清掉)就不受节流限制, 必须立刻补 ——
        #否则"清空索引后 60 秒内重建"这种事会被节流挡掉, 静默地一直搜不到
        if indexed and now - self._last_sync.get(user_id, 0) < SYNC_MIN_INTERVAL:
            return
        todo = [
            m for m in active_memories
            if indexed.get(m.id) != m.update_time.isoformat()
        ][:SYNC_BATCH]
        self._last_sync[user_id] = now
        if not todo:
            return
        self._syncing.add(user_id)
        asyncio.create_task(self._background_upsert(user_id, todo))

    async def _background_upsert(self, user_id: int, memories) -> None:
        try:
            n = await self.upsert(user_id, memories)
            logger.info(f"[memory_index] 用户{user_id} 补建索引 {n} 条")
        except Exception as e:
            logger.warning(f"[memory_index] 用户{user_id} 补建索引失败: {e}")
        finally:
            self._syncing.discard(user_id)

    # ---------------- 检索 ----------------
    #语义召回, 返回 {memory_id: 相似度(0~1]}。失败返回空字典 → 调用方退回规则通道
    async def recall(self, user_id: int, query: str, k: int = VECTOR_K) -> dict[int, float]:
        try:
            qv = await embedding_model.aembed_query(query)
            res = await asyncio.to_thread(
                self._col().query,
                query_embeddings=[qv],
                n_results=k,
                where={"user_id": int(user_id)},
            )
        except Exception as e:
            logger.warning(f"[memory_index] 语义召回失败, 退回规则通道: {e}")
            return {}
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: dict[int, float] = {}
        for doc_id, dist in zip(ids, dists):
            memory_id = self._sid(doc_id)
            if memory_id is None:
                continue
            #1/(1+距离) 对 cosine 和 l2 都单调递减, 不必关心 collection 用的是哪种度量
            out[memory_id] = 1.0 / (1.0 + max(0.0, float(dist)))
        return out

    # ---------------- 重建 ----------------
    #清掉该用户的全部索引(重建前先清, 避免旧向量残留)
    async def clear(self, user_id: int) -> None:
        try:
            await asyncio.to_thread(
                self._col().delete, where={"user_id": int(user_id)}
            )
        except Exception as e:
            logger.warning(f"[memory_index] 清理旧索引失败: {e}")

    #告诉索引"这批已经对齐过了", 免得重建完立刻又被惰性补建扫一遍
    def mark_synced(self, user_id: int) -> None:
        self._last_sync[user_id] = time.time()


memory_index = MemoryIndex()
