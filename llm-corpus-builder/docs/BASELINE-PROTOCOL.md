# BASELINE-PROTOCOL（基线协议与冻结记录）

**日期：** 2026-10-13 晚 · **状态：** S0 通过 Parity 门禁后冻结

## 1. Parity 校验记录（S0 门禁）

新仓库（代码迁入重构后）与旧项目 `academic-affairs-assistant`（冻结参照）在同一
评测集上逐位一致，证明迁移保真：

| 校验 | 旧（build `20261013-180841-67d1b0`） | 新（build `20261013-221420-a3fa1c`） |
| --- | --- | --- |
| 构建统计 | 2,245 文档 / 114 重复组 / 32 版本链 | 完全一致 |
| BM25 四档（48 题 × 10 指标） | anchor@1 = 0.154/0.410/0.513/0.538 | **逐位一致** |
| rr 档 bge-v2-m3 | doc@1 0.769 / anchor@1 0.744 / MRR 0.770 / gold_usable 0.821 / correct_abstain 0.889 | **逐位一致** |

复现命令（注意 `--out` 是**目录**参数）：

```bash
# 构建
PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m pipeline --sites zjui,sharepoint
# BM25 四档
PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m eval.harness \
  --corpus data/corpus/<build_id> --eval data/benchmarks/corpus-eval/eval_domain_48.jsonl \
  --configs raw,clean,active,active_ir --include-unverified --today 2026-10-13 --out <目录>
# 重排档
PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m eval.harness \
  --corpus data/corpus/<build_id> --eval data/benchmarks/corpus-eval/eval_domain_48.jsonl \
  --configs active_ir_rr --rerank-model bge-v2-m3 --include-unverified --today 2026-10-13 --out <目录>
```

结果文件：`data/benchmarks/corpus-eval/results/parity_check/20261013-221420-a3fa1c__eval_domain_48.json`

## 2. 冻结的基线

| 项 | 值 |
| --- | --- |
| domain pilot build | `20261013-221420-a3fa1c`（zjui,sharepoint；2,245 文档 / 857 active / 31,959 块 / 32 版本链） |
| 评测集（探索性，未全核验） | `eval_domain_48.jsonl`（48 题 = 20 verified + 28 candidate；`--include-unverified`） |
| 词法检索 | BM25（`retrieve/lexical.py`，k1=1.2 b=0.75，课程代码复合 token） |
| 重排基线 | **bge-reranker-v2-m3**，本地权重 `models/rerank/bge-v2-m3`，sha256[:16] = `d9e3e081faff1eef` |
| 重排延迟（RTX 4080, torch 2.11.0+cu128） | mean 257.8 ms / p95 346.9 ms（48 题） |
| today（陷阱题口径） | `2026-10-13` |
| full-corpus 轨道 | 待 P1 修复后的 full build（旧项目 `20261013-120932-8c2bd4` 仅作规模参照） |

**规则：不再比较更多重排器型号**（报告 §17）；dense/hybrid 进入 P3 矩阵时嵌入模型
版本钉死入 manifest。

## 3. 每次实验必须落盘的 protocol 字段

```text
build_id, corpus universe (domain/full), eval 文件与 verified 状态,
configs, today, code_hash (pipeline.py 内置计算), rerank/embedding 模型与哈希,
torch 版本, 延迟统计, 结果文件路径, 运行命令
```

结果 JSON 自带 meta（build_id / eval / n / verified_only / category_counts /
rerank_model）；手写实验需补记 code_hash 与模型哈希。

## 4. correct_abstain 语义审计结论（2026-10-13）

**结论：拒答决策层已存在，不是缺失。** `build_evidence_pack`（`evidence/pack.py`）
内部有真实规则：`abstain = 无可用证据（全部 blocking）or 覆盖率<0.12 or
scope_gap（请求 term 语料无证据）or constraint_gaps（受众错配/term 缺口）`，
并输出 `abstain_reason` 字符串（no applicable supporting evidence / term-gap /
audience-mismatch / scope gap / low support）。评测器 `correct_abstain` 度量的就是
这个 pack 级决策在陷阱题上的正确率（当前 0.889 = 9 道不可答题对 8 道；
此前记忆中的"correct_abstain=0 / 该拒未拒"已过时——19:31 之后的代码与结果一致）。

**差距（P0.5 的形态据此确定）：**
1. 现有输出是布尔 + 字符串，未映射到六状态词表（sufficient / no_material /
   wrong_term / wrong_audience / stale_or_uncertain / conflict）——缺
   stale_or_uncertain（现仅有非阻断的 stale_risk 提示）与 conflict 态；
2. 决策逻辑内嵌在 pack 构建里，无法独立测试与独立指标化；
3. 唯一失败的受众陷阱题应成为 decision 层的回归测试用例。

**P0.5 任务因此定为：** 把决策逻辑提炼为 `decision/status.py` 六状态 +
报告 §10-P0.5 的决策顺序，pack 的 `abstain`/`abstain_reason` 改由该模块驱动
（保持对外行为兼容），评测器新增六状态逐题输出与六个拒答指标。

## 5. 已知边界

- 48 题中 28 题未核验：当前全部数字为**探索性**，正式指标等 verified_eval.jsonl；
- domain build ≠ full build，两者结果不得同表比较；
- 逐题配对分析（win/loss）是该题量下唯一可信的比较方式，显著性检验等扩量后。
