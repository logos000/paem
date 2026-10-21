# -*- coding: utf-8 -*-
"""Build the governance trap question set from the SP corpus (2026-10-17).

Five families, each mapping to a governance capability:

  wrong_term      ask term X's fact; a sibling member of the same version
                  series (term Y != X, near-duplicate title) must surface in
                  NAIVE dense top-k for the trap to count
  current_version ask the LATEST member's fact without a term qualifier
  abstain         plausible-but-absent term / program; topical neighbours
                  must still fire in naive top-k (so a naive RAG would answer)
  audience        faculty-only policy asked as undergraduate
  conflict        intra-doc dual-value fact; correct answer presents BOTH

Anti-rigging rules (agreed 2026-10-17):
  * questions are ordinary user phrasings — no mechanism language;
  * every trap is mechanically verified to FIRE: the misleading doc must
    appear in the naive-dense top-10 (governance flags off, config
    active_dense) — otherwise the question is dropped, not kept;
  * trap family is NON-dimension metadata (qshape); the five-dim matrix
    fields keep their existing value sets.

Output rows are e2e_product --eval compatible: id/question/gold/category/
level/qshape + trap metadata.  Rows start verified:false (draft) — dual AI
review + user 30% spot-check happen before any freeze.

Usage:
  ../llm-corpus-builder/.venv/Scripts/python.exe scripts/build_trapset.py \
      --corpus <build> --out data/.../trapset_v1.jsonl [--today 2026-10-15]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evalsuite import builder_root  # noqa: E402

DATE_RE = re.compile(
    r"(?:19|20)\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}日?|\d{4}-\d{1,2}-\d{1,2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{2}",
    re.I,
)
FACT_KEYWORDS = [
    ("application_deadline", r"application.{0,40}deadline|deadline.{0,40}application|申请.{0,12}截止|截止.{0,12}申请"),
    ("drop_deadline", r"drop|withdraw|退课"),
    ("enrollment_deadline", r"enroll|registration|选课|注册"),
    ("exam", r"exam|final|考试"),
    ("deadline_generic", r"deadline|due|截止"),
]
TERM_ZH = {"spring": "春季学期", "fall": "秋季学期", "summer": "夏季学期", "winter": "冬季学期"}
SERIES_ZH = {
    "course enrollment guide": "选课指南",
    "zjui class schedule": "课表",
    "class schedule": "课表",
    "course number": "课程号清单",
    "course class number": "课程号清单",
    "zjui course offerings": "开课清单",
    "uiuc exchange application": "UIUC 交换申请",
}


def _term_zh(term: str) -> str:
    year, season = term.split("-")
    return f"{year}年{TERM_ZH[season]}"


def _term_en(term: str) -> str:
    year, season = term.split("-")
    return f"{'Spring' if season == 'spring' else 'Fall' if season == 'fall' else season.title()} {year}"


def _fact_word(kind: str, lang: str) -> str:
    words = {
        "application_deadline": ("申请截止日期", "application deadline"),
        "drop_deadline": ("退课申请最迟需要在什么时候提交并获批准？", "by when a course-drop request must be submitted and approved?"),
        "enrollment_deadline": ("选课截止日期", "enrollment deadline"),
        "exam": ("考试时间安排", "exam schedule"),
        "deadline_generic": ("截止日期", "deadline"),
    }
    if kind == "drop_deadline":
        return words[kind][0 if lang == "zh" else 1]
    return words[kind][0 if lang == "zh" else 1]


def _fact_kind(text: str) -> str | None:
    for kind, pattern in FACT_KEYWORDS:
        if re.search(pattern, text, re.I):
            return kind
    return None


def _signature(text: str) -> str:
    """Same-fact signature across series members: dates/numbers stripped."""
    key = re.sub(r"\d", "#", text.lower())
    key = re.sub(r"[^a-z#\u4e00-\u9fff]", "", key)
    return key[:60]


def _lang_of(doc: dict) -> str:
    return doc.get("language", "en")


def _quote_around(text: str, match_start: int, before: int = 120, after: int = 260) -> str:
    """Gold quote centred on the fact (a date or a course code), so the window
    always contains the value the question asks for — reviewers rejected
    head-truncated block quotes."""
    start = max(0, match_start - before)
    return text[start : match_start + after].strip()


def _quote_sentences(text: str, match_start: int) -> str:
    """Whole sentence(s) containing the fact, plus one neighbour each side."""
    parts = re.split(r"(?<=[。！？.!?])\s+|\n+", text)
    pos = 0
    hit = 0
    for i, part in enumerate(parts):
        if pos <= match_start < pos + len(part):
            hit = i
            break
        pos += len(part) + 1
    window = parts[max(0, hit - 1) : hit + 2]
    return " ".join(p.strip() for p in window if p.strip())[:600]


def mine_candidates(corpus: Path) -> list[dict]:
    docs = {d["doc_uid"]: d for d in (json.loads(l) for l in open(corpus / "documents.jsonl", encoding="utf-8"))}
    blocks_by_doc: dict[str, list[dict]] = defaultdict(list)
    for line in open(corpus / "blocks.jsonl", encoding="utf-8"):
        b = json.loads(line)
        blocks_by_doc[b["doc_uid"]].append(b)
    series = json.loads((corpus / "series.json").read_text(encoding="utf-8"))
    active_terms = {t for d in docs.values() if d.get("status") == "active" for t in (d.get("terms") or [])}

    candidates: list[dict] = []

    # ---- wrong_term + current_version: date facts that differ across members
    emitted = 0
    for s in series:
        if "exchange" in s["key"]:  # reviewer finding: exchange-app facts mis-mine (exam/advice text)
            continue
        members = [(docs[m["doc_uid"]], m.get("terms") or []) for m in s["members"] if docs[m["doc_uid"]].get("status") == "active"]
        if len(members) < 2:
            continue
        series_zh = SERIES_ZH.get(s["key"].split(" #")[0], s["key"][:20])
        per_series = 0
        for doc, terms in members:
            if per_series >= 3 or emitted >= 26:
                break
            term = terms[0] if terms else ""
            if not term:
                continue
            for block in blocks_by_doc.get(doc["doc_uid"], []):
                dates = DATE_RE.findall(block["text"])
                if not dates:
                    continue
                kind = _fact_kind(block["text"])
                if kind not in ("application_deadline", "drop_deadline", "enrollment_deadline", "deadline_generic"):
                    continue
                date_pos = DATE_RE.search(block["text"]).start()
                sig = _signature(block["text"])
                siblings = [
                    (doc2, terms2) for doc2, terms2 in members
                    if doc2 is not doc and terms2 and terms2[0] != term
                    and any(DATE_RE.search(b2["text"]) for b2 in blocks_by_doc.get(doc2["doc_uid"], []))
                ]
                if not siblings:
                    continue
                # the sibling must state a DIFFERENT date somewhere (any fact kind —
                # the rank-based fire check below is the real gate)
                sib = siblings[0]
                sib_dates = [d for b2 in blocks_by_doc.get(sib[0]["doc_uid"], []) for d in DATE_RE.findall(b2["text"])]
                if sorted(map(str, dates)) == sorted(map(str, sib_dates)):
                    continue
                lang = _lang_of(doc)
                quote = _quote_sentences(block["text"], date_pos)
                base = {
                    "trap_type": "wrong_term",
                    "term": term,
                    "doc_uid": doc["doc_uid"],
                    "misleading_doc": sib[0]["doc_uid"],
                    "misleading_term": sib[1][0],
                    "series_key": s["key"],
                    "fact_kind": kind,
                    "gold": [{"doc_uid": doc["doc_uid"], "quote": quote}],
                    "level": "C2",
                    "intent": "point",
                    "tone": "neutral",
                    "family": kind,
                }
                for q_lang, q_text in (
                    ("zh", f"ZJUI {_term_zh(term)}的{series_zh}里，{_fact_word(kind, 'zh')}"),
                    ("en", f"In the ZJUI {_series_en(s['key'])} for {_term_en(term)}, what is the {_fact_word(kind, 'en')}"),
                ):
                    candidates.append({**base, "language": q_lang, "question": q_text})
                per_series += 1
                emitted += 2
                break  # one fact per member is enough

        # wrong_term via schedule course rows was RETIRED after dual review:
        # the schedule "rows" are final-exam time grids (No.: 3 | Time: 9:00-9:30
        # | Monday: <courses>), so "上课时间和教室" questions mismatched them.

        # current_version: latest member's fact, no term qualifier
        if s.get("ambiguous_latest"):
            continue
        latest = members[-1] if members else None
        if latest and len(members) >= 2:
            doc, terms = latest
            old = members[0][0]
            for block in blocks_by_doc.get(doc["doc_uid"], []):
                kind = _fact_kind(block["text"])
                date_match = DATE_RE.search(block["text"])
                if kind in ("application_deadline", "drop_deadline", "enrollment_deadline", "deadline_generic") and date_match:
                    # inert-trap guard (audit 2026-10-17): if the OLD member carries
                    # the identical fact text, "current version" tests citation
                    # source only, not fact currency — skip.
                    import unicodedata
                    norm = lambda s: "".join(ch for ch in unicodedata.normalize("NFKC", s) if not ch.isspace())
                    fact_text = norm(block["text"])
                    if any(fact_text in norm(b2["text"]) for b2 in blocks_by_doc.get(old["doc_uid"], [])):
                        continue
                    series_zh = SERIES_ZH.get(s["key"].split(" #")[0], s["key"][:20])
                    candidates.append({
                        "trap_type": "current_version", "language": "zh",
                        "question": f"最新的 ZJUI {series_zh}里，{_fact_word(kind, 'zh')}是什么时候？",
                        "doc_uid": doc["doc_uid"], "misleading_doc": old["doc_uid"],
                        "misleading_term": (members[0][1] or [""])[0],
                        "series_key": s["key"], "fact_kind": kind,
                        "gold": [{"doc_uid": doc["doc_uid"], "quote": _quote_sentences(block["text"], date_match.start())}],
                        "level": "C2", "intent": "point", "tone": "neutral", "family": kind,
                    })
                    break

    # ---- abstain: absent terms + absent programs (neighbours must exist)
    absent_terms = [t for t in ("2024-summer", "2020-fall", "2027-fall", "2021-spring", "2028-spring") if t not in active_terms]
    zh_q = {
        "2024-summer": "ZJUI 2024年夏季学期的选课申请截止日期是什么时候？",
        "2020-fall": "ZJUI 2020年秋季学期的退课截止日期是什么时候？",
        "2027-fall": "ZJUI 2027年秋季学期选课什么时候开始？",
        "2021-spring": "ZJUI 2021年春季学期的课表什么时候发布？",
        "2028-spring": "ZJUI 2028年春季学期的UIUC交换申请截止日期是什么？",
    }
    for t in absent_terms:
        candidates.append({"trap_type": "abstain", "language": "zh", "question": zh_q[t],
                           "doc_uid": "", "misleading_doc": "", "absent_term": t,
                           "gold": [], "level": "C1", "intent": "point", "tone": "neutral",
                           "family": "deadline_generic"})
    for prog_zh, prog_en in (("化学工程", "Chemical Engineering"), ("环境工程", "Environmental Engineering")):
        candidates.append({"trap_type": "abstain", "language": "zh",
                           "question": f"ZJUI {prog_zh}专业的培养方案里有哪些必修课？",
                           "doc_uid": "", "misleading_doc": "", "absent_program": prog_zh,
                           "gold": [], "level": "C1", "intent": "quantity", "tone": "neutral",
                           "family": "curriculum"})
        candidates.append({"trap_type": "abstain", "language": "en",
                           "question": f"Which courses are required in the {prog_en} curriculum at ZJUI?",
                           "doc_uid": "", "misleading_doc": "", "absent_program": prog_en,
                           "gold": [], "level": "C1", "intent": "quantity", "tone": "neutral",
                           "family": "curriculum"})

    # ---- audience family RETIRED (dual review 2026-10-17): the corpus's only
    # faculty-audience doc explicitly answers undergraduates ("If you are a UG
    # student, you must have enrolled the course… A level grade" + deadlines),
    # and a newer undergrad-audience TA doc exists — gold=[] was factually wrong.
    # The SP corpus has no valid audience trap; the family stays implemented
    # for corpora that do.

    # ---- conflict: intra-doc dual-value facts
    for line in open(corpus / "conflict_candidates.jsonl", encoding="utf-8"):
        row = json.loads(line)
        if row.get("status") != "candidate" or len({v["value"] for v in row.get("values", [])}) < 2:
            continue
        kind = row["fact_kind"]
        values = row["values"][:2]
        doc = values[0]
        if kind == "deadline":
            question = "ZJUI UIUC交换项目的申请截止日期有哪些？"
        else:
            question = "ZJUI UIUC交换项目的联系人邮箱有哪些？"
        candidates.append({"trap_type": "conflict", "language": "zh", "question": question,
                           "doc_uid": doc["doc_uid"], "misleading_doc": doc["doc_uid"],
                           "gold": [{"doc_uid": v["doc_uid"], "quote": v["span"]} for v in values],
                           "level": "C3", "intent": "point", "tone": "neutral",
                           "family": f"conflict_{kind}"})

    # dedupe by question, number ids by family
    seen: set[str] = set()
    counters: dict[str, int] = defaultdict(int)
    out: list[dict] = []
    for c in candidates:
        if c["question"] in seen:
            continue
        seen.add(c["question"])
        counters[c["trap_type"]] += 1
        c["id"] = f"trap-{c['trap_type'][:4]}-{counters[c['trap_type']]:02d}"
        c["qshape"] = c["trap_type"]
        c["category"] = "trapset"
        c["verified"] = False
        out.append(c)
    return out


def naive_topk(corpus: Path, questions: list[str], today: date, k: int = 20) -> dict[str, list[str]]:
    """Doc-id ranking a naive dense RAG would see: governance switches off,
    dense-only config — the pack evidence plus dropped candidates are the raw
    retrieval view."""
    os.environ.update({
        "RUNTIME_HARD_FILTER": "0", "RUNTIME_NEIGHBOR_STITCH": "0",
        "RUNTIME_SECTION_ROLLUP": "0", "RUNTIME_COVERAGE_UNION": "0",
        "RUNTIME_FACT_RESCUE": "0",
    })
    from runtime.engine import CorpusEngine  # via evalsuite path bridge

    engine = CorpusEngine.load(corpus, config="active_dense")
    rankings: dict[str, list[str]] = {}
    for q in questions:
        payload = engine.answer_context(q, today=today, k=k)
        ids: list[str] = []
        for item in payload["evidence_pack"]["evidence"] + (payload["evidence_pack"].get("dropped_candidates") or []):
            if item.get("doc_id") not in ids:
                ids.append(item.get("doc_id"))
        rankings[q] = ids
    return rankings


def verify(rows: list[dict], corpus: Path, today: date) -> tuple[list[dict], list[dict]]:
    rankings = naive_topk(corpus, [r["question"] for r in rows], today)
    docs_terms: dict[str, list[str]] = {}
    for line in open(corpus / "documents.jsonl", encoding="utf-8"):
        d = json.loads(line)
        docs_terms[d["doc_uid"]] = d.get("terms") or []
    kept: list[dict] = []
    dropped: list[dict] = []
    for r in rows:
        top = [i for i in rankings.get(r["question"], []) if i]
        t = r["trap_type"]
        rank_of = lambda uid: (top.index(uid) + 1) if uid in top else 0
        gold_rank = rank_of(r["doc_uid"]) if r.get("doc_uid") else 0
        if t in ("wrong_term", "current_version"):
            # a wrong-term doc is one whose terms do not contain the question's
            # target (the latest member for current_version)
            want = {r["term"]} if t == "wrong_term" else set(docs_terms.get(r["doc_uid"], []))
            wrong_above = [i for i in top[:10] if set(docs_terms.get(i, [])) and not (set(docs_terms.get(i, [])) & want)]
            fired = bool(wrong_above) and (gold_rank == 0 or top.index(wrong_above[0]) < max(gold_rank - 1, 0) or gold_rank > 1)
            r = {**r, "gold_rank_naive": gold_rank}
        elif t == "audience":
            fired = r["misleading_doc"] in set(top[:10])
        elif t == "conflict":
            fired = r["misleading_doc"] in set(top[:5])
        else:  # abstain: a topical neighbour must fire (else nothing to refuse against)
            fired = len(set(top[:10])) >= 3
        r = {**r, "naive_rank_of_misleading": (top.index(r["misleading_doc"]) + 1) if r.get("misleading_doc") in top else 0,
             "naive_top10_n": len(set(top[:10])), "trap_fired": bool(fired)}
        (kept if fired else dropped).append(r)
    return kept, dropped


def _series_en(key: str) -> str:
    en = {"course enrollment guide": "Course Enrollment Guide", "zjui class schedule": "Class Schedule",
          "class schedule": "Class Schedule", "course number": "Course Number List",
          "course class number": "Course Class Number List", "zjui course offerings": "Course Offerings",
          "uiuc exchange application": "UIUC Exchange Application"}
    return en.get(key.split(" #")[0], key[:30])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", type=Path, default=builder_root() / "data" / "corpus" / "sharepoint-only-20261015")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "trapset_v1.jsonl")
    parser.add_argument("--today", default="2026-10-15")
    args = parser.parse_args()

    rows = mine_candidates(args.corpus)
    kept, dropped = verify(rows, args.corpus, date.fromisoformat(args.today))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        for r in kept:
            handle.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"candidates {len(rows)} -> kept {len(kept)} (dropped {len(dropped)}: trap did not fire)")
    from collections import Counter
    print("mined by family:", dict(Counter(r['trap_type'] for r in rows)))
    print("kept by family:", dict(Counter(r['trap_type'] for r in kept)))
    for r in dropped[:10]:
        print(f"  DROP [{r['trap_type']}] {r['id']}: naive rank {r['naive_rank_of_misleading']} | {r['question'][:60]}")


if __name__ == "__main__":
    main()
