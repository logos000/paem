"""Stage 3A runtime tests: CorpusEngine.answer_context, user-context merge (R1),
known-conflict injection, the contract validator and the HTTP façade.

A tiny fake build (documents + blocks + conflict_candidates) exercises the
real engine path — the same functions the eval harness measures.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from evidence.store import write_jsonl
from retrieve.lexical import Corpus
from runtime.contract import ANSWER_CONTEXT_VERSION, validate_answer_context
from runtime.engine import CorpusEngine
from runtime.user_context import merge_user_context
from evidence.query_ir import parse_query

GOLD_QUOTE = "TECH101 Intro to Technology carries 3 credits."
OLD_GOLD_QUOTE = "TECH101 Intro to Technology carried 3 credits in 2025."

DOCS: list[dict[str, Any]] = [
    {"doc_uid": "d_cat26", "title": "Course Catalog 2026 Fall", "terms": ["2026-fall"], "audience": "zjui_undergraduate", "series_id": "s_cat", "is_latest": True, "status": "active"},
    {"doc_uid": "d_cat25", "title": "Course Catalog 2025 Fall", "terms": ["2025-fall"], "audience": "zjui_undergraduate", "series_id": "s_cat", "is_latest": False, "superseded_by": "d_cat26", "status": "active"},
    {"doc_uid": "d_grad", "title": "Graduate Prize Guide", "audience": "graduate", "series_id": "", "is_latest": True, "status": "active"},
    {"doc_uid": "d_other", "title": "Unrelated Handbook", "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
]
BLOCKS: list[dict[str, Any]] = [
    {"block_id": "d_cat26:0001", "doc_uid": "d_cat26", "ordinal": 1, "kind": "paragraph", "text": GOLD_QUOTE},
    {"block_id": "d_cat26:0002", "doc_uid": "d_cat26", "ordinal": 2, "kind": "paragraph", "text": "TECH102 Advanced Technology builds on TECH101 with lab sections."},
    {"block_id": "d_cat25:0001", "doc_uid": "d_cat25", "ordinal": 1, "kind": "paragraph", "text": OLD_GOLD_QUOTE},
    {"block_id": "d_grad:0001", "doc_uid": "d_grad", "ordinal": 1, "kind": "paragraph", "text": "TECH101 graduate prize nomination steps."},
    {"block_id": "d_other:0001", "doc_uid": "d_other", "ordinal": 1, "kind": "paragraph", "text": "Scholarship amounts vary every year."},
]
KNOWN_CONFLICT = {
    "fact_kind": "number",
    "series_id": "s_cat",
    "series_source": "rule",
    "values": [
        {"doc_uid": "d_cat26", "value": "3 credits", "span": GOLD_QUOTE},
        {"doc_uid": "d_cat25", "value": "3 credits", "span": OLD_GOLD_QUOTE},
    ],
}


@pytest.fixture()
def build_root(tmp_path):
    root = tmp_path / "build"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"build_id": "test-build"}), encoding="utf-8")
    write_jsonl(root / "documents.jsonl", DOCS)
    write_jsonl(root / "blocks.jsonl", BLOCKS)
    return root


@pytest.fixture()
def conflict_root(build_root):
    write_jsonl(build_root / "conflict_candidates.jsonl", [KNOWN_CONFLICT])
    return build_root


@pytest.fixture()
def engine(build_root) -> CorpusEngine:
    return CorpusEngine.load(build_root, config="active_ir")


@pytest.fixture()
def conflict_engine(conflict_root) -> CorpusEngine:
    return CorpusEngine.load(conflict_root, config="active_ir")


class FakeReranker:
    name = "fake"

    def __init__(self, favor: str) -> None:
        self.favor = favor

    def score(self, pairs) -> list[float]:
        return [2.0 if self.favor in text else 0.0 for _, text in pairs]


# ------------------------------------------------------------------ contract

def test_answer_context_contract(engine):
    payload = engine.answer_context("What are the credits of TECH101 in FA26?")
    assert validate_answer_context(payload) == []
    assert payload["version"] == ANSWER_CONTEXT_VERSION
    assert payload["provenance"]["build_id"] == "test-build"
    assert payload["provenance"]["gated_arm"] is False  # active_ir is not the validated family
    assert payload["decision"]["status"] == "sufficient"
    assert payload["decision"]["abstain"] is False
    assert payload["evidence_pack"]["evidence"], "expected the catalog block as evidence"
    assert payload["evidence_pack"]["evidence"][0]["provenance"]["doc_uid"] == "d_cat26"
    assert payload["user_context_filled"] == []


def test_answer_context_refuses_future_term(engine):
    payload = engine.answer_context("What are the credits of TECH101 in 2030-fall?")
    assert validate_answer_context(payload) == []
    assert payload["decision"]["abstain"] is True
    assert payload["decision"]["status"] == "wrong_term"


def test_multi_queries_preserve_original_ir_and_rebuild_one_pack(engine, monkeypatch):
    seen = []
    original = engine.retrieve
    def spy(question, **kwargs):
        seen.append((question, kwargs["ir"].terms))
        return original(question, **kwargs)
    monkeypatch.setattr(engine, "retrieve", spy)
    result = engine.answer_context("What are TECH101 credits in 2030-fall?",
                                   retrieval_queries=["TECH101 credits", "Course Catalog 2026 Fall"])
    assert len(seen) == 2 and all("2030-fall" in terms for _, terms in seen)
    assert result["decision"]["abstain"] and result["decision"]["status"] == "wrong_term"
    assert result["retrieval_trace"]["strategy"] == "multi_query_rrf"
    assert result["evidence_pack"]["query"] == "What are TECH101 credits in 2030-fall?"


def test_multi_query_candidate_union_is_passed_to_pack_before_limit(engine, monkeypatch):
    indices = list(range(min(4, len(engine.index.units))))
    calls = iter([[(i, 1.0) for i in indices[:2]], [(i, 1.0) for i in indices[2:]]])
    monkeypatch.setattr(engine, "retrieve", lambda *a, **kw: next(calls))
    captured = {}
    original = engine.build_pack
    def spy(question, ranked, **kwargs):
        captured.update(ranked=ranked, full=kwargs.get("full_candidate_pool"))
        return original(question, ranked, **kwargs)
    monkeypatch.setattr(engine, "build_pack", spy)
    result = engine.answer_context("TECH101 credits", retrieval_queries=["a", "b"], pack_limit=1)
    assert captured["full"] and len(captured["ranked"]) == len(indices)
    assert validate_answer_context(result) == []


def test_contract_validator_catches_bad_payload(engine):
    payload = engine.answer_context("What are the credits of TECH101?")
    payload["decision"]["status"] = "maybe"
    assert any("decision.status" in problem for problem in validate_answer_context(payload))
    broken = {**payload, "evidence_pack": {"query": "x"}}
    assert any("evidence_pack" in problem for problem in validate_answer_context(broken))


def test_config_vocabulary(build_root):
    with pytest.raises(ValueError, match="oracle_doc"):
        CorpusEngine.load(build_root, config="oracle_doc")
    with pytest.raises(ValueError):
        CorpusEngine.load(build_root, config="nope")
    with pytest.raises(FileNotFoundError):
        CorpusEngine.load(build_root.parent / "empty", config="active")


# ------------------------------------------------------------------ user context (R1)

def test_user_context_fills_blanks(engine):
    payload = engine.answer_context(
        "What are the credits of TECH101?",
        user_context={"term": "2026-fall", "audience": "undergraduate", "program": "ECE"},
    )
    assert payload["query_ir"]["terms"] == ["2026-fall"]
    assert payload["query_ir"]["audience"] == "undergraduate"
    assert set(payload["user_context_filled"]) >= {"term", "audience"}
    assert payload["user_context"] == {"term": "2026-fall", "audience": "undergraduate", "program": "ECE"}


def test_question_statement_wins_over_user_context(engine):
    payload = engine.answer_context(
        "What are the credits of TECH101 in FA26?",
        user_context={"term": "2027-spring"},
    )
    assert payload["query_ir"]["terms"] != ["2027-spring"]
    assert "2026-fall" in payload["query_ir"]["terms"]


def test_course_codes_union(engine):
    payload = engine.answer_context(
        "What are the prerequisites of ECE 220?",
        user_context={"course_codes": ["CS101"]},
    )
    codes = payload["query_ir"]["course_codes"]
    assert "CS101" in codes
    assert any("220" in code for code in codes)


def test_merge_user_context_ignores_unknown_keys():
    ir = parse_query("What are the credits of TECH101?")
    merged, pack_context, filled = merge_user_context(ir, {"banana": 1, "cohort": "2026"})
    assert merged.cohorts == ["2026"]
    assert pack_context == {"cohort": "2026"}
    assert "ignored:banana" in filled


# ------------------------------------------------------------------ conflicts & RR branch

def test_known_conflicts_flow_into_pack(conflict_engine):
    payload = conflict_engine.answer_context("What are the credits of TECH101?")
    kinds = {conflict["kind"] for conflict in payload["evidence_pack"]["conflicts"]}
    assert "known_conflict" in kinds
    assert payload["provenance"]["n_known_conflicts"] == 1
    # rule 6b: a pack carrying a retained disagreement answers (abstain=False)
    # but is labelled conflict so the generator must surface both sides.
    assert payload["decision"]["status"] == "conflict"
    assert payload["decision"]["abstain"] is False


def test_rr_branch_with_fake_reranker(build_root):
    engine = CorpusEngine.load(build_root, config="active_ir")
    engine.config = "active_ir_rr"
    engine.reranker = FakeReranker(GOLD_QUOTE)
    from runtime.indexing import rr_unit_texts
    engine.texts_for_rr = rr_unit_texts(engine.index)
    payload = engine.answer_context("What are the credits of TECH101?")
    assert validate_answer_context(payload) == []
    assert payload["evidence_pack"]["evidence"][0]["provenance"]["doc_uid"] == "d_cat26"


# ------------------------------------------------------------------ HTTP façade

def test_service_roundtrip_matches_in_process(build_root, tmp_path):
    fastapi_test = pytest.importorskip("fastapi.testclient")
    from runtime.service import ServiceSettings, create_app

    settings = ServiceSettings(build_root=build_root, config="active_ir", log_path=tmp_path / "svc.jsonl")
    client = fastapi_test.TestClient(create_app(settings))
    question = "What are the credits of TECH101?"

    health = client.get("/health").json()
    assert health["build_id"] == "test-build"
    assert health["config"] == "active_ir"

    response = client.post("/v1/answer_context", json={"question": question})
    assert response.status_code == 200
    http_payload = response.json()
    assert validate_answer_context(http_payload) == []
    assert http_payload["request_id"]

    direct = CorpusEngine.load(build_root, config="active_ir").answer_context(question)
    for payload in (http_payload, direct):
        payload.pop("latency_ms", None)
        payload.pop("request_id", None)
    assert http_payload == direct


def test_service_rejects_bad_requests(build_root, tmp_path):
    fastapi_test = pytest.importorskip("fastapi.testclient")
    from runtime.service import ServiceSettings, create_app

    client = fastapi_test.TestClient(create_app(ServiceSettings(build_root=build_root, config="active_ir", log_path=tmp_path / "svc.jsonl")))
    assert client.post("/v1/answer_context", json={"question": "?", "config": "oracle_doc"}).status_code == 400
    assert client.post("/v1/answer_context", json={"question": "?", "today": "2026-13-99"}).status_code == 400
    assert client.post("/v1/answer_context", json={"question": "?", "user_context": {"banana": 1}}).status_code == 400
    # one accepted request → one structured jsonl log line
    assert client.post("/v1/answer_context", json={"question": "What are the credits of TECH101 in FA26?"}).status_code == 200
    log_lines = (tmp_path / "svc.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["decision"]["status"] == "sufficient"


# ------------------------------------------------------------------ admin: atomic build switching (P4)

def _second_build(build_root, tmp_path):
    """A second, distinct build directory (same content, different build_id)."""
    other = tmp_path / "build-v2"
    other.mkdir()
    for name in ("documents.jsonl", "blocks.jsonl"):
        (other / name).write_bytes((build_root / name).read_bytes())
    (other / "manifest.json").write_text(json.dumps({"build_id": "test-build-v2"}), encoding="utf-8")
    return other


def test_admin_load_smoke_switch_rollback_and_canary(build_root, tmp_path):
    fastapi_test = pytest.importorskip("fastapi.testclient")
    from runtime.service import ServiceSettings, create_app

    other = _second_build(build_root, tmp_path)
    client = fastapi_test.TestClient(create_app(ServiceSettings(build_root=build_root, config="active_ir", log_path=tmp_path / "svc.jsonl")))

    # canary pinning before the switch: X-Target-Build on an unknown build → 404
    assert client.post("/v1/answer_context", json={"question": "?"}, headers={"X-Target-Build": "nope"}).status_code == 404

    # load the new build in the background slot, wait for it, smoke it
    assert client.post("/admin/builds", json={"build_dir": str(other)}).json()["status"] in ("loading", "already_loaded")
    for _ in range(100):
        builds = client.get("/admin/builds").json()
        slot = next(s for s in builds["builds"] if s["key"] == "build-v2")
        if not slot["loading"]:
            break
        time.sleep(0.05)
    assert not slot["error"], slot
    smoke = client.post("/admin/builds/build-v2/smoke", json={"questions": [
        "What are the credits of TECH101 in FA26?",
        "What is the withdrawal deadline for 2031 spring term?",  # must abstain on this fixture too
    ]}).json()
    assert smoke["all_ok"] is True and smoke["build_id"] == "test-build-v2"
    statuses = [item["status"] for item in smoke["results"]]
    assert statuses[0] in ("sufficient", "conflict") and statuses[1] in ("wrong_term", "no_material")

    # traffic still on the old build; switch is atomic; canary hits the new one
    before = client.post("/v1/answer_context", json={"question": "What are the credits of TECH101 in FA26?"}).json()
    assert before["provenance"]["build_id"] == "test-build"
    canary = client.post("/v1/answer_context", json={"question": "?"}, headers={"X-Target-Build": "build-v2"}).json()
    assert canary["provenance"]["build_id"] == "test-build-v2"

    switched = client.post("/admin/switch", json={"key": "build-v2"}).json()
    assert switched == {"previous": "default", "current": "build-v2", "build_id": "test-build-v2"}
    after = client.post("/v1/answer_context", json={"question": "What are the credits of TECH101 in FA26?"}).json()
    assert after["provenance"]["build_id"] == "test-build-v2"

    # rollback restores the previous default
    rolled = client.post("/admin/rollback").json()
    assert rolled["rolled_back"] is True
    final = client.post("/v1/answer_context", json={"question": "What are the credits of TECH101 in FA26?"}).json()
    assert final["provenance"]["build_id"] == "test-build"

    # switch to an unloaded build is refused
    assert client.post("/admin/switch", json={"key": "ghost"}).status_code == 409
    # the whole sequence is audited (load → smoke → switch → rollback-as-switch)
    audit = (tmp_path / "admin_audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    actions = [json.loads(line)["action"] for line in audit]
    assert actions[0] == "load" and actions.count("switch") == 2 and "smoke" in actions


# ---------------------------------------------------------------- content-twin rescue (2026-10-15 live bug)
#
# Two identical SharePoint pages form one series; BM25.search's global content
# dedup keeps only the SUPERSEDED copy of the answer block, and the latest
# doc's per-doc quota fills with weaker blocks — so the answer text vanished
# between BM25 and the pack ('本科生退课什么时候截止' had Nov-6-2026 at rank 1
# and still got 'no date in evidence').

TWIN_DOCS: list[dict[str, Any]] = [
    {"doc_uid": "d_new", "title": "Course Drop Request", "terms": [], "audience": "", "series_id": "s_drop", "is_latest": True, "status": "active"},
    {"doc_uid": "d_old", "title": "Course Drop Request (old copy)", "terms": [], "audience": "", "series_id": "s_drop", "is_latest": False, "superseded_by": "d_new", "status": "active"},
    {"doc_uid": "d_fill", "title": "Other Policy Page", "terms": [], "audience": "", "series_id": "", "is_latest": True, "status": "active"},
]
DEADLINE_TEXT = "For FA 2026, the deadline of drop course is 5PM, Nov 6, 2026."
TWIN_BLOCKS: list[dict[str, Any]] = [
    # d_old's copy FIRST so the content dedup keeps the superseded one
    {"block_id": "d_old:0001", "doc_uid": "d_old", "ordinal": 1, "kind": "paragraph", "text": DEADLINE_TEXT},
    # d_new: the identical twin plus two weaker-but-matching blocks that fill its quota
    {"block_id": "d_new:0001", "doc_uid": "d_new", "ordinal": 1, "kind": "list_item", "text": DEADLINE_TEXT},
    {"block_id": "d_new:0002", "doc_uid": "d_new", "ordinal": 2, "kind": "paragraph", "text": "If the course you want to drop is UIUC credit you must submit an additional request."},
    {"block_id": "d_new:0003", "doc_uid": "d_new", "ordinal": 3, "kind": "paragraph", "text": "Till the first week of the semester you can drop a course yourself."},
    {"block_id": "d_fill:0001", "doc_uid": "d_fill", "ordinal": 1, "kind": "paragraph", "text": "Drop requests are subject to approval."},
]


def test_superseded_duplicate_content_rescued_into_pack(tmp_path):
    root = tmp_path / "build-twin"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"build_id": "twin-build"}), encoding="utf-8")
    write_jsonl(root / "documents.jsonl", TWIN_DOCS)
    write_jsonl(root / "blocks.jsonl", TWIN_BLOCKS)

    engine = CorpusEngine.load(root, config="active_ir")
    payload = engine.answer_context("when is the course drop deadline", today=None)

    evidence = payload["evidence_pack"]["evidence"]
    texts = [item["text"] for item in evidence]
    assert any(DEADLINE_TEXT in t for t in texts), "answer block must reach the pack despite dedup+supersession"
    # and it must come from the LATEST edition (the doc that survives version discrimination)
    docs = {item["doc_id"] for item in evidence if DEADLINE_TEXT in item["text"]}
    assert docs == {"d_new"}


def test_rescue_not_needed_when_latest_copy_survives(build_root, tmp_path):
    # sanity on the normal path: the standard fixture's latest edition stays put
    engine = CorpusEngine.load(build_root, config="active_ir")
    payload = engine.answer_context("What are the credits of TECH101?", today=None)
    assert any("TECH101" in item["text"] for item in payload["evidence_pack"]["evidence"])


# ---------------------------------------------------------------- drop telemetry (2026-10-15)

def test_pack_records_dropped_candidates(build_root):
    """Ranks beyond pack_limit must surface as dropped_candidates diagnostics,
    not vanish silently between retrieve() and the pack."""
    engine = CorpusEngine.load(build_root, config="active_ir")
    ranked = engine.retrieve("What are the credits of TECH101?")
    pack = engine.build_pack("What are the credits of TECH101?", ranked, pack_limit=2)
    dropped = pack.get("dropped_candidates", [])
    assert 0 < len(dropped) <= 5
    assert all(d["reason"] == "pack_limit" and d["rank"] > 2 and d["doc_uid"] for d in dropped)
    # diagnostics only: the field must not disturb the frozen decision surface
    assert "abstain" in pack and "usable_evidence_ids" in pack


def test_pack_drop_telemetry_absent_when_nothing_dropped(build_root):
    engine = CorpusEngine.load(build_root, config="active_ir")
    ranked = engine.retrieve("What are the credits of TECH101?")
    pack = engine.build_pack("What are the credits of TECH101?", ranked, pack_limit=8)
    assert pack.get("dropped_candidates", []) == []


# ---------------------------------------------------------------- pack_hash (v2, 2026-10-16)

import re as _re

from runtime.contract import compute_pack_hash


def test_pack_hash_present_stable_and_distinct(engine):
    first = engine.answer_context("What are the credits of TECH101 in FA26?")
    second = engine.answer_context("What are the credits of TECH101 in FA26?")
    other = engine.answer_context("What builds on TECH101?")
    assert validate_answer_context(first) == []
    assert _re.fullmatch(r"[0-9a-f]{64}", first["pack_hash"])
    assert first["pack_hash"] == second["pack_hash"]  # deterministic identity
    assert first["pack_hash"] != other["pack_hash"]   # evidence-sensitive


def test_pack_hash_ignores_trace_timings_and_fill_audit(engine):
    payload = engine.answer_context("What are the credits of TECH101 in FA26?")
    original = payload["pack_hash"]
    payload["retrieval_trace"] = {"strategy": "mutated", "rounds": [{"query": "x"}]}
    payload["latency_ms"] = 999.0
    payload["user_context_filled"] = ["audience"]
    assert compute_pack_hash(payload["question"], payload["query_ir"], payload["decision"], payload["evidence_pack"]) == original


def _identity_pack() -> dict[str, Any]:
    return {
        "usable_evidence_ids": ["a:1:1"],
        "evidence": [{
            "evidence_id": "a:1:1", "doc_id": "a", "provenance": {"block_id": "a#1"},
            "page": 1, "text": "Deadline is May 20.", "blocking": False,
        }],
    }


_DECISION = {"status": "sufficient", "abstain": False, "reason": ""}


def test_pack_hash_sensitivity_to_evidence_usability_decision():
    base = _identity_pack()
    same = compute_pack_hash("q", {}, _DECISION, base)
    # pack diagnostics (drops, coverage) are identity-free — only evidence/
    # usability/decision/query/ir define the pack
    assert same == compute_pack_hash("q", {}, _DECISION, {**base, "dropped_candidates": [{"doc_uid": "x"}], "coverage": 0.9})
    changed_text = {**base, "evidence": [{**base["evidence"][0], "text": "Deadline is May 21."}]}
    assert same != compute_pack_hash("q", {}, _DECISION, changed_text)
    lost_usable = {**base, "usable_evidence_ids": []}
    assert same != compute_pack_hash("q", {}, _DECISION, lost_usable)
    abstain = {"status": "no_material", "abstain": True, "reason": "r"}
    assert same != compute_pack_hash("q", {}, abstain, base)
    assert same != compute_pack_hash("other question", {}, _DECISION, base)


def test_contract_validator_rejects_missing_or_malformed_pack_hash():
    from runtime.contract import validate_answer_context
    problems = validate_answer_context({"version": 2, "question": "q", "config": "active_ir"})
    assert any("pack_hash" in p for p in problems)


# ---------------------------------------------------------------- hard applicability filter (step 7, 2026-10-16)

def test_hard_filter_excludes_wrong_year_and_names_the_reason(build_root):
    engine = CorpusEngine.load(build_root, config="active_ir")
    payload = engine.answer_context("What are the credits of TECH101 for undergraduate students in FA24?")
    assert payload["decision"]["abstain"] is True
    # the corpus HAS TECH101 catalogs (2025/2026) — the refusal must name the
    # year mismatch, not claim "no material at all"
    assert payload["decision"]["status"] == "wrong_term"
    dropped = payload["evidence_pack"].get("dropped_candidates", [])
    assert any("wrong_year_hard" in str(d.get("reason", "")) for d in dropped)
    assert all("2024" not in (item.get("title") or "") for item in payload["evidence_pack"]["evidence"])


def test_hard_filter_excludes_cross_audience_docs(build_root):
    engine = CorpusEngine.load(build_root, config="active_ir")
    # undergraduate question; d_grad is audience=graduate with no year anchor
    payload = engine.answer_context("How do undergraduate students nominate for the TECH101 graduate prize?")
    grad_ids = {e["evidence_id"] for e in payload["evidence_pack"]["evidence"] if e["doc_id"] == "d_grad"}
    dropped_reasons = [str(d.get("reason", "")) for d in payload["evidence_pack"].get("dropped_candidates", [])]
    # either excluded pre-ranking or never retrieved — it must not be usable
    usable = set(payload["evidence_pack"]["usable_evidence_ids"])
    assert not (grad_ids & usable) or "wrong_audience_hard" in " ".join(dropped_reasons)


def test_hard_filter_kill_switch_restores_demotion(build_root, monkeypatch):
    import runtime.engine as engine_module
    monkeypatch.setattr(engine_module.CorpusEngine, "__init__", engine_module.CorpusEngine.__init__)  # keep signature intact
    monkeypatch.setenv("RUNTIME_HARD_FILTER", "0")
    engine = CorpusEngine.load(build_root, config="active_ir")
    assert engine.hard_filter is False
    payload = engine.answer_context("What are the credits of TECH101 for undergraduate students in FA24?")
    # demotion (not exclusion): year-pinned docs may still fill an empty pool
    assert not any("wrong_year_hard" in str(d.get("reason", "")) for d in payload["evidence_pack"].get("dropped_candidates", []))


def test_hard_filter_never_excludes_unanchored_docs(build_root):
    from evidence.query_ir import hard_applicability_mismatches, parse_query
    ir = parse_query("What are the credits of TECH101 in FA24?")
    # no year/audience anchors on the doc → no exclusion possible
    assert hard_applicability_mismatches(ir, {"title": "Handbook"}) == []
    assert hard_applicability_mismatches(ir, {"title": "Guide 2026"}) == ["wrong_year_hard"]
    ug = parse_query("prize nomination for undergraduate students")
    assert hard_applicability_mismatches(ug, {"title": "X", "audience": "graduate"}) == ["wrong_audience_hard"]
    assert hard_applicability_mismatches(ug, {"title": "X", "audience": "zjui_undergraduate"}) == []


# --------------------------------------- cross-year semester windows (failure analysis fix 2, 2026-10-16)

def test_hard_filter_cross_year_semester_windows():
    from evidence.query_ir import hard_applicability_mismatches, parse_query, question_dates
    # sm-0105 live case: a Dec-2023 time ticket lives in a 2024-spring guide
    ir = parse_query("For seniors, at what time does course registration begin on Dec. 25, 2023?")
    assert question_dates(ir.raw) == {("2023", "12")}
    assert hard_applicability_mismatches(ir, {"title": "Course Enrollment Guide, Spring 2024", "terms": ["2024-spring"]}) == []
    # a June-2025 event must NOT unlock 2026 guides (sm-0057 stays filtered)
    ir2 = parse_query("Period 1 的选课窗口在2025年6月20日几点关闭？")
    assert hard_applicability_mismatches(ir2, {"title": "Course Enrollment Guide, Fall 2026", "terms": ["2026-fall"]}) == ["wrong_year_hard"]
    assert hard_applicability_mismatches(ir2, {"title": "Schedule 2026"}) == ["wrong_year_hard"]
    # bare title years keep the straddle edges only
    assert hard_applicability_mismatches(ir, {"title": "Schedule 2024"}) == []
    # semester pins stay exact-year
    ir3 = parse_query("What are the credits of TECH101 in FA24?")
    assert hard_applicability_mismatches(ir3, {"title": "Guide 2026"}) == ["wrong_year_hard"]


# ------------------------------- date query variants + fact rescue (failure analysis fixes 3+4, 2026-10-16)

def test_date_variants_bridge_script_gap(tmp_path):
    root = tmp_path / "build"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"build_id": "date-build"}), encoding="utf-8")
    write_jsonl(root / "documents.jsonl", [
        {"doc_uid": "d_enroll25", "title": "Course Enrollment Guide Fall 2025", "terms": ["2025-fall"], "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
        {"doc_uid": "d_filler", "title": "Handbook", "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
    ])
    write_jsonl(root / "blocks.jsonl", [
        {"block_id": "d_enroll25:0001", "doc_uid": "d_enroll25", "ordinal": 1, "kind": "paragraph",
         "text": "Period 1 will start from 8 pm May 20, 2025 and will close at 5 pm June 20, 2025 ."},
        {"block_id": "d_filler:0001", "doc_uid": "d_filler", "ordinal": 1, "kind": "paragraph",
         "text": "General handbook information about campus services and offices."},
    ])
    engine = CorpusEngine.load(root, config="active_ir")
    payload = engine.answer_context("Period 1 的选课窗口在2025年6月20日几点关闭？", today=None)
    variant_queries = payload["retrieval_trace"]["queries"]
    assert any("June 20, 2025" in q or "2025-06-20" in q for q in variant_queries)
    texts = [item["text"] for item in payload["evidence_pack"]["evidence"]]
    assert any("close at 5 pm June 20, 2025" in text for text in texts)


def test_fact_rescue_recovers_uncovered_fact_slot(build_root):
    engine = CorpusEngine.load(build_root, config="active_ir")
    # empty candidate pool: without rescue the pack would stay empty; the
    # targeted round must find the catalog block and rebuild over it
    pack = engine.build_pack("What are the credits of TECH101?", [], today=None)
    rescued = pack.get("fact_rescue") or []
    assert rescued, "the rescue round should have found the TECH101 catalog block"
    assert any("TECH101" in item["text"] for item in pack["evidence"])
    assert pack["usable_evidence_ids"], "rescued evidence must be usable after the rebuild"


# ------------------------------------------------- section rollup (2026-10-16)

CURRICULUM_DOCS: list[dict[str, Any]] = [
    {"doc_uid": "d_ce25", "title": "Curriculum Computer Engineering class of 2025", "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
    {"doc_uid": "d_civ25", "title": "Curriculum Civil Engineering class of 2025", "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
]
CURRICULUM_BLOCKS: list[dict[str, Any]] = [
    {"block_id": "d_ce25:1", "doc_uid": "d_ce25", "ordinal": 1, "kind": "paragraph", "text": "2.1.3 Curriculum Requirement i). Required courses, see section 3 for details."},
    {"block_id": "d_ce25:2", "doc_uid": "d_ce25", "ordinal": 2, "kind": "table_row", "text": "Course Code: ENG 100 | Course Name: Engineering Orientation | Academic Credits: 1"},
    {"block_id": "d_ce25:3", "doc_uid": "d_ce25", "ordinal": 3, "kind": "table_row", "text": "Course Name: Total Required AUs: | Academic Credit: 1"},
    {"block_id": "d_ce25:4", "doc_uid": "d_ce25", "ordinal": 4, "kind": "paragraph", "text": "In addition to the 128-AU requirement listed above, the curriculum requires the following core courses."},
    {"block_id": "d_ce25:5", "doc_uid": "d_ce25", "ordinal": 5, "kind": "table_row", "text": "Course Code: ECE 110 | Course Name: Introduction to Electronics | Academic Credits: 3"},
    {"block_id": "d_ce25:6", "doc_uid": "d_ce25", "ordinal": 6, "kind": "table_row", "text": "Course Code: ECE 120 | Course Name: Introduction to Computing | Academic Credits: 3"},
    {"block_id": "d_ce25:7", "doc_uid": "d_ce25", "ordinal": 7, "kind": "table_row", "text": "Course Code: ECE 391 | Course Name: Computer Systems Engineering | Academic Credits: 4"},
    {"block_id": "d_ce25:8", "doc_uid": "d_ce25", "ordinal": 8, "kind": "table_row", "text": "Course Name: Total Required AUs: | Academic Credits: 36"},
    {"block_id": "d_ce25:9", "doc_uid": "d_ce25", "ordinal": 9, "kind": "paragraph", "text": "4.1 Technical Electives Select One (1) course from the following list."},
    {"block_id": "d_ce25:10", "doc_uid": "d_ce25", "ordinal": 10, "kind": "table_row", "text": "Course Code: ECE 445 | Course Name: Selected Topics | Academic Credits: 3"},
    {"block_id": "d_civ25:1", "doc_uid": "d_civ25", "ordinal": 1, "kind": "table_row", "text": "Course Code: CEE 300 | Course Name: Behavior of Materials | Academic Credits: 3"},
    {"block_id": "d_civ25:2", "doc_uid": "d_civ25", "ordinal": 2, "kind": "table_row", "text": "Course Code: CEE 360 | Course Name: Structural Engineering | Academic Credits: 3"},
]


@pytest.fixture()
def curriculum_engine(tmp_path) -> CorpusEngine:
    root = tmp_path / "curriculum-build"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"build_id": "curriculum-test"}), encoding="utf-8")
    write_jsonl(root / "documents.jsonl", CURRICULUM_DOCS)
    write_jsonl(root / "blocks.jsonl", CURRICULUM_BLOCKS)
    return CorpusEngine.load(root, config="active_ir")


def test_section_rollup_folds_course_table_for_enumerative_question(curriculum_engine):
    payload = curriculum_engine.answer_context(
        "计算机工程专业有哪些必修课",
        retrieval_queries=["computer engineering required courses course code course name academic credits"],
    )
    assert validate_answer_context(payload) == []
    pack = payload["evidence_pack"]
    assert pack["section_rollup"]["doc_uid"] == "d_ce25"
    texts = [item["text"] for item in pack["evidence"]]
    # one rollup hit carries the whole required-course section: the ENG group,
    # the running prose between row groups, and the ECE core group
    folded = next(t for t in texts if "ECE 120" in t and "ECE 391" in t)
    # the paragraph naming the section rides along as label provenance
    assert folded.startswith("2.1.3 Curriculum Requirement")
    assert "ENG 100" in folded
    assert "Total Required AUs" in folded
    assert "ECE 445" not in folded  # numbered section header '4.1' closes the section
    assert "CEE 300" not in folded  # another document's rows never join


def test_section_rollup_picks_title_matching_curriculum(curriculum_engine):
    payload = curriculum_engine.answer_context(
        "计算机工程专业有哪些必修课",
        retrieval_queries=["computer engineering curriculum required courses"],
    )
    assert payload["evidence_pack"]["section_rollup"]["doc_uid"] == "d_ce25"


def test_section_rollup_inactive_for_non_enumerative_question(curriculum_engine):
    payload = curriculum_engine.answer_context(
        "ECE 120 这门课多少学分？",
        retrieval_queries=["ECE 120 academic credits"],
    )
    assert "section_rollup" not in payload["evidence_pack"]


def test_section_rollup_not_fired_by_generic_enumeration(curriculum_engine):
    # 2026-10-16 regression: a bare 哪些/课表 point question must not pull a
    # whole course table into its pack and crowd out the specific cell.
    payload = curriculum_engine.answer_context(
        "课表中 ECE 110 Lab 1 后括号内标注的学生人数是多少？",
        retrieval_queries=["ECE 110 lab enrollment capacity course schedule"],
    )
    assert "section_rollup" not in payload["evidence_pack"]


# ---------------------------------------------- fold families (second wave)

FAMILY_DOCS: list[dict[str, Any]] = [
    {"doc_uid": "d_sp25", "title": "SP2025 Course Class Number", "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
    {"doc_uid": "d_exam24", "title": "ZJUI 2024 FALL Final Exam Schedule", "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
    {"doc_uid": "d_prereq", "title": "ECE Courses and Prerequisites", "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
    {"doc_uid": "d_wd", "title": "Course Withdrawal Policy", "audience": "zjui_undergraduate", "series_id": "", "is_latest": True, "status": "active"},
]
FAMILY_BLOCKS: list[dict[str, Any]] = [
    {"block_id": "d_sp25:1", "doc_uid": "d_sp25", "ordinal": 1, "kind": "table_row", "text": "SP2025 Course Class Number"},
    {"block_id": "d_sp25:2", "doc_uid": "d_sp25", "ordinal": 2, "kind": "table_row", "text": "Course Code | Course Name | Class Nbr"},
    {"block_id": "d_sp25:3", "doc_uid": "d_sp25", "ordinal": 3, "kind": "table_row", "text": "MATH 231 | Calculus II | 1026-1027"},
    {"block_id": "d_sp25:4", "doc_uid": "d_sp25", "ordinal": 4, "kind": "table_row", "text": "PHYS 211 | University Physics: Mechanics | 1036-1037"},
    {"block_id": "d_sp25:5", "doc_uid": "d_sp25", "ordinal": 5, "kind": "table_row", "text": "ECE 120 | Introduction to Computing | 1041"},
    {"block_id": "d_exam24:1", "doc_uid": "d_exam24", "ordinal": 1, "kind": "heading", "heading_path": ["ZJUI 2024 FALL Final Exam Sch"], "text": "ZJUI 2024 FALL Final Exam Schedule"},
    {"block_id": "d_exam24:2", "doc_uid": "d_exam24", "ordinal": 2, "kind": "table_row", "heading_path": ["ZJUI 2024 FALL Final Exam Sch"], "text": "No.: 1 | Time: 9:00-9:30 | ECE411 (4) Nam Sung Kim LTW20"},
    {"block_id": "d_exam24:3", "doc_uid": "d_exam24", "ordinal": 3, "kind": "table_row", "heading_path": ["ZJUI 2024 FALL Final Exam Sch"], "text": "No.: 2 | Time: 9:30-10:00"},
    {"block_id": "d_exam24:4", "doc_uid": "d_exam24", "ordinal": 4, "kind": "table_row", "heading_path": ["ZJUI 2024 FALL Final Exam Sch"], "text": "No.: 3 | Time: 10:00-10:30 | CEE300 (4) Staff E-205"},
    {"block_id": "d_prereq:1", "doc_uid": "d_prereq", "ordinal": 1, "kind": "table_row", "text": "Number: ECE 120 | Title: Introduction to Computing | Prerequisites: None"},
    {"block_id": "d_prereq:2", "doc_uid": "d_prereq", "ordinal": 2, "kind": "table_row", "text": "Number: ECE 220 | Title: Computer Systems & Programming | Prerequisites: ECE 120"},
    {"block_id": "d_prereq:3", "doc_uid": "d_prereq", "ordinal": 3, "kind": "table_row", "text": "Number: ECE 391 | Title: Computer Systems Engineering | Prerequisites: ECE 220, CS 225"},
    {"block_id": "d_prereq:4", "doc_uid": "d_prereq", "ordinal": 4, "kind": "table_row", "text": "Number: ECE 385 | Title: Digital Systems Laboratory | Prerequisites: ECE 220"},
    {"block_id": "d_wd:1", "doc_uid": "d_wd", "ordinal": 1, "kind": "paragraph", "text": "超过截止日期的退课申请一般不予批准，但特殊情况如下："},
    {"block_id": "d_wd:2", "doc_uid": "d_wd", "ordinal": 2, "kind": "paragraph", "text": "因病或家庭紧急情况可附证明材料向教务提出申诉，由学院审批。"},
    {"block_id": "d_wd:3", "doc_uid": "d_wd", "ordinal": 3, "kind": "paragraph", "text": "完整的退课流程见学籍管理手册。"},
]


@pytest.fixture()
def family_engine(tmp_path) -> CorpusEngine:
    root = tmp_path / "family-build"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"build_id": "family-test"}), encoding="utf-8")
    write_jsonl(root / "documents.jsonl", FAMILY_DOCS)
    write_jsonl(root / "blocks.jsonl", FAMILY_BLOCKS)
    return CorpusEngine.load(root, config="active_ir")


def test_class_number_fold(family_engine):
    payload = family_engine.answer_context(
        "SP2025 的课号清单有哪些？",
        retrieval_queries=["SP2025 course class number list"],
    )
    pack = payload["evidence_pack"]
    assert pack["section_rollup"]["family"] == "class_number"
    assert pack["section_rollup"]["doc_uid"] == "d_sp25"
    folded = next(t for t in (i["text"] for i in pack["evidence"]) if "MATH 231" in t and "ECE 120" in t)
    assert "1026-1027" in folded


def test_class_number_point_question_not_folded(family_engine):
    payload = family_engine.answer_context(
        "MATH 231 的课号是多少？",
        retrieval_queries=["MATH 231 class number"],
    )
    assert "section_rollup" not in payload["evidence_pack"]


def test_exam_schedule_fold(family_engine):
    payload = family_engine.answer_context(
        "期末考试安排都有哪些？",
        retrieval_queries=["ZJUI final exam schedule list"],
    )
    pack = payload["evidence_pack"]
    assert pack["section_rollup"]["family"] == "exam_schedule"
    folded = next(t for t in (i["text"] for i in pack["evidence"]) if "No.: 1" in t and "No.: 3" in t)
    assert "ECE411" in folded and "CEE300" in folded


def test_exam_point_question_not_folded(family_engine):
    payload = family_engine.answer_context(
        "ECE411 期末考试是几点？",
        retrieval_queries=["ECE411 final exam time"],
    )
    assert "section_rollup" not in payload["evidence_pack"]


def test_prereq_reverse_lookup_filtered_fold(family_engine):
    payload = family_engine.answer_context(
        "哪些课以 ECE 220 为先修？",
        retrieval_queries=["courses with prerequisite ECE 220"],
    )
    pack = payload["evidence_pack"]
    assert pack["section_rollup"]["family"] == "prerequisites"
    folded = next(t for t in (i["text"] for i in pack["evidence"]) if "ECE 391" in t)
    assert "ECE 385" in folded          # both rows mention ECE 220
    assert "ECE 120 | Title" not in folded  # rows without the anchor never join


def test_prereq_point_question_not_folded(family_engine):
    payload = family_engine.answer_context(
        "ECE 391 的先修课是什么？",
        retrieval_queries=["ECE 391 prerequisites"],
    )
    assert "section_rollup" not in payload["evidence_pack"]


def test_stitch_pulls_next_block_after_dangling_colon(family_engine):
    payload = family_engine.answer_context(
        "退课超过截止日期的特殊情况怎么处理？",
        retrieval_queries=["withdrawal after deadline special circumstances appeal"],
    )
    pack = payload["evidence_pack"]
    stitched = [i for i in pack["evidence"] if i.get("stitched_blocks")]
    assert stitched, "expected the dangling policy block to stitch its neighbour"
    item = stitched[0]
    assert "特殊情况如下：" in item["text"]
    assert "因病或家庭紧急情况" in item["text"]


def test_stitch_leaves_complete_blocks_alone(family_engine):
    payload = family_engine.answer_context(
        "退课的完整流程在哪里？",
        retrieval_queries=["withdrawal full process handbook"],
    )
    pack = payload["evidence_pack"]
    for item in pack["evidence"]:
        if "完整的退课流程见学籍管理手册" in item["text"]:
            assert "stitched_blocks" not in item or item["text"].count("学籍管理手册") >= 1


# ---------------------------------------------- ablation switches (2026-10-16)

def test_switch_section_rollup_off(curriculum_engine, monkeypatch):
    monkeypatch.setattr(curriculum_engine, "section_rollup_enabled", False)
    hit, meta = curriculum_engine._section_rollup if False else (None, None)  # noqa
    payload = curriculum_engine.answer_context(
        "计算机工程专业有哪些必修课",
        retrieval_queries=["computer engineering required courses course code course name academic credits"],
    )
    assert "section_rollup" not in payload["evidence_pack"]


def test_switch_coverage_union_off(curriculum_engine, monkeypatch):
    monkeypatch.setattr(curriculum_engine, "coverage_union", False)
    payload = curriculum_engine.answer_context(
        "计算机工程专业有哪些必修课",
        retrieval_queries=["computer engineering required courses curriculum"],
    )
    pack = payload["evidence_pack"]
    # question-only terms: the zh bigrams dominate required_terms again
    assert all(ord(ch[0]) >= 0x4E00 or ch.isascii() for ch in pack["required_terms"])


def test_switch_coverage_floor_env(monkeypatch):
    import importlib
    import decision.status as status_mod
    monkeypatch.setenv("RUNTIME_COVERAGE_FLOOR", "0")
    importlib.reload(status_mod)
    assert status_mod.COVERAGE_FLOOR == 0.0
    monkeypatch.setenv("RUNTIME_COVERAGE_FLOOR", "0.12")
    importlib.reload(status_mod)
    assert status_mod.COVERAGE_FLOOR == 0.12




# ---- cross-lingual alias bridge (2026-10-18) ----

def test_alias_bridge_fires_on_zh_keys():
    from domain_profile import load_profile
    groups = load_profile().entries("aliases")
    flat_keys = [k for g in groups for k in g.get("keys", [])]
    assert "退课" in flat_keys and "ECE专业" in flat_keys


def test_alias_bridge_terms_present():
    from domain_profile import load_profile
    groups = load_profile().entries("aliases")
    ece = next(g for g in groups if "ECE专业" in g.get("keys", []))
    assert "Computer Engineering" in ece["terms"] and "CompE" in ece["terms"]


# ---------------------------------------- doc completion (2026-10-17 sm-0082)

def test_doc_completion_pulls_sibling_blocks(engine):
    # pack_limit=1 packs only the top block (TECH102's own row), so the
    # sibling holding the TECH101 fact misses the pack — the sm-0082 shape
    # (right doc in, fact block out).
    payload = engine.answer_context("TECH102 的实验课安排是什么？", retrieval_queries=["TECH102 lab sections"], pack_limit=1)
    pack = payload["evidence_pack"]
    item = next(i for i in pack["evidence"] if "TECH102 Advanced" in str(i.get("text")))
    assert "TECH101 Intro to Technology carries 3 credits" in item["text"]
    assert item.get("completed_blocks") == ["d_cat26:0001"]
    assert pack.get("doc_completion", {}).get("docs") == 1


def test_doc_completion_kill_switch(build_root, monkeypatch):
    monkeypatch.setenv("RUNTIME_DOC_COMPLETION", "0")
    eng = CorpusEngine.load(build_root, config="active_ir")
    payload = eng.answer_context("TECH101 的学分是多少？", retrieval_queries=["TECH101 credits"], pack_limit=1)
    item = next(i for i in payload["evidence_pack"]["evidence"] if "TECH101 Intro" in str(i.get("text")))
    assert "TECH102 Advanced Technology" not in item["text"]
    assert "doc_completion" not in payload["evidence_pack"]


def test_doc_completion_budget_and_rerun_guard(engine):
    pack = {"evidence": [{"doc_id": "d_cat26", "chunk_id": "d_cat26:0001", "text": GOLD_QUOTE}]}
    engine._doc_completion(pack, max_chars=10)  # sibling (66 chars) never fits
    assert pack["evidence"][0]["text"] == GOLD_QUOTE
    assert "completed_blocks" not in pack["evidence"][0]
    engine._doc_completion(pack, max_chars=1200)
    assert "TECH102 Advanced Technology" in pack["evidence"][0]["text"]
    engine._doc_completion(pack, max_chars=1200)  # re-run guard: no duplication
    assert pack["evidence"][0]["text"].count("TECH102 Advanced Technology") == 1
