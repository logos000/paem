"""Document-level metadata rules: type, audience, scope, authority, tier, terms."""

from __future__ import annotations

import re
from typing import Any

from evidence.store import normalize_text
from domain_profile import load_profile
from evidence.query_ir import extract_cohorts, extract_course_codes, extract_terms

DOC_NUMBER_RE = re.compile(r"([一-龥A-Za-z]{1,12}[〔\[【(（]\s*(?:19|20)\d{2}\s*[〕\]】)）]\s*\d{1,4}\s*号)")
CN_DATE_RE = re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
ISO_DATE_RE = re.compile(r"((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
US_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})(?!\d)")

# Domain knowledge (sites, doc types, audience/major markers, effective-date
# formats) lives in the DomainProfile build section (2026-10-16).
_BUILD = load_profile().section("build")
SITE_DEFAULTS: dict[str, dict[str, Any]] = {k: dict(v) for k, v in _BUILD.get("sites", {}).items()}
DOC_TYPE_RULES = tuple((str(name), re.compile(str(pat), re.I)) for name, pat in _BUILD.get("doc_type_rules", []))
AUTHORITY_BY_TYPE = {str(k): int(v) for k, v in _BUILD.get("authority_by_type", {}).items()}

def _bp(key: str) -> "re.Pattern":
    return load_profile().pattern("build", key)

FACULTY_RE = re.compile(str(_BUILD.get("audience_titles", {}).get("faculty", "")), re.I)
GRADUATE_RE = re.compile(str(_BUILD.get("audience_titles", {}).get("graduate", "")), re.I)
UNDERGRAD_RE = re.compile(str(_BUILD.get("audience_titles", {}).get("undergraduate", "")), re.I)
EXCHANGE_RE = re.compile(str(_BUILD.get("audience_titles", {}).get("exchange", "")), re.I)
COLLEGE_RE = re.compile(str(_BUILD.get("college_marker", "")))
PROGRAM_RE = _bp("program_marker")
COURSE_RE = re.compile(str(_BUILD.get("course_marker", "")))


EFFECTIVE_CN_RE = re.compile(str(_BUILD.get("effective_cn", "")))
EFFECTIVE_EN_RE = _bp("effective_en")


def effective_from(text: str) -> tuple[str, str]:
    """P1-5: the document's own effective date ("本办法自…起施行").

    Returns ``(date, evidence_snippet)`` — the snippet is the matched原文 so the
    value stays source-anchored; empty when the document states none.
    """
    for pattern in (EFFECTIVE_CN_RE, EFFECTIVE_EN_RE):
        match = pattern.search(text[:6000])
        if match:
            raw = match.group(1)
            dates = find_dates(raw)
            if dates:
                return dates[0], match.group(0)[:80]
    return "", ""


def find_dates(text: str) -> list[str]:
    values: list[str] = []
    for pattern, order in ((CN_DATE_RE, (1, 2, 3)), (ISO_DATE_RE, (1, 2, 3)), (US_DATE_RE, (3, 1, 2))):
        for match in pattern.finditer(text):
            year, month, day = (int(match.group(i)) for i in order)
            if 1 <= month <= 12 and 1 <= day <= 31:
                values.append(f"{year:04d}-{month:02d}-{day:02d}")
    return values


def doc_type(title: str, body: str = "", *, site: str = "", kind: str = "page") -> str:
    for name, pattern in DOC_TYPE_RULES:
        if pattern.search(title):
            return name
    head = body[:400]
    # Navigation menus in the body head link to "Calendar"/"课表" pages; only
    # the rule-like types are trusted from body text.
    for name, pattern in DOC_TYPE_RULES[3:5]:
        if pattern.search(head):
            return name
    if site == "news":
        return "news"
    if kind == "attachment" and re.search(r"\.(?:xlsx?|docx?)$", title, re.I):
        return "form"
    return "page"


