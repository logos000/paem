"""P3: generated-question lint — a companion to eval/lint.py (which stays frozen).

Validates the five generated-only fields, family invariants, layer↔category
consistency, cross-set dedup against the existing eval files, and a direct
answer-leak check.  Run AFTER eval.lint passes; exits non-zero on errors.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from evidence.store import match_key, read_jsonl
from evalsuite.questiongen.schemas import INTENTS, LANGUAGES, LAYERS, LAYER_CATEGORIES, LEVELS, LEVEL_CATEGORIES, TONES, TRAP_BEHAVIORS, leaks_answer


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text)
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, len(a | b))


def lint_generated(items: list[dict[str, Any]], *, existing_files: list[Path] | None = None,
                   jaccard_threshold: float = 0.8) -> list[str]:
    errors: list[str] = []
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)

    existing_norms: set[str] = set()
    existing_grams: list[tuple[str, set[str]]] = []
    for path in existing_files or []:
        if not Path(path).is_file():
            continue
        for row in read_jsonl(path):
            q = row.get("question", "")
            existing_norms.add(match_key(q))
            existing_grams.append((str(row.get("id", "")), _bigrams(q)))

    for item in items:
        ident = item.get("id", "?")
        err = lambda msg: errors.append(f"{ident}: {msg}")  # noqa: E731

        for field, allowed in (("language", LANGUAGES), ("tone", TONES), ("layer", LAYERS), ("intent", INTENTS), ("level", LEVELS)):
            value = item.get(field)
            if value is not None and value not in allowed:
                err(f"{field}={value!r} not in {list(allowed)}")
        layer = item.get("layer")
        level = item.get("level")
        category = item.get("category", "")
        gold = item.get("gold") or []
        if level:
            if category and category not in LEVEL_CATEGORIES.get(level, ()):
                err(f"category {category!r} inconsistent with level {level}")
            if level == "C1" and (gold or item.get("trap_gold")):
                err("C1 must carry no corpus anchors (gold/trap_gold empty)")
            if level == "C2" and not gold:
                err("C2 extraction must carry a gold anchor")
            if level == "C3" and item.get("golden_answer") and match_key(str(item["golden_answer"])) in {
                match_key(str((gold or [{}])[0].get("quote", "")))
            }:
                err("C3 golden_answer must paraphrase, not copy the quote")
            if level == "C4":
                if len({match_key(str(g.get("quote", ""))) for g in gold if g.get("quote")}) < 2:
                    err("C4 multi-hop requires two distinct premise quotes, not duplicate holders")
                if any(re.search(r"\.{4,}", str(g.get("quote", ""))) or str(g.get("block_id", "")).startswith("toc") for g in gold):
                    err("C4 cannot use table-of-contents lines as answer evidence")
        if layer == "L4":
            if gold:
                err("L4 trap must use gold: []")
            if item.get("expected_behavior") not in set(TRAP_BEHAVIORS.values()):
                err(f"L4 expected_behavior must be one of {sorted(set(TRAP_BEHAVIORS.values()))}")
            if category not in TRAP_BEHAVIORS:
                err(f"L4 category {category!r} must be one of {sorted(TRAP_BEHAVIORS)}")
        elif layer in LAYER_CATEGORIES and category and category not in LAYER_CATEGORIES[layer]:
            err(f"category {category!r} inconsistent with layer {layer}")
        if item.get("family"):
            families[item["family"]].append(item)
        # answer leak: answer-carrying values from gold must not appear verbatim
        if leaks_answer(str(item.get("question", "")), gold + list(item.get("trap_gold") or [])):
            err("question leaks an answer value from the gold quote")
        # dedup against existing eval sets
        key = match_key(str(item.get("question", "")))
        if key and key in existing_norms:
            err("duplicate of an existing eval question")
        grams = _bigrams(str(item.get("question", "")))
        for other_id, other_grams in existing_grams:
            if _jaccard(grams, other_grams) >= jaccard_threshold:
                err(f"near-duplicate of existing {other_id}")
                break

    # family invariants
    for fam, members in families.items():
        if len(members) < 2:
            errors.append(f"{members[0].get('id', '?')}: family {fam!r} has a single member (need >=2)")
            continue
        gold_sets = {tuple(sorted({a.get("doc_uid", "") for a in (m.get("gold") or [])})) for m in members}
        if len(gold_sets) > 1:
            errors.append(f"family {fam!r} members disagree on gold docs: {gold_sets}")
        variants = {(m.get("language"), m.get("tone"), match_key(m.get("question", ""))) for m in members}
        if len(variants) != len(members):
            errors.append(f"family {fam!r} has identical variants")
    # in-file duplicates
    seen: dict[str, str] = {}
    for item in items:
        key = match_key(str(item.get("question", "")))
        if key in seen:
            errors.append(f"{item.get('id', '?')}: duplicate of {seen[key]} in this file")
        elif key:
            seen[key] = str(item.get("id", "?"))
    return errors
