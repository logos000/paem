# 题集亲检指南（REVIEW GUIDE）：把 ai_reviewed 升级为 user_approved

正式评测数字必须声明所依赖的核验层级：`machine_checked < ai_reviewed < human_verified < user_approved`。
当前 92 题 = 87 ai_reviewed + 少量 machine_checked（见 `data/benchmarks/corpus-eval/verified_eval_v3_provenance.jsonl` 的 `provenance` 字段）。本指南让你（项目所有者）亲自抽检/全检并把通过者升级为 **user_approved**。

## 流程（两步式工具，全部本地）

```powershell
cd E:\Projects\zjui-product\llm-corpus-builder
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="."

# 1) 生成本批审查工作表（默认基于 provenance 题集）
.venv\Scripts\python.exe -m eval.verify gen --eval data\benchmarks\corpus-eval\verified_eval_v3_provenance.jsonl

# 2) 用编辑器填写同目录生成的 review_<date>.json：
#    每题 decision = approve / reject，可改 gold 与加 reason
#    （建议顺序：先 conflict_flag、rule_trap_audience 类，再 30% 抽样，最后全量）

# 3) 写回核验结果（生成 verified 输出，供对照）
.venv\Scripts\python.exe -m eval.verify write --review review_<date>.json --eval data\benchmarks\corpus-eval\verified_eval_v3_provenance.jsonl

# 4) 把你的 approve 转成 user_approved 覆盖文件并重算层级
#    新建 data\benchmarks\corpus-eval\review_manual.json：[{"id": "batch4-001", "level": "user_approved"}, ...]
.venv\Scripts\python.exe scripts\mark_provenance.py --manual data\benchmarks\corpus-eval\review_manual.json
```

输出行如 `verified_eval_v3_provenance.jsonl: 95 items — machine_checked=…, ai_reviewed=…, user_approved=…`。

## 质量标准（沿用项目既定把关）

- 逐题亲自看：题面是否泄答案、gold quote 是否真在所引文档、类别标注是否正确；
- 抽检从 30% 起，重点 conflict_flag / rule_trap_audience / audience_interference；
- 指标解读永远配 provenance 层级 + 中立口径（gold_fact_hit），+29pp 类结论不脱离语境复述。

## 表述规范

正式表格脚注必须类似："76 可答题，provenance：user_approved N / ai_reviewed M（2026-10-14）"；
未完成亲检前，对外表述用 ai_reviewed 层级，不写"全部真人核验"。

## 3 题样例（走通即算上手）

取 `batch4-001`、一条 `rule_trap_future_term`、一条 `conflict_flag`：gen → 填 approve/ reject → write → review_manual.json 三行 → mark_provenance 输出计数变化正确。
