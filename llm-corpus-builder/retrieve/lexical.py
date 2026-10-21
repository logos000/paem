"""Block-level lexical retrieval over a corpus build, with Query-IR filtering.

This is the retrieval half of the evidence infrastructure evaluation: a sparse
BM25 index over evidence blocks (or raw text windows for the baseline), plus
an applicability/version-aware reranker that uses the same Query IR and
document metadata as :func:`evidence.pack.build_evidence_pack`.

The BM25 weights are precomputed into a CSC matrix so a query is a column
slice and a sum; a full 250k-block corpus scores a question in milliseconds.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date
import json
import pickle
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import numpy as np

from evidence.store import match_key, read_jsonl
from evidence.query_ir import QueryIR, applicability_mismatches, parse_query

_TOKEN_RE = re.compile(r"[a-z0-9一-鿿]")
# "ECE 220" and "ECE220" tokenize differently and "220" alone is common; a
# compound token makes the course code itself a rare, decisive term.
_COURSE_RE = re.compile(r"(?<![a-z0-9])([a-z]{2,5})\s?(\d{3})(?![0-9])")
# Question scaffolding: frequent in essays and news, never the topic.
QUERY_STOP = frozenset(
    "的 了 吗 呢 和 与 及 或 是 在 中 有 为 对 请 能否 可以 是否 什么 是什么 什么样 哪些 哪个 哪天 哪里 多少 几 如何 怎么 怎样 "
    "分别 分别是 以及 还是 公开 时间 什么时间 何时 定于 举行 负责 区间 判断 适用 直接 办理 名称 记录"
    " what which who when where how is are the of in for and a an to do does can".split()
)


def tokens(text: str) -> list[str]:
    """jieba tokens (lowercased) plus compound course-code tokens."""
    import jieba

    lowered = text.lower()
    words = [word for word in jieba.lcut(lowered) if word.strip() and _TOKEN_RE.search(word)]
    present = set(words)
    words += [code for code in {a + b for a, b in _COURSE_RE.findall(lowered)} if code not in present]
    return words


def query_tokens(text: str) -> list[str]:
    words = [word for word in tokens(text) if word not in QUERY_STOP]
    return words or tokens(text)


def _tokenize_batch(texts: list[str]) -> list[list[str]]:
    import jieba

    jieba.setLogLevel(60)
    return [tokens(text) for text in texts]


def tokenize_all(texts: list[str], workers: int = 6, batch: int = 4000) -> list[list[str]]:
    chunks = [texts[i:i + batch] for i in range(0, len(texts), batch)]
    if workers <= 1 or len(chunks) == 1:
        return [row for chunk in chunks for row in _tokenize_batch(chunk)]
    with ProcessPoolExecutor(workers) as pool:
        return [row for result in pool.map(_tokenize_batch, chunks) for row in result]


@dataclass
class Unit:
    """One retrievable unit: an evidence block or a raw text window."""

    doc_uid: str
    text: str
    block_id: str = ""
    index_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class BM25Index:
    def __init__(self, units: list[Unit], *, k1: float = 1.2, b: float = 0.75, workers: int = 6):
        from scipy import sparse

        self.units = units
        docs = tokenize_all([unit.index_text or unit.text for unit in units], workers=workers)
        vocab: dict[str, int] = {}
        rows, cols, vals = [], [], []
        lengths = np.zeros(len(docs), dtype=np.float32)
        for row, words in enumerate(docs):
            lengths[row] = len(words)
            counts: dict[int, int] = {}
            for word in words:
                col = vocab.setdefault(word, len(vocab))
                counts[col] = counts.get(col, 0) + 1
            rows.extend([row] * len(counts))
            cols.extend(counts.keys())
            vals.extend(counts.values())
        tf = sparse.csr_matrix((np.asarray(vals, dtype=np.float32), (rows, cols)), shape=(len(docs), len(vocab)))
        df = np.bincount(tf.indices, minlength=len(vocab)).astype(np.float32)
        n = max(1, len(docs))
        idf = np.log1p((n - df + 0.5) / (df + 0.5)).astype(np.float32)
        avg = float(lengths.mean()) if len(lengths) else 1.0
        norm = k1 * (1 - b + b * lengths / max(avg, 1e-6))
        row_index = np.repeat(np.arange(len(docs)), np.diff(tf.indptr))
        data = tf.data
        tf.data = (data * (k1 + 1) / (data + norm[row_index]) * idf[tf.indices]).astype(np.float32)
        self.weights = tf.tocsc()
        self.vocab = vocab
        self.doc_units: dict[str, list[int]] = {}
        for i, unit in enumerate(units):
            self.doc_units.setdefault(unit.doc_uid, []).append(i)

    def scores(self, query: str) -> np.ndarray:
        ids = sorted({self.vocab[word] for word in query_tokens(query) if word in self.vocab})
        if not ids:
            return np.zeros(len(self.units), dtype=np.float32)
        return np.asarray(self.weights[:, ids].sum(axis=1)).ravel()

    def search(self, query: str, k: int = 10, scores: np.ndarray | None = None, max_per_doc: int = 2) -> list[tuple[int, float]]:
        """Rank units by score with result diversity.

        Near-duplicate course-catalog rows are shared by several documents; a
        plain top-k can be filled by copies of one row and crowd out the gold
        document.  Two guards keep the window diverse: identical unit content
        (``match_key``) is returned once, and one document contributes at most
        ``max_per_doc`` units.
        """
        scores = self.scores(query) if scores is None else scores
        k = min(k, len(scores))
        if k <= 0:
            return []
        pool = min(max(k * 8, k + 32), len(scores))
        top = np.argpartition(-scores, pool - 1)[:pool]
        top = top[np.argsort(-scores[top], kind="stable")]
        seen_content: set[str] = set()
        per_doc: dict[str, int] = {}
        per_doc_tables: dict[str, int] = {}
        result: list[tuple[int, float]] = []
        for i in top:
            if scores[i] <= 0:
                continue
            unit = self.units[int(i)]
            content = match_key(unit.text)
            if content and content in seen_content:
                continue
            # Answer-dense table rows ("最低总学分 | 26 | …") score lower than
            # prose blocks on term overlap, so a same-doc quota filled by two
            # longer blocks evicts exactly the row that carries the number.
            # Table rows therefore draw from their OWN per-doc quota.
            block = (unit.extra or {}).get("block") or {}
            is_table = block.get("kind") == "table_row" or bool(block.get("table"))
            quota = per_doc_tables if is_table else per_doc
            if max_per_doc and quota.get(unit.doc_uid, 0) >= max_per_doc:
                continue
            if content:
                seen_content.add(content)
            quota[unit.doc_uid] = quota.get(unit.doc_uid, 0) + 1
            result.append((int(i), float(scores[i])))
            if len(result) >= k:
                break
        return result

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            pickle.dump(self, handle, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: Path) -> "BM25Index":
        with open(path, "rb") as handle:
            return pickle.load(handle)


# ---------------------------------------------------------------- corpus


DOC_META_KEYS = (
    "doc_uid", "source_url", "alias_urls", "site", "publisher", "published", "modified", "doc_number",
    "doc_type", "audience", "scope_level", "tier", "terms", "academic_years", "cohorts", "authority_rank",
    "series_id", "is_latest", "superseded_by", "version_order", "raw_sha256",
)


class Corpus:
    """Documents and blocks of one build directory."""

    def __init__(self, root: Path):
        self.root = root
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.build_id = self.manifest["build_id"]
        self.documents = {doc["doc_uid"]: doc for doc in read_jsonl(root / "documents.jsonl")}
        self.blocks = list(read_jsonl(root / "blocks.jsonl"))
        inactive = root / "inactive_blocks.jsonl"
        self.inactive_blocks = list(read_jsonl(inactive)) if inactive.is_file() else []

    @classmethod
    def latest(cls, out_root: Path) -> "Corpus":
        return cls(out_root / (out_root / "LATEST").read_text(encoding="utf-8").strip())

    def canonical(self, uid: str) -> str:
        doc = self.documents.get(uid)
        return (doc or {}).get("duplicate_of") or uid

    def doc_metadata(self, uid: str) -> dict[str, Any]:
        doc = self.documents.get(uid, {})
        meta = {key: doc[key] for key in DOC_META_KEYS if key in doc}
        meta["build_id"] = self.build_id
        return meta


def block_index_text(doc: dict[str, Any], block: dict[str, Any]) -> str:
    """Retrieval text: title and heading path give a bare table row its context."""
    parts = [doc.get("title", ""), " / ".join(block.get("heading_path") or []), block["text"]]
    return "\n".join(part for part in parts if part)


def block_units(corpus: Corpus, blocks: Iterable[dict[str, Any]]) -> list[Unit]:
    units = []
    for block in blocks:
        doc = corpus.documents.get(block["doc_uid"], {})
        units.append(Unit(block["doc_uid"], block["text"], block["block_id"], block_index_text(doc, block), {"block": block}))
    return units


def window_units(uid: str, text: str, size: int = 500, stride: int = 400) -> list[Unit]:
    text = text.strip()
    if not text:
        return []
    return [Unit(uid, text[start:start + size]) for start in range(0, max(1, len(text) - size + stride), stride)]


def corpus_hit(corpus: Corpus, unit: Unit, score: float) -> dict[str, Any]:
    """Adapt a unit to the hit shape consumed by ``build_evidence_pack``."""
    doc = corpus.documents.get(unit.doc_uid, {})
    block = unit.extra.get("block") or {}
    row_meta = {key: block[key] for key in ("block_id", "char_start", "char_end", "heading_path", "kind") if key in block}
    if block.get("table"):
        table = block["table"]
        row_meta.update({"table_id": table.get("table_id"), "row_index": table.get("row_index"), "kind": "table_row"})
        # Step 8 table-context binding: a numeric cell is only a fact together
        # with its column header and row identity ("Credits: 3" of AFRO 100).
        if table.get("header"):
            row_meta["column_header"] = table["header"]
        if table.get("cells"):
            row_meta["row_cells"] = table["cells"]
    return {
        "chunk": SimpleNamespace(id=unit.block_id or f"{unit.doc_uid}:w", page=block.get("page", 1), heading=" / ".join(block.get("heading_path") or []),
                                 parent_text=unit.text, metadata_json=json.dumps(row_meta, ensure_ascii=False)),
        "document": SimpleNamespace(id=unit.doc_uid, title=doc.get("title", ""), filename="", published_at=doc.get("published", ""), metadata_json=json.dumps(corpus.doc_metadata(unit.doc_uid), ensure_ascii=False)),
        "score": score,
    }


# ---------------------------------------------------------------- IR rerank


def _structured(meta: dict[str, Any]) -> bool:
    return bool(meta.get("terms") or meta.get("academic_years") or meta.get("audience"))


def ir_rerank(
    corpus: Corpus,
    index: BM25Index,
    question: str,
    *,
    k: int = 10,
    depth: int = 200,
    today: date | None = None,
    expand_latest: bool = True,
    ir: QueryIR | None = None,
    hard_filter: bool = False,
    drop_sink: list[dict[str, Any]] | None = None,
) -> list[tuple[int, float, dict[str, Any]]]:
    """BM25 → applicability/version-aware ordering.

    1. Candidates whose document does not apply to the question (term,
       academic year, audience, cohort) are moved behind applicable ones.
    2. When the question does not pin a term, a superseded edition is moved
       behind the latest edition of its series; if the latest edition was not
       retrieved its best-scoring block is pulled in (``expand_latest``).
    Returns ``(unit_index, score, info)`` with ``info`` explaining the move.

    ``ir`` may carry a pre-parsed (or user-context-merged) QueryIR; when
    omitted the question is parsed here as before.

    ``hard_filter=True`` (step 7, taxonomy v1) removes provably inapplicable
    candidates BEFORE ranking instead of demoting them (question pins 2025,
    document pins 2026 → wrong-year pollution can no longer backfill the
    pack).  Each removed candidate is appended to ``drop_sink`` with its
    mismatch kinds so the pack can record why it vanished.
    """
    from evidence.query_ir import hard_applicability_mismatches

    from evidence.query_ir import question_dates

    if ir is None:
        ir = parse_query(question, today=today)
    scores = index.scores(question)
    candidates = index.search(question, depth, scores)
    # A question that pins a year (term OR full event date) targets a specific
    # edition: when that edition is the superseded one (asking about a 2025
    # event whose only carrier is the 2025 guide of a series that now has a
    # 2026 edition), version demotion must not push it behind year-irrelevant
    # blocks (failure analysis 2026-10-16: sm-0057's gold block ranked #1 on
    # the date-variant query and still vanished behind superseded demotion).
    pinned = (bool(ir.terms or ir.academic_years) or bool(question_dates(ir.raw))) and not ir.wants_latest
    ranked: list[tuple[int, float, dict[str, Any]]] = []
    seen_units = {i for i, _ in candidates}
    for i, score in candidates:
        meta = corpus.doc_metadata(index.units[i].doc_uid)
        if hard_filter:
            hard_kinds = hard_applicability_mismatches(ir, meta)
            if hard_kinds:
                if drop_sink is not None and len(drop_sink) < 20:
                    drop_sink.append({
                        "unit_index": i,
                        "doc_uid": index.units[i].doc_uid,
                        "block_id": index.units[i].block_id or "",
                        "score": round(float(score), 4),
                        "kinds": hard_kinds,
                    })
                continue
        mismatches = applicability_mismatches(ir, meta) if _structured(meta) else []
        ranked.append((i, score, {"mismatches": [m["kind"] for m in mismatches], "superseded": False, "expanded": False}))
    if not pinned:
        present_series = {}
        for i, score, info in ranked:
            meta = corpus.doc_metadata(index.units[i].doc_uid)
            if meta.get("series_id") and meta.get("is_latest") and not info["mismatches"]:
                present_series[meta["series_id"]] = True
        additions = []
        for i, score, info in ranked:
            meta = corpus.doc_metadata(index.units[i].doc_uid)
            sid = meta.get("series_id")
            if not sid or meta.get("is_latest", True):
                continue
            info["superseded"] = True
            if sid in present_series or not expand_latest:
                # The latest edition is already a candidate — but BM25.search's
                # global content dedup keeps ONE copy of a duplicated block,
                # and it may be THIS superseded one (identical SharePoint
                # pages: "Course-Drop-Request.aspx" vs "(1).aspx").  The latest
                # doc's own quota then holds only its lower-scoring blocks and
                # the answer text dies with the superseded hit.  Rescue the
                # latest doc's content twin so version discrimination cannot
                # orphan the block that carries the answer (seen live
                # 2026-10-15: 'drop deadline Nov 6 2026' was BM25 rank 1 and
                # still never reached the pack).
                _rescue_content_twin(corpus, index, ir, i, score, sid, ranked, seen_units, additions, hard_filter=hard_filter)
                continue
            latest = _series_latest(corpus, sid)
            units = index.doc_units.get(latest or "", [])
            if units:
                best = max(units, key=lambda u: scores[u])
                if scores[best] > 0 and best not in seen_units:
                    latest_meta = corpus.doc_metadata(latest)
                    if hard_filter and hard_applicability_mismatches(ir, latest_meta):
                        seen_units.discard(best)  # expansion must not bypass the filter
                    else:
                        seen_units.add(best)
                        mismatches = applicability_mismatches(ir, latest_meta) if _structured(latest_meta) else []
                        # Inherit the superseded hit's score: it was retrieved for
                        # this topic, the newer edition is what should answer it.
                        additions.append((best, max(score, float(scores[best])), {"mismatches": [m["kind"] for m in mismatches], "superseded": False, "expanded": True}))
            present_series[sid] = True
        ranked.extend(additions)
    ranked.sort(key=lambda row: (bool(row[2]["mismatches"]), row[2]["superseded"], -row[1]))
    return ranked[:k]


def _rescue_content_twin(corpus: "Corpus", index: BM25Index, ir: QueryIR, unit_i: int, score: float,
                         sid: int, ranked: list[tuple[int, float, dict[str, Any]]],
                         seen_units: set[int], additions: list[tuple[int, float, dict[str, Any]]],
                         hard_filter: bool = False) -> None:
    """Pull the latest edition's copy of a duplicated block when only the
    superseded edition's copy survived BM25's content dedup."""
    from evidence.query_ir import hard_applicability_mismatches

    latest = _series_latest(corpus, sid)
    if not latest:
        return
    content = match_key(index.units[unit_i].text)
    if not content:
        return
    already = {match_key(index.units[j].text) for j, _, _ in ranked
               if index.units[j].doc_uid == latest}
    if content in already:
        return
    latest_meta = corpus.doc_metadata(latest)
    if hard_filter and hard_applicability_mismatches(ir, latest_meta):
        return  # the twin may not bypass the pre-ranking filter either
    mismatches = applicability_mismatches(ir, latest_meta) if _structured(latest_meta) else []
    for u in index.doc_units.get(latest, []):
        if match_key(index.units[u].text) == content and u not in seen_units:
            seen_units.add(u)
            additions.append((u, score, {"mismatches": [], "superseded": False, "expanded": True}))
            return


_SERIES_LATEST_CACHE: dict[int, dict[str, str]] = {}


def _series_latest(corpus: Corpus, sid: str) -> str:
    cache = _SERIES_LATEST_CACHE.get(id(corpus))
    if cache is None:
        cache = {doc["series_id"]: uid for uid, doc in corpus.documents.items() if doc.get("series_id") and doc.get("is_latest") and doc.get("status") == "active"}
        _SERIES_LATEST_CACHE[id(corpus)] = cache
    return cache.get(sid, "")


def quote_in(unit_text: str, quote: str) -> bool:
    key = match_key(quote)
    return bool(key) and key in match_key(unit_text)
