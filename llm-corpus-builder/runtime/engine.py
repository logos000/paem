"""CorpusEngine: the model-agnostic query-side runtime over one corpus build.

One engine instance owns a loaded build (Corpus), the index of one arm
configuration and every model that arm needs (reranker, dense vectors), then
answers questions through the full offline-proven chain::

    parse_query → (user-context merge, R1) → retrieve (lexical/IR/RR/dense/hybrid)
    → corpus_hit → build_evidence_pack(+known conflicts) → decide → contract

This is the same code path the eval harness measures (Stage 3A single-path
guarantee): the offline benchmark and the online product service must never
diverge into two implementations.
"""

from __future__ import annotations

from datetime import date
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from evidence.pack import build_evidence_pack
from evidence.query_ir import parse_query
from export.arms import IR_CONFIGS, RR_CONFIGS
from retrieve import dense
from retrieve.lexical import Corpus, corpus_hit, ir_rerank
from retrieve.rerank import get_reranker
from runtime.contract import ANSWER_CONTEXT_VERSION, DEFAULT_CONFIG, SERVICE_CONFIGS, assemble_answer_context
from runtime.indexing import DENSE_CONFIGS, DENSE_MODEL, load_dense_matrix, load_index, load_known_conflicts, rr_unit_texts
from runtime.user_context import merge_user_context

REPO_ROOT = Path(__file__).resolve().parents[1]

_PURE_DENSE_CONFIGS = ("active_dense", "active_llm_validated_dense")

# Failure-analysis fix 3 (2026-10-16): Chinese questions pin dates as
# 2025年6月20日 while the corpus writes "June 20, 2025" / "2025-06-20" —
# plain BM25 never bridges the script gap and six failing rows never saw
# their gold block.  These deterministic variants are derived from the
# ORIGINAL question (no LLM, single retrieval round).
# Domain knowledge (date bridges, fold families, anchors, stitch heuristics)
# lives in the DomainProfile YAML; the constants below are compiled views so
# the rest of this module keeps its names (2026-10-16 generalization).
from domain_profile import DomainProfile, load_profile  # noqa: E402

_PROFILE: DomainProfile = load_profile()
_FULL_DATE_PATTERNS = (
    _PROFILE.pattern("calendar", "full_date_zh"),
    _PROFILE.pattern("calendar", "full_date_iso"),
    _PROFILE.pattern("calendar", "full_date_en"),
)
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")


def _family_regex(spec: Any) -> re.Pattern:
    """Compile one profile pattern spec (plain string or {pattern, flags})."""
    flags = 0
    if isinstance(spec, dict):
        for token in str(spec.get("flags", "")).lower().split("|"):
            flags |= {"i": re.I, "m": re.M, "s": re.S}.get(token.strip(), 0)
        spec = spec.get("pattern")
    return re.compile(str(spec), flags)


# Fold families (2026-10-16): whole-section / filtered-row answers.  Point
# questions inside these tables ("ECE 391 的先修课是什么") stay on the
# single-row path: no 哪些/list/which in the trigger, no fold.  Order in the
# YAML = specificity; skip_if_anchor makes a course code in the question a
# point ask inside that table.
_ROLLUP_FAMILIES: tuple[dict[str, Any], ...] = tuple({
    "name": str(entry["name"]),
    "mode": str(entry.get("mode", "section")),
    "skip_if_anchor": bool(entry.get("skip_if_anchor", False)),
    "label_preference": re.compile(str(entry["label_preference"]), re.I) if entry.get("label_preference") else None,
    "trigger": _family_regex(entry["trigger"]),
    "row": _family_regex(entry["row"]),
} for entry in _PROFILE.entries("fold_families"))
_ENUMERATIVE_COURSE_RE = _ROLLUP_FAMILIES[0]["trigger"]
_COURSE_ROW_RE = _ROLLUP_FAMILIES[0]["row"]
_COURSE_CODE_ANCHOR_RE = _PROFILE.pattern("anchors", "course_code")
_ANCHOR_IGNORED_PREFIXES = frozenset(p.casefold() for p in _PROFILE.words("anchors", "course_code_ignored_prefixes"))


def _course_code_anchors(text: str) -> set[str]:
    """Course-code anchors in a question, minus semester keys.

    SP2025 / FA24 look exactly like course codes to the regex but are term
    keys (this domain has no FA/SP/SU/WI course prefixes) — counting them
    would falsely mark enumerative questions ("SP2025 的课号清单") as point
    asks.  Both the anchor shape and the ignored prefixes come from the
    profile.
    """
    anchors: set[str] = set()
    for match in _COURSE_CODE_ANCHOR_RE.finditer(str(text)):
        code = match.group(1)
        letters = re.match(r"[A-Z]{2,5}", code)
        digits = code[len(letters.group(0)):].strip() if letters else ""
        if letters and letters.group(0).casefold() in _ANCHOR_IGNORED_PREFIXES and len(digits) in (2, 4):
            continue
        anchors.add(code)
    return anchors


def _date_query_variants(question: str, limit: int = 4) -> list[str]:
    """Lexical forms of the question's full dates for the BM25 pool."""
    found: list[tuple[str, int, int]] = []
    for match in _FULL_DATE_PATTERNS[0].finditer(question):
        found.append((match.group(1), int(match.group(2)), int(match.group(3))))
    for match in _FULL_DATE_PATTERNS[1].finditer(question):
        found.append((match.group(1), int(match.group(2)), int(match.group(3))))
    for match in _FULL_DATE_PATTERNS[2].finditer(question):
        month = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
                 "sep": 9, "oct": 10, "nov": 11, "dec": 12}[match.group(1)[:3].lower()]
        found.append((match.group(3), month, int(match.group(2))))
    variants: list[str] = []
    for year, month, day in dict.fromkeys(found):
        variants.append(f"{_MONTH_NAMES[month - 1]} {day}, {year}")
        variants.append(f"{year}-{month:02d}-{day:02d}")
        if len(variants) >= limit:
            break
    return variants[:limit]


