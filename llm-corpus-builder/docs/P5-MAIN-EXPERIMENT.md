# P5 主实验四级指标表（框架 + 探索性首批数字）

**口径声明**：以下全部为 48 题域内集（20 verified + 28 candidate）`--include-unverified`
的**探索性数字**。正式主表必须在人工核验产出 `verified_eval.jsonl` 后以 verified-only
重跑（`python -m eval.verify gen --eval data/benchmarks/corpus-eval/eval_domain_48.jsonl
--corpus data/corpus/20261013-221420-a3fa1c` → 人工逐题决定 → `python -m eval.verify
write …` → 各臂去掉 `--include-unverified` 重跑）。

三系统定义：**旧系统**（1.0 冻结参照，数字引用旧库记录）、**确定性证据层**（本库
S0 build + P1 修复）、**+LLM 语料构建**（deepseek-flash 300 篇 pilot 注入）。

## 四级指标主表（answerable = 39；生成层 deepseek-flash，39 题）

| 级 | 指标 | 旧系统（引用） | 确定性证据层（active_ir） | +LLM（metadata_ir） |
|---|---|---|---:|---:|
| **语料** | active 文档比例 | — | 0.383（857/2245） | 同左 |
| | 模板/文档内重复块降级 | — | 9,268 块（P1-2 后） | 同左 |
| | 显式受众比例 | — | 0.137（P1-1 后） | 0.97 支持率（300 篇 LLM 标注） |
| | published 实为修改时间比例 | — | 0.142（110/772，P1-5 可测量） | 同左 |
| **检索** | gold_in_candidate@50 | — | 0.949 | 0.923 |
| | anchor@1 / anchor@10 | 0.538* | 0.538 / 0.821 | **0.590 / 0.872**(注1) |
| | doc@1 | 0.615* | 0.615 | **0.667** |
| | wrong_edition@1 / inapplicable@1 | 0 / 0 | 0 / 0 | 0 / 0 |
| **证据** | gold_usable@8 | 0.795* | 0.795 | 0.795 |
| | correct_abstain（9 陷阱题） | 0.889 | 0.889 | 0.889 |
| | 检索包误拒（生成侧察觉） | — | 0.231（P4） | 待跑 |
| **答案** | 引用标注率（cites） | — | 0.846（retrieved 包） | **1.000**（gold 包） |
| | 数字保真率（numfid） | — | 0.872 | 0.872（gold 包同值） |
| | 拒答率（应为 0） | — | 0.231 | **0.000**（gold 包） |
| | （+LLM 检索包）cites / numfid / 误拒 | — | — | 0.821 / 0.872 / 0.231（注2） |

\* 旧系统列引用 S0 Parity 基线（与本库 active_ir 逐位一致），非独立重跑。
注2：LLM 臂（active_llm_metadata_ir）检索包的答案级指标与 active_ir 包几乎相同
（cites 0.821 vs 0.846，numfid/refused 同）——增强救的是"top-10 内低排名"题（本就
进 top-8 包），而被误拒的 9 题在两臂均 top-10 外：**检索增强的收益未传导到生成层**。
注1：+LLM 列为 `active_llm_metadata` 臂（anchor@10 0.872 来自该臂；metadata_ir 臂
anchor@10 0.795 但版本控制双零——臂间取舍见 P2-P3 文档）。

## 读法（误差分解链）

1. **检索层**：LLM 语料增强把 anchor@1 +5.1pp、召回上限（@10）+2~5pp——重排器
   （bge-v2-m3 anchor@1 0.744）进一步增强排序但受候选池扰动传导限制（P3 交互效应）。
2. **证据层**：gold_usable 与拒答在语料臂间持平——证据包质量由 ir 规则主导，
   LLM 增强暂未穿透到包级（后续：LLM 元数据进 pack 的适用性过滤）。
3. **答案层**（P4 oracle）：证据正确时生成层残留 ~12.8% 数字幻觉、0 误拒；证据
   错误时 23.1% 误拒 + 引用率掉到 84.6%——**下游误差 = 证据层误差 × (1-模型自觉
   拒答率) + 生成层固有幻觉**。

## 缺口（正式化前必须补）

- verified-only 重跑（用户人工核验，关键路径）；
- ~~LLM 臂答案级数字~~ ✅ 已补（oracle_pack --arm active_llm_metadata_ir，见注2）；
- 旧系统列的答案级数字（需在旧库跑生成或标注引用口径）；
- 全量轨道复跑（full-corpus build + P1 修复）后出第二张表。
