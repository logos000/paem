# P1 确定性修复记录（每项：before/after + 检索影响）

基线参照：S0 build `20261013-221420-a3fa1c`（Parity 已证与旧项目逐位一致）。
评测口径：48 题域内集 / `--include-unverified` / `--today 2026-10-13`（探索性数字）。

## P1-1 受众与 doc_type 规则修复 ✅（2026-10-13）

**改动**（`curate/metadata.py`）：
1. `audience()`：sharepoint/zjui 站点补正文证据检查——正文前 1500 字符中
   ≥2 个不同 faculty/graduate 关键词命中且无"本科"字样时，翻转站点默认受众；
   news 类文档豁免（研究新闻提到教授/研究生是被报道对象而非受众）。
2. `FACULTY_RE` 增加 招聘|岗位|薪酬|待遇（教工侧信号，行政岗招聘原被误标 graduate）。
3. `DOC_TYPE_RULES` news 规则扩充：喜报|荣获|获奖|来访|到访|访问|调研|签署|签约|
   战略合作|就职|称号|讲堂|工作坊|展览|大会|研究成果|发表|师生|年报|年终总结。

**迭代记录**：v1（正文≥2 关键词翻转）48 翻转、近半误伤（研究新闻被翻成 graduate）→
v2（news 豁免）招聘类全对但喜报/访问类仍误伤（doc_type=page 不触发豁免）→
v3（news 规则扩充）22 翻转。

**构建期 before → after**（build `20261013-223815-77fa7f`）：
- 显式受众（≠站点默认）：95（11.1%）→ 117（13.7%）；翻转样本人工抽查：
  ~10 项招聘/教工类正确（教务管理岗/实验员/行政助理/科研助理/英才加盟/TA 申请），
  ~8 项长尾新闻误伤（标题不含 news 规则词），~4 项附件噪声页（无影响）。
- doc_type：active news 76 → 189（新闻正确归类，authority_rank 相应修正）；
  'page' 622 → 510。剩余 'page' 长尾是 P2 LLM doc_type 分类的靶子（报告 §16.2）。

**检索影响（48 题逐题配对 vs S0 基线）**：
- eval-v2-004 **修复**：inapplicable@top1 消除，rank 2→1（受众修正使 gold 适用）；
- batch3-016 **改善** 3→2；
- batch3-004 微降 2→3（仍在 top-3）；
- batch3-008 **回退**（active 档 1→出 top-10）：gold 文档自身元数据/文本/块均无变化，
  根因是 2 个块的 inactive 变化引起 IDF 微扰，使招生简章模板近重复簇内排名重排——
  **记录为 P1-2 块级冗余治理的动机数据点**（模板簇内近平局排名对语料微扰敏感）。
- 汇总：clean anchor@1 +1 题、clean inapplicable@1 -1 题；active_ir anchor@10 / gold_usable
  各 -1 题。48 题下均为噪声量级，无系统性退化。

**结论**：确定性受众规则的合理上限已到；长尾新闻与 doc_type 细分移交 P2 LLM 标注。
**副作用记录**：`build_series` 按站点+doc_type 分组，news 扩容使 2 条版本链合并
（32→30，两 build 一致性已核对）；属分组定义的合理结果。

## P1-2 块级冗余三分法 ✅（2026-10-13）

**改动**（`pipeline.py`，series 构建后新增 triage 段）：
1. 文档内精确重复块（规范化文本+kind 相同）：保留首次出现，其余降级
   （`inactive_reason: intra_doc_duplicate`）；
2. 跨文档模板块：**非 table_row** 的相同规范化文本出现于 ≥5 个 active 文档
   （导航菜单/学院标签/交换项目套话）→ 在所有文档中降级
   （`inactive_reason: template_boilerplate`）；
3. 跨文档相同 table_row = 合法共享课程事实 → **保留**（三分法的第三类）。

