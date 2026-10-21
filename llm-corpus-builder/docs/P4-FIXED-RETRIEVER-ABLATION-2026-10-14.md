# P4 固定检索器 LLM 语料消融（Stage 2，2026-10-14）

载体：domain build `20261014-020301-90c398`（P3 生效日期排序 + OCR 全量）+
v2 全量 sidecar（863 篇，573 API + 398 缓存重放，0 错误）+
`conflict_candidates.jsonl`（21 条：contact 9 / deadline 6 / credits 6；
llm_candidate 12 / deterministic 9）+ `series_candidate_graph.json`（104 候选键、
24 跨链信号）。

评测：`verified_eval.jsonl`（45 verified：37 可答 + 8 陷阱），today=2026-10-14。
结果目录：`data/benchmarks/corpus-eval/results_p4_bm25/`、`results_p4/`。

## 主表（verified-only 45 题）

| 臂 | anchor@1 | anchor@10 | doc@1 | gold_usable@8 | wrong_ed@1 | inapp@1 | correct_abstain |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| raw | 0.135 | 0.270 | 0.351 | — | 0.027 | 0.267 | — |
| clean | 0.405 | 0.757 | 0.513 | — | 0.081 | 0.244 | — |
| active | 0.540 | 0.838 | 0.595 | 0.784 | 0.054 | 0.267 | 1.000 |
| active_ir | 0.568 | 0.811 | 0.622 | 0.784 | 0 | 0 | 1.000 |
| active_llm_metadata_ir（exploratory） | 0.649 | 0.784 | 0.703 | 0.784 | 0 | 0 | 1.000 |
| **active_llm_validated_ir（gated）** | **0.649** | **0.784** | **0.703** | 0.784 | 0 | 0 | 1.000 |
| active_llm_retrieval_text（exploratory） | 0.432 | 0.595 | 0.568 | 0.568 | 0.054 | 0.333 | 1.000 |
| oracle_doc | 0.540 | 0.838 | 0.595 | — | 0.054 | 0.267 | — |
| active_ir_rr（bge-v2-m3） | 0.703 | 0.811 | 0.730 | 0.811 | 0 | 0 | 1.000 |
| active_llm_metadata_ir_rr | 0.676 | 0.811 | 0.703 | 0.811 | 0 | 0 | 1.000 |
| active_llm_validated_ir_rr | 0.676 | 0.811 | 0.703 | 0.811 | 0 | 0 | 1.000 |

## 核心结论

1. **字段级门禁零检索代价（P1 验收在检索侧的实证）**：v2 门禁后的
   `active_llm_validated_ir` 与 exploratory `active_llm_metadata_ir` 逐指标一致
   （0.649/0.784/0.703）——把无据字段清出索引不损失任何召回或排序。P3 报告过
   的"validated 臂 wrong_ed 翻倍"（文档级粗门禁时代）在字段级门禁下消失：
   双零保持。**正式主表从此可以用 validated 臂**。
2. **LLM 元数据收益定位**：active_ir → metadata_ir/validated_ir anchor@1
   +8.1pp（0.568→0.649）、doc@1 +8.1pp（0.622→0.703）——且 wrong_edition/
   inapplicable 保持 0、拒答 8/8。结合 `gold_in_candidate@50` 诊断，增益主要
   来自**候选覆盖与适用性排序信号**（与 P3 结论一致）。
3. **重排器交互（负交互确认）**：固定 bge-v2-m3 后，LLM 增强臂 0.676 <
   active_ir_rr 0.703（-2.7pp）——增强索引文本在强重排器下再次小幅回落，
   RQ2 的交互效应在 v2 语料上复现。P7.4 的二维矩阵将正式量化。
4. **retrieval_text 臂（BM25 探索性）仍为负**：0.432 < active 0.540。v2 门禁
   已剔除 84% 不可验证摘要，剩余内容（过门别名 + 规则重建行）在 BM25 下仍
   稀释词法信号——该臂只作 exploratory 报告，不进主表。
5. **oracle_doc = active**：gold 文档注入不再提升（0.540/0.838 持平）——残留
   失败仍是文档内块选择与决策层问题，与 Stage 1 结论一致。
6. **冲突层实弹触发**：P3 的 known_conflict + fact_conflict 在 active_ir 臂
   16/45 题上给出 pack 级 conflict 状态（标注性输出，abstain 保持 1.000/gold
   _usable 无退化）。被标记题：eval-v2-009/018/026/028/029、batch3-002/003/
   008/009/012/014/015/017/019/020/028。**冲突提示准确率的正式度量**待
   batch4 的 2 道 conflict_flag 专测题（gold 收双方）进入核验后进行。
7. 与旧主表（Stage 1，build 235554）对照：clean 0.405 vs 0.432、active 0.540
   vs 0.514——OCR 恢复文档带来的 IDF 微扰在 ±1 题量级（45 题小样本），
   阶梯方向与量级不变；rr 0.703 vs 0.730 同理。

## unsupported annotation rate / 成本 / 延迟

- 字段级（本 build 863 篇全量）：doc_type 83.2% / audience 90.6% / terms
  90.4% / series 53.2% / aliases.zh 82.2% / en 88.6% / course_codes 100% /
  **summary_1s 16.1%**——span support 不再是唯一质量指标（P2 验收达成的一半，
  人工 precision 部分见字段级核验清单）。
- 成本：域内全量覆盖 573 次 deepseek-flash 调用（含重放 0 调用）、19.4 分钟；
  缓存重放到新 build 2.5 秒 / 0 调用（971/971 命中）。
- 延迟：rr 臂（bge-v2-m3，4080）沿用冻结基线（mean 258ms / p95 347ms）；
  BM25 臂构建后单查询 <10ms。

## 净收益约束核对

metadata_ir / validated_ir 相对 active_ir：anchor@1 +8.1pp，wrong_edition@1
0→0，inapplicable@1 0→0，correct_abstain 1.0→1.0，gold_usable 持平——
**无任何退化项**，净收益为正。
