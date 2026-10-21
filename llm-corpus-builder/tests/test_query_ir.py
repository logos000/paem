from datetime import date

from evidence.query_ir import (
    applicability_mismatches,
    extract_cohorts,
    extract_course_codes,
    extract_terms,
    next_term,
    parse_query,
    term_from_date,
    term_order,
)


# --------------------------------------------------------------------- extract_terms: English


def test_extract_terms_english_season_year():
    terms, years = extract_terms("Classes resume Fall 2026 for everyone.")
    assert terms == ["2026-fall"]
    assert years == []


def test_extract_terms_english_year_season():
    terms, _ = extract_terms("The 2026 Fall semester starts in September.")
    assert terms == ["2026-fall"]


def test_extract_terms_abbreviation_fa26():
    terms, _ = extract_terms("Please check the FA26 class schedule.")
    assert terms == ["2026-fall"]


def test_extract_terms_abbreviation_sp2025():
    terms, _ = extract_terms("SP2025 enrollment opens soon.")
    assert terms == ["2025-spring"]


def test_extract_terms_abbreviation_year_first_26fa():
    terms, _ = extract_terms("See the 26FA room assignment sheet.")
    assert terms == ["2026-fall"]


# --------------------------------------------------------------------- extract_terms: Chinese


def test_extract_terms_chinese_spring_summer():
    terms, _ = extract_terms("2026春夏学期的选课时间已公布。")
    assert terms == ["2026-spring"]


def test_extract_terms_chinese_fall_winter():
    terms, _ = extract_terms("2025秋冬学期课表请查看附件。")
    assert terms == ["2025-fall"]


def test_extract_terms_chinese_academic_year_second_term():
    terms, years = extract_terms("2025-2026学年第二学期的教学日历已发布。")
    assert years == ["2025-2026"]
    assert terms == ["2026-spring"]


def test_extract_terms_chinese_academic_year_first_term():
    terms, years = extract_terms("2025-2026学年第一学期的教学日历已发布。")
    assert years == ["2025-2026"]
    assert terms == ["2025-fall"]


def test_extract_terms_chinese_academic_year_without_qualifier_has_no_term():
    terms, years = extract_terms("2025-2026学年的教学日历已发布。")
    assert years == ["2025-2026"]
    assert terms == []


# --------------------------------------------------------------------- term_from_date / next_term / term_order


def test_term_from_date_spring_range():
    assert term_from_date(date(2026, 2, 1)) == "2026-spring"
    assert term_from_date(date(2026, 7, 31)) == "2026-spring"


def test_term_from_date_fall_range_including_january_rollover():
    assert term_from_date(date(2026, 8, 1)) == "2026-fall"
    assert term_from_date(date(2026, 12, 31)) == "2026-fall"
    assert term_from_date(date(2027, 1, 15)) == "2026-fall"


def test_next_term_from_fall_rolls_to_next_spring():
    assert next_term("2026-fall") == "2027-spring"


def test_next_term_from_spring_goes_to_fall_same_year():
    assert next_term("2026-spring") == "2026-fall"


def test_term_order_orders_seasons_within_a_year():
    assert term_order("2026-spring") < term_order("2026-summer") < term_order("2026-fall") < term_order("2026-winter")
    assert term_order("2026-fall") < term_order("2027-spring")


# --------------------------------------------------------------------- extract_cohorts


def test_extract_cohorts_chinese_suffix():
    assert extract_cohorts("2023级本科生名单") == ["2023"]


def test_extract_cohorts_class_of_english():
    # a 4-year undergraduate program: "class of 2027" enrolled as the 2023 cohort
    assert extract_cohorts("Welcome, class of 2027!") == ["2023"]


def test_extract_cohorts_dedupes_and_sorts():
    # class of 2025 -> cohort 2021, duplicated with the explicit "2021级"
    cohorts = extract_cohorts("2023级和2021级学生, class of 2025")
    assert cohorts == ["2021", "2023"]


# --------------------------------------------------------------------- extract_course_codes


def test_extract_course_codes_basic():
    assert extract_course_codes("Please enroll in CS101 and MATH 201A.") == ["CS101", "MATH201A"]


def test_extract_course_codes_lowercase_not_matched():
    assert extract_course_codes("register for cs101 please") == []


# --------------------------------------------------------------------- parse_query


def test_parse_query_detects_graduate_audience():
    ir = parse_query("研究生的报名流程是什么？")
    assert ir.audience == "graduate"
    assert ir.language == "zh"


def test_parse_query_detects_junior_class_standing():
    ir = parse_query("I am a junior, when can I register?")
    assert ir.class_standing == [3]


def test_parse_query_detects_chinese_class_standing():
    ir = parse_query("大三学生什么时候选课？")
    assert ir.class_standing == [3]


