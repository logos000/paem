from types import SimpleNamespace

from evidence.pack import build_evidence_pack


def hit(doc_id, text, *, metadata=None, score=0.8):
    return {
        'chunk': SimpleNamespace(id='chunk-' + doc_id, page=2, heading='Policy', parent_text=text, metadata_json='{}'),
        'document': SimpleNamespace(id=doc_id, title='Official policy', metadata_json=metadata or '{}'),
        'score': score,
    }


def test_evidence_pack_preserves_page_and_supports_claim():
    pack = build_evidence_pack('What is the 2025 deadline?', [hit('doc', 'The 2025 deadline is 2025-09-01.')])
    assert pack['abstain'] is False
    assert pack['evidence'][0]['page'] == 2
    assert pack['evidence'][0]['source_hash'] == ''
    assert pack['evidence'][0]['support_score'] > 0


def test_evidence_pack_marks_year_conflict_before_generation():
    pack = build_evidence_pack('What is the 2026 deadline?', [hit('old', 'The 2025 deadline is 2025-09-01.')])
    assert pack['abstain'] is True
    assert any(item['kind'] == 'year' for item in pack['conflicts'])


def test_evidence_pack_reads_json_metadata_context_conflicts():
    pack = build_evidence_pack(
        'When is the deadline?',
        [hit('program', 'Submit before the deadline.', metadata='{"program": "Physics"}')],
        context={'program': 'Chemistry'},
    )
    assert pack['abstain'] is True
    assert any(item['kind'] == 'program' for item in pack['conflicts'])


def test_evidence_pack_keeps_document_revision_provenance():
    document = SimpleNamespace(
        id='doc-v2',
        title='Official policy',
        metadata_json='{"version_id":"2026-spring"}',
        content_hash='sha256-content',
    )
    row = SimpleNamespace(id='chunk-v2', page=4, heading='Rules', parent_text='The deadline is 2026-03-01.', metadata_json='{}')
    pack = build_evidence_pack('What is the 2026 deadline?', [{'chunk': row, 'document': document}])
    item = pack['evidence'][0]
    assert item['version_id'] == '2026-spring'
    assert item['source_hash'] == 'sha256-content'
    assert item['bbox_source'] == ''


def test_evidence_pack_scores_chinese_conditions_and_reports_coverage():
    pack = build_evidence_pack(
        '2025年补选截止日期是什么？',
        [hit('cn', '2025年补选截止日期为9月1日。')],
    )
    assert pack['coverage'] > 0.5
    assert '截止' in pack['covered_terms']
    assert pack['evidence'][0]['matched_terms']


def test_evidence_pack_flags_numeric_premise_without_discarding_evidence():
    # The evidence contradicts the question's premise; it is still the evidence
    # needed to answer "no, it is 3 credits", so it stays usable but flagged.
    pack = build_evidence_pack('Is the requirement 4 credits?', [hit('credits', 'The program requires 3 credits.')])
    assert pack['abstain'] is False
    assert any(item['kind'] == 'number' and not item['blocking'] for item in pack['conflicts'])
    assert [item['kind'] for item in pack['premise_conflicts']] == ['number']
    assert pack['fact_coverage'] == 0.0


def test_evidence_pack_tracks_course_code_fact():
    pack = build_evidence_pack('What is the requirement for CS101?', [hit('course', 'CS101 requires a final project.')])
    assert pack['abstain'] is False
    assert pack['fact_coverage'] == 1.0
    assert 'cs101' in pack['covered_facts']['course_codes']


def test_evidence_pack_extracts_english_dates_and_units():
    pack = build_evidence_pack(
        'Is the proposal deadline 2027-01-09 and is the penalty 10 points?',
        [hit('english', 'The proposal deadline is Jan 9 2027 at 5 pm. A late submission loses 10 points.')],
    )
    assert pack['fact_coverage'] > 0.0
    assert '2027-01-09' in pack['covered_facts'].get('dates', [])
    assert any('10points' in value for value in pack['covered_facts'].get('number_facts', []))


def test_evidence_pack_preserves_table_structure_coordinates():
    row = SimpleNamespace(id='cell', page=3, heading='Schedule', parent_text='4', metadata_json='{"kind":"table_cell","bbox":[0.1,0.2,0.3,0.4],"table_id":"t1","row_index":2,"col_index":1}')
    document = SimpleNamespace(id='table-doc', title='Schedule', metadata_json='{}')
    pack = build_evidence_pack('What is the value in the schedule?', [{'chunk': row, 'document': document}])
    assert pack['evidence'][0]['structure'] == {'table_id': 't1', 'row_index': 2, 'col_index': 1}


