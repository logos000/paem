# llm-corpus-builder

本项目的输入是已经准备好的原始材料包，不负责网站爬取和附件下载。独立的 `raw-material-provider` 提供不可变 raw snapshot；builder 读取文本、图片、PDF、DOCX、XLSX 等对象及其 manifest，负责结构恢复、OCR、去重、版本、受众、provenance、验证和 retrieval-ready corpus 构建。

builder 也负责模型无关的查询侧能力：问题解析（Query IR）、候选召回、适用性/版本过滤、EvidencePack 和拒答状态。在线产品只应调用这条能力链，再把 EvidencePack 交给外层 LLM。

**LLM-assisted, provenance-preserving, retrieval-ready corpus construction**
（保留出处的 LLM 辅助检索就绪证据语料构建）

## 研究定位

把动态、带版本、带受众差异、包含表格和附件的高校事务资料，转换成**可检索、可引用、可判断时效性、可识别适用范围、支持合理拒答**的证据语料，并量化这种转换对下游检索的独立贡献。

三条研究问题：

- **RQ1** 哪些语料构建操作最能提升正确证据召回与版本适用性？
- **RQ2** 证据语料质量与检索器能力（BM25 / dense / hybrid / reranker）之间的关系与交互？
- **RQ3** 哪些表现为检索失败的问题，可通过语料结构、元数据和查询路由转化为更稳定的查找或拒答决策？

当前 runtime 执行计划见 [docs/ACTIVE-PLAN-2026-10-16.md](docs/ACTIVE-PLAN-2026-10-16.md)；早期研究阶段计划已归档到 `docs/archive/2026-10-16/PLAN.md`。

## 架构（按链路分层：产品 / 构建 / 研究）

### ① 产品链路 — RUNTIME 闭包（16 模块，= `Dockerfile.runtime` 复制清单）

在线产品与离线评测共享的唯一检索路径（`runtime/service.py` 起 HTTP，engine 驱动）：

| 模块 | 职责 |
| --- | --- |
| `runtime/` | CorpusEngine（折叠/缝合/融合）、answer_context 契约 v1、HTTP 服务、user_context 规则 |
| `evidence/` | EvidencePack 构建 + coverage 门（pack.py）、Query IR（query_ir.py）、块存储原语（store.py） |
| `decision/` | evidence_status 六状态 + 拒答决策（status.py） |
| `retrieve/` | BM25（lexical）+ dense（bge）+ RRF 融合 + reranker |
| `export/arms.py` | 检索臂定义（active_ir / active_hybrid / …） |
| `annotate/schema.py`、`curate/versions.py` | 被上二者引用的标注状态/版本键原语 |
| `domain_profile.py` + `profiles/zjui_academic.yaml` | 全部领域知识（声明式，换域只换 YAML） |

### ② 构建链路 — BUILD（`python -m pipeline`，九阶段函数按序执行）

`pipeline.py::build()` 编排：`collect_pages → collect_attachments → apply_snapshot（严格快照摄入）
→ extract_attachments（旧格式 COM 转换 + 结构抽取）→ assemble_documents → dedup_documents
→ build_version_series → triage_blocks（模板/重复块分诊）→ write_build（不可变产物 + manifest + LATEST）`。

配套：`ingest/`（源适配 + raw snapshot 门）、`structure/extract.py`（块/表格抽取）、
`curate/`（metadata/versions/dedup/conflicts/series_graph）、`metrics/`（构建期指标）、
`validate/anchor.py`（锚点校验）、`scripts/update_cycle.py`（快照→构建→冲突→门禁→切换的运维环）。

### ③ 研究专用（不在产品链路 — 论文证据，勿删）

`annotate/run_pilot.py` 等 P2 标注 pilot 工具（双后端）、`bench/`（P7 外部基线 A0–A3 / 全流程对比 /
ViDoRe 适配器）、`tests/`（287 项，锁行为）、`docs/`（P1–P7 分阶段实验报告）。

评测 harness 与题集系统在独立仓 `../eval-suite`（经路径桥导入本仓模块，复用本仓 venv）。

## 查询侧运行时（Stage 3A）

builder 同时是**模型无关的查询能力层**。离线评测与在线产品走同一个
`CorpusEngine.answer_context`（W1b 92 题逐题零漂移证明）：