def test_parse_query_wants_latest_marker():
    ir = parse_query("最新的教学日历是什么？")
    assert ir.wants_latest is True


def test_parse_query_relative_next_term_resolved_with_today():
    ir = parse_query("下学期的课表什么时候出？", today=date(2026, 10, 13))
    assert ir.relative_term == "next"
    assert ir.terms == ["2027-spring"]


def test_parse_query_relative_current_term_resolved_with_today():
    ir = parse_query("这学期的考试安排是什么？", today=date(2026, 10, 13))
    assert ir.relative_term == "current"
    assert ir.terms == ["2026-fall"]


def test_parse_query_explicit_term_takes_precedence_over_relative():
    ir = parse_query("下学期FA26课表", today=date(2026, 10, 13))
    assert ir.terms == ["2026-fall"]
    assert ir.relative_term == ""


def test_parse_query_detects_enrollment_intent():
    ir = parse_query("How do I enroll in a new course?")
    assert "enrollment" in ir.intents


def test_parse_query_detects_institute_marker():
    ir = parse_query("ZJUI students need to check the schedule.")
    assert ir.institute is True


def test_parse_query_context_single_term_and_cohort():
    ir = parse_query("2026春夏学期 2023级学生选课指南")
    ctx = ir.context()
    assert ctx["term"] == "2026-spring"
    assert ctx["cohort"] == "2023"


def test_parse_query_context_omits_term_when_multiple():
    ir = parse_query("FA26和SP25的课表对比")
    assert "term" not in ir.context()


# --------------------------------------------------------------------- applicability_mismatches


def test_applicability_mismatches_term_mismatch():
    ir = parse_query("2026春夏学期的课表")
    mismatches = applicability_mismatches(ir, {"terms": ["2025-fall"]})
    assert any(m["kind"] == "term" for m in mismatches)


def test_applicability_mismatches_term_covered_by_academic_year():
    ir = parse_query("FA26的课表")
    mismatches = applicability_mismatches(ir, {"academic_years": ["2026-2027"]})
    assert mismatches == []


def test_applicability_mismatches_audience_mismatch():
    ir = parse_query("研究生奖学金申请")
    mismatches = applicability_mismatches(ir, {"audience": "undergraduate"})
    assert any(m["kind"] == "audience" for m in mismatches)


def test_applicability_mismatches_audience_compatible_with_public():
    ir = parse_query("研究生奖学金申请")
    mismatches = applicability_mismatches(ir, {"audience": "public"})
    assert mismatches == []


def test_applicability_mismatches_cohort_mismatch():
    ir = parse_query("2023级培养方案")
    mismatches = applicability_mismatches(ir, {"cohorts": ["2021"]})
    assert any(m["kind"] == "cohort" for m in mismatches)


def test_applicability_mismatches_no_mismatch_when_no_metadata():
    ir = parse_query("2023级培养方案")
    assert applicability_mismatches(ir, {}) == []


def test_extract_course_codes_adjacent_to_cjk():
    assert extract_course_codes("关于CS101课程安排的通知") == ["CS101"]
    assert extract_course_codes("FA2026 学期") == []


# ---- relative-term resolution (2026-10-17, trap-curr-01 blind spot) ----

def test_relative_latest_resolves_soft_term():
    from datetime import date
    ir = parse_query("最新的 ZJUI 选课指南里，选课截止日期是什么时候？", today=date(2026, 10, 15))
    assert ir.terms == ["2026-fall"]
    assert ir.relative_term == "latest"
    assert ir.wants_latest  # hard pack filter stays off — old-series "最新" is safe


def test_relative_current_marker():
    from datetime import date
    ir = parse_query("本学期的课表什么时候出？", today=date(2026, 10, 15))
    assert ir.terms == ["2026-fall"]
    assert ir.relative_term == "current"


def test_relative_next_marker():
    from datetime import date
    ir = parse_query("下学期什么时候开始选课？", today=date(2026, 10, 15))
    assert ir.terms == ["2027-spring"]
    assert ir.relative_term == "next"


def test_relative_english_latest():
    from datetime import date
    ir = parse_query("What is the enrollment deadline in the latest Course Enrollment Guide?", today=date(2026, 10, 15))
    assert ir.terms == ["2026-fall"]
    assert ir.wants_latest


def test_no_relative_marker_no_term():
    from datetime import date
    ir = parse_query("ZJUI 选课截止日期是什么？", today=date(2026, 10, 15))
    assert ir.terms == []
    assert ir.relative_term == ""


def test_explicit_term_beats_relative():
    from datetime import date
    ir = parse_query("最新的 2023 年秋季学期选课指南的截止日期是什么？", today=date(2026, 10, 15))
    assert ir.terms == ["2023-fall"]
