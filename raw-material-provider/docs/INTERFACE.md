# Raw snapshot interface

`llm-corpus-builder` 的输入是不可变 raw snapshot。builder 只读取对象并验证 hash，不从 `source_url` 回源下载。

必须保证：

1. `snapshot_id` 唯一且不可复用；
2. 成功材料的原始字节可通过对象路径读取；
3. hash 与实际字节一致；
4. records 记录抓取时间、来源 URL、父页面、媒体类型、大小和状态；
5. 失败下载进入 `failures.jsonl`；
6. 同一 snapshot 不原地覆盖；
7. 原始字节永不被解码结果覆盖。

状态使用 `ok`、`not_found`、`forbidden`、`timeout`、`decode_error`、`too_large`、`unsupported` 和 `manual_upload`。

## Legacy-tree import v1 (2026-10-14)

`snapshot.py` materializes the EXISTING crawl tree into the object layout above
without re-crawling ("legacy-tree import"): bytes are content-addressed under
`objects/<sha256>.<ext>`, `records.jsonl` carries one line per file (with
`download_status: "archived"` and crawl-time `fetched_at` from the source
mtime), and the three historical failure ledgers are normalized into
`failures.jsonl` against the status vocabulary. The discovery tree remains the
enumeration source for this v1; the object store is the authority for bytes
(builder `pipeline --snapshot` verifies every input against it and reads from
it). Re-run `snapshot.py --snapshot-id <id> --resume` after any crawl-tree
mutation script to re-freeze a new snapshot version.
