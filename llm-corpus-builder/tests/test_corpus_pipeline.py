from pathlib import Path

import pytest

from pipeline import assemble_blocks, attachment_title
from curate.dedup import duplicate_groups
from structure.extract import RawBlock
from curate.metadata import DOC_NUMBER_RE, audience, build_metadata, doc_type
from ingest.sources import parse_cms_page
from curate.versions import build_series, series_key, version_number


# ===================================================================== dedup.duplicate_groups

_LONG_BASE = (
    "The university announces that the application deadline for the scholarship program this "
    "year is set to the thirtieth of September and all students who wish to apply must submit "
    "their complete documentation package including transcripts, recommendation letters, and a "
    "personal statement describing their academic achievements and future goals in the program "
    "they have chosen. Late submissions will not be considered under any circumstances and "
    "incomplete applications will be rejected automatically by the review committee before the "
    "final evaluation stage begins in 2024. "
)
assert len(_LONG_BASE) > 500


def test_duplicate_groups_exact_duplicates_grouped():
    docs = [
        {"doc_uid": "d1", "text": _LONG_BASE},
        {"doc_uid": "d2", "text": _LONG_BASE},
        {"doc_uid": "d3", "text": "Something completely unrelated and different from the base text."},
    ]
    groups = duplicate_groups(docs, text_of=lambda d: d["text"])
    assert len(groups) == 1
    assert set(groups[0]["members"]) == {"d1", "d2"}
    assert groups[0]["kind"] == "exact"
    assert groups[0]["max_distance"] == 0


def test_duplicate_groups_near_duplicate_one_word_changed_in_long_text():
    # a single word changed ("deadline" -> "foobar") in a ~570-char notice
    variant = _LONG_BASE.replace("deadline", "foobar", 1)
    docs = [
        {"doc_uid": "d1", "text": _LONG_BASE},
        {"doc_uid": "d2", "text": variant},
    ]
    groups = duplicate_groups(docs, text_of=lambda d: d["text"])
    assert len(groups) == 1
    assert set(groups[0]["members"]) == {"d1", "d2"}
    assert groups[0]["kind"] == "near"
    assert 0 < groups[0]["max_distance"] <= 3


def test_duplicate_groups_near_duplicate_with_zero_hamming_distance_is_near():
    # "package" -> "xyz" happens to leave the 64-bit simhash completely unchanged for this text,
    # so the pair is unioned through the near-duplicate band-matching path with distance 0 -
    # but the texts are not byte-for-byte identical.
    variant = _LONG_BASE.replace("package", "xyz", 1)
    assert variant != _LONG_BASE
    docs = [
        {"doc_uid": "d1", "text": _LONG_BASE},
        {"doc_uid": "d2", "text": variant},
    ]
    groups = duplicate_groups(docs, text_of=lambda d: d["text"])
    assert len(groups) == 1
    assert groups[0]["kind"] == "near"


def test_duplicate_groups_annual_editions_differing_only_in_numbers_not_grouped():
    # same notice text, but the year in it changed -> different fact, not a duplicate
    edition_2025 = _LONG_BASE.replace("2024", "2025")
    docs = [
        {"doc_uid": "d1", "text": _LONG_BASE},
        {"doc_uid": "d2", "text": edition_2025},
    ]
    groups = duplicate_groups(docs, text_of=lambda d: d["text"])
    assert groups == []


def test_duplicate_groups_short_texts_only_grouped_on_exact_match():
    exact_a = {"doc_uid": "s1", "text": "Office hours: 9am to 5pm, Monday to Friday."}
    exact_b = {"doc_uid": "s2", "text": "Office hours: 9am to 5pm, Monday to Friday."}
    near_but_short = {"doc_uid": "s3", "text": "Office hours: 9am to 5pm, Monday to Saturday."}
    groups = duplicate_groups([exact_a, exact_b, near_but_short], text_of=lambda d: d["text"])
    assert len(groups) == 1
    assert set(groups[0]["members"]) == {"s1", "s2"}
    assert groups[0]["kind"] == "exact"


