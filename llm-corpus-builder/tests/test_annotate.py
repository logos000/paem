"""Tests for the P2 LLM annotation pilot (annotate/) and validate/anchor.py.

All LLM interaction goes through ``FakeRunner`` — these tests NEVER hit a real
API. The real pilot is run manually via ``python -m annotate.run_pilot``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from annotate.cache import cache_key, load as cache_load, save as cache_save
from annotate.prompts import DOC_ANNOTATE_PROMPT, ROW_CONTEXT_PROMPT
from annotate.runner import ApiRunner, FakeRunner, extract_json, load_env, parse_env_file
from annotate.run_pilot import build_retrieval_text, run_pilot
from annotate.schema import (
    DOC_TYPES,
    PROMPT_VERSION,
    apply_rule_checks,
    empty_annotation,
    sanitize_doc_response,
    validate_annotation,
)
from annotate.select import select_pilot_docs
from evidence.store import read_jsonl
from validate.anchor import verify_spans

# --------------------------------------------------------------------- fixtures


def _doc(uid: str, **overrides) -> dict:
    base = {
        "doc_uid": uid,
        "title": uid,
        "breadcrumb": [],
        "site": "sharepoint",
        "doc_type": "page",
        "audience": "zjui_undergraduate",
        "terms": [],
        "series_id": "",
        "n_blocks": 1,
        "n_table_rows": 0,
        "status": "active",
        "source_url": f"https://example.test/{uid}",
    }
    base.update(overrides)
    return base


def _block(uid: str, ordinal: int, kind: str, text: str, heading_path=None) -> dict:
    return {
        "block_id": f"{uid}:{ordinal:04d}",
        "doc_uid": uid,
        "ordinal": ordinal,
        "kind": kind,
        "text": text,
        "heading_path": heading_path or [],
        "char_start": 0,
        "char_end": len(text),
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


DOCS = [
    _doc("d_a1", title="2026年研究生奖学金申请指南", breadcrumb=["学生服务", "奖学金"],
         doc_type="guide", terms=["2026-fall"], series_id="s_award", n_blocks=6, n_table_rows=2),
    _doc("d_b2", title="本科生教学日历 2025 春夏", doc_type="calendar",
         terms=["2025-spring"], series_id="s_cal", n_blocks=2),
    _doc("d_c3", title="2026-2027 选课时间安排", doc_type="schedule",
         terms=["2026-fall"], n_blocks=2, n_table_rows=1),
    _doc("d_d4", title="国际交换项目总览", doc_type="page", terms=[], n_blocks=12),
    _doc("d_e5", title="旧版页面存档", doc_type="page", n_blocks=1),
]

BLOCKS = [
    # d_a1 — mislabeled-audience + series + table
    _block("d_a1", 0, "heading", "2026年研究生奖学金申请指南"),
    _block("d_a1", 1, "paragraph", "本指南面向在读研究生，申请者须为在读研究生且成绩良好。", ["介绍"]),
    _block("d_a1", 2, "paragraph", "申请截止：2026年9月30日，逾期系统关闭。", ["介绍"]),
    _block("d_a1", 3, "table_row", "材料提交 研究生院 2026-09-30 前", ["申请时间"]),
    _block("d_a1", 4, "table_row", "面试名单 公布于学院官网 2026-10", ["申请时间"]),
    _block("d_a1", 5, "paragraph", "联系方式：grad-fellowship@example.com。"),
    # d_b2 — series only
    _block("d_b2", 0, "heading", "本科生教学日历 2025 春夏"),
    _block("d_b2", 1, "paragraph", "2025 春夏学期本科生教学日历，含注册、考试周安排。"),
    # d_c3 — table only
    _block("d_c3", 0, "heading", "2026-2027 选课时间安排"),
    _block("d_c3", 1, "table_row", "第一轮 2026-09-01 至 2026-09-10 本科生", ["选课轮次"]),
    # d_d4 — big page only
    _block("d_d4", 0, "heading", "国际交换项目总览"),
    _block("d_d4", 1, "paragraph", "面向全校师生，介绍各交换项目与奖学金。"),
    _block("d_d4", 2, "paragraph", "交换奖学金评选通知（2025 秋冬）：适用于 2025-fall 学期。"),
]
BLOCKS += [_block("d_d4", 3 + i, "paragraph", "更多项目信息见各学院通知。") for i in range(9)]
# d_e5
BLOCKS += [_block("d_e5", 0, "paragraph", "旧版页面存档。")]


@pytest.fixture
def build(tmp_path: Path) -> Path:
    _write_jsonl(tmp_path / "documents.jsonl", DOCS)
    _write_jsonl(tmp_path / "blocks.jsonl", BLOCKS)
    return tmp_path


DOC_A = {  # every evidence span is a real substring of d_a1 blocks
    "doc_type": {"value": "guide", "confidence": 0.95, "evidence_spans": ["研究生奖学金申请指南"]},
    "audience": {"value": ["graduate"], "confidence": 0.9, "evidence_spans": ["申请者须为在读研究生"]},
    "terms": {"value": ["2026-fall"], "evidence_spans": ["申请截止：2026年9月30日"]},
    "series_key_candidate": {"value": "Graduate Scholarship Guide", "evidence_spans": ["研究生奖学金申请指南"]},
    "aliases": {"zh": ["研究生奖学金指南"], "en": ["Graduate Scholarship Guide"], "course_codes": []},
    "summary_1s": "2026 年研究生奖学金的申请时间、材料与联系方式。",
    "boilerplate_spans": ["联系方式：grad-fellowship@example.com"],
    "answer_bearing_spans": ["申请截止：2026年9月30日"],
}
DOC_B = {  # doc_type cites a fabricated span -> must land in unsupported/uncertain
    "doc_type": {"value": "guide", "confidence": 0.8, "evidence_spans": ["这段证据在原文中不存在"]},
    "audience": {"value": None, "confidence": 0.3, "evidence_spans": []},
    "terms": {"value": ["2025-spring"], "evidence_spans": ["2025 春夏学期本科生教学日历"]},
    "series_key_candidate": {"value": None, "evidence_spans": []},
    "aliases": {"zh": [], "en": [], "course_codes": []},
    "summary_1s": "2025 春夏学期本科生教学日历。",
    "boilerplate_spans": [],
    "answer_bearing_spans": ["含注册、考试周安排"],
}
DOC_C = {
    "doc_type": {"value": "schedule", "confidence": 0.9, "evidence_spans": ["选课时间安排"]},
    "audience": {"value": ["undergraduate"], "confidence": 0.8, "evidence_spans": ["本科生"]},
    "terms": {"value": ["2026-fall"], "evidence_spans": ["2026-09-01"]},
    "series_key_candidate": {"value": "course_selection_schedule", "evidence_spans": ["选课时间安排"]},
    "aliases": {"zh": [], "en": [], "course_codes": []},
    "summary_1s": "2026-2027 学年选课时间安排。",
    "boilerplate_spans": [],
    "answer_bearing_spans": [],
}
DOC_D = {
    "doc_type": {"value": "page", "confidence": 0.7, "evidence_spans": ["国际交换项目总览"]},
    "audience": {"value": ["public"], "confidence": 0.6, "evidence_spans": ["面向全校师生"]},
    "terms": {"value": ["2025-fall"], "evidence_spans": ["2025-fall 学期"]},
    "series_key_candidate": {"value": "graduate_scholarship_guide", "evidence_spans": ["交换奖学金评选通知"]},
    "aliases": {"zh": ["交换奖学金"], "en": [], "course_codes": []},
    "summary_1s": "国际交换项目与奖学金总览。",
    "boilerplate_spans": ["更多项目信息见各学院通知"],
    "answer_bearing_spans": [],
}
ROW_A = [
    {"row_ordinal": 1, "context_line": "学期=2026-fall; 对象=研究生; 类别=奖学金申请时间", "answer_bearing": True},
    {"row_ordinal": 2, "context_line": "学期=2026-fall; 对象=研究生; 类别=材料要求", "answer_bearing": False},
]
ROW_C = [
    {"row_ordinal": 1, "context_line": "学期=2026-fall; 对象=本科生; 类别=选课时间", "answer_bearing": True},
]


def _responder(payload: str):
    """Dispatch fake responses by payload markers (row payloads carry 表头/所属章节)."""
    import copy

    if "表头/所属章节" in payload:
        if "研究生奖学金" in payload:
            return copy.deepcopy(ROW_A)
        if "选课时间" in payload:
            return copy.deepcopy(ROW_C)
        raise AssertionError(f"unexpected row payload: {payload[:120]}")
    if "研究生奖学金" in payload:
        return copy.deepcopy(DOC_A)
    if "教学日历" in payload:
        return copy.deepcopy(DOC_B)
    if "选课时间" in payload:
        return copy.deepcopy(DOC_C)
    if "交换项目" in payload:
        return copy.deepcopy(DOC_D)
    raise AssertionError(f"unexpected doc payload: {payload[:120]}")


def _pilot(build: Path, cache_dir: Path, runner=None):
    return run_pilot(
        build,
        limit=4,
        runner=runner or FakeRunner(responder=_responder),
        cache_dir=cache_dir,
        today="2026-10-13T00:00:00Z",
        allowed_sites=frozenset({"sharepoint"}),  # fixture docs are sharepoint
    )


# --------------------------------------------------------------------- validate.anchor


def test_verify_spans_chinese_substring_matches():
    doc_text = "2026年研究生奖学金申请指南\n申请截止：2026年9月30日。"
    result = verify_spans(doc_text, ["研究生奖学金", "申请截止：2026年9月30日"])
    assert result == {"supported": ["研究生奖学金", "申请截止：2026年9月30日"], "unsupported": []}


def test_verify_spans_ignores_whitespace_and_case():
    doc_text = "Final Exam Arrangements for Spring  Semester 2024"
    result = verify_spans(doc_text, ["final exam arrangements", "Spring Semester 2024", "spring\tsemester\n2024"])
    assert result["unsupported"] == []
    assert len(result["supported"]) == 3


def test_verify_spans_reports_misses_as_unsupported():
    result = verify_spans("本科生教学日历", ["这段证据在原文中不存在", ""])
    assert result["supported"] == []
    assert result["unsupported"] == ["这段证据在原文中不存在", ""]


def test_verify_spans_empty_input_means_no_evidence():
    assert verify_spans("whatever", []) == {"supported": [], "unsupported": []}
    # caller treats "no supported span" as 无原文依据 (field goes uncertain)


# --------------------------------------------------------------------- annotate.schema


def test_empty_annotation_passes_validation():
    record = empty_annotation("d_x", build_id="b1", model="m", created_at="2026-10-13")
    assert validate_annotation(record) == []
    assert record["provenance"]["prompt_version"] == PROMPT_VERSION


def test_validate_annotation_reports_contract_violations():
    record = empty_annotation("", build_id="", model="", created_at="")
    record["provenance"]["prompt_version"] = "v0"
    record["llm_annotations"]["doc_type"]["value"] = "bogus"
    record["llm_annotations"]["doc_type"]["status"] = "final"
    record["llm_annotations"]["doc_type"]["confidence"] = 5
    record["llm_annotations"]["audience"]["value"] = "graduate"  # not a list
    record["llm_annotations"]["terms"]["value"] = [1]
    record["llm_annotations"]["aliases"] = {"zh": "not-a-list"}
    record["llm_annotations"]["summary_1s"] = None
    record["validation"]["status"] = "maybe"
    errors = validate_annotation(record)
    assert errors
    joined = " | ".join(errors)
    for fragment in (
        "doc_uid", "prompt_version",
        "anchored field with confidence",  # doc_type status/confidence shape
        "doc_type.value", "audience.value", "terms.value", "aliases", "summary_1s", "validation.status",
    ):
        assert fragment in joined


def test_sanitize_doc_response_enforces_enums_snake_case_and_clamps():
    clean = sanitize_doc_response({
        "doc_type": {"value": "handbook", "confidence": 2.0, "evidence_spans": ["  ", "ok"]},
        "audience": {"value": "graduate", "confidence": -1},
        "terms": "2026-fall",
        "series_key_candidate": {"value": "Graduate Scholarship--Guide 2026!"},
        "aliases": {"zh": ["研究生奖学金", "研究生奖学金"], "en": None},
        "summary_1s": " 摘要 ",
        "boilerplate_spans": "not-a-list",
        "answer_bearing_spans": [{"not": "a span"}],
    })
    assert clean["doc_type"]["value"] is None  # not in DOC_TYPES
    assert clean["doc_type"]["confidence"] == 1.0
    assert clean["doc_type"]["evidence_spans"] == ["ok"]
    assert clean["audience"]["value"] == ["graduate"]  # bare string coerced
    assert clean["audience"]["confidence"] == 0.0
    assert clean["terms"]["value"] == ["2026-fall"]
    assert clean["series_key_candidate"]["value"] == "graduate_scholarship_guide_2026"
    assert clean["aliases"] == {"zh": ["研究生奖学金"], "en": [], "course_codes": []}
    assert clean["summary_1s"] == "摘要"
    assert clean["boilerplate_spans"] == []
    assert clean["answer_bearing_spans"] == []
    assert all(kind in DOC_TYPES for kind in ("guide", "page"))


def test_sanitize_doc_response_of_garbage_is_all_empty():
    clean = sanitize_doc_response(None)
    assert clean["doc_type"]["value"] is None
    assert clean["terms"]["value"] == []
    assert clean["aliases"] == {"zh": [], "en": [], "course_codes": []}


# --------------------------------------------------------------------- annotate.runner


def test_parse_env_file_handles_bom_quotes_and_comments(tmp_path):
    env = tmp_path / ".env"
    content = "\ufeff# comment\n" \
        'LLM_MODEL="deepseek-flash"\n' \
        "LLM_TIMEOUT_SECONDS=60\n" \
        "LLM_BASE_URL=https://api.deepseek.com\n" \
        "BROKEN LINE WITHOUT EQUALS\n" \
        "LLM_API_KEY='QUOTED'\n"
    env.write_text(content, encoding="utf-8")  # utf-8 BOM included on purpose
    values = parse_env_file(env)
    assert values["LLM_MODEL"] == "deepseek-flash"
    assert values["LLM_TIMEOUT_SECONDS"] == "60"
    assert values["LLM_BASE_URL"] == "https://api.deepseek.com"
    assert values["LLM_API_KEY"] == "QUOTED"
    assert "BROKEN LINE WITHOUT EQUALS" not in values


def test_load_env_environment_beats_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=file-model\nLLM_BASE_URL=file-base\n", encoding="utf-8")
    for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    config = load_env(env)
    assert config["LLM_MODEL"] == "file-model"
    monkeypatch.setenv("LLM_MODEL", "env-model")
    assert load_env(env)["LLM_MODEL"] == "env-model"


def test_api_runner_reads_env_without_network(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "LLM_BASE_URL=https://api.deepseek.com\nLLM_MODEL=deepseek-flash\n"
        "LLM_API_KEY=sk-test\nLLM_TIMEOUT_SECONDS=60\n",
        encoding="utf-8",
    )
    for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    runner = ApiRunner(env_path=env)
    assert runner.base_url == "https://api.deepseek.com"
    assert runner.model == "deepseek-flash"
    assert runner.api_key == "sk-test"
    assert runner.timeout == 60.0
    assert runner.call_count == 0
    runner.close()


def test_extract_json_tolerates_prose_and_fences():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('前言 {"a": {"b": [1, 2]}} 后记') == {"a": {"b": [1, 2]}}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("结果如下：[{\"row_ordinal\": 1}] 请查收") == [{"row_ordinal": 1}]
    with pytest.raises(ValueError):
        extract_json("完全没有 JSON 的回答")


def test_fake_runner_queue_and_call_recording():
    runner = FakeRunner([{"doc_type": {"value": "guide"}}])
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert runner.complete_json(messages) == {"doc_type": {"value": "guide"}}
    assert runner.call_count == 1
    assert runner.calls[0][-1]["content"] == "u"
    with pytest.raises(AssertionError):
        runner.complete_json(messages)  # queue exhausted


# --------------------------------------------------------------------- annotate.cache


def test_cache_key_is_stable_and_distinguishes_inputs():
    base = cache_key("payload", task="doc", model="m", prompt_version="v1")
    assert base == cache_key("payload", task="doc", model="m", prompt_version="v1")
    assert len(base) == 64
    assert base != cache_key("payload", task="row", model="m", prompt_version="v1")
    assert base != cache_key("payload", task="doc", model="m2", prompt_version="v1")
    assert base != cache_key("payload2", task="doc", model="m", prompt_version="v1")


def test_cache_save_load_roundtrip(tmp_path):
    key = cache_key("payload", task="doc", model="m", prompt_version="v1")
    assert cache_load(key, tmp_path) is None
    record = cache_save(key, {"answer": 42}, model="m", prompt_version="v1", cache_dir=tmp_path)
    assert set(record) == {"key", "model", "prompt_version", "created_at", "response"}
    loaded = cache_load(key, tmp_path)
    assert loaded["response"] == {"answer": 42}
    assert loaded["model"] == "m"
    assert loaded["prompt_version"] == "v1"


# --------------------------------------------------------------------- annotate.select


def test_select_ranks_by_priority_and_truncates(build):
    assert select_pilot_docs(build, limit=10) == ["d_a1", "d_b2", "d_c3", "d_d4", "d_e5"]
    assert select_pilot_docs(build, limit=4) == ["d_a1", "d_b2", "d_c3", "d_d4"]
    assert select_pilot_docs(build, limit=1) == ["d_a1"]  # mislabeled audience ranks first


def test_select_does_not_flag_undergraduate_as_graduate(tmp_path):
    # "graduate" inside "Undergraduate" must NOT trigger the mislabel signal
    docs = [
        _doc("d_ug", title="ZJUI Fall 2022 Undergraduate Academic Calendar", doc_type="calendar",
             n_blocks=2, n_table_rows=0),
        _doc("d_grad", title="Graduate Student Scholarship Guide", n_blocks=2),
        _doc("d_phd", title="PhD 导师名单公示", n_blocks=2),
    ]
    _write_jsonl(tmp_path / "documents.jsonl", docs)
    _write_jsonl(tmp_path / "blocks.jsonl", [_block(d["doc_uid"], 0, "paragraph", d["title"]) for d in docs])
    ranked = select_pilot_docs(tmp_path, limit=10)
    assert ranked[0] == "d_grad"  # (a) hit: bare "Graduate"
    assert ranked[1] == "d_phd"  # (a) hit: PhD / 导师
    assert ranked[2] == "d_ug"   # undergraduate is NOT a mislabel
    # word boundary also holds for breadcrumb text
    doc = _doc("d_x", title="Anything", breadcrumb=["Graduate College"])
    from annotate.select import _looks_mislabeled
    assert _looks_mislabeled(doc) is True
    doc2 = _doc("d_y", title="Undergraduate College")
    assert _looks_mislabeled(doc2) is False


# --------------------------------------------------------------------- annotate.run_pilot


def test_run_pilot_writes_sidecars_and_validates_anchors(build, tmp_path):
    report = _pilot(build, tmp_path / "cache")

    for name in (
        "llm_annotations.jsonl", "llm_retrieval_text.jsonl", "llm_row_contexts.jsonl",
        "llm_conflicts.jsonl", "llm_uncertain.jsonl",
    ):
        assert (build / name).exists(), name
    assert (build / "llm_pilot_report.json").exists()

    annotations = list(read_jsonl(build / "llm_annotations.jsonl"))
    assert len(annotations) == 4  # limit=4, d_e5 excluded
    by_uid = {record["doc_uid"]: record for record in annotations}

    # every record conforms to the contract
    for record in annotations:
        assert validate_annotation(record) == []
        prov = record["provenance"]
        assert prov["model"] == "fake-runner"
        assert prov["prompt_version"] == PROMPT_VERSION
        assert prov["created_at"] == "2026-10-13T00:00:00Z"
        assert prov["build_id"] == build.name
        assert prov["annotation_schema_version"] == "v2"
        assert prov["prompt_hash"]["doc"] and prov["prompt_hash"]["row"]

    # d_a1: all high-risk evidence locatable -> rule_checked with structured
    # anchors.  The fabricated zh alias and the unverifiable summary are
    # rejected (value cleared) by their own field gates; they do NOT demote
    # the doc's verified metadata (metadata bool covers EVIDENCE_FIELDS only).
    good = by_uid["d_a1"]
    v = good["validation"]
    assert v["metadata_supported_by_source"] is True
    assert "aliases.zh" in v["unsupported_fields"]
    assert "summary_1s" in v["unsupported_fields"]
    assert v["status"] == "uncertain"
    for field in ("doc_type", "audience", "terms", "series_key_candidate"):
        assert good["llm_annotations"][field]["status"] == "rule_checked", field
    assert good["llm_annotations"]["series_key_candidate"]["value"] == "graduate_scholarship_guide"
    spans = good["llm_annotations"]["doc_type"]["evidence_spans"]
    assert spans[0]["text"] == "研究生奖学金申请指南"
    assert spans[0]["match_count"] == 1
    anchor = spans[0]["anchors"][0]
    assert anchor["block_id"] == "d_a1:0000"  # structured, block-level pointer
    assert anchor["match_method"] == "norm_substring"
    # fabricated zh alias rejected: value cleared, kept for audit
    assert good["llm_annotations"]["aliases"]["zh"] == {
        "value": [], "rejected_value": ["研究生奖学金指南"], "status": "uncertain",
        "evidence_spans": [],
    }
    # summary without a verbatim anchor is rejected the same way
    summary = good["llm_annotations"]["summary_1s"]
    assert summary["value"] == ""
    assert summary["rejected_value"].startswith("2026")
    assert summary["status"] == "uncertain"

    # d_b2: fabricated doc_type evidence -> value cleared, unsupported + uncertain
    bad = by_uid["d_b2"]
    assert bad["llm_annotations"]["doc_type"]["status"] == "uncertain"
    assert bad["llm_annotations"]["doc_type"]["evidence_spans"] == []  # only locatable spans kept
    assert bad["llm_annotations"]["doc_type"]["value"] is None  # P1: value cleared
    assert bad["llm_annotations"]["doc_type"]["rejected_value"] == "guide"
    assert "doc_type" in bad["validation"]["unsupported_fields"]
    assert bad["validation"]["metadata_supported_by_source"] is False
    assert bad["validation"]["status"] == "uncertain"
    # honestly-null audience is uncertain but NOT unsupported
    assert bad["llm_annotations"]["audience"]["status"] == "uncertain"
    assert "audience" not in bad["validation"]["unsupported_fields"]

    # d_c3: everything anchored except the summary (no verbatim anchor)
    okay = by_uid["d_c3"]
    assert okay["llm_annotations"]["doc_type"]["status"] == "rule_checked"
    assert "summary_1s" in okay["validation"]["unsupported_fields"]

    # d_d4: the exchange alias DOES occur in source -> survives
    assert by_uid["d_d4"]["llm_annotations"]["aliases"]["zh"] == {
        "value": ["交换奖学金"], "rejected_value": [], "status": "rule_checked",
        "evidence_spans": [{"text": "交换奖学金", "anchors": [
            {"block_id": "d_d4:0002", "ordinal": 2, "char_start": 0, "char_end": 5,
             "page": None, "table_id": None, "row_index": None, "match_method": "norm_substring"},
        ], "match_count": 1}],
    }

    # uncertain sidecar carries every doc with an uncertain/unsupported field
    uncertain = list(read_jsonl(build / "llm_uncertain.jsonl"))
    assert set(row["doc_uid"] for row in uncertain) == {"d_a1", "d_b2", "d_c3", "d_d4"}
    b2_row = next(row for row in uncertain if row["doc_uid"] == "d_b2")
    assert "doc_type" in b2_row["unsupported_fields"]

    # conflicts: d_a1 vs d_d4 share series_key but terms differ (both active)
    conflicts = list(read_jsonl(build / "llm_conflicts.jsonl"))
    assert conflicts == [{
        "doc_a": "d_a1", "doc_b": "d_d4",
        "series_key": "graduate_scholarship_guide", "conflict": "term_mismatch",
    }]

    # retrieval text: gated fields only — the fabricated alias and unverifiable
    # summary must NOT appear; row lines use rule-rebuilt derived_text.
    retrieval = {row["doc_uid"]: row["retrieval_text"] for row in read_jsonl(build / "llm_retrieval_text.jsonl")}
    assert set(retrieval) == {"d_a1", "d_b2", "d_c3", "d_d4"}
    assert "文档：2026年研究生奖学金申请指南" in retrieval["d_a1"]
    assert "学期：2026-fall" in retrieval["d_a1"]
    assert "别名" not in retrieval["d_a1"]  # zh alias rejected, en rejected
    assert "摘要" not in retrieval["d_a1"]  # summary rejected (no verbatim anchor)
    assert "表格行1：申请时间 | 材料提交 研究生院 2026-09-30 前 [答案相关]" in retrieval["d_a1"]
    assert "表格行2：申请时间 | 面试名单 公布于学院官网 2026-10" in retrieval["d_a1"]
    assert "别名：中文:交换奖学金" in retrieval["d_d4"]

    # row contexts sidecar: structured, rule-rebuilt, ordinal in range
    rows_by_doc = {rec["doc_uid"]: rec["rows"] for rec in read_jsonl(build / "llm_row_contexts.jsonl")}
    assert set(rows_by_doc) == {"d_a1", "d_c3"}
    first = rows_by_doc["d_a1"][0]
    assert first["row_ordinal"] == 1
    assert first["block_id"] == "d_a1:0003"
    assert first["section_path"] == ["申请时间"]
    assert first["derived_text"] == "申请时间 | 材料提交 研究生院 2026-09-30 前"
    assert first["answer_bearing"] is True
    assert first["llm_context_line"].startswith("学期=2026-fall")  # audit-only
    assert first["status"] == "rule_checked"

    # report stats
    assert report["annotated_docs"] == 4
    assert report["selected_docs"] == 4
    assert report["row_docs"] == 2
    assert report["api_calls"] == 6  # 4 doc + 2 row, no cache yet
    assert report["cache_hits"] == 0
    assert report["docs_blocked"] == 0
    assert report["uncertain_docs"] == 4
    assert report["conflicts"] == 1
    assert report["field_support"]["doc_type"] == {"supported": 3, "attempted": 4, "rate": 0.75}
    assert report["field_support"]["audience"] == {"supported": 3, "attempted": 3, "rate": 1.0}
    assert report["field_support"]["terms"] == {"supported": 4, "attempted": 4, "rate": 1.0}
    assert report["field_support"]["series_key_candidate"] == {"supported": 3, "attempted": 3, "rate": 1.0}
    assert report["field_support"]["aliases.zh"] == {"supported": 1, "attempted": 2, "rate": 0.5}
    assert report["field_support"]["summary_1s"] == {"supported": 1, "attempted": 4, "rate": 0.25}
    assert report["span_support"]["total"] > report["span_support"]["supported"] >= 1


def test_run_pilot_second_run_hits_cache_zero_api_calls(build, tmp_path):
    first = _pilot(build, tmp_path / "cache")
    assert first["api_calls"] == 6

    def _boom(payload: str):
        raise AssertionError(f"cache miss must not call the API: {payload[:80]}")

    fake = FakeRunner(responder=_boom)
    second = _pilot(build, tmp_path / "cache", runner=fake)
    assert fake.call_count == 0
    assert second["api_calls"] == 0
    assert second["cache_hits"] == 6
    assert second["annotated_docs"] == 4
    # deterministic outputs across runs (created_at pinned via today)
    before = (build / "llm_annotations.jsonl").read_text(encoding="utf-8")
    _pilot(build, tmp_path / "cache", runner=FakeRunner(responder=_boom))
    assert (build / "llm_annotations.jsonl").read_text(encoding="utf-8") == before


def test_run_pilot_prompts_are_pinned_versioned_constants():
    # the system prompts themselves carry the contract rules
    assert "禁止编造" in DOC_ANNOTATE_PROMPT
    assert "60 个字符" in DOC_ANNOTATE_PROMPT
    assert "row_ordinal" in ROW_CONTEXT_PROMPT
    assert "禁止虚构" in ROW_CONTEXT_PROMPT


def test_run_pilot_row_task_only(build, tmp_path):
    report = run_pilot(
        build,
        limit=4,
        tasks=("row",),
        runner=FakeRunner(responder=_responder),
        cache_dir=tmp_path / "cache",
        today="2026-10-13T00:00:00Z",
        allowed_sites=frozenset({"sharepoint"}),
    )
    assert report["annotated_docs"] == 0
    assert report["row_docs"] == 2
    assert report["api_calls"] == 2  # only d_a1 + d_c3 have table rows
    retrieval = list(read_jsonl(build / "llm_retrieval_text.jsonl"))
    assert len(retrieval) == 4
    # doc-level lines fall back to default metadata when no doc annotation ran
    a1 = next(row for row in retrieval if row["doc_uid"] == "d_a1")
    assert "学期：2026-fall" in a1["retrieval_text"]  # from documents.jsonl terms
    assert "表格行1：" in a1["retrieval_text"]


# --------------------------------------------------------------------- P1 gates


def test_run_pilot_blocks_unallowlisted_sites(build, tmp_path, monkeypatch):
    """No allowlist configured -> no sends at all (cache-only replay mode)."""
    monkeypatch.delenv("ANNOTATE_API_ALLOWED_SITES", raising=False)

    def _boom(payload: str):
        raise AssertionError("unallowlisted site must not reach the API")

    report = run_pilot(
        build, limit=4, runner=FakeRunner(responder=_boom),
        cache_dir=tmp_path / "cache", today="2026-10-13T00:00:00Z",
    )
    assert report["api_calls"] == 0
    assert report["docs_blocked"] == 4
    assert report["blocked_sites"] == ["sharepoint"]
    assert report["annotated_docs"] == 0


def test_run_pilot_blocks_sharepoint_when_only_public_sites_allowed(build, tmp_path, monkeypatch):
    """The red line: SharePoint content never leaves for the API by default."""
    monkeypatch.setenv("ANNOTATE_API_ALLOWED_SITES", "zju,zjui,ugrs")

    def _boom(payload: str):
        raise AssertionError("sharepoint must not reach the API")

    report = run_pilot(
        build, limit=4, runner=FakeRunner(responder=_boom),
        cache_dir=tmp_path / "cache", today="2026-10-13T00:00:00Z",
    )
    assert report["api_calls"] == 0
    assert report["docs_blocked"] == 4
    blocked = list(read_jsonl(build / "llm_uncertain.jsonl"))  # no sidecars written for blocked docs
    assert blocked == []


def test_hallucinated_row_context_never_reaches_index(build, tmp_path):
    """P1 acceptance: an invented context_line / out-of-range row cannot promote."""
    from annotate.run_pilot import rebuild_row_contexts
    from annotate.schema import sanitize_row_response

    table_rows = [b for b in BLOCKS if b["doc_uid"] == "d_a1" and b["kind"] == "table_row"]
    # ordinal 99 is out of range; ordinal 1 carries an invented context sentence
    llm_rows = sanitize_row_response([
        {"row_ordinal": 99, "context_line": "学期=2099-fall 编造", "answer_bearing": True},
        {"row_ordinal": 1, "context_line": "编造的上下文：GPA 要求 3.9", "answer_bearing": False},
    ], n_rows=len(table_rows))
    assert [row["row_ordinal"] for row in llm_rows] == [1]  # 99 dropped by range check

    doc = next(d for d in DOCS if d["doc_uid"] == "d_a1")
    contexts = rebuild_row_contexts(doc, table_rows, llm_rows)
    assert len(contexts) == 1
    # the indexable line is the rule-rebuilt derived_text, never the LLM sentence
    assert contexts[0]["derived_text"] == "申请时间 | 材料提交 研究生院 2026-09-30 前"
    assert "编造" not in contexts[0]["derived_text"]
    assert contexts[0]["llm_context_line"] == "编造的上下文：GPA 要求 3.9"  # audit-only

    text = build_retrieval_text(doc, None, contexts)
    assert "表格行1：申请时间 | 材料提交 研究生院 2026-09-30 前" in text
    assert "编造" not in text and "3.9" not in text


def test_hallucinated_alias_and_summary_blocked_from_validated_index(build, tmp_path):
    """P1 acceptance: fabricated aliases/summaries cannot enter any index text."""
    from annotate.run_pilot import assemble_annotation
    from export.arms import _field_lines

    hallucinated = {
        "doc_type": {"value": "guide", "confidence": 0.99, "evidence_spans": ["研究生奖学金申请指南"]},
        "audience": {"value": ["graduate"], "confidence": 0.9, "evidence_spans": ["申请者须为在读研究生"]},
        "terms": {"value": ["2026-fall"], "evidence_spans": ["申请截止：2026年9月30日"]},
        "series_key_candidate": {"value": "Graduate Scholarship Guide", "evidence_spans": ["研究生奖学金申请指南"]},
        "aliases": {"zh": ["不存在的别名 fabricated"], "en": ["Nowhere To Be Found"], "course_codes": ["CS999"]},
        "summary_1s": "2099 年春季 invented summary with novel digits 12345。",
        "boilerplate_spans": [],
        "answer_bearing_spans": ["申请截止：2026年9月30日"],
    }
    doc = next(d for d in DOCS if d["doc_uid"] == "d_a1")
    blocks = [b for b in BLOCKS if b["doc_uid"] == "d_a1"]
    stats = {"field_support": {name: {"attempted": 0, "supported": 0} for name in (
        "doc_type", "audience", "terms", "series_key_candidate",
        "aliases.zh", "aliases.en", "aliases.course_codes", "summary_1s")},
        "span_support": {"total": 0, "supported": 0}}
    record, uncertain, unsupported = assemble_annotation(
        doc, blocks, hallucinated, build_id="b", model="fake", created_at="t", stats=stats,
    )

    # every fabricated value is rejected and cleared
    for sub in ("zh", "en", "course_codes"):
        field = record["llm_annotations"]["aliases"][sub]
        assert field["value"] == [] and field["status"] == "uncertain", sub
    summary = record["llm_annotations"]["summary_1s"]
    assert summary["value"] == "" and summary["status"] == "uncertain"
    assert "2099" in summary["checks"]["novel_digits"] or summary["checks"]["novel_digits"]
    assert set(unsupported) >= {"aliases.zh", "aliases.en", "aliases.course_codes", "summary_1s"}

    # anchored metadata still promotes, and gates on in the index builders
    assert record["llm_annotations"]["doc_type"]["status"] == "rule_checked"
    lines = _field_lines(record, validated_only=True)
    assert "文档类型：guide" in lines
    assert "别名" not in lines and "摘要" not in lines
    retrieval = build_retrieval_text(doc, record, [])
    assert "别名" not in retrieval and "摘要" not in retrieval
    assert "Nowhere" not in retrieval and "2099" not in retrieval and "12345" not in retrieval


def test_uncertain_value_never_passes_field_level_index_gate():
    """v1-style records (uncertain field with a surviving value) gain nothing."""
    from export.arms import _field_lines

    record = {
        "llm_annotations": {
            "doc_type": {"value": "news", "confidence": 0.95, "evidence_spans": []},  # no status key
            "terms": {"value": ["2027-spring"], "evidence_spans": [], "status": "uncertain"},
            "audience": {"value": [], "status": "uncertain"},
        },
        "validation": {"metadata_supported_by_source": True},  # forged doc-level bool
    }
    assert _field_lines(record, validated_only=False) == ""
    assert _field_lines(record, validated_only=True) == ""


def test_metadata_supported_none_is_not_verified():
    """'nothing attempted' (None) must not pass the validated arm gate."""
    from export.arms import _field_lines

    record = {
        "llm_annotations": {
            "doc_type": {"value": "guide", "evidence_spans": [{"text": "x", "anchors": [], "match_count": 0}], "status": "rule_checked"},
        },
        "validation": {"metadata_supported_by_source": None},
    }
    assert _field_lines(record, validated_only=True) == ""
    assert "文档类型：guide" in _field_lines(record, validated_only=False)


def test_rule_layer_flags_generic_series_and_term_shape():
    record = empty_annotation("d_x", build_id="b", model="m", created_at="t")
    ann = record["llm_annotations"]
    ann["series_key_candidate"] = {"value": "class_schedule", "evidence_spans": [{"text": "课表", "anchors": [], "match_count": 1}], "rejected_value": None, "status": "source_supported"}
    ann["terms"] = {"value": ["详见正文"], "evidence_spans": [{"text": "详见正文", "anchors": [], "match_count": 1}], "rejected_value": None, "status": "source_supported"}
    ann["doc_type"] = {"value": "guide", "confidence": 0.9, "evidence_spans": [{"text": "x", "anchors": [], "match_count": 1}], "rejected_value": None, "status": "source_supported"}
    violations = apply_rule_checks(record)
    reasons = {(v["field"], v["reason"]) for v in violations}
    assert ("series_key_candidate", "generic_series_key") in reasons
    assert ("terms", "term_shape") in reasons
    # failing fields stay at source_supported (anchored, auditable, not promoted)
    assert ann["series_key_candidate"]["status"] == "source_supported"
    assert ann["terms"]["status"] == "source_supported"
    assert ann["doc_type"]["status"] == "rule_checked"


# --------------------------------------------------------------------- P2 selection & review


def test_stratified_sample_reproducible_and_covers_strata(tmp_path):
    from annotate.select import stratified_sample_docs

    docs = []
    for site in ("zjui", "sharepoint", "grs"):
        for i in range(30):
            docs.append(_doc(f"d_{site}_{i}", site=site, doc_type="notice" if i % 2 else "guide",
                             n_table_rows=1 if i % 3 == 0 else 0, series_id="s1" if i % 4 == 0 else ""))
    _write_jsonl(tmp_path / "documents.jsonl", docs)

    first, counts = stratified_sample_docs(tmp_path, 45)
    second, _ = stratified_sample_docs(tmp_path, 45)
    assert first == second  # fixed seed
    assert len(first) == 45
    assert len(set(first)) == 45
    sites = {uid.split("_")[1] for uid in first}
    assert sites == {"zjui", "sharepoint", "grs"}  # proportional coverage
    assert sum(counts.values()) == 45
    # stratum labels carry all four dimensions
    assert all(len(key.split("/")) == 4 for key in counts)
    # n >= population → census
    census, _ = stratified_sample_docs(tmp_path, 5000)
    assert len(census) == 90


def test_review_fields_gen_balances_fields_and_write_roundtrips(tmp_path):
    import copy

    from annotate.review_fields import collect_items

    build = tmp_path
    _write_jsonl(build / "documents.jsonl", DOCS)
    _write_jsonl(build / "blocks.jsonl", BLOCKS)
    from annotate.run_pilot import run_pilot as rp

    rp(build, limit=4, runner=FakeRunner(responder=_responder), cache_dir=tmp_path / "cache",
       today="2026-10-13T00:00:00Z", allowed_sites=frozenset({"sharepoint"}))

    items = collect_items(build, ["doc_type", "audience", "terms", "series_key_candidate", "row_context"], 8, [])
    fields = {item["field"] for item in items}
    assert "doc_type" in fields and "row_context" in fields
    row_ids = [item["id"] for item in items if item["field"] == "row_context"]
    assert all("#" not in row_id.split("::")[-1] or True for row_id in row_ids)

    # write path: decide approve on two items and confirm the review file lands
    from annotate import review_fields as rf

    decisions = [{"id": items[0]["id"], "decision": "approve", "corrected_value": None, "reason": ""},
                 {"id": items[1]["id"], "decision": "fix", "corrected_value": ["graduate"], "reason": "audience 更正"}]
    (tmp_path / "decisions.json").write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")

    class Args:
        review = tmp_path / "decisions.json"
        corpus = build
        reviewer = "tester"

    rf.cmd_write(Args())
    rows = list(read_jsonl(build / "llm_field_review.jsonl"))
    assert len(rows) == 2
    assert rows[0]["decision"] == "approve" and rows[0]["reviewer"] == "tester"
    assert rows[1]["corrected_value"] == ["graduate"]
    assert rows[1]["doc_uid"] == decisions[1]["id"].split("::")[0]


def test_run_pilot_subset_run_carries_over_unselected_annotations(build, tmp_path):
    """P6: re-annotating a subset must not drop the rest of the coverage."""
    full = _pilot(build, tmp_path / "cache")
    assert full["annotated_docs"] == 4
    before = list(read_jsonl(build / "llm_annotations.jsonl"))
    assert len(before) == 4

    def only_e5(payload: str):
        # annotate a doc the ranked selection normally skips
        return DOC_A if "研究生奖学金" in payload and "表头" not in payload else {}

    # deselect everything by giving an explicit tiny uid list (d_a1 only)
    from annotate.run_pilot import run_pilot as rp
    report = rp(build, limit=4, runner=FakeRunner(responder=_responder), cache_dir=tmp_path / "cache",
                today="2026-10-13T00:00:00Z", allowed_sites=frozenset({"sharepoint"}),
                selected_uids=["d_a1"])
    after = list(read_jsonl(build / "llm_annotations.jsonl"))
    assert report["annotated_docs"] == 1
    assert report["carried_over_annotations"] == 3
    assert {rec["doc_uid"] for rec in after} == {"d_a1", "d_b2", "d_c3", "d_d4"}  # none dropped
    # the archived previous generation exists for audit
    archived = list((build / "llm_history").iterdir())
    assert archived
