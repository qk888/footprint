"""知识库入库: 城市攻略(travel_guide) + 槽位示例(slot_examples) → Chroma

为什么必须有这一步:
  槽位填充是 few-shot 的(检索相似示例拼进提示词)。库里空着 → 提示词"示例"段为空 →
  1.5B 小模型只能瞎猜, 实测"重庆旅游两日计划"输出 `[{"city": "重庆"}]`,
  解析不出任何槽位 → travel_plan 永远返回"无法规划"; 攻略库空着 rag_summary 也永远 miss。
  原项目靠手工跑脚本入库, 容器化之后没人跑过, 所以两个 collection 都是 0 条。

两个 loader 都按文件 md5 去重(记在 app/agent/md5.text), 入过就是空操作,
因此挂在容器启动脚本里每次开机跑一遍也安全; 失败时不会写 md5, 下次启动自动重试。
"""
import sys, io, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import httpx
from app.agent.travel.services.vector_store import VectorStoreService

# 入库要把文本发给 embedding 模型, embed-model(profile: ai)没起来就别白跑
EMBED_HEALTH_URL = os.getenv(
    "EMBED_MODEL_BASE_URL", "http://127.0.0.1:8081/v1"
).removesuffix("/v1") + "/health"


def wait_embed_model(retries: int = 30, interval: float = 2.0) -> bool:
    for attempt in range(retries):
        try:
            # trust_env=False: 绕过系统代理, 直连容器网络里的模型服务
            if httpx.get(EMBED_HEALTH_URL, timeout=3.0, trust_env=False).status_code == 200:
                print(f"[load_knowledge] embed-model 就绪(第{attempt}次探测)")
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


if not wait_embed_model():
    print(f"[load_knowledge] 等不到 {EMBED_HEALTH_URL}, 跳过入库(旅行规划会不可用)")
    sys.exit(0)

svc = VectorStoreService()
svc.load_document()                                     # 攻略 txt 已按城市切好, 直接入库
slots = svc.load_slot_examples()                        # 槽位示例 jsonl
print(f"[load_knowledge] 槽位示例新增 {slots} 条(0 = 已入过库或失败, 详情看上面日志)")
