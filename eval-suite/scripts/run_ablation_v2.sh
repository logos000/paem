#!/usr/bin/env bash
# System-level ablation v2 on benchmark_superset_v1_275 (2026-10-16).
# One arm per ARCHITECTURE LAYER (user ruling: system-level, not per-feature):
#   full          完整系统
#   no-dense      嵌入模型质量（bge-m3 → bge-small-en）
#   no-queryopt   −查询理解层（LLM 检索词优化）
#   lexical       −语义检索通道（纯词汇检索，治理全开）
#   no-governance −证据治理层（裁决/硬过滤/救援/折叠/缝合/覆盖门全关 + 纯词汇）
#   template      −生成层（证据模板摘录，无 LLM 生成）
# Concurrency: 20 → 10 on rate limit (final 3 safety). Restartable: arms with
# a snapshot + full judge are skipped.
set -u
BUILDER=/e/Projects/zjui-product/llm-corpus-builder
PRODUCT=/e/Projects/zjui-product/academic-affairs-product
EVAL=/e/Projects/zjui-product/eval-suite
CORPUS='E:\Projects\zjui-product\llm-corpus-builder\data\corpus\sharepoint-only-20261015'
BENCH="$EVAL/data/benchmarks/corpus-eval/benchmark_superset_v2_289.jsonl"
PY="$BUILDER/.venv/Scripts/python.exe"
LOG=/tmp/abl_progress.log

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

