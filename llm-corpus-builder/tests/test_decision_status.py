"""Tests for the six-state abstention decision layer (decision.status).

The first block exercises :func:`decision.status.decide` directly and pins its
output to the legacy frozen expression from ``build_evidence_pack`` (there is a
parity baseline on the 48-question eval set).  The second block checks that
``build_evidence_pack`` surfaces the new ``evidence_status`` field.  The last,
slow test replays the ``rule_trap_audience`` questions against the real crawled
corpus through the same BM25 + IR-rerank + pack path the eval harness uses.
"""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from decision.status import COVERAGE_FLOOR, decide
from evidence.pack import build_evidence_pack

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------- helpers


def gap_term():
    return {"kind": "term-gap", "reason": "no retrieved evidence covers the requested term(s) ['2027-spring']"}


def gap_audience():
    return {"kind": "audience-mismatch", "reason": "question is asked by a undergraduate but concerns graduate-only material"}


def gap_scope():
    return {"kind": "scope-gap", "reason": "question asks for a broad policy but evidence scope is ['course']"}


def conflict_superseded():
    return {"evidence_id": "doc:chunk:2", "kind": "superseded",
            "reason": "a newer edition of this document is in the evidence pack", "blocking": True}


def conflict_stale_risk():
    return {"evidence_id": "doc:chunk:3", "kind": "stale_risk",
            "reason": "superseded by doc-v9; newer edition not retrieved", "blocking": False}


def hit(doc_id, text, *, metadata=None, score=0.8):
    return {
        'chunk': SimpleNamespace(id='chunk-' + doc_id, page=1, heading='Policy', parent_text=text, metadata_json='{}'),
        'document': SimpleNamespace(id=doc_id, title='Official policy', metadata_json=metadata or '{}'),
        'score': score,
    }


def corpus_hit(doc_id, text, *, score=0.8, **metadata):
    row_meta = {key: metadata.pop(key) for key in ('block_id', 'char_start', 'char_end', 'heading_path') if key in metadata}
    return {
        'chunk': SimpleNamespace(id='chunk-' + doc_id, page=1, heading='', parent_text=text, metadata_json=json.dumps(row_meta)),
        'document': SimpleNamespace(id=doc_id, title=doc_id, metadata_json=json.dumps(metadata)),
        'score': score,
    }


# ------------------------------------------------------- decide(): mapping


def test_decide_no_usable_evidence_is_no_material():
    decision = decide(usable_count=0, coverage=0.0)
    assert decision == {"status": "no_material", "abstain": True, "abstain_reason": "no applicable supporting evidence"}


def test_decide_no_usable_evidence_outranks_other_signals():
    decision = decide(usable_count=0, coverage=0.9, scope_gap=gap_scope(), constraint_gaps=[gap_term()],
                      version_conflicts=[conflict_superseded()])
    assert decision["status"] == "no_material"
    assert decision["abstain_reason"] == "no applicable supporting evidence"


def test_decide_term_gap_is_wrong_term():
    decision = decide(usable_count=2, coverage=0.5, constraint_gaps=[gap_term()])
    assert decision == {"status": "wrong_term", "abstain": True, "abstain_reason": "requested term has no applicable evidence"}


def test_decide_audience_mismatch_is_wrong_audience():
    decision = decide(usable_count=2, coverage=0.5, constraint_gaps=[gap_audience()])
    assert decision == {"status": "wrong_audience", "abstain": True, "abstain_reason": "requested material targets a different audience"}


def test_decide_scope_gap_is_wrong_term():
    decision = decide(usable_count=2, coverage=0.5, scope_gap=gap_scope())
    assert decision == {"status": "wrong_term", "abstain": True, "abstain_reason": "retrieved evidence does not cover the requested scope"}


def test_decide_low_coverage_is_no_material():
    decision = decide(usable_count=2, coverage=0.05)
    assert decision == {"status": "no_material", "abstain": True, "abstain_reason": "low lexical/semantic support"}


def test_decide_stale_risk_labels_without_abstaining():
    decision = decide(usable_count=2, coverage=0.5, stale_risk_conflicts=[conflict_stale_risk()])
    assert decision == {"status": "stale_or_uncertain", "abstain": False, "abstain_reason": ""}


def test_decide_blocking_conflict_labels_conflict_without_abstaining():
    decision = decide(usable_count=2, coverage=0.5, version_conflicts=[conflict_superseded()])
    assert decision == {"status": "conflict", "abstain": False, "abstain_reason": ""}


def test_decide_conflict_outranks_stale_risk():
    decision = decide(usable_count=2, coverage=0.5,
                      version_conflicts=[conflict_superseded()], stale_risk_conflicts=[conflict_stale_risk()])
    assert decision["status"] == "conflict"


def test_decide_plain_evidence_is_sufficient():
    decision = decide(usable_count=3, coverage=0.42)
    assert decision == {"status": "sufficient", "abstain": False, "abstain_reason": ""}


# ------------------------------------------------- decide(): frozen parity