def _normalize_today(today: date | str | None) -> date | None:
    if today is None or isinstance(today, date):
        return today
    return date.fromisoformat(str(today))


def _resolve_dense_model(dense_model: str) -> str:
    """Dense model ids are repo-relative paths in the harness; the engine must
    work regardless of the service's working directory."""
    path = Path(dense_model)
    return str(path if path.is_absolute() else (REPO_ROOT / path))


class CorpusEngine:
    def __init__(self, corpus: Corpus, config: str, index, *, reranker=None, dense_matrix=None, texts_for_rr: list[str] | None = None, known_conflicts: list[dict[str, Any]] | None = None, rerank_model: str = "", dense_model: str = ""):
        self.corpus = corpus
        self.config = config
        self.index = index
        self.reranker = reranker
        self.dense_matrix = dense_matrix
        self.texts_for_rr = texts_for_rr or []
        self.known_conflicts = known_conflicts or []
        self.rerank_model = rerank_model
        self.dense_model = dense_model
        self._model_lock = threading.Lock()
        self.last_rerank_seconds: float | None = None
        # Step 7 hard applicability filter (taxonomy v1 2026-10-16): provably
        # wrong-year/wrong-audience candidates are removed pre-ranking.  Env
        # kill-switch RUNTIME_HARD_FILTER=0 restores pure demotion for A/B.
        # Advisory governance (2026-10-17): RUNTIME_GOVERNANCE_MODE=advisory is
        # the one-switch shadow mode — every gate still computes its telemetry,
        # but none may change the answer: the five switches below go off, the
        # superseded version block becomes a non-blocking label, and decide()
        # keeps its status labels while never vetoing.  Enforce is the default
        # and byte-identical to the historical behavior.
        self.governance_advisory = os.getenv("RUNTIME_GOVERNANCE_MODE", "enforce") == "advisory"
        self.hard_filter = os.getenv("RUNTIME_HARD_FILTER", "1") not in ("0", "false", "no") and not self.governance_advisory
        # Failure-analysis fix 4: after pack assembly, fact slots the pool
        # never covered (missing dates/codes/number facts) get one targeted
        # index round and the pack is rebuilt once — inside this request.
        self.fact_rescue = os.getenv("RUNTIME_FACT_RESCUE", "1") not in ("0", "false", "no") and not self.governance_advisory
        # Split-policy stitching (2026-10-16 second wave): a block cut at a
        # rule/exception boundary ("…特殊情况如下：" | next block holds the
        # exceptions) answers half a question.  Pull ONE adjacent block when
        # the text visibly dangles.  RUNTIME_NEIGHBOR_STITCH=0 disables.
        self.stitch_neighbors = os.getenv("RUNTIME_NEIGHBOR_STITCH", "1") not in ("0", "false", "no") and not self.governance_advisory
        # Ablation switches (2026-10-16 superset round): every component the
        # ablation matrix needs is individually killable per process.
        self.coverage_union = os.getenv("RUNTIME_COVERAGE_UNION", "1") not in ("0", "false", "no") and not self.governance_advisory
        self.section_rollup_enabled = os.getenv("RUNTIME_SECTION_ROLLUP", "1") not in ("0", "false", "no") and not self.governance_advisory
        # Doc completion (2026-10-17 sm-0082 class): a packed unit's sibling
        # blocks may hold the asked fact while ranking below every channel
        # (the Grade-Replacement email's deadline sentence shares one query
        # term and misses top-50 in BM25, dense AND hybrid).  Recall mechanism,
        # not veto — deliberately NOT tied to the governance mode; own
        # kill-switch RUNTIME_DOC_COMPLETION=0.
        self.doc_completion = os.getenv("RUNTIME_DOC_COMPLETION", "1") not in ("0", "false", "no")
        self.last_hard_drops: list[dict[str, Any]] = []

    # ------------------------------------------------------------ loading

    @classmethod
    def load(
        cls,
        root: Path | str | None = None,
        *,
        config: str = DEFAULT_CONFIG,
        rebuild_index: bool = False,
        rerank_model: str = "bge-base",
        device: str = "",
        dense_model: str = DENSE_MODEL,
        index_workers: int = 6,
    ) -> "CorpusEngine":
        """Load one build + one arm configuration.

        ``root`` accepts a build directory (has manifest.json), a directory
        with a ``LATEST`` pointer, or None for ``<repo>/data/corpus/LATEST``.
        ``index_workers=1`` builds indexes with serial tokenization (needed
        when loading from a background thread on Windows).
        """
        if config not in SERVICE_CONFIGS:
            raise ValueError(f"unknown config {config!r}; allowed: {SERVICE_CONFIGS}")
        corpus = cls._load_corpus(root)
        index = load_index(corpus, config, rebuild_index, index_workers=index_workers)
        reranker = None
        texts_for_rr = None
        if config in RR_CONFIGS:
            reranker = get_reranker(rerank_model, device)
            texts_for_rr = rr_unit_texts(index)
        resolved_dense = _resolve_dense_model(dense_model) if config in DENSE_CONFIGS else ""
        dense_matrix = load_dense_matrix(corpus, config, index, resolved_dense) if config in DENSE_CONFIGS else None
        return cls(
            corpus,
            config,
            index,
            reranker=reranker,
            dense_matrix=dense_matrix,
            texts_for_rr=texts_for_rr,
            known_conflicts=load_known_conflicts(corpus.root),
            rerank_model=rerank_model if reranker is not None else "",
            dense_model=resolved_dense,
        )

    @staticmethod
    def _load_corpus(root: Path | str | None) -> Corpus:
        if root is None:
            return Corpus.latest(REPO_ROOT / "data" / "corpus")
        root = Path(root)
        if (root / "manifest.json").is_file():
            return Corpus(root)
        if (root / "LATEST").is_file():
            return Corpus.latest(root)
        raise FileNotFoundError(f"{root} is neither a build directory (manifest.json) nor a LATEST pointer directory")

    # ------------------------------------------------------------ answering

    def retrieve(self, question: str, ir=None, today: date | None = None, k: int = 10) -> list[tuple[int, float]]:
        """The arm's retrieval step — branch-for-branch the harness path.

        Public because the eval harness consumes it too (single-path
        guarantee): offline metrics and the online service call this same
        function.  ``self.last_rerank_seconds`` records the reranker timing of
        the most recent call (None when the arm has no rerank step).
        """
        from retrieve.rerank import rerank_candidates

        self.last_rerank_seconds = None
        self.last_hard_drops = []
        if ir is None:
            ir = parse_query(question, today=today)
        if self.config in IR_CONFIGS:
            drop_sink: list[dict[str, Any]] = [] if self.hard_filter else None  # type: ignore[assignment]
            triples = ir_rerank(self.corpus, self.index, question, k=50 if self.config in RR_CONFIGS else k, today=today, ir=ir,
                                hard_filter=self.hard_filter, drop_sink=drop_sink)
            self.last_hard_drops = drop_sink or []
            if self.config in RR_CONFIGS:
                with self._model_lock:
                    ranked, seconds = rerank_candidates(question, triples, self.reranker, self.texts_for_rr, k=k)
                self.last_rerank_seconds = seconds
                return ranked
            return [(i, score) for i, score, _ in triples]
        if self.config in DENSE_CONFIGS:
            # The pre-ranking hard applicability filter applies to the
            # semantic channel too (2026-10-16): without it, hybrid happily
            # fuses year-mismatched semantically-similar docs back in — seen
            # live as a 2023 calendar topping a FA2026 question.
            from evidence.query_ir import hard_applicability_mismatches
            with self._model_lock:
                qvec = dense.embed_query(question, self.dense_model)
            dense_hits = dense.topk(self.dense_matrix, qvec, 50)
            ir_eff = ir
            if self.hard_filter and getattr(ir, "relative_term", "") == "latest" and ir.terms:
                # Conditional current-term gate (2026-10-17, trap-curr-01): a
                # "最新" query resolves to a SOFT term key (pack-level filter
                # stays off so an old series' latest edition is never excluded).
                # Promote it to a hard pin for the retrieval channels only when
                # the candidate pool actually contains current-term docs — then
                # the 2026-fall guide surfaces and year-mismatched calendars
                # drop, exactly like an explicit-term query.
                meta_of = self.corpus.doc_metadata
                pool_terms = [t for i, _ in dense_hits for t in (meta_of(self.index.units[i].doc_uid).get("terms") or [])]
                if any(t in ir.terms for t in pool_terms):
                    from dataclasses import replace as _dc_replace
                    ir_eff = _dc_replace(ir, wants_latest=False)
            if self.hard_filter:
                dense_hits = [
                    (i, score) for i, score in dense_hits
                    if not hard_applicability_mismatches(ir_eff, self.corpus.doc_metadata(self.index.units[i].doc_uid))
                ]
            # RRF decides ORDER only; the pack's support/coverage math needs
            # native-scale scores (BM25 ~10+, RRF ranks ~0.02 — fused ranks
            # collapsed coverage below the abstain floor on 52/153 questions
            # in the first hybrid run, 2026-10-16).  Dense-only hits are
            # calibrated to BM25 scale (cosine ~0.5-0.8 x 10 ≈ mid-range BM25).
            native: dict[int, float] = {i: score * 10.0 for i, score in dense_hits}
            if self.config in _PURE_DENSE_CONFIGS:
                return dense_hits[:k]
            drop_sink: list[dict[str, Any]] = [] if self.hard_filter else None  # type: ignore[assignment]
            lexical = ir_rerank(self.corpus, self.index, question, k=50, today=today, ir=ir_eff,
                                hard_filter=self.hard_filter, drop_sink=drop_sink)
            self.last_hard_drops = drop_sink or []
            for i, score, _ in lexical:
                native[i] = max(native.get(i, 0.0), float(score))
            # Full fusion for every language.  (A zh script-match abstention —
            # dense-only ordering for zh queries — fixed the C4 multi-hop
            # pack-loss cases (6/13→9/13) but the full-275 regression showed
            # the lexical channel's messier packs were load-bearing elsewhere:
            # zh 110→98, C1 honesty 16→6, net −18 good.  Reverted 2026-10-16;
            # C4 needs an intent-level mechanism, not a language-level one.)
            fused = dense.rrf_fuse([[(i, score) for i, score, _ in lexical], dense_hits])[:k]
            return [(i, native.get(i, score)) for i, score in fused]
        return self.index.search(question, k)

    def _family_docs(self, family: dict[str, Any]) -> set[str]:
        """Documents owning at least one row of this fold family (cached)."""
        cache = getattr(self, "_family_doc_cache", None)
        if cache is None:
            cache = self._family_doc_cache = {}
        name = family["name"]
        if name not in cache:
            row_re = family["row"]
            cache[name] = {
                b.get("doc_uid", "")
                for b in self.corpus.blocks
                if row_re.search(str(b.get("text") or ""))
            }
        return cache[name]

    def _section_rollup(self, question: str, ranked: list[tuple[int, float]],
                        query_pool: list[str] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Fold one document's row section (or filtered row set) into one hit.

        Enumerative/aggregative questions whose ANSWER IS a table section or
        a row subset (哪些必修课 / FA26 开了哪些课 / 期末考试安排 / 哪些课以
        ECE 220 为先修) need DEPTH in one table, but two layers spread the
        evidence thin: BM25's ``max_per_doc=2`` diversity guard keeps one
        document to two units per query round, and the pack's diversity
        selection spends the 8-block budget across documents — the generator
        then honestly reports a missing list.  The rollup is a synthetic
        evidence hit carrying the section (or the anchor-filtered rows) of
        the best-matching document, so the normal pack pipeline (coverage,
        decision, requirements) runs unchanged over it.  Returns
        (hit, meta); (None, None) whenever no family trigger matches or no
        family document was retrieved.
        """
        doc_scores: dict[str, float] = {}
        for unit_index, score in ranked[:50]:
            uid = self.index.units[unit_index].doc_uid
            doc_scores[uid] = max(doc_scores.get(uid, float("-inf")), score)
        if not doc_scores:
            return None, None
        pool_tokens: set[str] = set()
        for text in [question, *(query_pool or [])]:
            pool_tokens |= set(re.findall(r"[a-z0-9]+", str(text).casefold()))
        for family in _ROLLUP_FAMILIES:
            if not family["trigger"].search(question):
                continue
            if family.get("skip_if_anchor") and _course_code_anchors(question):
                continue
            candidates = [uid for uid in doc_scores if uid in self._family_docs(family)]
            if not candidates:
                continue
            if family["mode"] == "filter":
                hit, meta = self._filtered_rollup(family, candidates, doc_scores, pool_tokens, question)
            else:
                hit, meta = self._contiguous_rollup(family, candidates, doc_scores, pool_tokens)
            if hit is not None:
                return hit, meta
        return None, None

    @staticmethod
    def _doc_priority(pool_tokens: set[str], documents: dict[str, Any], candidates: list[str],
                      doc_scores: dict[str, float]) -> str:
        def key(uid: str) -> tuple[int, float, str]:
            # Which document to fold: the one whose title best matches the
            # question's own restatements (pool tokens bridge zh→en), then
            # retrieval strength.  Deterministic tie-break on uid.
            title = str((documents.get(uid) or {}).get("title") or "")
            overlap = len(set(re.findall(r"[a-z0-9]+", title.casefold())) & pool_tokens)
            return (overlap, doc_scores[uid], uid)

        return max(candidates, key=key)

    def _contiguous_rollup(self, family: dict[str, Any], candidates: list[str],
                           doc_scores: dict[str, float], pool_tokens: set[str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        row_re = family["row"]
        best_doc = self._doc_priority(pool_tokens, self.corpus.documents, candidates, doc_scores)
        doc_blocks = self._doc_blocks(best_doc)
        anchor_pos = next(
            (k for k, b in enumerate(doc_blocks) if row_re.search(str(b.get("text") or ""))),
            None,
        )
        if anchor_pos is None:  # unreachable: _family_docs guarantees a row
            return None, None

        def is_rowish(block: dict[str, Any]) -> bool:
            text = str(block.get("text") or "")
            if block.get("kind") != "table_row":
                return False
            # Family rows, their group totals and short wrapped-line
            # continuations ("Computation").
            return bool(row_re.search(text)) or "Total Required" in text or len(text) <= 80

        def is_section_header(block: dict[str, Any]) -> bool:
            # '4.1 Technical Electives' / '3. Required Courses' — a numbered
            # heading paragraph closes the section; running prose between row
            # groups ('In addition to the 128-AU requirement…') does not.
            return block.get("kind") == "paragraph" and bool(
                re.match(r"^\s*\d+(?:\.\d+)*\.?\s+\S", str(block.get("text") or ""))
            )

        lo = anchor_pos
        while lo > 0 and is_rowish(doc_blocks[lo - 1]):
            lo -= 1
        # Label provenance: a paragraph naming the section rides along so the
        # generator can say WHAT the rows are.  The immediately preceding
        # paragraph can be the WRONG sibling ("ii). Elective courses…" sits
        # directly above the required-course table, with "i). Required
        # courses…" one block higher) — prefer the nearest paragraph that
        # names required courses, within three blocks above the table.
        label_block = None
        for k in range(lo - 1, max(-1, lo - 4), -1):
            block = doc_blocks[k]
            if block.get("kind") != "paragraph":
                continue
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            if label_block is None:
                label_block = block
            if family["label_preference"] is not None and family["label_preference"].search(text):
                label_block = block
                break
        prefix = ""
        if label_block is not None:
            context_line = str(label_block.get("text") or "").strip()
            prefix = context_line[:200] + ("\n…" if len(context_line) > 200 else "") + "\n"
        hi = anchor_pos
        while (
            hi + 1 < len(doc_blocks)
            and not is_section_header(doc_blocks[hi + 1])
            and not str(doc_blocks[hi + 1].get("text") or "").startswith("Course Code Course Name")
            and hi - lo < 40
        ):
            hi += 1
        section = doc_blocks[lo : hi + 1][:40]
        rollup_text = prefix + "\n".join(str(b.get("text") or "") for b in section)
        if len(rollup_text) > 3800:
            rollup_text = rollup_text[:3800] + "\n…(section truncated)"
        return self._synth_rollup_hit(family["name"], best_doc, doc_blocks, anchor_pos, section, rollup_text,
                                      max(doc_scores[best_doc], 0.0))

    def _filtered_rollup(self, family: dict[str, Any], candidates: list[str],
                         doc_scores: dict[str, float], pool_tokens: set[str], question: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Reverse-lookup folds: the answer is every row mentioning the
        question's course-code anchors (the prerequisites table has 292 rows —
        folding it whole is both impossible and wrong)."""
        anchors = _course_code_anchors(question)
        if not anchors:
            return None, None
        row_re = family["row"]
        best_doc, best_rows = None, []
        for uid in sorted(candidates, key=lambda u: (-doc_scores[u], u)):
            rows = [
                b for b in self.corpus.blocks
                if b.get("doc_uid") == uid and row_re.search(str(b.get("text") or ""))
                and any(a in str(b.get("text") or "") for a in anchors)
            ]
            if rows:
                best_doc, best_rows = uid, rows
                break
        if best_doc is None:
            return None, None
        section = best_rows[:40]
        rollup_text = f"Rows mentioning {sorted(anchors)}:\n" + "\n".join(str(b.get("text") or "") for b in section)
        if len(rollup_text) > 3800:
            rollup_text = rollup_text[:3800] + "\n…(rows truncated)"
        return self._synth_rollup_hit(family["name"], best_doc, [section[0]], 0, section, rollup_text,
                                      max(doc_scores[best_doc], 0.0))

    def _synth_rollup_hit(self, family: str, best_doc: str, doc_blocks: list[dict[str, Any]], anchor_pos: int,
                          section: list[dict[str, Any]], rollup_text: str, score: float
                          ) -> tuple[dict[str, Any], dict[str, Any]]:
        anchor_block = doc_blocks[anchor_pos] if 0 <= anchor_pos < len(doc_blocks) else section[0]
        doc = self.corpus.documents.get(best_doc, {})
        hit = {
            "chunk": {
                "id": f"{anchor_block.get('block_id', '')}:rollup",
                "page": anchor_block.get("page", 1),
                "heading": " / ".join(anchor_block.get("heading_path") or []) or f"{family} table (section rollup)",
                "parent_text": rollup_text,
                "metadata_json": json.dumps({
                    "block_id": anchor_block.get("block_id", ""),
                    "kind": "table_row",
                    "rollup": True,
                    "rollup_family": family,
                    "rollup_blocks": [b.get("block_id") for b in section],
                }, ensure_ascii=False),
            },
            "document": {
                "id": best_doc,
                "title": doc.get("title", ""),
                "filename": "",
                "published_at": doc.get("published", ""),
                "metadata_json": json.dumps(self.corpus.doc_metadata(best_doc), ensure_ascii=False),
            },
            "score": score,
        }
        meta = {"doc_uid": best_doc, "title": doc.get("title", ""), "family": family,
                "blocks": len(section), "chars": len(rollup_text)}
        return hit, meta

    def _fact_rescue(self, question: str, pack: dict[str, Any], ir) -> tuple[list[tuple[int, float]], list[dict[str, Any]]]:
        """One targeted index round per uncovered fact slot (fix 4, 2026-10-16).

        Rows the pack could not cover (a date, course code or number fact the
        question demands) get a direct lexical probe; hits whose text actually
        carries the fact (and that pass the hard applicability filter) extend
        the candidate pool for a single pack rebuild.  Returns
        (extra_candidates, rescue_metadata) — empty lists when nothing helps.
        """
        from evidence.query_ir import hard_applicability_mismatches
        from retrieve.lexical import match_key

        missing: list[str] = []
        for kind in ("dates", "course_codes", "number_facts", "years"):
            missing.extend(str(v) for v in (pack.get("missing_facts") or {}).get(kind, [])[:2])
        extra: list[tuple[int, float]] = []
        meta: list[dict[str, Any]] = []
        seen_units = set()
        for fact in missing[:4]:
            for unit_index, score in self.index.search(fact, 3):
                if unit_index in seen_units:
                    continue
                unit = self.index.units[unit_index]
                if fact not in match_key(unit.text):
                    continue  # the probe must actually carry the fact
                doc_meta = self.corpus.doc_metadata(unit.doc_uid)
                if self.hard_filter and hard_applicability_mismatches(ir, doc_meta):
                    continue
                seen_units.add(unit_index)
                extra.append((unit_index, float(score)))
                meta.append({"doc_uid": unit.doc_uid, "block_id": unit.block_id or "", "fact": fact})
                break  # one rescuing block per fact is enough
            if len(extra) >= 3:
                break
        return extra, meta

    def build_pack(self, question: str, ranked: list[tuple[int, float]], *, today: date | None = None, ir=None, context: dict[str, Any] | None = None, pack_limit: int = 8, full_candidate_pool: bool = False, hard_drops: list[dict[str, Any]] | None = None, query_pool: list[str] | None = None) -> dict[str, Any]:
        """Turn ranked units into the EvidencePack (the same builder the
        service path uses; the harness calls this for its pack metrics).

        ``hard_drops`` carries pre-ranking applicability exclusions (step 7);
        None falls back to the most recent retrieve() call's drops so the
        offline harness sees them without changing its call shape."""
        if ir is None:
            ir = parse_query(question, today=today)
        if hard_drops is None:
            hard_drops = self.last_hard_drops
        hard_drop_kinds = sorted({kind for drop in hard_drops or [] for kind in drop.get("kinds", [])})
        candidates = ranked if full_candidate_pool else ranked[:pack_limit]
        rollup_hit, rollup_meta = (
            self._section_rollup(question, ranked, query_pool) if self.section_rollup_enabled else (None, None)
        )
        hits = ([rollup_hit] if rollup_hit else []) + [
            corpus_hit(self.corpus, self.index.units[i], score) for i, score in candidates
        ]
        pack = build_evidence_pack(
            question,
            hits,
            context=context or None,
            limit=pack_limit,
            today=today,
            known_conflicts=self.known_conflicts,
            ir=ir,
            hard_drop_kinds=hard_drop_kinds,
            query_pool=query_pool if self.coverage_union else None,
            advisory=self.governance_advisory,
        )
        if rollup_meta:
            pack["section_rollup"] = rollup_meta
        if self.fact_rescue and not full_candidate_pool:
            rescued, rescued_meta = self._fact_rescue(question, pack, ir)
            if rescued:
                # rebuild once with the extended pool so coverage/decision/
                # requirements are recomputed over the rescued evidence too
                merged = ranked + rescued
                pack = build_evidence_pack(
                    question,
                    ([rollup_hit] if rollup_hit else []) + [
                        corpus_hit(self.corpus, self.index.units[i], score) for i, score in merged
                    ],
                    context=context or None,
                    limit=pack_limit,
                    today=today,
                    known_conflicts=self.known_conflicts,
                    ir=ir,
                    hard_drop_kinds=hard_drop_kinds,
                    query_pool=query_pool if self.coverage_union else None,
                )
                if rollup_meta:
                    pack["section_rollup"] = rollup_meta
                pack["fact_rescue"] = rescued_meta
        # Retrieval returned up to k candidates; ranks beyond pack_limit never
        # reach pack assembly — record them so the drop is a visible signal
        # instead of vanishing between retrieve() and the pack (2026-10-15).
        for rank, (unit_index, score) in enumerate([] if full_candidate_pool else ranked[pack_limit:], pack_limit + 1):
            if len(pack.get("dropped_candidates", [])) >= 5:
                break
            unit = self.index.units[unit_index]
            pack.setdefault("dropped_candidates", []).append({
                "doc_uid": unit.doc_uid,
                "block_id": unit.block_id or "",
                "rank": rank,
                "score": round(float(score), 4),
                "reason": "pack_limit",
            })
        # Pre-ranking hard exclusions get their own visible records with the
        # mismatch kinds — a refusal must be able to say WHY nothing applied.
        for drop in (hard_drops or [])[:5]:
            if len(pack.get("dropped_candidates", [])) >= 10:
                break
            pack.setdefault("dropped_candidates", []).append({
                "doc_uid": drop.get("doc_uid", ""),
                "block_id": drop.get("block_id", ""),
                "rank": 0,
                "score": drop.get("score", 0.0),
                "reason": "+".join(drop.get("kinds", [])) or "hard_applicability",
            })
        if self.stitch_neighbors:
            self._stitch_dangling(pack)
        if self.doc_completion:
            self._doc_completion(pack)
        return pack

    def _doc_blocks(self, uid: str) -> list[dict[str, Any]]:
        """One document's blocks in reading order (cached per engine)."""
        cache = getattr(self, "_doc_block_cache", None)
        if cache is None:
            cache = self._doc_block_cache = {}
        if uid not in cache:
            cache[uid] = sorted(
                (b for b in self.corpus.blocks if b.get("doc_uid") == uid),
                key=lambda b: b.get("ordinal", 0),
            )
        return cache[uid]

    _DANGLING_END_RE = _PROFILE.pattern("stitch", "dangling_end")
    _DANGLING_START_RE = _PROFILE.pattern("stitch", "dangling_start")

    def _stitch_dangling(self, pack: dict[str, Any], *, max_chars: int = 0) -> None:
        max_chars = max_chars or int(_PROFILE.text("stitch", "max_neighbor_chars", "600"))
        """Pull ONE adjacent block for evidence cut at a rule/exception seam.

        Extraction splits policy prose into blocks at layout boundaries; a
        block ending "…特殊情况如下：" or starting "此外/其中/若…" reads as
        half a rule.  Stitching appends (or prepends) the immediate neighbour
        — bounded, recorded on the item, and deliberately AFTER coverage was
        computed: the gate stays conservative, only the generator's view is
        completed.  Synthetic rollup hits (``:rollup`` ids) never stitch.
        """
        for item in pack.get("evidence", []):
            chunk_id = str(item.get("chunk_id") or "")
            if not chunk_id or chunk_id.endswith(":rollup"):
                continue
            blocks = self._doc_blocks(str(item.get("doc_id") or ""))
            pos = next((k for k, b in enumerate(blocks) if b.get("block_id") == chunk_id), None)
            if pos is None:
                continue
            text = str(item.get("text") or "")
            stitched: list[str] = []
            if self._DANGLING_END_RE.search(text) and pos + 1 < len(blocks):
                nxt = str(blocks[pos + 1].get("text") or "").strip()
                if 0 < len(nxt) <= max_chars:
                    item["text"] = text + "\n" + nxt
                    item["quote"] = item["text"][:1200]
                    stitched.append(str(blocks[pos + 1].get("block_id") or ""))
            elif self._DANGLING_START_RE.match(text) and pos > 0:
                prev = str(blocks[pos - 1].get("text") or "").strip()
                if 0 < len(prev) <= max_chars:
                    item["text"] = prev + "\n" + text
                    item["quote"] = item["text"][:1200]
                    stitched.insert(0, str(blocks[pos - 1].get("block_id") or ""))
            if stitched:
                item["stitched_blocks"] = stitched

    def _doc_completion(self, pack: dict[str, Any], *, max_chars: int = 0, max_docs: int = 0) -> None:
        """Widen a packed unit to a bounded window of its own document.

        The sm-0082 class of miss: the RIGHT document ranks #1 but the block
        holding the asked fact lives in the same doc, shares ~no query term
        and loses every channel — not truncated by the pack, never a
        candidate.  Completion merges the doc's sibling blocks (reading
        order, per-doc char budget) into the FIRST packed item of that doc so
        the generator sees — and can cite — the fact.  Like stitching it runs
        AFTER coverage/decision: the gate stays conservative, only the
        generator's view is completed.  Siblings packed as their own items
        (or stitched into this one) keep their text and are never duplicated.
        """
        max_chars = max_chars or int(_PROFILE.text("doc_completion", "max_chars", "1200"))
        max_docs = max_docs or int(_PROFILE.text("doc_completion", "max_docs", "4"))
        packed_elsewhere: dict[str, set[str]] = {}
        for item in pack.get("evidence", []):
            doc = str(item.get("doc_id") or "")
            if doc:
                packed_elsewhere.setdefault(doc, set()).add(str(item.get("chunk_id") or ""))
        added_docs = 0
        added_chars = 0
        for item in pack.get("evidence", []):
            if added_docs >= max_docs:
                break
            if item.get("completed_blocks") is not None:
                continue  # re-run guard (budget tests call this directly)
            chunk_id = str(item.get("chunk_id") or "")
            doc = str(item.get("doc_id") or "")
            if not chunk_id or chunk_id.endswith(":rollup") or not doc:
                continue
            blocks = self._doc_blocks(doc)
            if len(blocks) <= 1:
                continue
            skip = set(packed_elsewhere.get(doc, set())) | set(item.get("stitched_blocks") or [])
            budget = max_chars
            brought: set[str] = set()
            for block in blocks:
                bid = str(block.get("block_id") or "")
                if bid == chunk_id or bid in skip or bid in brought:
                    continue
                text = str(block.get("text") or "").strip()
                if not text or len(text) > budget:
                    continue
                brought.add(bid)
                budget -= len(text)
            if not brought:
                continue
            parts: list[str] = []
            for block in blocks:
                bid = str(block.get("block_id") or "")
                if bid == chunk_id:
                    parts.append(str(item.get("text") or ""))
                elif bid in brought:
                    parts.append(str(block.get("text") or "").strip())
            item["text"] = "\n".join(part for part in parts if part)
            item["quote"] = item["text"][:1200]
            item["completed_blocks"] = sorted(brought)
            added_docs += 1
            added_chars += max_chars - budget
        if added_docs:
            pack["doc_completion"] = {"docs": added_docs, "chars_added": added_chars}

    def answer_context(
        self,
        question: str,
        today: date | str | None = None,
        user_context: dict[str, Any] | None = None,
        *,
        k: int = 10,
        pack_limit: int = 8,
        retrieval_query: str | None = None,
        retrieval_queries: list[str] | None = None,
    ) -> dict[str, Any]:
        """Answer the contract payload for one question (see runtime.contract).

        ``retrieval_query`` (optional, user request 2026-10-15): LLM-optimized
        BM25 keywords.  IR parsing and applicability filtering always run
        against the original ``question``; pack TERM coverage additionally
        unions the retrieval variants (2026-10-16) so a cross-lingual
        question's translated restatement also counts as covering the need.
        """
        started = time.perf_counter()
        reference_day = _normalize_today(today)
        ir = parse_query(question, today=reference_day)
        merged_ir, pack_context, filled = merge_user_context(ir, user_context)
        queries = list(dict.fromkeys(q.strip() for q in (retrieval_queries or []) if q.strip()))
        if len(queries) > 6 or any(len(q) > 2000 for q in queries):
            raise ValueError("retrieval_queries must contain at most six queries of at most 2000 characters")
        # Coverage pool (2026-10-16 cross-lingual fix): the LLM-optimized
        # variants only.  Pack required terms are the question UNION these
        # paraphrases, so a Chinese question is judged by its English
        # restatement too.  Date-form variants stay retrieval-only — they are
        # lexical workarounds, not semantic paraphrases of the need.
        coverage_pool = list(dict.fromkeys([*queries, *([retrieval_query.strip()] if retrieval_query and retrieval_query.strip() else [])]))
        # Failure-analysis fix 3: deterministic date-form variants join the
        # lexical pool (same single retrieval round, original IR throughout).
        date_variants = _date_query_variants(question)
        if date_variants:
            queries = list(dict.fromkeys([*queries, *date_variants]))[:9]
        # Soft-term lexical bridge (2026-10-17, trap-curr-01): a "最新" query
        # resolves a term key whose digits never appear in the question text,
        # so zh BM25 has nothing to match in English titles — unlike explicit
        # term queries ("2026年秋季…" → the bare "2026" bridges lexically).
        # Inject the term's canonical surface forms as retrieval-only variants,
        # the same trick the date-form bridge above uses; coverage pool untouched.
        # GATED on recurrence: only when the BASE pool (original question, no
        # bridge) already contains current-term docs — i.e. the topic really is
        # a per-semester series.  An unconditioned bridge floods an old-series
        # "最新的交换申请" with 2026-fall docs it then hard-promotes over the
        # series' true latest (2024) — self-defeating (caught live same day).
        if merged_ir.terms and getattr(merged_ir, "relative_term", ""):
            # top-10, not the whole pool: a stray current-term doc deep in the
            # candidate list must not arm the bridge for an unrelated old series.
            probe_hits = self.retrieve(question, ir=merged_ir, today=reference_day, k=10)
            meta_of = self.corpus.doc_metadata
            probe_terms = [t for i, _ in probe_hits for t in (meta_of(self.index.units[i].doc_uid).get("terms") or [])]
            if any(t in merged_ir.terms for t in probe_terms):
                year, season = merged_ir.terms[0].split("-")
                en_form = f"{'Spring' if season == 'spring' else 'Fall'} {year}"
                abbr_form = f"{'SP' if season == 'spring' else 'FA'}{year[2:]}"
                queries = list(dict.fromkeys([*queries, f"{question} {en_form}", f"{question} {abbr_form}"]))[:9]
        # Cross-lingual alias bridge (2026-10-18, deterministic): a zh question
        # against the English corpus leaves the lexical channel mute (zh tokens
        # match nothing in English text), so RRF degenerates to dense-only and
        # paraphrase-heavy correspondences ("退课截止日期" vs "submit a request by
        # the end of 8th week") lose to term-matched schedules.  The profile's
        # aliases table appends retrieval-only keyword variants — same pool,
        # same mechanism as the bridges above.  Fires on key substrings in the
        # question regardless of script (ECE/CompE are latin keys); coverage
        # pool untouched; en questions without the keys stay bit-identical.
        alias_terms: list[str] = []
        if re.search(r"[一-鿿]", question):  # zh questions only — en must stay bit-identical
            for group in _PROFILE.entries("aliases"):
                if any(key in question for key in group.get("keys", [])):
                    alias_terms.extend(term for term in group.get("terms", []) if term not in alias_terms)
        if alias_terms:
            queries = list(dict.fromkeys([*queries, f"{question} {' '.join(alias_terms[:8])}"]))[:9]
        trace = []
        accumulated_hard_drops: list[dict[str, Any]] = []
        if queries:
            # All query variants retain the original question's applicability IR.
            fused: dict[int, float] = {}
            scores: dict[int, float] = {}
            for query in queries:
                hits = self.retrieve(query, ir=merged_ir, today=reference_day, k=k)
                accumulated_hard_drops.extend(self.last_hard_drops)
                trace.append({"query": query, "candidates": [
                    {"doc_uid": self.index.units[i].doc_uid, "block_id": self.index.units[i].block_id,
                     "rank": rank, "score": float(score)}
                    for rank, (i, score) in enumerate(hits, 1)
                ]})
                for rank, (i, score) in enumerate(hits, 1):
                    fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank)
                    scores[i] = max(scores.get(i, float("-inf")), score)
            # RRF determines order; retain native support scores for pack scoring.
            # Fusion re-imposes the per-document cap the single channels each
            # enforce (BM25 max_per_doc=2): without it a zh query's lexical
            # channel gives script-matching bilingual forms double credit and
            # one document swallows the top-8 (live: LeaveOfAbsence ×4 while
            # the actual guideline sat at rank 9+, 2026-10-16 C4 losses).
            ranked = [(i, scores[i]) for i in sorted(fused, key=lambda i: (-fused[i], i))][:50]
        else:
            ranked = self.retrieve(retrieval_query or question, ir=merged_ir, today=reference_day, k=k)
            accumulated_hard_drops.extend(self.last_hard_drops)
            # Uniform trace shape: the single-query path records its one round
            # too, so "trace missing" means missing everywhere (2026-10-16).
            trace.append({"query": retrieval_query or question, "candidates": [
                {"doc_uid": self.index.units[i].doc_uid, "block_id": self.index.units[i].block_id,
                 "rank": rank, "score": float(score)}
                for rank, (i, score) in enumerate(ranked, 1)
            ]})
        pack = self.build_pack(question, ranked, today=reference_day, ir=merged_ir, context=pack_context,
                               pack_limit=pack_limit, full_candidate_pool=bool(queries), hard_drops=accumulated_hard_drops,
                               query_pool=coverage_pool)
        payload = assemble_answer_context(
            question=question,
            today=reference_day.isoformat() if reference_day else None,
            user_context=user_context,
            merged_ir=merged_ir,
            pack=pack,
            engine_meta={
                "build_id": self.corpus.build_id,
                "corpus_root": str(self.corpus.root),
                "config": self.config,
                "rerank_model": self.rerank_model,
                "dense_model": self.dense_model,
                "n_units": len(self.index.units),
                "n_documents": len(self.corpus.documents),
                "n_known_conflicts": len(self.known_conflicts),
            },
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        payload["user_context_filled"] = filled
        payload["retrieval_trace"] = {"strategy": "multi_query_rrf" if queries else "single_query",
                                      "queries": queries or [retrieval_query or question], "rounds": trace}
        return payload

    # ------------------------------------------------------------ introspection

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": ANSWER_CONTEXT_VERSION,
            "build_id": self.corpus.build_id,
            "config": self.config,
            "corpus_root": str(self.corpus.root),
            "n_units": len(self.index.units),
            "n_documents": len(self.corpus.documents),
            "n_known_conflicts": len(self.known_conflicts),
            "rerank_model": self.rerank_model,
            "dense_model": self.dense_model,
        }