restart_stack() {  # $1 = runtime env prefix, $2 = product env prefix
  for port in 8773 8775; do
    for pid in $(netstat -ano | grep ":$port" | grep LISTEN | awk '{print $NF}' | sort -u); do
      taskkill //PID "$pid" //F >/dev/null 2>&1
    done
  done
  sleep 2
  (cd "$BUILDER" && env $1 ./.venv/Scripts/python.exe -m runtime.service --build "$CORPUS" --config active_ir --port 8773 > /tmp/abl_rt.log 2>&1 &)
  for i in $(seq 1 30); do curl -s -m 2 http://127.0.0.1:8773/health >/dev/null 2>&1 && break; sleep 1; done
  curl -s -m 2 http://127.0.0.1:8773/health >/dev/null || { say "runtime 8773 failed"; tail -3 /tmp/abl_rt.log; exit 1; }
  "$PY" -c "import httpx; httpx.post('http://127.0.0.1:8773/v1/answer_context', json={'question':'prewarm','config':'active_hybrid','top_k':2}, timeout=600)" || say "prewarm WARN"
  (cd "$PRODUCT" && env BUILDER_URL=http://127.0.0.1:8773 PYTHONIOENCODING=utf-8 MAX_CONCURRENT_ASKS=30 RATE_LIMIT_PER_MINUTE=600 RATE_LIMIT_BURST=100 PYTHONPATH='E:\Projects\zjui-product\academic-affairs-product' $2 ./.venv/Scripts/python.exe -m uvicorn product.app:app --host 127.0.0.1 --port 8775 > /tmp/abl_prod.log 2>&1 &)
  for i in $(seq 1 20); do curl -s -m 2 http://127.0.0.1:8775/health/ready >/dev/null 2>&1 && break; sleep 1; done
  curl -s -m 2 http://127.0.0.1:8775/health/ready >/dev/null || { say "product 8775 failed"; tail -3 /tmp/abl_prod.log; exit 1; }
}

snapshot_of() { ls "$EVAL"/data/benchmarks/corpus-eval/results_e2e_product/e2e_product__abl-"$1"__*.json 2>/dev/null | grep -v consistency | head -1; }

error_ids() {
  "$PY" - "$1" <<'PYEOF'
import json, sys
rows = json.loads(open(sys.argv[1], encoding='utf-8').read())['rows']
print(','.join(r['id'] for r in rows if r.get('error')))
PYEOF
}

e2e_with_fallback() {  # 20 → 10 → 3, merging fixed rows back into the snapshot
  local arm="$1" snap fix ids
  (cd "$EVAL" && PYTHONIOENCODING=utf-8 PYTHONPATH=. "$PY" -m evalsuite.e2e_product \
      --url http://127.0.0.1:8775 --eval "$BENCH" --include-unverified \
      --label "abl-$arm" --today 2026-10-15 --workers 20 --skip-engine-check >> "$LOG" 2>&1) || say "E2E20 $arm nonzero"
  snap=$(snapshot_of "$arm")
  if [ -z "$snap" ]; then
    say "E2E $arm: no snapshot after w=20, retrying whole run at w=10"
    (cd "$EVAL" && PYTHONIOENCODING=utf-8 PYTHONPATH=. "$PY" -m evalsuite.e2e_product \
        --url http://127.0.0.1:8775 --eval "$BENCH" --include-unverified \
        --label "abl-$arm" --today 2026-10-15 --workers 10 --skip-engine-check >> "$LOG" 2>&1) || true
    snap=$(snapshot_of "$arm")
  fi
  [ -n "$snap" ] || { say "no snapshot $arm"; return 1; }
  for w in 10 3; do
    ids=$(error_ids "$snap"); [ -z "$ids" ] && break
    say "E2E $arm: $(echo "$ids" | tr ',' '\n' | wc -l) error rows, refixing at w=$w"
    (cd "$EVAL" && PYTHONIOENCODING=utf-8 PYTHONPATH=. "$PY" -m evalsuite.e2e_product \
        --url http://127.0.0.1:8775 --eval "$BENCH" --include-unverified \
        --label "abl-$arm-fix" --today 2026-10-15 --workers "$w" --skip-engine-check --ids "$ids" >> "$LOG" 2>&1)
    fix=$(snapshot_of "$arm-fix"); [ -n "$fix" ] || continue
    "$PY" - "$snap" "$fix" <<'PYEOF'
import json, sys
main_p, fix_p = sys.argv[1], sys.argv[2]
main = json.loads(open(main_p, encoding='utf-8').read())
fixed = {r['id']: r for r in json.loads(open(fix_p, encoding='utf-8').read())['rows'] if not r.get('error')}
kept, merged = [], 0
for r in main['rows']:
    if r.get('error') and r['id'] in fixed:
        kept.append(fixed[r['id']]); merged += 1
    else:
        kept.append(r)
main['rows'] = kept
json.dump(main, open(main_p, 'w', encoding='utf-8'), ensure_ascii=False)
print(f'merged {merged} fixed rows, {sum(1 for r in kept if r.get("error"))} errors remain')
PYEOF
    rm -f "$fix" "${fix%.json}.consistency.json"
  done
}

judge_with_fallback() {  # 20 → 10 (resume)
  local arm="$1" snap n out
  snap=$(snapshot_of "$arm"); out="$EVAL/data/benchmarks/corpus-eval/results/llm_judge/judge__abl-$arm.jsonl"
  for w in 20 10; do
    n=$(wc -l < "$out" 2>/dev/null || echo 0); [ "$n" -ge 275 ] && return 0
    say "judge $arm at w=$w (have $n)"
    (cd "$EVAL" && PYTHONIOENCODING=utf-8 PYTHONPATH=. "$PY" scripts/llm_judge.py \
        --e2e "$snap" --eval "$BENCH" --corpus "$CORPUS" --all --workers "$w" \
        --out "$out" \
        --report "$EVAL/data/benchmarks/corpus-eval/results/llm_judge/judge__abl-$arm.report.md" >> "$LOG" 2>&1) || say "judge w=$w $arm nonzero"
  done
  n=$(wc -l < "$out" 2>/dev/null || echo 0); [ "$n" -lt 275 ] && say "WARN judge $arm short: $n/275"
}

run_arm() {
  local arm="$1" snap judge_ok=0
  snap=$(snapshot_of "$arm")
  if [ -f "$EVAL/data/benchmarks/corpus-eval/results/llm_judge/judge__abl-$arm.jsonl" ]; then
    judge_ok=$(wc -l < "$EVAL/data/benchmarks/corpus-eval/results/llm_judge/judge__abl-$arm.jsonl")
  fi
  if [ -n "$snap" ] && [ "$judge_ok" -ge 275 ]; then
    say "=== ARM $arm already complete, skip ==="; return
  fi
  say "=== ARM $arm ==="
  if [ -z "$snap" ]; then
    restart_stack "$2" "$3"
    e2e_with_fallback "$arm" || return
  else
    say "$arm snapshot exists, judge only"
  fi
  snap=$(snapshot_of "$arm")
  (cd "$EVAL" && PYTHONIOENCODING=utf-8 PYTHONPATH=. "$PY" scripts/check_consistency.py --snapshot "$snap" >> "$LOG" 2>&1) || say "consistency $arm FAILED"
  judge_with_fallback "$arm"
  say "=== ARM $arm DONE ==="
}

say "ablation v2 (system-level) start: full/no-dense/no-queryopt/lexical/no-governance/template"
run_arm full        "" ""
run_arm no-dense    "ENGINE_DENSE_MODEL=models/embed/bge-small-en" ""
run_arm no-queryopt "" "FEATURE_QUERY_OPTIMIZATION=0"
run_arm lexical     "" "PRODUCT_ARM=sp_lexical"
run_arm no-governance "RUNTIME_SECTION_ROLLUP=0 RUNTIME_NEIGHBOR_STITCH=0 RUNTIME_COVERAGE_UNION=0 RUNTIME_HARD_FILTER=0 RUNTIME_FACT_RESCUE=0 RUNTIME_COVERAGE_FLOOR=0" "PRODUCT_ARM=sp_lexical FEATURE_QUERY_OPTIMIZATION=0"
run_arm template    "" "LLM_API_KEY= LLM_BASE_URL= LLM_MODEL="
run_arm naive-dense "RUNTIME_SECTION_ROLLUP=0 RUNTIME_NEIGHBOR_STITCH=0 RUNTIME_COVERAGE_UNION=0 RUNTIME_HARD_FILTER=0 RUNTIME_FACT_RESCUE=0 RUNTIME_COVERAGE_FLOOR=0" "PRODUCT_ARM=active_dense PRODUCT_EXTRA_ARMS=active_dense"
say "ablation v2 ALL DONE"