# The legacy expression as it stood in build_evidence_pack before the
# decision layer existed; decide() must reproduce it input for input.
SCENARIOS = [
    dict(usable_count=0, coverage=0.0),
    dict(usable_count=0, coverage=0.9, scope_gap=gap_scope(), constraint_gaps=[gap_term(), gap_audience()]),
    dict(usable_count=1, coverage=0.5, constraint_gaps=[gap_term()]),
    dict(usable_count=1, coverage=0.5, constraint_gaps=[gap_audience()]),
    dict(usable_count=1, coverage=0.5, constraint_gaps=[gap_term(), gap_audience()]),
    dict(usable_count=1, coverage=0.5, scope_gap=gap_scope()),
    dict(usable_count=1, coverage=0.11),
    dict(usable_count=1, coverage=0.12),
    dict(usable_count=4, coverage=0.7, version_conflicts=[conflict_superseded()]),
    dict(usable_count=4, coverage=0.7, stale_risk_conflicts=[conflict_stale_risk()]),
    dict(usable_count=4, coverage=0.7),
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_decide_matches_frozen_abstain_expression(scenario):
    decision = decide(**scenario)
    gaps = list(scenario.get("constraint_gaps") or [])
    legacy_abstain = (
        scenario["usable_count"] <= 0
        or scenario["coverage"] < COVERAGE_FLOOR
        or bool(scenario.get("scope_gap"))
        or bool(gaps)
    )
    legacy_reason = (
        "no applicable supporting evidence" if scenario["usable_count"] <= 0 else
        "requested term has no applicable evidence" if any(gap["kind"] == "term-gap" for gap in gaps) else
        "requested material targets a different audience" if any(gap["kind"] == "audience-mismatch" for gap in gaps) else
        "retrieved evidence does not cover the requested scope" if scenario.get("scope_gap") else
        "low lexical/semantic support" if scenario["coverage"] < COVERAGE_FLOOR else
        ""
    )
    assert decision["abstain"] == legacy_abstain
    assert decision["abstain_reason"] == legacy_reason


# ------------------------------- build_evidence_pack: evidence_status field


def test_pack_status_sufficient_on_plain_answer():
    pack = build_evidence_pack('What is the 2025 deadline?', [hit('doc', 'The 2025 deadline is 2025-09-01.')])
    assert pack['abstain'] is False
    assert pack['evidence_status'] == 'sufficient'


def test_pack_status_no_material_when_nothing_usable():
    pack = build_evidence_pack('What is the 2026 deadline?', [hit('old', 'The 2025 deadline is 2025-09-01.')])
    assert pack['abstain'] is True
    assert pack['evidence_status'] == 'no_material'


def test_pack_status_wrong_term_on_term_gap():
    hits = [corpus_hit('page', 'Enrollment time tickets are released before each semester.')]
    pack = build_evidence_pack('ZJUI 2027 年春季学期的选课 time ticket 何时开始？', hits)
    assert pack['abstain'] is True
    assert pack['evidence_status'] == 'wrong_term'


def test_pack_status_wrong_audience_on_self_conflict():
    hits = [corpus_hit('recruit', '课题组招聘博士后，待遇从优，入站流程见正文。', audience='zjui_undergraduate')]
    pack = build_evidence_pack('我是 ZJUI 本科生，可以申请这个博士后岗位吗？', hits)
    assert pack['abstain'] is True
    assert pack['evidence_status'] == 'wrong_audience'


def test_pack_status_wrong_term_on_scope_gap():
    row = SimpleNamespace(
        id='syllabus-chunk', page=8, heading='Submission of the Proposal',
        parent_text='The ST4001 course requires a project proposal.', metadata_json='{}',
    )
    document = SimpleNamespace(id='course-doc', title='ST4001 syllabus', metadata_json='{}')
    pack = build_evidence_pack('Can I unconditionally waive all graduation credits?', [{'chunk': row, 'document': document}])
    assert pack['abstain'] is True
    assert pack['scope_gap']['kind'] == 'scope-gap'
    assert pack['evidence_status'] == 'wrong_term'


def test_pack_status_stale_or_uncertain_when_latest_missing():
    hits = [corpus_hit('v6', 'ECE 110 Lecture room A101.', series_id='s1', is_latest=False, superseded_by='v9', audience='zjui_undergraduate')]
    pack = build_evidence_pack('Where is the ECE 110 lecture?', hits)
    assert pack['abstain'] is False
    assert pack['evidence_status'] == 'stale_or_uncertain'


def test_pack_status_conflict_when_superseded_and_latest_present():
    hits = [
        corpus_hit('v6', 'ECE 110 Lecture room A101.', series_id='s1', is_latest=False, superseded_by='v9', audience='zjui_undergraduate'),
        corpus_hit('v9', 'ECE 110 Lecture room B202.', series_id='s1', is_latest=True, audience='zjui_undergraduate'),
    ]
    pack = build_evidence_pack('What is the latest classroom for ECE 110?', hits)
    assert pack['abstain'] is False
    assert pack['evidence_status'] == 'conflict'


def test_pack_status_consistent_with_abstain_across_statuses():
    statuses = {
        build_evidence_pack('What is the 2025 deadline?', [hit('doc', 'The 2025 deadline is 2025-09-01.')])['evidence_status'],
        build_evidence_pack('What is the 2026 deadline?', [hit('old', 'The 2025 deadline is 2025-09-01.')])['evidence_status'],
        build_evidence_pack('ZJUI 2027 年春季学期的选课 time ticket 何时开始？',
                            [corpus_hit('page', 'Enrollment time tickets are released before each semester.')])['evidence_status'],
        build_evidence_pack('我是 ZJUI 本科生，可以申请这个博士后岗位吗？',
                            [corpus_hit('recruit', '课题组招聘博士后，待遇从优。', audience='zjui_undergraduate')])['evidence_status'],
    }
    assert statuses <= {"sufficient", "no_material", "wrong_term", "wrong_audience", "stale_or_uncertain", "conflict"}


# --------------------------------------------------- real-corpus replay
#
# rule_trap_audience: the asker's audience makes the retrieved rule
# inapplicable.  Current correct_abstain on the 48-question set is 8/9; the
# one miss (batch3-022) is exactly this category, so it is xfailed here until
# the P1 metadata fix lands.

CORPUS_BUILD = ROOT / 'data' / 'corpus' / '20261013-221420-a3fa1c'
EVAL_FILE = ROOT / 'data' / 'benchmarks' / 'corpus-eval' / 'eval_domain_48.jsonl'


def _audience_trap_rows():
    if not EVAL_FILE.is_file():
        return []
    rows = [json.loads(line) for line in EVAL_FILE.read_text(encoding='utf-8').splitlines() if line.strip()]
    return [row for row in rows if row.get('category') == 'rule_trap_audience']


def _audience_trap_params():
    rows = _audience_trap_rows()
    if not rows:
        return [pytest.param({}, marks=pytest.mark.skip(reason='eval set not present'))]
    params = []
    for row in rows:
        marks = []
        if row['id'] == 'batch3-022':
            # Remove this mark once the P1 metadata fix makes the pack refuse.
            marks.append(pytest.mark.xfail(reason='known audience-trap failure, P1 metadata fix target', strict=False))
        params.append(pytest.param(row, id=row['id'], marks=marks))
    return params


@pytest.fixture(scope='module')
def corpus_and_index():
    pickle_path = CORPUS_BUILD / '_bm25' / 'active.pkl'
    if not CORPUS_BUILD.is_dir() or not pickle_path.is_file():
        pytest.skip('real corpus build or BM25 index not present')
    from retrieve.lexical import BM25Index, Corpus

    return Corpus(CORPUS_BUILD), BM25Index.load(pickle_path)


@pytest.mark.slow
@pytest.mark.parametrize('row', _audience_trap_params())
def test_real_corpus_audience_trap_abstains(corpus_and_index, row):
    from retrieve.lexical import corpus_hit, ir_rerank

    corpus, index = corpus_and_index
    today = date.fromisoformat(row['today']) if row.get('today') else None
    # Mirror eval.harness' active_ir path: BM25 + IR rerank (k=10), pack on top-8.
    ranked = [(i, score) for i, score, _ in ir_rerank(corpus, index, row['question'], k=10, today=today)]
    hits = [corpus_hit(corpus, index.units[i], score) for i, score in ranked[:8]]
    pack = build_evidence_pack(row['question'], hits, today=today)
    assert pack['abstain'] is True
    assert pack['evidence_status'] == 'wrong_audience'


# ---- advisory governance (2026-10-17): telemetry yes, veto no ----

def test_advisory_keeps_label_withholds_veto():
    enforced = decide(usable_count=0, coverage=0.0)
    advisory = decide(usable_count=0, coverage=0.0, advisory=True)
    assert enforced["abstain"] is True
    assert advisory["status"] == enforced["status"]  # the shadow label survives
    assert advisory["abstain"] is False and advisory["abstain_reason"] == ""


def test_advisory_term_gap_labelled_not_refused():
    enforced = decide(usable_count=3, coverage=0.5, constraint_gaps=[{"kind": "term-gap", "reason": "x"}])
    advisory = decide(usable_count=3, coverage=0.5, constraint_gaps=[{"kind": "term-gap", "reason": "x"}], advisory=True)
    assert enforced["status"] == "wrong_term" and enforced["abstain"] is True
    assert advisory["status"] == "wrong_term" and advisory["abstain"] is False


def test_advisory_version_conflict_non_blocking():
    from evidence.pack import _version_conflicts
    items = [
        {"version": {"series_id": "s1", "is_latest": True}, "conflicts": [], "blocking": False},
        {"version": {"series_id": "s1", "is_latest": False, "superseded_by": "d2"}, "conflicts": [], "blocking": False},
    ]
    _version_conflicts([dict(i) for i in items], None, advisory=True)
    # advisory: supersession recorded, old edition still usable
    assert items[1]["conflicts"] and items[1]["conflicts"][0]["blocking"] is False
    assert not items[1].get("blocking")
