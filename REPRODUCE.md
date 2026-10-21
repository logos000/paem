# Reproducing the reported experiments

This file maps each reported result to the code that produces it, and states the
honest status of each. Read [DATA_AVAILABILITY.md](DATA_AVAILABILITY.md) first:
the archives are not redistributed, so the frozen numbers cannot be regenerated
from this repository alone.

Legend: **[here]** runs on this repository with no corpus · **[corpus]** needs an
archive you supply · **[restricted]** cannot be regenerated without the private
corpora and frozen question sets.

## 0. Environment

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ./llm-corpus-builder -e ./eval-suite -e ./academic-affairs-product
export PYTHONPATH="$PWD/llm-corpus-builder:$PWD/eval-suite:$PWD/academic-affairs-product"
pytest llm-corpus-builder eval-suite academic-affairs-product
```

Expected: `300 passed, 1 skipped` · `38 passed, 1 xfailed` · `95 passed`.
The skip needs a corpus build; the xfail is a stale expectation left visible on
purpose (see `eval-suite/tests/test_harness_diagnostics.py`).

Copy `.env.example` to `.env` in `llm-corpus-builder/` and
`academic-affairs-product/` and fill in your own OpenAI-compatible endpoint and
key. `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` are the three that matter.

## 1. Build-time annotation gate (main text 2.1; Fig. 2C)

**[here]** unit level

```bash
pytest llm-corpus-builder/tests/test_annotate.py -q
```

**[corpus]** field-level review over your own archive

```bash
python llm-corpus-builder/annotate/run_pilot.py --corpus <build> --sample 300
python llm-corpus-builder/annotate/review_fields.py --corpus <build>
```

The reported span-alignment rates (96.7% rule-rebuilt row context, 71.7% version
series, 16.1% free summaries) are properties of the reviewed archive. The review
procedure and the accept/fix/reject accounting are identical.

## 2. Eight-arm materialization ladder and Track A (main text 2.2, Table 1, Fig. 3)

**[corpus + restricted]** — the ladder needs a built corpus; the 92-question
frozen set is not published, so the numbers will differ.

```bash
python llm-corpus-builder/pipeline.py --source <snapshot> --out data/corpus/<build>
python llm-corpus-builder/bench/track_a.py --corpus data/corpus/<build> --eval <your_questions>.jsonl
```

Arm definitions (`raw`, `clean`, `active`, `active_ir`, `metadata_ir`,
`validated_ir`, `active_ir_rr`, `validated_ir_rr`) live in
`llm-corpus-builder/export/arms.py`; the ladder is reproducible up to the corpus.

## 3. Corpus–retriever interaction matrix (main text 2.2, Fig. 3A–B)

**[corpus]** — four retrievers x two corpora, no frozen questions needed beyond
your own set.

```bash
python llm-corpus-builder/bench/track_b.py --corpus data/corpus/<build> --eval <your_questions>.jsonl
```

## 4. Runtime contract and claim validation (main text 2.3, Fig. 4A)

**[here]** contract, hash stability, validator and guard

```bash
pytest llm-corpus-builder/tests/test_runtime_engine.py -q
pytest academic-affairs-product/tests/test_p1_answers.py -q
pytest academic-affairs-product/tests/test_product.py -q
```

**[corpus]** live service

```bash
python -m runtime.service --corpus data/corpus/<build> --port 8772
curl -s localhost:8772/health
```

The `answer_context v2` payload is specified by
`llm-corpus-builder/runtime/answer_context.schema.json`. `pack_hash` covers the
canonical projection of question, merged Query IR, decision and each evidence
item's identity; retrieval traces and timings are deliberately excluded, so the
same evidence reached by a different path is the same pack.

## 5. Governance matrix: hard front gate vs advisory (main text 2.3, Fig. 4B)

**[restricted]** — the 289-question superset is not published. The arm
definitions and the runner are in `eval-suite/scripts/release_gate_v1.py`;
`academic-affairs-product/product/flow.py` shows how advisory telemetry is
surfaced without a front-door veto.

## 6. Judge protocol and citation hard cap (main text 2.3–2.4; SI S5)

**[here]**

```bash
pytest eval-suite/tests/test_llm_judge.py -q
```

The scorer is `eval-suite/evalsuite/llm_judge/scoring.py`: the full
`evidence-judge-v3.1` prompt, the dimension gating (`null` when a reference is
invalid or evidence is missing), and the deterministic cap that holds
`citation_quality` at or below 2 when dangling or blocked ids are detected —
before any model call is made.

## 7. Five-system comparison (main text 2.4, Table 2, Fig. 5)

**[corpus + restricted]** — same archive, same question set and same judge are
required for the comparison to mean anything.

| Arm | Script |
|---|---|
| PAEM | `academic-affairs-product/scripts/launch.ps1` + `eval-suite/scripts/llm_judge.py` |
| RAGFlow v1.0.0-rc2 | `eval-suite/scripts/ragflow_baseline.py` |
| LangChain hybrid | `eval-suite/scripts/langchain_baseline.py` |
| Long-context whole-corpus | `eval-suite/scripts/longcontext_run.py` |
| Closed-book | `eval-suite/scripts/closedbook_run.py` |

Baseline credentials and endpoints are read from the environment
(`RAGFLOW_API`, `RAGFLOW_EMAIL`, `RAGFLOW_PASSWORD`, `LLM_BASE_URL`); nothing is
hard-coded.

## 8. Cross-domain transfer and dual-domain merge (main text 2.5, Table 3, Fig. 6)

**[corpus + restricted]** — requires the second archive and its 743-question set.

```bash
python llm-corpus-builder/scripts/update_cycle.py --profile <other-domain>.yaml
```

The claim under test is structural: swapping only the declaration-style domain
profile (term windows, code patterns, version suffixes, alias table) and touching
no mechanism code. To check that claim on your own data, diff the profile against
`llm-corpus-builder/profiles/` and confirm no branch in `structure/`, `curate/`,
`retrieve/` or `runtime/` reads a domain literal.

## 9. Statistical reporting

Confidence intervals are bootstrap percentiles; paired comparisons use McNemar on
the per-question outcome table. Both are in
`eval-suite/scripts/ablation_report_v1.py`; the inputs are per-question result
files, so they can be recomputed for any run.

## Known limitations of this release

- No corpus, no frozen question sets: absolute numbers in the manuscript cannot be
  regenerated here. The manuscript's availability section states the same.
- `llm-corpus-builder/docs/FROZEN-92-MASTER-RESULTS-2026-10-14.md` records the
  frozen-table values for reference; it is documentation, not a runnable check.
- The product launchers default to the Windows paths used during development. Set
  `BUILDER_DIR`, `PROVIDER_DIR`, `CORPUS_DIR` and `INBOX_DIR` to relocate them.
