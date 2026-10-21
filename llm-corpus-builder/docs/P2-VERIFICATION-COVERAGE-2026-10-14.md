# P2 人工核验与覆盖扩展（Stage 2，2026-10-14）

依据：STAGE-RESULT §5 P2。验收：domain 覆盖表 + full 分层覆盖表；高风险字段
有人工 precision/错误率；span support 不再是唯一质量指标。

## 1. 覆盖表

| 轨道 | 载体 | 覆盖 | 调用构成 |
| --- | --- | --- | --- |
| domain 全量 | `20261014-020301-90c398`（863 active） | **863/863（100%）** | 573 API + 398 缓存，0 错误，19.4 分钟；重放到新 build 971/971 缓存命中 2.5 秒 0 API |
| full 分层抽样 | `20261014-012656-cea731`（10,119 active） | **700 篇分层样本**（site×doc_type×有表×版本链成员，比例配额+固定种子，uid 清单落盘 `llm_sample_uids.txt`） | 741 API + 87 缓存，0 错误，25.1 分钟 |

## 2. 字段级指标（v2 门禁后；span support 之外的独立维度）

| 字段 | domain top-300 | domain 其余 563 | full 分层 700 |
| --- | ---: | ---: | ---: |
| doc_type | 83.9% | 82.9% | 80.4% |
| audience | 95.3% | 88.0% | 88.1% |
| terms | 89.1% | 92.7% | 91.2% |
| series_key_candidate | 61.5% | 41.8% | 50.5% |
| aliases.zh | 67.3% | 89.9% | 77.6% |
| aliases.en | 77.9% | 93.4% | 84.7% |
| aliases.course_codes | 100% | 100% | 96.4% |
| summary_1s | 16.1% | 16.1% | 16.8% |

## 3. Selection-bias 结论（300 篇 pilot vs 全量）

- **series 候选支持率被 pilot 高估 19.7pp**（61.5%→41.8%）：高价值文档标题
  更规整，series 识别在普通文档上差得多——series 是全场最弱字段，人工核验
  与后续 prompt 迭代应优先投入这里。
- **中文别名被 pilot 低估 22.6pp**（67.3%→89.9%）：普通文档的中文别名反而
  更贴原文。
- **audience 被高估 7.3pp**（95.3%→88.0%）。
- summary（~16%）与 course_codes（~100%）无选择偏差——前者是方法性问题
  （压缩天然无逐字锚点），后者模型如实抄录。
- full 分层样本与 domain 全量量级一致（series ~50%、summary ~17%），
  域内结论可外推。

## 4. 人工核验（工具就绪，裁决待用户）

- 工具：`annotate/review_fields.py`（gen/write/metrics，与 eval/verify 同
  两步式；字段均衡抽样；工作单内嵌锚点指向的原文块摘录，无需打开语料）。
- 已生成：`data/benchmarks/field-review/field_review_2026-10-14.md` + 同名
  决策 json（300 条：doc_type/audience/terms/series/row_context 各 60）。
- 填法：json 内逐条 decision=approve/fix/reject（fix 附 corrected_value）→
  `python -m annotate.review_fields write --review <json> --corpus <build>
  --reviewer <名>` → `llm_field_review.jsonl`，此后 metrics 表自动补上
  人工 precision 与修正率两列。
- 每条决策落 reviewer/decided_at/corrected_value，满足"保存人工审核者、
  时间、决策、理由和 annotation revision"。

## 5. 结论

高风险字段的**机器侧质量画像已完整**（覆盖表 + 字段级支持率 + 偏差对照）；
span support 不再是唯一指标（P1 的字段状态机 + 本阶段 attempted/unsupported/
uncertain 分维）。人工 precision/修正率两列随用户裁决自动补齐——这是 Stage 2
唯一等待人工输入的交付项。
