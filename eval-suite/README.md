# eval-suite — 题集构建与评测系统（第四仓）

ZJUI 教务问答项目的评测子系统，与三个兄弟仓并排：

```
zjui-product/
  academic-affairs-product/   产品（问答 UI/API）
  llm-corpus-builder/         语料构建 + 检索/证据运行时
  raw-material-provider/      原始材料摄入
  eval-suite/                 ← 本仓：题集生成 + 全部评测
```

## 为什么独立

评测必须测量 builder 的**单一路径**（Stage 3A 零漂移保证），因此本仓**不复制任何检索代码**：
`retrieve/runtime/evidence/annotate` 全部通过 `evalsuite/__init__.py` 的路径桥
（`BUILDER_ROOT` 环境变量可覆盖，默认 `../llm-corpus-builder`）从 builder 导入。
BM25 索引 pickle 与运行环境绑定，**本仓直接复用 builder 的 venv**（见下）。

## 组成

| 模块 | 职责 |
|---|---|
| `evalsuite/questiongen/` | 题集生成子项目（P1 素材包 → P2 矩阵生成 → P3 双 lint → P4 AI 复核 → P5 verify/provenance → P6 分维度报告） |
| `evalsuite/harness.py` | 检索评测（四档配置、anchor/doc/gold_usable/gold_fact_hit、诊断字段） |
| `evalsuite/lint.py` / `verify.py` | 冻结的题集门禁 + gen/write 人工复核流 |
| `evalsuite/e2e_product.py` | 产品全链路 E2E（HTTP 到 8770） |
| `evalsuite/candidates.py` / `oracle_pack.py` | 抽样素材包 / 答案层 oracle |
| `scripts/` | `gen_questions.py`（五阶段 CLI）、`mark_provenance.py`（四级 provenance） |
| `data/benchmarks/corpus-eval/` | 全部题集/复核/结果（gitignored，含冻结 92 题载体） |

## 运行约定

```bash
# 统一用 builder 的 venv（pickle/依赖版本一致）
PY=../llm-corpus-builder/.venv/Scripts/python.exe

# 题集生成五阶段
PYTHONIOENCODING=utf-8 PYTHONPATH=. $PY scripts/gen_questions.py generate|refine|lint|review|report ...

# 检索评测（对照冻结协议）
PYTHONIOENCODING=utf-8 PYTHONPATH=. $PY -m evalsuite.harness \
  --corpus ../llm-corpus-builder/data/corpus/<build> --eval data/benchmarks/corpus-eval/<set>.jsonl ...

# 人工复核 / provenance
PYTHONIOENCODING=utf-8 PYTHONPATH=. $PY -m evalsuite.verify gen|write ...
$PY scripts/mark_provenance.py --eval <set> --reviews review_<date>.json
```

网关凭据在 `.env`（`QUESTIONGEN_*`，不进 git）；SP 红线合规依据见
`questiongen` 文档与 builder 侧 QUESTIONGEN/E2E 报告。

## 迁移记录（2026-10-15）

自 llm-corpus-builder 迁出：`eval/` 全部、`scripts/gen_questions.py`、
`scripts/mark_provenance.py`、`tests/test_questiongen.py`、`tests/test_harness_diagnostics.py`、
`data/benchmarks/corpus-eval/`。builder 的 `update_cycle.py` 回归题集路径已改指本仓。
抽取后 harness 数字逐位一致（active_ir doc@1 0.281 / anchor@1 0.225 复核通过）。

## LLM 评审系统（2026-10-15）

```bash
# golden answer 回填 + 独立校验（v1 集两步；v2 起生成时同 call 产出）
PYTHONIOENCODING=utf-8 PYTHONPATH=. $PY scripts/backfill_golden.py --eval <set>
PYTHONIOENCODING=utf-8 PYTHONPATH=. $PY scripts/verify_golden.py --eval <set>
# LLM 评审：对照题目自带 golden bundle（标准答案+出题引用资料）多维打分 + 失败归因
#   打分器无搜索能力（用户裁决）；材料存在性由构造可知（gold=语料原文，弱 gold 单列）
PYTHONIOENCODING=utf-8 PYTHONPATH=. $PY scripts/llm_judge.py     --e2e <e2e json> --eval <set> --sample-validated 25
```

golden answer 可靠性三层：严格-from-quote 指令 + 数字守卫（非日期数字必须出自 quote∪问题）、
独立校验轮（只看 quote+GA 逐句支撑性，不过则弃）、gold_insufficient 隔离（写不出=题目问题，不算系统失败）。
已知边界：列表型问题的 GA 可能薄于系统全列表答案（v2 准则修正：列表题 GA 必须全列表）。
