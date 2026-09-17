"""联网搜索（SearXNG）

定位：**本地知识库查不到时才用它**。顺序是

    本地 RAG（kb_retriever）→ 命中就直接答
        ↓ 没命中，或答完校验发现"资料里没写具体信息"
    SearXNG → 拿真实网页摘要当参考资料再答一次
        ↓ 失败 / 无结果
    如实说没找到

为什么不干脆全走联网：
  1. 本地已有 3.4 万块攻略，离线、0 延迟、还不消耗上游额度。
     SearXNG 上游限流是真实存在的 —— 实测连续快速请求会被压到只剩一个引擎有结果。
  2. 联网回来的是碎片摘要，不如本地攻略成体系；能本地答的就没必要联网。

几个实测出来的点：
  - **只取 title + content（摘要），不抓正文**：抓正文慢，而且容易被反爬
  - **必须走缓存**：同一 query 短时间内重复打上游会被限流
  - **空结果也缓存，但只留 300 秒**（`empty_cache_ttl`）：本地库真没有的信息会被反复问到，
    不缓存就等于每次都去打上游；但空结果也可能是上游临时抽风，所以不敢按 1 小时留
  - **任何异常都返回空列表**，绝不能让联网把问答链路搞挂
  - 容器里访问走服务名 `http://searxng:8080/search`（配置在 config/web_search.yml）
  - **返回列表而不是拼好的文本**：调用方要把本地资料和网络资料**统一编号**成一个
    参考资料序列。之前分开标注（[参考资料i] / [网络资料i]）会让小模型读串，
    实测出现过"资料里没写这个。不过不用担心，免费的！"这种自相矛盾的回答。
"""
import hashlib
import json
import os
import urllib.parse
import urllib.request

from app.agent.utils.config_handler import web_search_config
from app.agent.utils.logger_handler import logger

_cache = None
_cache_ready = False


def _get_cache():
    """拿项目的 Redis 做缓存。

    这里用**同步**客户端：调用方 rag_summary 跑在 asyncio.to_thread 的线程里，
    用不了 cache_config 里那个 redis.asyncio 客户端。redis 包本身已经装了，不需要新依赖。
    """
    global _cache, _cache_ready
    if _cache_ready:
        return _cache
    _cache_ready = True
    try:
        import redis as redis_lib

        _cache = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    except Exception as e:
        logger.warning(f"[web_search] 缓存不可用，将每次直连搜索: {e}")
        _cache = None
    return _cache


class WebSearchService:
    def __init__(self):
        self.cfg = web_search_config or {}

    def search(self, query: str) -> list[str]:
        """返回 ["标题：摘要", ...]；无结果或失败返回空列表"""
        if not self.cfg.get("enabled", True):
            return []
        query = (query or "").strip()
        if not query:
            return []

        # 用 md5 而不是截断原文：截断到前 80 字符会让"前 80 字相同"的不同 query 共用缓存
        key = "web_search:" + hashlib.md5(query.encode("utf-8")).hexdigest()
        cache = _get_cache()
        if cache is not None:
            try:
                hit = cache.get(key)
                if hit:
                    lines = json.loads(hit)
                    # 空结果也走缓存（"[]" 是真值，不会被误判成 miss）
                    logger.info(f"[web_search] 命中缓存({len(lines)}条): {query[:24]}")
                    return lines
            except Exception as e:
                logger.warning(f"[web_search] 读缓存失败: {e}")

        lines = self._fetch(query)
        if cache is not None:
            # **空结果也缓存，但 TTL 短得多**：本地知识库真没有的信息（比如反复被问的
            # 某个景点门票）会一直被问到，不缓存就等于每次都去打上游 1~8 秒，而上游限流。
            # 空结果不敢久留 —— 也可能是上游临时抽风，缓存一小时就把"暂时查不到"
            # 变成了"一小时内的既定事实"。
            ttl_key = "cache_ttl" if lines else "empty_cache_ttl"
            default_ttl = 3600 if lines else 300
            try:
                cache.setex(key, int(self.cfg.get(ttl_key, default_ttl)),
                            json.dumps(lines, ensure_ascii=False))
            except Exception as e:
                logger.warning(f"[web_search] 写缓存失败: {e}")
        return lines

    def _fetch(self, query: str) -> list[str]:
        base = self.cfg.get("base_url", "http://searxng:8080/search")
        params = urllib.parse.urlencode({
            "q": query,
            "format": "json",
            "language": self.cfg.get("language", "zh-CN"),
            "safesearch": "0",
        })
        req = urllib.request.Request(
            base + "?" + params, headers={"User-Agent": "Mozilla/5.0 (compatible; footprint/1.0)"}
        )
        try:
            with urllib.request.urlopen(req, timeout=float(self.cfg.get("timeout", 8))) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"[web_search] 搜索失败({query[:24]}): {type(e).__name__} {e}")
            return []

        results = data.get("results") or []
        if not results:
            logger.info(
                f"[web_search] 无结果: {query[:24]} "
                f"(unresponsive={data.get('unresponsive_engines')})"
            )
            return []

        limit = int(self.cfg.get("top_n", 6))
        snip = int(self.cfg.get("snippet_len", 160))
        lines = []
        for r in results[:limit]:
            title = (r.get("title") or "").strip()
            content = (r.get("content") or "").replace("\n", " ").strip()[:snip]
            if not (title or content):
                continue
            lines.append(f"{title}：{content}" if title else content)
        if lines:
            logger.info(f"[web_search] {query[:24]} -> {len(lines)} 条")
        return lines


_service = None


def get_web_search() -> WebSearchService:
    global _service
    if _service is None:
        _service = WebSearchService()
    return _service