```bash
# 起 HTTP 服务（product 学术问答产品的检索后端）
# 两段式：这里用 active_ir 便宜启动做 fail-fast；产品 launch.ps1 随后预热
# active_hybrid（sp 臂）——裸 CLI 不预热时首个 hybrid 请求付一次懒加载。
PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m runtime.service --port 8772
#   可选：--build <build目录或LATEST目录>  --config active_hybrid（产品 sp 臂；SP 域）
#          --rerank-model bge-v2-m3（quality 臂专用）

curl -X POST http://127.0.0.1:8772/v1/answer_context -H "Content-Type: application/json" \
  -d '{"question":"...","today":"2026-10-13","user_context":{"audience":"undergraduate"}}'
```

契约（`runtime/answer_context.schema.json`，版本 `ANSWER_CONTEXT_VERSION`）返回
`query_ir / decision{六状态,abstain,reason} / evidence_pack / provenance / latency_ms`；
user_context 规则 R1：问题内显式声明优先、只补空、留痕。消费方：
`academic-affairs-product`（薄 HTTP 客户端，见其 README / launch.ps1 脚本）。

## raw snapshot（不可变输入）

`raw-material-provider/snapshot.py` 把爬取树一次性导入内容寻址对象库
（44,216 records / 42,103 objects / 15GiB）。构建时加
`--snapshot <快照目录>` 进入严格摄入：每个字节 hash 对照快照记录
（不一致→`raw_hash_mismatch`、无记录→`not_in_snapshot`），存活读取重定向
到对象库；爬取树任何变更后须重跑 snapshot 再构建。

## 关键契约

1. **四文本分层互不覆盖**：`raw_text` / `clean_text` / `retrieval_text` / `evidence_text`。
   `retrieval_text` 命中后，最终引用必须返回 `raw_text`/`clean_text` 的可验证片段。
2. **三类字段治理**：
   - 派生字段（doc_type/受众候选/别名/课程代码）→ 可直接索引；
   - 检索辅助字段（摘要/关键词/表格行上下文）→ 只进索引、与原文分开保存；
   - 高风险字段（发布/生效日期、当前版本、适用人群、冲突裁决）→ 必须有
     `evidence_spans` 原文支持 + 规则校验 + 必要时人工核验，否则标 `uncertain`。
3. **LLM 只增结构，不增事实**：不得改写替代原文、不得凭抓取时间猜发布日期、
   不得自动选"最新版"删旧版、不得把不确定标签写成确定事实。
4. **annotation 旁路化**：LLM 产物写入独立旁路文件（`llm_annotations.jsonl` 等），
   每条带 `model` / `prompt_version` / `created_at` / `validation_status`；
   缓存键 = (doc_sha256, model, prompt_version)。
5. **净收益约束**：召回提升不得伴随 wrong_edition / wrong_audience / citation / 拒答退化。

## 数据与安全红线

- 原始爬取数据（`E:\Projects\浙大网站爬取`）为**只读共享输入**，保留在原地，
  本仓库只通过 `--crawl-root` 读取，绝不修改、绝不移动、绝不复制 browser-profile。
- `data/`（构建产物、评测题集、LLM 缓存）与 `models/` 不进 git。
- SharePoint 内部语料**永不发送外部 API**；LLM 标注发送前按
  `ANNOTATE_API_ALLOWED_SITES` 白名单**代码级阻断**（未配置 = 拒绝一切发送，
  仅缓存重放可用；缓存命中不产生网络发送）。扩大豁免须用户明示。
- 正式实验指标只用 `verified: true` 的评测题。

## 快速开始

```bash
uv venv .venv
uv sync --all-extras              # 全部 extras：rerank + annotate + ocr
# CUDA torch 轮在锁外另装（uv sync 会换回 CPU 版）：
uv pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
```

两条构建轨道（结果目录 `data/corpus/<build_id>`，参数记录在 manifest.config）：

```bash
# 域内轨道（zjui + sharepoint + OCR，约 2.5 分钟）
PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m pipeline --sites zjui,sharepoint --ocr

# 全量轨道（全部站点 + OCR + 旧格式 COM 转换，约 19 分钟）
PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m pipeline --convert-legacy --ocr
```

评测（四档消融矩阵、judge、题集生成、线上 E2E）已整体迁至独立仓
`../eval-suite`（复用本仓 venv，经路径桥导入 builder 模块），入口见其
`scripts/README.md`。
