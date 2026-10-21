# What this repository does and does not let you reproduce

The study is built on two real institutional archives: a bilingual
international-college operating archive (English course guides, program plans,
schedules) and a university-level regulatory document collection (Chinese formal
notices and handbooks). Neither is redistributed here: they are institutional
records that contain personal and operational data, and they are not ours to
republish.

This file states precisely what can and cannot be regenerated from this
repository, so no claim in the manuscript rests on an unreproducible step taken
on trust.

## Reproducible here, with no corpus

| Item | How |
|---|---|
| Build pipeline stage semantics (snapshot gate, table binding, version chains, template triage, four strata) | `pytest llm-corpus-builder` — 300 tests over synthetic fixtures |
| Anchor-gate acceptance and rejection paths | `llm-corpus-builder/tests/test_annotate.py`, `llm-corpus-builder/validate/anchor.py` |
| EvidencePack contract and `pack_hash` stability | `llm-corpus-builder/tests/test_runtime_engine.py`, `runtime/contract.py` |
| Query IR parsing, audience/term resolution, unit normalisation | `llm-corpus-builder/tests/test_query_ir.py` |
| Deterministic claim validation (dates, times, course codes, number@dimension) | `academic-affairs-product/tests/test_p1_answers.py` |
| Evidence-free-sentence guard and citation redaction | `academic-affairs-product/tests/test_product.py` |
| Judge protocol: prompt, dimension gating, citation-integrity cap, response parsing | `eval-suite/tests/test_llm_judge.py`, `evalsuite/llm_judge/scoring.py` |
| Question-pipeline linting and reference audit | `eval-suite/tests/test_questiongen.py`, `tests/test_reference_audit.py` |

All of the above are exercised by the three suites listed in the README
(433 tests pass on a clean checkout with no corpus).

## Reproducible here, but needs a corpus you must supply

Everything below runs unchanged on any archive that provides the same intake
contract (a snapshot manifest plus text/PDF/DOCX/XLSX objects). Absolute numbers
will differ from the manuscript, because the corpus differs.

| Item | Entry point |
|---|---|
| Full nine-stage build and the eight-arm materialization ladder | `llm-corpus-builder/pipeline.py`, `llm-corpus-builder/bench/track_a.py` |
| Corpus-vs-retriever interaction matrix | `llm-corpus-builder/bench/track_a.py`, `bench/track_b.py` |
| Runtime service and the `answer_context v2` payload | `llm-corpus-builder/runtime/service.py` |
| End-to-end scoring over a question set | `eval-suite/scripts/llm_judge.py` |
| External baselines | `eval-suite/scripts/ragflow_baseline.py`, `langchain_baseline.py`, `longcontext_run.py`, `closedbook_run.py` |
| Ablation report assembly | `eval-suite/scripts/ablation_report_v1.py`, `release_gate_v1.py` |
| Product-level end-to-end run | `academic-affairs-product/scripts/launch.ps1`, `scripts/smoke.py` |

An archive of your own needs a declaration-style domain profile
(`llm-corpus-builder/profiles/`) covering term windows, code patterns, version
suffixes and the cross-lingual alias table. Nothing else in the pipeline is
domain-specific.

## Not reproducible from this repository

- **The reported percentages** (`anchor@1 = 56.6%`, `91.7%` core accuracy, the
  five-system comparison, the dual-domain merge gains). These are properties of
  the restricted archives plus the frozen question sets, neither of which is
  published.
- **The frozen question sets** (`verified_eval_v3`, `benchmark_superset_v2_289`,
  `gfxwj_editorial_v1_main`). Their gold answers quote archival passages
  verbatim, so publishing them would republish the corpus. The generation
  pipeline that produced them *is* published, and can build an equivalent set
  over any corpus.
- **The five-system comparison runs.** The harness and the exact baseline
  configuration are published; the answer snapshots are not, for the same reason.

## What can be provided for audit

On request, and subject to the data owner's approval, the following can be made
available to reviewers without public redistribution:

- the SHA-256 snapshot manifests for both archives (hash, source, size — no
  content);
- build manifests and per-stage metrics;
- the frozen question sets in a form that omits verbatim archive text;
- judge ledgers and the independent reference-audit logs.

Requests go to the corresponding author of the manuscript.
