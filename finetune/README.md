# 微调（P5）—— Qwen2.5-1.5B-Instruct QLoRA

> ## ⚠️ 先看这一节：走"意图分类"这条线，不是下面那个 tool calling
>
> 下面第 1~5 节写的是**早期方案**：教模型用 tool call 扮演 supervisor 调子助手、调具体工具。
> **那条路现在已经不存在了** —— supervisor 换成了纯文本三分类 `classify_intent`，
> 而记账/点亮/账单查询/记忆/规划/攻略**六个工具全部有了直达通道**，都把 tool calling 绕开了。
> 拿旧数据训，训出来的能力没有代码会去调用。
>
> **现在该训的是这一件事**：给一句关键词规则判不出来的话，输出 `travel` / `operation` / `chat`。
>
> | | 旧（tool calling） | 新（意图分类） |
> |---|---|---|
> | 数据 | `train_data/tool_calling_train.jsonl`（398 条，90% 已废弃） | `train_data/intent_classify_train.jsonl`（**370 条**，三类均衡） |
> | 生成脚本 | `scripts/gen_finetune_data.py` | **`scripts/gen_intent_data.py`** |
> | LLaMA-Factory 数据集名 | `footprint_tool_calling` | **`footprint_intent_classify`** |
> | 训练配置 | `finetune/qwen2.5-1.5b-qlora.yaml` | **`finetune/qwen2.5-1.5b-intent-qlora.yaml`** |
> | 任务形态 | 多轮 + function_call + observation | 单轮：一句话 → 一个标签 |
>
> 所以：**第 1 节（WSL 环境）、第 3 节（合并导出）、第 4 节（部署）、第 5 节（验证）照旧**，
> 只有**第 2 节的数据和配置换成新的**：
>
> ```bash
> cp /mnt/c/Users/Administrator/Documents/Qoder/2026-09-11/75a45244/footprint/backend/train_data/intent_classify_train.jsonl ~/footprint-finetune/data/
> cp /mnt/c/Users/Administrator/Documents/Qoder/2026-09-11/75a45244/footprint/finetune/dataset_info.json ~/footprint-finetune/data/
> cp /mnt/c/Users/Administrator/Documents/Qoder/2026-09-11/75a45244/footprint/finetune/qwen2.5-1.5b-intent-qlora.yaml ~/footprint-finetune/
> llamafactory-cli train qwen2.5-1.5b-intent-qlora.yaml
> ```
>
> 数据不够就往 `scripts/gen_intent_data.py` 的 `MANUAL` / `CHAT_EXTRA` 里加，然后重跑脚本。
>
> ### 还有一个前提：先修规则
> `靠关键词就能定的句子压根不会走到模型`，所以规则判错的那些，微调一点忙都帮不上。
> 实测 199 条路由样本里，60% 规则已命中，其中 20 条与权威标签冲突 →
> 已通过把 `旅游`/`旅行` 降级为弱信号（要配动作词）等改动压到 3 条。
> 详见 `app/agent/footprint_agent.py` 的 `WEAK_TRAVEL_WORDS` / `_RE_TRAVEL_PLAY` / `classify_intent`。

---


目标：用项目自带的工具调用数据微调基座模型，让 1.5B 小模型稳定扮演
supervisor / travel / operation / chat 四个角色（路由 + 工具调用）。

> 全程在 **WSL2 Ubuntu** 里做（RTX 3060 6GB）。Windows 侧只负责取数据和拷回 GGUF。
> 这一步在基础链路（后端 + 模型 + 前端）跑通之后再做。

---

## 0. 一次性环境准备（WSL2 Ubuntu）

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
python3 -m venv ~/lf-env && source ~/lf-env/bin/activate
pip install "llamafactory[bitsandbytes]" -U
# 验证 GPU 可见
python3 -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

---

## 1. 准备训练数据

数据生成脚本在后端，输出 `backend/train_data/tool_calling_train.jsonl`
（sharegpt + tools 格式，自带 398 条，可继续扩充）。

在 **Windows** 侧（项目 backend 环境）扩充数据：

```bash
cd footprint/backend
# 编辑 scripts/gen_finetune_data.py 的 build_examples() 加新样本，然后：
python scripts/gen_finetune_data.py
```

把数据放到 LLaMA-Factory 能读到的目录（WSL 侧）：

```bash
mkdir -p ~/footprint-finetune/data
cp /mnt/c/Users/Administrator/Documents/Qoder/2026-09-11/75a45244/footprint/backend/train_data/tool_calling_train.jsonl ~/footprint-finetune/data/
cp /mnt/c/Users/Administrator/Documents/Qoder/2026-09-11/75a45244/footprint/finetune/dataset_info.json ~/footprint-finetune/data/
```

> `dataset_info.json` 已按本项目数据格式配好（messages/system/tools 列 + 四角色 tag）。

---

## 2. 开始训练

把 `finetune/qwen2.5-1.5b-qlora.yaml` 拷到 WSL，按实际路径改两处：
- `dataset_dir: finetune/data` → 改成 `~/footprint-finetune/data` 的绝对路径
- `output_dir` → 改成你想存 LoRA 的目录

```bash
cp /mnt/c/.../footprint/finetune/qwen2.5-1.5b-qlora.yaml ~/footprint-finetune/
cd ~/footprint-finetune
llamafactory-cli train qwen2.5-1.5b-qlora.yaml
```

6GB 显存要点（配置里已设）：`quantization_bit: 4`、`per_device_train_batch_size: 1`、
`gradient_accumulation_steps: 8`、`gradient_checkpointing: true`、`cutoff_len: 2048`。
若仍 OOM，把 `cutoff_len` 降到 1536 或 1024。

---

## 3. 合并 LoRA 并导出 GGUF

```bash
# 3.1 合并 adapter 回基座（export 配置见下）
llamafactory-cli export merge.yaml

# 3.2 用 llama.cpp 转 GGUF
cd ~/llama.cpp   # 见 models/README.md 的克隆步骤
python3 convert_hf_to_gguf.py ~/footprint-finetune/merged \
    --outfile ~/footprint-finetune/footprint-qwen-f16.gguf --outtype f16

# 3.3 量化（部署用 q4_k_m / q5_k_m）
./llama-quantize ~/footprint-finetune/footprint-qwen-f16.gguf footprint-qwen-q5_k_m.gguf Q5_K_M
```

`merge.yaml` 示例：

```yaml
model_name_or_path: Qwen/Qwen2.5-1.5B-Instruct
adapter_name_or_path: ~/footprint-finetune/output/qwen2.5-1.5b-footprint
template: qwen
finetuning_type: lora
export_dir: ~/footprint-finetune/merged
export_size: 2
export_legacy_format: false
```

---

## 4. 部署微调后的模型

把导出的 GGUF 拷到 Windows 侧 `footprint/models/`，改名为
`qwen2.5-1.5b-instruct.gguf`（覆盖基座版），然后重启 ai profile：

```bash
cd footprint
docker compose --profile ai up -d --build
```

后端 `.env` 里 `LLM_MODEL=qwen`、`CHAT_MODEL_BASE_URL=http://chat-model:8080/v1` 不用改，
compose 里 chat-model 的 `--alias qwen` 已对齐。

---

## 5. 验证微调效果

```bash
cd footprint/backend
python scripts/test_agent_interactive.py   # 交互式跑 agent，看路由/工具调用是否更稳
```

重点看：意图路由是否选对子助手、operation 工具参数是否正确、travel 是否产出
`⟦PLAN⟧...⟦/PLAN⟧` 行程结构（前端靠它渲染卡片）。
