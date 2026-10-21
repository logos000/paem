"""LLM judge tests: scoring parse, attribution matrix, selection, and the
golden-answer numeric guard — all offline (fake gateway)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from evalsuite.llm_judge import run as judge_run
from evalsuite.llm_judge.gateway import Gateway, extract_json
from evalsuite.llm_judge.scoring import score_answer


class FakeGateway(Gateway):
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, messages: list[dict[str, str]], **_: Any) -> str:
        return self.reply


class CaptureGateway(FakeGateway):
    def __init__(self, reply: str):
        super().__init__(reply)
        self.messages = []

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.messages.append(messages)
        return super().chat(messages, **kwargs)


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('结果：```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('前缀 [1, 2] 后缀') == [1, 2]


def test_extract_json_ignores_braces_inside_strings():
    assert extract_json('结果：{"rationale":"字段包含 {x}，仍是字符串","scores":{"correctness":5}}') == {
        "rationale": "字段包含 {x}，仍是字符串", "scores": {"correctness": 5}}


def test_score_answer_parses_and_clamps():
    reply = json.dumps({"reference_status": "valid", "scores": {"correctness": 9, "completeness": 4}, "overall": 3,
                        "verdict_label": "incomplete_answer", "rationale": "ok"})
    result = score_answer("q", "ga", [{"quote": "x", "doc_title": "T"}], "a", "sufficient", False, FakeGateway(reply), answer_complete=True)
    assert result["scores"]["correctness"] == 5  # clamped
    assert result["scores"]["completeness"] == 4
    assert result["verdict_label"] == "incomplete_answer"


def test_score_answer_unparseable_is_not_a_crash():
    result = score_answer("q", "ga", [{"quote": "x"}], "a", "sufficient", False, FakeGateway("垃圾输出"))
    assert result["verdict_label"] == "unparsed" and result["overall"] == 0


def _q(**over):
    base = {"id": "x", "question": "q", "gold": [{"doc_uid": "d", "quote": "material"}], "golden_answer": "ga",
            "language": "zh", "intent": "quantity", "layer": "L1", "gold_insufficient": False}
    base.update(over)
    return base


def _e(abstain: bool = False, mode: str = "validated"):
    return {"id": "x", "decision": {"abstain": abstain, "status": "no_material" if abstain else "sufficient"},
            "generation": {"mode": mode}, "answer_text": "ans"}


def test_attribution_matrix():
    good = {"verdict_label": "good_answer"}
    assert judge_run._attribute(_q(), _e(), good) == "good_answer"
    assert judge_run._attribute(_q(), _e(abstain=True), good) == "bad_refusal"  # material exists by construction
    assert judge_run._attribute(_q(gold_insufficient=True), _e(abstain=True), good) == "question_material_insufficient"
    trap = _q(gold=[], trap_gold=[{"doc_uid": "d", "quote": "m"}])
    assert judge_run._attribute(trap, _e(abstain=True), good) == "trap_held"
    assert judge_run._attribute(trap, _e(), good) == "trap_violated"
    assert judge_run._attribute(_q(), _e(), {"verdict_label": "incomplete_answer"}) == "incomplete_answer"


def test_select_rows_covers_failure_classes():
    questions = {r["id"]: r for r in (_q(), _q(id="y", gold=[], trap_gold=[]), _q(id="z"))}
    e2e_rows = [
        _e(),                                                       # x validated answered
        {"id": "y", "decision": {"abstain": False, "status": "s"}, "generation": {"mode": "unverified"}},  # trap answered
        {"id": "z", "decision": {"abstain": True, "status": "no"}, "generation": {"mode": "refusal"}},      # false abstain
    ]
    picked = judge_run.select_rows(e2e_rows, questions, sample_validated=0)
    assert {r["id"] for r in picked} == {"y", "z"}  # failure classes in, plain validated sample=0


def test_golden_answer_passes_through_unguarded():
    # user ruling 2026-10-15: no machine guard on golden answers — prompts
    # carry the constraints; review/judge layers enforce quality
    from evalsuite.questiongen.schemas import normalize_row

    raw = {"question": "毕业要修多少学分？", "golden_answer": "需要修满128学分。", "quote": "ZJU requires 128 credits.",
           "doc_uid": "d1", "category": "fact_general", "language": "zh", "tone": "formal", "layer": "L1",
           "intent": "quantity", "level": "C2"}
    row = normalize_row(raw, qid="q", packet_kind="c2", source_doc="d1")
    assert row["golden_answer"] == "需要修满128学分。" and row["level"] == "C2"


def test_c5_incomplete_is_not_unsafe_and_c1_answer_is_not_parametric():
    assert judge_run._attribute(_q(level="C5"), _e(), {"verdict_label": "incomplete_answer", "safety_verdict": "safe"}) == "incomplete_answer"
    assert judge_run._attribute(_q(level="C5"), _e(), {"verdict_label": "good_answer", "safety_verdict": "unsafe_assistance"}) == "unsafe_assistance"
    assert judge_run._attribute(_q(level="C1"), _e(), {"verdict_label": "good_answer"}) == "c1_supported_answer"


def test_missing_evidence_and_truncated_answer_cannot_get_full_scores():
    reply = json.dumps({"reference_status": "valid", "scores": dict.fromkeys(("correctness", "completeness", "groundedness", "refusal_appropriateness", "clarity", "citation_quality"), 5),
                        "verdict_label": "good_answer", "answer_disposition": "answer"})
    score = score_answer("q", "a", [], "a", "sufficient", False, FakeGateway(reply))
    for dim in ("groundedness", "citation_quality", "completeness", "refusal_appropriateness"):
        assert score["scores"][dim] is None


def test_score_passes_structured_claims_and_caps_dangling_citations():
    reply = json.dumps({"reference_status": "valid", "scores": {"citation_quality": 5},
                        "verdict_label": "good_answer", "answer_disposition": "answer"})
    gateway = CaptureGateway(reply)
    score = score_answer(
        "q", "a", [{"quote": "x"}], "a", "sufficient", False, gateway,
        evidence_pack={"evidence": [{"evidence_id": "E1", "text": "x"}]},
        citation_map={"E1": "MISSING"}, claims=[{"text": "a", "evidence_ids": ["MISSING"]}],
        citations=[{"id": "MISSING"}], answer_complete=True)
    context = json.loads(gateway.messages[0][1]["content"])
    assert context["structured_claims"][0]["evidence_ids"] == ["MISSING"]
    assert score["citation_integrity"]["status"] == "invalid"
    assert score["scores"]["citation_quality"] == 2


def test_claims_only_citations_are_supported_without_legacy_map():
    reply = json.dumps({"reference_status": "valid", "scores": {"citation_quality": 5},
                        "verdict_label": "good_answer", "answer_disposition": "answer"})
    score = score_answer(
        "q", "a", [{"quote": "x"}], "a", "sufficient", False, FakeGateway(reply),
        evidence_pack={"evidence": [{"evidence_id": "E1", "text": "x"}], "usable_evidence_ids": ["E1"]},
        citation_map={}, claims=[{"text": "a", "evidence_ids": ["E1"]}], answer_complete=True)
    assert score["citation_integrity"]["status"] == "valid_claims_only"
    assert score["scores"]["citation_quality"] == 5


def test_empty_evidence_pack_is_missing_for_groundedness():
    reply = json.dumps({"reference_status": "valid", "scores": {"groundedness": 5},
                        "verdict_label": "good_answer"})
    score = score_answer("q", "a", [], "a", "sufficient", False, FakeGateway(reply),
                         evidence_pack={"evidence": []}, citation_map={"E1": "x"}, answer_complete=True)
    assert score["scores"]["groundedness"] is None


def test_selection_sample_is_bounded_by_eligible_rows():
    assert len(judge_run.select_rows([_e()], {"x": _q()}, sample_validated=25)) == 1


def test_snapshot_keeps_long_answer_and_final_evidence():
    from evalsuite.e2e_product import response_record
    answer = "x" * 1600 + " FINAL ANSWER"
    pack = {"evidence": [{"evidence_id": "b2", "text": "final source"}]}
    record = response_record(_q(), {"answer": answer, "decision": {"status": "sufficient", "abstain": False},
        "provider": "fake", "evidence_pack": pack, "citation_map": {"E1": "b2"}}, 1)
    assert record["answer_text"] == answer and record["answer_complete"]
    assert record["evidence_pack"] == pack and record["citation_map"] == {"E1": "b2"}
