"""Stage 3A W4: 92-question end-to-end run through the product HTTP API.

The retrieval path is the same CorpusEngine the offline harness measures
(proven byte-identical in W1b), so this measures the ONLINE stack: the
runtime HTTP service + the product adapter + a provider (deterministic
``evidence-template`` or an OpenAI-compatible LLM).  Per question it also
asserts that the pack served by the product equals the in-process engine's
pack — the single-path guarantee, checked over HTTP.

Usage:
    python -m evalsuite.e2e_product --label template \
        --corpus data/corpus/sharepoint-only-20261015 --today 2026-10-15
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]

from evalsuite import builder_root  # noqa: E402
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence.store import match_key, read_jsonl  # noqa: E402
from evalsuite.harness import quote_hit  # noqa: E402
from runtime.engine import CorpusEngine  # noqa: E402


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


#: Language-neutral claim anchors: digit runs (dates/credits/codes) and long
#: Latin tokens (course codes, system names, proper nouns).  A zh answer
#: paraphrasing en evidence still carries these verbatim.
_CLAIM_ANCHOR_RE = re.compile(r"\d[\d.:/-]*\d|\d|[A-Za-z][A-Za-z./-]{3,}")
_STOP_ANCHORS = {"http", "https", "sharepoint", "zjuintl", "www", "com", "edu", "html", "aspx", "pdf", "xlsx", "docx"}


def _claim_grounded(claims: list[dict[str, Any]], evidence_by_id: dict[str, str]) -> bool:
    """True when some claim's anchors all appear in the evidence it cites.

    Replaces the verbatim gold-quote echo (2026-10-16): zh answers against
    en gold quotes scored ~0 forever — the metric measured script overlap,
    not grounding.  Anchoring on digits + Latin tokens is deterministic,
    language-agnostic, and exactly the same evidence the claim validator
    trusts; claims citing no usable evidence do not count.
    """
    for claim in claims or []:
        ids = claim.get("evidence_ids") or []
        if not ids:
            continue
        text = " ".join(evidence_by_id.get(str(eid), "") for eid in ids)
        if not text:
            continue
        anchors = {a.casefold() for a in _CLAIM_ANCHOR_RE.findall(str(claim.get("text") or ""))}
        anchors = {a for a in anchors if a not in _STOP_ANCHORS and not set(a) <= {"."} and a not in {"/", "//"}}
        if len(anchors) < 2:
            continue
        body = text.casefold()
        missed = [a for a in anchors if a not in body]
        # allow one miss (truncation, heading refs) — grounding is a smoke
        # signal here, the judge measures the real thing
        if len(missed) <= 1:
            return True
    return False


def _answer_metrics(answer: str, evidence_texts: list[str], quotes_by_doc: dict[str, list[str]], question: str, abstained: bool,
                    claims: list[dict[str, Any]] | None = None, evidence_by_id: dict[str, str] | None = None) -> dict[str, Any]:
    """Answer-layer metrics adapted to the product citation format.

    Schema v2 answers carry ``[E1]``-style inline markers (legacy template
    prose used ``[1]``) — BOTH must be stripped before the digit scan, or the
    marker indices pollute numeric_fidelity (seen as 0.05 on the 2026-10-15
    generated-set run).
    """
    cites = any(marker in answer for marker in (f"[{i}]" for i in range(1, len(evidence_texts) + 1))) or bool(re.search(r"\[\s*[Ee]?\s*\d+\s*\]", answer))
    clean_answer = re.sub(r"\[\s*[Ee]?\s*\d+\s*\]", "", answer)
    pack_digits = set(re.findall(r"\d+", match_key("\n".join(evidence_texts))))
    answer_digits = set(re.findall(r"\d+", match_key(clean_answer)))
    question_digits = set(re.findall(r"\d+", match_key(question)))
    novel_digits = answer_digits - question_digits
    numeric_fidelity = bool(not novel_digits or novel_digits <= pack_digits)
    quote_keys = [match_key(q) for quotes in quotes_by_doc.values() for q in quotes]
    legacy_echo = any(q and len(q) >= 8 and (q[:8] in match_key(clean_answer) or q[-8:] in match_key(clean_answer)) for q in quote_keys)
    grounded = _claim_grounded(claims or [], evidence_by_id or {}) or legacy_echo
    return {"cites_evidence": cites, "numeric_fidelity": numeric_fidelity, "grounded": grounded, "abstained": abstained}


def response_record(item: dict[str, Any], body: dict[str, Any], elapsed: float) -> dict[str, Any]:
    """Lossless answer/evidence snapshot for independent, replayable scoring."""
    quotes_by_doc: dict[str, list[str]] = {}
    for g in item.get("gold") or []:
        quotes_by_doc.setdefault(g["doc_uid"], []).append(g["quote"])
    evidence = body["evidence_pack"]["evidence"]
    return {
        "id": item["id"], "category": item.get("category", ""), "level": item.get("level"),
        "answerable": bool(item.get("gold")), "request_id": body.get("request_id", ""),
        "build_id": body.get("build_id", ""), "config": body.get("config", ""),
        "status": body.get("status", ""), "pack_hash": body.get("pack_hash", ""),
        "diagnostics": body.get("diagnostics", {}),
        "query_ir": body.get("query_ir", {}),
        "decision": body["decision"], "provider": body["provider"], "generation": body.get("generation", {}),
        "answer_disposition": body.get("answer_disposition", "unknown"),
        "n_claims": len(body.get("claims", [])), "answer_text": body["answer"], "answer_complete": True,
        "evidence_pack": body["evidence_pack"], "claims": body.get("claims", []),
        "citations": body.get("citations", []), "citation_map": body.get("citation_map", {}),
        "retrieval_trace": body.get("retrieval_trace", {}), "follow_up_queries": body.get("follow_up_queries", []),
        "timing": body.get("timing", {}), "latency_ms": round(elapsed, 1),
        "snapshot_version": 3,
        **_answer_metrics(body["answer"], [ev["text"] for ev in evidence], quotes_by_doc, item["question"], body["decision"]["abstain"],
                          claims=body.get("claims") or [], evidence_by_id={ev["evidence_id"]: ev.get("text") or "" for ev in evidence}),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8770")
    parser.add_argument("--eval", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "verified_eval_v3.jsonl")
    parser.add_argument("--corpus", type=Path, default=builder_root() / "data" / "corpus" / "sharepoint-only-20261015",
                        help="engine-check corpus only (SP domain); the snapshot FILENAME uses the build that actually served")
    parser.add_argument("--config", default="active_hybrid")
    parser.add_argument("--arm", default="", help="P6: per-request arm override through the product API (requires PRODUCT_EXTRA_ARMS)")
    parser.add_argument("--rerank-model", default="bge-v2-m3")
    parser.add_argument("--today", default="2026-10-15", help="default today sent for items without their own")
    parser.add_argument("--label", default="template", help="provider label for the output files")
    parser.add_argument("--skip-engine-check", action="store_true", help="skip the in-process pack-consistency pass")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-unverified", action="store_true", help="also run rows with verified != true (smoke/pilot sets)")
    parser.add_argument("--workers", type=int, default=20, help="concurrent ask requests (campus-gateway default 20, user ruling 2026-10-16; use 1-3 for local Ollama single-slot)")
    parser.add_argument("--ids", default="", help="comma-separated question ids to run (subset re-runs; default all)")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "results_e2e_product")
    args = parser.parse_args(argv)

    items = [row for row in read_jsonl(args.eval) if row.get("verified") or args.include_unverified]
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [row for row in items if row["id"] in wanted]
    if args.limit:
        items = items[: args.limit]
    engine = None
    if not args.skip_engine_check:
        engine = CorpusEngine.load(args.corpus, config=args.arm or args.config, rerank_model=args.rerank_model)
        print(f"[engine] {engine.health()}", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    pack_consistent = 0

    def _one(item: dict[str, Any], client_factory) -> dict[str, Any]:
        today = item.get("today") or args.today
        question = item["question"]
        started = time.perf_counter()
        body_json = {"question": question, "today": today}
        if args.arm:
            body_json["arm"] = args.arm
        try:
            response = client_factory().post("/v1/ask", json=body_json)
            if response.status_code >= 500:  # transient runtime blip (e.g. 502 unreachable): one retry
                time.sleep(2)
                response = client_factory().post("/v1/ask", json=body_json)
        except Exception as error:  # transport reset (WinError 10054) must be an
            # error ROW, not a process crash — the ablation fallback passes
            # re-run exactly these ids (2026-10-16 live: w=20 killed the run).
            elapsed = (time.perf_counter() - started) * 1000
            return {"id": item["id"], "error": f"transport: {type(error).__name__}: {str(error)[:100]}", "latency_ms": round(elapsed, 1)}
        elapsed = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            return {"id": item["id"], "error": f"{response.status_code}: {response.text[:120]}", "latency_ms": round(elapsed, 1)}
        body = response.json()
        row = response_record(item, body, elapsed)
        if body.get("claims") is not None:
            row["cites_evidence"] = bool(body["claims"])
        return row

    with httpx.Client(base_url=args.url, timeout=httpx.Timeout(30, read=300)) as client:
        health = client.get("/health").json()
        print(f"[product] runtime build={health['runtime']['build_id']} config={health['runtime']['config']}", file=sys.stderr)

        def _run_one(item: dict[str, Any]) -> dict[str, Any]:
            row = _one(item, lambda: client)
            if args.workers > 1:  # each worker thread gets its own client (httpx Client is not thread-safe for concurrent posts)
                return row
            return row

        if args.workers > 1:
            import threading
            from concurrent.futures import ThreadPoolExecutor

            clients: dict[int, httpx.Client] = {}
            lock = threading.Lock()

            def _worker(item: dict[str, Any]) -> dict[str, Any]:
                tid = threading.get_ident()
                with lock:
                    c = clients.get(tid)
                    if c is None:
                        c = httpx.Client(base_url=args.url, timeout=httpx.Timeout(30, read=300))
                        clients[tid] = c
                row = _one(item, lambda: c)
                if args.limit == 0 or True:
                    pass
                return row

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                rows = list(pool.map(_worker, items))
            for c in clients.values():
                c.close()
        else:
            for index, item in enumerate(items, 1):
                row = _one(item, lambda: client)
                rows.append(row)
                if index % 10 == 0:
                    print(f"[e2e] {index}/{len(items)} ({row.get('latency_ms', 0):.0f}ms)", file=sys.stderr)

    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms")]
    if engine is not None:
        for row in rows:
            item = next(i for i in items if i["id"] == row["id"])
            today = item.get("today") or args.today
            trace = row.get("retrieval_trace", {})
            queries = trace.get("queries", [])
            direct = engine.answer_context(item["question"], today=date.fromisoformat(today),
                                           retrieval_queries=queries if trace.get("strategy") == "multi_query_rrf" else None,
                                           retrieval_query=queries[0] if queries and trace.get("strategy") == "single_query" else None)
            consistent = row.get("build_id") == engine.corpus.build_id and direct["evidence_pack"] == row.get("evidence_pack")
            row["pack_consistent"] = consistent
            pack_consistent += int(consistent)

    def rate(values: list[bool]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    answerable = [r for r in rows if r.get("answerable")]
    unanswerable = [r for r in rows if not r.get("answerable") and "decision" in r]
    from collections import Counter

    modes = dict(Counter((r.get("generation") or {}).get("mode", "?") for r in rows if "generation" in r))
    answered = [r for r in answerable if "decision" in r and not r["decision"]["abstain"]]
    summary = {
        "label": args.label,
        "url": args.url,
        "n": len(rows),
        "errors": sum(1 for r in rows if r.get("error")),
        "false_abstain": rate([r["decision"]["abstain"] for r in answerable]),
        "correct_abstain": rate([r["decision"]["abstain"] for r in unanswerable]),
        "answer_cites": rate([r["cites_evidence"] for r in answered]),
        "numeric_fidelity": rate([r["numeric_fidelity"] for r in answered]),
        "grounded": rate([r["grounded"] for r in answered]),
        "generation_modes": modes,
        "answer_validated": rate([(r.get("generation") or {}).get("mode") == "validated" for r in answered]),
        "claims_per_answer": round(sum(r.get("n_claims", 0) for r in answered) / max(1, len(answered)), 2),
        "pack_consistency": f"{pack_consistent}/{len(items)}" if engine is not None else "skipped",
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    # Run manifest (P6 traceability): every product-level number file carries
    # its build/arm/provider/urls so tables can be reproduced.
    answered_rows = [r for r in rows if isinstance(r, dict) and r.get("build_id")]
    # Name the snapshot by the build that ACTUALLY served the rows.  Naming it
    # after --corpus (the engine-check default) once pointed readers at the
    # frozen master while every row was served from the SP build (2026-10-17).
    served = answered_rows[0]["build_id"] if answered_rows else args.corpus.name
    stem = f"e2e_product__{args.label}__{served}"

    def _git_commit(repo: Path) -> str:
        import subprocess
        try:
            return subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    providers = sorted({r.get("provider", "") for r in answered_rows if r.get("provider")})
    meta = {
        "snapshot_version": 3,
        "eval_sha256": hashlib.sha256(args.eval.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "label": args.label,
        "arm": args.arm or (engine.config if engine is not None else ""),
        "corpus_build": args.corpus.name,
        "served_build": (answered_rows[0]["build_id"] if answered_rows else ""),
        "url": args.url,
        "today_default": args.today,
        "engine_check": engine is not None,
        "workers": args.workers,
        "providers": providers,
        "product_commit": _git_commit(ROOT.parent / "academic-affairs-product"),
        "runtime_commit": _git_commit(builder_root()),
        "n": len(rows),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (args.out / f"{stem}.json").write_text(json.dumps({"meta": meta, "summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
