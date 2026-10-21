# 运营手册（OPERATIONS）

面向负责维护本系统的运营者。用户侧操作见 `USER-GUIDE.md`。**内网专用**（无认证，P3 安全栈未启用）。

## 生命周期总览

```text
日常：双击启动 → 用户提问/导入 → Ctrl+C 全停
新材料：界面导入（或 CLI add_material）→ watch_inbox 自动：入库→快照→增量构建→冲突扫描→门禁→原子切换
出问题：管理页一键回滚；gate 拦截按下方流程处置
```

## 目录结构（E:\Projects\zjui-product\）

- `academic-affairs-product\` 产品（API/Web UI/启动器/发布门禁）
- `llm-corpus-builder\` 语料与检索运行时（构建流水线、检索服务、管理 API）
- `raw-material-provider\` 摄入（inbox、入库脚本、看门狗、raw snapshot 对象库）
- 爬取树在 `E:\Projects\浙大网站爬取`（只读共享输入，`CRAWL_ROOT` 可覆盖）

## 日常巡检（管理页）

当前 build 与 arm；请求/错误/延迟 p50-p99；拒答码分布；LLM 熔断（连续失败≥5 开 60s，期间自动模板降级）；失败材料队列。

## 新材料两条入口

1. **界面**（首选）：导入材料页，见 USER-GUIDE §3。
2. **CLI**（批量/脚本）：
   ```powershell
   cd E:\Projects\zjui-product\raw-material-provider
   ..\llm-corpus-builder\.venv\Scripts\python.exe scripts\add_material.py 文件.pdf --kind pdf --path "Registrar/2026"
   # 网页通知：--kind page --site zjui --url https://.../x.html --title "标题"
   ```
   落位+快照后由看门狗自动发布；或手动 `..\llm-corpus-builder\.venv\Scripts\python.exe ..\llm-corpus-builder\scripts\update_cycle.py --switch`。

## 发布门禁拦截处置（重要）

门禁 = 罐装 smoke + 18 题回归（未来学期/错受众陷阱为硬断言）+ p95 预算。拦截时**旧 build 继续服务**，任务标 failed。处置：

1. 状态列表/`inbox/status.jsonl` 看 `detail` 里的失败项。
2. 判断原因：
   - **语料合法变化**（如新增了某学期真实通知，使"该学期必拒"过时）→ 编辑 `llm-corpus-builder\data\benchmarks\corpus-eval\product_regression.jsonl`，给对应题的 `expect_status` 增加可接受状态并写 `expect_note` 说明原因与日期 → 把 `inbox/jobs/failed\<job>.json` 复制回 `inbox\jobs\` 重发。
   - **真实回归**（指标下降/拒答退化）→ 不要重发；回滚或修复后重建。管理页一键回滚。
3. 每次演进都留 `expect_note` 审计。

## 回滚

管理页 build 行「切换」/「回滚上一版」，或 `POST http://127.0.0.1:8772/admin/rollback`。审计在 `llm-corpus-builder\data\service\admin_audit.jsonl`。

## 保留与清理

```powershell
..\llm-corpus-builder\.venv\Scripts\python.exe ..\llm-corpus-builder\scripts\update_cycle.py --prune 12        # 干跑
..\llm-corpus-builder\.venv\Scripts\python.exe ..\llm-corpus-builder\scripts\update_cycle.py --prune 12 --yes # 真删
```
冻结主表 build（92 题正式数字载体）与当前 LATEST 永不删除。

## 什么时候找维护者

- 启动器报 [build]/[索引]/[模型] 类缺失且按提示无法自愈；
- 契约错误（HTTP 502 `dependency_contract_error`）= 产品与运行时版本不匹配；
- 熔断持续开启（LLM provider 故障）；
- gate 连续多次拦截且无法归因于内容变化。

## LLM 延迟与超时旋钮（.env，均有默认值）

| 变量 | 默认 | 含义 |
|---|---:|---|
| `LLM_TIMEOUT_SECONDS` | 120 | 非流式调用/连接基础超时 |
| `LLM_STREAM_IDLE_SECONDS` | 30 | 流式相邻 chunk 最大间隔（超时→降级模板） |
| `LLM_STREAM_TOTAL_SECONDS` | 120 | 单次生成**总时长死线**（防思考型模型无限推理；超时→降级模板，旧答案路径不受影响） |
| `LLM_MAX_TOKENS` | 1500 | 生成输出上限（截尾保护；0=不限） |

说明（2026-10-15 实测）：DeepSeek 官方端点的**非流式模式会挂死**（61s 无响应）而流式正常，因此优化与生成全部走流式；`deepseek-flash` 为混合推理模型，思考流不受 `max_tokens` 约束，拥塞时段可能长时间不吐首字节——总死线保证最坏 2 分钟内降级为模板回答，而不是 2026-10-15 上午出现过的 30 分钟挂死。冒烟脚本：`python scripts/smoke_latency.py`（走真实端点，验证流式提取/缓存/内联引用）。

## 本地模型（Qwen3.8-27B via Ollama）

2026-10-15 部署：`qwen3.8:27b-iq4xs`（unsloth UD-IQ4_XS，14.25GB），Ollama 0.40.4，模型库 `G:\model_download\ollama`。

- 切换 provider：`python scripts/switch_llm.py local|deepseek`（.env 三行 + LLM_EXTRA_BODY，产品零代码改动）
- **必须的环境变量**（已 setx 用户级）：`HIP_VISIBLE_DEVICES=-1`（否则调度器把模型分给 AMD 核显，速度从 ~50char/s 掉到 4.6t/s）、`OLLAMA_KV_CACHE_TYPE=q8_0`、`OLLAMA_KEEP_ALIVE=30m`、`OLLAMA_MODELS=G:\model_download\ollama`
- 模型是混合推理型：思考开启时 TTFT 30-50s 且思考计入 max_tokens（需 ~3000 预算）；**`LLM_EXTRA_BODY={"reasoning_effort": "none"}` 已验证可关思考**（TTFT 0.9-3.5s），当前 .env 保留思考是用户裁决
- 重建模型：`E:\llm-deploy\Modelfile`（num_ctx 4096）+ `ollama create qwen3.8:27b-iq4xs -f Modelfile`；GGUF 源文件保留在 `E:\llm-deploy\gguf\`

## 安全边界（当前）

无认证/无 TLS/日志已脱敏问题文本。不要暴露公网；密钥只在本地 .env。启用认证属 P3（下一轮）。本地 Ollama 模式下证据不出本机。