降级块移入 `inactive_blocks.jsonl`：仍在 clean 消融档（clean = blocks+inactive），
从 active 档消失——契约"原始内容始终保留"不违反。

**构建期 before → after**（build `20261013-225742-88dad9` vs `…-77fa7f`）：
- 降级 9,268 块 = 模板 7,311 + 文档内重复 1,957（占原 blocks 29.0%）；
- active 档 blocks 31,959 → 22,691。

**检索影响（48 题，active_ir 终档 vs P1-1 build）**：
- anchor@1 **0.5385 → 0.5641（+1 题）**，anchor@3 +1，MRR@10 0.6182 → 0.6319；
- wrong_edition@1 / inapplicable@1 保持 0；gold_usable 持平；
- active 中间档 wrong_edition@1 暂升至 0.0769（模板移除后排序重排），被 ir 层全部
  压回 0——符合"中间档观察、终档裁决"的读法；
- clean 档 anchor@10 -1 题：降级块从 blocks.jsonl 移至 inactive 改变了单元顺序，
  BM25 平局翻转（clean 档单元集合不变）。
- 累计 vs S0 基线（P1-1+P1-2）：active_ir anchor@1 0.5385→0.5641、
  eval-v2-004 修复、batch3-016 +1；batch3-008 仍未恢复（模板簇 IDF 微扰的残留）。

## P1-3 导航与模板块降权（待做）
## P1-4 GRS 编码修复（待做；域内 build 不含 grs，属全量轨道）
## P1-5 四日期分离 ✅（2026-10-13 深夜）

**改动**：documents 新增 `date_provenance`（date_node 662 / modified_fallback 110 /
空 85——**14.2% 的 published 实为 SharePoint 修改时间**，语义修正列为后续带
before/after 的独立变更）、`retrieved_at`（原始文件 mtime，全部 2026-10-12）、
`effective_from` + `effective_from_source`（正文"自…起施行 / effective from"
正则提取，带匹配片段；域内语料为真阴性 0——91 个提及均为课程描述性 prose，
施行条款属 ugrs/grs 全量轨道）。build `20261013-233232-7916ef`。
**行为未变**：published 字段语义与版本排序本轮不动（date_provenance 先使问题可测量）。
## P1-6 series key 收紧 ✅（2026-10-13 深夜）

数据驱动检查 30 条链：真版本链（教学日历 9/选课指南 8/课表 V6-V9）健康；
**过粗合并实锤 = `预览PDF` [5]**——4 个无 terms 同名附件被强行排序成假版本链。
修复（`curate/versions.py`）：新增泛化键守卫——归一化键命中 `_GENERIC_KEY_RE`
（预览pdf/class schedule/course number/课表/日历/指南 等）时，**全部成员必须自带
terms 才允许建链**。before/after：series 30 → **29**（假链拆除，成员回归独立文档，
不再被误 supersede）。

## P1-7 future-term Query IR 约束 ✅（2026-10-13 深夜）

1. **年份上下限**（`evidence/query_ir.extract_terms`）：term/学年年份过滤到
   [2000, 今年+3]——挡爬取伪迹（2100-spring 类），**刻意保留 2027-spring 陷阱**
   （拒答层依赖其可解析）；before/after：越界 terms 0→0（域内本无越界，护栏生效）。
2. **listing 伪 term 抑制**（`pipeline.py`）：page_kind∈{listing,home} 的文档
   terms/academic_years 清空（导航/链接文本不构成 term 证据）；listing 带词数
   → 0。
3. 检索影响（build `20261013-234446-555155`）：active_ir anchor@1 0.5641 /
   anchor@10 0.7949 与 P1-2 后一致（被修对象不在评测 gold 内，零回退）；
   `active_llm_metadata_ir` anchor@1 0.5897 / 双零 / MRR 0.6662。
## P1-8 OCR/未下载附件/旧格式（待做）


## P1-4 GRS 编码修复 → 归因修正关闭（2026-10-13 深夜）

