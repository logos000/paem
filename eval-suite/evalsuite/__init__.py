"""Evaluation & question-set suite — the fourth repo of the ZJUI QA project.

Owns everything question-set and metrics: question generation (questiongen),
lint/verify/provenance pipelines, the retrieval harness, product E2E runs,
and the benchmark data.  It deliberately does NOT own any retrieval code:
the harness must measure the builder's SINGLE path, so builder modules
(retrieve/runtime/evidence/annotate) are imported through the path bridge
below — same code the offline harness and the online service share
(the Stage 3A zero-drift guarantee).

Path layout (sibling repos under one parent by default):

    ../llm-corpus-builder   retrieval/evidence runtime (imported, not copied)
    ./data/benchmarks/      eval sets, reviews, results (moved from builder)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SUITE_ROOT = Path(__file__).resolve().parents[1]


def builder_root() -> Path:
    """llm-corpus-builder location (BUILDER_ROOT env overrides the sibling default)."""
    return Path(os.getenv("BUILDER_ROOT", str(SUITE_ROOT.parent / "llm-corpus-builder")))


def _ensure_builder_on_path() -> None:
    root = str(builder_root())
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_builder_on_path()
