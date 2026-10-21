"""ViDoRe v3 public benchmark adapter (P7.4 外部可迁移性).

域内题集证明工程贡献；ViDoRe v3 证明检索组件的外部可迁移性——两类结果
**分开报告、分开解释**（公共 benchmark 不含本项目的版本/受众/拒答维度）。

协议：corpus = 每页一个检索单元（markdown 全文，不做任何本项目式语料治理
——这正是"可迁移性"的诚实边界）；BM25 用本项目 ``retrieve.lexical`` 实现；
指标 nDCG@10 / Recall@10（qrels 的 graded score 二值化：>0 为相关，与
vidore 官方一致）。

用法::

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m bench.vidore \\
        --data data/benchmarks/vidore-v3 --k 10
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

__all__ = ["run_vidore"]


def _ndcg(ranked_ids: list[str], rels: dict[str, float], k: int) -> float:
    dcg = sum(rels.get(doc_id, 0.0) / (1 << (rank - 1)) ** 0.5 for rank, doc_id in enumerate(ranked_ids[:k], 1))
    ideal = sorted(rels.values(), reverse=True)[:k]
    idcg = sum(value / (1 << rank) ** 0.5 for rank, value in enumerate(ideal, 0))
    return dcg / idcg if idcg > 0 else 0.0


# ---------------------------------------------------------------- governed units


def _split_table_rows(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def markdown_units(markdown: str, doc_uid: str) -> list[dict[str, str]]:
    """Governed units from one ViDoRe markdown page (our transferable ops).

    Pipe tables go through ``structure.extract.table_blocks`` — the header
    self-containment that made table rows citable evidence in-domain.  Plain
    text becomes paragraph blocks (consecutive non-empty lines).  Cross-page
    template demotion is applied by the caller (needs corpus-level counts).
    """
    import re

    from structure.extract import normalize_text, table_blocks

    units: list[dict[str, str]] = []
    paragraph: list[str] = []
    table_buffer: list[list[str]] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = normalize_text(" ".join(paragraph))
            if text:
                units.append({"doc_uid": doc_uid, "block_id": f"{doc_uid}#u{len(units):04d}", "text": text, "kind": "paragraph"})
            paragraph.clear()

    def flush_table() -> None:
        if not table_buffer:
            return
        width = max(len(row) for row in table_buffer)
        grid = [row + [""] * (width - len(row)) for row in table_buffer]
        # OCR tables open with caption/unit rows before the real header; find
        # the most header-like of the first rows and pass it explicitly (the
        # auto-detect only ever considers row 0).
        from structure.extract import _looks_like_header

        header: list[str] | None = None
        for i in range(min(3, len(grid) - 1)):
            if _looks_like_header(grid[i], grid[i + 1:]):
                header = grid[i]
                grid = grid[:i] + grid[i + 1:]
                break
        for block in table_blocks(grid, f"p{len(units)}", header=header):
            text = normalize_text(block.text)
            if text:
                units.append({"doc_uid": doc_uid, "block_id": f"{doc_uid}#u{len(units):04d}", "text": text,
                              "kind": block.kind})
        table_buffer.clear()

    for line in str(markdown or "").splitlines():
        if _is_table_line(line):
            cells = _split_table_rows(line)
            if all(re.fullmatch(r":?-{2,}:?", cell or "-") for cell in cells):
                continue  # markdown alignment row, not content
            table_buffer.append(cells)
            continue
        if line.strip():
            if table_buffer:
                flush_table()
            paragraph.append(line.strip())
        else:
            flush_paragraph()
            if table_buffer:
                flush_table()
    flush_paragraph()
    flush_table()
    return units


def governed_units(corpus_rows: list[dict], *, template_min_pages: int = 5) -> list[dict[str, str]]:
    """All units + cross-page template demotion (the pipeline's P1-2 rule)."""
    from collections import Counter

    from evidence.store import match_key

    by_page = [markdown_units(str(row.get("markdown") or ""), str(row["corpus_id"])) for row in corpus_rows]
    key_counts: Counter[str] = Counter()
    for units in by_page:
        for unit in units:
            if unit["kind"] != "table_row":
                key_counts[f"{unit['kind']}:{match_key(unit['text'])[:80]}"] += 1
    out: list[dict[str, str]] = []
    demoted = 0
    for units in by_page:
        for unit in units:
            if unit["kind"] != "table_row" and key_counts[f"{unit['kind']}:{match_key(unit['text'])[:80]}"] >= template_min_pages:
                demoted += 1
                continue
            out.append(unit)
    return out


def run_vidore(data_dir: Path, k: int = 10, languages: tuple[str, ...] | None = None, governed: bool = False, pooling: str = "unit", hybrid: bool = False) -> dict:
    import pyarrow.parquet as pq
    from retrieve.lexical import BM25Index, Unit

    data_dir = Path(data_dir)
    corpus = pq.read_table(data_dir / "corpus" / "test-00000-of-00001.parquet").to_pylist()
    queries = pq.read_table(data_dir / "queries" / "test-00000-of-00001.parquet").to_pylist()
    qrels = pq.read_table(data_dir / "qrels" / "test-00000-of-00001.parquet").to_pylist()

    if governed:
        rows = governed_units(corpus)
        units = [Unit(doc_uid=r["doc_uid"], text=r["text"], block_id=r["block_id"]) for r in rows]
    elif hybrid:
        # whole-page unit PLUS its self-contained table rows: prose keeps
        # page-level term accumulation, table-lookup queries gain row units.
        units = []
        for row in corpus:
            uid = str(row["corpus_id"])
            units.append(Unit(doc_uid=uid, text=str(row["markdown"] or "")[:20000], block_id=f"{uid}#page"))
            units.extend(Unit(doc_uid=uid, text=u["text"], block_id=u["block_id"])
                         for u in markdown_units(str(row["markdown"] or ""), uid) if u["kind"] == "table_row")
    else:
        units = [Unit(doc_uid=str(row["corpus_id"]), text=str(row["markdown"] or "")[:20000], block_id=f"vidore-p{row['corpus_id']}") for row in corpus]
    index = BM25Index(units)

    rels: dict[str, dict[str, float]] = defaultdict(dict)
    for row in qrels:
        if (row.get("score") or 0) > 0:
            rels[str(row["query_id"])][str(row["corpus_id"])] = float(row["score"])

    by_language: dict[str, list[float]] = defaultdict(list)
    recalls: list[float] = []
    recalls_by_language: dict[str, list[float]] = defaultdict(list)
    n_evaluated = 0
    for row in queries:
        qid = str(row["query_id"])
        if qid not in rels:
            continue
        language = str(row.get("language") or "?")
        if languages and language not in languages:
            continue
        if pooling == "page_sum":
            scores = index.scores(str(row["query"]))
            page_scores: dict[str, float] = defaultdict(float)
            for i, unit in enumerate(index.units):
                if scores[i] > 0:
                    page_scores[unit.doc_uid] += float(scores[i])
            ranked_ids = [doc_id for doc_id, _ in sorted(page_scores.items(), key=lambda kv: -kv[1])[:k]]
        else:
            ranked = index.search(str(row["query"]), k=k, max_per_doc=1)
            ranked_ids = [index.units[i].doc_uid for i, _ in ranked]
        rel = rels[qid]
        by_language[language].append(_ndcg(ranked_ids, rel, k))
        recalls_by_language[language].append(len(set(ranked_ids) & set(rel)) / len(rel))
        recalls.append(len(set(ranked_ids) & set(rel)) / len(rel))
        n_evaluated += 1

    overall = sum(v for values in by_language.values() for v in values) / max(1, n_evaluated)
    n_pages = len({u.doc_uid for u in units})
    return {
        "k": k,
        "governed": governed,
        "hybrid": hybrid,
        "pooling": pooling,
        "n_queries": n_evaluated,
        "n_pages": n_pages,
        "n_units": len(units),
        "ndcg@10": round(overall, 4),
        "recall@10": round(sum(recalls) / max(1, len(recalls)), 4),
        "by_language": {language: {"ndcg@10": round(sum(values) / len(values), 4),
                                   "recall@10": round(sum(recalls_by_language[language]) / max(1, len(recalls_by_language[language])), 4)}
                        for language, values in sorted(by_language.items()) if values},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.vidore", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "benchmarks" / "vidore-v3")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--governed", action="store_true", help="our structure ops on the markdown (table-row self-containment + template demotion) instead of whole-page units")
    parser.add_argument("--hybrid", action="store_true", help="whole-page units plus self-contained table rows (additive, not replacement)")
    parser.add_argument("--pooling", default="unit", choices=("unit", "page_sum"), help="page_sum = SumP aggregation of block scores (block corpora at page-level qrels)")
    args = parser.parse_args(argv)
    result = run_vidore(args.data, args.k, governed=args.governed, pooling=args.pooling, hybrid=args.hybrid)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    tag = ("hybrid" if args.hybrid else "governed" if args.governed else "raw_pages") + (f"_{args.pooling}" if args.pooling != "unit" else "")
    (args.data / f"vidore_bm25_{tag}_k{args.k}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