def test_duplicate_groups_empty_text_not_grouped():
    docs = [{"doc_uid": "e1", "text": ""}, {"doc_uid": "e2", "text": ""}]
    assert duplicate_groups(docs, text_of=lambda d: d["text"]) == []


# ===================================================================== versions.py


def test_series_key_strips_terms_and_versions_so_editions_share_a_key():
    a = series_key("V7-ZJUI Class Schedule FA26-with rooms")
    b = series_key("ZJUI Class Schedule FA24 online")
    assert a == b == "zjui class schedule"


def test_series_key_keeps_distinct_cohorts_separate():
    a = series_key("2023级培养方案")
    b = series_key("2024级培养方案")
    assert a != b


def test_version_number_extracts_v_prefixed_number():
    assert version_number("V7-ZJUI Class Schedule FA26-with rooms") == 7.0


def test_version_number_extracts_parenthesized_number():
    assert version_number("Course-Drop-Request(1)") == 1.0


def test_version_number_defaults_to_zero_when_absent():
    assert version_number("ZJUI Class Schedule FA24 online") == 0.0


def _series_doc(uid, title, *, terms=None, academic_years=None, published="", modified="",
                 site="zjui", doc_type="schedule", status="active", page_kind="article"):
    return {
        "doc_uid": uid, "title": title, "site": site, "doc_type": doc_type, "status": status,
        "page_kind": page_kind, "terms": terms or [], "academic_years": academic_years or [],
        "published": published, "modified": modified,
    }


def test_build_series_orders_by_term_and_marks_latest_and_superseded():
    docs = [
        _series_doc("d1", "V7-ZJUI Class Schedule FA24-with rooms", terms=["2024-fall"]),
        _series_doc("d2", "ZJUI Class Schedule FA25 online", terms=["2025-fall"]),
        _series_doc("d3", "ZJUI Class Schedule FA26", terms=["2026-fall"]),
    ]
    series = build_series(docs)
    assert len(series) == 1
    record = series[0]
    assert record["size"] == 3
    assert record["latest"] == "d3"
    assert record["ambiguous_latest"] is False

    by_uid = {d["doc_uid"]: d for d in docs}
    assert by_uid["d1"]["is_latest"] is False
    assert by_uid["d1"]["superseded_by"] == "d2"
    assert by_uid["d2"]["is_latest"] is False
    assert by_uid["d2"]["superseded_by"] == "d3"
    assert by_uid["d3"]["is_latest"] is True
    assert by_uid["d3"]["superseded_by"] == ""
    assert [by_uid[u]["version_order"] for u in ("d1", "d2", "d3")] == [1, 2, 3]


def test_build_series_single_document_gets_default_latest_fields():
    solo = _series_doc("solo", "A completely unique one-off document title")
    series = build_series([solo])
    assert series == []
    assert solo["is_latest"] is True
    assert solo["series_id"] == ""
    assert solo["superseded_by"] == ""


def test_build_series_different_site_not_grouped_together():
    docs = [
        _series_doc("d1", "ZJUI Class Schedule FA24", terms=["2024-fall"], site="zjui"),
        _series_doc("d2", "ZJUI Class Schedule FA25", terms=["2025-fall"], site="ugrs"),
    ]
    series = build_series(docs)
    assert series == []


def test_build_series_skips_listing_and_news_documents():
    docs = [
        _series_doc("d1", "ZJUI Class Schedule FA24", terms=["2024-fall"], page_kind="listing"),
        _series_doc("d2", "ZJUI Class Schedule FA25", terms=["2025-fall"]),
    ]
    series = build_series(docs)
    assert series == []


# ===================================================================== metadata.py


def test_doc_type_calendar_from_title():
    assert doc_type("2026年校历") == "calendar"


def test_doc_type_schedule_from_title():
    assert doc_type("Fall 2026 Class Schedule") == "schedule"


