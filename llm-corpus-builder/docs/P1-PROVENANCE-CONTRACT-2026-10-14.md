# P1 字段级 provenance contract（Stage 2，2026-10-14）

依据：`STAGE-RESULT-AND-NEXT-PLAN-2026-10-14.md` §5 P1 与
`PROJECT-IMPLEMENTATION-ASSESSMENT-2026-10-14.md` §4.1/4.3/4.4/4.6/§7。

## 1. 交付内容

### 1.1 字段状态机（annotate/schema.py v2）

```text
candidate → source_supported → rule_checked → human_verified → promoted_to_index
uncertain（梯外沉降态：无 value，或 value 无原文依据——value 已清空进 rejected_value）
```

- `ANNOTATION_SCHEMA_VERSION = "v2"`（PROMPT_VERSION 保持 v1，prompt 未改，
  pilot 缓存键不变）；
- `apply_rule_checks()`：枚举形状、term 形状（须含年份/学期标记）、泛化 series
  key（复用 `curate.versions.is_generic_series_key`，先去 snake 化再匹配）、
  aliases/summary 的 occurrence/summary 校验视为规则层；
- `status_at_least()` 供导出门禁使用；`human_verified`/`promoted_to_index` 留给
  P2 人工核验与导出标记。

### 1.2 value/status 联动（run_pilot.assemble_annotation 重写）

无原文依据的 value **当场清空**（移入 `rejected_value` 留档）——审计 §4.1 的实锤
案例（"doc_type=news conf 0.95、evidence_spans 空、uncertain，但 course_codes 与
summary 仍进 llm_retrieval_text.jsonl"）从机制上不可能再发生。

`validation` 块升级：

- `metadata_supported_by_source`：**None=四个高风险字段全部未尝试**（与"全有据"
  严格区分）、True=尝试且全部有据、False=存在无据字段；该布尔只覆盖
  EVIDENCE_FIELDS——aliases/summary 失败由各自字段级门禁拦截，不再把整篇文档
  的已验证元数据一起拖下水（审计 §4.3 的"文档级粗门禁"问题）；
- 新增 `attempted_fields`（字段级"尝试过什么"）与 `rule_violations`。

### 1.3 EVIDENCE_FIELDS 扩围 + 结构化 anchor（validate/anchor.py）

- `aliases.{zh,en,course_codes}`：**别名串本身即 span**，规范化后必须出现在原文，
  未出现者按子字段拒入 `rejected_value`；
- `summary_1s`：`verify_summary()` 门禁 = 无 novel 数字串（答案数字必须来自原文）
  + 至少一个 ≥8 字符规范化词组逐字命中（证明是"压缩本文"而非"想象本文"）；
- `boilerplate/answer_bearing_spans`：改为带 status 的对象，仅可定位者存活；
- `locate_spans()`：每个存活 span 解析为
  `{text, anchors: [{block_id, ordinal, char_start, char_end, page, table_id,
  row_index, match_method}], match_count}`——多处命中全记录，锚点从"这句话在文档
  里出现过"升级为"指向哪个块、哪个表格行"。

### 1.4 context_line 规则重建（P1 核心行为变更）

- `sanitize_row_response` 迁入 schema 并硬校验 `row_ordinal ∈ [1, n_rows]`；
- `rebuild_row_contexts()`：可入索引的 `derived_text` 一律由规则从该行的
  heading_path + 自包含行文本重建；LLM 的自由文本 context_line 仅留档为
  `llm_context_line`（审计对照用），**永不入索引**；
- 新 sidecar `llm_row_contexts.jsonl`：每行携带 block_id/table_id/row_index/
  header_cells/section_path/derived_text/answer_bearing/status=rule_checked。

### 1.5 索引门禁（export/arms.py + run_pilot.build_retrieval_text）

- `_field_lines()`：字段级门禁——仅 `status ≥ source_supported` 且（validated 臂）
  文档级布尔严格 `is True` 才输出；v1 式"uncertain 字段带存活 value"的记录得
  到零行（防御纵深）；
- `build_retrieval_text()`：同样只接受达标字段；LLM 字段未达标时回落到确定性
  规则值（规则值天然有据）；行上下文只用 rule_checked 的 derived_text；
- 臂分类常量：`VALIDATED_CONFIGS = active_llm_validated*`（唯一 gated 臂），
  `EXPLORATORY_CONFIGS = metadata/retrieval_text 系`（报告必须标注 exploratory；
  正式主表用 validated 系）。

### 1.6 安全契约代码化 + provenance/cache 完备

