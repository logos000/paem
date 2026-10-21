"""P1: full-corpus material packets — every active page, plus combination pairs.

Unlike eval/candidates.py (sampled drafting batches), this walks EVERY active
document so generation coverage is per-page, and adds three combination kinds:

``series_pair``    latest + one superseded edition of a version chain
                   (L3 version-comparison questions)
``conflict_pair``  build-detected fact-conflict candidates
                   (L3 conflict questions, expected flag_conflict)
``doc``            a single page (L1 single-block, L2 in-page combos, L4 traps)
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evalsuite.candidates import FACT_RE, META_KEYS, informative
from evidence.pack import _fact_slots
from retrieve.lexical import Corpus

MAX_BLOCKS = 14


def _blocks_view(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "block_id": block["block_id"],
        "ordinal": block.get("ordinal", 0),
        "kind": block["kind"],
        "heading_path": block.get("heading_path") or [],
        "text": block["text"],
        "facts": {key: sorted(values) for key, values in _fact_slots(block["text"]).items() if values},
    }


def build_packets(corpus: Corpus, *, max_blocks: int = MAX_BLOCKS) -> list[dict[str, Any]]:
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in corpus.blocks:
        by_doc[block["doc_uid"]].append(block)

    series_latest: dict[str, str] = {}
    for uid, doc in corpus.documents.items():
        if doc.get("series_id") and doc.get("is_latest") and doc.get("status") == "active":
            series_latest[doc["series_id"]] = uid
    series_docs: set[str] = {uid for uid, doc in corpus.documents.items() if doc.get("series_id")}

    packets: list[dict[str, Any]] = []
    for uid, doc in sorted(corpus.documents.items()):
        if doc.get("status") != "active" or uid not in by_doc:
            continue
        blocks = by_doc[uid]
        good = [b for b in blocks if informative(b)]
        if len(good) < 2:
            continue  # nothing question-worthy (boilerplate pages etc.)
        chosen = good[: max_blocks - 2]
        context = [b for b in blocks if b not in chosen][:2]
        packets.append({
            "packet_id": f"pkt-{uid[:16]}",
            "kind": "doc",
            "doc": {key: doc.get(key) for key in META_KEYS},
            "blocks": [_blocks_view(b) for b in chosen + context],
        })

    # table-section packets (2026-10-16): documents whose answer lives in a
    # contiguous table-row SECTION (curricula / class-number lists / exam
    # schedules / prerequisites).  These mirror the runtime's fold families
    # (runtime/engine.py) — deliberately compact local patterns so this
    # module stays independent of the builder's engine.
    import re as _re
    _SECTION_ROW_RES = (
        ("curriculum", _re.compile(r"Course Code\s*[:：]", _re.I)),
        ("class_number", _re.compile(r"^[A-Z]{2,5} ?\d{2,4}[A-Za-z0-9*]*\s*\|")),
        ("exam_schedule", _re.compile(r"^No\.\s*:?\s*\d+\s*\|", _re.I)),
        ("prerequisites", _re.compile(r"^Number:\s*[A-Z]{2,5} ?\d", _re.I)),
    )
    for uid, doc in sorted(corpus.documents.items()):
        if doc.get("status") != "active" or uid not in by_doc:
            continue
        blocks = sorted(by_doc[uid], key=lambda b: b.get("ordinal", 0))
        for section_kind, row_re in _SECTION_ROW_RES:
            best_lo = best_hi = cursor = 0
            for index, block in enumerate(blocks):
                text = str(block.get("text") or "")
                if block.get("kind") == "table_row" and row_re.search(text):
                    if cursor == 0:
                        best_lo = index
                    cursor += 1
                    best_hi = index + 1
                else:
                    cursor = 0
            if best_hi - best_lo < 8:
                continue
            label = ""
            for back in range(best_lo - 1, max(-1, best_lo - 4), -1):
                if blocks[back].get("kind") == "paragraph":
                    label = str(blocks[back].get("text") or "")[:200]
                    if _re.search(r"required courses", label, _re.I):
                        break
            rows = [str(b.get("text") or "") for b in blocks[best_lo:best_hi]][:30]
            packets.append({
                "packet_id": f"pkt-sec-{section_kind}-{uid[:12]}",
                "kind": "table_section",
                "section_kind": section_kind,
                "doc": {key: doc.get(key) for key in META_KEYS},
                "label": label,
                "block_ids": [b.get("block_id") for b in blocks[best_lo:best_hi]][:30],
                "rows": rows,
            })
            break  # one packet per document (first matching family)

    # series pairs: latest + the most recent superseded edition
    series_rows = []
    series_path = corpus.root / "series.json"
    if series_path.is_file():
        series_rows = json.loads(series_path.read_text(encoding="utf-8"))
    for row in series_rows:
        sid = row.get("series_id")
        latest = series_latest.get(sid or "")
        if not latest or latest not in by_doc:
            continue
        older = [m["doc_uid"] for m in row.get("members", []) if m["doc_uid"] != latest and m["doc_uid"] in by_doc]
        if not older:
            continue
        old = older[-1]
        def _mini(uid: str, tag: str) -> dict[str, Any]:
            good = [b for b in by_doc[uid] if informative(b)][:6]
            doc = corpus.documents[uid]
            return {"tag": tag, "doc": {key: doc.get(key) for key in META_KEYS}, "blocks": [_blocks_view(b) for b in good]}
        packets.append({
            "packet_id": f"pkt-series-{str(sid)[:16]}",
            "kind": "series_pair",
            "series_key": row.get("key", ""),
            "latest": _mini(latest, "latest"),
            "older": _mini(old, "older"),
        })

    # conflict pairs from build-time candidates
    conflict_path = corpus.root / "conflict_candidates.jsonl"
    if conflict_path.is_file():
        for line in conflict_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            members = [v.get("doc_uid") for v in cand.get("values", []) if isinstance(v, dict) and v.get("doc_uid")]
            members = [m for m in members if m in by_doc and m][:2]
            if len(members) < 2:
                continue
            packets.append({
                "packet_id": f"pkt-conflict-{str(cand.get('series_id', ''))[:12]}-{members[0][:8]}",
                "kind": "conflict_pair",
                "fact_kind": cand.get("fact_kind", ""),
                "members": [_mini(m, f"side{i}") for i, m in enumerate(members)],
            })

    return packets
