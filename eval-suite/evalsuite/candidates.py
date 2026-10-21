"""Sample seed packets from a corpus build for drafting eval questions.

A packet is one document (title, metadata) plus a short window of its
evidence blocks.  Question writers (human or LLM) must quote a block
verbatim, so every drafted question carries a resolvable ``{doc_uid, quote}``
anchor.  Packets are stratified so the eval exercises the evidence layer:

``core``      SharePoint / ZJUI pages and attachments (facts, tables, lists)
``ugrs``      undergraduate-school notices and regulations
``grs``       graduate-school documents (audience distractors for ZJUI students)
``series``    latest + one superseded edition of a version chain
``general``   university-level pages (zju, news, libweb)

Output: ``<out>/batch_XX.json`` (lists of packets), all under gitignored data/.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import random
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

from evalsuite import builder_root  # noqa: E402
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retrieve.lexical import Corpus  # noqa: E402

QUOTAS = {"core": 80, "ugrs": 50, "grs": 30, "series": 30, "general": 15}
META_KEYS = ("doc_uid", "site", "title", "published", "doc_type", "audience", "terms", "academic_years", "cohorts", "series_id", "is_latest", "source_url")
FACT_RE = re.compile(r"\d|月|日|学分|截止|deadline|credit|email|@|申请|apply|required|须|应当|不得")


def informative(block: dict[str, Any]) -> bool:
    text = block["text"]
    return 20 <= len(text) <= 600 and block["kind"] != "heading" and bool(FACT_RE.search(text))


def packet(corpus: Corpus, uid: str, blocks: list[dict[str, Any]], stratum: str, rng: random.Random, size: int = 10) -> dict[str, Any] | None:
    good = [i for i, b in enumerate(blocks) if informative(b)]
    if len(good) < 2:
        return None
    seed = rng.choice(good)
    start = max(0, min(seed - size // 3, len(blocks) - size))
    window = blocks[start:start + size]
    doc = corpus.documents[uid]
    return {
        "stratum": stratum,
        "doc": {key: doc.get(key) for key in META_KEYS},
        "blocks": [{"block_id": b["block_id"], "kind": b["kind"], "heading_path": b.get("heading_path") or [], "text": b["text"]} for b in window],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "seed_blocks")
    parser.add_argument("--batches", type=int, default=5)
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    corpus = Corpus(args.corpus) if args.corpus else Corpus.latest(builder_root() / "data" / "corpus")
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in corpus.blocks:
        by_doc[block["doc_uid"]].append(block)
    active = {uid: doc for uid, doc in corpus.documents.items() if doc.get("status") == "active" and uid in by_doc}

    pools: dict[str, list[str]] = defaultdict(list)
    for uid, doc in active.items():
        if doc.get("series_id"):
            continue  # sampled through the series stratum
        site = doc["site"]
        if site in ("sharepoint", "zjui") and doc.get("tier") in ("core", "support"):
            pools["core"].append(uid)
        elif site == "ugrs" and doc.get("doc_type") not in ("news", "page"):
            pools["ugrs"].append(uid)
        elif site == "grs" and doc.get("doc_type") in ("notice", "regulation", "guide", "calendar"):
            pools["grs"].append(uid)
        elif site in ("zju", "news", "libweb") and doc.get("doc_type") != "news":
            pools["general"].append(uid)

    packets: list[dict[str, Any]] = []
    for stratum in ("core", "ugrs", "grs", "general"):
        pool = sorted(pools[stratum])
        rng.shuffle(pool)
        taken = 0
        for uid in pool:
            if taken >= QUOTAS[stratum]:
                break
            item = packet(corpus, uid, by_doc[uid], stratum, rng)
            if item:
                packets.append(item)
                taken += 1

    series = json.loads((corpus.root / "series.json").read_text(encoding="utf-8"))
    # Prefer ZJUI-relevant and undergraduate series, then the rest.
    def series_rank(row: dict[str, Any]) -> tuple[int, float]:
        return ({"sharepoint": 0, "zjui": 0, "ugrs": 1}.get(row["site"], 2), rng.random())
    series = sorted((s for s in series if s["size"] >= 2 and not s.get("ambiguous_latest") and s["latest"] in active), key=series_rank)
    taken = 0
    for row in series:
        if taken >= QUOTAS["series"]:
            break
        older = [m["doc_uid"] for m in row["members"] if m["doc_uid"] != row["latest"] and m["doc_uid"] in by_doc]
        if not older:
            continue
        latest = packet(corpus, row["latest"], by_doc[row["latest"]], "series", rng)
        old = packet(corpus, older[-1], by_doc[older[-1]], "series", rng)
        if latest and old:
            packets.append({"stratum": "series", "series_id": row["series_id"], "series_key": row["key"],
                            "members": [{"doc_uid": m["doc_uid"], "title": m["title"], "terms": m.get("terms", [])} for m in row["members"]],
                            "latest": latest, "older": old})
            taken += 1

    rng.shuffle(packets)
    args.out.mkdir(parents=True, exist_ok=True)
    for i in range(args.batches):
        batch = packets[i::args.batches]
        (args.out / f"batch_{i:02d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = defaultdict(int)
    for item in packets:
        counts[item["stratum"]] += 1
    print(json.dumps({"build_id": corpus.build_id, "packets": len(packets), "by_stratum": counts, "batches": args.batches}, ensure_ascii=False))


if __name__ == "__main__":
    main()
