"""Rule-based query understanding (Query IR) for academic-affairs questions.

The IR is derived from the question text alone (plus an optional ``today``),
so benchmarks no longer need to leak gold ``context`` fields into retrieval.
The same term/cohort normalizers are used by the corpus builder, so a
question's ``2026-spring`` and a document's ``2026-spring`` compare directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import re
from typing import Any, Iterable

from domain_profile import load_profile

from evidence.store import normalize_text

SEASONS = ("spring", "summer", "fall", "winter")
_EN_SEASON = {"spring": "spring", "summer": "summer", "fall": "fall", "autumn": "fall", "winter": "winter"}
_ABBR = {"sp": "spring", "su": "summer", "fa": "fall", "wi": "winter"}
# ZJU splits each semester into two quarters: 春+夏 form the spring-summer
# semester and 秋+冬 the fall-winter semester.  "夏季学期/暑期/短学期" is the
# separate summer session.
_CN_SEASON = (
    ("春夏", "spring"), ("秋冬", "fall"), ("夏季学期", "summer"), ("暑期", "summer"), ("短学期", "summer"),
    ("小学期", "summer"), ("春", "spring"), ("夏", "spring"), ("秋", "fall"), ("冬", "fall"),
)

_RE_EN_SEASON_YEAR = re.compile(r"\b(spring|summer|fall|autumn|winter)(?:\s+(?:semester|term|session))?[\s,]+(20\d{2})\b", re.I)
_RE_EN_YEAR_SEASON = re.compile(r"\b(20\d{2})[\s-]+(spring|summer|fall|autumn|winter)\b", re.I)
_RE_ABBR = re.compile(r"(?<![A-Za-z])(SP|SU|FA|WI)[\s_-]?((?:20)?\d{2})(?!\d)", re.I)
_RE_ABBR_YEAR_FIRST = re.compile(r"(?<!\d)((?:20)?\d{2})[\s_-]?(SP|SU|FA|WI)(?![A-Za-z])", re.I)
_RE_CN_TERM = re.compile(r"(20\d{2})\s*年?\s*(春夏|秋冬|夏季学期|暑期|短学期|小学期|春|夏|秋|冬)(?:季)?(?:学期)?")
_RE_ACADEMIC_YEAR = re.compile(r"(20\d{2})\s*[-—–~至/]\s*((?:20)?\d{2})\s*学年(?:\s*第\s*([一二12])\s*学期|\s*(春夏|秋冬))?")
_RE_ACADEMIC_YEAR_EN = re.compile(r"\b(?:AY\s*)?(20\d{2})\s*[-/–]\s*((?:20)?\d{2})\b(?=\s*(?:academic|school)?\s*(?:year|calendar|学年|$))", re.I)
_RE_SHORT_AY = re.compile(r"(?<!\d)(\d{2})-(\d{2})\b(?=\s*(?:academic|calendar|学年))", re.I)
_RE_COHORT = re.compile(r"(20\d{2})\s*级")
_RE_CLASS_OF = re.compile(r"\bclass\s+of\s+(20\d{2})\b", re.I)
# Lookarounds instead of \b: CJK characters are \w, so "关于CS101课程" has no \b.
COURSE_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5})\s?(\d{3})([A-Z]?)(?![A-Za-z0-9])")
YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")

STANDING = {
    "freshman": 1, "freshmen": 1, "大一": 1, "一年级": 1,
    "sophomore": 2, "sophomores": 2, "大二": 2, "二年级": 2,
    "junior": 3, "juniors": 3, "大三": 3, "三年级": 3,
    "senior": 4, "seniors": 4, "大四": 4, "四年级": 4,
}

AUDIENCE_MARKERS = {
    "graduate": ("研究生", "硕士", "博士", "graduate student", "phd", "master", "postgraduate", "doctoral"),
    "undergraduate": ("本科", "undergrad", "大一", "大二", "大三", "大四", "freshm", "sophomore", "junior", "senior"),
    "faculty": ("教师", "教职工", "老师申报", "教改", "师资", "faculty member", "instructor"),
}
# "我是一名 ZJUI 本科生" states who is asking; it outranks topic words such as
# "研究生专项" or "博士后岗位" elsewhere in the question.
IDENTITY_RE = re.compile(
    r"(?:我是|本人是|作为)(?:一名|一个|一位|名)?[^，,。？?！!]{0,12}?"
    r"(本科生|本科|大[一二三四]学生|研究生|硕士生|博士生|老师|教师|教职工)"
    r"|(?<![a-z])i(?:'m| am)\s+(?:an?\s+)?[^,.?!]{0,24}?(undergrad|graduate student|master'?s student|phd student|faculty|instructor)",
    re.I,
)
IDENTITY_AUDIENCE = {
    "本科生": "undergraduate", "本科": "undergraduate", "undergrad": "undergraduate",
    "研究生": "graduate", "硕士生": "graduate", "博士生": "graduate",
    "graduate student": "graduate", "master's student": "graduate", "masters student": "graduate", "phd student": "graduate",
    "老师": "faculty", "教师": "faculty", "教职工": "faculty", "faculty": "faculty", "instructor": "faculty",
}
# Attribute words: "授课教师" asks who teaches a course, not a faculty-only rule.
AUDIENCE_NOISE_RE = re.compile(r"授课教师|任课教师|指导教师|主讲教师|教师姓名|instructors?(?![a-z])|导师姓名", re.I)
INSTITUTE_MARKERS = ("zjui", "zju-uiuc", "国际校区", "海宁", "uiuc", "illinois")

INTENTS = {
    "calendar": ("校历", "教学日历", "日历", "calendar", "开学", "放假", "假期", "holiday", "教学周", "semester start", "第几周"),
    "enrollment": ("选课", "enroll", "registration", "register", "course selection", "补选", "加课", "add course", "peoplesoft"),
    "drop": ("退课", "drop", "withdraw", "退选"),
    "exam": ("考试", "exam", "final", "期末", "期中", "midterm", "补考", "缓考"),
    "graduation": ("毕业", "graduat", "学位", "degree"),
    "change_major": ("转专业", "change major", "change of major", "专业确认", "major declaration"),
    "ta": ("助教", "teaching assistant", " ta ", "ta application"),
    "exchange": ("交换", "exchange", "访学", "study abroad", "海外"),
    "credits": ("学分", "credit", "credits"),
    "program_plan": ("培养方案", "教学计划", "curriculum", "program plan", "prerequisite", "先修"),
    "schedule": ("课表", "class schedule", "上课时间", "class number", "课程号", "时间表"),
    "contact": ("联系", "邮箱", "电话", "contact", "email", "office", "办公室"),
    "form": ("申请表", "表格", "form", "下载"),
}
_CAL_PROFILE = load_profile()
CURRENT_MARKERS = tuple(_CAL_PROFILE.words("calendar", "relative_current"))
LATEST_MARKERS = tuple(_CAL_PROFILE.words("calendar", "relative_latest"))
NEXT_MARKERS = tuple(_CAL_PROFILE.words("calendar", "relative_next"))


def season_from_cn(label: str) -> str:
    for key, season in _CN_SEASON:
        if label.startswith(key):
            return season
    return ""


def _year(value: str) -> int:
    number = int(value)
    return number + 2000 if number < 100 else number


def term_key(year: int, season: str) -> str:
    return f"{year:04d}-{season}"


def academic_year_terms(start: int) -> list[str]:
    return [term_key(start, "fall"), term_key(start + 1, "spring")]


def term_from_date(day: date) -> str:
    """Map a date to the semester it belongs to (Feb-Jul spring, Aug-Jan fall)."""
    if 2 <= day.month <= 7:
        return term_key(day.year, "spring")
    return term_key(day.year if day.month >= 8 else day.year - 1, "fall")


def next_term(term: str) -> str:
    year, season = term.split("-")
    return term_key(int(year), "fall") if season in ("spring", "summer") else term_key(int(year) + 1, "spring")


def term_order(term: str) -> float:
    year, season = term.split("-")
    return int(year) + {"spring": 0.1, "summer": 0.5, "fall": 0.7, "winter": 0.9}.get(season, 0.0)


def extract_terms(text: str) -> tuple[list[str], list[str]]:
    """Return (terms, academic_years) mentioned in ``text``.

    Terms are ``YYYY-season``; academic years are ``YYYY-YYYY``.  An academic
    year with a semester qualifier also yields the concrete term.
    """
    text = normalize_text(text)
    terms: list[str] = []
    years: list[str] = []

    def add(value: str, target: list[str]) -> None:
        if value not in target:
            target.append(value)

    for match in _RE_ACADEMIC_YEAR.finditer(text):
        start, end = _year(match.group(1)), _year(match.group(2))
        if end != start + 1:
            continue
        add(f"{start}-{end}", years)
        qualifier = match.group(3) or match.group(4) or ""
        if qualifier in ("一", "1", "秋冬"):
            add(term_key(start, "fall"), terms)
        elif qualifier in ("二", "2", "春夏"):
            add(term_key(end, "spring"), terms)
    for pattern in (_RE_ACADEMIC_YEAR_EN, _RE_SHORT_AY):
        for match in pattern.finditer(text):
            start, end = _year(match.group(1)), _year(match.group(2))
            if end == start + 1:
                add(f"{start}-{end}", years)
    for match in _RE_EN_SEASON_YEAR.finditer(text):
        add(term_key(int(match.group(2)), _EN_SEASON[match.group(1).casefold()]), terms)
    for match in _RE_EN_YEAR_SEASON.finditer(text):
        add(term_key(int(match.group(1)), _EN_SEASON[match.group(2).casefold()]), terms)
    for match in _RE_ABBR.finditer(text):
        add(term_key(_year(match.group(2)), _ABBR[match.group(1).casefold()]), terms)
    for match in _RE_ABBR_YEAR_FIRST.finditer(text):
        add(term_key(_year(match.group(1)), _ABBR[match.group(2).casefold()]), terms)
    consumed = {m.span() for m in _RE_ACADEMIC_YEAR.finditer(text)}
    for match in _RE_CN_TERM.finditer(text):
        if any(a <= match.start() < b for a, b in consumed):
            continue
        season = season_from_cn(match.group(2))
        if season:
            add(term_key(int(match.group(1)), season), terms)
    # P1-7: year sanity bounds.  Terms beyond today+3 years are crawl/garbage
    # artifacts (a real notice never announces 2100-spring); pre-2000 ones are
    # misparses.  The cap deliberately keeps future-term traps like 2027-spring
    # valid so the abstention layer still sees them.
    from datetime import date as _date
    max_year = _date.today().year + 3
    sane = [t for t in terms if t[:4].isdigit() and 2000 <= int(t[:4]) <= max_year]
    terms = sane or terms
    years = [y for y in years if y[:4].isdigit() and 2000 <= int(y[:4]) <= max_year] or years
    return terms, years


def extract_cohorts(text: str) -> list[str]:
    text = normalize_text(text)
    cohorts = [match.group(1) for match in _RE_COHORT.finditer(text)]
    cohorts += [str(int(match.group(1)) - 4) for match in _RE_CLASS_OF.finditer(text)]
    return sorted(set(cohorts))


def extract_course_codes(text: str) -> list[str]:
    return sorted({f"{a}{b}{c}".upper() for a, b, c in COURSE_CODE_RE.findall(normalize_text(text))})


def _has(lowered: str, markers: Iterable[str]) -> bool:
    return any(marker in lowered for marker in markers)


@dataclass
class QueryIR:
    raw: str
    language: str
    terms: list[str] = field(default_factory=list)
    academic_years: list[str] = field(default_factory=list)
    years: list[str] = field(default_factory=list)
    cohorts: list[str] = field(default_factory=list)
    class_standing: list[int] = field(default_factory=list)
    audience: str = ""
    topic_audience: str = ""
    institute: bool = False
    course_codes: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    wants_latest: bool = False
    relative_term: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def context(self) -> dict[str, str]:
        """Context dict accepted by ``build_evidence_pack`` and ``pipeline.applicable``."""
        result: dict[str, str] = {}
        if len(self.terms) == 1:
            result["term"] = self.terms[0]
        if len(self.cohorts) == 1:
            result["cohort"] = self.cohorts[0]
        if self.audience:
            result["audience"] = self.audience
        return result


def identity_audience(text: str) -> str:
    match = IDENTITY_RE.search(text)
    if not match:
        return ""
    word = (match.group(1) or match.group(2) or "").casefold()
    if word.startswith("大"):
        return "undergraduate"
    return IDENTITY_AUDIENCE.get(word, "")


def parse_query(question: str, today: date | None = None) -> QueryIR:
    text = normalize_text(question)
    lowered = f" {text.casefold()} "
    language = "zh" if re.search(r"[一-鿿]", text) else "en"
    terms, academic_years = extract_terms(text)
    relative = ""
    if today is not None and not terms:
        if _has(lowered, NEXT_MARKERS):
            terms = [next_term(term_from_date(today))]
            relative = "next"
        elif _has(lowered, CURRENT_MARKERS):
            terms = [term_from_date(today)]
            relative = "current"
        elif _has(lowered, LATEST_MARKERS):
            # SOFT term key (2026-10-17, trap-curr-01 blind spot): ir.terms biases
            # the IR/lexical channels toward the current-term family so "最新的选课
            # 指南" actually retrieves it, while wants_latest keeps the pack-level
            # hard filter OFF (hard_applicability_mismatches guard) — "最新的UIUC
            # 交换申请" (latest edition 2024) is never excluded by a 2026-fall key.
            terms = [term_from_date(today)]
            relative = "latest"
    standing = sorted({value for key, value in STANDING.items() if key in lowered})
    identity = identity_audience(text)
    # Topic audience is read from the question body with the identity clause
    # removed: "我是本科生…博士后岗位" keeps asker and topic separable, so the
    # evidence layer can detect an in-question audience contradiction.
    body = f" {IDENTITY_RE.sub(' ', text).casefold()} " if identity else lowered
    topical = AUDIENCE_NOISE_RE.sub(" ", body)
    topic_audience = ""
    for name in ("graduate", "faculty", "undergraduate"):
        surface = topical
        if name == "graduate":
            # "undergraduate students" CONTAINS the substring "graduate
            # student" — neutralize undergrad word forms before the graduate
            # scan or every undergrad question parses as graduate.
            surface = re.sub(r"(?<![a-z])undergrad[a-z]*(?![a-z])", " ", topical)
        if _has(surface, AUDIENCE_MARKERS[name]):
            topic_audience = name
            break
    audience = identity or topic_audience
    intents = [name for name, markers in INTENTS.items() if _has(lowered, markers)]
    return QueryIR(
        raw=question,
        language=language,
        terms=terms,
        academic_years=academic_years,
        years=sorted(set(YEAR_RE.findall(text))),
        cohorts=extract_cohorts(text),
        class_standing=standing,
        audience=audience,
        topic_audience=topic_audience if identity else "",
        institute=_has(lowered, INSTITUTE_MARKERS),
        course_codes=extract_course_codes(text),
        intents=intents,
        wants_latest=_has(lowered, LATEST_MARKERS),
        relative_term=relative,
    )


AUDIENCE_COMPATIBLE = {
    "undergraduate": {"undergraduate", "zjui_undergraduate", "public", "all", ""},
    "graduate": {"graduate", "public", "all", ""},
    "faculty": {"faculty", "public", "all", ""},
}


def applicability_mismatches(ir: QueryIR, metadata: dict[str, Any]) -> list[dict[str, str]]:
    """Compare a question's IR with document metadata; return explicit mismatches.

    A mismatch does not mean the evidence is false, only that it applies to a
    different term/audience/cohort than the one asked about.
    """
    result: list[dict[str, str]] = []
    doc_terms = set(metadata.get("terms") or [])
    doc_years = set(metadata.get("academic_years") or [])
    asked = set(ir.terms)
    # A document's own terms are more specific than its academic year: a
    # "2025-2026学年第一学期" notice does not apply to 2026-spring.
    if asked and doc_terms:
        if asked.isdisjoint(doc_terms):
            result.append({"kind": "term", "reason": f"evidence is for {sorted(doc_terms)[:3]}, question asks {sorted(asked)}"})
    elif asked and doc_years:
        if not any(term in academic_year_terms(int(ay[:4])) for ay in doc_years for term in asked):
            result.append({"kind": "term", "reason": f"evidence is for {sorted(doc_years)}, question asks {sorted(asked)}"})
    elif ir.academic_years and doc_years:
        if set(ir.academic_years).isdisjoint(doc_years):
            result.append({"kind": "academic_year", "reason": f"evidence is for {sorted(doc_years)}, question asks {ir.academic_years}"})
    elif ir.academic_years and doc_terms:
        if not any(term in academic_year_terms(int(ay[:4])) for ay in ir.academic_years for term in doc_terms):
            result.append({"kind": "academic_year", "reason": f"evidence is for {sorted(doc_terms)[:3]}, question asks {ir.academic_years}"})
    audience = str(metadata.get("audience") or "")
    if ir.audience and audience and audience not in AUDIENCE_COMPATIBLE.get(ir.audience, {audience}):
        result.append({"kind": "audience", "reason": f"evidence targets {audience}, question asks about {ir.audience}"})
    elif not ir.audience and audience in ("graduate", "faculty") and (ir.institute or ir.class_standing):
        result.append({"kind": "audience", "reason": f"evidence targets {audience}, question is from an undergraduate context"})
    doc_cohorts = set(metadata.get("cohorts") or [])
    if ir.cohorts and doc_cohorts and set(ir.cohorts).isdisjoint(doc_cohorts):
        result.append({"kind": "cohort", "reason": f"evidence is for cohort {sorted(doc_cohorts)}, question asks {ir.cohorts}"})
    return result


_HARD_YEAR_RE = load_profile().pattern("calendar", "bare_year")
# Full-date patterns: a year inside a DATE is an event pin ("选课窗口在2025年
# 6月20日关闭" can only be answered by 2025 material), unlike a bare year
# mention which may be cohort context.  DS-2019 (form name) must never match.
_HARD_DATE_YEAR_PATTERNS = (
    re.compile(r"(20\d{2})\s*年\s*\d{1,2}\s*月"),
    re.compile(r"(20\d{2})[-/]\d{1,2}[-/]\d{1,2}"),
    re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(20\d{2})", re.I),
)


def question_date_years(text: str) -> set[str]:
    """Years that appear inside full dates in the question (event anchors)."""
    return {year for year, _month in question_dates(text)}


_EN_MONTHS = {name: f"{index:02d}" for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def question_dates(text: str) -> set[tuple[str, str]]:
    """Full dates in the question as (year, month) event anchors.

    Academic semesters straddle calendar years: spring-2024 registration runs
    in Dec 2023, a fall semester ends in Jan of the next year (seen live:
    sm-0105's gold block "8:00PM, Dec. 25, 2023" lives in a 2024-spring
    guide).  Year-only comparison misclassifies those as wrong-year; the
    month is required for the cross-year windows below.
    """
    text = str(text or "")
    dates: set[tuple[str, str]] = set()
    _cal = load_profile()
    for match in _cal.pattern("calendar", "question_date_zh").finditer(text):
        dates.add((match.group(1), f"{int(match.group(2)):02d}"))
    for match in _cal.pattern("calendar", "question_date_iso").finditer(text):
        dates.add((match.group(1), f"{int(match.group(2)):02d}"))
    for match in _cal.pattern("calendar", "question_date_en").finditer(text):
        dates.add((match.group(2), _EN_MONTHS[match.group(1)[:3].lower()]))
    return dates


def _semester_windows(term: str) -> list[tuple[str, str]]:
    """(year, month) pairs a semester term's events may carry.

    spring Y: Dec(Y-1) through Jul Y (registration happens before the term);
    fall Y:   the whole calendar year Y (spring-term registration for the
              fall cohort happens May-June) plus the exam tail Jan(Y+1);
    anything else (summer/winter/unknown): calendar year Y.
    """
    _cal = load_profile()
    match = _cal.pattern("calendar", "term_parse").fullmatch(str(term).strip().lower())
    if not match:
        return []
    year = int(match.group(1))
    season = match.group(2) or ""
    springs = set(load_profile().words("calendar", "seasons_spring"))
    falls = set(load_profile().words("calendar", "seasons_fall"))
    key = "spring" if season in springs else "fall" if season in falls else "bare"
    window = load_profile().mapping("calendar", "windows").get(key) or {}
    yms = (
        [f"{year - 1}-{m:02d}" for m in window.get("prev", [])]
        + [f"{year}-{m:02d}" for m in window.get("same", [])]
        + [f"{year + 1}-{m:02d}" for m in window.get("next", [])]
    )
    return [(ym[:4], ym[5:7]) for ym in yms]


def _event_fits_bare_year(event: tuple[str, str], doc_year: str) -> bool:
    """Cross-year-aware check of an event date against a bare (title) year:
    same calendar year, or the straddle edges — a December event may belong
    to next year's spring term, a January event to last year's fall."""
    year, month = int(event[0]), int(event[1])
    doc = int(doc_year)
    if year == doc:
        return True
    if month == 12 and doc == year + 1:
        return True
    return month == 1 and doc == year - 1


def hard_applicability_mismatches(ir: QueryIR, metadata: dict[str, Any]) -> list[str]:
    """Pre-ranking HARD exclusions (step 7, taxonomy v1 2026-10-16).

    Unlike :func:`applicability_mismatches` (post-retrieval demotion), these
    remove a candidate from the pool BEFORE ranking when the question pins a
    constraint the document provably contradicts.  A document with NO year /
    audience anchor is never excluded — absence of metadata is not evidence of
    inapplicability.

    wrong_year_hard   — the question pins semesters (exact-year match) and/or
                        full event dates (cross-year semester windows: a
                        Dec-2023 event fits a 2024-spring guide), and every
                        document year anchor contradicts all of them.
    wrong_audience_hard — question audience and document audience are
                        explicitly incompatible (undergraduate vs graduate /
                        faculty).
    """
    kinds: list[str] = []
    if not ir.wants_latest:
        # Semester pins: "2024-fall" term keys land in ir.terms (4-char year
        # prefix), calendar-year forms in ir.academic_years.
        semester_pins = {str(y)[:4] for y in (ir.academic_years or []) if str(y)[:4].isdigit()}
        semester_pins |= {str(t)[:4] for t in (ir.terms or []) if str(t)[:4].isdigit()}
        events = question_dates(ir.raw)
        if semester_pins or events:
            doc_terms = [str(t) for t in (metadata.get("academic_years") or [])]
            for term in metadata.get("terms") or []:
                doc_terms.extend([term] if isinstance(term, str) else term)
            doc_semester_years = {str(t)[:4] for t in doc_terms if str(t)[:4].isdigit()}
            doc_windows: set[tuple[str, str]] = set()
            for term in doc_terms:
                doc_windows |= set(_semester_windows(term))
            doc_title_years = set(_HARD_YEAR_RE.findall(str(metadata.get("title") or "")))

            semester_ok = bool(semester_pins & (doc_semester_years | doc_title_years)) if semester_pins else None
            event_ok = any(
                event in doc_windows or any(_event_fits_bare_year(event, y) for y in doc_title_years)
                for event in events
            ) if events else None
            anchors_exist = bool(doc_semester_years or doc_windows or doc_title_years)
            # a mismatch needs every asked anchor class to fail
            failed = all(result is False for result in (semester_ok, event_ok) if result is not None)
            if anchors_exist and failed:
                kinds.append("wrong_year_hard")
    audience_doc = _audience_class(metadata.get("audience"))
    audience_asked = _audience_class(ir.audience)
    if audience_asked and audience_doc and {audience_asked, audience_doc} == {"undergraduate", "graduate"}:
        kinds.append("wrong_audience_hard")
    return kinds


def _audience_class(value: Any) -> str:
    """Canonical audience category — "undergraduate" CONTAINS "graduate" and
    "grad", so raw substring checks misclassify every undergrad doc."""
    text = str(value or "").lower()
    if not text:
        return ""
    _aud = load_profile().section("audience")
    if any(key in text for key in [str(k) for k in _aud.get("undergraduate_markers", [])]):
        return "undergraduate"
    if any(key in text for key in [str(k) for k in _aud.get("graduate_markers", [])]):
        return "graduate"
    if any(key in text for key in [str(k) for k in _aud.get("faculty_markers", [])]):
        return "faculty"
    return text
