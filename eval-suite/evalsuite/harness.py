"""Ablation of the evidence infrastructure on the crawled-corpus eval set.

Configurations (each adds one layer):

``raw``       crawler ``_text`` of every page plus flat attachment text, cut
              into 500-char windows — what a naive "crawl → chunk" pipeline sees.
``clean``     structure-preserving evidence blocks of every parsed document,
              including duplicates, listings and shells.
``active``    only active documents (boilerplate pages and duplicates removed).
``active_ir`` ``active`` + Query-IR applicability filtering and version-aware
              ordering (superseded editions behind the latest one).

All configurations use the same BM25 so the differences measure parsing,
cleaning and evidence metadata, not the retriever.  Metrics:

* doc / anchor Hit@k and anchor MRR@10 (an anchor hit is a unit of a gold
  document — or a duplicate of it — that contains the gold quote);
* ``wrong_edition@1``: the top unit is another edition of a gold document's
  series; ``inapplicable@1``: the top unit's document mismatches the question's
  term/audience/cohort;
* evidence-pack metrics on the top 8 units: answerable questions whose gold
  anchor is among *usable* evidence, false abstentions, and correct
  abstentions on unanswerable questions.

On top of the headline metrics, every answerable row carries additive
candidate-coverage diagnostics (``gold_in_corpus``, ``gold_in_bm25@50``,
``gold_in_candidate@50``, ``rerank_recovery@1``, ``rerank_harm@10``,
``candidate_miss_reason``, ``pack_drop_reason``) that explain *where* a gold
answer was lost — never in the build, never in the lexical pool, filtered by
the applicability layer, crowded out of the pack.  They never feed a metric.


The eval file is JSONL: ``{"id", "question", "category", "gold": [{"doc_uid",
"quote"}], "today"?}``; ``gold == []`` marks an unanswerable question.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

from evalsuite import builder_root  # noqa: E402
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retrieve.rerank import MODEL_ALIASES, get_reranker, release  # noqa: E402
from retrieve.lexical import BM25Index, Corpus, Unit, ir_rerank  # noqa: E402
from evidence.store import match_key, read_jsonl  # noqa: E402
from evidence.query_ir import QueryIR, applicability_mismatches, parse_query  # noqa: E402
from export.arms import IR_CONFIGS, LLM_CONFIGS, RR_CONFIGS  # noqa: E402
# Stage 3A: index construction/loading is owned by runtime.indexing and the
# query path by runtime.engine — the harness is a metric layer on top of the
# very same code the online product service calls.
from runtime.indexing import DENSE_CONFIGS, load_index, load_known_conflicts  # noqa: E402,F401
from runtime.engine import CorpusEngine  # noqa: E402

CONFIGS = ("raw", "clean", "active", "active_ir", "active_ir_rr", "oracle_doc") + LLM_CONFIGS + DENSE_CONFIGS
KS = (1, 3, 5, 10)


def _oracle_merge(index: BM25Index, question: str, ranked: list[tuple[int, float]], gold_docs: set[str], corpus: Corpus) -> list[tuple[int, float]]:
    """Oracle document (P4): force-merge each gold doc's top-2 BM25 units into
    the candidate pool at their natural scores, then re-truncate. Downstream
    block selection, pack build and abstain logic stay untouched, so whatever
    still fails is document-internal structure/selection, not retrieval."""
    pool = dict(ranked)
    scores = index.scores(question)
    for uid in gold_docs:
        canonical = corpus.canonical(uid)
        gold_units = sorted(
            (i for i, u in enumerate(index.units) if corpus.canonical(u.doc_uid) == canonical),
            key=lambda i: scores[i], reverse=True,
        )[:2]
        for i in gold_units:
            pool.setdefault(i, float(scores[i]))
    return sorted(pool.items(), key=lambda pair: pair[1], reverse=True)[:10]


def quote_hit(text: str, quote: str) -> bool:
    key, hay = match_key(quote), match_key(text)
    if not key:
        return False
    if key in hay:
        return True
    # Raw windows can cut a quote in half; accept a window holding either half.
    return len(key) >= 24 and (key[:24] in hay or key[-24:] in hay)


# ------------------------------------------------------------ diagnostics
# Everything below is additive: these fields explain where a gold answer was
# lost but never feed a headline metric.

def _gold_in(units_doc_uids, corpus: Corpus, gold_docs: set[str]) -> bool:
    return any(corpus.canonical(uid) in gold_docs for uid in units_doc_uids)


def _miss_reason(corpus: Corpus, config: str, gold: list[dict[str, Any]], diag: dict[str, Any], ir: QueryIR) -> str:
    """Classify why no gold document reached the final top-k of ``config``."""
    if not diag["gold_in_corpus"]:
        return "not_in_corpus"
    if not diag["gold_in_bm25@50"]:
        return "not_in_top50_lexical"
    if config in IR_CONFIGS:
        kinds: set[str] = set()
        for g in gold:
            if g["doc_uid"] in corpus.documents:
                kinds.update(m["kind"] for m in applicability_mismatches(ir, corpus.doc_metadata(g["doc_uid"])))
        if "term" in kinds:
            return "term_filtered"
        if "audience" in kinds:
            return "audience_filtered"
        if diag.get("gold_in_candidate@50"):
            # It reached the IR candidate pool with no applicability mismatch,
            # so the loss happened in the final ordering (superseded demotion
            # or rerank reorder) — no signal pins it further.
            return "other"
    return "rank_low"


def _coverage_diagnostics(
    corpus: Corpus,
    index: BM25Index,
    item: dict[str, Any],
    question: str,
    config: str,
    gold: list[dict[str, Any]],
    gold_docs: set[str],
    today: date | None,
    ir: QueryIR,
    doc_rank: int | None,
    ir_candidates: list[tuple[int, float, dict[str, Any]]] | None,
    baseline_doc_rank: dict[str, int | None] | None,
) -> dict[str, Any]:
    """Candidate-coverage fields for one answerable question.

    ``gold_in_bm25@50`` probes the raw BM25 top-50 pool (``max_per_doc``
    disabled) with one extra retrieval; the main-flow ``k`` is untouched.
    ``gold_in_candidate@50`` re-runs the IR rerank at k=50 for ``active_ir``
    (for ``active_ir_rr`` the main flow already produces that pool).
    """
    diag: dict[str, Any] = {"gold_in_corpus": any(g["doc_uid"] in corpus.documents for g in gold)}
    pool = index.search(question, 50, scores=index.scores(question), max_per_doc=0)
    diag["gold_in_bm25@50"] = _gold_in((index.units[i].doc_uid for i, _ in pool), corpus, gold_docs)
    if config in IR_CONFIGS:
        candidates = ir_candidates if ir_candidates is not None else ir_rerank(corpus, index, question, k=50, today=today)
        diag["gold_in_candidate@50"] = _gold_in((index.units[i].doc_uid for i, _, _ in candidates), corpus, gold_docs)
        if config in RR_CONFIGS:
            diag["rerank_recovery@1"] = bool(diag["gold_in_candidate@50"]) and doc_rank == 1
            if baseline_doc_rank is not None:
                baseline = baseline_doc_rank.get(item.get("id"))
                # Harm = the same run's active_ir config had the gold doc in
                # its top-10 but the reranked ordering dropped it out again.
                diag["rerank_harm@10"] = baseline is not None and (doc_rank is None or doc_rank > 10)
    if doc_rank is None:
        diag["candidate_miss_reason"] = _miss_reason(corpus, config, gold, diag, ir)
    return diag


def _pack_drop_reason(corpus: Corpus, top_units: list[Unit], pack: dict[str, Any], gold_docs: set[str], quotes: dict[str, list[str]]) -> str | None:
    """Why a gold hit inside the top-8 window did not become usable evidence.

    ``None`` when the gold document never reached the window; otherwise one of
    ``blocking`` (its evidence carries a blocking conflict), ``dedup_merge``
    (an identical doc/page/text twin replaced it), ``coverage`` (the coverage
    selection spent the slots elsewhere) or ``other`` (the retrieved window of
    the gold document never contained the quote).
    """
    if not _gold_in((u.doc_uid for u in top_units), corpus, gold_docs):
        return None
    quote_evidence = [
        ev for ev in pack["evidence"]
        if corpus.canonical(ev["doc_id"]) in gold_docs and any(quote_hit(ev["text"], q) for q in quotes.get(corpus.canonical(ev["doc_id"]), []))
    ]
    if quote_evidence:
        # A quote-bearing item exists; gold_usable being false means every
        # such item carried a blocking conflict (term/audience/superseded).
        return "blocking"
    quote_units = [
        u for u in top_units
        if corpus.canonical(u.doc_uid) in gold_docs and any(quote_hit(u.text, q) for q in quotes.get(corpus.canonical(u.doc_uid), []))
    ]
    if not quote_units:
        return "other"
    evidence_keys = {(corpus.canonical(ev["doc_id"]), match_key(ev["text"])) for ev in pack["evidence"]}
    if any((corpus.canonical(u.doc_uid), match_key(u.text)) in evidence_keys for u in quote_units):
        return "dedup_merge"
    return "coverage"


def evaluate(engine: CorpusEngine, items: list[dict[str, Any]], default_today: date | None, baseline_doc_rank: dict[str, int | None] | None = None) -> dict[str, Any]:
    """Run the eval metrics over one runtime engine (Stage 3A single path).

    Retrieval and pack construction go through ``engine.retrieve`` /
    ``engine.build_pack`` — the same methods the online service calls.  Only
    gold comparison, candidate-coverage diagnostics and metric aggregation
    are eval-specific.
    """
    corpus = engine.corpus
    index = engine.index
    config = engine.config
    rows = []
    rerank_seconds: list[float] = []
    for item in items:
        today = date.fromisoformat(item["today"]) if item.get("today") else default_today
        question = item["question"]
        ir = parse_query(question, today=today)
        ranked = engine.retrieve(question, ir=ir, today=today, k=10)
        if config in RR_CONFIGS and engine.last_rerank_seconds is not None:
            rerank_seconds.append(engine.last_rerank_seconds)
        units = [index.units[i] for i, _ in ranked]
        gold = item.get("gold") or []
        gold_docs = {corpus.canonical(g["doc_uid"]) for g in gold}
        if config == "oracle_doc" and gold_docs:
            ranked = _oracle_merge(index, question, ranked, gold_docs, corpus)
            units = [index.units[i] for i, _ in ranked]
        quotes = defaultdict(list)
        for g in gold:
            quotes[corpus.canonical(g["doc_uid"])].append(g["quote"])
        doc_hits = [corpus.canonical(u.doc_uid) in gold_docs for u in units]
        anchor_hits = [corpus.canonical(u.doc_uid) in gold_docs and any(quote_hit(u.text, q) for q in quotes[corpus.canonical(u.doc_uid)]) for u in units]
        top = corpus.documents.get(corpus.canonical(units[0].doc_uid), {}) if units else {}
        gold_series = {corpus.documents.get(uid, {}).get("series_id") for uid in gold_docs} - {"", None}
        doc_rank = next((r for r, hit in enumerate(doc_hits, 1) if hit), None)
        anchor_rank = next((r for r, hit in enumerate(anchor_hits, 1) if hit), None)
        row = {
            # The eval schema uses ``id``.  Keep the failure explicit instead
            # of silently accepting the old question_id field.
            "id": item["id"],
            "category": item.get("category", ""),
            "answerable": bool(gold),
            "top": [{"doc_uid": u.doc_uid, "block_id": u.block_id, "title": corpus.documents.get(u.doc_uid, {}).get("title", "")[:60]} for u in units[:3]],
            "doc_rank": doc_rank,
            "anchor_rank": anchor_rank,
            "wrong_edition": bool(top) and top.get("series_id") in gold_series and top.get("doc_uid") not in gold_docs,
            "inapplicable": bool(top) and bool(applicability_mismatches(ir, top)) if top.get("terms") or top.get("audience") else False,
        }
        if config in ("active", "active_ir", "active_ir_rr") or config in LLM_CONFIGS:
            pack = engine.build_pack(question, ranked, today=today, ir=ir)
            usable = set(pack["usable_evidence_ids"])
            # The pack dedups/merges hits, so match evidence by its own doc and text, not by position.
            usable_ev = [(corpus.canonical(ev["doc_id"]), ev["text"]) for ev in pack["evidence"] if ev["evidence_id"] in usable]
            row["abstain"] = bool(pack["abstain"])
            row["gold_usable"] = any(uid in gold_docs and any(quote_hit(text, q) for q in quotes[uid]) for uid, text in usable_ev)
            row["blocked"] = sum(1 for ev in pack["evidence"] if ev.get("blocking"))
            row["pack_conflict_status"] = bool(pack.get("fact_conflicts"))
            if gold and not row["gold_usable"]:
                reason = _pack_drop_reason(corpus, units[:8], pack, gold_docs, quotes)
                if reason:
                    row["pack_drop_reason"] = reason
        if gold:
            row.update(_coverage_diagnostics(
                corpus, index, item, question, config, gold, gold_docs, today, ir, doc_rank,
                # ir_candidates=None: the diagnostics recompute the k=50 IR pool
                # themselves — deterministic, so identical to the main flow's.
                None, baseline_doc_rank,
            ))
            # Neutral (quote-independent) recall, P6: every atomic fact of the
            # gold quotes (dates / numbers-with-units / course codes) appears
            # in some retrieved top-10 unit of the gold documents.  Reported
            # alongside the strict quote anchors so conclusions do not hinge
            # on continuous-string matching alone.
            from evidence.pack import _fact_slots

            wanted: set[str] = set()
            for g in gold:
                for values in _fact_slots(g["quote"]).values():
                    wanted |= values
            if wanted:
                pool: set[str] = set()
                for u in units:
                    if corpus.canonical(u.doc_uid) in gold_docs:
                        for values in _fact_slots(u.text).values():
                            pool |= values
                row["gold_fact_hit@10"] = wanted <= pool
        rows.append(row)
    result = {"config": config, "rows": rows, "summary": summarize(rows)}
    if rerank_seconds:
        ordered = sorted(rerank_seconds)
        result["rerank"] = {
            "model": getattr(engine.reranker, "name", ""),
            "questions": len(rerank_seconds),
            "mean_ms": round(1000 * sum(rerank_seconds) / len(rerank_seconds), 1),
            "p95_ms": round(1000 * ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
        }
    return result


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def block(subset: list[dict[str, Any]]) -> dict[str, Any]:
        answerable = [r for r in subset if r["answerable"]]
        result: dict[str, Any] = {"n": len(subset), "answerable": len(answerable)}
        for k in KS:
            result[f"doc@{k}"] = _rate([r["doc_rank"] is not None and r["doc_rank"] <= k for r in answerable])
            result[f"anchor@{k}"] = _rate([r["anchor_rank"] is not None and r["anchor_rank"] <= k for r in answerable])
        result["anchor_mrr@10"] = round(sum(1 / r["anchor_rank"] for r in answerable if r["anchor_rank"]) / max(1, len(answerable)), 4)
        result["wrong_edition@1"] = _rate([r["wrong_edition"] for r in answerable])
        result["inapplicable@1"] = _rate([r["inapplicable"] for r in subset])
        if subset and "abstain" in subset[0]:
            result["gold_usable@8"] = _rate([r["gold_usable"] for r in answerable])
            result["false_abstain"] = _rate([r["abstain"] for r in answerable])
            result["correct_abstain"] = _rate([r["abstain"] for r in subset if not r["answerable"]])
        # Additive candidate-coverage rates; absent when the config does not
        # produce the field (non-IR configs, or non-rr for the rerank ones).
        for field in ("gold_in_candidate@50", "rerank_recovery@1", "rerank_harm@10"):
            values = [r[field] for r in answerable if field in r]
            if values:
                result[field] = _rate(values)
        # P6 neutral recall: atomic-fact coverage of the gold quotes.
        fact_hits = [r["gold_fact_hit@10"] for r in answerable if "gold_fact_hit@10" in r]
        if fact_hits:
            result["gold_fact_hit@10"] = _rate(fact_hits)
        return result

    by_category = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)
    return {"all": block(rows), "by_category": {name: block(group) for name, group in sorted(by_category.items())}}


def _diagnostic_rows(result: dict[str, Any]) -> list[str]:
    """One compact markdown line per answerable question of one config."""
    mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
    lines = []
    for row in result["rows"]:
        if not row["answerable"]:
            continue
        cand = "–" if "gold_in_candidate@50" not in row else mark(row["gold_in_candidate@50"])
        if "gold_usable" in row:
            pack = "ok" if row["gold_usable"] else row.get("pack_drop_reason") or "not_in_top8"
        else:
            pack = "–"
        abstain = "–" if "abstain" not in row else ("abstain" if row["abstain"] else "answer")
        lines.append(
            f"| {row['id']} | {mark(row.get('gold_in_corpus'))} | {mark(row.get('gold_in_bm25@50'))} | {cand} | "
            f"{row['doc_rank'] if row['doc_rank'] is not None else '–'} | {pack} | {abstain} |"
        )
    return lines


def render(results: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    cols = ["doc@1", "doc@5", "anchor@1", "anchor@5", "anchor@10", "anchor_mrr@10", "gold_fact_hit@10", "wrong_edition@1", "inapplicable@1", "gold_usable@8", "false_abstain", "correct_abstain"]
    fmt = lambda v: "–" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))  # noqa: E731
    lines = [f"# Corpus retrieval ablation — build `{meta['build_id']}`, eval `{meta['eval']}` ({meta['n']} questions)", ""]
    lines += ["| config | " + " | ".join(cols) + " |", "| --- | " + " | ".join("---:" for _ in cols) + " |"]
    for result in results:
        lines.append(f"| {result['config']} | " + " | ".join(fmt(result["summary"]["all"].get(c)) for c in cols) + " |")
    categories = sorted({c for r in results for c in r["summary"]["by_category"]})
    for category in categories:
        lines += ["", f"## {category}", "", "| config | n | " + " | ".join(cols) + " |", "| --- | ---: | " + " | ".join("---:" for _ in cols) + " |"]
        for result in results:
            summary = result["summary"]["by_category"].get(category)
            if summary:
                lines.append(f"| {result['config']} | {summary['n']} | " + " | ".join(fmt(summary.get(c)) for c in cols) + " |")
    # Additive per-question minimal diagnostics: where each gold answer was
    # lost, one line per answerable question, per config.
    lines += ["", "## 逐题最小诊断", ""]
    for result in results:
        lines += [f"### {result['config']}", "", "| id | in_corpus | bm25@50 | cand@50 | rank(doc) | pack | abstain决策 |", "| --- | --- | --- | --- | ---: | --- | --- |"]
        body = _diagnostic_rows(result)
        lines += body if body else ["| – | – | – | – | – | – | – |"]
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=None, help="build directory (default: data/corpus/LATEST)")
    parser.add_argument("--eval", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "eval.jsonl")
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--today", default="", help="reference date for relative questions (YYYY-MM-DD)")
    parser.add_argument("--include-unverified", action="store_true", help="also evaluate candidates not yet verified by a human")
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--rerank-model", default="bge-base", help=f"reranker alias for active_ir_rr, one of {sorted(MODEL_ALIASES)}")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "results")
    args = parser.parse_args(argv)

    configs = args.configs.split(",")
    reranker = None
    if any(c in RR_CONFIGS for c in configs):
        reranker = get_reranker(args.rerank_model)  # warm load; engines share the singleton
        print(f"[rerank] {args.rerank_model} loaded", file=sys.stderr)

    corpus = Corpus(args.corpus) if args.corpus else Corpus.latest(builder_root() / "data" / "corpus")
    items = [row for row in read_jsonl(args.eval) if args.include_unverified or row.get("verified")]
    invalid = []
    for row in items:
        if not row.get("id") or "question_id" in row:
            invalid.append(f"missing id or legacy question_id: {row.get('id') or row.get('question_id')}")
        if not isinstance(row.get("gold"), list):
            invalid.append(f"gold must be a list: {row.get('id', '<unknown>')}")
    if invalid:
        raise SystemExit("invalid eval schema:\n" + "\n".join(invalid[:20]))
    if not items:
        raise SystemExit("no eval items (use --include-unverified for candidates)")
    today = date.fromisoformat(args.today) if args.today else None
    results = []
    doc_ranks: dict[str, dict[str, int | None]] = {}
    for config in configs:
        if config == "oracle_doc":
            # eval-only arm (gold-merged retrieval); index and pack still come
            # from the runtime's builders.
            engine = CorpusEngine(corpus, config, load_index(corpus, config, args.rebuild_index), known_conflicts=load_known_conflicts(corpus.root))
        else:
            engine = CorpusEngine.load(corpus.root, config=config, rebuild_index=args.rebuild_index, rerank_model=args.rerank_model)
        started = time.perf_counter()
        result = evaluate(engine, items, today, baseline_doc_rank=doc_ranks.get("active_ir"))
        result["seconds"] = round(time.perf_counter() - started, 1)
        results.append(result)
        doc_ranks[config] = {row["id"]: row["doc_rank"] for row in result["rows"]}
        print(f"[eval] {config}: {json.dumps(result['summary']['all'], ensure_ascii=False)}", file=sys.stderr)
    release()
    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"{corpus.build_id}__{args.eval.stem}"
    meta = {"build_id": corpus.build_id, "eval": args.eval.name, "n": len(items), "verified_only": not args.include_unverified,
            "rerank_model": args.rerank_model if reranker is not None else "",
            "category_counts": dict(Counter(row.get("category", "") for row in items))}
    (args.out / f"{stem}.json").write_text(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / f"{stem}.md").write_text(render(results, meta), encoding="utf-8")
    print(render(results, meta))


if __name__ == "__main__":
    main()