def audience(site: str, title: str, breadcrumb: list[str], body: str, *, doc_type_value: str = "") -> str:
    default = SITE_DEFAULTS.get(site, {}).get("audience", "public")
    crumbs = " ".join(breadcrumb)
    head = f"{title} {crumbs}"
    if FACULTY_RE.search(head):
        return "faculty"
    if site in ("sharepoint", "zjui"):
        # ZJUI also admits master's/PhD students; those notices are not for undergraduates.
        if GRADUATE_RE.search(head) and not UNDERGRAD_RE.search(head):
            return "graduate"
        # P1-1: body evidence overrides the site default too (ZJUI hosts
        # graduate/faculty/postdoc material whose title is generic).  Two
        # distinct keyword hits are required so a single nav or related-link
        # mention does not flip an undergraduate document.  News pages stay on
        # the default audience: research news mentions professors and grad
        # students as subjects, not as the addressed audience.
        body_head = body[:1500]
        faculty_hits = {m.group(0).lower() for m in FACULTY_RE.finditer(body_head)}
        grad_hits = {m.group(0).lower() for m in GRADUATE_RE.finditer(body_head)}
        if doc_type_value != "news" and not UNDERGRAD_RE.search(body_head):
            if len(faculty_hits) >= 2:
                return "faculty"
            if len(grad_hits) >= 2 and not faculty_hits:
                return "graduate"
        return default
    if GRADUATE_RE.search(head) and not UNDERGRAD_RE.search(head):
        return "graduate"
    if UNDERGRAD_RE.search(head) and not GRADUATE_RE.search(head):
        return "undergraduate"
    if site in ("zju", "news", "libweb") and GRADUATE_RE.search(body[:600]) and not UNDERGRAD_RE.search(body[:600]):
        return "graduate"
    return default


def scope_level(site: str, title: str, breadcrumb: list[str]) -> str:
    head = f"{title} {' '.join(breadcrumb)}"
    if COURSE_RE.search(title):
        return "course"
    if PROGRAM_RE.search(title) and re.search(r"培养方案|program\s+plan|curriculum|prerequisite", head, re.I):
        return "program"
    if site in ("sharepoint", "zjui"):
        return "institute"
    if COLLEGE_RE.search(title):
        return "school"
    return SITE_DEFAULTS.get(site, {}).get("scope_level", "university")


def tier(site: str, audience_value: str, title: str, breadcrumb: list[str], kind: str) -> str:
    base = SITE_DEFAULTS.get(site, {}).get("tier", "distractor")
    if base != "core":
        return base
    head = f"{title} {' '.join(breadcrumb)}"
    if audience_value == "faculty":
        return "support"
    if site == "ugrs" and EXCHANGE_RE.search(head):
        return "support"
    return "core"


def build_metadata(*, site: str, kind: str, title: str, body: str, breadcrumb: list[str] | None = None, published: str = "", url: str = "") -> dict[str, Any]:
    breadcrumb = breadcrumb or []
    title = normalize_text(title)
    head_text = f"{title}\n{' '.join(breadcrumb)}\n{body[:1500]}"
    title_terms, title_years = extract_terms(title)
    body_terms, body_years = extract_terms(body[:4000])
    numbers = DOC_NUMBER_RE.findall(body[:3000]) or DOC_NUMBER_RE.findall(title)
    eff_date, eff_snippet = effective_from(body)
    dtype = doc_type(title, body, site=site, kind=kind)
    aud = audience(site, title, breadcrumb, body, doc_type_value=dtype)
    meta = {
        "doc_type": dtype,
        "audience": aud,
        "scope_level": scope_level(site, title, breadcrumb),
        "tier": tier(site, aud, title, breadcrumb, kind),
        "publisher": SITE_DEFAULTS.get(site, {}).get("publisher", site),
        "authority_rank": min(5, AUTHORITY_BY_TYPE.get(dtype, 2) + (1 if SITE_DEFAULTS.get(site, {}).get("authority", 0) >= 4 and dtype in ("notice", "page") else 0)),
        "doc_number": normalize_text(numbers[0]).replace(" ", "") if numbers else "",
        # Title terms are the document's own term; body terms are mentions.
        "terms": title_terms or body_terms[:4],
        "academic_years": title_years or body_years[:2],
        "term_source": "title" if title_terms or title_years else ("body" if body_terms or body_years else ""),
        "cohorts": extract_cohorts(head_text),
        "course_codes": extract_course_codes(title) or extract_course_codes(body[:2000])[:20],
        "dates_mentioned": sorted(set(find_dates(body[:20000])))[:50],
        "published": published,
        # P1-5: four-date separation. effective_from is body-extracted with its
        # matched span; retrieved_at/date_provenance are attached by the pipeline.
        "effective_from": eff_date,
        "effective_from_source": eff_snippet,
        "language": "zh" if len(re.findall(r"[一-鿿]", body[:2000] or title)) > 20 or re.search(r"[一-鿿]", title) else "en",
    }
    return meta
