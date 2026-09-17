"""意图分类的向量层。

关键词规则不命中时，把用户这句话 embed 成向量，和各类的"种子句"比相似度，
最像哪类就归哪类。种子句在 `config/intent_seeds.yml`，只加句子不用改代码。

为什么不用生成模型做这件事（199 条人工标注路由样本实测）：
    规则不命中的 67 条长尾上 —— 生成模型 50.7%（基本掷硬币），向量 73.1%
    端到端 79.4% → 86.9%
  而且实测"向量 + 生成模型兜底"反而更低（70.1% < 73.1%）：
  分差小的样本里向量往往是对的，交给模型就被带偏 —— **模糊时也要信向量**。

代价：首次调用要 embed 全部种子（约 110 句，1 秒左右），之后常驻内存（约 0.3MB）。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import numpy as np
import yaml

from app.agent.model.factory import embedding_model

logger = logging.getLogger(__name__)

SEEDS_PATH = Path(__file__).resolve().parent / "config" / "intent_seeds.yml"

LABELS = ("operation", "travel", "chat")
# 取每类最相似的前 TOPK 句求均值 —— 比只看单句稳（单句可能撞上一条异常种子）
TOPK = 3
# 最高分低于这个值说明三类都不像，交回上层（上层按 chat 处理：无副作用，最安全）
MIN_TOP_SCORE = 0.60


class IntentVectorIndex:
    def __init__(self) -> None:
        self._vecs = None
        self._labels: list[str] = []
        self._phrases: list[str] = []
        self._lock = asyncio.Lock()
        self._ready = False

    def _load_seeds(self) -> None:
        data = yaml.safe_load(SEEDS_PATH.read_text(encoding="utf-8")) or {}
        labels, phrases = [], []
        for label in LABELS:
            for s in data.get(label) or []:
                s = str(s).strip()
                if s:
                    labels.append(label)
                    phrases.append(s)
        if not phrases:
            raise RuntimeError(f"种子句为空: {SEEDS_PATH}")
        self._labels = labels
        self._phrases = phrases
        logger.info("[intent_vec] 载入种子句 %d 条 (%s)" % (len(phrases), SEEDS_PATH.name))

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            self._load_seeds()
            vecs = await embedding_model.aembed_documents(self._phrases)
            arr = np.asarray(vecs, dtype=np.float32)
            arr = arr / np.linalg.norm(arr, axis=1, keepdims=True)
            self._vecs = arr
            self._ready = True
            logger.info("[intent_vec] 种子向量就绪, shape=%s" % (arr.shape,))

    async def classify(self, query: str):
        """返回 (意图 | None, 分差, 最高分)。意图为 None 表示三类都不像。"""
        await self._ensure_ready()
        q = await embedding_model.aembed_query(query)
        qv = np.asarray(q, dtype=np.float32)
        norm = float(np.linalg.norm(qv)) or 1.0
        qv = qv / norm

        sims = self._vecs @ qv
        scores = {}
        for label in LABELS:
            idx = [i for i, l in enumerate(self._labels) if l == label]
            if idx:
                scores[label] = float(np.sort(sims[idx])[::-1][:TOPK].mean())
        if not scores:
            return None, 0.0, 0.0

        ordered = sorted(scores.items(), key=lambda x: -x[1])
        best_label, best = ordered[0]
        gap = best - ordered[1][1] if len(ordered) > 1 else best
        if best < MIN_TOP_SCORE:
            logger.info("[intent_vec] '%s' → 都不像(最高%.3f) " % (query[:24], best))
            return None, gap, best

        top_i = int(np.argmax(sims))
        logger.info(
            "[intent_vec] '%s' → %s (%.3f, 分差%.3f, 最像『%s』)"
            % (query[:24], best_label, best, gap, self._phrases[top_i][:24])
        )
        return best_label, gap, best


intent_index = IntentVectorIndex()
