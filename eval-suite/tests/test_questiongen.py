"""Questiongen sub-project tests: row normalization, leak rule, family split,
lint_ext rules, and gold expansion over a mini corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalsuite.questiongen import generate as gen
from evalsuite.questiongen.lint_ext import lint_generated
from evalsuite.questiongen.schemas import leaks_answer, normalize_row
from evidence.store import write_jsonl


def _row(**over):
    base = {"question": "毕业需要修满多少学分？", "quote": "ZJU requires 135 credits for graduation.",
            "doc_uid": "d1", "block_id": "d1:0001", "category": "fact_general",
            "language": "zh", "tone": "formal", "layer": "L1", "intent": "quantity", "family": "fam1"}
    base.update(over)
    return base


def test_normalize_row_basic_shape():
    row = normalize_row(_row(), qid="q-1", packet_kind="doc", source_doc="d1")
    assert row["id"] == "q-1" and row["gold"][0]["doc_uid"] == "d1"
    assert row["layer"] == "L1" and row["verified"] is False


def test_normalize_row_trap_defaults():
    row = normalize_row(_row(layer="L4", category="rule_trap_audience", question="我是研究生，能参加本科军训吗"),
                        qid="q-2", packet_kind="doc", source_doc="d1", default_today="2026-10-15")
    assert row["gold"] == [] and row["trap_gold"] and row["today"] == "2026-10-15"
    assert row["expected_behavior"] == "refuse_audience_mismatch"
    # audience trap without identity words coerces to future-term
    row2 = normalize_row(_row(layer="L4", category="rule_trap_audience", question="能参加2035年军训吗"),
                         qid="q-3", packet_kind="doc", source_doc="d1", default_today="2026-10-15")
    assert row2["category"] == "rule_trap_future_term"


def test_leaks_answer_course_code_exempt_but_value_not():
    golds = [{"quote": "Course Code: CEE 495 | Course Name: Professional Practice | Credit: 45"}]
    assert not leaks_answer("CEE 495 这门课多少学分", golds)  # code is the ask, not the answer
    assert leaks_answer("CEE 495 是 45 学分吗", golds)  # the credit VALUE leaked
    # single digits stay exempt (noise > signal; the review round catches them)
    assert not leaks_answer("这门课是 4 学分吗", [{"quote": "Credit: 4"}])


def test_split_families_and_pair_uniqueness():
    rows = [
        {"id": "a", "question": "q1", "family": "f1", "gold": [{"doc_uid": "d1", "quote": "x1"}]},
        {"id": "b", "question": "q2", "family": "f1", "gold": [{"doc_uid": "d1", "quote": "x1"}]},
        {"id": "c", "question": "q3", "gold": [{"doc_uid": "d1", "quote": "x1"}]},  # non-family dup gold → dropped
        {"id": "d", "question": "q4", "gold": [{"doc_uid": "d2", "quote": "x2"}]},
    ]
    main, variants = gen._split_families(rows)
    assert [r["id"] for r in main] == ["a", "d"]
    assert [r["id"] for r in variants] == ["b"] and variants[0]["variant_of"] == "f1"


def test_lint_ext_field_and_family_rules():
    bad = [{"id": "x1", "question": "q", "category": "fact_general", "language": "fr", "layer": "L9", "gold": [{"doc_uid": "d", "quote": "x"}], "family": "f"}]
    errors = lint_generated(bad)
    assert any("language=" in e for e in errors) and any("layer=" in e for e in errors)
    assert any("single member" in e for e in errors)


def test_lint_ext_leak_and_duplicate(tmp_path: Path):
    existing = tmp_path / "old.jsonl"
    write_jsonl(existing, [{"id": "old-1", "question": "请问毕业需要修满多少学分才可以"}])
    items = [
        {"id": "n1", "question": "毕业要求135学分，请问是多少？", "category": "fact_general", "layer": "L1",
         "language": "zh", "tone": "formal", "intent": "quantity", "gold": [{"doc_uid": "d", "quote": "requires 135 credits"}]},
        {"id": "n2", "question": "请问毕业需要修满多少学分才可以", "category": "fact_general", "layer": "L1",
         "language": "zh", "tone": "formal", "intent": "quantity", "gold": [{"doc_uid": "d", "quote": "other"}]},
    ]
    errors = lint_generated(items, existing_files=[existing])
    assert any("leaks" in e for e in errors)
    assert any("duplicate of an existing" in e for e in errors)


def test_expand_gold_drops_boilerplate_and_fills_holders(tmp_path: Path):
    root = tmp_path / "build"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"build_id": "t"}), encoding="utf-8")
    write_jsonl(root / "documents.jsonl", [
        {"doc_uid": "d1", "title": "A", "status": "active", "site": "sharepoint"},
        {"doc_uid": "d2", "title": "B", "status": "active", "site": "sharepoint"},
        {"doc_uid": "d3", "title": "C", "status": "active", "site": "sharepoint"},
        {"doc_uid": "d4", "title": "D", "status": "active", "site": "sharepoint"},
    ])
    write_jsonl(root / "blocks.jsonl", [
        {"block_id": "d1:1", "doc_uid": "d1", "kind": "paragraph", "text": "Unique policy sentence about withdrawal."},
        {"block_id": "d2:1", "doc_uid": "d2", "kind": "table_row", "text": "Shared catalog row PHIL 101"},
        {"block_id": "d3:1", "doc_uid": "d3", "kind": "table_row", "text": "Shared catalog row PHIL 101"},
        {"block_id": "d4:1", "doc_uid": "d4", "kind": "table_row", "text": "Shared catalog row PHIL 101"},
    ])
    from retrieve.lexical import Corpus
    corpus = Corpus(root)
    rows = [
        {"id": "keep", "gold": [{"doc_uid": "d1", "quote": "Unique policy sentence about withdrawal."}]},
        {"id": "expand", "gold": [{"doc_uid": "d2", "quote": "Shared catalog row PHIL 101"}]},
    ]
    kept, dropped = gen._expand_gold(rows, corpus)
    assert [r["id"] for r in kept] == ["keep", "expand"]
    assert {g["doc_uid"] for g in kept[1]["gold"]} == {"d2", "d3", "d4"}  # ≤3 holders → all gold
    assert dropped == 0
    rows_boiler = [{"id": "x", "gold": [{"doc_uid": "d2", "quote": "Shared catalog row PHIL 101"}]}] * 1
    four = [dict(rows_boiler[0])]
    # make it 4 holders by adding d5
    write_jsonl(root / "blocks.jsonl", [
        {"block_id": "d1:1", "doc_uid": "d1", "kind": "paragraph", "text": "Unique policy sentence about withdrawal."},
        {"block_id": "d2:1", "doc_uid": "d2", "kind": "table_row", "text": "Shared catalog row PHIL 101"},
        {"block_id": "d3:1", "doc_uid": "d3", "kind": "table_row", "text": "Shared catalog row PHIL 101"},
        {"block_id": "d4:1", "doc_uid": "d4", "kind": "table_row", "text": "Shared catalog row PHIL 101"},
        {"block_id": "d5:1", "doc_uid": "d5", "kind": "table_row", "text": "Shared catalog row PHIL 101"},
    ])
    write_jsonl(root / "documents.jsonl", [
        {"doc_uid": f"d{i}", "title": chr(64 + i), "status": "active", "site": "sharepoint"} for i in range(1, 6)
    ])
    corpus2 = Corpus(root)
    kept2, dropped2 = gen._expand_gold(four, corpus2)
    assert kept2 == [] and dropped2 == 1  # >3 holders → dropped as boilerplate