def test_doc_type_body_fallback_only_for_rule_like_types():
    # Navigation chrome in the body head must not type a page as calendar or
    # exam schedule; only regulation/program_plan wording is trusted there.
    assert doc_type("每周简报", body="本次考试安排已经确定") == "page"
    assert doc_type("每周简报", body="本学籍管理办法适用于全日制本科生") == "regulation"


def test_doc_type_news_site_default():
    assert doc_type("今日校园活动", site="news") == "news"


def test_audience_graduate_title_on_ugrs_site_overrides_site_default():
    assert audience("ugrs", "2026年研究生奖学金申请通知", [], "") == "graduate"


def test_audience_faculty_marker_wins_over_everything():
    assert audience("ugrs", "教师教学竞赛通知", [], "") == "faculty"


def test_audience_institute_site_graduate_title_promotes_to_graduate():
    # ZJUI also serves master's/PhD programs; graduate wording in the title
    # promotes the audience so those notices stop looking like undergraduate
    # material (audience-interference traps depend on this).
    assert audience("zjui", "研究生交流活动", [], "") == "graduate"
    assert audience("zjui", "本科生选课通知", [], "") == "zjui_undergraduate"


def test_audience_undergrad_marker_on_public_site():
    assert audience("zju", "本科生交流活动的通知", [], "") == "undergraduate"


def test_build_metadata_prefers_title_terms_over_body_terms():
    meta = build_metadata(
        site="ugrs", kind="page", title="2026春夏学期选课通知",
        body="本通知适用于2025秋冬学期和2026春夏学期", breadcrumb=[], published="2026-01-01",
        url="https://example.com/a",
    )
    assert meta["terms"] == ["2026-spring"]
    assert meta["term_source"] == "title"


def test_build_metadata_falls_back_to_body_terms_when_title_has_none():
    meta = build_metadata(
        site="ugrs", kind="page", title="选课通知", body="本通知适用于2025秋冬学期",
        breadcrumb=[], published="", url="https://example.com/b",
    )
    assert meta["terms"] == ["2025-fall"]
    assert meta["term_source"] == "body"


def test_doc_number_re_matches_zju_style_document_number():
    match = DOC_NUMBER_RE.search("浙大发本〔2024〕12号")
    assert match is not None
    assert match.group(1) == "浙大发本〔2024〕12号"


# ===================================================================== sources.parse_cms_page


def test_parse_cms_page_sudy(tmp_path):
    html = """
    <html><head><title>Sudy Title</title></head>
    <body>
    <div class="position">当前位置：<a href="/">首页</a> &gt; <a href="/col1">通知公告</a> &gt; <a href="/col2">本科生</a></div>
    <div class="article-title"><h3>关于选课的通知</h3></div>
    <div class="arti_metas">时间：2020年09月24日</div>
    <div class="wp_articlecontent">
    <p>正文内容在这里，说明选课流程。</p>
    <div pdfsrc="/_upload/article/files/ab/cd/x.pdf" sudyfile-attr="{'title':'通知.pdf'}"></div>
    </div>
    </body></html>
    """
    item = parse_cms_page("ugrs", tmp_path, "https://ugrs.zju.edu.cn/col1/col2/2020/0924/c100a200/page.htm", html)
    assert item.title == "关于选课的通知"
    assert item.title_source == "page"
    assert item.breadcrumb == ["通知公告", "本科生"]
    assert item.published == "2020-09-24"
    assert len(item.attachments) == 1
    attachment = item.attachments[0]
    assert attachment.url.endswith("/_upload/article/files/ab/cd/x.pdf")
    assert attachment.label == "通知.pdf"
    assert attachment.embedded is True
    assert any("选课流程" in block.text for block in item.blocks)