def test_evidence_selection_covers_multiple_explicit_facts():
    hits = [
        hit('deadline', 'The 2026 application deadline is 2026-09-01.', score=0.95),
        hit('credits', 'The program requires 4 credits.', score=0.70),
        hit('noise', 'General information about the program.', score=0.99),
    ]
    pack = build_evidence_pack(
        'What are the 2026 application deadline and credit requirement?',
        hits,
        limit=2,
    )
    evidence_ids = {item['doc_id'] for item in pack['evidence']}
    assert evidence_ids == {'deadline', 'credits'}
    assert pack['fact_coverage'] == 1.0


def test_evidence_selection_keeps_rank_order_for_single_condition():
    hits = [
        hit('first', 'The deadline is announced in the official notice.', score=0.90),
        hit('second', 'The deadline is also listed in the handbook.', score=0.80),
    ]
    pack = build_evidence_pack('When is the deadline?', hits, limit=2)
    assert [item['doc_id'] for item in pack['evidence']] == ['first', 'second']


def test_evidence_selection_is_stable_when_no_explicit_facts_exist():
    hits = [
        hit('first', 'General registration guidance.', score=0.90),
        hit('second', 'General course guidance.', score=0.80),
    ]
    first = build_evidence_pack('How does registration work?', hits, limit=1)
    second = build_evidence_pack('How does registration work?', hits, limit=1)
    assert first['evidence'][0]['doc_id'] == 'first'
    assert first['evidence'] == second['evidence']


def test_evidence_selection_reserves_a_compatible_second_source():
    primary = hit('schedule', 'The 2027-01-09 deadline is submission of the project proposal.', score=0.95)
    secondary = hit('syllabus', 'Submission of the Proposal is required through Blackboard.', score=0.40)
    pack = build_evidence_pack('When is the 2027-01-09 project proposal deadline?', [primary, secondary], limit=2)
    assert [item['doc_id'] for item in pack['evidence']] == ['schedule', 'syllabus']


def test_evidence_pack_marks_broad_question_as_scope_gap():
    row = SimpleNamespace(
        id='syllabus-chunk', page=8, heading='Submission of the Proposal',
        parent_text='The ST4001 course requires a project proposal.', metadata_json='{}',
    )
    document = SimpleNamespace(id='course-doc', title='ST4001 syllabus', metadata_json='{}')
    pack = build_evidence_pack(
        'Can I unconditionally waive all graduation credits?',
        [{'chunk': row, 'document': document}],
    )
    assert pack['scope_gap']['kind'] == 'scope-gap'
    assert pack['abstain'] is True
    assert any(item['kind'] == 'scope-gap' for item in pack['conflicts'])



def corpus_hit(doc_id, text, *, score=0.8, **metadata):
    import json
    row_meta = {key: metadata.pop(key) for key in ('block_id', 'char_start', 'char_end', 'heading_path') if key in metadata}
    return {
        'chunk': SimpleNamespace(id='chunk-' + doc_id, page=1, heading='', parent_text=text, metadata_json=json.dumps(row_meta)),
        'document': SimpleNamespace(id=doc_id, title=doc_id, metadata_json=json.dumps(metadata)),
        'score': score,
    }


def test_evidence_pack_uses_query_ir_terms_instead_of_gold_context():
    hits = [
        corpus_hit('fa25', 'Seniors: course selection starts 8:00 pm.', terms=['2025-fall'], audience='zjui_undergraduate'),
        corpus_hit('fa26', 'Seniors: course selection starts 9:00 pm.', terms=['2026-fall'], audience='zjui_undergraduate'),
    ]
    pack = build_evidence_pack('2026秋季学期大四什么时候选课？', hits)
    assert pack['query_ir']['terms'] == ['2026-fall']
    assert pack['usable_evidence_ids'] == [item['evidence_id'] for item in pack['evidence'] if item['doc_id'] == 'fa26']
    blocked = next(item for item in pack['evidence'] if item['doc_id'] == 'fa25')
    assert blocked['blocking'] and blocked['conflicts'][0]['kind'] == 'term'


