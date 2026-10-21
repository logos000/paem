"""Index construction and loading shared by the eval harness and the runtime engine.

This module is the single home of the "which units does a config index, and
where is its cache" knowledge (Stage 3A): ``eval.harness`` and
``runtime.engine`` must never grow their own copies again — the offline
benchmark and the online product service read indices through exactly these
functions.
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from export.arms import LLM_CONFIGS, augment_index_text, load_annotations, load_retrieval_texts
from evidence.store import read_jsonl
from retrieve.lexical import BM25Index, Corpus, Unit, block_units, window_units
from retrieve import dense

DENSE_CONFIGS = ("active_dense", "active_hybrid", "active_llm_validated_dense", "active_llm_validated_hybrid")
# bge-m3 (2026-10-16): multilingual — Chinese questions must embed against the
# English-dominant corpus semantically; the en-only small model turned every
# Chinese query into dense noise (CLIR failure mode).  Matrix cache keys
# include the model dir so switching models never reuses a stale matrix.
DENSE_MODEL = "G:/model_download/builder/embed/bge-m3"
# P7.4 interaction matrix: validated-augmented corpus under dense/hybrid —
# augmentation flows through load_index's LLM branch, retrieval through dense.
MATRIX_LLM_DENSE = ("active_llm_validated_dense", "active_llm_validated_hybrid")


def build_units(corpus: Corpus, config: str) -> list[Unit]:
    if config == "raw":
        units: list[Unit] = []
        for row in read_jsonl(corpus.root / "baseline_pages.jsonl"):
            units.extend(window_units(row["doc_uid"], row["text"]))
        by_doc: dict[str, list[str]] = defaultdict(list)
        for block in corpus.blocks + corpus.inactive_blocks:
            if corpus.documents.get(block["doc_uid"], {}).get("kind") == "attachment":
                by_doc[block["doc_uid"]].append(block["text"])
        for uid, texts in by_doc.items():
            units.extend(window_units(uid, "\n".join(texts)))
        return units
    if config == "clean":
        return block_units(corpus, corpus.blocks + corpus.inactive_blocks)
    return block_units(corpus, corpus.blocks)


def load_index(corpus: Corpus, config: str, rebuild: bool = False, index_workers: int = 6) -> BM25Index:
    if config in LLM_CONFIGS or config in MATRIX_LLM_DENSE:
        annotations = load_annotations(corpus.root)
        if not annotations:
            raise SystemExit(f"[index] config {config} needs {corpus.root / 'llm_annotations.jsonl'} — run the annotate pilot first (python -m annotate.run_pilot)")
        base_config = "active_llm_validated" if config in MATRIX_LLM_DENSE else config
        units = augment_index_text(build_units(corpus, "active"), base_config, annotations, load_retrieval_texts(corpus.root))
        started = time.perf_counter()
        # No pickle cache: annotation sidecars evolve between pilot runs, and a
        # stale cache would silently mix annotation versions.
        index = BM25Index(units, workers=index_workers)
        print(f"[index] {config}: {len(index.units)} units, {len(index.vocab)} terms, {time.perf_counter() - started:.0f}s", file=sys.stderr)
        return index
    name = "active" if config in ("active_ir", "active_ir_rr") else config
    path = corpus.root / "_bm25" / f"{name}.pkl"
    if path.is_file() and not rebuild:
        return BM25Index.load(path)
    started = time.perf_counter()
    index = BM25Index(build_units(corpus, name), workers=index_workers)
    index.save(path)
    print(f"[index] {name}: {len(index.units)} units, {len(index.vocab)} terms, {time.perf_counter() - started:.0f}s", file=sys.stderr)
    return index


def load_known_conflicts(corpus_root: Path) -> list[dict[str, Any]]:
    """Build-time fact-conflict candidates (P3) that flow into every pack.

    Both sides of a disagreement are retained by ``build_evidence_pack``
    instead of being silently merged.
    """
    path = Path(corpus_root) / "conflict_candidates.jsonl"
    return list(read_jsonl(path)) if path.is_file() else []


def load_dense_matrix(corpus: Corpus, config: str, index: BM25Index, dense_model: str = DENSE_MODEL):
    """Unit vectors for a dense config, with the per-config resumable cache."""
    texts = [unit.index_text or unit.text for unit in index.units]
    # Per-config vectors AND per-config cache: augmented corpora must never
    # reuse the plain active vectors even when counts coincide.
    cache_name = f"{config}__{Path(dense_model).name}.npy"
    return dense.embed_units(texts, dense_model, corpus.root / "_dense" / cache_name)


def rr_unit_texts(index: BM25Index) -> list[str]:
    """Rerank input per unit: the pre-augmentation text when the config keeps
    the augmented form out of the reranker (``*_rr_raw`` hypothesis), else the
    normal index text."""
    return [
        (unit.extra or {}).get("base_index_text") or unit.index_text or unit.text
        for unit in index.units
    ]
