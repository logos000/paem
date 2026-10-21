"""Track B runner: full-workflow end-to-end comparison (P7.3).

同一 raw snapshot、同一 verified-only 题集、同一生成模型、同一回答 prompt、
同一证据预算（top-8）、同一引用后核验（oracle_pack 的 cites/numfid/refused/
grounded 度量）下比较完整工作流：

- B0 naive RAG：常规清洗切块 + dense top-k + 生成（无规则层）
- B1 standard hybrid：BM25+dense RRF + 生成
- B2 strong baseline：hybrid top-50 → bge-v2-m3 重排 → top-8 + 生成
  （禁用本项目版本链与 EvidencePack 规则）
- B3 本项目 deterministic evidence pipeline（active_ir + EvidencePack + 拒答）
- B4 本项目 validated LLM corpus pipeline（active_llm_validated_ir + EvidencePack）

公平性：所有工作流共享 chunk 参数（B0-B2）、证据预算、生成器与核验器；每个
配置落 BenchmarkManifest（track=B）。陷阱题对 B0-B2 是"必然作答"（无决策层，
记 answered_on_trap），对 B3/B4 走 abstain 决策（记 correct_abstain）。

用法::

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m bench.track_b \\
        --corpus data/corpus/<build> --eval data/benchmarks/corpus-eval/verified_eval.jsonl \\
        --configs B0,B1,B2,B3,B4 --today 2026-10-14
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.chunkers import build_baseline_units  # noqa: E402
from bench.manifest import BenchmarkManifest, schema_json  # noqa: E402
from eval.oracle_pack import PROMPT, _complete_text, _metrics, _pack_items  # noqa: E402
from evidence.store import read_jsonl  # noqa: E402
from annotate.runner import load_env  # noqa: E402

__all__ = ["run_track_b", "CONFIGS"]

CONFIGS = ("B0", "B1", "B2", "B3", "B4")
DENSE_MODEL = "models/embed/bge-small-zh"
RERANK_ALIAS = "bge-v2-m3"


def _baseline_top8(corpus, units, index, question, mode, dense_matrix, reranker):
    """B0/B1/B2 retrieval over baseline chunks; returns up to 8 unit dicts."""
    import retrieve.dense as dense

    if mode == "B0":
        ranked = dense.topk(dense_matrix, dense.embed_query(question, DENSE_MODEL), 8)
    elif mode == "B1":
        ranked = dense.rrf_fuse([index.search(question, 50), dense.topk(dense_matrix, dense.embed_query(question, DENSE_MODEL), 50)])[:8]
    else:  # B2
        pool = dense.rrf_fuse([index.search(question, 50), dense.topk(dense_matrix, dense.embed_query(question, DENSE_MODEL), 50)])[:50]
        if reranker is not None and pool:
            texts = [units[i].index_text or units[i].text for i, _ in pool]
            scores = reranker.score([(question, text) for text in texts])
            ranked = [pair for _, pair in sorted(zip(scores, pool), key=lambda p: -p[0])][:8]
        else:
            ranked = pool[:8]
    return [{"title": corpus.documents.get(corpus.canonical(units[i].doc_uid), {}).get("title", ""),
             "terms": corpus.documents.get(corpus.canonical(units[i].doc_uid), {}).get("terms"),
             "audience": corpus.documents.get(corpus.canonical(units[i].doc_uid), {}).get("audience"),
             "text": units[i].text} for i, _ in ranked]


def _pipeline_top8(corpus, index, question, today, arm):
    """B3/B4 retrieval through our IR; usable pack items only (decision applies)."""
    from retrieve.lexical import corpus_hit, ir_rerank
    from runtime.indexing import load_known_conflicts

    triples = ir_rerank(corpus, index, question, k=10, today=today)
    hits = [corpus_hit(corpus, index.units[i], score) for i, score, _ in triples[:8]]
    from evidence.pack import build_evidence_pack

    pack = build_evidence_pack(question, hits, today=today, known_conflicts=load_known_conflicts(corpus.root))
    usable = {ev["evidence_id"] for ev in pack["evidence"] if ev["evidence_id"] in set(pack["usable_evidence_ids"])}
    items = [{"title": ev.get("title", ""), "terms": (ev.get("applicability") or {}).get("terms"),
              "audience": (ev.get("applicability") or {}).get("audience"), "text": ev["text"]}
             for ev in pack["evidence"] if ev["evidence_id"] in usable]
    return items, pack


def run_track_b(corpus_dir: Path, eval_file: Path, configs: list[str], *, today: str = "", generator_model: str = "", out_dir: Path | None = None) -> dict[str, Any]:
    from retrieve.lexical import BM25Index, Corpus, Unit

    corpus = Corpus(corpus_dir)
    items_all = [row for row in read_jsonl(eval_file) if row.get("verified")]
    if not items_all:
        raise SystemExit("no verified items")
    out_dir = out_dir or corpus_dir / "_bench_track_b"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "benchmark_manifest.schema.json").write_text(schema_json(), encoding="utf-8")
    env = load_env()

    baseline_units = None
    baseline_index = None
    dense_matrix = None
    reranker = None
    pipeline_indexes: dict[str, Any] = {}

    results: dict[str, Any] = {}
    for config in configs:
        started = time.perf_counter()
        gen_seconds: list[float] = []
        rows = []
        for item in items_all:
            question = item["question"]
            qtoday = date.fromisoformat(item["today"]) if item.get("today") else (date.fromisoformat(today) if today else None)
            gold = item.get("gold") or []
            quotes_by_doc: dict[str, list[str]] = {}
            for g in gold:
                quotes_by_doc.setdefault(corpus.canonical(g["doc_uid"]), []).append(g["quote"])

            pack_abstain = False
            if config in ("B0", "B1", "B2"):
                if baseline_units is None:
                    raw = build_baseline_units(corpus_dir, "clean_recursive")
                    docs_map = corpus.documents
                    baseline_units = [Unit(doc_uid=u["doc_uid"], text=u["text"], block_id=u["block_id"],
                                           index_text=f"{docs_map.get(u['doc_uid'], {}).get('title', '')}\n{u['text']}") for u in raw]
                    baseline_index = BM25Index(baseline_units)
                if config in ("B0", "B1", "B2") and dense_matrix is None:
                    import retrieve.dense as dense

                    dense_matrix = dense.embed_units([u.index_text or u.text for u in baseline_units], DENSE_MODEL, out_dir / "track_b_dense.npy")
                if config == "B2" and reranker is None:
                    from retrieve.rerank import get_reranker

                    reranker = get_reranker(RERANK_ALIAS)
                mode = {"B0": "dense", "B1": "hybrid", "B2": "hybrid_rr"}[config]
                evidence = _baseline_top8(corpus, baseline_units, baseline_index, question, mode, dense_matrix, reranker)
            else:
                arm = "active_ir" if config == "B3" else "active_llm_validated_ir"
                if arm not in pipeline_indexes:
                    from runtime.indexing import load_index

                    pipeline_indexes[arm] = load_index(corpus, arm)
                evidence, pack = _pipeline_top8(corpus, pipeline_indexes[arm], question, qtoday, arm)
                pack_abstain = bool(pack["abstain"])

            row = {"id": item["id"], "answerable": bool(gold), "abstained": pack_abstain or not evidence}
            if gold and not row["abstained"] and evidence:
                t0 = time.perf_counter()
                answer = _complete_text(question, _pack_items(evidence), env, model=generator_model)
                gen_seconds.append(time.perf_counter() - t0)
                row.update(_metrics(answer, evidence, quotes_by_doc, question))
                row["answer"] = answer[:300]
            elif not gold:
                # trap: baselines must answer (no decision layer) -> answered_on_trap;
                # B3/B4 abstain decision -> correct_abstain
                if config in ("B0", "B1", "B2") and evidence:
                    t0 = time.perf_counter()
                    answer = _complete_text(question, _pack_items(evidence), env, model=generator_model)
                    gen_seconds.append(time.perf_counter() - t0)
                    row["trap_refused_verbal"] = "证据不足" in answer
                    row["answered_on_trap"] = not row["trap_refused_verbal"]
                    row["answer"] = answer[:200]
                else:
                    row["correct_abstain"] = row["abstained"]
            rows.append(row)
        results[config] = {"rows": rows, "summary": _summarize(rows),
                           "mean_gen_seconds": round(sum(gen_seconds) / max(1, len(gen_seconds)), 2),
                           "build_seconds": round(time.perf_counter() - started, 1)}
        _write_manifest(out_dir, config, corpus, eval_file, today, generator_model or env.get("LLM_MODEL", "deepseek-flash"))
    payload = {"meta": {"corpus": str(corpus_dir), "eval": str(eval_file), "today": today, "n": len(items_all)},
               "results": {config: results[config] for config in configs}}
    (out_dir / f"track_b__{corpus.build_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if row["answerable"] and isinstance(row.get("cites_evidence"), bool)]
    traps = [row for row in rows if not row["answerable"]]
    out: dict[str, Any] = {"n": len(rows)}
    if answerable:
        n = len(answerable)
        out.update({
            "answered": n,
            "cites": round(sum(r["cites_evidence"] for r in answerable) / n, 4),
            "numeric_fidelity": round(sum(r["numeric_fidelity"] for r in answerable) / n, 4),
            "grounded": round(sum(r["grounded"] for r in answerable) / n, 4),
            "refused": round(sum(r["refused"] for r in answerable) / n, 4),
        })
    if traps:
        out["traps"] = len(traps)
        if any("answered_on_trap" in r for r in traps):
            out["answered_on_trap"] = round(sum(1 for r in traps if r.get("answered_on_trap")) / len(traps), 4)
        if any("correct_abstain" in r for r in traps):
            out["correct_abstain"] = round(sum(1 for r in traps if r.get("correct_abstain")) / len(traps), 4)
    return out


def _write_manifest(out_dir: Path, config: str, corpus, eval_file: Path, today: str, model: str) -> None:
    is_pipeline = config in ("B3", "B4")
    snap = ((corpus.manifest.get("config") or {}).get("snapshot") or {})
    (BenchmarkManifest(track="B", system=config, eval_file=eval_file, corpus_build=corpus.root)
     .with_raw_snapshot(Path(corpus.manifest.get("crawl_root", ".")), int(snap.get("records") or 0), str(snap.get("sha256_of_hashes") or "crawl-root"))
     .with_corpus_construction(strategy="structured_evidence_blocks" if is_pipeline else "clean_recursive",
                               title_prefix=not is_pipeline, attachment_extraction="pipeline" if is_pipeline else "rawdump_plain",
                               version_rules=is_pipeline, applicability_rules=is_pipeline,
                               llm_validated_fields=(config == "B4"))
     .with_retrieval(retriever={"B0": "dense", "B1": "hybrid_rrf", "B2": "hybrid_rrf_cross_encoder",
                                "B3": "bm25_query_ir", "B4": "bm25_query_ir_llm_validated"}[config],
                     dense_model="" if is_pipeline else DENSE_MODEL, bm25=not (config == "B0"),
                     rrf_k=60 if config in ("B1", "B2") else None,
                     rerank_model=RERANK_ALIAS if config == "B2" else "",
                     rerank_top_k=8 if config == "B2" else None,
                     query_ir=is_pipeline, evidence_pack_rules=is_pipeline)
     .with_evidence_pack(limit=8, query_ir=is_pipeline, version_rules=is_pipeline, decision_layer=is_pipeline)
     .with_generation(model=model, prompt_version="oracle-pack-v1", temperature=0.0, max_tokens=800)
     .with_eval(verified_only=True, today=today)
     ).save(out_dir / f"manifest_{config}.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.track_b", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--eval", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "verified_eval.jsonl")
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--today", default="")
    parser.add_argument("--model", default="", help="override api generator model")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in configs if c not in CONFIGS]
    if unknown:
        raise SystemExit(f"unknown configs {unknown}")
    payload = run_track_b(args.corpus, args.eval, configs, today=args.today, generator_model=args.model, out_dir=args.out)
    for config, entry in payload["results"].items():
        s = entry["summary"]
        print(f"[track_b] {config}: {json.dumps(s, ensure_ascii=False)} gen={entry['mean_gen_seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