def test_evidence_pack_blocks_graduate_evidence_for_undergraduate_question():
    hits = [corpus_hit('grs', 'Thesis defence registration closes in May.', audience='graduate', scope_level='university')]
    pack = build_evidence_pack('本科生毕业论文答辩什么时候报名？', hits)
    assert pack['abstain'] is True
    assert pack['evidence'][0]['conflicts'][0]['kind'] == 'audience'


def test_evidence_pack_blocks_superseded_edition_when_latest_is_present():
    hits = [
        corpus_hit('v6', 'ECE 110 Lecture room A101.', series_id='s1', is_latest=False, superseded_by='v9', audience='zjui_undergraduate'),
        corpus_hit('v9', 'ECE 110 Lecture room B202.', series_id='s1', is_latest=True, audience='zjui_undergraduate'),
    ]
    pack = build_evidence_pack('What is the latest classroom for ECE 110?', hits)
    old = next(item for item in pack['evidence'] if item['doc_id'] == 'v6')
    assert any(conflict['kind'] == 'superseded' for conflict in old['conflicts'])
    assert pack['abstain'] is False


def test_evidence_pack_flags_stale_edition_when_latest_not_retrieved():
    hits = [corpus_hit('v6', 'ECE 110 Lecture room A101.', series_id='s1', is_latest=False, superseded_by='v9', audience='zjui_undergraduate')]
    pack = build_evidence_pack('Where is the ECE 110 lecture?', hits)
    assert pack['abstain'] is False
    assert pack['evidence'][0]['conflicts'][0]['kind'] == 'stale_risk'


def test_evidence_pack_scope_uses_corpus_metadata():
    course = corpus_hit('syl', 'ST4001 requires a proposal.', scope_level='course', audience='zjui_undergraduate')
    policy = corpus_hit('reg', 'Graduation requires completing all program credits.', scope_level='university', audience='undergraduate')
    assert build_evidence_pack('Can I waive all graduation credits?', [course])['scope_gap']['kind'] == 'scope-gap'
    assert build_evidence_pack('Can I waive all graduation credits?', [course, policy])['scope_gap'] is None


def test_evidence_pack_exposes_block_provenance():
    hits = [corpus_hit('doc', 'Drop deadline is 5 PM Nov 6.', block_id='d_x:abc', char_start=10, char_end=38,
                       source_url='https://example.edu/drop', doc_uid='d_x', terms=['2026-fall'])]
    item = build_evidence_pack('When is the drop deadline in Fall 2026?', hits)['evidence'][0]
    assert item['provenance']['block_id'] == 'd_x:abc'
    assert item['provenance']['char_start'] == 10
    assert item['provenance']['source_url'] == 'https://example.edu/drop'
    assert item['applicability']['terms'] == ['2026-fall']


def test_evidence_pack_abstains_when_no_evidence_covers_pinned_term():
    # Future-term trap: nothing retrieved declares (or titles) 2027-spring.
    hits = [corpus_hit('page', 'Enrollment time tickets are released before each semester.')]
    pack = build_evidence_pack('ZJUI 2027 年春季学期的选课 time ticket 何时开始？', hits)
    assert pack['abstain'] is True
    assert [gap['kind'] for gap in pack['constraint_gaps']] == ['term-gap']
    assert pack['abstain_reason'] == 'requested term has no applicable evidence'


def test_evidence_pack_answers_when_evidence_declares_the_pinned_term():
    hits = [corpus_hit('guide', 'Seniors register from 2026-10-26.', terms=['2026-fall'])]
    pack = build_evidence_pack('ZJUI Fall 2026 选课中 Seniors 的 time ticket 何时开始？', hits)
    assert pack['constraint_gaps'] == []
    assert pack['abstain'] is False


def test_evidence_pack_title_covers_pinned_term_when_metadata_lacks_it():
    document = SimpleNamespace(
        id='guide', title='Course Enrollment Guide Spring 2026',
        metadata_json='{"audience": "zjui_undergraduate"}',
    )
    pack = build_evidence_pack('Spring 2026 选课什么时候开始？', [{
        'chunk': SimpleNamespace(id='c1', page=1, heading='', parent_text='选课 registration opens in March for Spring 2026.', metadata_json='{}'),
        'document': document,
        'score': 0.8,
    }])
    assert pack['constraint_gaps'] == []
    assert pack['abstain'] is False


