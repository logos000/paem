"""Track A runner for external baselines A0–A3 (P7.1/P7.2).

固定检索器、只换语料构建（P7 文档 §2）：A0–A3 是"外部方法族"的代表性基线，
A4–A6 由 eval.harness 的既有臂承担（active_ir / active_llm_metadata_ir /
active_llm_validated_ir）。本模块只负责外部基线行，输出与 harness 同形的
逐题行 + 汇总，并为每个配置落一份 BenchmarkManifest。

诚实边界（写进 manifest，也写进结果表）：

- 基线没有版本链/受众规则 → 不产生 wrong_edition/inapplicable/abstain
  信号；"永远作答"本身是基线属性，在汇总里显式标注 ``decision_layer: false``。
- A2/A3 的 chunk 携带文档标题前缀（标准做法），但不得携带任何本项目
  生成的元数据或检索辅助文本。
- 附件文本来自 rawdump 朴素抽取（无结构恢复）；exact-duplicate 附件沿
  build 的去重结果移除（任何标准管线都有的最基本去重）。

用法::

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m bench.track_a \\
        --corpus data/corpus/<build> --eval data/benchmarks/corpus-eval/verified_eval.jsonl \\
        --configs A0,A1,A2,A3 --today 2026-10-14
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.chunkers import build_baseline_units  # noqa: E402
from bench.manifest import BenchmarkManifest, schema_json  # noqa: E402
from evidence.store import read_jsonl  # noqa: E402
from eval.harness import quote_hit  # noqa: E402
from retrieve.lexical import BM25Index, Corpus, Unit  # noqa: E402

__all__ = ["run_track_a", "CONFIGS"]

CONFIGS = {
    "A0": {"strategy": "raw_window", "retriever": "bm25", "title_prefix": False},
    "A1": {"strategy": "clean_recursive", "retriever": "bm25", "title_prefix": False},
    "A2": {"strategy": "clean_recursive", "retriever": "dense", "title_prefix": True},
    "A3": {"strategy": "clean_recursive", "retriever": "hybrid_rr", "title_prefix": True},
}
DENSE_MODEL = "models/embed/bge-small-zh"
RERANK_ALIAS = "bge-v2-m3"


def _units_with_title(units: list[dict[str, Any]], corpus: Corpus) -> list[Unit]:
    out = []
    for unit in units:
        title = corpus.documents.get(unit["doc_uid"], {}).get("title", "")
        text = unit["text"]
        index_text = f"{title}\n{text}" if title else text
        out.append(Unit(doc_uid=unit["doc_uid"], text=text, block_id=unit["block_id"], index_text=index_text))
    return out


def _search(config: str, index: BM25Index, question: str, dense_matrix=None):
    if CONFIGS[config]["retriever"] == "bm25":
        return index.search(question, 50)
    import retrieve.dense as dense

    qvec = dense.embed_query(question, DENSE_MODEL)
    if CONFIGS[config]["retriever"] == "dense":
        return dense.topk(dense_matrix, qvec, 50)
    return dense.rrf_fuse([index.search(question, 50), dense.topk(dense_matrix, qvec, 50)])[:50]


def run_track_a(corpus_dir: Path, eval_file: Path, configs: list[str], *, today: str = "", out_dir: Path | None = None) -> dict[str, Any]:
    corpus = Corpus(corpus_dir)
    items = [row for row in read_jsonl(eval_file) if row.get("verified")]
    if not items:
        raise SystemExit("no verified items in eval file")
    out_dir = out_dir or corpus_dir / "_bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "benchmark_manifest.schema.json").write_text(schema_json(), encoding="utf-8")

    results: dict[str, Any] = {}
    dense_matrix = None
    reranker = None
    for config in configs:
        spec = CONFIGS[config]
        started = time.perf_counter()
        units_raw = build_baseline_units(corpus_dir, spec["strategy"])
        units = _units_with_title(units_raw, corpus) if spec["title_prefix"] else [Unit(doc_uid=u["doc_uid"], text=u["text"], block_id=u["block_id"]) for u in units_raw]
        index = BM25Index(units)
        if spec["retriever"] in ("dense", "hybrid_rr"):
            import retrieve.dense as dense

            dense_matrix = dense.embed_units([u.index_text or u.text for u in units], DENSE_MODEL, out_dir / f"{config}_dense.npy")
        build_seconds = time.perf_counter() - started

        rows = []
        search_seconds: list[float] = []
        for item in items:
            question = item["question"]
            gold = item.get("gold") or []
            gold_docs = {corpus.canonical(g["doc_uid"]) for g in gold}
            quotes = defaultdict(list)
            for g in gold:
                quotes[corpus.canonical(g["doc_uid"])].append(g["quote"])
            t0 = time.perf_counter()
            ranked = _search(config, index, question, dense_matrix)
            if spec["retriever"] == "hybrid_rr":
                # A3: cross-encoder over the hybrid top-50 (frozen model family
                # with A4-A6, no IR hard rules — baselines get none of ours).
                if reranker is None:
                    from retrieve.rerank import get_reranker

                    reranker = get_reranker(RERANK_ALIAS)
                pairs = [(question, index.units[i].index_text or index.units[i].text) for i, _ in ranked]
                scores = reranker.score(pairs)
                ranked = [pair for _, pair in sorted(zip(scores, ranked), key=lambda p: -p[0])][:10]
            else:
                ranked = ranked[:10]
            search_seconds.append(time.perf_counter() - t0)
            units_top = [index.units[i] for i, _ in ranked]
            doc_hits = [corpus.canonical(u.doc_uid) in gold_docs for u in units_top]
            anchor_hits = [corpus.canonical(u.doc_uid) in gold_docs and any(quote_hit(u.text, q) for q in quotes[corpus.canonical(u.doc_uid)]) for u in units_top]
            rows.append({
                "id": item["id"],
                "answerable": bool(gold),
                "doc_rank": next((r for r, hit in enumerate(doc_hits, 1) if hit), None),
                "anchor_rank": next((r for r, hit in enumerate(anchor_hits, 1) if hit), None),
                "gold_usable": any(anchor_hits[:8]),
            })
        results[config] = {"rows": rows, "build_seconds": round(build_seconds, 1),
                           "mean_search_ms": round(1000 * sum(search_seconds) / max(1, len(search_seconds)), 1),
                           "n_units": len(units), "summary": _summarize(rows)}
        _write_manifest(out_dir, config, spec, corpus, eval_file, today, len(units), build_seconds)
    payload = {"meta": {"corpus": str(corpus_dir), "eval": str(eval_file), "today": today, "n": len(items)},
               "results": {config: results[config] for config in configs}}
    (out_dir / f"track_a__{corpus.build_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if row["answerable"]]
    n = len(rows)
    out: dict[str, Any] = {"n": n, "answerable": len(answerable), "decision_layer": False}
    for key in ("doc", "anchor"):
        for k in (1, 5, 10):
            hits = sum(1 for row in answerable if row[f"{key}_rank"] is not None and row[f"{key}_rank"] <= k)
            out[f"{key}@{k}"] = round(hits / max(1, len(answerable)), 4)
    out["gold_usable@8"] = round(sum(1 for row in answerable if row["gold_usable"]) / max(1, len(answerable)), 4)
    return out


def _write_manifest(out_dir: Path, config: str, spec: dict[str, Any], corpus: Corpus, eval_file: Path, today: str, n_units: int, build_seconds: float) -> None:
    snap = ((corpus.manifest.get("config") or {}).get("snapshot") or {})
    manifest = (BenchmarkManifest(track="A", system=config, eval_file=eval_file, corpus_build=corpus.root)
                .with_raw_snapshot(Path(corpus.manifest.get("crawl_root", ".")), int(snap.get("records") or 0), str(snap.get("sha256_of_hashes") or "crawl-root"))
                .with_corpus_construction(strategy=spec["strategy"], chunker=spec["strategy"],
                                          window_size=500 if spec["strategy"] == "raw_window" else None,
                                          chunk_size=512 if spec["strategy"] == "clean_recursive" else None,
                                          title_prefix=spec["title_prefix"],
                                          attachment_extraction="rawdump_plain",
                                          duplicate_attachments="removed_via_build_dedup",
                                          n_units=n_units, build_seconds=build_seconds)
                .with_retrieval(retriever=spec["retriever"], bm25=spec["retriever"] in ("bm25", "hybrid_rr"),
                                dense_model=DENSE_MODEL if spec["retriever"] in ("dense", "hybrid_rr") else "",
                                dense_top_k=50, rrf_k=60,
                                rerank_model=RERANK_ALIAS if spec["retriever"] == "hybrid_rr" else "",
                                rerank_top_k=10, metadata_filter="none", query_ir=False, version_rules=False)
                .with_evidence_pack(limit=8, query_ir=False, version_rules=False, decision_layer=False)
                .with_generation(model="", prompt_version="")
                .with_eval(verified_only=True, today=today))
    manifest.save(out_dir / f"manifest_{config}.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.track_a", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--eval", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "verified_eval.jsonl")
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--today", default="")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in configs if c not in CONFIGS]
    if unknown:
        raise SystemExit(f"unknown configs {unknown}; A4-A6 请用 eval.harness 的对应臂")
    payload = run_track_a(args.corpus, args.eval, configs, today=args.today, out_dir=args.out)
    for config, entry in payload["results"].items():
        summary = entry["summary"]
        print(f"[track_a] {config}: units={entry['n_units']} anchor@1={summary['anchor@1']} "
              f"doc@1={summary['doc@1']} gold_usable@8={summary['gold_usable@8']} "
              f"build={entry['build_seconds']}s search={entry['mean_search_ms']}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
