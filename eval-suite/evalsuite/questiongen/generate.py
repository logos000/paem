"""Five independent generation tasks, one per capability level (user ruling
2026-10-15): each level selects its own material from the P1 packets, runs
its own specialized prompt, and writes its own sub-question-set file —
``<stem>_C1.jsonl`` … ``<stem>_C5.jsonl`` (variants in ``_variants``
siblings).  The union of sub-sets is the master set; each can be
linted/reviewed/verified independently.

Shared infrastructure stays: packets, joint golden answers + numeric guard,
family variants, dedup/leak cleanup, gold-holder expansion.
"""

from __future__ import annotations

import json
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from evalsuite.lint import QuoteFinder
from evalsuite.questiongen import prompts
from evalsuite.questiongen.packets import build_packets
from evalsuite.questiongen.provider import QuestionGenProvider
from evalsuite.questiongen.schemas import normalize_row
from evidence.store import match_key
from retrieve.lexical import Corpus

TONE_CYCLE = ("formal", "colloquial", "keyword", "polite")
C1_QUOTA = 30
WORKERS = 20  # campus gateway default concurrency (user ruling 2026-10-16)

_RULE_MARKERS = ("不得", "必须", "禁止", "shall", "must not", "not allowed", "cannot", "is required to", "截止", "逾期", "违规", "诚信", "deadline")
_PARAPHRASE_MARKERS = r"如果|若|须|应当|否则|means|指|是指|定义"


def _rule_blocks(blocks: list[dict[str, Any]], cap: int = 5) -> list[dict[str, Any]]:
    return [b for b in blocks if any(m in str(b.get("text", "")) for m in _RULE_MARKERS)][:cap]


def _value_blocks(blocks: list[dict[str, Any]], cap: int = 6) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("facts")][:cap]


def _paraphrase_blocks(blocks: list[dict[str, Any]], cap: int = 5) -> list[dict[str, Any]]:
    return [b for b in blocks if re.search(_PARAPHRASE_MARKERS, str(b.get("text", ""))) or any(m in str(b.get("text", "")) for m in _RULE_MARKERS)][:cap]


def _over_pool(packets: list[dict[str, Any]], fn: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fn, packets))
    return [item for result in results for item in result]


def _parse_list(raw: Any) -> list[dict[str, Any]]:
    return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


# ------------------------------------------------------------------ tasks
def _task_c1(corpus: Corpus, provider: QuestionGenProvider, rng: random.Random) -> list[dict[str, Any]]:
    titles = sorted({str(d.get("title", "")) for d in corpus.documents.values() if d.get("title")})[:120]
    items: list[dict[str, Any]] = []
    for batch, start in enumerate(range(0, C1_QUOTA, 10)):
        # Rotate the title window per batch: with one fixed context the model
        # re-emits the same commonsense asks and exact-dedup leaves ~1/3 of
        # the quota (seen live 2026-10-16: 30 raw → 10 kept).
        window = titles[batch * 40 % max(1, len(titles)):][:60] or titles[:60]
        context = "\n".join(f"- {t}" for t in window)
        try:
            raw = provider.generate_json(prompts.c1_prompt(min(10, C1_QUOTA - start), context), task="qg-c1")
        except Exception as error:
            print(f"[questiongen] C1 batch failed: {error}", file=sys.stderr)
            continue
        items.extend(_parse_list(raw))
    return items


def _task_c2(packets: list[dict[str, Any]], provider: QuestionGenProvider, rng: random.Random) -> list[dict[str, Any]]:
    def one(packet: dict[str, Any]) -> list[dict[str, Any]]:
        blocks = _value_blocks(packet.get("blocks", []))
        if len(blocks) < 2:
            return []
        n = 3 + (1 if rng.random() < 0.5 else 0)
        try:
            raw = provider.generate_json(prompts.c2_extraction_prompt(packet["doc"], blocks, n), task="qg-c2")
        except Exception as error:
            print(f"[questiongen] C2 {packet.get('packet_id')} failed: {error}", file=sys.stderr)
            return []
        return _parse_list(raw)
    return _over_pool(packets, one)


def _task_enum(packets: list[dict[str, Any]], provider: QuestionGenProvider, rng: random.Random) -> list[dict[str, Any]]:
    """C2E supplement (2026-10-16): enumeration/aggregation/point-control
    questions over table sections — the question side of the fold families.
    Section packets are few (~30), so no packet limit applies here."""
    section_packets = [p for p in packets if p.get("kind") == "table_section"]
    rng.shuffle(section_packets)
    items: list[dict[str, Any]] = []
    for packet in section_packets:
        try:
            raw = provider.generate_json(prompts.enumeration_prompt(packet, 5), task="qg-enum")
        except Exception as error:
            print(f"[questiongen] C2E {packet.get('packet_id')} failed: {error}", file=sys.stderr)
            continue
        items.extend(_parse_list(raw))
    return items


