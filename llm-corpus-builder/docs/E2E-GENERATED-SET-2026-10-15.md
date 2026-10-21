# 全系统 E2E 评测报告：生成题集 × 完整产品链路（2026-10-15）

**被测系统**：双击启动的产品全链路——产品 API → LLM 检索词优化（本地 Qwen3.8-27B，reasoning_effort=none）
→ runtime 检索（BM25+IR，SP-only 域，含去重×版本压制修复）→ 证据包 → 本地 27B 生成（schema v2 内联引用）
→ 非阻断校验（含事实粒度冲突强制）。
**题集**：`eval_sp_generated_v1.jsonl` 281 题（生成→双lint→AI复核通过，**provenance = ai_reviewed，未经用户亲检**），
维度：zh 168 / en 57 / mixed 56；L1 241 / L2 26 / 陷阱 14。
**载体**：build sharepoint-only-20261015，arm active_ir，today=2026-10-15。三轮运行（v1 发现两个测量缺陷并修复后 v3 为准）。

## 总览（v3，n=281，零错误）

| 指标 | 值 | 说明 |
|---|---:|---|
| answer_validated | **0.658** | validated 173 / unverified 86 / refusal 22 |
| answer_cites | 0.996 | 几乎所有回答携带结构化引用 |
| numeric_fidelity | 0.697 | 答案数字 ⊆ 证据/问题数字（v1 曾误报 0.047：[E1] 角标数字未剥离） |
| fact_hit（gold 事实命中） | 0.511–0.647 | 语言无关的答案忠实度（按语言分层见下） |
| 误拒率（可答题） | 0.049 | 13/267 |
| 陷阱正确拒答 | 0.571 | 8/14 |
| 延迟 p50 / p95 | 14.3s / 19.8s | 冷缓存；热缓存 12.0s / 17.3s |

三轮一致性：v1/v2/v3 的答案与决策**逐题相同**（检索与生成确定性）；v2→v3 仅 validated 标签变化
（0.567→0.658），全部来自事实粒度冲突修复对误标的纠正。

## CLIR：桥的价值与残余断层

| 检索方式 | zh doc@1 | zh anchor@1 |
|---|---:|---:|
| 裸 BM25+IR（harness，n=154 zh） | 0.214 | 0.175 |
| +LLM 检索词优化桥（探针，n=60 zh） | **0.300** | **0.217** |
| 英文裸查参照 | 0.404 | 0.316 |

桥把中文检索拉回约 40%（doc 层），但未完全闭合；**下游全系统把语言差进一步抹平**：

| 语言 | validated | fact_hit |
|---|---:|---:|
| en | 0.614 | 0.647 |
| mixed | 0.714 | 0.519 |
| zh | 0.583 | 0.511 |

中文问题最终答案的事实命中率（0.511）已达英文（0.647）的 79%——对比裸检索 anchor@1 只有 55%。

## 弱项画像（改进靶点）

1. **deadline 意图**：fact_hit 0.205、numeric_fid 0.333、unverified 0.444——截止日期类答案的
   日期数字与证据不合（常因证据日期为"9月18日边界/第8周"而答案引用完整日期）。最大单点改进项。
2. **陷阱拒答 0.571**：6/14 陷阱被答（均为"近似存在"型）——与冻结集已知 unrefused-trap 盲区一致；
   SP 域陷阱量少（14），需要 v2 题集补充陷阱多样性后再下结论。
3. **quantity 意图 validated 0.484**：学分数字改写（128学分 vs 128 credits）在证据合并时偶有失配。

## 评测方法学修正（本轮发现并修复的三个测量缺陷）

1. `[E1]` 内联引用角标的数字污染 numeric_fidelity（0.047→0.697，e2e_product 已修）；
2. ask 日志不存答案文本 → e2e 行内记录 answer_text，fact_hit 可离线计算；
3. 冲突强制文档粒度误标（108/110 unverified 源于同一日历文档的一个已知冲突）→ 事实粒度强制
   （product commit eec10e0），仅当答案陈述冲突事实值本身时才要求双方。

## 复现

```bash
# 全系统（需产品 8770 + runtime 8772 + Ollama 在跑）
.venv/Scripts/python.exe -m eval.e2e_product --url http://127.0.0.1:8770 \
  --eval data/benchmarks/corpus-eval/eval_sp_generated_v1.jsonl --today 2026-10-15 \
  --label local-27b-sp --skip-engine-check
.venv/Scripts/python.exe -m eval.questiongen.e2e_report --e2e <结果json> --eval <题集>
# CLIR 桥探针（产品仓）
scripts/clir_bridge_probe.py 60
```

正式引用前按 REVIEW-GUIDE 完成用户亲检（30% 抽检）升级 provenance。
