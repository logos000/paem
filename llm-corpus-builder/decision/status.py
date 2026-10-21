"""Six-state abstention decision distilled from evidence-pack signals.

:func:`evidence.pack.build_evidence_pack` historically ended with a single
boolean, ``abstain``, plus one of five refusal strings.  That decision is
extracted here unchanged and refined with a status label so downstream
consumers (telemetry, chat UI, claim verifier) can tell *why* a pack looks the
way it does without re-deriving the rule.  This module deliberately imports
nothing from ``evidence`` so the dependency stays one-directional.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, TypedDict

EvidenceStatus = Literal[
    "sufficient",
    "no_material",
    "wrong_term",
    "wrong_audience",
    "stale_or_uncertain",
    "conflict",
]

#: Cross-document version conflicts produced by ``pack._version_conflicts``:
#: a superseded edition is blocked because the newer edition of the same
#: series is present in the pack.
BLOCKING_CONFLICT_KINDS = frozenset({"superseded"})
#: An edition was superseded but its newer version was not retrieved.
STALE_RISK_KIND = "stale_risk"
#: P3: two current sources assert disjoint facts (dates/credits/…) — the pack
#: keeps both sides and the answer must surface the disagreement.
FACT_CONFLICT_KINDS = frozenset({"fact_conflict", "known_conflict"})
#: Historical coverage floor of ``build_evidence_pack``; kept verbatim.
# Ablation (2026-10-16): the floor is the abstention gate the naive-RAG
# baseline removes entirely (RUNTIME_COVERAGE_FLOOR=0).
COVERAGE_FLOOR = float(__import__("os").environ.get("RUNTIME_COVERAGE_FLOOR", "0.12"))

REFUSAL_REASONS = {
    "no_material_evidence": "no applicable supporting evidence",
    "term_gap": "requested term has no applicable evidence",
    "audience_mismatch": "requested material targets a different audience",
    "scope_gap": "retrieved evidence does not cover the requested scope",
    "low_support": "low lexical/semantic support",
}


class Decision(TypedDict):
    """Outcome of :func:`decide`; the last two fields are parity-frozen."""

    status: EvidenceStatus
    abstain: bool
    abstain_reason: str


def _has_kind(entries: Iterable[dict[str, Any]] | None, kinds: frozenset[str]) -> bool:
    return any(entry.get("kind") in kinds for entry in entries or [])


def decide(
    *,
    usable_count: int,
    coverage: float,
    scope_gap: dict[str, Any] | None = None,
    constraint_gaps: Iterable[dict[str, Any]] | None = None,
    version_conflicts: Iterable[dict[str, Any]] | None = None,
    stale_risk_conflicts: Iterable[dict[str, Any]] | None = None,
    fact_conflicts: Iterable[dict[str, Any]] | None = None,
    coverage_floor: float = COVERAGE_FLOOR,
    hard_drop_kinds: list[str] | None = None,
    advisory: bool = False,
) -> Decision:
    """Enforce-mode decision, or its advisory shadow (2026-10-17).

    ``advisory=True`` keeps every computation — the status label still records
    what the gates WOULD have refused (no_material / wrong_term / …) — but
    withholds the veto: ``abstain`` is always False and the generator receives
    the pack with only the honesty prompt deciding.  Enforce mode is
    parity-frozen and byte-identical to the historical output.
    """
    result = _decide_enforced(
        usable_count=usable_count,
        coverage=coverage,
        scope_gap=scope_gap,
        constraint_gaps=constraint_gaps,
        version_conflicts=version_conflicts,
        stale_risk_conflicts=stale_risk_conflicts,
        fact_conflicts=fact_conflicts,
        coverage_floor=coverage_floor,
        hard_drop_kinds=hard_drop_kinds,
    )
    if advisory:
        result = {**result, "abstain": False, "abstain_reason": ""}
    return result


def _decide_enforced(
    *,
    usable_count: int,
    coverage: float,
    scope_gap: dict[str, Any] | None = None,
    constraint_gaps: Iterable[dict[str, Any]] | None = None,
    version_conflicts: Iterable[dict[str, Any]] | None = None,
    stale_risk_conflicts: Iterable[dict[str, Any]] | None = None,
    fact_conflicts: Iterable[dict[str, Any]] | None = None,
    coverage_floor: float = COVERAGE_FLOOR,
    hard_drop_kinds: list[str] | None = None,
) -> Decision:
    """Map evidence-pack signals onto a six-way status plus the legacy abstain pair.

    ``abstain`` and ``abstain_reason`` reproduce ``build_evidence_pack``'s
    historical output verbatim (there is a parity baseline on the 48-question
    eval set); ``status`` only labels the surviving cases.  Rules, in exactly
    the historical precedence order — the first match wins:

    ========  ===========================  ========  ===============================================
    Priority  Signal                       Status    ``abstain_reason``
    ========  ===========================  ========  ===============================================
    1         ``usable_count == 0``        no_material      ``"no applicable supporting evidence"``
    2         gap ``term-gap``             wrong_term       ``"requested term has no applicable evidence"``
    3         gap ``audience-mismatch``    wrong_audience   ``"requested material targets a different audience"``
    4         ``scope_gap``                wrong_term       ``"retrieved evidence does not cover the requested scope"``
    5         ``coverage < coverage_floor`` no_material     ``"low lexical/semantic support"``
    6         blocking version conflict    conflict         ``""`` (no abstain)
    6b        fact conflict (P3)           conflict         ``""`` (no abstain)
    7         ``stale_risk`` conflict      stale_or_uncertain  ``""`` (no abstain)
    8         otherwise                    sufficient       ``""`` (no abstain)
    ========  ===========================  ========  ===============================================

    Status notes:

    - Rule 4 (scope gap): usable evidence exists but answers a narrower
      population than the question asks about — semantically the closest kin
      of a term mismatch, hence ``wrong_term``.
    - Rule 5 (low coverage): the signal is lexical/semantic poverty, i.e.
      effectively nothing usable was retrieved for this question.  Calling it
      ``stale_or_uncertain`` would be a stretch (nothing says the evidence is
      outdated or contradictory), so it is filed under ``no_material``.
    - Rules 6-7 are new, purely informational labels: usable evidence
      answers the question, so ``abstain`` stays ``False``.  Rule 6 marks a
      pack that also contains a blocked superseded edition (a cross-document
      conflict hint); rule 7 marks a pack built only from an edition whose
      newer version was never retrieved.
    - Rule 6b (P3): two current editions of one series, or a build-detected
      fact conflict between two live documents, assert disjoint facts.  The
      answer must present both sides, so this labels the pack ``conflict``
      without abstaining — same legacy abstain outputs as rules 6-8.
    """
    gaps = list(constraint_gaps or [])
    if usable_count <= 0:
        # Step 7 (taxonomy v1): when the only lexically relevant candidates
        # were hard-excluded pre-ranking, the refusal names the applicable
        # reason instead of a generic "no material" — the corpus HAS material,
        # it just does not apply to the asked year/audience.
        kinds = set(hard_drop_kinds or [])
        if "wrong_year_hard" in kinds:
            return {"status": "wrong_term", "abstain": True, "abstain_reason": REFUSAL_REASONS["term_gap"]}
        if "wrong_audience_hard" in kinds:
            return {"status": "wrong_audience", "abstain": True, "abstain_reason": REFUSAL_REASONS["audience_mismatch"]}
        return {"status": "no_material", "abstain": True, "abstain_reason": REFUSAL_REASONS["no_material_evidence"]}
    if any(gap.get("kind") == "term-gap" for gap in gaps):
        return {"status": "wrong_term", "abstain": True, "abstain_reason": REFUSAL_REASONS["term_gap"]}
    if any(gap.get("kind") == "audience-mismatch" for gap in gaps):
        return {"status": "wrong_audience", "abstain": True, "abstain_reason": REFUSAL_REASONS["audience_mismatch"]}
    if scope_gap:
        return {"status": "wrong_term", "abstain": True, "abstain_reason": REFUSAL_REASONS["scope_gap"]}
    if coverage < coverage_floor:
        return {"status": "no_material", "abstain": True, "abstain_reason": REFUSAL_REASONS["low_support"]}
    if _has_kind(version_conflicts, BLOCKING_CONFLICT_KINDS):
        return {"status": "conflict", "abstain": False, "abstain_reason": ""}
    if _has_kind(fact_conflicts, FACT_CONFLICT_KINDS):
        return {"status": "conflict", "abstain": False, "abstain_reason": ""}
    if _has_kind(stale_risk_conflicts, frozenset({STALE_RISK_KIND})):
        return {"status": "stale_or_uncertain", "abstain": False, "abstain_reason": ""}
    return {"status": "sufficient", "abstain": False, "abstain_reason": ""}