def _task_c3(packets: list[dict[str, Any]], provider: QuestionGenProvider, rng: random.Random) -> list[dict[str, Any]]:
    def one(packet: dict[str, Any]) -> list[dict[str, Any]]:
        blocks = _paraphrase_blocks(packet.get("blocks", []))
        if not blocks:
            return []
        try:
            raw = provider.generate_json(prompts.c3_paraphrase_prompt(packet["doc"], blocks, 2), task="qg-c3")
        except Exception as error:
            print(f"[questiongen] C3 {packet.get('packet_id')} failed: {error}", file=sys.stderr)
            return []
        return _parse_list(raw)
    return _over_pool(packets, one)


def _task_c4(packets: list[dict[str, Any]], provider: QuestionGenProvider, rng: random.Random) -> list[dict[str, Any]]:
    def one(packet: dict[str, Any]) -> list[dict[str, Any]]:
        if packet.get("kind") == "series_pair":
            materials = (f"新版：{json.dumps(packet['latest']['doc'], ensure_ascii=False)[:200]}\n"
                         + "\n".join(b["text"][:200] for b in packet["latest"]["blocks"][:4])
                         + f"\n旧版：{json.dumps(packet['older']['doc'], ensure_ascii=False)[:200]}\n"
                         + "\n".join(b["text"][:200] for b in packet["older"]["blocks"][:4]))
            note = "这是同一系列的新旧两个版本。"
        elif packet.get("kind") == "conflict_pair":
            materials = "\n\n".join(
                f"方{i}: {json.dumps(m['doc'], ensure_ascii=False)[:200]}\n" + "\n".join(b["text"][:200] for b in m["blocks"][:4])
                for i, m in enumerate(packet["members"], 1)
            )
            note = "这两份文档对同一事实存在分歧。"
        else:
            blocks = [b for b in packet.get("blocks", []) if len(str(b.get("text", ""))) > 40][:6]
            if len(blocks) < 2:
                return []
            materials = (f"同页材料：{json.dumps(packet['doc'], ensure_ascii=False)[:200]}\n"
                         + "\n".join(f"- {b['text'][:250]}" for b in blocks))
            note = "这是同一页面的多个相关块。"
        try:
            raw = provider.generate_json(prompts.c4_multihop_prompt(materials, 2, note), task="qg-c4")
        except Exception as error:
            print(f"[questiongen] C4 {packet.get('packet_id')} failed: {error}", file=sys.stderr)
            return []
        return _parse_list(raw)
    return _over_pool(packets, one)


def _task_c5(packets: list[dict[str, Any]], provider: QuestionGenProvider, rng: random.Random) -> list[dict[str, Any]]:
    def one(packet: dict[str, Any]) -> list[dict[str, Any]]:
        if packet.get("kind") != "doc":
            return []
        blocks = _rule_blocks(packet.get("blocks", []))
        if not blocks:
            return []
        try:
            raw = provider.generate_json(prompts.c5_safety_prompt(packet["doc"], blocks, 2), task="qg-c5")
        except Exception as error:
            print(f"[questiongen] C5 {packet.get('packet_id')} failed: {error}", file=sys.stderr)
            return []
        return _parse_list(raw)
    return _over_pool(packets, one)


def generate_level(corpus: Corpus, provider: QuestionGenProvider, level: str, *, seed: int = 20261015,
                   limit_packets: int | None = None) -> list[dict[str, Any]]:
    """Run one level task; returns raw drafted items (un-normalized)."""
    rng = random.Random(seed + hash(level) % 1000)
    packets = build_packets(corpus)
    if limit_packets:
        packets = packets[:limit_packets]
    if level == "C1":
        return _task_c1(corpus, provider, rng)
    tasks = {"C2": _task_c2, "C3": _task_c3, "C4": _task_c4, "C5": _task_c5}
    if level == "C2":
        # section supplement rides inside C2 (2026-10-16 user ruling:
        # new question shapes belong to the existing five-dimension
        # matrix — C2 quantity cell — never a new level of its own.
        return _task_c2(packets, provider, rng) + _task_enum(packets, provider, rng)
    if level not in tasks:
        raise ValueError(f"unknown level {level}")
    return tasks[level](packets, provider, rng)


