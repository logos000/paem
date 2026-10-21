# Raw Material Provider

原始材料提供层，位于抓取源和 `llm-corpus-builder` 之间。它负责把网页、附件和外部文件转成可审计、可复现、可交给 corpus builder 的原始材料包。

## 职责

- 站点抓取、附件发现和下载；
- 原始字节的只读归档；
- URL、HTTP 状态、响应头、抓取时间和来源关系记录；
- 编码识别与原始字节保留；
- 下载重试、失败队列和限速；
- 内容 SHA-256、媒体类型和文件扩展名记录；
- robots、访问权限、来源白名单和 PII 风险标记；
- 输出不可变 raw snapshot 和 manifest。

## 不负责

本项目不负责 HTML 主体提取、OCR、表格恢复、文档分类、去重、版本链、受众判断、EvidencePack 或问答。那些工作属于 `llm-corpus-builder`。

## 输出

```text
raw-snapshot/<snapshot_id>/
  manifest.json
  objects/<sha256>.<ext>
  records.jsonl
  failures.jsonl
```

每条 record 记录 `material_id`、对象路径、SHA-256、媒体类型、source URL、父页面、抓取时间、HTTP 状态、声明编码、字节大小和 `download_status`。

乱码、缺页、未下载附件、错误响应编码和访问失败应在这里被发现、重试或标记，不应在 corpus builder 中被悄悄修复后当作正常输入。
