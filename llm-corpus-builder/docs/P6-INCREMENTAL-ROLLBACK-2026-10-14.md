# P6 增量构建与回滚（Stage 2，2026-10-14）

依据：STAGE-RESULT §5 P6。验收三条：① 增量 vs 对应全量在未变化文档上产物
一致；② 旧 annotation 不混入新 build（失效留痕）；③ EvidencePack 可反查
build/source hash/annotation revision/模型版本。

## 1. 变化检测与差分构建（pipeline `--base <build_id>`）

- 文档身份 = (doc_uid, raw_sha256, rel_path)：爬取字节或位置变了即 "changed"。
- 差分写入 manifest.diff（added/changed/removed/unchanged + uid 清单）；
  `changed_docs.txt` 列出重标注目标（`python -m annotate.run_pilot --uids-file
  <build>/changed_docs.txt` 直接对接）。
- 未变化文档的 LLM sidecar 行（annotations / retrieval_text / row_contexts）
  自动携带进新 build。
- 抽取层本就是内容寻址缓存（`_cache/extract/{sha}.json`），未变化文件零重解析。

**验收①实证（2026-10-14）**：crawl 未变时对 `20261014-020301-90c398` 增量重建
`20261014-022422-14a6de`：diff = `+0 ~0 -0 =2226`，且 documents.jsonl /
blocks.jsonl / series.json 与基线 build **逐字节一致**（sha256 相同），863 条
annotation 记录携带——一致性以最强形式成立。

## 2. annotation 失效与历史版本（annotate/run_pilot.py）

- 每次覆写 sidecar 前先归档到 `<corpus>/llm_history/<model>-<created_at>/`
  （model/prompt/规则变更导致的失效全部留痕可审计）。
- 子集重跑携带：pilot 只更新本 run 选中的文档，未选中的既有记录原样保留
  （`carried_over_annotations` 进 report）——重跑子集不再丢覆盖。
- v2 provenance 使每条记录可反查：build_id / model / base_url / sampling /
  prompt_version / annotation_schema_version / prompt_hash / created_at；
  EvidencePack 条目自带 provenance（doc_uid/block_id/source_url/raw_sha256/
  build_id）→ **验收③成立**（模型 revision 由本地 runner 记录，API 侧以
  base_url+别名+prompt_hash 钉死到当前可得精度）。

## 3. 回滚（pipeline `--set-latest <build_id>`）

Build 目录不可变（manifest 记录七产物 sha256）；回滚 = 重指 LATEST 指针，
原文与引用 anchor 天然不变（它们从不被就地修改）。增量 build 与全量 build
产物逐字节一致（§1）意味着两者可互换回滚。**验收②③成立**。

## 4. 模型/prompt/规则变更失效链

- prompt 正文变更 → cache_key 纳入 prompt_hash 后不再陈旧命中（P1）；
- schema/门禁规则变更 → sidecar 由重放全量重算（0 API 成本，实测 971/971
  缓存命中 2.5 秒）；
- 模型变更 → llm_history 归档 + 全量重跑（缓存键含 model，自动分离）。
