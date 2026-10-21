"""The answer-context contract, validated on the product side (P0).

The runtime's payload (llm-corpus-builder runtime/answer_context.schema.json,
``ANSWER_CONTEXT_VERSION = 2``) is parsed into typed models before anything
downstream touches it.  A schema drift must surface as one stable
``dependency_contract_error`` (502) with the offending fields and the
request_id — never as an unhandled exception in the answer path.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONTRACT_VERSION = 2

DecisionStatus = Literal["sufficient", "no_material", "wrong_term", "wrong_audience", "stale_or_uncertain", "conflict"]

#: The ONE public answer status (2026-10-16 convergence).  decision_status,
#: generation mode, validation problems and traces are diagnostics — callers
#: must never recombine booleans into a status of their own.
AnswerStatus = Literal["answered", "partial", "refused_evidence", "refused_policy", "conflict", "unverified"]


class ContractMismatch(ValueError):
    """The runtime payload does not satisfy contract v1."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:8]))
        self.problems = problems


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: DecisionStatus
    abstain: bool
    reason: str = ""


class ProvenanceItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    doc_uid: str = ""
    block_id: str = ""
    source_url: str = ""


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    evidence_id: str
    doc_id: str = ""
    title: str = ""
    text: str
    blocking: bool = False
    provenance: ProvenanceItem = Field(default_factory=ProvenanceItem)


class EvidencePackModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    evidence: list[EvidenceItem]
    usable_evidence_ids: list[str] = Field(default_factory=list)
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    evidence_status: DecisionStatus
    abstain: bool
    abstain_reason: str = ""
    coverage: float = 0.0
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    fact_conflicts: list[dict[str, Any]] = Field(default_factory=list)


class RuntimeProvenance(BaseModel):
    model_config = ConfigDict(extra="allow")
    build_id: str
    config: str = ""


class AnswerContextModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: Literal[2]
    question: str
    config: str
    decision: DecisionModel
    pack_hash: str = Field(min_length=64, max_length=64)
    evidence_pack: EvidencePackModel
    provenance: RuntimeProvenance
    latency_ms: float = 0.0
    today: str | None = None
    user_context: dict[str, Any] = Field(default_factory=dict)
    retrieval_trace: dict[str, Any] = Field(default_factory=dict)


def parse_answer_context(raw: Any) -> AnswerContextModel:
    """Validate one runtime payload; raise ContractMismatch with field problems."""
    try:
        return AnswerContextModel.model_validate(raw)
    except ValidationError as error:
        problems = []
        for item in error.errors(include_url=False):
            location = ".".join(str(part) for part in item["loc"]) or "<root>"
            problems.append(f"{location}: {item['msg']}")
        raise ContractMismatch(problems) from error
