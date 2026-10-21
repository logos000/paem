"""Version chains: group documents that are editions of the same series.

Examples from the crawl: ``V6/V7/V9-ZJUI Class Schedule FA26``, the ZJUI
teaching calendars for every semester, ``Course Enrollment Guide`` for each
term, ``Course-Drop-Request`` vs ``Course-Drop-Request(1)``.  A series is keyed
by the normalized title with term/version markers removed; members are ordered
by (term, explicit version number, date) and the newest is ``is_latest``.
"""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any

from evidence.store import normalize_text, sha1
from domain_profile import load_profile
from evidence.query_ir import term_order

# Series-key strip rules live in the DomainProfile build section.
_STRIP = [str(rule) for rule in load_profile().section("build").get("series_strip", [])]
_STRIP_RE = re.compile("|".join(f"(?:{p})" for p in _STRIP), re.I)
_VERSION_RE = load_profile().pattern("build", "series_version")

# P1-6: titles that carry no business information after normalization.  A
# chain under such a generic key is only trustworthy when every member has its
# own term — otherwise unrelated attachments ("预览PDF" ×5) get force-ordered
# into a fake version chain and the wrong one is superseded.
_GENERIC_KEY_RE = re.compile(str(load_profile().section("build").get("series_generic_key", "")))


def _generic_key(key: str) -> bool:
    return bool(_GENERIC_KEY_RE.match(key))


#: Public alias: the annotation rule layer reuses the same guard so an LLM
#: series candidate can never create a fake version chain from a generic key.
is_generic_series_key = _generic_key


_COHORT_RE = re.compile(str(load_profile().section("build").get("series_cohort", "")))


def series_key(title: str) -> str:
    text = normalize_text(title).casefold()
    text = re.sub(r"\.(?:pdf|docx?|xlsx?|html?|aspx)$", "", text)
    # Curricula for different cohorts are parallel documents, not editions.
    cohorts = sorted({a or b for a, b in _COHORT_RE.findall(text)})
    text = _COHORT_RE.sub(" ", text)
    text = _STRIP_RE.sub(" ", text)
    text = re.sub(r"[\s_\-–—,，、:：|()（）\[\]【】]+", " ", text).strip()
    return f"{text} #cohort{'/'.join(cohorts)}" if cohorts and text else text


def version_number(title: str) -> float:
    match = _VERSION_RE.search(title)
    if not match:
        return 0.0
    return float(match.group(1) or match.group(2))


def _sort_key(doc: dict[str, Any]) -> tuple:
    terms = doc.get("terms") or []
    ay = doc.get("academic_years") or []
    order = max((term_order(t) for t in terms), default=0.0)
    if not order and ay:
        order = int(ay[0][:4]) + 0.7
    status = 1 if re.search(r"final|最终|正式", doc.get("title", ""), re.I) else (-1 if re.search(r"draft|草案|征求意见", doc.get("title", ""), re.I) else 0)
    # P3: "自某日起施行" beats publication date when title versions tie —
    # effective_from is the document's own claim of when it takes precedence.
    return (order, version_number(doc.get("title", "")), status, doc.get("effective_from") or "", doc.get("published") or "", doc.get("modified") or "")


def build_series(docs: list[dict[str, Any]], *, min_key_chars: int = 4) -> list[dict[str, Any]]:
    """Return series records and annotate ``docs`` in place.

    Grouping is restricted to the same site and doc_type so that a ugrs
    notice and a SharePoint guide with similar titles are not one series.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        if doc.get("status") != "active" or doc.get("page_kind") in ("listing", "home") or doc.get("doc_type") == "news":
            continue
        key = series_key(doc.get("title", ""))
        if len(key) < min_key_chars:
            continue
        site = "zjui-family" if doc.get("site") in ("zjui", "sharepoint") else doc.get("site", "")
        groups[(site, doc.get("doc_type", ""), key)].append(doc)
    series: list[dict[str, Any]] = []
    for (site, dtype, key), members in groups.items():
        if len(members) < 2:
            continue
        # Same title with no term/version/date difference is not a version
        # chain (e.g. one fact-sheet title reused by several exchange programs).
        if len({_sort_key(m)[:2] for m in members}) < 2 and len({_sort_key(m)[3:] for m in members}) < 2:
            continue
        # P1-6: a generic key ("预览PDF", "class schedule") only forms a chain
        # when every member carries its own term — otherwise the chain is
        # indistinguishable from unrelated documents sharing a generic name.
        if _generic_key(key) and not all(m.get("terms") for m in members):
            continue
        members.sort(key=_sort_key)
        sid = "s_" + sha1(f"{site}|{dtype}|{key}")[:12]
        latest = members[-1]
        tied = [m for m in members if _sort_key(m) == _sort_key(latest)]
        for order, doc in enumerate(members, 1):
            doc["series_id"] = sid
            doc["version_order"] = order
            doc["is_latest"] = doc is latest
            doc["superseded_by"] = "" if doc is latest else members[order]["doc_uid"]
        series.append({
            "series_id": sid,
            "key": key,
            "site": site,
            "doc_type": dtype,
            "size": len(members),
            "latest": latest["doc_uid"],
            "ambiguous_latest": len(tied) > 1,
            "members": [{"doc_uid": m["doc_uid"], "title": m.get("title", ""), "terms": m.get("terms", []), "published": m.get("published", "")} for m in members],
        })
    for doc in docs:
        doc.setdefault("series_id", "")
        doc.setdefault("version_order", 1)
        doc.setdefault("is_latest", True)
        doc.setdefault("superseded_by", "")
    series.sort(key=lambda row: (-row["size"], row["key"]))
    return series
