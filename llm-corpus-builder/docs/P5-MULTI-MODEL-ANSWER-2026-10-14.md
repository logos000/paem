# P5 答案级多模型闭环（Stage 2，2026-10-14）

载体：domain `20261014-020301-90c398`，verified-only 45 题（37 可答 + 8 陷阱），
检索包一律来自 **validated 臂**（`active_llm_validated_ir`，生成模型只读门禁后
的 EvidencePack）。oracle 对照 = gold 包（人工核验 gold quote 直给）。

## 1. 三生成器 × 双证据包（37 可答题）

| 生成器 | 包 | cites | 数字保真 | 逐字锚 | 误拒 |
| --- | --- | ---: | ---: | ---: | ---: |
| deepseek-flash | gold | **1.000** | 0.865 | 0.324 | 0.000 |
| deepseek-flash | retrieved | 0.865 | 0.919 | 0.324 | 0.243 |
| deepseek-chat | gold | **1.000** | 0.865 | 0.297 | 0.000 |
| deepseek-chat | retrieved | 0.811 | 0.919 | 0.324 | 0.243 |
| Qwen3-4B（本地） | gold | 0.973 | 0.919 | 0.297 | 0.027 |
| Qwen3-4B（本地） | retrieved | 0.676 | 0.757 | 0.270 | 0.324 |

生成器 provenance：deepseek-flash/chat = api.deepseek.com（temperature 0 /
thinking disabled / max_tokens 800）；Qwen3-4B-Instruct-2507 = 本地 transformers
（revision cdbee75f17c0，bfloat16@4080，temperature 0.1）。

## 2. 四层误差分列（验收①）

- **语料层**：oracle 差距的主要来源不在生成器选择——三个模型在 gold 包上
  表现几乎一致（cites 0.97-1.00、误拒 0.00-0.03）；
- **检索/EvidencePack 层**：retrieved 包使 cites 掉到 0.68-0.87、误拒升到
  0.24-0.32——检索侧缺口（gold_usable 0.784 → 约 8 题包内无 gold quote）
  以"误拒"形态传导到答案层，且**跨全部三个生成器复现**（与模型无关）；
- **生成层**：数字幻觉与包无关的残余（gold 包 numfid 0.865-0.919 ≈ Stage 1
  的 ~12.8% 结论保持）；Qwen3-4B 在弱包上 numfid 掉得更深（0.757）；
- **结论不依赖单一生成模型**（验收②）：两 API 模型逐指标差 ≤3pp，本地 4B
  方向一致。生成只读 validated EvidencePack（验收③，oracle_pack 无
  retrieval_text 通路）。

## 3. 与 Stage 1 的衔接

Stage 1 记录（cites 0.821 / numfid 0.872 / 误拒 0.231）与本次 retrieved 包
数字（0.865/0.919/0.243）同量级——OCR 语料与 v2 门禁未改变生成层结论：
**检索增强不自动传导到答案层，缺口在证据包的 gold 覆盖**。