def test_evidence_pack_abstains_on_audience_self_conflict():
    hits = [corpus_hit('recruit', '课题组招聘博士后，待遇从优，入站流程见正文。', audience='zjui_undergraduate')]
    pack = build_evidence_pack('我是 ZJUI 本科生，可以申请这个博士后岗位吗？', hits)
    assert pack['abstain'] is True
    assert [gap['kind'] for gap in pack['constraint_gaps']] == ['audience-mismatch']
    assert pack['abstain_reason'] == 'requested material targets a different audience'


def test_evidence_pack_no_audience_gap_when_asker_matches_topic():
    hits = [corpus_hit('rule', '研究生奖学金每学年评审一次。', audience='graduate')]
    pack = build_evidence_pack('我是研究生，硕士奖学金什么时候评审？', hits)
    assert pack['constraint_gaps'] == []


def test_evidence_pack_abstains_when_topic_only_covered_by_other_audience():
    # The question never says 研究生; the mismatch lives in evidence metadata.
    grad = corpus_hit('grad', '拟录取研究生须于5月19日前将思想政治素质和品德鉴定表寄到学院。', audience='graduate')
    noise = corpus_hit('news', '学院近期举行了新生见面会。', audience='zjui_undergraduate')
    pack = build_evidence_pack('我是 ZJUI 大一本科生，也需要在 5 月 19 日前把品德鉴定表寄到学院吗？', [grad, noise])
    assert pack['abstain'] is True
    assert any(gap['kind'] == 'audience-mismatch' for gap in pack['constraint_gaps'])


def test_evidence_pack_no_audience_gap_when_usable_covers_the_same_terms():
    # Same wording exists for undergraduates: nothing is exclusively blocked.
    grad = corpus_hit('grad', '学位论文须在5月提交至学院。', audience='graduate')
    undergrad = corpus_hit('rule', '学位论文须在5月提交至学院。', audience='zjui_undergraduate')
    pack = build_evidence_pack('我是本科生，学位论文什么时候提交到学院？', [grad, undergrad])
    assert all(gap['kind'] != 'audience-mismatch' or 'only evidence' not in gap['reason'] for gap in pack['constraint_gaps'])
    assert pack['abstain'] is False


def test_query_pool_makes_cross_lingual_pack_answerable():
    # 2026-10-16: a Chinese question against English evidence is judged by its
    # LLM-translated restatement — coverage rises, req-terms is satisfied by
    # the English paraphrase group, and the item matches the variant terms.
    question = '计算机工程专业有哪些必修课'
    en_hit = hit('curriculum', 'The Computer Engineering curriculum lists the required core courses and electives.')
    bare = build_evidence_pack(question, [en_hit])
    pack = build_evidence_pack(question, [en_hit], query_pool=['computer engineering required courses curriculum'])
    assert pack['abstain'] is False
    assert pack['coverage'] > bare['coverage']
    assert 'curriculum' in pack['evidence'][0]['matched_terms']
    req_terms = next(r for r in pack['requirements'] if r['requirement_id'] == 'req-terms')
    assert req_terms['status'] == 'covered'
    bare_req = next(r for r in bare['requirements'] if r['requirement_id'] == 'req-terms')
    assert bare_req['status'] == 'missing'


def test_query_pool_still_refuses_junk_evidence():
    # Pooling must not loosen the gate: noise that only echoes the original
    # Chinese tokens stays below the coverage floor even with variants present.
    question = '计算机工程专业有哪些必修课'
    junk = [hit('form', 'Application form. 姓名 (Name): 目前专业 (From Major): ____________', score=0.05)]
    pack = build_evidence_pack(question, junk, query_pool=['computer engineering core required courses curriculum'])
    assert pack['abstain'] is True
    assert pack['evidence_status'] == 'no_material'


def test_requirements_covered_via_question_group_despite_variant_only_gaps():
    # English regression guard: req-terms stays anchored on the question's own
    # tokens — variant-only synonyms being uncovered must not flip it.
    question = 'When is the 2025 deadline?'
    hits = [hit('doc', 'The 2025 deadline is 2025-09-01.')]
    pack = build_evidence_pack(question, hits, query_pool=['deadline schedule date calendar'])
    req_terms = next(r for r in pack['requirements'] if r['requirement_id'] == 'req-terms')
    assert req_terms['status'] == 'covered'
