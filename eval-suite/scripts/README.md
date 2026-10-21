# scripts/ — 谁是干什么的

分类原则：**常驻 harness**（会随新实验反复跑）与**一次性阶段工具**（产出了已冻结的确定性
artifact，重跑无意义也不允许覆盖——新实验一律用新 run id / 新版本号）。

所有 Python 脚本用 builder 的解释器跑：`../llm-corpus-builder/.venv/Scripts/python.exe`。

## 常驻 harness（可重复运行）

| 脚本 | 用途 |
| --- | --- |
| `run_ablation_v2.sh` | 系统级消融总入口：七臂（full / no-dense / no-queryopt / lexical / no-governance / template / naive-dense）逐臂重启栈跑 275 题 E2E + judge；可断点续跑（已有快照的臂跳过） |
| `llm_judge.py` | LLM judge CLI：语料核查 agent + 金答案多维评分 + 失败归因（`--e2e` / `--eval` / `--corpus` / `--all`，按行数断点续跑） |
| `check_consistency.py` | P0 一致性门：对一个 E2E 快照做 6 项计数校验（全必须为 0） |
| `gate_profile_equiv.py` | 零漂移门禁：把基准题直接重放到 runtime（无 LLM），与基线快照逐题比对证据包签名/pack_hash——重构是否动了行为的标准答案 |
| `check_table_binding.py` | 表格事实绑定链检查：table_context 下数字 claim 的引用-表格行绑定 |
| `gen_questions.py` | 题集五阶段 CLI（generate / lint / refine / review / report），驱动 `evalsuite/questiongen/` |
| `ablation_report_v1.py` | 消融报告：臂 × C 层级 × qshape × 语言，Wilson 95% CI，写 `results/ablation/` |
| `release_gate_v1.py` | 发布门：聚合冻结 artifact 出一份裁决文件（无 LLM 调用） |
| `backfill_golden.py` | 从 gold quotes 严格回填金答案（写新文件，verify write 必须显式 `--out` 防覆写） |
| `verify_golden.py` | 金答案独立逐句核验，不通过的 GA 降级为 `gold_insufficient` |
| `closedbook_run.py` | 横评基线：无检索闭卷 LLM 直答，产出 e2e 形状快照走标准 judge |
| `mark_provenance.py` | 给题集盖 provenance 章（machine_checked < ai_reviewed < human_verified < user_approved） |

## 一次性阶段工具（产出已冻结，勿重跑覆盖）

| 脚本 | 冻结产出 |
| --- | --- |
| `build_editorial_review.py` | `editorial_v1_*` 题集修订账本 + revised benchmark（sha256 入 PROVENANCE 台账） |
| `render_editorial_report.py` | 上述账本的 Markdown 渲染 |
| `build_diagnostic_pilot.py` | 24 题诊断探针 `diagnostic_v3_24.jsonl` |
| `build_taxonomy.py` | 检索错误五分类 taxonomy（单快照归因） |
| `build_adjudication.py` | 无效/存疑审计行的裁决卷宗页 |
| `audit_references.py` | 独立源绑定引用审计 → 76-id judge 采样基（accepted.jsonl 已冻结） |

冻结台账见 `../data/benchmarks/corpus-eval/PROVENANCE-2026-10-16.md`；`data/` 不进 git。
