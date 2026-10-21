"""Oracle evidence package (P4, second half): hand the question either the
gold evidence pack (oracle) or the retrieved pack (active_ir top-8) and let a
generator answer, then measure citation faithfulness and groundedness.

The gap between the two packs isolates how much of the final-answer error
comes from evidence retrieval/selection versus from the generation stage.

Usage:
  PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m eval.oracle_pack \
      --corpus data/corpus/20261013-221420-a3fa1c \
      --eval data/benchmarks/corpus-eval/eval_domain_48.jsonl \
      --today 2026-10-13 --limit 39 --out <dir>

Model: deepseek-flash via .env (thinking disabled), per user authorization.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from evidence.store import match_key, read_jsonl
from evidence.query_ir import parse_query
from evalsuite.harness import quote_hit
from runtime.indexing import load_index
from retrieve.lexical import corpus_hit, ir_rerank
from annotate.runner import load_env

PROMPT = """你是教务问答助手。仅依据下面给出的证据回答问题；每个事实后标注依据的证据编号，格式如 [E1]。
如果证据不足以回答，直接回答"证据不足"。不要编造证据中没有的日期、数字、人名或条件。

{evidence_block}

问题：{question}"""


def _complete_text(question: str, evidence_block: str, env: dict[str, str], model: str = "") -> str:
    import httpx

    body = {
        "model": model or env.get("LLM_MODEL", "deepseek-v4-flash-780k-local"),
        "messages": [{"role": "user", "content": PROMPT.format(evidence_block=evidence_block, question=question)}],
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    for attempt in range(3):
        try:
            # Campus gateway ONLY (user ruling 2026-10-16): the historical
            # api.deepseek.com fallback silently billed the official API
            # whenever LLM_BASE_URL was missing from the environment.
            response = httpx.post(
                f"{env.get('LLM_BASE_URL', 'https://your-gateway.example.com/v1')}/chat/completions",
                headers={"Authorization": f"Bearer {env.get('LLM_API_KEY') or env.get('QUESTIONGEN_API_KEY', '')}"},
                json=body, timeout=float(env.get("LLM_TIMEOUT_SECONDS", "120")),
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"] or ""
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return ""


def _complete_local(question: str, evidence_block: str, generator) -> str:
    return generator.generate(
        system="你是教务问答助手。",
        user=PROMPT.format(evidence_block=evidence_block, question=question),
        max_new_tokens=800,
    )


def _pack_items(items: list[dict[str, Any]]) -> str:
    lines = []
    for i, item in enumerate(items, 1):
        meta = " ".join(
            part for part in [
                f"文档《{item.get('title', '')}》",
                f"学期={','.join(item.get('terms') or [])}" if item.get("terms") else "",
                f"对象={item.get('audience', '')}" if item.get("audience") else "",
            ] if part
        )
        lines.append(f"[E{i}] {meta}\n{item['text'][:900]}")
    return "\n\n".join(lines)


def _gold_items(corpus, quotes_by_doc: dict[str, list[str]], blocks_by_doc) -> list[dict[str, Any]]:
    items = []
    for uid, quotes in quotes_by_doc.items():
        doc = corpus.documents.get(uid, {})
        hit_block = next(
            (b for b in blocks_by_doc.get(uid, []) if any(quote_hit(b["text"], q) for q in quotes)),
            None,
        )
        if hit_block is None:
            continue
        items.append({
            "title": doc.get("title", ""), "terms": doc.get("terms"), "audience": doc.get("audience"),
            "text": hit_block["text"],
        })
    return items[:8]


def _retrieved_items(corpus, index, question: str, today) -> list[dict[str, Any]]:
    triples = ir_rerank(corpus, index, question, k=10, today=today)
    items = []
    for i, _score, _hit in triples[:8]:
        unit = index.units[i]
        doc = corpus.documents.get(corpus.canonical(unit.doc_uid), {})
        items.append({
            "title": doc.get("title", ""), "terms": doc.get("terms"), "audience": doc.get("audience"),
            "text": unit.text,
        })
    return items


def _metrics(answer: str, items: list[dict[str, Any]], quotes_by_doc, question: str = "") -> dict[str, Any]:
    cites = any(f"[E{i}]" in answer for i in range(1, len(items) + 1))
    # Groundedness, paraphrase-tolerant: every digit run in the answer (dates,
    # credits, course numbers, prices) must occur in the pack — a fabricated
    # number almost never matches, a faithful paraphrase keeps its numbers.
    import re
    # Citation marks [E1][E2] inject digits that are not content — strip them
    # before extracting digit runs, or every citing answer fails fidelity.
    clean_answer = re.sub(r"\[E\d+\]", "", answer)
    pack_digits = set(re.findall(r"\d+", match_key("\n".join(item["text"] for item in items))))
    answer_digits = set(re.findall(r"\d+", match_key(clean_answer)))
    question_digits = set(re.findall(r"\d+", match_key(question)))
    novel_digits = answer_digits - question_digits
    numeric_fidelity = bool(not novel_digits or novel_digits <= pack_digits)
    # Light verbatim anchor: an 8-char normalized slice of a gold quote.
    quote_keys = [match_key(q) for quotes in quotes_by_doc.values() for q in quotes]
    grounded = any(q and len(q) >= 8 and (q[:8] in match_key(answer) or q[-8:] in match_key(answer)) for q in quote_keys)
    return {"cites_evidence": cites, "numeric_fidelity": numeric_fidelity, "grounded": grounded, "refused": "证据不足" in answer, "answer_len": len(answer)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--eval", type=Path, required=True)
    parser.add_argument("--today", default=None)
    parser.add_argument("--limit", type=int, default=0, help="cap answerable questions (0 = all)")
    parser.add_argument("--arm", default="active_ir", help="retrieval arm for the retrieved pack, e.g. active_ir / active_llm_metadata_ir")
    parser.add_argument("--generator", default="api", choices=("api", "local"), help="P5: api = .env LLM (optionally --model), local = LOCAL_ANNOTATE_MODEL via transformers")
    parser.add_argument("--model", default="", help="override the api generator model alias (third-model experiments)")
    parser.add_argument("--only-verified", action="store_true", help="skip items without verified: true")
    parser.add_argument("--out", type=Path, default=Path("data/benchmarks/corpus-eval/results/oracle_pack"))
    args = parser.parse_args(argv)

    from retrieve.lexical import Corpus
    corpus = Corpus(args.corpus)
    index = load_index(corpus, args.arm)
    blocks_by_doc: dict[str, list[dict]] = {}
    for block in read_jsonl(args.corpus / "blocks.jsonl"):
        blocks_by_doc.setdefault(block["doc_uid"], []).append(block)

    env = load_env()
    generator = None
    gen_provenance: dict[str, Any] = {}
    if args.generator == "local":
        from generate.local import LocalGenerator

        generator = LocalGenerator()
        gen_provenance = generator.provenance()
        print(f"[oracle_pack] local generator: {gen_provenance['model']} on {gen_provenance['sampling']['device']}", file=sys.stderr)
    else:
        gen_provenance = {"model": args.model or env.get("LLM_MODEL", "deepseek-flash"),
                          "base_url": env.get("LLM_BASE_URL", ""), "sampling": {"temperature": 0, "max_tokens": 800, "thinking": "disabled"}}

    def complete(question: str, evidence_block: str) -> str:
        if generator is not None:
            return _complete_local(question, evidence_block, generator)
        return _complete_text(question, evidence_block, env, model=args.model)

    rows = []
    for item in read_jsonl(args.eval):
        if not item.get("gold"):
            continue
        if args.only_verified and not item.get("verified"):
            continue
        if args.limit and len(rows) >= args.limit:
            break
        today = date.fromisoformat(item["today"]) if item.get("today") else (date.fromisoformat(args.today) if args.today else None)
        quotes_by_doc: dict[str, list[str]] = {}
        for g in item["gold"]:
            quotes_by_doc.setdefault(corpus.canonical(g["doc_uid"]), []).append(g["quote"])
        gold_items = _gold_items(corpus, quotes_by_doc, blocks_by_doc)
        retrieved_items = _retrieved_items(corpus, index, item["question"], today)
        row = {"id": item["id"], "question": item["question"], "gold_pack_size": len(gold_items), "retrieved_pack_size": len(retrieved_items)}
        for name, items in (("gold", gold_items), ("retrieved", retrieved_items)):
            if not items:
                row[name] = {"error": "empty pack"}
                continue
            answer = complete(item["question"], _pack_items(items))
            row[name] = {"answer": answer[:500], **_metrics(answer, items, quotes_by_doc, item["question"])}
            print(f"[{item['id']}] {name}: cites={row[name]['cites_evidence']} grounded={row[name]['grounded']} numfid={row[name]['numeric_fidelity']} refused={row[name]['refused']}", file=sys.stderr)
        rows.append(row)

    args.out.mkdir(parents=True, exist_ok=True)
    tag = f"{args.generator}" + (f"-{args.model}" if args.model else "")
    out_file = args.out / f"oracle_pack__{tag}__{args.arm}__{corpus.build_id}.json"
    payload = {"meta": {"corpus": str(args.corpus), "build_id": corpus.build_id, "eval": str(args.eval),
                        "arm": args.arm, "generator": gen_provenance, "n": len(rows)},
               "rows": rows}
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    for name in ("gold", "retrieved"):
        valid = [r for r in rows if isinstance(r.get(name), dict) and "cites_evidence" in r[name]]
        n = len(valid)
        print(f"[{tag}] {name}: n={n} cites={sum(r[name]['cites_evidence'] for r in valid)/max(1,n):.3f} "
              f"grounded={sum(r[name]['grounded'] for r in valid)/max(1,n):.3f} "
              f"refused={sum(r[name]['refused'] for r in valid)/max(1,n):.3f} numfid={sum(r[name]['numeric_fidelity'] for r in valid)/max(1,n):.3f}")
    print(f"→ {out_file}")
    if generator is not None:
        generator.close()


if __name__ == "__main__":
    main()