def finalize_level(raw_items: list[dict[str, Any]], corpus: Corpus, *, default_today: str) -> list[dict[str, Any]]:
    """normalize → gold expansion → cleanup, shared across levels."""
    rows = []
    for n, item in enumerate(raw_items, 1):
        row = normalize_row(item, qid=f"qg-{n:04d}", packet_kind=str(item.get("level", "?")).lower(),
                            source_doc=str(item.get("doc_uid", "")), default_today=default_today)
        if row is not None:
            rows.append(row)
    expanded, _ = _expand_gold(rows, corpus)
    cleaned, _ = _cleanup(expanded)
    return cleaned


def _expand_gold(rows: list[dict[str, Any]], corpus: Corpus) -> tuple[list[dict[str, Any]], int]:
    """Expand each gold quote to all holder docs (≤3) or drop the row (>3)."""
    finder = QuoteFinder(corpus.blocks)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        if "__error__" in row or not row.get("gold"):
            kept.append(row)
            continue
        expanded: list[dict[str, Any]] = []
        ok = True
        seen_docs: set[str] = set()
        for anchor in row["gold"]:
            key = match_key(str(anchor.get("quote", "")))
            holders = {corpus.canonical(h) for h in finder.docs(key)} if key else set()
            holders = holders or {corpus.canonical(anchor["doc_uid"])}
            if len(holders) > 3:
                ok = False
                break
            for holder in sorted(holders):
                if holder in seen_docs:
                    continue
                seen_docs.add(holder)
                expanded.append({"doc_uid": holder, "quote": anchor["quote"], "block_id": anchor.get("block_id", "") if holder == anchor.get("doc_uid") else ""})
        if ok and expanded:
            row["gold"] = expanded
            kept.append(row)
        else:
            dropped += 1
    return kept, dropped


def _cleanup(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Structural cleanup only (user ruling 2026-10-15: value constraints live
    in the PROMPTS — no hard answer-leak/numeric guards here): drop exact
    duplicate questions, demote broken families, strip family on C1/C5."""
    stats = {"dup_dropped": 0, "family_demoted": 0}
    kept: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for row in rows:
        if "__error__" in row:
            kept.append(row)
            continue
        key = match_key(str(row.get("question", "")))
        if key and key in seen_questions:
            stats["dup_dropped"] += 1
            continue
        seen_questions.add(key)
        if row.get("level") in ("C1", "C5"):
            row.pop("family", None)
        kept.append(row)
    fam_gold: dict[str, tuple[str, ...]] = {}
    fam_seen: set[str] = set()
    for row in kept:
        fam = row.get("family")
        if not fam:
            continue
        docs = tuple(sorted({g.get("doc_uid", "") for g in (row.get("gold") or [])}))
        if fam not in fam_seen:
            fam_seen.add(fam)
            fam_gold[fam] = docs
        elif docs != fam_gold[fam]:
            row.pop("family", None)
            stats["family_demoted"] += 1
    members: dict[str, int] = {}
    for row in kept:
        if row.get("family"):
            members[row["family"]] = members.get(row["family"], 0) + 1
    for row in kept:
        fam = row.get("family")
        if fam and members.get(fam, 0) < 2:
            row.pop("family", None)
            stats["family_demoted"] += 1
    return kept, stats


def write_rows(rows: list[dict[str, Any]], out_path: Path) -> dict[str, int]:
    good = [r for r in rows if "__error__" not in r]
    main, variants = _split_families(good)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    variant_path = out_path.with_name(out_path.stem + "_variants.jsonl")
    for path, items in ((out_path, main), (variant_path, variants)):
        with path.open("w", encoding="utf-8") as handle:
            for row in items:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _count(items: list[dict[str, Any]], field: str) -> dict[str, int]:
        bucket: dict[str, int] = {}
        for row in items:
            bucket[str(row.get(field, "?"))] = bucket.get(str(row.get(field, "?")), 0) + 1
        return bucket

    return {"rows": len(main), "variants": len(variants), "by_level": _count(main, "level"),
            "by_language": _count(main, "language")}


def _split_families(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Family variants go to a sibling file (frozen lint: unique
    (canonical-doc, quote) pairs per file)."""
    main: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    seen_family: set[str] = set()
    seen_pair: set[tuple[str, str]] = set()
    for row in rows:
        fam = row.get("family") or ""
        pairs = {(g.get("doc_uid", ""), match_key(str(g.get("quote", "")))) for g in (row.get("gold") or row.get("trap_gold") or [])}
        if fam and fam in seen_family:
            row["variant_of"] = fam
            variants.append(row)
            continue
        if not pairs or not (pairs & seen_pair):
            main.append(row)
            seen_family.add(fam)
            seen_pair |= pairs
        else:
            continue
    return main, variants
