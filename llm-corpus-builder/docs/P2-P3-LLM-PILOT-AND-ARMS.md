# P2 LLM pilot 与 P3 臂阶梯结果（2026-10-13 深夜，探索性）

## P2：deepseek-flash 全量 pilot（300 篇）

- 模型 deepseek-flash（`thinking: disabled`，temperature 0，prompt v1），
  缓存键 = sha256(payload+task+model+prompt_version)，`data/llm-cache/annotations/`。
- **392 次 API 调用、0 错误、18.3 分钟**；产出四个旁路文件于 S0 build
  `20261013-221420-a3fa1c/`：llm_annotations / llm_retrieval_text /
  llm_conflicts（481 对 term_mismatch）/ llm_uncertain + llm_pilot_report.json。
- **字段级原文支持率**：audience **97.0%**、doc_type **87.3%**、terms **88.7%**、
  series_key_candidate 66.1%（候选字段，低风险）；span 支持率 **91.2%**（4994/5474）。
- **晋级门禁判定：通过**（高风险字段 audience/terms ≥ 88%）。
- 修复记录：deepseek-flash 为推理模型，须 `thinking: disabled` 否则 2000 token
  全耗在思维链、content 为空；select 初版误选 listing 壳页（已限定 status=active）。

## P3：BM25 臂阶梯（48 题，--include-unverified，today 2026-10-13）

| 臂 | anchor@1 | anchor@5 | anchor@10 | doc@1 | gold_usable | wrong_ed@1 | inapp@1 | correct_abstain |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| active | 0.5128 | 0.7949 | 0.8205 | 0.6410 | 0.7692 | 0.0256 | 0.2708 | 1.0 |
| active_ir | 0.5385 | 0.7692 | 0.8205 | 0.6154 | 0.7949 | 0 | 0 | 0.8889 |
| **active_llm_metadata** | **0.5897** | **0.8205** | **0.8718** | 0.6667 | 0.7949 | 0.0256 | 0.2708 | 1.0 |
| active_llm_retrieval_text | 0.5897 | 0.7949 | 0.8205 | 0.6667 | 0.7692 | 0.0256 | 0.2917 | 1.0 |
| active_llm_validated | 0.5897 | 0.8462 | 0.8462 | 0.6410 | 0.7949 | **0.0513** | 0.2917 | 1.0 |
| active_llm_validated_ir | 0.6154 | 0.7436 | 0.7692 | **0.6923** | 0.7692 | 0.0256 | 0 | 0.8889 |

**结论（逐题配对口径）**：
1. **LLM 元数据臂提升召回上限**：anchor@1 +2 题、anchor@10 +2 题 vs active_ir
   ——正确证据更容易进入候选/前排，正是重排器修不了的部分（报告 §16.2 预期成立）。
2. **净收益约束的预警应验**：validated 臂 wrong_edition@1 翻倍、retrieval_text 臂
   inapplicable 微升、validated_ir 的 anchor@10 回落——别名/摘要加进各版本索引
   稀释版本判别信号（PLAN 修订 2 机制警告）。锚定门禁挡住了无据字段，但挡不住
   "有据但同构"的版本混淆。
3. **待跑组合**：active_llm_metadata + ir 过滤（metadata 臂尚无 _ir 变体）与
   active_llm_validated_ir + bge-v2-m3（P3 对照路径）；预计 metadata+ir 兼得
   召回与版本控制。
4. 全部数字为未核验探索性；正式结论等 verified_eval.jsonl。

## P4 Oracle document（BM25 路径数据点）

`oracle_doc`（gold 文档 top-2 块按自然分并入候选）：anchor@10 0.8205 与 active
持平——**文档在场时块级选择仍抓不到含 quote 的块**，残留失败是文档内结构/选块
问题（RQ3 靶子），非文档召回问题。

## 下一步

1. metadata+ir 组合臂；rr×llm 对照（active_ir+bge-v2-m3 vs validated_ir+bge-v2-m3）。
2. dense（bge-small-zh-v1.5，旧项目 models/embedding 466MB 可拷）/hybrid RRF 接入矩阵。
3. 人工核验（用户）→ verified_eval 重跑全部臂。
4. P1-5/6/7 剩余确定性修复；oracle evidence package（P4 第二版）。