- **站点白名单**：`run_pilot` 发送前按 `ANNOTATE_API_ALLOWED_SITES` 阻断
  （`SiteBlockedError` → 跳过并记 `blocked_by_policy`；未配置 = 拒绝一切发送，
  仅缓存重放可用；缓存命中不产生网络发送）。SharePoint 红线从"文档纪律"升级
  为"代码门禁"（审计 §4.6）。
- **provenance**：每条记录携带 `model / base_url / sampling（temperature/
  max_tokens/thinking/stream）/ prompt_version / annotation_schema_version /
  prompt_hash{doc,row} / created_at`（runner 新增 `provenance()`）。
- **cache**：`cache_key()` 支持纳入 `prompt_hash`（防"同名 v1 改了 prompt 正文"
  的陈旧命中）；v1 键公式保持不变，现有缓存全部继续命中。

## 2. 离线重放验证（不依赖新 API 的部分全部缓存命中）

在 Stage 2 P0 的 OCR 域内 build `20261014-010623-b492d1` 上重放 300 篇 pilot
（deepseek-flash；211 次调用系旧缓存载体为 P1 修复前的 221420 build、payload
漂移所致，属既授权 pilot 范围）：

| 字段 | 支持率（v2 字段级） |
| --- | ---: |
| doc_type | 250/298 = 83.9% |
| audience | 284/298 = 95.3% |
| terms | 90/101 = 89.1% |
| series_key_candidate | 128/208 = 61.5% |
| **aliases.zh** | 101/150 = 67.3% |
| **aliases.en** | 148/190 = 77.9% |
| **aliases.course_codes** | 107/107 = 100% |
| **summary_1s** | **48/298 = 16.1%** |

**关键发现：summary_1s 的原文可验证率只有 16.1%**——84% 的"一句话摘要"无法
同时满足"无新数字 + 至少一词组逐字命中"，审计 §4.1 的担忧被量化坐实；v2 下
它们被拒出检索文本。课程代码 100% 可回连（模型如实抄录），中文别名 67.3%
（约三分之一是模型的改写而非原文出现）。

sidecar 机器审计：

- 300/300 记录通过 v2 契约校验（0 错误）；
- **uncertain-带-value 泄漏 = 0**；
- metadata True/None/False = 203/2/95；
- 检索文本只剩过门内容：48 篇带摘要、243 篇带别名、93 篇带规则重建表格行；
- 结构化锚点抽查 84/84 解析到真实 block_id；
- conflicts 481 → 145（无据 series 值被清空后假配对消失）；
- eval 冒烟：`active_llm_metadata_ir` 在新 build + v2 sidecar 上端到端可跑，
  wrong_edition@1 / inapplicable@1 双零。

## 3. 测试

- 234 fast + 3 slow + 1 xfail 全绿；
- 新增：per-page OCR 降级 ×3（P0）；白名单默认拒绝/公开站点放行 sharepoint
  阻断 ×2；幻觉注入 ×3（编造别名+摘要+novel digits 不得晋级任何索引路径；
  编造 context_line/越界 row_ordinal 不得入检索文本；uncertain 带值记录在
  字段级门禁下得零行）；`metadata_supported=None≠verified`；规则层泛化
  series key/term 形状标记。

## 4. 验收对照（计划 P1 验收）

- ✅ 被索引字段全部有可定位 anchor（结构化 anchor + 抽查全解析）；
- ✅ retrieval text 可由原文+结构化字段重建（derived_text 规则重建；摘要/别名
  仅存原文可定位子集）；
- ✅ 幻觉注入测试阻止晋级（§3）；
- ✅ uncertain/unsupported 不进 validated index（字段级+文档级双门禁）；
- ✅ sidecar 与 build_id/模型/prompt hash 一一对应（provenance 完备）；
- ✅ validated/exploratory 臂区分（VALIDATED/EXPLORATORY_CONFIGS + 本报告）。

## 5. 运行注意

- 真实 API 跑 pilot 前需在 `.env` 配置 `ANNOTATE_API_ALLOWED_SITES`（如
  `zju,zjui,ugrs,grs,bksy,news`）；未配置 = 只重放缓存。SharePoint 站点默认
  不在白名单，扩大豁免须用户明示。
- 正式评测（P4）使用 `verified_eval.jsonl`（48 题/45 verified）作为 eval 文件；
  `eval_domain_48.jsonl` 内联 flag 只反映人工核验前的状态（20 verified）。
- v1 旧 sidecar（如 221420/235554 上的）在 v2 门禁下产出为零行——评测一律使用
  重放后的 v2 sidecar（当前在 `20261014-010623-b492d1`）。
