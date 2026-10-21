# PAEM — Provenance-Anchored Evidence Materialization

Implementation of **PAEM**, a framework that rewrites institutional archives into
provenance-anchored, self-contained evidence carriers before any query is asked,
and then keeps evidence adjudication strictly separate from natural-language
generation at answer time.

The accompanying manuscript studies a bilingual academic-affairs archive as a
regulatory sandbox. This repository contains the implementation, the evaluation
protocol and the reproducible test suites described there.

> **Source documents are not distributed.** The corpora are real institutional
> archives. See [DATA_AVAILABILITY.md](DATA_AVAILABILITY.md) for what is and is
> not reproducible from this repository.

## What is here

Four components, resolved as sibling directories because the code addresses them
relatively (`../llm-corpus-builder`, `../eval-suite`, ...):

| Directory | Role |
|---|---|
| `raw-material-provider/` | Content-addressed intake: SHA-256 snapshot of web pages, attachments and Office files; inbox watcher |
| `llm-corpus-builder/` | Nine-stage build pipeline (snapshot gate, layout repair, table binding, version chains, template triage, four-strata materialization), the runtime engine (Query IR, hybrid retrieval, in-document completion, EvidencePack), and build-time anchor verification |
| `eval-suite/` | Benchmark generation, the `evidence-judge-v3.1` scoring protocol, ablation runners, and the external baselines (RAGFlow, LangChain, long-context, closed-book) |
| `academic-affairs-product/` | HTTP product layer: answer flow, deterministic claim validation, session handling, Vite web UI |

## Key ideas in code

- **Four strata per block** — raw / clean / retrieval / evidence, with `clean`
  frozen as the only quotable base (`llm-corpus-builder/structure/`,
  `llm-corpus-builder/export/arms.py`).
- **Table row self-containment** — multi-level headers, captions and units are
  bound into each row so a bare `26 | 7` becomes a citable unit
  (`llm-corpus-builder/structure/extract.py`).
- **Anchor gate** — build-time model labels enter the index only when their claim
  is a verbatim span of the clean text
  (`llm-corpus-builder/validate/anchor.py`).
- **Immutable EvidencePack** — a canonical SHA-256 `pack_hash` over the hashed
  projection of query, evidence and decision
  (`llm-corpus-builder/runtime/contract.py`).
- **Deterministic claim validation** — dates, times, course codes and
  number@dimension pairs are checked against the cited spans, with an
  evidence-free-sentence guard
  (`academic-affairs-product/product/claim_validator.py`).
- **Citation-integrity hard cap** — dangling or blocked citation ids cap the
  citation-quality dimension in the judge
  (`eval-suite/evalsuite/llm_judge/scoring.py`).

## Install and test

Python 3.11+. Each component has its own `pyproject.toml`; a single virtualenv at
the repository root is enough.

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ./llm-corpus-builder -e ./eval-suite -e ./academic-affairs-product

export PYTHONPATH="$PWD/llm-corpus-builder:$PWD/eval-suite:$PWD/academic-affairs-product"
pytest llm-corpus-builder eval-suite academic-affairs-product
```

Measured on this tree with no corpus present:

| Suite | Result |
|---|---|
| `llm-corpus-builder` | **300 passed, 1 skipped** |
| `eval-suite` | **38 passed, 1 xfailed** |
| `academic-affairs-product` | **95 passed** |

The single `xfail` is a stale expectation kept visible rather than rewritten; the
skipped test requires a corpus build.

Configuration is read from `.env`; copy the per-component `.env.example` and fill
in your own endpoint and key. No credentials are committed.

## Reproducibility

See [REPRODUCE.md](REPRODUCE.md) for which reported tables and figures can be
regenerated here, and which require the restricted corpora.

## 中文说明

PAEM 把制度档案在**查询发生之前**重写为溯源锚定、自包含的证据载体，并在回答阶段把
「证据适用性裁决」与「自然语言生成」彻底解耦。本仓包含四个组件：原始材料摄入、
九阶段语料构建与检索运行时、评测题集与盲审协议、产品层与确定性事实核验。

**原始档案（真实公文与学生手册）不随仓库分发**，因此论文中的绝对数值无法仅凭本仓
复现；本仓提供的是完整实现、评测协议，以及不依赖语料的确定性部分——三套单元测试
在无任何语料的情况下全部通过。可复现范围见 [REPRODUCE.md](REPRODUCE.md)。

## License

Code is released under the MIT License (see [LICENSE](LICENSE)). Third-party
documents used in the study remain under their own terms and are not
redistributed here.

## Citation

See [CITATION.cff](CITATION.cff).