## 补充实验（同夜）：组合臂与 rr×LLM 对照（结果目录 p3_llm_arms / p3_rr_llm）

| 臂 | anchor@1 | anchor@10 | doc@1 | gold_usable | wrong_ed@1 | inapp@1 |
|---|---:|---:|---:|---:|---:|---:|
| **active_llm_metadata_ir** | 0.5897 | 0.7949 | 0.6667 | 0.7949 | **0** | **0** |
| active_ir_rr（bge-v2-m3） | **0.7436** | 0.8205 | **0.7692** | **0.8205** | 0 | 0 |
| active_llm_metadata_ir_rr | 0.6923 | 0.8205 | 0.7179 | 0.8205 | 0 | 0 |
| active_llm_validated_ir_rr | 0.6667 | 0.7949 | 0.6923 | 0.7949 | 0 | 0 |

**P3 核心结论（RQ2 交互效应的直接证据，48 题探索性）**：

1. **metadata_ir 兼得确认**：BM25 路径下 LLM 元数据增强的召回收益（anchor@1/doc@1
   各 +2 题）与 ir 的版本/受众控制（双零）同时成立——BM25 路径最优确定性+LLM 组合。
2. **重排器在场时增强变净伤害**：bge-v2-m3 下任何 LLM 增强索引都回退
   （metadata 臂 -2 题、validated 臂 -3 题 top-1）；机制假设 = 增强行进入重排打分
   文本（unit_texts），干扰交叉编码器对原文相关性的判断 + validated 臂候选池
   本身有版本稀释。
3. **收益依赖检索器强度**：BM25（弱）上增强 +5.1pp anchor@1，rr（强）上 -2~-3 题——
   "高质量语料让简单检索器逼近复杂检索器"的交互形态有了数据。
4. **下一假设（待验证）**：重排输入改用原文（不带增强行），增强只作用于 BM25 候选池
   生成——预期恢复 rr 家族的 top-1 同时保留增强召回。需作为新配置（不动 active_ir_rr
   的 Parity 基线）。
5. 全部数字未核验探索性；正式结论等 verified_eval.jsonl。

## 补充实验 2：重排输入假设（证伪）与 dense/hybrid 矩阵列

**重排输入假设证伪**（`active_llm_metadata_ir_rr_raw`，重排输入用无增强 index_text、
增强只作用于 BM25 池）：结果与 `metadata_ir_rr` **逐位相同**（anchor@1 0.6923 /
MRR 0.7444）——增强行进不进重排打分文本毫无差别。**机制修正：LLM 索引增强的
作用点在候选池生成阶段**（BM25 打分改变池构成；rerank_recovery@1 从 active_ir_rr
的 0.7692 降到 0.7179）。含义：强重排器会把词法池扰动传导为最终排序损失；
正确设计 = 增强只服务无重排路径，或增强池与原池取并集后重排（待验证）。

**dense/hybrid 列**（bge-small-zh-v1.5 @ models/embed/bge-small-zh，npy 可续传缓存，
results/p3_dense）：

| 臂 | anchor@1 | anchor@10 | doc@1 | wrong_ed@1 | inapp@1 |
|---|---:|---:|---:|---:|---:|
| active（BM25 参照） | 0.5128 | 0.8205 | 0.6410 | 0.0256 | 0.2708 |
| active_dense | 0.2564 | 0.3333 | 0.4103 | 0.0769 | 0.3750 |
| active_hybrid（RRF） | 0.3077 | 0.8205 | 0.3846 | 0.1282 | 0.3750 |

- **纯稠密在本域大幅弱于 BM25**：课程代码、表格行、中英混排依赖精确词法匹配，
  bge-small-zh 的语义空间不敏感——支持"该域词法+规则为主线"的架构判断。
- hybrid 的 anchor@10 追平 BM25（0.8205）但 top-1 被 RRF 平权融合稀释；
  无 ir 层时版本/受众错误暴露。改进方向：ir 过滤后融合、加权 RRF、bge-m3 重跑。
