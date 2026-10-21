#!/usr/bin/env bash
# Local CI (P4): both repos' tests + contract equivalence + startup smoke.
# Usage: bash scripts/ci.sh [--compose]   (--compose also builds/starts docker compose)
set -euo pipefail
export PYTHONIOENCODING=utf-8

PRODUCT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILDER_DIR="${BUILDER_DIR:-/e/Projects/zjui-product/llm-corpus-builder}"

echo "== [1/4] product tests =="
(cd "$PRODUCT_DIR" && .venv/Scripts/python.exe -m pytest -q)

echo "== [2/4] builder tests (incl. runtime contract + admin switching) =="
(cd "$BUILDER_DIR" && PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q)

echo "== [3/4] startup smoke: runtime + product + preflight + one ask =="
(cd "$BUILDER_DIR" && PYTHONPATH=. .venv/Scripts/python.exe -m runtime.service --port 8772 > data/service/ci_server.log 2>&1 &
  echo $! > /tmp/ci_runtime.pid)
for i in $(seq 1 40); do
  curl -s -m 5 http://127.0.0.1:8772/health > /dev/null 2>&1 && break; sleep 5
done
curl -sf -m 5 http://127.0.0.1:8772/health > /dev/null || { echo "runtime did not become healthy"; kill "$(cat /tmp/ci_runtime.pid)" 2>/dev/null || true; exit 1; }
(cd "$PRODUCT_DIR" && BUILDER_URL=http://127.0.0.1:8772 PYTHONPATH=. .venv/Scripts/python.exe -m uvicorn product.app:app --port 8770 > /tmp/ci_product.log 2>&1 &
  echo $! > /tmp/ci_product.pid)
trap 'kill "$(cat /tmp/ci_runtime.pid)" "$(cat /tmp/ci_product.pid)" 2>/dev/null || true' EXIT
for i in $(seq 1 20); do
  curl -s -m 5 http://127.0.0.1:8770/health/ready > /dev/null 2>&1 && break; sleep 3
done
(cd "$PRODUCT_DIR" && .venv/Scripts/python.exe scripts/preflight.py)
printf '%s' '{"question":"ZJUI 2026 fall undergraduate course withdrawal deadline?"}' > /tmp/ci_q.json
ANSWER=$(curl -sf -m 60 -X POST http://127.0.0.1:8770/v1/ask -H "Content-Type: application/json" --data-binary @/tmp/ci_q.json)
echo "$ANSWER" | grep -q '"ok":true' || { echo "ask smoke failed"; exit 1; }
echo "ask smoke ok: $(echo "$ANSWER" | head -c 120)"

echo "== [4/4] summary =="
echo "CI OK (tests + contract + startup smoke)"

if [[ "${1:-}" == "--compose" ]]; then
  echo "== [compose] build + up + ask + down =="
  cd "$PRODUCT_DIR/deploy"
  CORPUS_DIR="$BUILDER_DIR/data/corpus" docker compose up -d --build
  for i in $(seq 1 40); do
    curl -s -m 5 http://127.0.0.1:8770/health/ready > /dev/null 2>&1 && break; sleep 5
  done
  curl -sf -m 90 -X POST http://127.0.0.1:8770/v1/ask -H "Content-Type: application/json" --data-binary @/tmp/ci_q.json | grep -q '"ok":true'
  docker compose down
  echo "COMPOSE OK"
fi