def test_parse_cms_page_zjui_drupal(tmp_path):
    html = """
    <html><head><title>ZJUI</title></head>
    <body>
    <div class="main">
    <div class="banner"><div class="con">教育教学</div></div>
    <div class="newsInfo">
    <div class="tits">关于开学安排的通知</div>
    <div class="tit"><span>时间： 03/02/2026</span></div>
    <div class="con">
    <p>以下是开学安排的具体内容。</p>
    </div>
    </div>
    </div>
    </body></html>
    """
    item = parse_cms_page("zjui", tmp_path, "https://zjui.intl.zju.edu.cn/news/123", html)
    assert item.title == "关于开学安排的通知"
    assert item.breadcrumb == ["教育教学"]
    assert item.published == "2026-03-02"
    assert item.page_kind == "article"
    assert any("开学安排" in block.text for block in item.blocks)


# ===================================================================== build.py helpers


def test_assemble_blocks_heading_path_offsets_and_salted_duplicate_ids():
    raw = [
        RawBlock("heading", "Chapter One", level=1),
        RawBlock("paragraph", "Intro text."),
        RawBlock("heading", "Section A", level=2),
        RawBlock("paragraph", "Some paragraph."),
        RawBlock("paragraph", "Some paragraph."),  # exact duplicate text
        RawBlock("heading", "Chapter Two", level=1),
        RawBlock("paragraph", "Another one."),
    ]
    blocks, text = assemble_blocks("d_test", "Doc Title", raw)
    assert [b["heading_path"] for b in blocks] == [
        [], ["Chapter One"], ["Chapter One"], ["Chapter One", "Section A"],
        ["Chapter One", "Section A"], [], ["Chapter Two"],
    ]
    # offsets are contiguous with a 1-char separator
    assert blocks[0]["char_start"] == 0
    assert blocks[0]["char_end"] == len("Chapter One")
    assert blocks[1]["char_start"] == blocks[0]["char_end"] + 1
    # duplicate paragraph text gets two distinct block ids, both under the same doc
    dup_ids = [b["block_id"] for b in blocks if b["text"] == "Some paragraph."]
    assert len(dup_ids) == 2
    assert dup_ids[0] != dup_ids[1]
    assert all(bid.startswith("d_test:") for bid in dup_ids)
    assert text == "Chapter One\nIntro text.\nSection A\nSome paragraph.\nSome paragraph.\nChapter Two\nAnother one."


def test_assemble_blocks_drops_empty_and_whitespace_only_blocks():
    raw = [RawBlock("paragraph", "   "), RawBlock("paragraph", "Real content.")]
    blocks, text = assemble_blocks("d_test", "Doc Title", raw)
    assert len(blocks) == 1
    assert blocks[0]["text"] == "Real content."


def test_attachment_title_generic_label_falls_back_to_parent_title():
    title, source = attachment_title("附件1", Path("a1b2c3d4e5f6a1b2.pdf"), "Parent Notice Title", 1)
    assert title == "Parent Notice Title"
    assert source == "parent_title"


def test_attachment_title_uuid_filename_falls_back_to_parent_title():
    title, source = attachment_title(
        "", Path("550e8400-e29b-41d4-a716-446655440000.pdf"), "Parent Notice Title", 1
    )
    assert title == "Parent Notice Title"
    assert source == "parent_title"


def test_attachment_title_uses_meaningful_link_label_as_is():
    title, source = attachment_title("Course Enrollment Guide", Path("abc.pdf"), "Parent Notice Title", 1)
    assert title == "Course Enrollment Guide"
    assert source == "link_label"


def test_attachment_title_uses_meaningful_filename_when_label_missing():
    title, source = attachment_title("", Path("schedule-fa26.pdf"), "Parent Notice Title", 1)
    assert title == "schedule-fa26"
    assert source == "filename"


def test_attachment_title_no_parent_falls_back_to_uuid_stem_when_nothing_else_available():
    # No link label, a UUID-like filename, and no parent title to fall back to: the function
    # has nothing meaningful left and returns the UUID stem as-is (not ideal, but documents the
    # current fallback behaviour when `attachment_title` is called without a usable parent).
    title, source = attachment_title("", Path("550e8400-e29b-41d4-a716-446655440000.pdf"), "", 1)
    assert title == "550e8400-e29b-41d4-a716-446655440000"
    assert source == "filename"
