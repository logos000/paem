# -*- coding: utf-8 -*-
"""Baseline B (2026-10-17): RAGFlow v1.0.0-rc2 — the production-grade OSS RAG
platform — on the same 111 SP source docs, same benchmark (v2_289), same judge.

Phases (state in data/external_baselines/ragflow_state.json; rerun skips done):
  1 login+dataset   2 upload+ingest+wait-parse
  3 configure the ZJU gateway as an OpenAI-compatible tenant model
  4 create chat assistant   5 run 289 completions -> e2e-shaped snapshot

Everything local (corpus never leaves the machine; LLM via the campus gateway).
Usage: ../llm-corpus-builder/.venv/Scripts/python.exe scripts/ragflow_baseline.py [--phase N] [--workers 8]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:9380/api/v1"
EMAIL, PASSWORD = "eval@example.com", "Eval12345!"
DOCS_DIR = ROOT / "data" / "external_baselines" / "sp_source_docs"
DOC_MAP = json.loads((ROOT / "data" / "external_baselines" / "sp_source_docs_map.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "data" / "external_baselines" / "ragflow_state.json"
OUT_PATH = ROOT / "data" / "benchmarks" / "corpus-eval" / "results_e2e_product" / "ext_ragflow.json"


def _load_state() -> dict:
    if STATE_PATH.is_file():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _client(state: dict) -> httpx.Client:
    token = state.get("token")
    headers = {"Authorization": token} if token else {}
    return httpx.Client(base_url=API, headers=headers, timeout=300)


def phase1(state: dict) -> None:
    with httpx.Client(base_url=API, timeout=60) as c:
        r = c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
        r.raise_for_status()
        token = r.headers.get("Authorization")
        assert token, "no Authorization header on login"
        state["token"] = token
        _save(state)
        c.headers["Authorization"] = token
        r = c.post("/datasets", json={"name": "sp-eval", "language": "Chinese", "permission": "me", "parse_type": 1, "parser_id": "naive"})
        r.raise_for_status()
        state["dataset_id"] = r.json()["data"]["id"]
        _save(state)
        print(f"[rf] dataset {state['dataset_id']}")


def phase2(state: dict) -> None:
    files = sorted(DOCS_DIR.iterdir())
    ds = state["dataset_id"]
    with _client(state) as c:
        if state.get("doc_ids") and state.get("parse_triggered"):
            uploaded = state["doc_ids"]
            print(f"[rf] resume: {len(uploaded)} docs already uploaded+triggered", flush=True)
        else:
            uploaded: list[str] = []
            for i in range(0, len(files), 10):
                batch = files[i : i + 10]
                multipart = [("file", (f.name, f.read_bytes(), _mime(f))) for f in batch]
                r = c.post(f"/datasets/{ds}/documents", files=multipart)
                r.raise_for_status()
                data = r.json()["data"]
                rows = data if isinstance(data, list) else [data]
                uploaded += [row["id"] for row in rows]
                print(f"[rf] uploaded {len(uploaded)}/{len(files)}", flush=True)
            state["doc_ids"] = uploaded
            state["parse_triggered"] = False
            _save(state)
        if not state.get("parse_triggered"):
            r = c.post(f"/datasets/{ds}/chunks", json={"document_ids": uploaded})
            r.raise_for_status()
            state["parse_triggered"] = True
            _save(state)
            print(f"[rf] parse triggered for {len(uploaded)} docs", flush=True)
        deadline = time.time() + 3600
        while time.time() < deadline:
            time.sleep(20)
            page = c.get(f"/datasets/{ds}/documents", params={"page": 1, "page_size": 200})
            page.raise_for_status()
            body = page.json()["data"]
            docs = body.get("docs", body.get("documents", body)) if isinstance(body, dict) else body
            runs: dict[str, int] = {}
            for row in docs or []:
                if not isinstance(row, dict):
                    continue
                status = row.get("ingestion_status") or row.get("run") or "?"
                runs[status] = runs.get(status, 0) + 1
            print(f"[rf] parse status: {runs}", flush=True)
            terminal = runs.get("COMPLETED", 0) + runs.get("DONE", 0) + runs.get("FAILED", 0) + runs.get("FAIL", 0)
            if runs and terminal >= len(uploaded):
                state["parse"] = runs
                _save(state)
                break
        else:
            raise SystemExit("[rf] parse timeout 60min")


def _mime(path: Path) -> str:
    return {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".html": "text/html"}.get(path.suffix.lower(), "application/octet-stream")


def phase3(state: dict) -> None:
    """Providers were configured manually via the providers API (2026-10-17):
    OpenAI-API-Compatible provider + instance 'default' (local bge-m3 shim,
    host.docker.internal:18081) + instance 'zju-gateway' (campus LLM gateway),
    tenant defaults embd/llm set.  Kept as a documented no-op."""
    print("[rf] phase3: providers already configured (see commit history / TRAPSET ledger)")


def phase4(state: dict) -> None:
    with _client(state) as c:
        r = c.post("/chats", json={"name": "sp-eval-chat", "dataset_ids": [state["dataset_id"]]})
        r.raise_for_status()
        state["chat_id"] = r.json()["data"]["id"]
        _save(state)
        print(f"[rf] chat {state['chat_id']}")


def phase5(state: dict, workers: int) -> None:
    bench = [json.loads(l) for l in open(ROOT / "data" / "benchmarks" / "corpus-eval" / "benchmark_superset_v2_289.jsonl", encoding="utf-8")]
    done: dict[str, dict] = {}
    if OUT_PATH.is_file():
        done = {r["id"]: r for r in json.loads(OUT_PATH.read_text(encoding="utf-8"))["rows"]}
        print(f"resume: {len(done)}")
    todo = [b for b in bench if b["id"] not in done]
    rows: list[dict] = []

    def ask(item):
        started = time.perf_counter()
        with _client(state) as c:
            r = c.post("/chat/completions", json={"chat_id": state["chat_id"], "question": item["question"], "stream": False})
            elapsed = (time.perf_counter() - started) * 1000
            r.raise_for_status()
            payload = r.json().get("data", r.json())
        answer = payload.get("answer", "")
        abstained = any(m in answer for m in ("没有该信息", "不知道", "无法确定", "no information", "I don't know", "empty_response"))
        ref = payload.get("reference") or {}
        chunks = (ref.get("chunks") or [])[:8]
        evidence = []
        cited_files: list[str] = []
        for i, ch in enumerate(chunks, 1):
            doc_name = ch.get("document_keyword") or ch.get("doc_name") or ""
            cited_files.append(doc_name)
            uid = DOC_MAP.get(Path(doc_name).name, "")
            evidence.append({"evidence_id": f"E{i}", "doc_id": uid, "chunk_id": ch.get("chunk_id", ""), "title": doc_name, "text": (ch.get("content") or "")[:1200]})
        return item, {
            "id": item["id"], "category": item.get("category", ""), "level": item.get("level"),
            "language": item.get("language"), "qshape": item.get("qshape", ""),
            "answerable": bool(item.get("gold")),
            "answer_text": answer, "answer_disposition": "evidence_refusal" if abstained else "answer",
            "abstained": abstained, "cites_evidence": bool(chunks), "grounded": bool(chunks),
            "numeric_fidelity": True, "latency_ms": round(elapsed, 1),
            "claims": [], "citations": [{"doc_uid": DOC_MAP.get(Path(n).name, ""), "title": n} for n in set(cited_files)], "citation_map": {},
            "decision": {"status": "sufficient", "abstain": abstained},
            "generation": {"mode": "ragflow"},
            "evidence_pack": {"evidence": evidence, "usable_evidence_ids": [e["evidence_id"] for e in evidence],
                              "requirements": [], "evidence_status": "sufficient", "abstain": False,
                              "abstain_reason": "", "coverage": 0.0, "conflicts": [], "fact_conflicts": []},
            "retrieval_trace": {"strategy": "ragflow", "queries": [], "rounds": []},
        }

    def write():
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps({"meta": {"label": "ext-ragflow", "url": API, "n": len(rows) + len(done),
                                                 "providers": ["ragflow-v1.0.0-rc2"], "snapshot_version": 3},
                                        "rows": rows + list(done.values())}, ensure_ascii=False), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(ask, item) for item in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                item, row = fut.result()
                rows.append(row)
            except Exception as error:
                print(f"[rf] ask failed: {type(error).__name__} {str(error)[:120]}", flush=True)
            if n % 20 == 0:
                print(f"[rf] {n}/{len(todo)}", flush=True)
                write()
    write()
    print(f"done: {len(rows) + len(done)} -> {OUT_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, default=0, help="run one phase; 0 = run all pending")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    state = _load_state()
    phases = {1: lambda: phase1(state), 2: lambda: phase2(state), 3: lambda: phase3(state),
              4: lambda: phase4(state), 5: lambda: phase5(state, args.workers)}
    order = [args.phase] if args.phase else [1, 2, 3, 4, 5]
    for n in order:
        needed = {1: not state.get("token") or not state.get("dataset_id"), 2: not state.get("doc_ids") or not state.get("parse"),
                  3: not state.get("llm_set"), 4: not state.get("chat_id"), 5: True}
        if needed.get(n):
            print(f"=== phase {n} ===", flush=True)
            phases[n]()
            if n == 3:
                state["llm_set"] = True
                _save(state)


if __name__ == "__main__":
    main()
