from evalsuite.reference_audit import structural_issues


def test_duplicate_holders_and_toc_are_not_multihop():
    q = {"level": "C4", "gold": [{"doc_uid": "d1", "quote": "Appendix .... 24"},
                                  {"doc_uid": "d2", "quote": "Appendix .... 24"}]}
    blocks = {d: [{"text": "Appendix .... 24"}] for d in ("d1", "d2")}
    issues = structural_issues(q, blocks)
    assert "multi_hop_missing_distinct_premises" in issues
    assert "table_of_contents_used_as_answer_evidence" in issues


def test_anchor_must_exist_in_selected_build():
    assert structural_issues({"level": "C2", "gold": [{"doc_uid": "d", "quote": "3 credits"}]}, {}) == ["gold_0_not_found_in_selected_build"]