数据推翻乱码假设：grs 3,127 active 文档中 latin-mojibake 标题仅 1 个且为合法德语
（Universität）。域外 5 题 anchor@1=0.2 的真实形态：**doc_rank 全部第 1**，quote 块
排在同文档导语/标题块之后（019/020/023 第 2 名、022 出 top-10、021 成功）——
通知类长文的文档内块排序问题，移交重排/结构线，不是编码问题。P1-4 关闭。

## P1-8 OCR ✅（2026-10-13 深夜，部分）

域内 `--ocr` 重建（build `20261013-235554-555155`，19 秒）：**正式 20 题
verified-only 上 active_ir anchor@1 0.6471 → 0.7059（+1 题）**，双零与拒答 3/3
保持，无任何回退。未下载附件（128，全量）与旧格式 COM 转换（144）留全量轨道。


## P1-8 收口（2026-10-14 凌晨）

全量 build `20261014-000254-555155`（--ocr --convert-legacy，180 秒）：142 个旧格式
文件经 Office COM 转换入语料（series 416→419，转换文档形成新链），OCR 全量执行，
遗留 `legacy_converter_unavailable` 仅 2 个文件（环境限制，记录在案）。27 题
verified-only 对照：anchor@1 0.5652 / anchor@10 0.8696 / gold_usable 0.8696 /
拒答 4/4——与转换前头行指标一致（转换收益在 coverage，不在现有 27 题的 gold 路径上）。
128 个"未下载附件"为爬取期被拒链接，属爬虫侧缺口，非构建管线问题。

## P1-8 真正关闭（2026-10-14，Stage 2 P0）

上一节的"OCR 全量执行"表述不实：该 build 的 rejected 中有 78 条
`ModuleNotFoundError: No module named 'rapidocr_onnxruntime'`——OCR 依赖（uv.lock
extra `ocr`）当时未安装，且缺依赖的 ImportError 从 `pdf_extract` 整篇传播，
连有文本的页也一并丢失；失败还连错误串写进 `_cache/extract/*.ocr.json` 固化。
审计（PROJECT-IMPLEMENTATION-ASSESSMENT §4.7）据此判定"OCR 已完成"不能成立。

Stage 2 P0 修复：

1. `uv sync --extra ocr`（rapidocr-onnxruntime==1.4.4，早已锁定）+ 清除 78 条
   ModuleNotFoundError sticky 缓存；
2. OCR 缺依赖/引擎异常改为 **per-page 降级**（`structure/extract.py`）：缺依赖页
   记 `ocr_dependency_missing`、失败页记 `ocr_failed`，其余页文本保留，不再整篇
   rejected；rejected reason 五分类 `ocr_dependency_missing/ocr_failed/no_text/
   needs_ocr/not_downloaded`；
3. manifest 新增 ocr 统计块（docs/pages × succeeded/failed/dependency_missing）、
   `converted_legacy_count`、deps 快照（uv.lock sha256 + 实装版本，含 torch
   cu128 变体）、models 快照（dense/reranker 路径与 config 哈希），产物哈希补
   baseline_pages.jsonl / inactive_blocks.jsonl。

重建结果（可从 manifest 复核）：

- 域内 `20261014-010623-b492d1`：10 条 ModuleNotFoundError → 0；OCR 9 篇成功
  （27 页尝试/26 成功/1 空）；active 854→863。
- 全量 `20261014-012656-cea731`：78 条 → 0；OCR 76 篇成功（394 页尝试/342 成功/
  52 页 OCR 后仍无文本、如实入账/0 失败）；active 10,042→10,119，blocks
  189,714→198,815；converted_legacy_count=142。rejected 仅余 unsupported_type 144
  （媒体文件）、not_downloaded 128（爬虫侧）、legacy_converter_unavailable 2
  （环境限制）、no_text 2。

**P1-8 关闭（本次以产物可复核为准）。** 128 个未下载附件仍属爬虫侧缺口。
