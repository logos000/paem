# Academic Affairs Product（ZJUI 教务证据问答）

**双击 `启动教务问答.bat` 即可使用**（自检→起服务→开浏览器；Ctrl+C 一键全停）。
使用见 [docs/USER-GUIDE.md](docs/USER-GUIDE.md)，运营见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。
产品套件三仓位于 `E:\Projects\zjui-product\`（本仓 + llm-corpus-builder + raw-material-provider）。
当前项目已经完成“产品 API → `llm-corpus-builder` runtime → EvidencePack → LLM/模板回答”的端到端闭环，产品层不再复制检索和证据判断逻辑。完成度评审见 [STAGE-4-PRODUCT-READINESS-REVIEW-2026-10-14.md](docs/STAGE-4-PRODUCT-READINESS-REVIEW-2026-10-14.md)，当前执行计划见 [ACTIVE-PLAN-2026-10-16.md](docs/ACTIVE-PLAN-2026-10-16.md)。当前定位是可复现、适合受控试用的证据问答 API；公开上线前仍需完成答案级引用校验、安全、部署、监控和回滚门禁。

面向真实使用环境的轻量教务问答产品层。**本仓库不含任何检索实现**：
问题解析、候选召回、版本/受众/学期适用性、冲突检测、EvidencePack 和
拒答决策全部由 `llm-corpus-builder` 的检索运行时
（`python -m runtime.service`，`POST /v1/answer_context`）提供；产品只做
API、LLM 适配、引用渲染和日志。

```text
llm-corpus-builder runtime service (build + Query IR + 检索 + EvidencePack + 拒答)
  ← HTTP (answer_context contract v1)
academic-affairs-product — 答案主流程 product/flow.py（PIPELINE 按序执行八个节点）
  resolve_arm → optimize_queries[默认关] → retrieve → generate(流式)
  → follow_up[默认关] → finalize → build_response → persist
  ├─ answer.py：LLM adapter（OpenAI 兼容 / 确定性模板兜底）
  ├─ 引用渲染（evidence_id → source_url/block_id）
  └─ 结构化 jsonl 日志（与 runtime 以 request_id 关联）
```

## Quick start（两进程）

先起语料检索运行时（builder 仓库根目录）：

```powershell
cd E:\Projects\zjui-product\llm-corpus-builder
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="."
.venv\Scripts\python.exe -m runtime.service --port 8772
```

再起产品（本仓库）：

```powershell
cd E:\Projects\zjui-product\academic-affairs-product
copy .env.example .env   # 按需修改 BUILDER_URL / LLM_*
.venv\Scripts\python.exe -m product          # 默认 127.0.0.1:8770
```

或一键启动：`启动教务问答.bat`（内部走 `scripts/launch.ps1`：自检、起双进程、预热、开浏览器，BUILDER_DIR/PROVIDER_DIR 可覆盖路径）。

调用：

```powershell
Invoke-RestMethod http://127.0.0.1:8770/health
Invoke-RestMethod -Method Post http://127.0.0.1:8770/v1/ask `
  -ContentType "application/json" `
  -Body '{"question":"ZJUI 2026 秋季学期本科生退课截止日期是什么时候？","user_context":{"audience":"undergraduate"}}'
```

返回 `decision`（六状态 + 拒答原因）、`answer`、`provider`、`citations`
和完整 `evidence_pack`。未配置 `LLM_*` 时返回确定性模板回答
（provider=`evidence-template`）；配置后走 OpenAI 兼容生成，模型只接收
EvidencePack，证据存在冲突时 prompt 强制双呈现。

## Product boundary（Stage 3A 起）

- `raw-material-provider`：抓取、原始字节归档、raw snapshot 契约。
- `llm-corpus-builder`：语料构建 + **查询侧全部能力**（Query IR、检索、
  适用性/版本/冲突、EvidencePack、拒答），以 HTTP 服务暴露。
- `academic-affairs-product`：API、LLM adapter、结构化答案与引用校验、Web UI、日志。不含
  BM25 / Query IR / pack 构建逻辑（`grep -r "bm25\|jieba" product/` 应为零）。

## Web UI（P5）

React/Vite 前端（`web/`），product 同源服务 `web/dist`：

```powershell
cd web
npm install; npm run build      # 需要 Node >= 18
# 正常启动 product 后浏览器打开 http://127.0.0.1:8770/
```

功能：侧栏多会话、首页示例引导、SSE 阶段流（检索→生成→校验）与流式答案、可点击角标打开引用原文抽屉、逐条依据、按状态拒答解释与建议、
冲突双方呈现、生成模式标注（校验通过/降级原因）、会话历史（仅展示与审计，不进检索）、
反馈、浅色/深色主题、拖放导入、运行状态视图（build 原子切换/回滚/smoke、指标、失败材料队列）。开发态 `npm run dev`。

## 部署（P4）

- Docker Compose（双容器，含前端构建）：`deploy/docker-compose.yml`，
  `CORPUS_DIR=<builder>/data/corpus docker compose up -d --build`。
- 无 Docker：`scripts/launch.ps1`（`启动教务问答.bat` 的内核；BUILDER_DIR/PROVIDER_DIR 参数化）。
- 本地 CI：`scripts/ci.sh [--compose]`（双仓测试 + 契约 + 启动冒烟，可选 compose 腿）。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest
```

测试用一个说同一 HTTP 契约的 stub runtime（不依赖 builder 环境）；
「in-process == HTTP == product」三路等价测试在 builder 仓库
`tests/test_runtime_engine.py`。
