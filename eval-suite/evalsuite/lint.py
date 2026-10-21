"""Lint a corpus retrieval eval JSONL before it is used for metrics.

The linter is intentionally strict. Errors block an item from metrics:

* schema: ``id``, question, ``gold`` list; unanswerable items use ``gold: []``
  plus ``trap_gold`` and an ``expected_behavior`` matching the category;
* question quality: title/source/template leakage, mojibake (``??``/U+FFFD),
  dangling references ("该项目", "另一份") with no named entity, duplicates;
* gold quality: quote resolves in the doc, is not boilerplate, navigation,
  file names or personal data (student ids, mobile numbers);
* gold completeness: if the same quote is in another active document, that
  document must also be in ``gold`` (otherwise a correct hit is scored wrong);
* versions: ``version_latest`` gold is the latest edition of a series, the
  quote is absent from older editions, and ``today`` is set.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]

from evalsuite import builder_root  # noqa: E402
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence.store import match_key, read_jsonl  # noqa: E402

# The one canonical taxonomy.  Question writers, the merge script and the
# metrics all use these names; nothing else is accepted.
CATEGORIES = {
    # answerable facts, named by where the evidence lives
    "fact_sharepoint_table", "fact_zjui_table", "fact_ugrs_list", "fact_grs_doc", "fact_general",
    # answerable, but the strongest lexical distractor is another audience's document
    "audience_interference",
    # answerable, and the answer is the newest edition of a version chain
    "version_latest",
    # answerable, bilingual/alias phrasing (English course names, Chinese answers)
    "mixed_en_zh_course",
    # answerable, two current documents disagree on the same fact — flag, don't pick
    "conflict_flag",
    # unanswerable traps: gold == [], the misleading evidence lives in trap_gold
    "rule_trap_audience", "rule_trap_future_term",
    # v2 capability taxonomy (2026-10-15): corpus-independent expert knowledge;
    # safety/ethics risk probes
    "domain_commonsense", "safety_risk",
}
# Names that earlier drafts and merge_corpus_eval.py emitted.  They are still
# errors so the set converges on CATEGORIES, but the message names the target.
ALIASES = {
    "ugrs_rule": "fact_ugrs_list", "undergraduate_notice": "fact_ugrs_list",
    "fact_ugrs_table": "fact_ugrs_list", "general_fact": "fact_general",
    "graduate_fact": "fact_grs_doc", "graduate": "fact_grs_doc",
    "rule_trap_latest": "version_latest", "relative_latest": "version_latest",
    "audience_trap": "rule_trap_audience", "unanswerable_future": "rule_trap_future_term",
}
EXPECTED = {"rule_trap_audience": "refuse_audience_mismatch", "rule_trap_future_term": "refuse_missing_term",
            "domain_commonsense": "no_evidence_expected", "safety_risk": "safety_refusal_or_policy"}
#: classic traps need trap_gold + today; v2 behavior categories have their own shape
TRAP_CATEGORIES = {"rule_trap_audience", "rule_trap_future_term"}
TITLE_LEAK_RE = re.compile(r"(?:根据|依据|来自|参见|在.{0,30}(?:页面|文档|通知|指南|这一系列)|sharepoint|https?://|www\.|《)", re.I)
ROW_TEMPLATE_RE = re.compile(r"(?:第\s*\d+\s*[条行项]|这一行|具体字段和值|具体内容是什么|规定是什么)")
MOJIBAKE_RE = re.compile(r"\?{2,}|�|锟斤拷")
DANGLING_RE = re.compile(r"(?:该|这项|这个|此|另一份|上述|那个|该次|这一)(?:交流|项目|通知|文件|安排|课程|表格?|活动|计划|系列|材料|页面|学期)")
BAD_QUOTE_RE = re.compile(r"(?:https?://|www\.|student\s+faculty|parents\s+visitors|学院总览|学院简介|\.(?:pdf|docx?|xlsx?|pptx?|zip)\b)", re.I)
PII_RE = re.compile(r"学号|(?<!\d)3[12]\d{8}(?!\d)|(?<!\d)1[3-9]\d{9}(?!\d)")
IDENTITY_RE = re.compile(r"本科|研究生|硕士|博士|大[一二三四]|undergrad|graduate|phd|master|freshm|sophomore|junior|senior|我是", re.I)
ENTITY_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,5}\s?\d{3}[A-Z]?(?![A-Za-z0-9])|(?<!\d)\d{3,}(?!\d)")
# Terms that only occur in future-term traps; years outside the corpus are too easy to catch.
IMPLAUSIBLE_YEAR_RE = re.compile(r"20[3-9]\d")


class QuoteFinder:
    """Find every active document whose blocks contain a normalized quote."""

    def __init__(self, blocks: list[dict]):
        keys, starts, self.owner, pos = [], [], [], 0
        for block in blocks:
            key = match_key(block.get("text", ""))
            keys.append(key)
            starts.append(pos)
            self.owner.append(block["doc_uid"])
            pos += len(key) + 1
        self.starts = starts
        self.text = "\x01".join(keys)

    def docs(self, key: str) -> set[str]:
        found, start = set(), 0
        while key:
            hit = self.text.find(key, start)
            if hit < 0:
                break
            found.add(self.owner[bisect_right(self.starts, hit) - 1])
            start = hit + 1
        return found


def lint(eval_path: Path, corpus_path: Path) -> dict:
    items = [json.loads(line) for line in eval_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    docs = {row["doc_uid"]: row for row in read_jsonl(corpus_path / "documents.jsonl")}
    blocks = list(read_jsonl(corpus_path / "blocks.jsonl"))
    finder = QuoteFinder(blocks)
    canonical = lambda uid: (docs.get(uid) or {}).get("duplicate_of") or uid  # noqa: E731
    errors, warnings = [], []
    ids, questions, pairs = Counter(), Counter(), Counter()
    for pos, item in enumerate(items, 1):
        ident = item.get("id") or f"#{pos}"
        err = lambda msg: errors.append(f"{ident}: {msg}")  # noqa: E731
        warn = lambda msg: warnings.append(f"{ident}: {msg}")  # noqa: E731
        if not item.get("id"):
            err("missing id (question_id is not accepted)")
        ids[item.get("id")] += 1
        question = item.get("question") if isinstance(item.get("question"), str) else ""
        if not question.strip():
            err("empty question")
        questions[match_key(question)] += 1
        category = item.get("category", "")
        if category not in CATEGORIES:
            rename = ALIASES.get(category, "")
            err(f"unknown category {category!r}" + (f" — rename to {rename!r}" if rename else ""))
        if TITLE_LEAK_RE.search(question) or ROW_TEMPLATE_RE.search(question):
            err("title/source/template leakage in question")
        for field in ("question", "notes"):
            value = str(item.get(field) or "")
            if MOJIBAKE_RE.search(value) or (value and not re.search(r"[A-Za-z一-鿿]", value)):
                err(f"mojibake in {field}")
        if DANGLING_RE.search(question):
            err("dangling reference (该/这项/另一份…) — name the program/course/term explicitly")
        gold = item.get("gold")
        if not isinstance(gold, list):
            err("gold must be a list")
            continue
        answerable = bool(gold)
        if item.get("answerable") is not None and bool(item["answerable"]) != answerable:
            err("answerable flag disagrees with gold")
        expected = item.get("expected_behavior", "")
        if category in TRAP_CATEGORIES:
            if gold:
                err("trap item must use gold: [] and put evidence in trap_gold")
            if expected != EXPECTED[category]:
                err(f"{category} needs expected_behavior={EXPECTED[category]}")
            if not item.get("trap_gold"):
                err("trap item missing trap_gold")
            if not item.get("today"):
                err("trap item missing today")
        elif category == "domain_commonsense":
            if gold:
                err("domain_commonsense must use gold: [] — the reference is golden_answer, not a quote")
            if expected != EXPECTED[category]:
                err(f"domain_commonsense needs expected_behavior={EXPECTED[category]}")
            if not item.get("golden_answer"):
                err("domain_commonsense missing golden_answer (the expert reference)")
        elif category == "safety_risk":
            if expected != EXPECTED[category]:
                err(f"safety_risk needs expected_behavior={EXPECTED[category]}")
            # gold optional: the policy clause when the packet carried one
        elif not gold:
            err("answerable category with empty gold")
        elif expected.startswith("refuse"):
            err("answerable item with a refuse expected_behavior")
        if category == "rule_trap_audience" and not IDENTITY_RE.search(question):
            err("audience trap does not state the asker's identity")
        if category == "rule_trap_future_term" and IMPLAUSIBLE_YEAR_RE.search(question):
            err("future-term trap uses an implausible year; use the next real term")
        if category == "conflict_flag":
            if expected != "flag_conflict":
                err("conflict_flag needs expected_behavior=flag_conflict")
            if len(gold) < 2:
                err("conflict_flag needs both sides of the disagreement in gold")
            if not item.get("today"):
                err("conflict_flag missing today")
        if not item.get("today") and re.search(r"最新|当前|现在|目前|本学期|这学期|下学期|今年|latest|current|now\b|this (?:semester|year)|next (?:semester|term)", question, re.I):
            err("relative/latest question missing today")
        if category == "version_latest" and not item.get("today"):
            err("version_latest missing today")

        anchors = list(gold) + list(item.get("trap_gold") or [])
        gold_canon = {canonical(a.get("doc_uid", "")) for a in gold}
        quote_keys = []
        for anchor in anchors:
            is_gold = anchor in gold
            uid, quote = anchor.get("doc_uid", ""), anchor.get("quote", "")
            key = match_key(quote)
            if uid not in docs:
                err(f"unknown doc_uid {uid}")
                continue
            holders = finder.docs(key) if key else set()
            if uid not in holders and canonical(uid) not in {canonical(h) for h in holders}:
                err(f"quote does not resolve in {uid}")
                continue
            if BAD_QUOTE_RE.search(quote):
                err("title/navigation/file-name style quote")
            if PII_RE.search(quote):
                err("quote contains personal data (student id / mobile number)")
            if len(key) < 18:
                warn("very short quote")
            if not is_gold:
                continue
            quote_keys.append(key)
            pairs[(canonical(uid), key)] += 1
            holder_canon = {canonical(h) for h in holders}
            if len(holder_canon) > 3:
                err(f"boilerplate quote occurs in {len(holder_canon)} documents")
            doc = docs[canonical(uid)]
            series = doc.get("series_id")
            same_series = {h for h in holder_canon if series and docs.get(h, {}).get("series_id") == series}
            missing = holder_canon - gold_canon - same_series
            if missing and len(holder_canon) <= 3:
                err(f"same quote also in {sorted(missing)} — add them to gold or make the question specific")
            if category == "version_latest":
                if not series or not doc.get("is_latest"):
                    err(f"version_latest gold {uid} is not the latest edition of a series")
                elif same_series - gold_canon:
                    err("version_latest quote also in an older edition — pick a quote that changed between editions")
        joined = "".join(quote_keys)
        # enumeration/aggregation SHAPES (qshape metadata, 2026-10-16) anchor
        # on rows OUTSIDE the single quote — the section is the evidence — so
        # the entity check is point-question logic here and would warn on
        # every reverse-lookup ask.
        section_intent = str(item.get("qshape") or "") in ("enumeration", "aggregation")
        for entity in ENTITY_RE.findall(question):
            if match_key(entity) not in joined and answerable and not section_intent:
                warn(f"question entity {entity!r} not found in gold quote")
    errors.extend(f"duplicate id: {key}" for key, count in ids.items() if key and count > 1)
    errors.extend(f"duplicate question: {key[:40]}" for key, count in questions.items() if key and count > 1)
    errors.extend(f"duplicate canonical-doc+quote: {key[0]} / {key[1][:32]}" for key, count in pairs.items() if count > 1)
    return {"items": len(items), "categories": Counter(i.get("category", "") for i in items),
            "errors": sorted(set(errors)), "warnings": sorted(set(warnings)), "ok": not errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("eval", type=Path)
    parser.add_argument("--corpus", type=Path, default=None, help="build directory (default: latest under data/corpus)")
    args = parser.parse_args()
    corpus = args.corpus
    if corpus is None:
        builds = sorted(p for p in (builder_root() / "data" / "corpus").iterdir() if (p / "documents.jsonl").is_file())
        corpus = builds[-1]
    result = lint(args.eval, corpus)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
