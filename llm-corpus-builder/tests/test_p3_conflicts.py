"""P3 tests: build-time fact conflicts, series graph, effective-date ordering,
and EvidencePack conflict retention (both sides kept, never silently merged).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from curate.conflicts import detect_fact_conflicts, extract_fact_claims
from curate.series_graph import merge_series_graph
from curate.versions import build_series
from evidence.pack import build_evidence_pack


def _block(uid: str, ordinal: int, text: str) -> dict:
    return {"block_id": f"{uid}:{ordinal:04d}", "doc_uid": uid, "ordinal": ordinal, "kind": "paragraph", "text": text}


def _doc(uid: str, **over) -> dict:
    base = {"doc_uid": uid, "title": uid, "site": "zjui", "doc_type": "notice", "status": "active",
            "terms": [], "academic_years": [], "series_id": "", "is_latest": True, "published": "",
            "effective_from": "", "authority_rank": ""}
    base.update(over)
    return base


# --------------------------------------------------------------------- extraction


def test_extract_fact_claims_deadline_requires_hint_and_canonicalizes():
    claims = extract_fact_claims([
        _block("d", 0, "申请截止：2026年9月30日，逾期不予受理。"),
        _block("d", 1, "开学日期为 2026-09-10。"),  # bare date: not a deadline claim
        _block("d", 2, "完成 3 学分课程且 GPA 不低于 3.2/4.0。"),
        _block("d", 3, "联系人：zjui-oss@example.com，电话 0000-0000000。"),
        _block("d", 4, "本项目名额为 5 人。"),
    ])
    kinds = {(c["kind"], c["value"]) for c in claims}
    assert ("deadline", "2026-09-30") in kinds
    assert ("credits", "3") in kinds
    assert ("condition", "3.2") in kinds
    assert ("contact", "zjui-oss@example.com") in kinds
    assert ("quota", "5") in kinds
    assert all(c["block_id"].startswith("d:") for c in claims)  # anchored
    assert not any(c["kind"] == "deadline" and c["value"] == "2026-09-10" for c in claims)


# --------------------------------------------------------------------- detection


def test_current_docs_disagreeing_same_series_is_candidate():
    docs = [
        _doc("d_a", series_id="s1", is_latest=True, published="2026-01-01"),
        _doc("d_b", series_id="s1", is_latest=True, published="2026-02-01"),
    ]
    claims = {
        "d_a": [{"kind": "deadline", "value": "2026-09-30", "span": "截止：2026年9月30日", "block_id": "d_a:0000"}],
        "d_b": [{"kind": "deadline", "value": "2026-10-15", "span": "deadline: 2026-10-15", "block_id": "d_b:0000"}],
    }
    out = detect_fact_conflicts(docs, claims)
    assert len(out) == 1
    assert out[0]["fact_kind"] == "deadline"
    assert out[0]["series_source"] == "deterministic"
    assert {side["value"] for side in out[0]["values"]} == {"2026-09-30", "2026-10-15"}
    assert out[0]["status"] == "candidate"


def test_agreement_and_superseded_disagreement_produce_no_candidate():
    docs = [
        _doc("d_a", series_id="s1", is_latest=True),
        _doc("d_b", series_id="s1", is_latest=True),
        _doc("d_old", series_id="s1", is_latest=False),
    ]
    claims = {
        "d_a": [{"kind": "deadline", "value": "2026-09-30", "span": "s", "block_id": "x"}],
        "d_b": [{"kind": "deadline", "value": "2026-09-30", "span": "s", "block_id": "x"}],  # agreement
        "d_old": [{"kind": "deadline", "value": "2025-09-30", "span": "s", "block_id": "x"}],  # history
    }
    assert detect_fact_conflicts(docs, claims) == []


def test_llm_candidate_group_conflict(tmp_path: Path):
    docs = [_doc("d_a"), _doc("d_b")]  # no deterministic series at all
    claims = {
        "d_a": [{"kind": "credits", "value": "2", "span": "2 学分", "block_id": "d_a:0"}],
        "d_b": [{"kind": "credits", "value": "3", "span": "3 学分", "block_id": "d_b:0"}],
    }
    out = detect_fact_conflicts(docs, claims, llm_series={"exchange_guide": ["d_a", "d_b"]})
    assert len(out) == 1 and out[0]["series_source"] == "llm_candidate"
    assert out[0]["series_id"] == "candidate:exchange_guide"


# --------------------------------------------------------------------- series graph


def _write(path: Path, rows) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def test_series_graph_merges_without_overwriting(tmp_path: Path):
    (tmp_path / "series.json").write_text(json.dumps([
        {"series_id": "s_x", "key": "scholarship guide", "size": 2,
         "members": [{"doc_uid": "d_a"}, {"doc_uid": "d_b"}]},
    ], ensure_ascii=False), encoding="utf-8")
    def ann(uid, key, status, anchored=True):
        spans = [{"text": "研究生奖学金", "anchors": [{"block_id": f"{uid}:0000"}], "match_count": 1}] if anchored else []
        return {"doc_uid": uid, "llm_annotations": {"series_key_candidate": {"value": key, "status": status, "evidence_spans": spans}}}
    _write(tmp_path / "llm_annotations.jsonl", [
        ann("d_a", "scholarship_guide", "rule_checked"),
        ann("d_c", "scholarship_guide", "rule_checked"),          # unchained doc joins candidate
        ann("d_z", "unanchored_key", "uncertain"),                 # ungated: excluded
    ])
    graph = merge_series_graph(tmp_path)
    assert len(graph["deterministic"]) == 1 and graph["deterministic"][0]["members"] == ["d_a", "d_b"]
    keys = {rec["key"] for rec in graph["candidates"]}
    assert keys == {"scholarship_guide"}  # uncertain candidate excluded
    members = {m["doc_uid"] for rec in graph["candidates"] for m in rec["members"]}
    assert members == {"d_a", "d_c"}
    cross = next(link for link in graph["cross_links"] if link["candidate_key"] == "scholarship_guide")
    assert set(cross["doc_uids_by_series"]) == {"s_x", "(none)"}  # missed-chain signal


# --------------------------------------------------------------------- effective-date ordering (P3-5)


def test_effective_from_breaks_version_ties(tmp_path: Path):
    docs = [
        _doc("d_old", title="研究生奖学金申请指南", effective_from="2025-09-01", published="2026-08-01"),
        _doc("d_new", title="研究生奖学金申请指南", effective_from="2026-09-01", published="2026-01-01"),
    ]
    series = build_series(docs)
    assert len(series) == 1
    # The chain forms on the date difference; effective_from — not published —
    # must decide which edition supersedes which ("自2026年9月1日起施行").
    assert docs[0]["is_latest"] is False and docs[1]["is_latest"] is True
    assert docs[0]["superseded_by"] == "d_new" and docs[1]["superseded_by"] == ""


# --------------------------------------------------------------------- EvidencePack retention


def hit(doc_id, text, *, metadata=None, score=0.8):
    return {
        'chunk': SimpleNamespace(id='chunk-' + doc_id, page=2, heading='Policy', parent_text=text, metadata_json='{}'),
        'document': SimpleNamespace(id=doc_id, title='Official policy', metadata_json=metadata or '{}'),
        'score': score,
    }


def test_pack_labels_fact_conflict_between_co_latest_editions():
    hits = [
        hit('doc-a', 'The 2026 deadline is 2026-09-30.', metadata='{"series_id":"s1","is_latest":true}'),
        hit('doc-b', 'The 2026 deadline is 2026-10-15.', metadata='{"series_id":"s1","is_latest":true}'),
    ]
    pack = build_evidence_pack('What is the 2026 deadline?', hits)
    assert pack['abstain'] is False  # parity: labels, never refuses
    assert pack['fact_conflicts'], pack['conflicts']
    assert pack['evidence_status'] == 'conflict'
    entry = pack['fact_conflicts'][0]
    assert entry['kind'] == 'fact_conflict' and entry['fact_kind'] == 'dates'


def test_pack_retains_known_conflict_counterpart():
    # Coverage selection would keep only doc-a (better score); the known
    # conflict pulls doc-b in so the generator sees both sides verbatim.
    known = [{
        "fact_kind": "deadline", "series_id": "candidate:exchange", "series_source": "llm_candidate",
        "values": [
            {"doc_uid": "doc-a", "block_id": "b1", "value": "2026-09-30", "span": "截止 2026-09-30", "published": "2026-01-01"},
            {"doc_uid": "doc-b", "block_id": "b2", "value": "2026-10-15", "span": "deadline 2026-10-15", "published": "2026-02-01"},
        ],
        "status": "candidate",
    }]
    hits = [
        hit('doc-a', 'Exchange deadline 2026-09-30.', score=0.9),
        hit('doc-b', 'Exchange deadline 2026-10-15.', score=0.3),
    ]
    pack = build_evidence_pack('When is the exchange deadline?', hits, limit=1, known_conflicts=known)
    assert pack['evidence_status'] == 'conflict'
    assert pack['abstain'] is False
    entry = next(c for c in pack['conflicts'] if c['kind'] == 'known_conflict')
    assert {p['doc_uid'] for p in entry['parties']} == {'doc-a', 'doc-b'}
    docs_in_pack = {ev['doc_id'] for ev in pack['evidence']}
    assert docs_in_pack == {'doc-a', 'doc-b'}  # counterpart appended
    counterpart = next(ev for ev in pack['evidence'] if ev['doc_id'] == 'doc-b')
    assert counterpart.get('conflict_counterpart') is True


def test_pack_ignores_known_conflicts_without_a_party_present():
    known = [{
        "fact_kind": "deadline", "series_id": "candidate:x", "series_source": "llm_candidate",
        "values": [
            {"doc_uid": "doc-x", "block_id": "b1", "value": "2026-09-30", "span": "s", "published": ""},
            {"doc_uid": "doc-y", "block_id": "b2", "value": "2026-10-15", "span": "s", "published": ""},
        ],
        "status": "candidate",
    }]
    pack = build_evidence_pack('When is the deadline?', [hit('doc-a', 'Deadline is 2026-09-30.')], known_conflicts=known)
    assert pack['evidence_status'] != 'conflict' or not pack['fact_conflicts']
    assert pack['abstain'] is False
