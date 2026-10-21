# P3 冲突、版本与 EvidencePack 集成（Stage 2，2026-10-14）

依据：STAGE-RESULT §5 P3；验收三条（两份现行来源不静默合并；旧版/未来版/
冲突版/缺证据版在 EvidencePack 可解释；版本与冲突错误逐题归因）。

## 1. 构建层：带锚点的事实冲突候选（curate/conflicts.py）

- 抽取器：deadline（须有截止/deadline 提示词，杜绝裸日期噪声；"2026年9月30日"
  与 "2026-09-30" 归一为同值）、credits、contact（邮箱/电话归一）、quota、
  condition（GPA/绩点）。每条断言带 block_id 锚点。
- 分组：确定性 series + **门禁后的 LLM 候选 series**（v2 侧车中
  status ≥ source_supported 的 series key，未过门禁的进不了图）。
- 判定：同组内**现行**文档（is_latest）对同一事实类型出现 ≥2 个不同规范值
  → 冲突候选；superseded 版本与后继的分歧是历史不是冲突，不产出。
- 记录：双方 doc_uid/block_id/value/span/published/effective_from/authority，
  status 恒为 `candidate`（裁决权在人工/门禁，本层只负责不漏不删）。

实测（domain `20261014-020301-90c398`）：**21 条候选**（contact 9 / deadline 6 /
credits 6；llm_candidate 组 12 / deterministic 组 9）。

## 2. 候选关系图（curate/series_graph.py）

确定性链（series.json，权威，documents.jsonl 的 series_id/superseded_by 不动）
× LLM 候选键合并为 `series_candidate_graph.json`：104 个过门候选键、
**24 条跨链信号**（一个候选键的成员落在 ≥2 条确定性链、或含无链文档——
"漏链/误拆分"信号，只报告不自动改链）。

## 3. 版本裁决：生效日期（P3-5）

`curate.versions._sort_key` 在 title 版本号与 final/draft 标记之后、published
之前加入 `effective_from`（"自某日起施行"，P1-5 抽取）——文档自述的生效时间
优于发布时间裁决先后。单测覆盖同题异效日期案例（eff 2026-09-01 的版本压过
published 更晚但 eff 更早的版本）。**域内重建实证：29 条链 0 篇文档状态变化**
（当前语料无该冲突形态）——规则上线且零扰动。

## 4. EvidencePack 集成（evidence/pack.py + decision/status.py）

- `_fact_disagreements`：同一 series 的两份**现行**版本（ambiguous_latest 形态）
  对 dates/number_facts 断言不相交 → pack 级 `fact_conflict`（非阻断）。
- `_match_known_conflicts`：构建层冲突候选流入每个 pack（harness 自动加载
  `conflict_candidates.jsonl`）；命中一方而另一方在检索结果中时，**追加
  conflict_counterpart 条目**把另一方原文带进 pack——两份现行来源不再被
  coverage 选择静默合并；另一方未检索到时仅记录冲突引用。
- `decide()` 新增规则 6b：fact_conflict / known_conflict → `evidence_status
  = "conflict"`（不 abstain，答案须呈现双方）；abstain/abstain_reason 输出
  与历史逐字节一致（parity 保持）。
- pack 新增 `fact_conflicts` 字段与逐条目 `conflict_counterpart` 标记。

## 5. 实弹验证（P4 联跑）

active_ir 臂 45 题中 **16 题**的 pack 带冲突状态（known/fact conflict 命中），
abstain 8/8 与 gold_usable 无退化（标注性输出）。冲突提示准确率的正式度量
待 batch4 的 2 道 conflict_flag 专测题（gold 收双方、expected_behavior=
flag_conflict）核验后进行。

## 6. 测试

`tests/test_p3_conflicts.py` 9 例：抽取（截止提示词门 + 归一 + 锚点）、
现行分歧→候选、一致/superseded→无候选、LLM 候选组、关系图不覆盖 + 跨链、
生效日期裁决、pack 双现行分歧标注、counterpart 保留、无当事方不动。
全套 259 测试绿（250 fast + 3 slow + 1 xfail + 其余）。
